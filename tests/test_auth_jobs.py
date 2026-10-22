import hashlib
import secrets
import threading
import time

import pytest
from fastapi.testclient import TestClient

from flowdesk.auth import SESSION_COOKIE
from flowdesk.config import Settings
from flowdesk.demo_data import seed_batch
from flowdesk.intelligence import Intelligence
from flowdesk.jobs import Workers
from flowdesk.main import create_app
from flowdesk.service import Service
from test_flowdesk import task_dir, wait_ticket


def initialize(client):
    password = secrets.token_urlsafe(24)
    response = client.post("/api/auth/setup", json={"username": "admin", "password": password})
    assert response.status_code == 201
    return password


def add_user(client, username, role, customer_id=None):
    password = secrets.token_urlsafe(24)
    response = client.post("/api/admin/users", json={"username": username, "password": password,
                           "role": role, "customer_id": customer_id})
    assert response.status_code == 201
    return response.json(), password


@pytest.fixture
def app_client(task_dir):
    app = create_app(Settings(task_dir))
    with TestClient(app) as client:
        initialize(client)
        yield app, client


def login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response


@pytest.mark.parametrize("path", ["/api/tickets", "/api/orders", "/api/meta", "/api/stats", "/api/jobs", "/api/admin/users"])
def test_unauthenticated_requests_have_no_business_data(task_dir, path):
    with TestClient(create_app(Settings(task_dir), worker_count=0)) as client:
        assert client.get(path).status_code == 401


def test_bootstrap_hash_cookie_and_logout(task_dir):
    app = create_app(Settings(task_dir), worker_count=0)
    with TestClient(app) as client:
        assert client.get("/api/auth/setup").json()["required"]
        password = initialize(client)
        assert not client.get("/api/auth/setup").json()["required"]
        assert client.post("/api/auth/setup", json={"username": "second", "password": password}).status_code == 409
        token = client.cookies.get(SESSION_COOKIE)
        with app.state.service.store.connect() as db:
            encoded = db.execute("SELECT password_hash FROM users").fetchone()[0]
            stored = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert encoded.startswith("pbkdf2_sha256$600000$")
        assert password not in encoded and stored == hashlib.sha256(token.encode()).hexdigest()
        assert client.get("/api/auth/me").json()["role"] == "admin"
        client.post("/api/auth/logout")
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/api/auth/me").status_code == 401


def test_login_cookie_attributes_and_no_password_echo(task_dir):
    with TestClient(create_app(Settings(task_dir), worker_count=0)) as client:
        password = initialize(client)
        response = login(client, "admin", password)
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        invalid = secrets.token_hex(4)
        response = client.post("/api/auth/login", json={"username": "admin", "password": invalid})
        assert response.status_code == 422 and invalid not in response.text


def test_failed_logins_are_rate_limited(app_client):
    _, client = app_client
    wrong = secrets.token_urlsafe(24)
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "admin", "password": wrong}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": wrong}).status_code == 429


def test_customer_isolation_staff_permissions_and_private_fields(app_client):
    _, client = app_client
    _, password = add_user(client, "customer1", "customer", "C001")
    own = client.post("/api/tickets", json={"customer_id": "C001", "description": "A1024 想换货"}).json()["id"]
    other = client.post("/api/tickets", json={"customer_id": "C002", "description": "A1027 想换货"}).json()["id"]
    assert wait_ticket(client, own)["status"] == "awaiting_approval"
    wait_ticket(client, other)
    login(client, "customer1", password)
    assert client.get(f"/api/tickets/{other}").status_code == 404
    assert client.post(f"/api/tickets/{other}/information", json={"text": "A1027"}).status_code == 404
    assert client.get("/api/orders?customer_id=C002").status_code == 403
    assert client.post("/api/tickets", json={"customer_id": "C002", "description": "A1027 换货"}).status_code == 403
    data = client.get("/api/tickets").json()
    assert data["total"] == 1 and data["items"][0]["customer_id"] == "C001"
    detail = client.get(f"/api/tickets/{own}").json()
    assert "draft" not in detail["state"] and detail["events"] == []
    assert client.post(f"/api/tickets/{own}/approval", json={"approved": True}).status_code == 403
    assert client.post(f"/api/tickets/{own}/retry").status_code == 403
    assert client.get("/api/jobs").status_code == 403
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/stats").json()["total"] == 1


def test_agent_cannot_manage_accounts_and_disabling_revokes_sessions(app_client):
    _, client = app_client
    admin_token = client.cookies.get(SESSION_COOKIE)
    admin_id = client.get("/api/auth/me").json()["id"]
    assert client.patch(f"/api/admin/users/{admin_id}", json={"active": False}).status_code == 409
    assert client.patch("/api/admin/users/not-found", json={"active": False}).status_code == 404
    agent, password = add_user(client, "agent1", "agent")
    login(client, "agent1", password)
    agent_token = client.cookies.get(SESSION_COOKIE)
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/demo-batches", json={"count": 1}).status_code == 403
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, admin_token)
    assert client.patch(f"/api/admin/users/{agent['id']}", json={"active": False}).status_code == 200
    client.cookies.set(SESSION_COOKIE, agent_token)
    assert client.get("/api/auth/me").status_code == 401


