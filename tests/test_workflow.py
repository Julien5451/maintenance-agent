"""Mock integration tests: a real LangGraph and SQLite, with scripted model responses.

These tests never contact a model endpoint and must be reported separately from
the real-model evaluation in evals/cases.json.
"""

from collections import deque

import pytest
from langchain_core.messages import AIMessage

from maintenance_agent.config import Settings
from maintenance_agent.service import MaintenanceService


class SequenceModel:
    """Return explicit tool calls while exercising the real application workflow."""

    def __init__(self, calls):
        self.responses = deque(calls)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if not self.responses:
            raise AssertionError("Unexpected additional model invocation")
        name, args = self.responses.popleft()
        return AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": args, "id": f"mock-{self.calls}", "type": "tool_call"}
            ],
        )


class ForbiddenModel:
    """Approval must resume saved execution without regenerating a draft."""

    calls = 0

    def invoke(self, messages):
        self.calls += 1
        raise AssertionError("Approval unexpectedly invoked the model")


def draft_call(**overrides):
    return (
        "propose_draft",
        {
            "asset_id": "EQ-001",
            "fault_summary": "轴承异响",
            "stopped": True,
            "category": "mechanical",
            **overrides,
        },
    )


def normal_model():
    return SequenceModel(
        [
            ("lookup_asset", {"query": "EQ-001"}),
            (
                "lookup_rules",
                {"asset_id": "EQ-001", "category": "mechanical", "stopped": True},
            ),
            draft_call(),
        ]
    )


@pytest.fixture
def service(tmp_path):
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=normal_model())
    yield svc
    svc.close()


def create_pending(svc):
    task = svc.create_task("EQ-001 轴承异响，属于机械故障，已经停机。")
    assert task["status"] == "awaiting_confirmation"
    assert task["draft"]["asset_id"] == "EQ-001"
    assert task["draft"]["priority"] == "P1"
    assert task["draft"]["team_id"] == "T-MECH"
    assert task["draft"]["rule_ids"] == ["R-MECH-STOP"]
    return task


def test_unapproved_draft_creates_no_work_order(service):
    task = create_pending(service)

    assert service.store.order_for_task(task["task_id"]) is None
    assert service.store.list_orders() == []


def test_cancel_creates_no_work_order(service):
    task = create_pending(service)

    cancelled = service.respond(task["task_id"], "cancel")

    assert cancelled["status"] == "cancelled"
    assert service.store.order_for_task(task["task_id"]) is None
    assert service.store.list_orders() == []


def test_provide_info_resumes_question_on_same_task(tmp_path):
    model = SequenceModel(
        [
            ("lookup_asset", {"query": "EQ-001"}),
            ("ask_user", {"question": "设备当前是否已经停机？"}),
            (
                "lookup_rules",
                {"asset_id": "EQ-001", "category": "mechanical", "stopped": True},
            ),
            draft_call(),
        ]
    )
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        question = svc.create_task("EQ-001 轴承异响，属于机械故障。")
        assert question["status"] == "question"
        assert svc.store.list_orders() == []

        task = svc.respond(question["task_id"], "provide_info", text="已经停机。")

        assert task["task_id"] == question["task_id"]
        assert task["status"] == "awaiting_confirmation"
        assert task["draft"]["stopped"] is True
        assert task["draft"]["priority"] == "P1"
        assert model.calls == 4
        assert svc.store.list_orders() == []
    finally:
        svc.close()


def test_repeated_confirmation_creates_exactly_one_work_order(service):
    task = create_pending(service)
    task_id = task["task_id"]
    draft_id = task["draft"]["draft_id"]

    first = service.respond(task_id, "confirm", draft_id=draft_id)
    first_order = service.store.order_for_task(task_id)
    second = service.respond(task_id, "confirm", draft_id=draft_id)
    second_order = service.store.order_for_task(task_id)

    assert first["status"] == second["status"] == "completed"
    assert len(service.store.list_orders()) == 1
    assert first_order["order_id"] == second_order["order_id"]
    assert second_order["payload"] == task["draft"]


