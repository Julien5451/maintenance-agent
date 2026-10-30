"""Streamlit AppTest against a real temporary HTTP backend.

The model is scripted. HTTP requests, LangGraph, persistence, and UI rendering
are real. This is mock-model integration coverage, not a real-model evaluation
or a browser screenshot/visual review.
"""

import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn
from streamlit.testing.v1 import AppTest
from test_api import SequenceModel, normal_calls

from maintenance_agent.api import create_app
from maintenance_agent.config import ROOT, Settings
from maintenance_agent.service import MaintenanceService


@contextmanager
def running_backend(database_dir, model):
    svc = MaintenanceService(settings=Settings(database_dir=database_dir), model=model)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    address = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config = uvicorn.Config(create_app(lambda: svc), log_level="error", lifespan="on", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise AssertionError("Temporary FastAPI server failed to start")
            time.sleep(0.01)
        with httpx.Client(base_url=address, timeout=10, trust_env=False) as client:
            assert client.get("/health").status_code == 200
            yield address, client
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "Temporary server did not stop cleanly"


def ui_app(monkeypatch, address):
    monkeypatch.setenv("API_BASE_URL", address)
    app = AppTest.from_file(ROOT / "src" / "maintenance_agent" / "ui.py", default_timeout=20)
    app.run()
    assert not app.exception
    assert "模拟数据" in app.warning[0].value
    return app


def button(app, label):
    return next(item for item in app.button if item.label == label)


def text_input(app, label):
    return next(item for item in app.text_input if item.label == label)


def submit_report(app, text):
    app.text_area[0].set_value(text)
    button(app, "提交报修").click().run()
    assert not app.exception
    assert not app.error


def test_ui_report_draft_evidence_tools_and_confirmation_over_http(tmp_path, monkeypatch):
    model = SequenceModel(normal_calls())
    with running_backend(tmp_path, model) as (address, client):
        app = ui_app(monkeypatch, address)
        submit_report(app, "EQ-001 机械轴承异响，已经停机。")

        task = app.session_state["task"]
        assert task["status"] == "awaiting_confirmation"
        assert client.get("/work-orders").json() == []
        assert any(item.value == "等待人工确认" for item in app.subheader)
        table = app.table[0].value
        assert "EQ-001" in table["内容"].tolist()
        assert "P1" in table["内容"].tolist()
        assert "T-MECH" in table["内容"].tolist()
        assert "R-MECH-STOP" in table["内容"].tolist()
        rendered_json = [json.loads(item.value) for item in app.json]
        assert any(
            isinstance(item, list) and item and item[0].get("rule_id") == "R-MECH-STOP"
            for item in rendered_json
        )
        tool_names = {item.get("tool_name") for item in rendered_json if isinstance(item, dict)}
        assert tool_names >= {"lookup_asset", "lookup_rules", "propose_draft"}

        button(app, "确认并创建工单").click().run()

        assert not app.exception
        assert not app.error
        order = app.session_state["task"]["work_order"]
        assert order["payload"] == task["draft"]
        assert any(order["order_id"] in item.value for item in app.success)
        assert len(client.get("/work-orders").json()) == 1
        assert app.session_state["task"]["status"] == "completed"
        assert model.calls == 3


def test_ui_missing_info_then_cancel_draft_over_http(tmp_path, monkeypatch):
    calls = normal_calls()
    calls.insert(1, ("ask_user", {"question": "设备是否已经停机？"}))
    model = SequenceModel(calls)
    with running_backend(tmp_path, model) as (address, client):
        app = ui_app(monkeypatch, address)
        submit_report(app, "EQ-001 机械轴承异响。")
        task_id = app.session_state["task"]["task_id"]
        assert app.session_state["task"]["status"] == "question"
        assert any("是否停机" in item.value for item in app.info)

        text_input(app, "补充设备、位置、故障或停机信息").set_value("已经停机。")
        button(app, "提交补充信息").click().run()

        assert not app.exception
        assert not app.error
        assert app.session_state["task"]["task_id"] == task_id
        assert app.session_state["task"]["status"] == "awaiting_confirmation"
        assert app.session_state["task"]["draft"]["stopped"] is True
        assert client.get("/work-orders").json() == []

        button(app, "取消任务").click().run()

        assert not app.exception
        assert not app.error
        assert app.session_state["task"]["status"] == "cancelled"
        assert any("未创建工单" in item.value for item in app.success)
        assert client.get("/work-orders").json() == []
        assert client.get(f"/tasks/{task_id}").json()["status"] == "cancelled"
        assert model.calls == 4


def test_ui_can_cancel_question_without_creating_order(tmp_path, monkeypatch):
    model = SequenceModel(
        [
            ("lookup_asset", {"query": "EQ-001"}),
            ("ask_user", {"question": "设备是否已经停机？"}),
        ]
    )
    with running_backend(tmp_path, model) as (address, client):
        app = ui_app(monkeypatch, address)
        submit_report(app, "EQ-001 机械轴承异响。")
        assert app.session_state["task"]["status"] == "question"

        button(app, "取消当前报修").click().run()

        assert not app.exception
        assert not app.error
        assert app.session_state["task"]["status"] == "cancelled"
        assert client.get("/work-orders").json() == []
        assert model.calls == 2
