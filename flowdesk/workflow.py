from time import perf_counter
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .policies import eligibility, retrieve


class WorkflowState(TypedDict, total=False):
    ticket_id: str
    mode: str
    model: str
    customer_id: str
    description: str
    intent: str
    order_id: str
    reason: str
    order: dict
    policies: list
    eligible: bool
    rule_reason: str
    draft: str
    reply: str
    decision: dict
    action: dict
    outcome: str
    input_tokens: int
    output_tokens: int


def build_graph(store, intelligence, checkpointer):
    def usage(state, extra):
        return {key: state.get(key, 0) + extra.get(key, 0)
                for key in ("input_tokens", "output_tokens")}

    def record(state, node, title, detail, start):
        store.event(state["ticket_id"], node, title, detail, round((perf_counter() - start) * 1000))

    def analyze(state):
        start = perf_counter()
        fields, tokens = intelligence.extract(state["description"])
        record(state, "analyze", "识别客户诉求", f"类型：{fields['intent']}；订单：{fields['order_id'] or '待补充'}", start)
        return {**fields, **usage(state, tokens)}

    def need_information(state):
        answer = interrupt({"kind": "information", "message": "请补充订单号，例如 A1024。"})
        store.event(state["ticket_id"], "information", "客户补充信息", answer["text"])
        return {"description": state["description"] + "\n补充：" + answer["text"]}

    def query_order(state):
        start = perf_counter()
        order = store.order(state["order_id"])
        # 不向模型、浏览器或日志暴露其他客户的订单详情。
        if order and order["customer_id"] != state["customer_id"]:
            order = None
        record(state, "query_order", "查询订单工具", "订单归属验证通过" if order else "未找到属于当前客户的订单", start)
        return {"order": order}

    def search_policy(state):
        start = perf_counter()
        policies = retrieve(state["description"], state["intent"])
        record(state, "search_policy", "检索售后政策", "、".join(p["id"] for p in policies) or "无适用政策", start)
        return {"policies": policies}

    def validate(state):
        start = perf_counter()
        ok, reason = eligibility(state["order"], state["customer_id"], state["intent"], state["policies"])
        record(state, "validate", "业务规则校验", reason, start)
        return {"eligible": ok, "rule_reason": reason}

    def draft_reply(state):
        start = perf_counter()
        draft, tokens = intelligence.draft(state)
        record(state, "draft_reply", "生成回复草稿", "草稿已生成，等待客服审核", start)
        return {"draft": draft, **usage(state, tokens)}

    def review(state):
        decision = interrupt({"kind": "approval", "message": "请审核政策依据及回复，批准后执行模拟操作。",
                              "draft": state["draft"]})
        detail = "批准" if decision["approved"] else "驳回：" + decision["note"]
        store.event(state["ticket_id"], "review", "人工审核", detail + "；操作者：" + decision.get("actor", "离线测试"))
        return {"decision": decision, "reply": decision.get("reply") or state["draft"]}

    def execute(state):
        start = perf_counter()
        # 恢复和重试可能重复进入节点，先取已提交结果，再核实当前业务条件。
        existing = store.action(state["ticket_id"])
        if existing:
            return {"action": existing, "outcome": "completed"}
        ok, reason = eligibility(store.order(state["order_id"]), state["customer_id"],
                                 state["intent"], state["policies"])
        if not ok:
            return {"outcome": "manual", "rule_reason": "执行前再次校验失败：" + reason}
        action = None
        if state["intent"] in {"return", "exchange"}:
            try:
                action = store.execute(state["ticket_id"], state["order_id"], state["intent"])
            except ValueError as exc:
                return {"outcome": "manual", "rule_reason": str(exc)}
        record(state, "execute", "执行模拟操作", f"创建模拟售后单 {action['id']}" if action else "物流回复已审核归档", start)
        return {"action": action, "outcome": "completed"}

    def manual(state):
        store.event(state["ticket_id"], "manual", "转人工处理", state["rule_reason"])
        return {"outcome": "manual"}

    def reject(state):
        return {"outcome": "rejected"}

    graph = StateGraph(WorkflowState)
    for name, function in [("analyze", analyze), ("information", need_information),
                           ("query_order", query_order), ("search_policy", search_policy),
                           ("validate", validate), ("draft_reply", draft_reply),
                           ("review", review), ("execute", execute), ("manual", manual), ("reject", reject)]:
        graph.add_node(name, function)
    graph.add_edge(START, "analyze")
    graph.add_conditional_edges("analyze", lambda s: "query_order" if s.get("order_id") else "information")
    graph.add_edge("information", "analyze")
    graph.add_edge("query_order", "search_policy")
    graph.add_edge("search_policy", "validate")
    graph.add_conditional_edges("validate", lambda s: "draft_reply" if s["eligible"] else "manual")
    graph.add_edge("draft_reply", "review")
    graph.add_conditional_edges("review", lambda s: "execute" if s["decision"]["approved"] else "reject")
    for name in ("execute", "manual", "reject"):
        graph.add_edge(name, END)
    return graph.compile(checkpointer=checkpointer)

