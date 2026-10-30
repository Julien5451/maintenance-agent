"""确定性台账与分派校验；只含模拟分类规则，不提供维修操作。"""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .config import ROOT


def _compact(value: str) -> str:
    """Compare identity labels without presentation punctuation or whitespace."""
    return re.sub(r"[\W_]+", "", value.casefold())


_IDENTITY_LABELS = re.compile(
    r"设备编号|资产编号|设备名称|设备位置|名称|位置|编号|设备|资产|机台|位于|名为|叫作|"
    r"这台|那台|这部|那部|id|为|是|在|的",
    re.IGNORECASE,
)


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    fault_summary: str = Field(min_length=2, max_length=500)
    stopped: StrictBool
    category: Literal["mechanical", "electrical", "control", "other"]


class Draft(Intake):
    draft_id: str
    location: str
    priority: Literal["P1", "P2", "P3"]
    team_id: str
    rule_ids: list[str]


class Catalog:
    def __init__(self, data_dir: Path = ROOT / "data"):
        self.assets = json.loads((data_dir / "assets.json").read_text(encoding="utf-8"))
        self.teams = json.loads((data_dir / "teams.json").read_text(encoding="utf-8"))
        self.rules = json.loads((data_dir / "rules.json").read_text(encoding="utf-8"))
        assert len(self.assets) == 10 and len(self.teams) == 3 and len(self.rules) == 6
        assert len({a["asset_id"] for a in self.assets}) == 10
        team_ids = {t["team_id"] for t in self.teams}
        assert all(r["team_id"] in team_ids for r in self.rules)
        # Recognize the actual ID namespace, including malformed IDs, and any
        # other explicit letter-prefix/numeric ID. Never truncate or fuzzy-fix one.
        prefixes = "|".join(
            sorted({re.escape(a["asset_id"].split("-", 1)[0]) for a in self.assets})
        )
        self._id_pattern = re.compile(
            rf"(?<![A-Za-z0-9_-])(?:(?:{prefixes})(?:-[A-Za-z0-9_-]+|\d[A-Za-z0-9_-]*)"
            r"|[A-Za-z][A-Za-z0-9]*-\d[A-Za-z0-9_-]*)",
            re.IGNORECASE,
        )
        self._identity_terms = {}
        for asset in self.assets:
            for kind, labels in (
                ("name", [asset["name"], *asset["aliases"]]),
                ("location", [asset["location"]]),
            ):
                for label in labels:
                    key = (kind, _compact(label))
                    item = self._identity_terms.setdefault(
                        key, {"value": label, "asset_ids": set()}
                    )
                    item["asset_ids"].add(asset["asset_id"])

    def _identity_clues(self, text: str) -> tuple[list[dict], str]:
        """Extract ledger terms, preferring a full label over its contained alias."""
        text = _compact(text)
        mentions = []
        for (kind, term), data in self._identity_terms.items():
            for match in re.finditer(re.escape(term), text):
                mentions.append(
                    {
                        "start": match.start(),
                        "end": match.end(),
                        "kind": kind,
                        "value": data["value"],
                        "asset_ids": sorted(data["asset_ids"]),
                    }
                )
        maximal = [
            m
            for m in mentions
            if not any(
                other["start"] <= m["start"]
                and other["end"] >= m["end"]
                and other["end"] - other["start"] > m["end"] - m["start"]
                for other in mentions
            )
        ]
        chars = list(text)
        for mention in maximal:
            chars[mention["start"] : mention["end"]] = [" "] * (mention["end"] - mention["start"])
        remaining = _IDENTITY_LABELS.sub("", "".join(chars)).strip()
        remaining = "".join(remaining.split())
        clues = [{k: v for k, v in m.items() if k not in {"start", "end"}} for m in maximal]
        # Preserve ordinary partial queries, e.g. 车床 or 1号工位, without
        # using their match to replace an explicit unknown ID.
        if remaining:
            owners = {
                asset["asset_id"]
                for asset in self.assets
                if any(
                    remaining in _compact(label)
                    for label in [asset["name"], *asset["aliases"], asset["location"]]
                )
            }
            if owners:
                clues.append(
                    {"kind": "partial_identity", "value": remaining, "asset_ids": sorted(owners)}
                )
                remaining = ""
        return clues, remaining

    def _asset_result(
        self, status, reason, matches=(), candidates=(), requested_ids=(), evidence=()
    ):
        return {
            "simulated": True,
            "status": status,
            "reason": reason,
            "matches": list(matches),
            "count": len(matches),
            "candidates": list(candidates),
            "requested_ids": list(requested_ids),
            "evidence": list(evidence),
            "needs_clarification": status in {"ambiguous", "conflict"},
        }

    def extract_asset_ids(self, text: str) -> list[str]:
        """Return complete explicit ID tokens, including unknown/malformed ones."""
        return list(dict.fromkeys(m.group().upper() for m in self._id_pattern.finditer(text)))

    def lookup_asset(self, query: str) -> dict:
        """Resolve a device reference: ID, names and/or locations, not repair prose.

        Explicit IDs are authoritative selectors, but accompanying identity
        labels must agree. Unknown IDs never fall back to names or aliases.
        """
        if not isinstance(query, str) or not query.strip():
            return self._asset_result("not_found", "empty_asset_query")
        requested_ids = self.extract_asset_ids(query)
        by_id = {a["asset_id"].upper(): a for a in self.assets}
        if len(requested_ids) > 1:
            candidates = [by_id[key] for key in requested_ids if key in by_id]
            # count must not accidentally advertise a unique match when only
            # one of several supplied IDs happens to be in the ledger.
            matches = candidates if len(candidates) > 1 else []
            return self._asset_result(
                "ambiguous", "multiple_asset_ids", matches, candidates, requested_ids
            )
        if requested_ids and requested_ids[0] not in by_id:
            return self._asset_result("not_found", "unknown_asset_id", requested_ids=requested_ids)
        without_ids = self._id_pattern.sub(" ", query)
        clues, unknown_description = self._identity_clues(without_ids)
        mentioned = {key for clue in clues for key in clue["asset_ids"]}
        if requested_ids:
            asset = by_id[requested_ids[0]]
            conflicting = any(asset["asset_id"] not in clue["asset_ids"] for clue in clues)
            if conflicting or unknown_description:
                candidates = [
                    a for a in self.assets if a["asset_id"] in mentioned | {asset["asset_id"]}
                ]
                reason = (
                    "id_description_conflict"
                    if conflicting
                    else "unrecognized_identity_description"
                )
                return self._asset_result(
                    "conflict",
                    reason,
                    candidates=candidates,
                    requested_ids=requested_ids,
                    evidence=clues,
                )
            return self._asset_result(
                "found", "exact_asset_id", [asset], requested_ids=requested_ids, evidence=clues
            )
        if not clues:
            return self._asset_result("not_found", "no_identity_match")
        matching = set.intersection(*(set(clue["asset_ids"]) for clue in clues))
        candidates = [a for a in self.assets if a["asset_id"] in mentioned]
        if not matching or unknown_description:
            return self._asset_result(
                "conflict",
                "identity_descriptions_conflict"
                if not matching
                else "unrecognized_identity_description",
                candidates=candidates,
                evidence=clues,
            )
        matches = [a for a in self.assets if a["asset_id"] in matching]
        return self._asset_result(
            "found" if len(matches) == 1 else "ambiguous",
            "identity_match" if len(matches) == 1 else "multiple_identity_matches",
            matches,
            matches,
            evidence=clues,
        )

    def asset(self, asset_id: str) -> dict:
        return next((a for a in self.assets if a["asset_id"] == asset_id), {})

    def lookup_rules(self, asset_id: str, category: str, stopped: bool) -> dict:
        a = self.asset(asset_id)
        matches = [
            r
            for r in self.rules
            if a
            and r["category"] == category
            and r["stopped"] == stopped
            and a["asset_type"] in r["asset_types"]
            and r["team_id"] in a["team_ids"]
            and r["team_id"] in {t["team_id"] for t in self.teams}
        ]
        return {"simulated": True, "rules": matches, "teams": self.teams, "count": len(matches)}

    def make_draft(self, task_id: str, intake: Intake) -> Draft:
        a = self.asset(intake.asset_id)
        rules = self.lookup_rules(intake.asset_id, intake.category, intake.stopped)["rules"]
        if not a or len(rules) != 1:
            raise ValueError("设备或规则不能唯一校验，不能形成草案")
        rule = rules[0]
        fields = {
            **intake.model_dump(),
            "location": a["location"],
            "priority": rule["priority"],
            "team_id": rule["team_id"],
            "rule_ids": [rule["rule_id"]],
        }
        digest = hashlib.sha256(
            json.dumps({"task_id": task_id, **fields}, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return Draft(draft_id=digest, **fields)
