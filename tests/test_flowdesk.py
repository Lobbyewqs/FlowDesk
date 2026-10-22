from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from types import SimpleNamespace
import secrets
import time

import pytest
from fastapi.testclient import TestClient

from flowdesk.config import ROOT, Settings
from flowdesk.intelligence import Intelligence
from flowdesk.main import create_app
from flowdesk.service import Service


@pytest.fixture
def task_dir():
    path = ROOT / ".tmp" / ("test-" + uuid4().hex)
    path.mkdir(parents=True)
    return path


@pytest.fixture
def service(task_dir):
    instance = Service(Settings(task_dir))
    yield instance
    instance.close()


@pytest.fixture
def client(task_dir):
    with TestClient(create_app(Settings(task_dir))) as instance:
        instance.post("/api/auth/setup", json={"username": "testadmin", "password": secrets.token_urlsafe(24)})
        yield instance


def wait_ticket(client, ticket_id, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ticket = client.get(f"/api/tickets/{ticket_id}").json()
        if ticket["status"] not in {"queued", "processing"} and not ticket.get("job"):
            return ticket
        time.sleep(.025)
    raise AssertionError("后台任务未在限定时间内完成")


APPROVE = {"approved": True, "note": "", "reply": ""}


def test_exchange_creates_only_one_action_and_conflicting_review_fails(service):
    ticket = service.create("C001", "订单 A1024 耳机左耳没声音，我想换货")
    assert ticket["status"] == "awaiting_approval"
    assert ticket["state"]["mode"] == "demo" and ticket["state"]["model"] == ""
    assert ticket["state"]["policies"][0]["id"] == "P-EXC-01"
    assert ticket["action"] is None
    result = service.respond(ticket["id"], "approval", APPROVE)
    assert result["status"] == "completed"
    assert result["action"]["kind"] == "exchange"
    repeated = service.respond(ticket["id"], "approval", APPROVE)
    assert repeated["action"]["id"] == result["action"]["id"]
    from flowdesk.service import Conflict
    with pytest.raises(Conflict):
        service.respond(ticket["id"], "approval", {"approved": False, "note": "撤回", "reply": ""})


def test_return_and_logistics_have_distinct_side_effects(service):
    ticket = service.create("C001", "A1028 支架不想要了，申请退货")
    done = service.respond(ticket["id"], "approval", APPROVE)
    assert done["action"]["kind"] == "return"
    logistics = service.create("C001", "A1025 快递到哪了，还没收到")
    assert "分拨中心" in logistics["state"]["draft"]
    done = service.respond(logistics["id"], "approval", APPROVE)
    assert done["status"] == "completed" and done["action"] is None


def test_missing_order_pauses_and_can_be_supplemented_more_than_once(service):
    ticket = service.create("C001", "耳机坏了想换货")
    assert ticket["status"] == "needs_info"
    same = service.respond(ticket["id"], "information", {"text": "我还在找订单号"})
    assert same["status"] == "needs_info"
    resumed = service.respond(ticket["id"], "information", {"text": "订单号 A1024"})
    assert resumed["status"] == "awaiting_approval"
    assert resumed["state"]["order"]["id"] == "A1024"


def test_order_number_can_touch_chinese_characters(service):
    result = service.create("C001", "订单A1024耳机坏了，希望换货")
    assert result["status"] == "awaiting_approval"
    assert result["state"]["order_id"] == "A1024"


@pytest.mark.parametrize("text,expected", [
    ("A1026 音箱我想退货", "超过"),
    ("A1025 键盘我想换货", "尚未签收"),
    ("A9999 我想退货", "订单不存在"),
    ("A1027 手表我想换货", "订单不存在"),
    ("A1024 帮我开发票", "仅支持"),
])
def test_ineligible_requests_route_to_manual_without_actions(service, text, expected):
    ticket = service.create("C001", text)
    assert ticket["status"] == "manual"
    assert expected in ticket["state"]["rule_reason"]
    assert ticket["action"] is None
    if "A1027" in text:
        assert ticket["state"]["order"] is None
        assert "59900" not in str(ticket)


def test_no_matching_policy_cannot_be_approved(service, monkeypatch):
    monkeypatch.setattr("flowdesk.workflow.retrieve", lambda *a: [])
    ticket = service.create("C001", "A1024 耳机想换货")
    assert ticket["status"] == "manual"
    assert "未检索到" in ticket["state"]["rule_reason"]


def test_rejection_has_no_action_and_edited_reply_is_persisted(service):
    first = service.create("C001", "A1024 想换货")
    result = service.respond(first["id"], "approval", {"approved": False, "note": "需核实商品情况", "reply": ""})
    assert result["status"] == "rejected" and result["action"] is None
    second = service.create("C001", "A1025 查询物流")
    result = service.respond(second["id"], "approval", {"approved": True, "note": "已核实", "reply": "客服修改后的回复"})
    assert result["state"]["reply"] == "客服修改后的回复"


def test_same_order_cannot_create_two_after_sales_actions(service):
    one = service.create("C001", "A1024 耳机换货")
    two = service.create("C001", "A1024 耳机退货")
    assert service.respond(one["id"], "approval", APPROVE)["status"] == "completed"
    result = service.respond(two["id"], "approval", APPROVE)
    assert result["status"] == "manual" and result["action"] is None
    assert "已有模拟售后单" in result["state"]["rule_reason"]


def test_concurrent_approval_is_idempotent(service):
    ticket = service.create("C001", "A1024 想换货")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.respond(ticket["id"], "approval", APPROVE), range(4)))
    assert len({r["action"]["id"] for r in results}) == 1
    assert sum(e["node"] == "execute" for e in results[-1]["events"]) == 1


