"""Local Docker/HTTP verification. Model responses are explicitly scripted mock data."""

import json
import subprocess
import time
from datetime import UTC, datetime

import httpx

from maintenance_agent.config import ROOT

SEED = """
import json
from collections import deque
from langchain_core.messages import AIMessage
from maintenance_agent.service import MaintenanceService
class MockModel:
    def __init__(self):
        self.calls = deque([
            ("lookup_asset", {"query":"EQ-001"}),
            ("lookup_rules", {"asset_id":"EQ-001","category":"mechanical","stopped":True}),
            ("propose_draft", {"asset_id":"EQ-001","category":"mechanical",
                               "stopped":True,"fault_summary":"轴承异响"})])
    def invoke(self, messages):
        name, args = self.calls.popleft()
        return AIMessage(content="",tool_calls=[{"name":name,"args":args,"id":name}])
s=MaintenanceService(model=MockModel())
try:
    t=s.create_task("EQ-001 机械轴承异响，已经停机。")
    print(json.dumps({"task_id":t["task_id"],"draft":t["draft"],
                      "status":t["status"],"model_calls":t["model_calls"]}))
finally:
    s.close()
"""


def docker(*args):
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def seed():
    return json.loads(docker("exec", "-T", "api", "python", "-c", SEED))


def main():
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "kind": "local_container_smoke",
        "model_mode": "scripted_mock",
        "real_model_evaluation": False,
        "simulated": True,
        "checks": {},
    }
    checks = report["checks"]
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False) as client:
            health = client.get("/health")
            checks["api_health"] = health.status_code == 200
            checks["ui_health"] = (
                httpx.get("http://127.0.0.1:8501/_stcore/health", trust_env=False).text == "ok"
            )
            pending = seed()
            task_id = pending["task_id"]
            report["task_id"] = task_id
            saved = client.get(f"/tasks/{task_id}").json()
            checks["unapproved_no_order"] = not any(
                o["request_key"] == task_id for o in client.get("/work-orders").json()
            )
            checks["pending_visible_over_http"] = saved["status"] == "awaiting_confirmation"
            docker("restart", "api")
            for attempt in range(30):
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            recovered = client.get(f"/tasks/{task_id}").json()
            checks["restart_recovers_identical_draft"] = (
                recovered["status"] == "awaiting_confirmation"
                and recovered["draft"] == pending["draft"]
            )
            action = {"action": "confirm", "draft_id": recovered["draft"]["draft_id"]}
            first = client.post(f"/tasks/{task_id}/responses", json=action).json()
            second = client.post(f"/tasks/{task_id}/responses", json=action).json()
            checks["confirmed_completed"] = first["status"] == "completed"
            checks["confirmation_does_not_call_model"] = (
                first["model_calls"] == pending["model_calls"]
            )
            orders = [o for o in client.get("/work-orders").json() if o["request_key"] == task_id]
            checks["duplicate_confirmation_one_order"] = (
                len(orders) == 1 and first["work_order"] == second["work_order"]
            )
            checks["stored_order_equals_approved_draft"] = orders[0]["payload"] == pending["draft"]
            cancelled = seed()
            cancelled_id = cancelled["task_id"]
            result = client.post(
                f"/tasks/{cancelled_id}/responses", json={"action": "cancel"}
            ).json()
            checks["cancel_no_order"] = result["status"] == "cancelled" and not any(
                o["request_key"] == cancelled_id for o in client.get("/work-orders").json()
            )
            report["work_order_id"] = orders[0]["order_id"]
        report["status"] = "passed" if all(checks.values()) else "failed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
    path = ROOT / "reports" / "container_smoke.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
