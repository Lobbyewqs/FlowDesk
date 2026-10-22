"""运行小规模、人工编写的演示评估集，结果不代表真实业务准确率。"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from flowdesk.config import ROOT, Settings
from flowdesk.service import Service


def evaluate(output, live=False):
    cases = json.loads((ROOT / "fixtures" / "evaluation.json").read_text(encoding="utf-8"))
    settings = Settings.load() if live else Settings(ROOT / ".tmp")
    if live and settings.mode != "live":
        raise ValueError("--live 要求先在 FlowDesk/.env 中明确启用 live 模式")
    evaluation_id = uuid4().hex[:12]
    results = []
    for index, case in enumerate(cases):
        case_settings = Settings(ROOT / ".tmp" / f"eval-{evaluation_id}" / str(index),
                                 settings.mode, settings.model, settings.base_url, settings.api_key)
        service = Service(case_settings)
        start = perf_counter()
        try:
            ticket = service.create("C001", case["text"])
            state = ticket["state"]
            actual_policy = state.get("policies", [{}])[0].get("id") if state.get("policies") else None
            results.append({**case, "actual_intent": state.get("intent"), "actual_status": ticket["status"],
                            "actual_policy": actual_policy,
                            "intent_correct": state.get("intent") == case["intent"],
                            "status_correct": ticket["status"] == case["status"],
                            "policy_correct": actual_policy == case["policy"],
                            "duration_ms": round((perf_counter() - start) * 1000),
                            "input_tokens": state.get("input_tokens", 0),
                            "output_tokens": state.get("output_tokens", 0)})
        finally:
            service.close()
    durations = sorted(row["duration_ms"] for row in results)
    retrieval_cases = [row for row in results if row["policy"]]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "mode": settings.mode,
        "dataset": "24 条人工编写的演示工单，含否定表达、同义表达及异常业务条件；非独立真实业务测试集。",
        "definitions": {"intent_accuracy": "初次识别类型与人工标注一致的比例",
                        "status_accuracy": "初次运行后的暂停/转人工状态与标注一致的比例，不评估最终审批执行",
                        "policy_hit_at_1": "标注有政策的样本中，首条检索政策正确的比例；受前置分类影响",
                        "latency_p95_ms": "本机串行运行初次工作流耗时的近似 P95，含数据库开销"},
        "summary": {"cases": len(results),
                    "intent_accuracy": round(sum(r["intent_correct"] for r in results) / len(results), 4),
                    "status_accuracy": round(sum(r["status_correct"] for r in results) / len(results), 4),
                    "policy_hit_at_1": round(sum(r["policy_correct"] for r in retrieval_cases) / len(retrieval_cases), 4),
                    "retrieval_cases": len(retrieval_cases),
                    "latency_p95_ms": durations[min(len(durations) - 1, int(len(durations) * .95))],
                    "input_tokens": sum(r["input_tokens"] for r in results),
                    "output_tokens": sum(r["output_tokens"] for r in results)},
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"完整结果：{output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "evaluation.json")
    parser.add_argument("--live", action="store_true", help="显式调用已配置模型，会产生供应商用量")
    args = parser.parse_args()
    evaluate(args.output.resolve(), args.live)

