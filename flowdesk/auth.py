import hashlib
import hmac
import secrets
import time
from uuid import uuid4

from .database import now

SESSION_COOKIE = "flowdesk_session"
SESSION_SECONDS = 8 * 3600
ITERATIONS = 600_000


class AuthError(Exception):
    def __init__(self, message, status=401):
        self.status = status
        super().__init__(message)


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password, encoded):
    _, iterations, salt, digest = encoded.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(actual.hex(), digest)


class Auth:
    def __init__(self, store):
        self.store = store
        self._dummy = password_hash(secrets.token_urlsafe(24))
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','agent','customer')),
                    customer_id TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    bucket TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start REAL NOT NULL
                );
            """)

    @staticmethod
    def public(row):
        return {key: row[key] for key in ("id", "username", "role", "customer_id", "active")}

    def needs_setup(self):
        with self.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    def create_user(self, username, password, role, customer_id=None, bootstrap=False):
        if role == "customer" and customer_id not in {"C001", "C002"}:
            raise AuthError("客户账号必须绑定客户编号", 422)
        encoded = password_hash(password)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if bootstrap and db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise AuthError("初始化已完成，请登录", 409)
            if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise AuthError("用户名已存在", 409)
            user_id = uuid4().hex
            db.execute("INSERT INTO users(id,username,password_hash,role,customer_id,created_at) VALUES (?,?,?,?,?,?)",
                       (user_id, username, encoded, role, customer_id if role == "customer" else None, now()))
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self.public(row)

    def authenticate(self, username, password, address):
        bucket = hashlib.sha256((address + "|" + username.lower()).encode()).hexdigest()
        moment = time.time()
        with self.store.connect() as db:
            attempt = db.execute("SELECT * FROM login_attempts WHERE bucket=?", (bucket,)).fetchone()
            if attempt and moment - attempt["window_start"] < 60 and attempt["failures"] >= 5:
                raise AuthError("尝试次数过多，请稍后重试", 429)
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        valid = password_matches(password, row["password_hash"] if row else self._dummy)
        if not row or not row["active"] or not valid:
            with self.store.connect() as db:
                if not attempt or moment - attempt["window_start"] >= 60:
                    db.execute("INSERT OR REPLACE INTO login_attempts VALUES (?,1,?)", (bucket, moment))
                else:
                    db.execute("UPDATE login_attempts SET failures=failures+1 WHERE bucket=?", (bucket,))
            raise AuthError("用户名或密码不正确")
        with self.store.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE bucket=?", (bucket,))
        return self.public(row)

    def session(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), user_id, time.time() + SESSION_SECONDS))
        return token

    def current(self, token):
        if not token:
            raise AuthError("请先登录")
        with self.store.connect() as db:
            row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                             "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row:
            raise AuthError("登录已过期，请重新登录")
        return self.public(row)

    def logout(self, token):
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256((token or "").encode()).hexdigest(),))

    def users(self):
        with self.store.connect() as db:
            return [self.public(row) for row in db.execute("SELECT * FROM users ORDER BY created_at")]

    def set_active(self, user_id, active, actor_id):
        if user_id == actor_id:
            raise AuthError("不能修改当前账号的启用状态", 409)
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise KeyError(user_id)
            db.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

