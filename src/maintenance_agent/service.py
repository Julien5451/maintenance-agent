import os
import threading
from contextlib import contextmanager
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from .config import Settings
from .domain import Catalog
from .facts import missing_fields
from .graph import Workflow
from .store import Store


class MaintenanceService:
    def __init__(self, settings: Settings | None = None, model=None):
        self.settings = settings or Settings()
        self.settings.database_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.lock_path = self.settings.database_dir / "workflow.lock"
        self.store = Store(self.settings.database_dir / "business.sqlite")
        self.catalog = Catalog()
        self.workflow = Workflow(self.settings, self.catalog, self.store, model)

    @contextmanager
    def serialized(self):
        # Demonstration-scale, local filesystem, one operation at a time across processes.
        with self.lock, self.lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def config(task_id):
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 40}

    def create_task(self, text: str) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("报修内容需要1–4000字")
        with self.serialized():
            task_id = uuid4().hex
            self.store.new_task(task_id)
            self.store.event(task_id, "status_change", previous=None, status="running")
            self.workflow.graph.invoke(
                {
                    "task_id": task_id,
                    "messages": [HumanMessage(text)],
                    "status": "running",
                    "tool_rounds": 0,
                    "model_calls": 0,
                    "question": None,
                    "draft": None,
                    "work_order": None,
                },
                self.config(task_id),
            )
            return self._get_task(task_id)

    def _get_task(self, task_id):
        self.store.require_task(task_id)
        snapshot = self.workflow.graph.get_state(self.config(task_id))
        state = snapshot.values
        draft = state.get("draft")
        return {
            "task_id": task_id,
            "simulated": True,
            "status": state.get("status", "error"),
            "question": state.get("question"),
            "reason": state.get("reason"),
            "draft": draft,
            "work_order": state.get("work_order"),
            "tool_rounds": state.get("tool_rounds", 0),
            "model_calls": state.get("model_calls", 0),
            "facts": state.get("facts", {}),
            "missing_fields": missing_fields(state),
            "rule_evidence": [
                r for r in self.catalog.rules if draft and r["rule_id"] in draft["rule_ids"]
            ],
            "events": self.store.events(task_id),
        }

    def get_task(self, task_id):
        with self.serialized():
            return self._get_task(task_id)

    def respond(self, task_id, action, text=None, draft_id=None):
        with self.serialized():
            task = self._get_task(task_id)
            status = task["status"]
            if action not in {"provide_info", "confirm", "cancel"}:
                raise ValueError("未知操作")
            if action == "confirm":
                if not draft_id or not task["draft"] or draft_id != task["draft"]["draft_id"]:
                    raise ValueError("批准必须绑定服务端当前草案ID")
                if status == "completed":
                    return task
                if status not in {"awaiting_confirmation", "approved"}:
                    raise ValueError("当前状态不能批准")
                self.store.decide(task_id, "confirm", draft_id)
            elif action == "cancel":
                if status == "cancelled":
                    return task
                if status not in {"question", "awaiting_confirmation"}:
                    raise ValueError("当前状态不能取消")
                self.store.decide(task_id, "cancel", None)
            elif status != "question" or not text or not text.strip() or len(text) > 4000:
                raise ValueError("仅追问状态可补充1–4000字信息")
            self.store.event(
                task_id, "human_action", action=action, draft_id=draft_id, status="accepted"
            )
            # Resume an interrupted node; if execute previously crashed, resume its saved next node.
            command = (
                None
                if status == "approved"
                else Command(resume={"action": action, "text": text, "draft_id": draft_id})
            )
            self.workflow.graph.invoke(command, self.config(task_id))
            return self._get_task(task_id)

    def list_orders(self):
        with self.serialized():
            return self.store.list_orders()

    def get_order(self, order_id):
        with self.serialized():
            return self.store.get_order(order_id)

    def health(self):
        with self.serialized():
            self.store.conn.execute("SELECT 1")
            return {
                "status": "ok",
                "simulated": True,
                "database": "ok",
                "model_configured": self.settings.model_ready,
            }

    def close(self):
        with self.lock:
            self.workflow.close()
            self.store.close()
