"""导入并通过真实后台队列处理 200 条离线演示工单；不调用模型服务。"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flowdesk.config import Settings
from flowdesk.demo_data import seed_batch
from flowdesk.jobs import Workers
from flowdesk.service import Service


def wait_batch(service, batch_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with service.store.connect() as db:
            active = db.execute("SELECT COUNT(*) FROM jobs j JOIN tickets t ON t.id=j.ticket_id "
                                "WHERE t.batch_id=? AND j.status IN ('queued','running')", (batch_id,)).fetchone()[0]
        if not active:
            return
        time.sleep(.1)
    raise RuntimeError("演示批次未在限定时间内处理完成")


def seed(new=False):
    service = Service(Settings.load())
    workers = Workers(service, count=2)
    start = time.monotonic()
    try:
        with service.store.connect() as db:
            existing = db.execute("SELECT id FROM demo_batches WHERE count=200 ORDER BY created_at DESC LIMIT 1").fetchone()
        if existing and not new:
            batch_id = existing[0]
            previous_report = ROOT / "docs" / "batch-200.json"
            if previous_report.exists():
                previous = json.loads(previous_report.read_text(encoding="utf-8"))
                if previous.get("batch_id") == batch_id:
                    print(json.dumps(previous, ensure_ascii=False, indent=2))
                    return
        else:
            batch_id = seed_batch(service, 200)["batch_id"]
        workers.start()
        wait_batch(service, batch_id)
        initial = service.store.ticket_counts(batch_id=batch_id)
        # 通过同一队列模拟客服操作，留下完整的审核审计，不直接伪造终态。
        with service.store.connect() as db:
            rows = db.execute("SELECT t.id,t.status,c.order_id,c.scenario FROM tickets t JOIN demo_cases c ON c.ticket_id=t.id "
                              "WHERE t.batch_id=? ORDER BY t.rowid", (batch_id,)).fetchall()
        additions = [r for r in rows if r["scenario"] == 4][:10]
        for row in additions:
            if service.store.ticket(row["id"])["status"] == "needs_info":
                service.enqueue_input(row["id"], "information", {"text": "订单号 " + row["order_id"]}, "演示客户补充")
        wait_batch(service, batch_id)
        for scenario in (0, 1, 2):
            candidates = [r for r in rows if r["scenario"] == scenario][:10]
            for row in candidates:
                if service.store.ticket(row["id"])["status"] == "awaiting_approval":
                    service.enqueue_input(row["id"], "approval", {"approved": True, "note": "模拟客服审批，非真实业务", "reply": ""}, "演示客服")
        rejects = [r for r in rows if r["scenario"] == 0][10:20]
        for row in rejects:
            if service.store.ticket(row["id"])["status"] == "awaiting_approval":
                service.enqueue_input(row["id"], "approval", {"approved": False, "note": "模拟人工驳回：需补充商品凭证", "reply": ""}, "演示客服")
        wait_batch(service, batch_id)
        with service.store.connect() as db:
            jobs = {r[0]: r[1] for r in db.execute("SELECT j.status,COUNT(*) FROM jobs j JOIN tickets t ON t.id=j.ticket_id WHERE t.batch_id=? GROUP BY j.status", (batch_id,))}
            actions = db.execute("SELECT COUNT(*) FROM actions a JOIN tickets t ON t.id=a.ticket_id WHERE t.batch_id=?", (batch_id,)).fetchone()[0]
            customers = {r[0]: r[1] for r in db.execute("SELECT customer_id,COUNT(*) FROM tickets WHERE batch_id=? GROUP BY customer_id", (batch_id,))}
            token_usage = db.execute("SELECT SUM(COALESCE(json_extract(snapshot,'$.input_tokens'),0)+COALESCE(json_extract(snapshot,'$.output_tokens'),0)) FROM tickets WHERE batch_id=?", (batch_id,)).fetchone()[0]
        if jobs.get("failed", 0) or sum(service.store.ticket_counts(batch_id=batch_id).values()) != 200 or token_usage != 0:
            raise RuntimeError("批次校验未通过，请检查隔离的后台任务记录")
        report = {"generated_at": datetime.now(timezone.utc).isoformat(), "batch_id": batch_id, "tickets": 200,
                  "mode": "demo", "workers": 2, "customers": customers, "initial_statuses": initial,
                  "final_statuses": service.store.ticket_counts(batch_id=batch_id), "jobs": jobs,
                  "simulated_after_sales_actions": actions, "model_tokens": token_usage,
                  "elapsed_seconds": round(time.monotonic() - start, 2),
                  "scope": "本机功能批次验证，非生产负载测试。模拟客服与客户操作均有显式审计标记。"}
        (ROOT / "docs" / "batch-200.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        workers.stop()
        service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new", action="store_true", help="额外创建一个新批次；默认复用已有 200 条批次")
    args = parser.parse_args()
    seed(args.new)
