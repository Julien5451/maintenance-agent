"""One real, read-only model tool-call probe. Never prints credentials or raw errors."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from langchain_core.messages import HumanMessage

from maintenance_agent.config import ROOT, Settings
from maintenance_agent.diagnostics import safe_model_error
from maintenance_agent.domain import Catalog
from maintenance_agent.llm import build_model


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-parallel", action="store_true")
    args = parser.parse_args()
    started = perf_counter()
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "kind": "real_model_tool_call_probe",
        "simulated_business_data": True,
        "status": "blocked",
        "model_calls": 0,
        "tool_calls": 0,
        "checks": {},
    }
    code = 2
    stage = "settings"
    try:
        settings = Settings()
        report["configuration_present"] = {
            "LLM_MODEL": bool(settings.llm_model),
            "LLM_BASE_URL": bool(settings.llm_base_url),
            "LLM_API_KEY": bool(settings.llm_api_key.get_secret_value()),
        }
        if not settings.model_ready:
            report["reason"] = "missing_model_configuration"
        else:
            stage = "build_model"
            model = build_model(settings)
            report["model"] = settings.llm_model
            report["model_calls"] = 1
            stage = "request"
            options = {"parallel_tool_calls": False} if args.no_parallel else {}
            report["parallel_tool_calls_requested"] = options.get("parallel_tool_calls")
            response = model.invoke(
                [HumanMessage(content="查询设备EQ-001，请调用lookup_asset。只查询模拟台账。")],
                tool_choice={"type": "function", "function": {"name": "lookup_asset"}},
                **options,
            )
            calls = response.tool_calls
            report["tool_calls"] = len(calls)
            checks = report["checks"]
            checks["no_invalid_tool_calls"] = not response.invalid_tool_calls
            checks["one_tool_call"] = len(calls) == 1
            checks["lookup_asset_called"] = (
                len(calls) == 1 and calls[0].get("name") == "lookup_asset"
            )
            args = calls[0].get("args", {}) if len(calls) == 1 else {}
            checks["correct_query"] = args == {"query": "EQ-001"}
            if all(checks.values()):
                result = Catalog().lookup_asset(**args)
                report["tool"] = {
                    "name": "lookup_asset",
                    "arguments": args,
                    "result_summary": {
                        "count": result["count"],
                        "asset_ids": [item["asset_id"] for item in result["matches"]],
                        "simulated": True,
                    },
                }
                checks["catalog_result_correct"] = (
                    result["count"] == 1 and result["matches"][0]["asset_id"] == "EQ-001"
                )
            report["status"] = "passed" if all(checks.values()) else "failed"
            if report["status"] == "failed":
                report["diagnostic"] = {
                    "category": "tool_calling_compatibility",
                    "reason": "工具调用响应不满足固定查询、参数与解析要求；详见checks",
                }
            code = 0 if report["status"] == "passed" else 1
    except Exception as exc:  # noqa: BLE001 - redact all provider errors at the CLI boundary
        report["status"] = "failed"
        report["reason"] = "configuration_or_model_probe_error"
        report["error_type"] = type(exc).__name__
        report["diagnostic"] = safe_model_error(exc, stage=stage)
        code = 1
    report["elapsed_seconds"] = round(perf_counter() - started, 3)
    path = ROOT / "reports" / "model_probe.json"
    history = (
        ROOT
        / "reports"
        / "model_probes"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    )
    write_report(report, history)
    write_report(report, path)
    print(json.dumps({"status": report["status"], "report": str(path)}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
