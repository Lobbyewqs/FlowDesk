"""显式调用真实模型，验证 HTTP 接口到模拟业务执行的完整流程。"""
import json
import sys
import secrets
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from flowdesk.config import Settings
from flowdesk.main import create_app


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def post(client, url, body=None):
    response = client.post(url, json=body)
    check(response.status_code in {200, 201, 202}, "接口未返回成功状态")
    ticket = response.json()
    deadline = time.monotonic() + 120
    while ticket["status"] in {"queued", "processing"} or ticket.get("job"):
        check(time.monotonic() < deadline, "后台任务超过验收时间限制")
        time.sleep(.05)
        ticket = client.get(f"/api/tickets/{ticket['id']}").json()
    check(ticket["status"] != "failed", "真实模型工作流失败；检查模型服务连接和兼容性")
    return ticket


def verify():
    settings = Settings.load()
    check(settings.mode == "live", "请先配置 live 模式")
    run_id = uuid4().hex[:12]
    rows = []
    approve = {"approved": True, "note": "真实模型验收：批准模拟业务操作", "reply": ""}
    cases = [
        ("换货与否定表达、重启恢复、重复审批", "订单A1024耳机左耳没声音。我不要退货，我要换货。", "exchange", "exchange"),
        ("退货同义表达", "订单A1028的支架想退回这件商品，请帮我处理。", "return", "return"),
        ("缺少订单号后补充", "耳机左耳没声音，我想换货，订单号暂时没找到。", "exchange", "information"),
        ("物流咨询审核归档", "订单A1025还没收到，查一下快递到哪了。", "logistics", "logistics"),
        ("人工驳回", "订单A1024的耳机需要换货。", "exchange", "reject"),
        ("超期退货转人工", "订单A1026的音箱不想要了，请申请退货。", "return", "manual"),
        ("订单归属不符", "订单A1027的手表想换货。", "exchange", "manual"),
        ("不支持的业务转人工", "订单A1024帮我开发票。", "other", "manual"),
    ]
    for index, (name, description, intent, behavior) in enumerate(cases):
        case_settings = replace(settings, data_dir=ROOT / ".tmp" / f"live-{run_id}" / str(index))
        start = perf_counter()
        password = secrets.token_urlsafe(24)
        try:
            with TestClient(create_app(case_settings)) as client:
                check(client.post('/api/auth/setup', json={'username':'liveadmin','password':password}).status_code == 201,
                      '初始化验收账号失败')
                ticket = post(client, "/api/tickets", {"customer_id": "C001", "description": description})
                ticket_id = ticket["id"]
                check(ticket["state"]["intent"] == intent, "模型识别类型与预期不一致")
                if behavior == "information":
                    check(ticket["status"] == "needs_info", "缺少订单号时没有暂停")
                    ticket = post(client, f"/api/tickets/{ticket_id}/information", {"text": "找到订单号了，是A1024"})
                if behavior == "manual":
                    check(ticket["status"] == "manual" and ticket["action"] is None, "不适用申请未正确转人工")
                    if index == 6:
                        check(ticket["state"]["order"] is None, "订单归属隔离失败")
                else:
                    check(ticket["status"] == "awaiting_approval", "没有暂停等待审核")
                    check(ticket["state"]["policies"] and ticket["state"]["draft"].strip(), "缺少政策或真实回复")
                    check(ticket["state"]["input_tokens"] > 0 and ticket["state"]["output_tokens"] > 0, "供应商未返回模型用量证据")
                    if behavior == "reject":
                        ticket = post(client, f"/api/tickets/{ticket_id}/approval",
                                      {"approved": False, "note": "需人工核验故障情况", "reply": ""})
                        check(ticket["status"] == "rejected" and ticket["action"] is None, "驳回后仍创建业务记录")
                    elif index != 0:
                        ticket = post(client, f"/api/tickets/{ticket_id}/approval", approve)
                        check(ticket["status"] == "completed", "审批后未完成归档")
                        if behavior == "logistics":
                            check(ticket["action"] is None and "分拨" in ticket["state"]["reply"], "物流回复与记录不一致")
                        else:
                            check(ticket["action"]["kind"] == intent, "模拟售后类型不一致")
            if index == 0:
                # 关闭并重建整个应用，恢复持久化的真实模型审批工单。
                with TestClient(create_app(case_settings)) as client:
                    check(client.post('/api/auth/login', json={'username':'liveadmin','password':password}).status_code == 200,
                          '应用重启后账号登录失败')
                    current = client.get(f"/api/tickets/{ticket_id}").json()
                    check(current["status"] == "awaiting_approval", "应用重启后审批状态丢失")
                    ticket = post(client, f"/api/tickets/{ticket_id}/approval", approve)
                    check(ticket["status"] == "completed" and ticket["action"]["kind"] == "exchange", "恢复后未创建模拟换货单")
                    again = post(client, f"/api/tickets/{ticket_id}/approval", approve)
                    check(again["action"]["id"] == ticket["action"]["id"], "重复审批创建了重复售后单")
            state = ticket["state"]
            rows.append({"name": name, "passed": True, "intent": state["intent"],
                         "final_status": ticket["status"], "ticket_id": ticket_id,
                         "policy_ids": [p["id"] for p in state.get("policies", [])],
                         "draft": state.get("draft"), "reply": state.get("reply"),
                         "rule_reason": state.get("rule_reason"), "action": ticket["action"],
                         "input_tokens": state.get("input_tokens", 0), "output_tokens": state.get("output_tokens", 0),
                         "duration_ms": round((perf_counter() - start) * 1000)})
        except Exception as exc:
            # 验收失败时只打印已定义的断言信息，不回显底层供应商异常。
            rows.append({"name": name, "passed": False, "error_type": type(exc).__name__,
                         "error": str(exc) if isinstance(exc, AssertionError) else "验收运行异常，未显示底层请求信息",
                         "duration_ms": round((perf_counter() - start) * 1000)})
        print(json.dumps({"case": index + 1, "name": name, "passed": rows[-1]["passed"],
                          "duration_ms": rows[-1]["duration_ms"]}, ensure_ascii=False), flush=True)
        if not rows[-1]["passed"]:
            break
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": settings.mode,
              "model": settings.model, "run_id": run_id, "planned_cases": len(cases),
              "passed": sum(r["passed"] for r in rows), "executed": len(rows),
              "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
              "output_tokens": sum(r.get("output_tokens", 0) for r in rows), "cases": rows}
    output = ROOT / "docs" / "live-verification.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False), flush=True)
    return report["passed"] == len(cases)


if __name__ == "__main__":
    raise SystemExit(0 if verify() else 1)
