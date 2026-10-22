import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                    product TEXT NOT NULL, amount INTEGER NOT NULL,
                    status TEXT NOT NULL, delivered_at TEXT,
                    tracking TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                    description TEXT NOT NULL, status TEXT NOT NULL,
                    snapshot TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL REFERENCES tickets(id),
                    node TEXT NOT NULL, title TEXT NOT NULL,
                    detail TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),
                    order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
                    kind TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id, id);
            """)
            self._seed(db)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _seed(self, db):
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        expired = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        orders = [
            ("A1024", "C001", "无线降噪耳机", 29900, "delivered", recent, "已签收，签收次日"),
            ("A1025", "C001", "机械键盘", 45900, "shipped", None, "运输中：已到达本地分拨中心"),
            ("A1026", "C001", "便携蓝牙音箱", 19900, "delivered", expired, "已签收，超过演示售后期限"),
            ("A1027", "C002", "智能手表", 59900, "delivered", recent, "已签收，属于另一位演示客户"),
            ("A1028", "C001", "桌面支架", 6900, "delivered", recent, "已签收，签收次日"),
        ]
        db.executemany("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?,?)", orders)

    def create_ticket(self, customer_id, description):
        ticket_id = "FD-" + uuid4().hex[:10].upper()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tickets(id,customer_id,description,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (ticket_id, customer_id, description, "queued", now(), now()),
            )
        return ticket_id

    def ticket(self, ticket_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise KeyError(ticket_id)
        item = dict(row)
        item["state"] = json.loads(item.pop("snapshot"))
        return item

    def save(self, ticket_id, status, state):
        with self.connect() as db:
            db.execute(
                "UPDATE tickets SET status=?, snapshot=?, updated_at=? WHERE id=?",
                (status, dumps(state), now(), ticket_id),
            )

    def list_tickets(self, customer_id=None, status=None, page=1, page_size=25, batch_id=None):
        where, args = self._ticket_filter(customer_id, status, batch_id)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tickets" + where + " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                              (*args, page_size, (page - 1) * page_size)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["state"] = json.loads(item.pop("snapshot"))
            result.append(item)
        return result

    @staticmethod
    def _ticket_filter(customer_id=None, status=None, batch_id=None):
        clauses, args = [], []
        for field, value in (("customer_id", customer_id), ("batch_id", batch_id)):
            if value:
                clauses.append(field + "=?")
                args.append(value)
        groups = {"pending": ("queued", "processing", "failed", "needs_info", "awaiting_approval"),
                  "done": ("completed", "rejected", "manual")}
        if status in groups:
            values = groups[status]
            clauses.append("status IN (" + ",".join("?" for _ in values) + ")")
            args.extend(values)
        elif status:
            clauses.append("status=?")
            args.append(status)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    def ticket_counts(self, customer_id=None, status=None, batch_id=None):
        where, args = self._ticket_filter(customer_id, status, batch_id)
        with self.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT status,COUNT(*) FROM tickets" + where + " GROUP BY status", args)}

    def order(self, order_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def orders(self, customer_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM orders WHERE customer_id=? ORDER BY id", (customer_id,)).fetchall()
        return [dict(row) for row in rows]

    def event(self, ticket_id, node, title, detail, duration_ms=0):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(ticket_id,node,title,detail,duration_ms,created_at) VALUES (?,?,?,?,?,?)",
                (ticket_id, node, title, detail, duration_ms, now()),
            )

    def events(self, ticket_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()
        return [dict(row) for row in rows]

    def action(self, ticket_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM actions WHERE ticket_id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def execute(self, ticket_id, order_id, kind):
        # 检查与写入在同一写事务中；不同工单也不能对同一订单重复创建售后。
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM actions WHERE ticket_id=?", (ticket_id,)).fetchone()
            if existing:
                return dict(existing)
            other = db.execute("SELECT id FROM actions WHERE order_id=?", (order_id,)).fetchone()
            if other:
                raise ValueError("该订单已有模拟售后单，请转人工核对，避免重复处理")
            item = {"id": "AS-" + uuid4().hex[:10].upper(), "ticket_id": ticket_id,
                    "order_id": order_id, "kind": kind, "created_at": now()}
            db.execute("INSERT INTO actions VALUES (:id,:ticket_id,:order_id,:kind,:created_at)", item)
        return item

