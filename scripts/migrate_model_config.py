"""Copy only the three chat-model settings from an authorized local dotenv file."""

import argparse
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values, set_key

from maintenance_agent.config import ROOT

MAPPING = {
    "CHAT_MODEL": "LLM_MODEL",
    "CHAT_BASE_URL": "LLM_BASE_URL",
    "CHAT_API_KEY": "LLM_API_KEY",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    target = ROOT / ".env"
    original = target.read_text(encoding="utf-8")
    report = {"timestamp": datetime.now(UTC).isoformat(), "status": "failed", "mapping": MAPPING}
    try:
        source_bytes = args.source.read_bytes()
        source = dotenv_values(
            stream=io.StringIO(source_bytes.decode("utf-8-sig")), interpolate=False
        )
        before = dotenv_values(stream=io.StringIO(original), interpolate=False)
        if not all(isinstance(source.get(key), str) and source[key].strip() for key in MAPPING):
            report["reason"] = "source_chat_model_settings_missing"
            raise ValueError("missing source fields")
        for old, new in MAPPING.items():
            set_key(target, new, source[old], quote_mode="always")
        after = dotenv_values(target, interpolate=False)
        targets = set(MAPPING.values())
        report["other_settings_unchanged"] = {
            key: value for key, value in before.items() if key not in targets
        } == {key: value for key, value in after.items() if key not in targets}
        report["source_file_unchanged"] = (
            hashlib.sha256(source_bytes).digest()
            == hashlib.sha256(args.source.read_bytes()).digest()
        )
        report["mapped_values_verified"] = all(
            after[new] == source[old] for old, new in MAPPING.items()
        )
        if not all(
            report.get(key)
            for key in (
                "other_settings_unchanged",
                "source_file_unchanged",
                "mapped_values_verified",
            )
        ):
            raise ValueError("verification failed")
        report["status"] = "passed"
    except Exception as exc:
        target.write_text(original, encoding="utf-8")
        report["error_type"] = type(exc).__name__
        report.setdefault("reason", "migration_failed_and_target_restored")
    (ROOT / "reports" / "config_migration.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