def test_restart_recovers_pending_task_and_approval_does_not_call_model(tmp_path):
    settings = Settings(database_dir=tmp_path)
    original = MaintenanceService(settings=settings, model=normal_model())
    try:
        task = create_pending(original)
    finally:
        original.close()

    forbidden = ForbiddenModel()
    resumed = MaintenanceService(settings=settings, model=forbidden)
    try:
        saved = resumed.get_task(task["task_id"])
        assert saved["status"] == "awaiting_confirmation"
        assert saved["draft"] == task["draft"]

        result = resumed.respond(task["task_id"], "confirm", draft_id=task["draft"]["draft_id"])

        assert result["status"] == "completed"
        assert forbidden.calls == 0
        assert len(resumed.store.list_orders()) == 1
        assert resumed.store.order_for_task(task["task_id"])["payload"] == task["draft"]
    finally:
        resumed.close()


def test_tampered_draft_id_is_rejected_without_creating_order(service):
    task = create_pending(service)

    with pytest.raises(ValueError):
        service.respond(task["task_id"], "confirm", draft_id="tampered-draft-id")

    assert service.get_task(task["task_id"])["status"] == "awaiting_confirmation"
    assert service.store.list_orders() == []

    # A bad request must not poison the pending task or prevent a valid approval.
    approved = service.respond(task["task_id"], "confirm", draft_id=task["draft"]["draft_id"])
    assert approved["status"] == "completed"
    assert len(service.store.list_orders()) == 1


def test_six_round_limit_ends_without_a_seventh_model_call(tmp_path):
    model = SequenceModel([("lookup_asset", {"query": "EQ-001"})] * 7)
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("EQ-001 轴承异响，已经停机。")

        assert task["status"] == "limit_reached"
        assert model.calls == 6
        assert svc.store.list_orders() == []
    finally:
        svc.close()


def test_unknown_tool_is_recorded_without_crashing_or_creating_order(tmp_path):
    model = SequenceModel(
        [
            ("not_an_allowed_tool", {"target": "EQ-001"}),
            ("ask_user", {"question": "请确认设备的具体编号。"}),
        ]
    )
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("设备坏了。")

        assert task["status"] in {"question", "error", "limit_reached"}
        assert svc.store.list_orders() == []
        assert "not_an_allowed_tool" in str(svc.store.events(task["task_id"]))
    finally:
        svc.close()


def test_ambiguous_asset_cannot_be_silently_selected_for_draft(tmp_path):
    model = SequenceModel(
        [
            ("lookup_asset", {"query": "数控车床"}),
            (
                "lookup_rules",
                {"asset_id": "EQ-001", "category": "mechanical", "stopped": True},
            ),
            draft_call(),
            ("ask_user", {"question": "是 A车间1号还是2号工位的数控车床？"}),
        ]
    )
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("数控车床轴承异响，已经停机。")

        assert task["status"] in {"question", "error"}
        assert not task.get("draft")
        assert svc.store.list_orders() == []
    finally:
        svc.close()


def test_draft_requires_preceding_rule_lookup(tmp_path):
    model = SequenceModel(
        [
            ("lookup_asset", {"query": "EQ-001"}),
            draft_call(),
            ("ask_user", {"question": "请补充故障信息以校验派单规则。"}),
        ]
    )
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("EQ-001 轴承异响，已经停机。")

        assert task["status"] in {"question", "error"}
        assert not task.get("draft")
        assert svc.store.list_orders() == []
    finally:
        svc.close()


def test_draft_cannot_reuse_rule_lookup_for_different_stopped_value(tmp_path):
    model = SequenceModel(
        [
            ("lookup_asset", {"query": "EQ-001"}),
            (
                "lookup_rules",
                {"asset_id": "EQ-001", "category": "mechanical", "stopped": True},
            ),
            draft_call(stopped=False),
            ("ask_user", {"question": "请确认当前是否停机。"}),
        ]
    )
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("EQ-001 轴承异响，已经停机。")

        assert task["status"] in {"question", "error"}
        assert not task.get("draft")
        assert svc.store.list_orders() == []
    finally:
        svc.close()
