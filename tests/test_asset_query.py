"""Regression checks for ledger identity parsing, independent of model wording."""

import pytest

from maintenance_agent.domain import Catalog


@pytest.fixture
def catalog():
    return Catalog()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("EQ-010 包装机", "EQ-010"),
        ("eq-010（自动包装机）", "EQ-010"),
        ("设备编号为 EQ-001，名称是1号车床，位置在A车间1号工位", "EQ-001"),
        ("EQ-001 1号车床 A车间1号工位", "EQ-001"),
        ("EQ-008 排风机 B车间通风区", "EQ-008"),
        ("A车间1号工位", "EQ-001"),
        ("数控车床 A车间2号工位", "EQ-002"),
        ("1号车床 A车间1号工位", "EQ-001"),
        ("EQ-001 车床", "EQ-001"),
        ("EQ-001 / EQ-001", "EQ-001"),
    ],
)
def test_matching_identity_combinations_resolve_one_ledger_asset(catalog, query, expected):
    result = catalog.lookup_asset(query)
    assert result["status"] == "found"
    assert result["count"] == 1
    assert result["matches"][0] == catalog.asset(expected)
    assert not result["needs_clarification"]


@pytest.mark.parametrize(
    "query", ["EQ-999 包装机", "EQ-0100 包装机", "EQ-010A 包装机", "EQ010 包装机", "ZZ-010 包装机"]
)
def test_explicit_unknown_id_never_falls_back_to_a_matching_name(catalog, query):
    result = catalog.lookup_asset(query)
    assert result["status"] == "not_found"
    assert result["reason"] == "unknown_asset_id"
    assert result["matches"] == []
    assert result["candidates"] == []
    assert result["count"] == 0


@pytest.mark.parametrize(
    "query",
    ["EQ-001 输送机", "EQ-001 2号车床", "EQ-001 A车间2号工位", "EQ-001 数控车床 A车间2号工位"],
)
def test_conflicting_known_name_or_location_requires_clarification(catalog, query):
    result = catalog.lookup_asset(query)
    assert result["status"] == "conflict"
    assert result["reason"] == "id_description_conflict"
    assert result["matches"] == []
    assert result["needs_clarification"]
    assert "EQ-001" in {a["asset_id"] for a in result["candidates"]}


def test_unknown_additional_name_is_not_silently_accepted(catalog):
    result = catalog.lookup_asset("EQ-001 激光切割机")
    assert result["status"] == "conflict"
    assert result["reason"] == "unrecognized_identity_description"
    assert not result["matches"]


@pytest.mark.parametrize("query", ["EQ-001 EQ-002", "EQ-001 EQ-999"])
def test_multiple_different_ids_require_clarification_even_if_only_one_is_known(catalog, query):
    result = catalog.lookup_asset(query)
    assert result["status"] == "ambiguous"
    assert result["reason"] == "multiple_asset_ids"
    assert result["needs_clarification"]
    assert len(result["requested_ids"]) == 2
    assert not (result["count"] == 1)


def test_shared_alias_stays_ambiguous_and_names_cannot_override_location(catalog):
    ambiguous = catalog.lookup_asset("数控车床")
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["count"] == 2
    assert {a["asset_id"] for a in ambiguous["matches"]} == {"EQ-001", "EQ-002"}
    conflict = catalog.lookup_asset("输送机 A车间1号工位")
    assert conflict["status"] == "conflict"
    assert not conflict["matches"]


def test_existing_device_without_applicable_rule_is_not_an_unknown_device(catalog):
    result = catalog.lookup_asset("EQ-010 包装机")
    assert result["status"] == "found"
    rules = catalog.lookup_rules(result["matches"][0]["asset_id"], "other", False)
    assert rules["count"] == 0
    assert rules["rules"] == []
