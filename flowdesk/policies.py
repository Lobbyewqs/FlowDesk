import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "policies.json"


def tokens(text):
    text = re.sub(r"\s+", "", text.lower())
    return re.findall(r"[a-z0-9]+", text) + [text[i:i + 2] for i in range(len(text) - 1)]


def retrieve(query, intent, limit=2):
    """本地 BM25 检索；先按业务类型过滤，再排序，不需要下载嵌入模型。"""
    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    candidates = [p for p in policies if intent in p["intents"]]
    if not candidates:
        return []
    corpus = [tokens(p["title"] + p["content"]) for p in policies]
    average = sum(map(len, corpus)) / len(corpus)
    query_terms = set(tokens(query))
    ranked = []
    for policy in candidates:
        document = tokens(policy["title"] + policy["content"])
        frequency = Counter(document)
        score = 0.0
        for term in query_terms:
            df = sum(term in doc for doc in corpus)
            idf = math.log(1 + (len(corpus) - df + 0.5) / (df + 0.5))
            tf = frequency[term]
            score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * len(document) / average))
        if score > 0:
            ranked.append({**policy, "score": round(score, 3)})
    return sorted(ranked, key=lambda p: p["score"], reverse=True)[:limit]


def eligibility(order, customer_id, intent, policies):
    if not order or order["customer_id"] != customer_id:
        return False, "订单不存在或不属于当前演示客户，请人工核实"
    if intent not in {"logistics", "return", "exchange"}:
        return False, "当前仅支持物流咨询、退货和换货，请人工处理其他诉求"
    matching = [p for p in policies if intent in p["intents"]]
    if not matching:
        return False, "未检索到适用政策，无法自动形成处理依据"
    if intent == "logistics":
        return True, "订单归属已验证，可回复当前物流信息"
    if order["status"] != "delivered" or not order["delivered_at"]:
        return False, "订单尚未签收，当前演示退换货流程不适用"
    days = (datetime.now(timezone.utc) - datetime.fromisoformat(order["delivered_at"])).total_seconds() / 86400
    max_days = matching[0]["max_days"]
    if days < 0 or days > max_days:
        return False, f"签收已超过演示政策规定的 {max_days} 天期限，需人工处理"
    return True, f"订单归属、签收状态及 {max_days} 天申请期限校验通过；实物条件仍需线下核验"

