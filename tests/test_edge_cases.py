"""Additional mock integration checks for identity grounding and crash recovery."""

import json
import os
import subprocess
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from maintenance_agent.config import ROOT, Settings
from maintenance_agent.service import MaintenanceService

ASSET = ("lookup_asset", {"query": "EQ-001"})
RULES = (
    "lookup_rules",
    {"asset_id": "EQ-001", "category": "mechanical", "stopped": True},
)
DRAFT = (
    "propose_draft",
    {
        "asset_id": "EQ-001",
        "fault_summary": "轴承异响",
        "stopped": True,
        "category": "mechanical",
    },
)
ASK = ("ask_user", {"question": "请补充具体设备编号和停机情况。"})


class EdgeModel:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if not self.responses:
            raise AssertionError("Unexpected extra model invocation")
        response = self.responses.popleft()
        calls = response if isinstance(response, list) else [response]
        return AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": args, "id": f"edge-{self.calls}-{i}", "type": "tool_call"}
                for i, (name, args) in enumerate(calls)
            ],
        )


def test_model_cannot_invent_specific_asset_id_from_underspecified_report(tmp_path):
    model = EdgeModel([ASSET, RULES, DRAFT, ASK])
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("设备轴承异响，已经停机，但我不知道具体是哪台。")

        assert task["status"] in {"question", "error", "limit_reached"}
        assert not task["draft"]
        assert svc.list_orders() == []
        asset_calls = [
            event for event in task["events"] if event.get("tool_name") == "lookup_asset"
        ]
        assert asset_calls[0]["status"] == "rejected"
    finally:
        svc.close()


def test_ambiguous_lookup_interrupts_before_model_can_guess_an_exact_id(tmp_path):
    model = EdgeModel([("lookup_asset", {"query": "数控车床"}), ASSET, RULES, DRAFT])
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("数控车床轴承异响，已经停机。")

        assert task["status"] == "question"
        assert model.calls == 1
        assert not task["draft"]
        assert svc.list_orders() == []
    finally:
        svc.close()


def test_unknown_asset_ends_before_model_can_substitute_a_known_one(tmp_path):
    model = EdgeModel([("lookup_asset", {"query": "EQ-999"}), ASSET, RULES, DRAFT])
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("EQ-999 机械轴承异响，已经停机。")

        assert task["status"] == "unmatched"
        assert task["reason"] == "unmatched_asset"
        assert model.calls == 1
        assert not task["draft"]
        assert svc.list_orders() == []
    finally:
        svc.close()


def test_ambiguous_batch_answers_all_tool_ids_but_skips_following_calls(tmp_path):
    model = EdgeModel(
        [[("lookup_asset", {"query": "数控车床"}), RULES], ("lookup_asset", {"query": "数控车床"})]
    )
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("数控车床机械轴承异响，已经停机。")

        assert task["status"] == "question"
        snapshot = svc.workflow.graph.get_state(svc.config(task["task_id"]))
        state = snapshot.values
        assert not state.get("resolved_asset_id")
        assert not state.get("rules_key")
        expected_ids = {"edge-1-0", "edge-1-1"}
        answered_ids = {
            message.tool_call_id
            for message in state["messages"]
            if isinstance(message, ToolMessage)
        }
        # Required by tool-calling providers when this thread is resumed later.
        assert expected_ids <= answered_ids
        rule_events = [
            event for event in task["events"] if event.get("tool_name") == "lookup_rules"
        ]
        assert not any(event["status"] == "ok" for event in rule_events)
    finally:
        svc.close()


def test_provide_info_invalidates_previous_asset_and_rule_selection(tmp_path):
    model = EdgeModel([ASSET, ASK, DRAFT, ("ask_user", {"question": "请确认更正后的设备信息。"})])
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("EQ-001 机械轴承异响。")
        assert task["status"] == "question"

        changed = svc.respond(
            task["task_id"], "provide_info", text="更正，是 EQ-002，而且实际上未停机。"
        )

        assert changed["status"] in {"question", "error", "limit_reached"}
        assert not changed["draft"]
        assert svc.list_orders() == []
    finally:
        svc.close()


def test_tool_round_limit_is_not_reset_by_provide_info(tmp_path):
    model = EdgeModel([ASK] * 7)
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("设备坏了。")
        for _ in range(5):
            assert task["status"] == "question"
            task = svc.respond(task["task_id"], "provide_info", text="我仍无法提供更多信息。")
        if task["status"] == "question":
            task = svc.respond(task["task_id"], "provide_info", text="再试一次。")

        assert task["status"] == "limit_reached"
        assert task["tool_rounds"] == 6
        assert model.calls == 6
        assert svc.list_orders() == []
    finally:
        svc.close()


def test_model_cannot_call_mutating_work_order_tool(tmp_path):
    model = EdgeModel(
        [("create_work_order", {"asset_id": "EQ-001", "request_key": "attacker-key"}), ASK]
    )
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    try:
        task = svc.create_task("轴承异响，已经停机，设备编号不清楚。")

        assert task["status"] == "question"
        assert svc.list_orders() == []
        events = [
            event for event in task["events"] if event.get("tool_name") == "create_work_order"
        ]
        assert len(events) == 1
        assert events[0]["status"] == "rejected"
    finally:
        svc.close()


