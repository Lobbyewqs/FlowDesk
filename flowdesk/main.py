import json
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, field_validator

from .auth import Auth, AuthError, SESSION_COOKIE, SESSION_SECONDS
from .config import ROOT, Settings
from .database import now
from .demo_data import seed_batch
from .jobs import JobConflict, Workers
from .policies import POLICY_FILE
from .service import Conflict, Service


class TicketInput(BaseModel):
    customer_id: Literal["C001", "C002"] | None = None
    description: str = Field(min_length=2, max_length=3000)

    @field_validator("description")
    @classmethod
    def clean(cls, value):
        if len(value.strip()) < 2:
            raise ValueError("请填写具体的售后问题")
        return value.strip()


class InformationInput(BaseModel):
    text: str = Field(min_length=2, max_length=1000)

    @field_validator("text")
    @classmethod
    def clean(cls, value):
        if len(value.strip()) < 2:
            raise ValueError("请填写补充信息")
        return value.strip()


class ApprovalInput(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=1000)
    reply: str = Field(default="", max_length=4000)

    @field_validator("note", "reply")
    @classmethod
    def clean(cls, value):
        return value.strip()


class Credentials(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.@-]{3,40}$")
    password: SecretStr

    @field_validator("password")
    @classmethod
    def limit_password(cls, value):
        if not 12 <= len(value.get_secret_value()) <= 128:
            raise ValueError("密码长度应为 12～128 个字符")
        return value


class UserInput(Credentials):
    role: Literal["admin", "agent", "customer"]
    customer_id: Literal["C001", "C002"] | None = None


class ActiveInput(BaseModel):
    active: bool


class BatchInput(BaseModel):
    count: int = Field(default=200, ge=1, le=500)


def current_user(request: Request):
    return request.app.state.auth.current(request.cookies.get(SESSION_COOKIE))


def staff(user=Depends(current_user)):
    if user["role"] not in {"admin", "agent"}:
        raise AuthError("需要客服或管理员权限", 403)
    return user


def admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise AuthError("需要管理员权限", 403)
    return user


def accessible(service, ticket_id, user):
    ticket = service.store.ticket(ticket_id)
    if user["role"] == "customer" and ticket["customer_id"] != user["customer_id"]:
        raise KeyError(ticket_id)
    return ticket


def present(ticket, user):
    if user["role"] != "customer":
        return ticket
    state = ticket["state"]
    allowed = {"description", "intent", "order_id", "order", "policies", "rule_reason", "mode", "model", "reply"}
    if ticket["status"] == "needs_info":
        allowed.add("pause")
    return {**ticket, "state": {k: v for k, v in state.items() if k in allowed}, "events": [], "job": None}


