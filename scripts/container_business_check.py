"""One real HTTP run of three explicitly marked container business scenarios.

Adds two simulated work orders and one cancelled simulated task. Existing rows are
verified unchanged. The model is real; no seeded graph state or scripted responses.
"""

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def existing_rows(task_ids=None):
    source = """
import hashlib,json,sqlite3
c=sqlite3.connect("/app/runtime/business.sqlite")
cp=sqlite3.connect("/app/runtime/checkpoints.sqlite")
ids=IDS
if ids is None: ids=[r[0] for r in c.execute("select task_id from tasks order by task_id")]
rows={}
for task_id in ids:
    records={}
    for table, key in (("tasks","task_id"),("drafts","task_id"),("events","task_id"),("work_orders","request_key")):
        values=list(c.execute(f"select * from {table} where {key}=?",(task_id,)))
        records[table]={"rows":len(values),"sha256":hashlib.sha256(repr(sorted(values,key=repr)).encode()).hexdigest()}
    for table in ("checkpoints","writes"):
        values=list(cp.execute(f"select * from {table} where thread_id=?",(task_id,)))
        records["checkpoint."+table]={"rows":len(values),"sha256":hashlib.sha256(repr(sorted(values,key=repr)).encode()).hexdigest()}
    rows[task_id]=records
print(json.dumps({"task_ids":ids,"rows":rows},sort_keys=True))
c.close()
cp.close()
""".replace("IDS", repr(task_ids))
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-c", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(proc.stdout)


def metrics(task):
    events = task.get("events", [])
    calls = [event for event in events if event.get("event_type") == "tool_call"]
    questions = []
    for event in calls:
        if event.get("tool_name") == "ask_user" and event.get("status") == "ok":
            try:
                questions.append(json.loads(event.get("result_summary", "{}"))["question"])
            except (ValueError, KeyError, TypeError):
                questions.append(None)
    return {
        "model_calls": task.get("model_calls", 0),
        "tool_rounds": task.get("tool_rounds", 0),
        "tool_rejections": sum(event.get("status") == "rejected" for event in calls),
        "question_tool_calls": len(questions),
        "repeated_question_texts": len(questions) - len(set(questions)),
        "redundant_question_attempts": sum(
            event.get("event_type") == "redundant_question" for event in events
        ),
        "automatic_tool_steps": sum(
            event.get("event_type") == "automatic_tool" for event in events
        ),
        "six_round_exhausted": task.get("status") == "limit_reached",
    }


def one_case(client, name, text, followup=None, cancel=False):
    started = time.perf_counter()
    checks = {}
    record = {"scenario": name, "input": text, "checks": checks, "status": "failed"}
    task = None
    try:
        response = client.post("/tasks", json={"text": text})
        response.raise_for_status()
        task = response.json()
        record["task_id"] = task["task_id"]
        record["initial_status"] = task["status"]
        checks["unapproved_no_order"] = not any(
            order["request_key"] == task["task_id"] for order in client.get("/work-orders").json()
        )
        if followup is not None:
            checks["followup_requested"] = task["status"] == "question"
            record["followup"] = followup
            if task["status"] == "question":
                response = client.post(
                    f"/tasks/{task['task_id']}/responses",
                    json={"action": "provide_info", "text": followup},
                )
                response.raise_for_status()
                task = response.json()
        checks["valid_draft_reached"] = task["status"] == "awaiting_confirmation"
        if checks["valid_draft_reached"]:
            draft = task["draft"]
            calls_before = task["model_calls"]
            if cancel:
                response = client.post(
                    f"/tasks/{task['task_id']}/responses", json={"action": "cancel"}
                )
                response.raise_for_status()
                task = response.json()
                checks["cancelled"] = task["status"] == "cancelled"
                checks["cancel_no_order"] = not any(
                    order["request_key"] == task["task_id"]
                    for order in client.get("/work-orders").json()
                )
            else:
                action = {"action": "confirm", "draft_id": draft["draft_id"]}
                response = client.post(f"/tasks/{task['task_id']}/responses", json=action)
                response.raise_for_status()
                task = response.json()
                checks["completed"] = task["status"] == "completed"
                checks["stored_order_matches_saved_draft"] = (task.get("work_order") or {}).get(
                    "payload"
                ) == draft
                response = client.post(f"/tasks/{task['task_id']}/responses", json=action)
                response.raise_for_status()
                second = response.json()
                orders = [
                    order
                    for order in client.get("/work-orders").json()
                    if order["request_key"] == task["task_id"]
                ]
                checks["repeat_confirmation_one_order"] = len(orders) == 1 and second.get(
                    "work_order"
                ) == task.get("work_order")
            checks["human_execution_no_model_call"] = task["model_calls"] == calls_before
        checks["at_most_six_rounds"] = task.get("tool_rounds", 0) <= 6
        record["actual"] = task
        record.update(metrics(task))
        record["status"] = "passed" if checks and all(checks.values()) else "failed"
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        # Do not retain raw HTTP/provider exception strings or headers.
        if task is not None:
            record["actual"] = task
            record.update(metrics(task))
    finally:
        if task is not None and task.get("status") in {"question", "awaiting_confirmation"}:
            try:
                client.post(
                    f"/tasks/{task['task_id']}/responses", json={"action": "cancel"}
                ).raise_for_status()
                record["test_cleanup"] = "cancelled_unfinished_test_task"
            except httpx.HTTPError:
                record["test_cleanup"] = "not_completed"
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return record


