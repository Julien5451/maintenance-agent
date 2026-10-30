import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          task_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
          decision TEXT, approved_draft_id TEXT);
        CREATE TABLE IF NOT EXISTS drafts (
          task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
          draft_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS work_orders (
          order_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
          draft_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          created_at TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        self.conn.commit()

    def new_task(self, task_id: str):
        with self.conn:
            self.conn.execute(
                "INSERT INTO tasks(task_id, created_at) VALUES (?,?)", (task_id, now())
            )

    def require_task(self, task_id: str):
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        return dict(row)

    def event(self, task_id: str, event_type: str, **kwargs):
        payload = {"event_type": event_type, "simulated": True, **kwargs}
        with self.conn:
            self.conn.execute(
                "INSERT INTO events(task_id,created_at,payload) VALUES (?,?,?)",
                (task_id, now(), json.dumps(payload, ensure_ascii=False)),
            )

    def events(self, task_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,))
        return [
            {"seq": r["seq"], "created_at": r["created_at"], **json.loads(r["payload"])}
            for r in rows
        ]

    def save_draft(self, task_id: str, draft: dict):
        with self.conn:
            self.conn.execute(
                "INSERT INTO drafts VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE "
                "SET draft_id=excluded.draft_id,payload=excluded.payload",
                (task_id, draft["draft_id"], json.dumps(draft, ensure_ascii=False)),
            )

    def decide(self, task_id: str, action: str, draft_id: str | None):
        with self.conn:
            task = self.require_task(task_id)
            if task["decision"] and (
                task["decision"] != action or task["approved_draft_id"] != draft_id
            ):
                raise ValueError("任务已有不同的确认/取消决定")
            if action == "confirm":
                saved = self.conn.execute(
                    "SELECT draft_id FROM drafts WHERE task_id=?", (task_id,)
                ).fetchone()
                if not saved or saved[0] != draft_id:
                    raise ValueError("草案版本不匹配")
            self.conn.execute(
                "UPDATE tasks SET decision=?,approved_draft_id=? WHERE task_id=?",
                (action, draft_id, task_id),
            )

    def create_work_order(self, task_id: str, draft: dict) -> dict:
        """仅由确认后的执行节点调用；唯一请求键覆盖重试/崩溃后重跑。"""
        with self.conn:
            task = self.require_task(task_id)
            saved = self.conn.execute("SELECT * FROM drafts WHERE task_id=?", (task_id,)).fetchone()
            if (
                task["decision"] != "confirm"
                or task["approved_draft_id"] != draft["draft_id"]
                or not saved
                or json.loads(saved["payload"]) != draft
            ):
                raise ValueError("执行需要批准服务端保存的当前草案")
            self.conn.execute(
                "INSERT INTO work_orders VALUES (?,?,?,?,?) ON CONFLICT(request_key) DO NOTHING",
                (
                    "WO-" + uuid4().hex[:12].upper(),
                    task_id,
                    draft["draft_id"],
                    json.dumps(draft, ensure_ascii=False),
                    now(),
                ),
            )
        return self.order_for_task(task_id)

    @staticmethod
    def _order(row):
        return {**dict(row), "payload": json.loads(row["payload"]), "simulated": True}

    def order_for_task(self, task_id: str):
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE request_key=?", (task_id,)
        ).fetchone()
        return self._order(row) if row else None

    def list_orders(self):
        return [
            self._order(r)
            for r in self.conn.execute("SELECT * FROM work_orders ORDER BY created_at")
        ]

    def get_order(self, order_id):
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if not row:
            raise KeyError("工单不存在")
        return self._order(row)

    def close(self):
        self.conn.close()