@pytest.mark.parametrize("description,status", [("A1024 耳机想换货", "awaiting_approval"), ("耳机想换货", "needs_info")])
def test_persistent_checkpoint_survives_service_restart(task_dir, description, status):
    first = Service(Settings(task_dir))
    ticket = first.create("C001", description)
    first.close()
    second = Service(Settings(task_dir))
    try:
        assert second.detail(ticket["id"])["status"] == status
        if status == "needs_info":
            assert second.respond(ticket["id"], "information", {"text": "A1024"})["status"] == "awaiting_approval"
        result = second.respond(ticket["id"], "approval", APPROVE)
        assert result["status"] == "completed"
    finally:
        second.close()


def test_model_failure_can_retry_from_failed_node_without_redoing_analysis(task_dir):
    class FailOnce(Intelligence):
        failed = False

        def draft(self, state):
            if not self.failed:
                self.failed = True
                raise RuntimeError("sensitive-vendor-error")
            return super().draft(state)

    service = Service(Settings(task_dir), FailOnce(Settings(task_dir)))
    try:
        failed = service.create("C001", "A1024 耳机想换货")
        assert failed["status"] == "failed"
        assert "sensitive-vendor-error" not in str(failed)
        resumed = service.retry(failed["id"])
        assert resumed["status"] == "awaiting_approval"
        assert sum(e["node"] == "analyze" for e in resumed["events"]) == 1
    finally:
        service.close()


def test_crash_after_business_commit_does_not_duplicate_action(service, monkeypatch):
    ticket = service.create("C001", "A1024 耳机换货")
    original = service.store.execute

    def commit_then_fail(*args):
        original(*args)
        raise RuntimeError("模拟：业务已提交，节点检查点尚未写入")

    monkeypatch.setattr(service.store, "execute", commit_then_fail)
    failed = service.respond(ticket["id"], "approval", APPROVE)
    assert failed["status"] == "failed" and failed["action"] is not None
    resumed = service.retry(ticket["id"])
    assert resumed["status"] == "completed"
    assert resumed["action"]["id"] == failed["action"]["id"]


def test_execution_rechecks_current_order_state(service):
    ticket = service.create("C001", "A1024 耳机换货")
    with service.store.connect() as db:
        db.execute("UPDATE orders SET status='cancelled' WHERE id='A1024'")
    result = service.respond(ticket["id"], "approval", APPROVE)
    assert result["status"] == "manual" and result["action"] is None
    assert "再次校验失败" in result["state"]["rule_reason"]


def test_real_http_contract_and_validation(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["version"] == "0.2.0"
    assert client.get("/api/tickets/unknown").status_code == 404
    assert client.post("/api/tickets", json={"description": "  "}).status_code == 422
    assert client.post("/api/tickets", json={"description": "申请退货", "customer_id": "unknown"}).status_code == 422
    result = client.post("/api/tickets", json={"description": "A1024 耳机换货"})
    assert result.status_code == 202
    ticket_id = result.json()["id"]
    assert wait_ticket(client, ticket_id)["status"] == "awaiting_approval"
    assert client.post(f"/api/tickets/{ticket_id}/approval", json={"approved": False}).status_code == 422
    assert client.post(f"/api/tickets/{ticket_id}/information", json={"text": "A1024"}).status_code == 409
    assert client.post(f"/api/tickets/{ticket_id}/approval", json={"approved": True}).status_code == 202
    assert wait_ticket(client, ticket_id)["status"] == "completed"
    assert len(client.get("/api/tickets").json()["items"]) == 1


def test_cross_site_browser_writes_rejected(client):
    response = client.post("/api/tickets", headers={"Origin": "https://unrelated.example"}, json={"description": "A1024 换货"})
    assert response.status_code == 403
    assert client.get("/api/tickets").json()["items"] == []


def test_live_adapter_validates_model_order_and_tracks_usage(task_dir):
    intelligence = Intelligence(Settings(task_dir))
    intelligence.mode = "live"

    class FakeModel:
        content = '{"intent":"exchange","order_id":"A1024","reason":"换货"}'

        def bind(self, **kwargs):
            assert kwargs["response_format"] == {"type": "json_object"}
            return self

        def invoke(self, messages):
            return SimpleNamespace(content=self.content, usage_metadata={"input_tokens": 42, "output_tokens": 12})

    intelligence.model = FakeModel()
    fields, usage = intelligence.extract("订单A1024耳机换货")
    assert fields["order_id"] == "A1024" and usage["input_tokens"] == 42
    with pytest.raises(ValueError, match="没有出现在"):
        intelligence.extract("耳机换货")


def test_provider_error_is_sanitized_before_checkpoint(task_dir):
    intelligence = Intelligence(Settings(task_dir))
    intelligence.mode = "live"

    class BrokenModel:
        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            raise RuntimeError("private-vendor-error-marker")

    intelligence.model = BrokenModel()
    service = Service(Settings(task_dir), intelligence)
    try:
        ticket = service.create("C001", "A1024 想换货")
        assert ticket["status"] == "failed"
        snapshot = service.graph.get_state(service._config(ticket["id"]))
        assert "private-vendor-error-marker" not in str(snapshot)
    finally:
        service.close()


def test_api_key_is_not_in_settings_representation(task_dir):
    settings = Settings(task_dir, api_key="local-test-placeholder")
    assert "local-test-placeholder" not in repr(settings)

