"""Offline tests for the OUTPUT_CONTRACT envelope converter and cache fetcher."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.cachefetch import CachingFetcher
from norway_company_agent.contract import build_envelope
from norway_company_agent.evidence import evidence
from norway_company_agent.http import FetchResult


def sample_profile() -> dict:
    return {
        "organisation_number": "810034882",
        "name": "SANDNES ELEKTRISKE AS",
        "legal_form": "AS",
        "employees": None,
        "bankrupt": False,
        "liquidating": False,
        "municipality": "SANDNES",
        "municipality_number": "1108",
        "industry_code": "43.210",
        "industry_label": "Elektrisk installasjonsarbeid",
        "website": "",
        "latest_submitted_accounts": "2025",
        "evidence": {
            "registry": evidence("registry", "available", "official_registry_bulk", "https://bulk", value={"navn": "SANDNES ELEKTRISKE AS"}, content_sha256="a" * 64),
            "registry_live": evidence("registry_live", "available", "official_registry_live", "https://entity", value={
                "organisation_number": "810034882", "name": "SANDNES ELEKTRISKE AS", "legal_form": "AS",
                "employees": None, "bankrupt": False, "liquidating": False, "website": None,
                "industry": {"kode": "43.210", "beskrivelse": "Elektrisk installasjonsarbeid"},
                "business_address": {"kommune": "SANDNES"}, "postal_address": None,
                "latest_submitted_accounts": "2025",
            }, content_sha256="b" * 64),
            "financials": evidence("financials", "available", "official_annual_accounts", "https://acc", value={"records": [{
                "period": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}, "currency": "NOK",
                "revenue": 1000.0, "operating_result": 10.0, "profit_before_tax": 9.0,
                "annual_result": 7.0, "assets": 500.0, "equity": 100.0, "debt": 400.0,
            }]}, content_sha256="c" * 64),
            "financial_history": evidence("financial_history", "not_found", "official_annual_account_copies", "https://years", note="HTTP 404"),
            "roles": evidence("roles", "available", "official_roles", "https://roles", value={"roles": [
                {"name": "Ola Nordmann", "role": "Styrets leder", "group": "Styre", "inactive": False},
            ]}, content_sha256="d" * 64),
            "locations": evidence("locations", "available", "official_subunits", "https://sub", value={"locations": []}, content_sha256="e" * 64),
            "group": evidence("group", "not_found", "official_group_structure", "https://group", note="HTTP 404"),
            "website": evidence("website", "not_found", "registry_linked_company_website", "https://entity", note="No valid registry website URL"),
            "accounting_obligation": evidence("accounting_obligation", "available", "official_rule_interpretation", "https://rules", value={"classification": "filing_observed"}),
        },
    }


def test_envelope_shape_and_missing_data_labels() -> None:
    envelope = build_envelope(sample_profile(), run_id="t1", started_at="2026-11-13T00:00:00Z",
                              completed_at="2026-11-13T00:00:05Z", runtime_ms=5000, requests=9)
    assert envelope["organisation_number"] == "810034882"
    assert envelope["run"]["terminal_status"] == "completed"
    assert envelope["operations"] == {"requests": 9, "runtime_ms": 5000, "third_party_cost_usd": 0.0}
    by_field = {}
    for claim in envelope["claims"]:
        by_field.setdefault(claim["field"], []).append(claim)
    # Employee count is null in the registry: must be labelled, never zero.
    employees = by_field["registry_employee_count"][0]
    assert employees["value"] is None and employees["availability"] == "not_available"
    # Accounts record keeps real figures and period.
    accounts = by_field["annual_accounts"][0]
    assert accounts["availability"] == "available" and accounts["value"]["revenue"] == 1000.0
    assert accounts["reporting_period"] == "2024-12-31"
    # 404 history is not_available, group 404 likewise; jobs honestly not_available.
    assert by_field["accounts_history"][0]["availability"] == "not_available"
    assert by_field["group_structure"][0]["availability"] == "not_available"
    assert by_field["job_postings"][0]["availability"] == "not_available"
    # Zero subunits returned is a labelled not_available, not an empty success.
    assert by_field["registered_workplace"][0]["availability"] == "not_available"
    # Every available claim points at evidence that exists.
    evidence_ids = {item["id"] for item in envelope["evidence"]}
    for claim in envelope["claims"]:
        for ev_id in claim["evidence_ids"]:
            assert ev_id in evidence_ids
    assert "SANDNES ELEKTRISKE AS" in envelope["summary"]


def test_refresh_diff_and_terminal_error_state() -> None:
    previous = sample_profile()
    current = sample_profile()
    current["employees"] = 12
    envelope = build_envelope(current, run_id="t2", started_at="2026-11-13T00:00:00Z",
                              completed_at="2026-11-13T00:00:05Z", runtime_ms=100, requests=3,
                              previous_profile=previous)
    fields = {change["field"] for change in envelope["changes"]}
    assert "registry.employees" in fields
    assert envelope["refresh"]["material_changes"] == len(envelope["changes"]) >= 1
    # Idempotent: same profile again yields no changes.
    again = build_envelope(current, run_id="t3", started_at="2026-11-13T00:00:00Z",
                           completed_at="2026-11-13T00:00:05Z", runtime_ms=100, requests=3,
                           previous_profile=current)
    assert again["changes"] == []


def test_caching_fetcher_roundtrip(tmp_path) -> None:
    calls = {"count": 0}

    def fake_fetch(url: str) -> FetchResult:
        calls["count"] += 1
        return FetchResult(url, 200, 5, 10, body={"ok": True}, content_sha256="f" * 64, retrieved_at="2026-11-13T00:00:00Z")

    fetcher = CachingFetcher(tmp_path, use_cache=True)
    fetcher._fetch_network = fake_fetch  # type: ignore[method-assign]
    first = fetcher("https://example.no/data")
    second = fetcher("https://example.no/data")
    assert first.body == second.body == {"ok": True}
    assert calls["count"] == 1 and fetcher.stats["cache_hits"] == 1
    persisted = list(tmp_path.glob("*.json"))
    assert len(persisted) == 1
    assert json.loads(persisted[0].read_text(encoding="utf-8"))["status"] == 200