def main():
    now = datetime.now(UTC)
    run_id = now.strftime("%Y%m%dT%H%M%S%fZ")
    started = time.perf_counter()
    report = {
        "timestamp": now.isoformat(),
        "run_id": run_id,
        "kind": "container_real_http_business_validation",
        "model_mode": "real_api",
        "simulated_business_data": True,
        "status": "failed",
        "results": [],
    }
    try:
        before = existing_rows()
        report["preexisting_before"] = before
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=360, trust_env=False) as client:
            health = client.get("/health")
            health.raise_for_status()
            report["api_model_configured"] = health.json().get("model_configured")
            cases = [
                (
                    "normal_confirm",
                    "【容器真实测试：正常；模拟数据】EQ-001 机械轴承异响，已经停机。",
                    None,
                    False,
                ),
                (
                    "followup_confirm",
                    "【容器真实测试：补充；模拟数据】EQ-004 机械轴承振动明显增大。",
                    "已经停机。",
                    False,
                ),
                (
                    "cancel",
                    "【容器真实测试：取消；模拟数据】EQ-005 电机电气保护跳闸，已经停机。",
                    None,
                    True,
                ),
            ]
            for name, text, followup, cancel in cases:
                result = one_case(client, name, text, followup, cancel)
                report["results"].append(result)
                # Retain intermediate failures if a later scenario is interrupted.
                (ROOT / "reports" / f"network_fix_business_{run_id}.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
        after = existing_rows(before["task_ids"])
        report["preexisting_after"] = after
        report["preexisting_task_ids"] = before["task_ids"]
        report["preexisting_rows_unchanged"] = before == after
        report["status"] = (
            "passed"
            if report["preexisting_rows_unchanged"]
            and all(item["status"] == "passed" for item in report["results"])
            else "failed"
        )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    report["summary"] = {
        "passed": sum(item["status"] == "passed" for item in report["results"]),
        "failed": sum(item["status"] != "passed" for item in report["results"]),
        "model_calls": sum(item.get("model_calls", 0) for item in report["results"]),
        "tool_rejections": sum(item.get("tool_rejections", 0) for item in report["results"]),
        "six_round_exhaustions": sum(
            item.get("six_round_exhausted", False) for item in report["results"]
        ),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    path = ROOT / "reports" / f"network_fix_business_{run_id}.json"
    path.write_text(payload, encoding="utf-8")
    (ROOT / "reports" / "network_fix_business_latest.json").write_text(payload, encoding="utf-8")
    print(
        json.dumps(
            {"status": report["status"], "summary": report["summary"], "report": str(path)},
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
