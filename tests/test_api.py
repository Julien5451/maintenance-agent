"""HTTP integration tests with the real service, LangGraph, and SQLite.

Only model responses are scripted; these are not real-model evaluations.
"""

from collections import deque

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from maintenance_agent.api import create_app
from maintenance_agent.config import Settings
from maintenance_agent.service import MaintenanceService


class SequenceModel:
    def __init__(self, calls):
        self.responses = deque(calls)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if not self.responses:
            raise AssertionError("Unexpected model call during an HTTP state transition")
        name, args = self.responses.popleft()
        return AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": args, "id": f"api-mock-{self.calls}", "type": "tool_call"}
            ],
        )


def normal_calls():
    return [
        ("lookup_asset", {"query": "EQ-001"}),
        ("lookup_rules", {"asset_id": "EQ-001", "category": "mechanical", "stopped": True}),
        (
            "propose_draft",
            {
                "asset_id": "EQ-001",
                "fault_summary": "轴承异响",
                "category": "mechanical",
                "stopped": True,
            },
        ),
    ]


@pytest.fixture
def harness(tmp_path):
    model = SequenceModel(normal_calls())
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    with TestClient(create_app(lambda: svc)) as client:
        yield client, svc, model


def create_pending(client):
    response = client.post("/tasks", json={"text": "EQ-001 机械轴承异响，已经停机。"})
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["status"] == "awaiting_confirmation"
    assert task["simulated"] is True
    return task


def test_http_creation_and_reads_do_not_create_order(harness):
    client, svc, model = harness
    task = create_pending(client)

    response = client.get(f"/tasks/{task['task_id']}")
    assert response.status_code == 200
    assert response.json()["draft"] == task["draft"]
    assert response.json()["rule_evidence"][0]["rule_id"] == "R-MECH-STOP"
    events = response.json()["events"]
    assert {e.get("tool_name") for e in events} >= {"lookup_asset", "lookup_rules"}
    assert client.get("/work-orders").json() == []
    assert svc.store.list_orders() == []
    assert model.calls == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "confirm"},
        {"action": "confirm", "draft_id": "CURRENT", "priority": "P3"},
        {"action": "confirm", "draft_id": "CURRENT", "draft": {"team_id": "FAKE"}},
        {"action": "confirm", "draft_id": "CURRENT", "text": "改成P3"},
    ],
)
def test_http_confirmation_rejects_missing_version_and_any_draft_edits(harness, payload):
    client, svc, model = harness
    task = create_pending(client)
    if payload.get("draft_id") == "CURRENT":
        payload = {**payload, "draft_id": task["draft"]["draft_id"]}

    response = client.post(f"/tasks/{task['task_id']}/responses", json=payload)

    assert response.status_code == 422
    assert client.get(f"/tasks/{task['task_id']}").json()["status"] == "awaiting_confirmation"
    assert svc.store.list_orders() == []
    assert model.calls == 3


def test_http_wrong_version_is_conflict_then_valid_confirmation_is_idempotent(harness):
    client, svc, model = harness
    task = create_pending(client)
    path = f"/tasks/{task['task_id']}/responses"

    rejected = client.post(path, json={"action": "confirm", "draft_id": "wrong-version"})
    assert rejected.status_code == 409
    assert svc.store.list_orders() == []

    payload = {"action": "confirm", "draft_id": task["draft"]["draft_id"]}
    first = client.post(path, json=payload)
    second = client.post(path, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "completed"
    first_order = first.json()["work_order"]
    assert second.json()["work_order"]["order_id"] == first_order["order_id"]
    assert first_order["payload"] == task["draft"]
    assert len(client.get("/work-orders").json()) == len(svc.store.list_orders()) == 1
    fetched = client.get(f"/work-orders/{first_order['order_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == first_order
    assert model.calls == 3


def test_http_cancel_never_creates_order_and_cannot_be_confirmed(harness):
    client, svc, model = harness
    task = create_pending(client)
    path = f"/tasks/{task['task_id']}/responses"

    cancelled = client.post(path, json={"action": "cancel"})
    repeated = client.post(path, json={"action": "cancel"})
    assert cancelled.status_code == repeated.status_code == 200
    assert cancelled.json()["status"] == repeated.json()["status"] == "cancelled"
    rejected = client.post(path, json={"action": "confirm", "draft_id": task["draft"]["draft_id"]})
    assert rejected.status_code == 409
    assert client.get("/work-orders").json() == []
    assert svc.store.list_orders() == []
    assert model.calls == 3


def test_http_missing_resources_and_health(harness):
    client, _svc, model = harness
    assert client.get("/tasks/nonexistent-task").status_code == 404
    assert client.get("/work-orders/nonexistent-order").status_code == 404
    assert (
        client.post("/tasks/nonexistent-task/responses", json={"action": "cancel"}).status_code
        == 404
    )

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "ok"
    assert response.json()["simulated"] is True
    assert isinstance(response.json()["model_configured"], bool)
    assert model.calls == 0


def test_http_app_restart_recovers_pending_task_without_regenerating_draft(tmp_path):
    settings = Settings(database_dir=tmp_path)
    initial_model = SequenceModel(normal_calls())
    original = MaintenanceService(settings=settings, model=initial_model)
    with TestClient(create_app(lambda: original)) as client:
        task = create_pending(client)
        assert client.get("/work-orders").json() == []

    # An empty sequence fails loudly if confirmation unexpectedly calls the model.
    resumed_model = SequenceModel([])
    restored = MaintenanceService(settings=settings, model=resumed_model)
    with TestClient(create_app(lambda: restored)) as client:
        saved = client.get(f"/tasks/{task['task_id']}")
        assert saved.status_code == 200
        assert saved.json()["status"] == "awaiting_confirmation"
        assert saved.json()["draft"] == task["draft"]
        response = client.post(
            f"/tasks/{task['task_id']}/responses",
            json={"action": "confirm", "draft_id": task["draft"]["draft_id"]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["work_order"]["payload"] == task["draft"]
        assert len(client.get("/work-orders").json()) == 1
        assert resumed_model.calls == 0


def test_http_info_in_wrong_state_is_conflict_without_side_effects(harness):
    client, svc, model = harness
    task = create_pending(client)
    path = f"/tasks/{task['task_id']}/responses"

    rejected = client.post(path, json={"action": "provide_info", "text": "改成未停机。"})

    assert rejected.status_code == 409
    saved = client.get(f"/tasks/{task['task_id']}").json()
    assert saved["status"] == "awaiting_confirmation"
    assert saved["draft"] == task["draft"]
    assert svc.store.list_orders() == []
    assert model.calls == 3


def test_http_provide_info_resumes_the_same_question_task(tmp_path):
    calls = normal_calls()
    calls.insert(1, ("ask_user", {"question": "设备是否已经停机？"}))
    model = SequenceModel(calls)
    svc = MaintenanceService(settings=Settings(database_dir=tmp_path), model=model)
    with TestClient(create_app(lambda: svc)) as client:
        initial = client.post("/tasks", json={"text": "EQ-001 机械轴承异响。"})
        assert initial.status_code == 201
        task = initial.json()
        assert task["status"] == "question"
        assert client.get("/work-orders").json() == []

        response = client.post(
            f"/tasks/{task['task_id']}/responses",
            json={"action": "provide_info", "text": "已经停机。"},
        )

        assert response.status_code == 200
        assert response.json()["task_id"] == task["task_id"]
        assert response.json()["status"] == "awaiting_confirmation"
        assert response.json()["draft"]["stopped"] is True
        assert client.get("/work-orders").json() == []
        assert model.calls == 4
