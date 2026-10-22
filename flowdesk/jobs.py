import json
import sqlite3
import threading
import time
from uuid import uuid4

from .database import dumps, now


class JobConflict(Exception):
    pass


class JobStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            if "batch_id" not in {row[1] for row in db.execute("PRAGMA table_info(tickets)")}:
                db.execute("ALTER TABLE tickets ADD COLUMN batch_id TEXT")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at REAL NOT NULL, lease_until REAL, owner TEXT,
                    last_error TEXT, created_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_job ON jobs(ticket_id)
                    WHERE status IN ('queued','running');
                CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status,available_at);
                CREATE TABLE IF NOT EXISTS demo_batches (
                    id TEXT PRIMARY KEY, count INTEGER NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demo_cases (
                    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id), scenario INTEGER NOT NULL,
                    order_id TEXT NOT NULL REFERENCES orders(id)
                );
                CREATE INDEX IF NOT EXISTS idx_tickets_scope ON tickets(customer_id,status,created_at);
                CREATE INDEX IF NOT EXISTS idx_tickets_batch ON tickets(batch_id);
            """)

    @staticmethod
    def insert(db, ticket_id, kind, payload, max_attempts=3):
        job_id = "JOB-" + uuid4().hex[:12].upper()
        db.execute("INSERT INTO jobs(id,ticket_id,kind,payload,status,max_attempts,available_at,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (job_id, ticket_id, kind, dumps(payload), "queued", max_attempts, time.time(), now()))
        return job_id

    def enqueue(self, ticket_id, kind, payload):
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                job_id = self.insert(db, ticket_id, kind, payload)
                db.execute("UPDATE tickets SET status='queued',updated_at=? WHERE id=?", (now(), ticket_id))
                return job_id
        except sqlite3.IntegrityError:
            raise JobConflict("该工单已有后台任务，请等待处理完成") from None

    def active(self, ticket_id):
        with self.store.connect() as db:
            row = db.execute("SELECT id,status,kind,attempts,max_attempts,last_error FROM jobs "
                             "WHERE ticket_id=? AND status IN ('queued','running')", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def claim(self, owner, lease_seconds):
        moment = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # 只接管已过租约的工作；仍存活的执行线程持续续租。
            exhausted = db.execute("SELECT ticket_id FROM jobs WHERE status='running' AND lease_until<? "
                                   "AND attempts>=max_attempts", (moment,)).fetchall()
            for row in exhausted:
                db.execute("UPDATE tickets SET status='failed',updated_at=? WHERE id=?", (now(), row[0]))
            db.execute("UPDATE jobs SET status='failed',owner=NULL,finished_at=?,last_error='执行租约过期且已达重试上限' "
                       "WHERE status='running' AND lease_until<? AND attempts>=max_attempts", (now(), moment))
            db.execute("UPDATE jobs SET status='queued',owner=NULL,lease_until=NULL,available_at=?, "
                       "last_error='工作进程中断，租约过期后重新排队' WHERE status='running' AND lease_until<?",
                       (moment, moment))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND available_at<=? "
                             "ORDER BY available_at,created_at,rowid LIMIT 1", (moment,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,owner=?,lease_until=? WHERE id=?",
                       (owner, moment + lease_seconds, row["id"]))
            job = dict(row)
            job.update(owner=owner, attempts=row["attempts"] + 1)
            job["payload"] = json.loads(job["payload"])
            return job

    def renew(self, job_id, owner, seconds):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND status='running'",
                       (time.time() + seconds, job_id, owner))

    def finish(self, job, success):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=? AND status='running'",
                             (job["id"], job["owner"])).fetchone()
            if not row:
                return
            retry = not success and row["attempts"] < row["max_attempts"]
            status = "done" if success else "queued" if retry else "failed"
            delay = min(2 ** (row["attempts"] - 1), 8) if retry else 0
            db.execute("UPDATE jobs SET status=?,owner=NULL,lease_until=NULL,available_at=?,finished_at=?,last_error=? WHERE id=?",
                       (status, time.time() + delay, None if retry else now(),
                        None if success else "节点执行失败，已保留检查点" if retry else "已达自动重试上限，请人工重试", job["id"]))
            if retry:
                db.execute("UPDATE tickets SET status='queued',updated_at=? WHERE id=?", (now(), job["ticket_id"]))

    def list(self, limit=50):
        with self.store.connect() as db:
            rows = db.execute("SELECT id,ticket_id,kind,status,attempts,max_attempts,last_error,created_at,finished_at "
                              "FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def summary(self):
        with self.store.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status")}


class Workers:
    def __init__(self, service, count=2, lease_seconds=45, poll_seconds=.15):
        self.service = service
        self.count = count
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.threads = []

    def start(self):
        for index in range(self.count):
            thread = threading.Thread(target=self._loop, name=f"flowdesk-worker-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join()

    def _heartbeat(self, job, stopped):
        while not stopped.wait(max(.1, self.lease_seconds / 3)):
            try:
                self.service.jobs.renew(job["id"], job["owner"], self.lease_seconds)
            except sqlite3.OperationalError:
                continue

    def _loop(self):
        owner = uuid4().hex
        while not self.stop_event.is_set():
            try:
                job = self.service.jobs.claim(owner, self.lease_seconds)
            except sqlite3.OperationalError:
                self.stop_event.wait(self.poll_seconds)
                continue
            if not job:
                self.stop_event.wait(self.poll_seconds)
                continue
            stopped = threading.Event()
            heartbeat = threading.Thread(target=self._heartbeat, args=(job, stopped), daemon=True)
            heartbeat.start()
            try:
                ticket = self.service.process_job(job)
                success = ticket["status"] != "failed"
            except Exception:
                success = False
                ticket = self.service.store.ticket(job["ticket_id"])
                self.service.store.save(job["ticket_id"], "failed", ticket["state"])
            finally:
                stopped.set()
                heartbeat.join()
            try:
                self.service.jobs.finish(job, success)
            except sqlite3.OperationalError:
                # 写入暂不可用时保留租约记录，后续由队列接管，执行线程继续服务。
                self.stop_event.wait(self.poll_seconds)
