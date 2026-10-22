import sqlite3
import threading
from dataclasses import replace

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .database import Store
from .intelligence import Intelligence
from .jobs import JobStore
from .workflow import build_graph


class Conflict(Exception):
    pass


class Service:
    def __init__(self, settings, intelligence=None):
        self.settings = settings
        self.store = Store(settings.data_dir / "business.sqlite3")
        self.jobs = JobStore(self.store)
        self.connection = sqlite3.connect(settings.data_dir / "checkpoints.sqlite3", check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.checkpointer = SqliteSaver(self.connection)
        self.graph = build_graph(self.store, intelligence or Intelligence(settings), self.checkpointer)
        self.graphs = {settings.mode: self.graph}
        if settings.mode != "demo":
            self.graphs["demo"] = build_graph(self.store, Intelligence(replace(settings, mode="demo")), self.checkpointer)
        # 首版只运行一个服务进程，锁用于隔离同一工单的并发审批/重试。
        self._locks = {}
        self._registry_lock = threading.Lock()

    def close(self):
        self.connection.close()

    def _lock(self, ticket_id):
        with self._registry_lock:
            return self._locks.setdefault(ticket_id, threading.RLock())

    def _config(self, ticket_id):
        return {"configurable": {"thread_id": ticket_id}, "recursion_limit": 100}

    def _graph(self, ticket_id):
        mode = self.store.ticket(ticket_id)["state"].get("mode", self.settings.mode)
        return self.graphs.get(mode, self.graph)

    def initial(self, ticket):
        mode = ticket["state"].get("mode", self.settings.mode)
        return {"ticket_id": ticket["id"], "customer_id": ticket["customer_id"],
                "description": ticket["description"], "input_tokens": 0, "output_tokens": 0,
                "mode": mode, "model": self.settings.model if mode == "live" else ""}

    def _project(self, ticket_id):
        snapshot = self._graph(ticket_id).get_state(self._config(ticket_id))
        state = dict(snapshot.values)
        interrupts = [i.value for task in snapshot.tasks for i in task.interrupts]
        if interrupts:
            pause = interrupts[0]
            status = "needs_info" if pause["kind"] == "information" else "awaiting_approval"
            state["pause"] = pause
        elif state.get("outcome"):
            status = state["outcome"]
        else:
            status = "processing"
        self.store.save(ticket_id, status, state)

    def _run(self, ticket_id, value):
        ticket = self.store.ticket(ticket_id)
        self.store.save(ticket_id, "processing", ticket["state"])
        try:
            self._graph(ticket_id).invoke(value, self._config(ticket_id))
        except Exception:
            # 不存储供应商原始异常，防止 URL、头信息等进入日志或浏览器。
            state = dict(self._graph(ticket_id).get_state(self._config(ticket_id)).values)
            state["error"] = "处理失败，已保留检查点。请检查模型配置或网络后重试。"
            self.store.save(ticket_id, "failed", state)
            self.store.event(ticket_id, "error", "执行失败", state["error"])
        else:
            self._project(ticket_id)
        return self.detail(ticket_id)

    def create(self, customer_id, description):
        ticket_id = self.store.create_ticket(customer_id, description)
        with self._lock(ticket_id):
            return self._run(ticket_id, {"ticket_id": ticket_id, "customer_id": customer_id,
                                         "description": description, "input_tokens": 0, "output_tokens": 0,
                                         "mode": self.settings.mode,
                                         "model": self.settings.model if self.settings.mode == "live" else ""})

    def detail(self, ticket_id):
        if self.jobs.active(ticket_id):
            return self._raw_detail(ticket_id)
        with self._lock(ticket_id):
            ticket = self.store.ticket(ticket_id)
            # 进程中断时业务投影可能落后于检查点，读取时先恢复投影。
            if ticket["status"] in {"queued", "processing"}:
                if self._graph(ticket_id).get_state(self._config(ticket_id)).values:
                    self._project(ticket_id)
                    ticket = self.store.ticket(ticket_id)
            return self._raw_detail(ticket_id)

    def _raw_detail(self, ticket_id):
        ticket = self.store.ticket(ticket_id)
        ticket["events"] = self.store.events(ticket_id)
        ticket["action"] = self.store.action(ticket_id)
        ticket["job"] = self.jobs.active(ticket_id)
        return ticket

    def respond(self, ticket_id, kind, payload):
        with self._lock(ticket_id):
            ticket = self.detail(ticket_id)
            state = ticket["state"]
            if kind == "approval" and ticket["status"] in {"completed", "rejected", "manual"} and "decision" in state:
                # 完全相同的重复请求返回原结果；不同审核内容属于冲突。
                if state["decision"] == payload:
                    return ticket
                raise Conflict("该工单已经审核，不能更改已有决定")
            required = {"information": "needs_info", "approval": "awaiting_approval"}[kind]
            if ticket["status"] != required:
                raise Conflict("当前工单状态不支持此操作，请刷新后重试")
            self.store.event(ticket_id, "input", "收到补充信息" if kind == "information" else "收到审核请求",
                             "人工输入已接收，正在恢复工作流")
            return self._run(ticket_id, Command(resume=payload))

    def retry(self, ticket_id):
        with self._lock(ticket_id):
            ticket = self.detail(ticket_id)
            if ticket["status"] not in {"failed", "processing", "queued"}:
                raise Conflict("仅失败或被中断的任务可以重试")
            snapshot = self._graph(ticket_id).get_state(self._config(ticket_id))
            value = None if snapshot.values else {"ticket_id": ticket_id, "customer_id": ticket["customer_id"],
                "description": ticket["description"], "input_tokens": 0, "output_tokens": 0,
                "mode": self.settings.mode, "model": self.settings.model if self.settings.mode == "live" else ""}
            return self._run(ticket_id, value)

    def submit(self, customer_id, description, actor, mode=None):
        from .database import dumps, now
        from uuid import uuid4
        ticket_id = "FD-" + uuid4().hex[:10].upper()
        mode = mode or self.settings.mode
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO tickets(id,customer_id,description,status,snapshot,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (ticket_id, customer_id, description, "queued", dumps({"mode": mode}), now(), now()))
            self.jobs.insert(db, ticket_id, "start", {"actor": actor})
        return self._raw_detail(ticket_id)

    def enqueue_input(self, ticket_id, kind, data, actor):
        if self.jobs.active(ticket_id):
            raise Conflict("该工单已有后台任务，请等待处理完成")
        with self._lock(ticket_id):
            ticket = self.detail(ticket_id)
            payload = {"actor": actor, "data": {**data, "actor": actor}}
            if kind == "retry":
                if ticket["status"] not in {"failed", "queued", "processing"}:
                    raise Conflict("仅失败或被中断的任务可以重试")
            else:
                required = "needs_info" if kind == "information" else "awaiting_approval"
                if kind == "approval" and ticket["state"].get("decision") == payload["data"]:
                    return ticket
                if ticket["status"] != required:
                    raise Conflict("当前工单状态不支持此操作")
                snapshot = self._graph(ticket_id).get_state(self._config(ticket_id))
                pauses = [i for task in snapshot.tasks for i in task.interrupts]
                if not pauses:
                    raise Conflict("未找到等待输入的检查点")
                payload["interrupt_id"] = pauses[0].id
            self.jobs.enqueue(ticket_id, kind, payload)
            self.store.event(ticket_id, "enqueue", "后台任务已提交", f"操作：{kind}；操作者：{actor}")
            return self._raw_detail(ticket_id)

    def process_job(self, job):
        ticket_id = job["ticket_id"]
        with self._lock(ticket_id):
            graph = self._graph(ticket_id)
            snapshot = graph.get_state(self._config(ticket_id))
            pauses = [i for task in snapshot.tasks for i in task.interrupts]
            if job["kind"] in {"information", "approval"}:
                if pauses and pauses[0].id == job["payload"]["interrupt_id"]:
                    value = Command(resume=job["payload"]["data"])
                elif pauses or not snapshot.next:
                    self._project(ticket_id)
                    return self._raw_detail(ticket_id)
                else:
                    value = None
            elif pauses or (snapshot.values and not snapshot.next):
                self._project(ticket_id)
                return self._raw_detail(ticket_id)
            else:
                value = None if snapshot.values else self.initial(self.store.ticket(ticket_id))
            return self._run(ticket_id, value)

