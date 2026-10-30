"""Metric regressions; no model calls and no changes to business scoring."""

import hashlib
import runpy

from maintenance_agent.config import ROOT

EVALUATION = runpy.run_path(str(ROOT / "scripts" / "evaluate.py"))
metrics = EVALUATION["metrics"]
summarize_results = EVALUATION["summarize_results"]


def test_model_attempts_and_automatic_steps_are_not_mixed():
    task = {
        "model_calls": 2,
        "tool_rounds": 2,
        "status": "limit_reached",
        "events": [
            {"event_type": "model_call", "status": "ok"},
            {"event_type": "model_call", "status": "error"},
            {"event_type": "tool_call", "tool_name": "lookup_asset", "status": "ok"},
            {"event_type": "automatic_tool", "tool_name": "lookup_asset", "status": "ok"},
            {"event_type": "automatic_tool", "tool_name": "lookup_rules", "status": "rejected"},
        ],
    }
    result = metrics(task)
    assert result["model_calls"] == result["tool_rounds"] == 2
    assert result["model_successes"] == result["model_errors"] == 1
    assert result["automatic_tool_steps"] == 2
    assert result["tool_calls"] == 1
    assert result["all_tool_records"] == 3
    assert result["automatic_tool_rejections"] == result["tool_rejections"] == 1
    assert result["six_round_exhaustions"] == 1


def test_redundant_question_annotation_is_not_counted_twice():
    task = {
        "events": [
            {
                "event_type": "tool_call",
                "tool_name": "ask_user",
                "status": "ok",
                "args": {"question": "请确认是否停机？"},
            },
            {
                "event_type": "tool_call",
                "tool_name": "ask_user",
                "status": "ok",
                "args": {"question": " 请确认是否停机? "},
            },
            {
                "event_type": "tool_call",
                "tool_name": "ask_user",
                "status": "rejected",
                "rejection_code": "redundant_question",
                "args": {"question": "设备已停机吗？"},
            },
            {"event_type": "question_rejected", "reason": "redundant_question"},
        ]
    }
    result = metrics(task)
    assert result["accepted_duplicate_questions"] == 1
    assert result["redundant_question_rejections"] == 1
    assert result["repeated_questions"] == 2
    assert result["tool_rejections"] == 1


def test_visible_automatic_question_repeats_use_conversation_snapshots():
    steps = [
        {"status": "question", "question": "请补充具体设备编号。"},
        {"status": "question", "question": "请补充具体设备编号。"},
        {"status": "cancelled", "question": "请补充具体设备编号。"},
    ]
    result = metrics({"events": []}, steps)
    assert result["accepted_duplicate_questions"] == 1


def test_replaced_question_uses_actual_result_and_is_not_double_counted():
    task = {
        "events": [
            {
                "event_type": "tool_call",
                "tool_name": "ask_user",
                "status": "ok",
                "args": {"question": requested},
                "result_summary": '{"question":"请补充具体设备编号。","requested_question":"原请求"}',
            }
            for requested in ("请确认是否停机？", "请重新描述故障。")
        ]
    }
    steps = [{"status": "question", "question": "请补充具体设备编号。"}] * 2
    result = metrics(task, steps)
    assert result["accepted_duplicate_questions"] == 1
    assert [event["displayed_question"] for event in result["question_records"]] == [
        "请补充具体设备编号。",
        "请补充具体设备编号。",
    ]
    assert result["question_records"][0]["requested_question"] == "请确认是否停机？"


def test_repeated_model_wording_is_not_a_displayed_repeat_after_replacement():
    task = {
        "events": [
            {
                "event_type": "tool_call",
                "tool_name": "ask_user",
                "status": "ok",
                "args": {"question": "请确认全部信息。"},
                "result_summary": {"question": actual},
            }
            for actual in ("请提供设备编号。", "请补充是否停机。")
        ]
    }
    result = metrics(task)
    assert result["accepted_duplicate_questions"] == 0
    assert result["question_records"][0]["args"]["question"] == "请确认全部信息。"


def test_summary_retains_failures_and_excludes_unexecuted_from_pass_rate():
    results = [
        {"pass": True, "execution_status": "finished", "model_calls": 3, "model_successes": 3},
        {
            "pass": False,
            "execution_status": "finished",
            "model_calls": 6,
            "model_successes": 6,
            "six_round_exhaustions": 1,
            "tool_rejections": 2,
        },
        {"pass": None, "execution_status": "blocked"},
    ]
    summary = summarize_results(results, 1.23456)
    assert summary["passed"] == summary["failed"] == summary["blocked"] == 1
    assert summary["total"] == 3
    assert summary["pass_rate"] == 0.5
    assert summary["model_calls"] == summary["model_successes"] == 9
    assert summary["tool_rejections"] == 2
    assert summary["six_round_exhaustions"] == 1
    assert summary["elapsed_seconds"] == 1.235


def test_original_and_new_cases_remain_frozen():
    assert hashlib.sha256((ROOT / "evals" / "cases.json").read_bytes()).hexdigest() == (
        "4de390acedbff199adc717f76e35e93915323971b8698b1185126e82afa6d849"
    )
    assert hashlib.sha256(
        (ROOT / "evals" / "generalization_cases.json").read_bytes()
    ).hexdigest() == ("a3f44bb06bbc3df4010abf614cdc929fd586eddadb9eb83a0e49fa67b0a25837")
