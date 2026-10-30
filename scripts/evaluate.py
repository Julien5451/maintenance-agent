"""Run the frozen cases against the configured real model; keep every result."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from maintenance_agent.config import ROOT, Settings

if TYPE_CHECKING:
    from maintenance_agent.service import MaintenanceService

ORIGINAL_CASES_SHA256 = "4de390acedbff199adc717f76e35e93915323971b8698b1185126e82afa6d849"
METRIC_DEFINITIONS = {
    "model_calls": "应用 invoke 尝试数；model_successes/model_errors 按 model_call 事件状态分别计数",
    "tool_rejections": "tool_call 和 automatic_tool 中 status=rejected 的条数，分别列出子计数",
    "accepted_duplicate_questions": "实际显示的追问重复次数：accepted ask_user优先读result_summary.question，旧记录回退args；自动追问从会话快照合并且不双计",
    "redundant_question_rejections": "仅 tool_call.rejection_code=redundant_question，解释事件不重复计数",
    "repeated_questions": "accepted_duplicate_questions + redundant_question_rejections；不是LLM语义评分",
    "six_round_exhaustions": "最终业务状态为 limit_reached 的任务数",
    "automatic_tool_steps": "automatic_tool 事件条数，不加入模型调用次数或六轮工具交互计数",
    "all_tool_records": "tool_calls + automatic_tool_steps，包含拒绝记录，不等同成功执行次数",
    "pass_rate": "passed/(passed+failed)，未执行的 blocked 不计入分母；原业务 checks 不变",
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def order_count(service: MaintenanceService, task_id: str) -> int:
    return service.store.conn.execute(
        "SELECT COUNT(*) FROM work_orders WHERE request_key=?", (task_id,)
    ).fetchone()[0]


def project(service: MaintenanceService, task: dict) -> dict:
    snapshot = service.workflow.graph.get_state(service.config(task["task_id"]))
    draft = task.get("draft") or {}
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "reason": task.get("reason"),
        "asset_id": draft.get("asset_id") or snapshot.values.get("resolved_asset_id"),
        "priority": draft.get("priority"),
        "team_id": draft.get("team_id"),
        "rule_ids": draft.get("rule_ids", []),
        "work_order_count": order_count(service, task["task_id"]),
        "draft": task.get("draft"),
        "question": task.get("question"),
        "order_id": (task.get("work_order") or {}).get("order_id"),
    }


def normalize_question(question: str) -> str:
    """Exact-repeat measurement only; it does not decide semantic necessity."""
    normalized = unicodedata.normalize("NFKC", question).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def displayed_question(event: dict) -> str:
    """The server can replace the requested wording with a missing-fields question."""
    result = event.get("result_summary")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if isinstance(result, dict):
        actual = result.get("question")
        if isinstance(actual, str) and actual.strip():
            return actual
    # Historical records may omit structured tool results.
    requested = event.get("args", {}).get("question", "")
    return requested if isinstance(requested, str) else ""


def metrics(task: dict, conversation_steps: list[dict] | None = None) -> dict:
    events = task.get("events", [])
    tools = [event for event in events if event["event_type"] == "tool_call"]
    automatic = [event for event in events if event["event_type"] == "automatic_tool"]
    model_events = [event for event in events if event["event_type"] == "model_call"]
    questions = [event for event in tools if event.get("tool_name") == "ask_user"]
    accepted_questions = [
        displayed_question(event) for event in questions if event.get("status") == "ok"
    ]
    question_counts = Counter(
        normalize_question(question) for question in accepted_questions if question
    )
    if conversation_steps is not None:
        visible = Counter(
            normalize_question(step["question"])
            for step in conversation_steps
            if step.get("status") == "question"
            and isinstance(step.get("question"), str)
            and step["question"].strip()
        )
        # Snapshots also capture graph-generated questions. A tool question already
        # represented by a snapshot must not be counted a second time.
        question_counts |= visible
    duplicates = sum(count - 1 for count in question_counts.values())
    redundant = sum(event.get("rejection_code") == "redundant_question" for event in questions)
    rejected_model = sum(event.get("status") == "rejected" for event in tools)
    rejected_automatic = sum(event.get("status") == "rejected" for event in automatic)
    return {
        "model_calls": task.get("model_calls", 0),
        "model_call_events": len(model_events),
        "model_successes": sum(event.get("status") == "ok" for event in model_events),
        "model_errors": sum(event.get("status") == "error" for event in model_events),
        "model_call_records": model_events,
        "tool_rounds": task.get("tool_rounds", 0),
        "tool_calls": len(tools),
        "all_tool_records": len(tools) + len(automatic),
        "confirmed_write_operations": sum(
            event.get("tool_name") == "create_work_order" for event in tools
        ),
        "tool_rejections": rejected_model + rejected_automatic,
        "model_tool_rejections": rejected_model,
        "automatic_tool_rejections": rejected_automatic,
        "tool_calls_by_name": dict(Counter(event["tool_name"] for event in tools)),
        "tool_records": tools,
        "automatic_tool_steps": len(automatic),
        "automatic_tool_records": automatic,
        "question_records": [
            {
                **event,
                "requested_question": event.get("args", {}).get("question"),
                "displayed_question": displayed_question(event)
                if event.get("status") == "ok"
                else None,
            }
            for event in questions
        ],
        "accepted_duplicate_questions": duplicates,
        "redundant_question_rejections": redundant,
        "repeated_questions": duplicates + redundant,
        "six_round_exhaustions": int(task.get("status") == "limit_reached"),
        "status_changes": [
            {key: event.get(key) for key in ("previous", "status", "created_at")}
            for event in events
            if event["event_type"] == "status_change"
        ],
    }


def conversation_step(operation: str, task: dict, text: str | None = None) -> dict:
    return {
        "operation": operation,
        "text": text,
        "status": task["status"],
        "question": task.get("question"),
        "model_calls": task.get("model_calls", 0),
        "tool_rounds": task.get("tool_rounds", 0),
        "draft_id": (task.get("draft") or {}).get("draft_id"),
    }


def summarize_results(results: list[dict], elapsed_seconds: float) -> dict:
    passed = sum(result["pass"] is True for result in results)
    failed = sum(result["pass"] is False for result in results)
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "blocked": sum(result["execution_status"] == "blocked" for result in results),
        "pass_rate": round(passed / (passed + failed), 4) if passed + failed else None,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }
    for key in (
        "model_calls",
        "model_successes",
        "model_errors",
        "tool_calls",
        "all_tool_records",
        "confirmed_write_operations",
        "tool_rejections",
        "model_tool_rejections",
        "automatic_tool_rejections",
        "automatic_tool_steps",
        "accepted_duplicate_questions",
        "redundant_question_rejections",
        "repeated_questions",
        "six_round_exhaustions",
    ):
        summary[key] = sum(result.get(key, 0) for result in results)
    return summary


def run_case(case: dict, settings: Settings, runtime_dir: Path) -> dict:
    from maintenance_agent.service import MaintenanceService

    started = perf_counter()
    result = {
        "id": case["id"],
        "name": case["name"],
        "kind": "real_model_evaluation",
        "simulated_business_data": True,
        "execution_status": "finished",
        "input": case["input"],
        "followups": case.get("followups", []),
        "action": case.get("action", "none"),
        "expected": case["expected"],
        "checks": {},
        "actual": None,
        "pass": False,
        "conversation_steps": [],
    }
    checks = result["checks"]
    service = None
    task = None
    try:
        service = MaintenanceService(
            settings=settings.model_copy(
                update={
                    "database_dir": runtime_dir / case["id"],
                }
            )
        )
        task = service.create_task(case["input"])
        result["conversation_steps"].append(conversation_step("create", task, case["input"]))
        checks["no_order_before_human_action"] = order_count(service, task["task_id"]) == 0
        for number, text in enumerate(case.get("followups", []), start=1):
            checks[f"followup_{number}_requested"] = task["status"] == "question"
            if task["status"] != "question":
                break
            task = service.respond(task["task_id"], "provide_info", text=text)
            result["conversation_steps"].append(conversation_step("provide_info", task, text))
            checks[f"no_order_after_followup_{number}"] = order_count(service, task["task_id"]) == 0
        action = case.get("action", "none")
        if action in {"confirm", "cancel"}:
            checks["action_precondition"] = task["status"] == "awaiting_confirmation"
            if checks["action_precondition"]:
                calls_before = task["model_calls"]
                task = service.respond(
                    task["task_id"],
                    action,
                    draft_id=task["draft"]["draft_id"] if action == "confirm" else None,
                )
                result["conversation_steps"].append(conversation_step(action, task))
                checks["no_model_call_during_human_execution"] = task["model_calls"] == calls_before

        actual = project(service, task)
        result["actual"] = actual
        result.update(metrics(task, result["conversation_steps"]))
        for key, expected in case["expected"].items():
            value = actual.get(key)
            checks[f"expected_{key}"] = (
                sorted(value or []) == sorted(expected) if key == "rule_ids" else value == expected
            )
        checks["at_most_six_tool_rounds"] = task["tool_rounds"] <= 6
        checks["model_was_called"] = task["model_calls"] > 0
        checks["model_call_count_matches_log"] = (
            result["model_calls"] == result["model_call_events"]
        )
        if actual["status"] == "question":
            checks["question_is_visible"] = bool(actual["question"])
        draft = task.get("draft")
        if draft:
            asset = service.catalog.asset(draft["asset_id"])
            rules = service.catalog.lookup_rules(
                draft["asset_id"],
                draft["category"],
                draft["stopped"],
            )["rules"]
            checks["draft_matches_catalog_and_rules"] = bool(
                asset
                and len(rules) == 1
                and draft["location"] == asset["location"]
                and draft["team_id"] in asset["team_ids"]
                and draft["team_id"] == rules[0]["team_id"]
                and draft["priority"] == rules[0]["priority"]
                and draft["rule_ids"] == [rules[0]["rule_id"]]
            )
        if actual["work_order_count"]:
            order = service.store.order_for_task(task["task_id"])
            decision = service.store.require_task(task["task_id"])
            checks["stored_order_equals_approved_draft"] = bool(
                order
                and order["payload"] == draft
                and decision["decision"] == "confirm"
                and decision["approved_draft_id"] == draft["draft_id"]
                and order["draft_id"] == draft["draft_id"]
            )
        result["pass"] = bool(checks) and all(checks.values())
    except Exception as exc:  # noqa: BLE001 - retain failed cases without exposing provider errors
        result["execution_status"] = "error"
        result["error_type"] = type(exc).__name__
        checks["no_runner_or_service_error"] = False
        # Never retain provider exception messages, URLs, response bodies or headers.
        if service is not None and task is not None:
            try:
                task = service.get_task(task["task_id"])
                result["actual"] = project(service, task)
                result.update(metrics(task, result["conversation_steps"]))
            except Exception as recovery_exc:  # noqa: BLE001 - best-effort sanitized recovery
                result["recovery_error_type"] = type(recovery_exc).__name__
    finally:
        if service is not None:
            try:
                service.close()
            except Exception as exc:  # noqa: BLE001 - record cleanup failure without raw error
                result["close_error_type"] = type(exc).__name__
                checks["service_closed"] = False
                result["pass"] = False
    result["elapsed_seconds"] = round(perf_counter() - started, 3)
    for key in (
        "model_calls",
        "model_successes",
        "model_errors",
        "tool_rounds",
        "tool_calls",
        "all_tool_records",
        "confirmed_write_operations",
        "tool_rejections",
        "model_tool_rejections",
        "automatic_tool_rejections",
        "automatic_tool_steps",
        "accepted_duplicate_questions",
        "redundant_question_rejections",
        "repeated_questions",
        "six_round_exhaustions",
    ):
        result.setdefault(key, 0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals" / "cases.json")
    parser.add_argument("--reports", type=Path, default=ROOT / "reports" / "real_eval")
    args = parser.parse_args()
    started = perf_counter()
    timestamp = datetime.now(UTC)
    run_id = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    source_hash = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    cases = read_json(args.cases)
    report = {
        "run_id": run_id,
        "timestamp": timestamp.isoformat(),
        "kind": "real_model_evaluation",
        "simulated_business_data": True,
        "source_cases": str(args.cases),
        "source_cases_sha256": source_hash,
        "metric_definitions": METRIC_DEFINITIONS,
        "status": "blocked",
        "results": [],
        "mock_results_included": False,
    }
    settings = None
    try:
        settings = Settings()
        report["model"] = settings.llm_model
        ready = settings.model_ready
        blocked_reason = "missing_model_configuration"
    except Exception as exc:  # noqa: BLE001 - report invalid settings without secret input values
        ready = False
        blocked_reason = "invalid_model_configuration"
        report["error_type"] = type(exc).__name__
    original_source = args.cases.resolve() == (ROOT / "evals" / "cases.json").resolve()
    if original_source and source_hash != ORIGINAL_CASES_SHA256:
        ready = False
        blocked_reason = "original_frozen_cases_hash_mismatch"
    if not ready:
        report["reason"] = blocked_reason
        report["real_model_run_performed"] = False
        report["results"] = [
            {
                "id": case["id"],
                "name": case["name"],
                "execution_status": "blocked",
                "reason": blocked_reason,
                "pass": None,
                "checks": {},
                "expected": case["expected"],
                "actual": None,
                "model_calls": 0,
                "tool_rounds": 0,
                "tool_calls": 0,
                "elapsed_seconds": 0,
            }
            for case in cases
        ]
        code = 2
    else:
        report["status"] = "running"
        report["real_model_run_performed"] = True
        runtime_dir = ROOT / "reports" / "eval-runtime" / run_id
        for case in cases:
            result = run_case(case, settings, runtime_dir)
            report["results"].append(result)
            report["summary"] = summarize_results(report["results"], perf_counter() - started)
            # Save after each case so an interrupted run still retains prior failures.
            write_json(args.reports / f"{run_id}.json", report)
        report["status"] = "completed"
        code = 0 if all(result["pass"] for result in report["results"]) else 1
    results = report["results"]
    report["summary"] = summarize_results(results, perf_counter() - started)
    report["source_cases_sha256_after"] = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    report["source_cases_unchanged"] = source_hash == report["source_cases_sha256_after"]
    if not report["source_cases_unchanged"]:
        report["reason"] = "source_cases_changed_during_run"
        code = 1
    write_json(args.reports / f"{run_id}.json", report)
    write_json(args.reports / "latest.json", report)
    print(
        json.dumps(
            {
                "status": report["status"],
                **report["summary"],
                "report": str(args.reports / f"{run_id}.json"),
            },
            ensure_ascii=False,
        )
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