def test_submission_and_reads_do_not_wait_for_model(task_dir):
    started, release = threading.Event(), threading.Event()

    class Slow(Intelligence):
        def extract(self, text):
            started.set()
            release.wait(8)
            return super().extract(text)

    settings = Settings(task_dir)
    with TestClient(create_app(settings, Slow(settings))) as client:
        initialize(client)
        try:
            start = time.monotonic()
            response = client.post("/api/tickets", json={"description": "A1024 想换货"})
            assert response.status_code == 202 and time.monotonic() - start < 1
            assert started.wait(2)
            ticket_id = response.json()["id"]
            start = time.monotonic()
            assert client.get(f"/api/tickets/{ticket_id}").status_code == 200
            assert time.monotonic() - start < 1
            assert client.post(f"/api/tickets/{ticket_id}/retry").status_code == 409
        finally:
            release.set()
        assert wait_ticket(client, ticket_id)["status"] == "awaiting_approval"


def test_queue_retries_and_stops_at_attempt_limit(task_dir):
    class Unavailable(Intelligence):
        fixed = False

        def extract(self, text):
            if not self.fixed:
                raise RuntimeError("temporary-test-failure")
            return super().extract(text)

    settings = Settings(task_dir)
    model = Unavailable(settings)
    app = create_app(settings, model)
    with TestClient(app) as client:
        initialize(client)
        ticket_id = client.post("/api/tickets", json={"description": "A1024 想换货"}).json()["id"]
        result = wait_ticket(client, ticket_id)
        assert result["status"] == "failed"
        jobs = client.get("/api/jobs").json()["items"]
        assert jobs[0]["attempts"] == 3 and jobs[0]["status"] == "failed"
        model.fixed = True
        assert client.post(f"/api/tickets/{ticket_id}/retry").status_code == 202
        assert wait_ticket(client, ticket_id)["status"] == "awaiting_approval"


def test_queued_work_and_auth_session_survive_restart(task_dir):
    settings = Settings(task_dir)
    with TestClient(create_app(settings, worker_count=0)) as first:
        initialize(first)
        token = first.cookies.get(SESSION_COOKIE)
        ticket_id = first.post("/api/tickets", json={"description": "A1024 想换货"}).json()["id"]
        assert first.get(f"/api/tickets/{ticket_id}").json()["status"] == "queued"
    with TestClient(create_app(settings)) as second:
        second.cookies.set(SESSION_COOKIE, token)
        assert second.get("/api/auth/me").status_code == 200
        assert wait_ticket(second, ticket_id)["status"] == "awaiting_approval"


def test_expired_lease_is_reclaimed_without_duplicate_work(task_dir):
    service = Service(Settings(task_dir))
    try:
        ticket = service.submit("C001", "A1024 换货", "test")
        first = service.jobs.claim("dead-worker", .02)
        assert service.jobs.claim("other", .02) is None
        time.sleep(.04)
        second = service.jobs.claim("recovery-worker", 10)
        assert first["id"] == second["id"] and second["attempts"] == 2
        result = service.process_job(second)
        service.jobs.finish(second, True)
        assert result["status"] == "awaiting_approval"
        assert service.jobs.summary() == {"done": 1}
        assert service.store.ticket(ticket["id"])["status"] == "awaiting_approval"
    finally:
        service.close()


def test_long_running_job_renews_lease(task_dir):
    started, release = threading.Event(), threading.Event()

    class Slow(Intelligence):
        def extract(self, text):
            started.set()
            release.wait(5)
            return super().extract(text)

    settings = Settings(task_dir)
    service = Service(settings, Slow(settings))
    workers = Workers(service, count=1, lease_seconds=.4, poll_seconds=.02)
    try:
        service.submit("C001", "A1024 换货", "test")
        workers.start()
        assert started.wait(2)
        time.sleep(.8)
        assert service.jobs.claim("other-worker", .4) is None
    finally:
        release.set()
        workers.stop()
        service.close()


def test_approval_retry_after_commit_and_audit_actor(app_client, monkeypatch):
    app, client = app_client
    ticket_id = client.post("/api/tickets", json={"description": "A1024 想换货"}).json()["id"]
    wait_ticket(client, ticket_id)
    original = app.state.service.store.execute

    def commit_then_fail(*args):
        original(*args)
        raise RuntimeError("after-commit-before-checkpoint")

    monkeypatch.setattr(app.state.service.store, "execute", commit_then_fail)
    assert client.post(f"/api/tickets/{ticket_id}/approval", json={"approved": True}).status_code == 202
    result = wait_ticket(client, ticket_id)
    assert result["status"] == "completed" and result["action"] is not None
    assert result["state"]["decision"]["actor"] == "admin"
    assert app.state.service.jobs.list()[0]["attempts"] == 2


def test_batch_200_pagination_and_global_metrics(app_client):
    app, client = app_client
    response = client.post("/api/admin/demo-batches", json={"count": 200})
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        summary = app.state.service.jobs.summary()
        if summary.get("done", 0) == 200:
            break
        time.sleep(.1)
    assert app.state.service.jobs.summary() == {"done": 200}
    assert app.state.service.store.ticket_counts(batch_id=batch_id) == {"awaiting_approval": 75, "manual": 100, "needs_info": 25}
    listing = client.get("/api/tickets?page=8").json()
    assert listing["total"] == 200 and listing["pages"] == 8 and len(listing["items"]) == 25
    assert client.get("/api/stats").json()["total"] == 200
    assert client.get("/api/tickets?status=needs_info").json()["total"] == 25
