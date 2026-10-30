"""Real, read-only tool-call probe in the API container; keep timestamped reports."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
import json, os, time
from langchain_core.messages import HumanMessage
from maintenance_agent.config import Settings
from maintenance_agent.domain import Catalog
from maintenance_agent.llm import build_model
started = time.perf_counter()
report = {"kind":"container_real_model_probe", "simulated_business_data":True,
          "model_calls":0,"database_writes":0,"parallel_tool_calls":False}
stage = "settings"
try:
    settings = Settings()
    report["model"] = settings.llm_model
    stage = "build_model"
    model = build_model(settings)
    report["runtime_proxy_present"] = bool(os.getenv("HTTPS_PROXY"))
    report["internal_api_no_proxy"] = "api" in os.getenv("NO_PROXY", "").split(",")
    stage = "request"
    report["model_calls"] = 1
    response = model.invoke([HumanMessage("查询设备EQ-001，请调用lookup_asset。只查询模拟台账。")],
        tool_choice={"type":"function","function":{"name":"lookup_asset"}},
        parallel_tool_calls=False)
    calls = response.tool_calls
    report["checks"] = {
        "no_invalid_tool_calls": not response.invalid_tool_calls,
        "one_lookup_asset_call": len(calls)==1 and calls[0]["name"]=="lookup_asset",
        "correct_query": len(calls)==1 and calls[0]["args"]=={"query":"EQ-001"},
        "catalog_result_correct": Catalog().lookup_asset("EQ-001")["count"]==1}
    report["status"] = "passed" if all(report["checks"].values()) else "failed"
except Exception as exc:
    report["status"] = "failed"
    report["diagnostic"] = safe_model_error(exc,stage=stage)
report["elapsed_seconds"] = round(time.perf_counter()-started,3)
print(json.dumps(report,ensure_ascii=False))
"""


def relay_count():
    path = ROOT / "reports" / "network_fix_relay_stats.json"
    return (
        json.loads(path.read_text(encoding="utf-8")).get("accepted", 0) if path.exists() else None
    )


def verify_internal_traffic():
    before = relay_count()
    checks = {}
    code = "import httpx; print(httpx.get('http://api:8000/health',trust_env=True).status_code)"
    for service in ("api", "ui"):
        process = subprocess.run(
            ["docker", "compose", "exec", "-T", service, "python", "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        checks[f"{service}_internal_api_http_200"] = (
            process.returncode == 0 and process.stdout.strip() == "200"
        )
    checks["internal_traffic_not_relayed"] = before == relay_count()
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "kind": "container_internal_http_routing",
        "checks": checks,
        "status": "passed" if all(checks.values()) else "failed",
        "model_calls": 0,
        "database_writes": 0,
    }
    path = ROOT / "reports" / "network_fix_internal_http.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--internal-only", action="store_true")
    args = parser.parse_args()
    if args.internal_only:
        return verify_internal_traffic()
    now = datetime.now(UTC)
    run_id = now.strftime("%Y%m%dT%H%M%S%fZ")
    before = relay_count()
    helper = (ROOT / "src" / "maintenance_agent" / "diagnostics.py").read_text(encoding="utf-8")
    try:
        process = subprocess.run(
            ["docker", "compose", "exec", "-T", "api", "python", "-"],
            input=helper + "\n" + PROBE,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        try:
            report = json.loads(process.stdout)
        except (ValueError, TypeError):
            report = {
                "kind": "container_real_model_probe",
                "status": "failed",
                "diagnostic": {
                    "category": "container_execution",
                    "reason": "容器探针未返回安全JSON",
                },
                "container_exit_code": process.returncode,
            }
    except (OSError, subprocess.TimeoutExpired) as exc:
        report = {
            "kind": "container_real_model_probe",
            "status": "failed",
            "diagnostic": {
                "category": "container_execution",
                "reason": "容器探针无法执行或执行超时",
                "error_type": type(exc).__name__,
            },
        }
    after = relay_count()
    report.update(timestamp=now.isoformat(), run_id=run_id)
    report["relay_connections_before"] = before
    report["relay_connections_after"] = after
    report["http_client_used_project_relay"] = (
        before is not None and after is not None and after > before
    )
    if report["status"] == "passed" and not report["http_client_used_project_relay"]:
        report["status"] = "failed"
        report["relay_evidence_missing"] = True
    history = ROOT / "reports" / f"network_fix_probe_{run_id}.json"
    latest = ROOT / "reports" / "network_fix_probe_latest.json"
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    history.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(history),
                "http_client_used_project_relay": report["http_client_used_project_relay"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