def test_confirmation_after_cancellation_is_rejected(tmp_path):
    svc = MaintenanceService(
        settings=Settings(database_dir=tmp_path), model=EdgeModel([ASSET, RULES, DRAFT])
    )
    try:
        task = svc.create_task("EQ-001 轴承异响，已经停机。")
        svc.respond(task["task_id"], "cancel")

        with pytest.raises(ValueError):
            svc.respond(task["task_id"], "confirm", draft_id=task["draft"]["draft_id"])

        assert svc.get_task(task["task_id"])["status"] == "cancelled"
        assert svc.list_orders() == []
    finally:
        svc.close()


def test_committed_order_survives_failure_before_execution_checkpoint(tmp_path, monkeypatch):
    settings = Settings(database_dir=tmp_path)
    svc = MaintenanceService(settings=settings, model=EdgeModel([ASSET, RULES, DRAFT]))
    try:
        task = svc.create_task("EQ-001 轴承异响，已经停机。")
        original_event = svc.store.event

        def fail_after_order_commit(task_id, event_type, **fields):
            if event_type == "tool_call" and fields.get("tool_name") == "create_work_order":
                raise RuntimeError("simulated failure after business transaction committed")
            return original_event(task_id, event_type, **fields)

        monkeypatch.setattr(svc.store, "event", fail_after_order_commit)
        with pytest.raises(RuntimeError, match="simulated failure"):
            svc.respond(task["task_id"], "confirm", draft_id=task["draft"]["draft_id"])
        committed_order = svc.store.order_for_task(task["task_id"])
        assert committed_order is not None
        assert len(svc.list_orders()) == 1
    finally:
        svc.close()

    forbidden = EdgeModel([])
    resumed = MaintenanceService(settings=settings, model=forbidden)
    try:
        result = resumed.respond(task["task_id"], "confirm", draft_id=task["draft"]["draft_id"])

        assert result["status"] == "completed"
        assert forbidden.calls == 0
        assert len(resumed.list_orders()) == 1
        assert result["work_order"]["order_id"] == committed_order["order_id"]
    finally:
        resumed.close()


CHILD_SCRIPT = r"""
import json
import sys
from collections import deque
from pathlib import Path
from langchain_core.messages import AIMessage
from maintenance_agent.config import Settings
from maintenance_agent.service import MaintenanceService

class Model:
    def __init__(self, forbid=False):
        self.forbid = forbid
        self.calls = 0
        self.steps = deque([
            ("lookup_asset", {"query": "EQ-001"}),
            ("lookup_rules", {"asset_id": "EQ-001", "category": "mechanical", "stopped": True}),
            ("propose_draft", {"asset_id": "EQ-001", "fault_summary": "轴承异响",
                               "stopped": True, "category": "mechanical"}),
        ])
    def invoke(self, messages):
        self.calls += 1
        if self.forbid:
            raise AssertionError("Restarted approval must not call the model")
        name, args = self.steps.popleft()
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": "child-" + str(self.calls), "type": "tool_call"
        }])

mode, directory = sys.argv[1:3]
model = Model(forbid=mode != "create")
svc = MaintenanceService(settings=Settings(database_dir=Path(directory)), model=model)
try:
    if mode == "create":
        task = svc.create_task("EQ-001 轴承异响，属于机械故障，已经停机。")
        result = {"task": task, "order_count": len(svc.list_orders())}
    else:
        task_id, draft_id = sys.argv[3:5]
        before = svc.get_task(task_id)
        task = svc.respond(task_id, "confirm", draft_id=draft_id)
        result = {"before": before, "task": task, "order_count": len(svc.list_orders())}
    result["model_calls_in_process"] = model.calls
    print(json.dumps(result, ensure_ascii=False))
finally:
    svc.close()
"""


def run_child(mode, directory, *extra):
    # Explicit PYTHONPATH also works when pytest is invoked without an editable install.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT, mode, str(directory), *extra],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=45,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_independent_process_restart_and_concurrent_duplicate_confirmation(tmp_path):
    # Process 1 exits completely; only SQLite files survive, not Python objects.
    created = run_child("create", tmp_path)
    pending = created["task"]
    assert pending["status"] == "awaiting_confirmation"
    assert created["order_count"] == 0

    args = ("confirm", tmp_path, pending["task_id"], pending["draft"]["draft_id"])
    # Two independent processes race to confirm the same persisted request.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_child, *args) for _ in range(2)]
        approved = [future.result() for future in futures]

    for result in approved:
        assert result["task"]["status"] == "completed"
        assert result["task"]["draft"] == pending["draft"]
        assert result["order_count"] == 1
        assert result["model_calls_in_process"] == 0
    assert (
        approved[0]["task"]["work_order"]["order_id"]
        == approved[1]["task"]["work_order"]["order_id"]
    )

    # A fourth process sees the same saved order, and repeated approval stays idempotent.
    final = run_child(*args)
    assert final["before"]["status"] == "completed"
    assert final["order_count"] == 1
    assert final["model_calls_in_process"] == 0