def create_app(settings=None, intelligence=None, worker_count=2):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(settings or Settings.load(), intelligence)
        app.state.auth = Auth(app.state.service.store)
        app.state.workers = Workers(app.state.service, count=worker_count)
        app.state.workers.start()
        try:
            yield
        finally:
            app.state.workers.stop()
            app.state.service.close()

    app = FastAPI(title="FlowDesk", version="0.2.0", lifespan=lifespan,
                  description="带角色权限、客户隔离与持久化后台任务的售后工作流平台。")

    @app.middleware("http")
    async def local_write_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin.rstrip("/") != str(request.base_url).rstrip("/")):
                return JSONResponse({"detail": "仅允许同源页面执行操作"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"detail": "输入不符合要求，请检查字段长度、格式与必填项"}, status_code=422)

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.exception_handler(Conflict)
    @app.exception_handler(JobConflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def sign_in(request, response, user):
        token = request.app.state.auth.session(user["id"])
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", path="/")
        return user

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.2.0", "time": now()}

    @app.get("/api/auth/setup")
    def setup_needed(request: Request):
        return {"required": request.app.state.auth.needs_setup()}

    @app.post("/api/auth/setup", status_code=201)
    def setup(body: Credentials, request: Request, response: Response):
        user = request.app.state.auth.create_user(body.username, body.password.get_secret_value(), "admin", bootstrap=True)
        return sign_in(request, response, user)

    @app.post("/api/auth/login")
    def login(body: Credentials, request: Request, response: Response):
        user = request.app.state.auth.authenticate(body.username, body.password.get_secret_value(), request.client.host)
        return sign_in(request, response, user)

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        request.app.state.auth.logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")

    @app.get("/api/auth/me")
    def me(user=Depends(current_user)):
        return user

    @app.get("/api/meta")
    def meta(request: Request, user=Depends(current_user)):
        service = request.app.state.service
        customers = [{"id": "C001", "name": "林同学"}, {"id": "C002", "name": "陈同学"}]
        return {"mode": service.settings.mode, "model": service.settings.model if service.settings.mode == "live" else None,
                "user": user, "customers": [c for c in customers if user["role"] != "customer" or c["id"] == user["customer_id"]],
                "policies": json.loads(POLICY_FILE.read_text(encoding="utf-8"))}

    @app.get("/api/orders")
    def orders(request: Request, customer_id: Literal["C001", "C002"] | None = None, user=Depends(current_user)):
        if user["role"] == "customer":
            if customer_id and customer_id != user["customer_id"]:
                raise AuthError("无权访问其他客户订单", 403)
            customer_id = user["customer_id"]
        return request.app.state.service.store.orders(customer_id or "C001")

    @app.get("/api/tickets")
    def tickets(request: Request, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                status: str | None = None, user=Depends(current_user)):
        store = request.app.state.service.store
        customer_id = user["customer_id"] if user["role"] == "customer" else None
        items = store.list_tickets(customer_id, status, page, page_size)
        total = sum(store.ticket_counts(customer_id, status).values())
        return {"items": [present(t, user) for t in items], "total": total, "page": page, "page_size": page_size,
                "pages": max(1, (total + page_size - 1) // page_size)}

    @app.get("/api/stats")
    def stats(request: Request, user=Depends(current_user)):
        service = request.app.state.service
        counts = service.store.ticket_counts(user["customer_id"] if user["role"] == "customer" else None)
        return {"counts": counts, "total": sum(counts.values()), "jobs": service.jobs.summary() if user["role"] != "customer" else {},
                "workers": request.app.state.workers.count if user["role"] != "customer" else 0}

    @app.post("/api/tickets", status_code=202)
    def create(body: TicketInput, request: Request, user=Depends(current_user)):
        if user["role"] == "customer" and body.customer_id and body.customer_id != user["customer_id"]:
            raise AuthError("不能为其他客户提交工单", 403)
        customer_id = user["customer_id"] if user["role"] == "customer" else body.customer_id or "C001"
        return present(request.app.state.service.submit(customer_id, body.description, user["username"]), user)

    @app.get("/api/tickets/{ticket_id}")
    def detail(ticket_id: str, request: Request, user=Depends(current_user)):
        service = request.app.state.service
        accessible(service, ticket_id, user)
        return present(service.detail(ticket_id), user)

    @app.post("/api/tickets/{ticket_id}/information", status_code=202)
    def information(ticket_id: str, body: InformationInput, request: Request, user=Depends(current_user)):
        service = request.app.state.service
        accessible(service, ticket_id, user)
        return present(service.enqueue_input(ticket_id, "information", body.model_dump(), user["username"]), user)

    @app.post("/api/tickets/{ticket_id}/approval", status_code=202)
    def approval(ticket_id: str, body: ApprovalInput, request: Request, user=Depends(staff)):
        if not body.approved and not body.note:
            raise HTTPException(422, "驳回时请填写原因")
        return request.app.state.service.enqueue_input(ticket_id, "approval", body.model_dump(), user["username"])

    @app.post("/api/tickets/{ticket_id}/retry", status_code=202)
    def retry(ticket_id: str, request: Request, user=Depends(staff)):
        return request.app.state.service.enqueue_input(ticket_id, "retry", {}, user["username"])

    @app.get("/api/jobs")
    def jobs(request: Request, user=Depends(staff)):
        service = request.app.state.service
        return {"items": service.jobs.list(), "summary": service.jobs.summary()}

    @app.get("/api/admin/users")
    def users(request: Request, user=Depends(admin)):
        return request.app.state.auth.users()

    @app.post("/api/admin/users", status_code=201)
    def add_user(body: UserInput, request: Request, user=Depends(admin)):
        return request.app.state.auth.create_user(body.username, body.password.get_secret_value(), body.role, body.customer_id)

    @app.patch("/api/admin/users/{user_id}")
    def active_user(user_id: str, body: ActiveInput, request: Request, user=Depends(admin)):
        request.app.state.auth.set_active(user_id, body.active, user["id"])
        return {"ok": True}

    @app.post("/api/admin/demo-batches", status_code=202)
    def demo_batch(body: BatchInput, request: Request, user=Depends(admin)):
        return seed_batch(request.app.state.service, body.count, user["username"])

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


app = create_app()
