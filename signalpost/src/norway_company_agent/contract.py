"""Convert a kit profile into the OUTPUT_CONTRACT.md terminal envelope.

The kit's native batch envelope nests the whole profile. The published minimal
output contract instead wants, per organisation number: a ``run`` block,
claim-level ``claims`` (field, value, availability, confidence, evidence_ids),
an ``evidence`` list (id, source_url, source_class, retrieved_at,
content_sha256, claim_span), ``changes``, ``errors`` and ``operations``.
This module derives that envelope deterministically from the same profile so
both shapes describe exactly the same underlying evidence.

Rules honoured here:

- Missing values are emitted as claims with ``value: null`` and an explicit
  availability state (``not_available`` / ``blocked`` / ``failed`` /
  ``ambiguous``). They are never converted to zero.
- Website-derived claims are only published when the deterministic identity
  gate marks the site an exact match; otherwise they are ``ambiguous``
  (quarantined), mirroring ``identity.apply_website_identity_gate``.
- Hiring / dated-activity families have no permitted live connector in this
  build, so they are declared ``not_available`` with an explanatory note
  instead of being silently omitted or fabricated.
- The synthesis paragraph only restates claim values; unknowns are listed
  explicitly in ``unknowns``.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .refresh import diff_profile

STATE_MAP = {
    "available": "available",
    "not_found": "not_available",
    "blocked": "blocked",
    "source_error": "failed",
    "not_fetched": "failed",
    "not_applicable": "not_applicable",
}

MAX_LIST_CLAIMS = 25


def map_state(status: str | None) -> str:
    return STATE_MAP.get(str(status or ""), "failed")


def _record(profile: dict[str, Any], module: str) -> dict[str, Any]:
    return profile.get("evidence", {}).get(module, {}) or {}


def _span(value: Any, limit: int = 240) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text[:limit]


def _money(value: Any) -> str:
    if value is None:
        return "not available"
    try:
        return f"NOK {float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


class EnvelopeBuilder:
    def __init__(self, profile: dict[str, Any]) -> None:
        self.profile = profile
        self.claims: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self._evidence_ids: dict[str, str] = {}
        self.errors: list[dict[str, Any]] = []

    # -- evidence ------------------------------------------------------
    def evidence_id(self, module: str) -> str | None:
        if module in self._evidence_ids:
            return self._evidence_ids[module]
        record = _record(self.profile, module)
        if not record:
            return None
        ev_id = f"ev-{module.replace('_', '-')}"
        self._evidence_ids[module] = ev_id
        span_source = record.get("value")
        self.evidence.append({
            "id": ev_id,
            "source_url": record.get("source_url"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "retrieved_at": record.get("retrieved_at"),
            "content_sha256": record.get("content_sha256"),
            "claim_span": _span(span_source) if span_source is not None else (record.get("note") or "")[:240],
        })
        if record.get("status") == "source_error":
            self.errors.append({
                "module": module,
                "source_url": record.get("source_url"),
                "error": record.get("note") or "source error",
            })
        return ev_id

    # -- claims ----------------------------------------------------------
    def claim(
        self,
        field: str,
        value: Any,
        availability: str,
        module: str | None,
        *,
        confidence: float = 0.95,
        note: str | None = None,
        period: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "field": field,
            "value": value,
            "availability": availability,
            "confidence": confidence,
            "evidence_ids": [self.evidence_id(module)] if module else [],
        }
        if note:
            entry["note"] = note
        if period:
            entry["reporting_period"] = period
        self.claims.append(entry)

    def module_claim(self, field: str, module: str, *, value: Any = None, confidence: float = 0.95, note: str | None = None) -> str:
        """Emit a claim whose availability mirrors a module record's status."""
        record = _record(self.profile, module)
        availability = map_state(record.get("status"))
        if availability == "available" and value is None:
            value = record.get("value")
        self.claim(field, value, availability, module, confidence=confidence, note=note or record.get("note"))
        return availability


def _period_label(record: dict[str, Any]) -> str | None:
    period = record.get("period") or {}
    if isinstance(period, dict):
        return period.get("tilDato") or period.get("fraDato")
    return str(period) if period else None


def _group_summary(body: Any, org: str) -> dict[str, Any]:
    """Summarise the konsernstruktur tree without embedding it wholesale."""
    if not isinstance(body, dict):
        return {"group_structure_returned": body is not None}
    total = 0
    match: dict[str, Any] | None = None

    def walk(node: Any) -> None:
        nonlocal total, match
        if isinstance(node, dict):
            if node.get("organisasjonsnummer"):
                total += 1
                if str(node.get("organisasjonsnummer")) == org:
                    match = node
            for child in node.get("children", []) or []:
                walk(child)

    walk(body)
    summary: dict[str, Any] = {"entities_in_group_tree": total}
    node = match or body
    children = node.get("children") or []
    summary["direct_subsidiaries"] = [
        {"name": child.get("navn"), "organisation_number": child.get("organisasjonsnummer")}
        for child in children[:MAX_LIST_CLAIMS]
        if isinstance(child, dict)
    ]
    parent_org = node.get("parentOrganisasjonsnummer")
    if parent_org and str(parent_org) != org:
        summary["parent"] = {"name": node.get("parentNavn"), "organisation_number": parent_org}
    return summary


def build_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
    runtime_ms: int,
    requests: int,
    previous_profile: dict[str, Any] | None = None,
    third_party_cost_usd: float = 0.0,
) -> dict[str, Any]:
    org = profile["organisation_number"]
    b = EnvelopeBuilder(profile)
    live = _record(profile, "registry_live")
    live_value = live.get("value") or {}
    bulk = _record(profile, "registry")

    def identity_value(key: str) -> Any:
        if live.get("status") == "available" and live_value.get(key) not in (None, ""):
            return live_value[key]
        return profile.get(key)

    identity_module = "registry_live" if live.get("status") == "available" else "registry"
    identity_state = map_state(live.get("status")) if live else map_state(bulk.get("status"))

    # 1 — Legal identity and public brand --------------------------------
    b.claim("organisation_number", org, "available", identity_module, confidence=0.99)
    name = identity_value("name")
    b.claim("legal_name", name, "available" if name else identity_state, identity_module, confidence=0.99)
    legal_form = identity_value("legal_form")
    b.claim("legal_form", legal_form, "available" if legal_form else identity_state, identity_module, confidence=0.99)
    bankrupt = identity_value("bankrupt")
    liquidating = identity_value("liquidating")
    b.claim(
        "registration_status",
        {"bankrupt": bankrupt, "under_liquidation": liquidating},
        "available" if bankrupt is not None else identity_state,
        identity_module,
        confidence=0.97,
    )
    address = live_value.get("business_address") if live.get("status") == "available" else None
    if not address:
        address = {
            "municipality": profile.get("municipality"),
            "municipality_number": profile.get("municipality_number"),
        } if profile.get("municipality") else None
    b.claim("business_address", address, "available" if address else identity_state, identity_module, confidence=0.97)
    postal = live_value.get("postal_address") if live.get("status") == "available" else None
    b.claim("postal_address", postal, "available" if postal else "not_available", "registry_live" if live else None,
            confidence=0.97, note=None if postal else "No separate postal address returned by the live registry record.")
    industry_live = live_value.get("industry") or {}
    industry = {
        "code": industry_live.get("kode") or profile.get("industry_code"),
        "label": industry_live.get("beskrivelse") or profile.get("industry_label"),
    }
    b.claim("industry", industry, "available" if industry["code"] else identity_state, identity_module, confidence=0.97)
    employees = identity_value("employees")
    b.claim(
        "registry_employee_count",
        employees,
        "available" if employees is not None else "not_available",
        identity_module,
        confidence=0.97,
        note=None if employees is not None else "The registry row carries no employee count; reported as not available, never as zero.",
    )
    latest_year = identity_value("latest_submitted_accounts")
    b.claim("latest_submitted_accounts_year", latest_year, "available" if latest_year else "not_available",
            identity_module, confidence=0.97)

    # 2 — Annual accounts and history ------------------------------------
    financials = _record(profile, "financials")
    fin_state = map_state(financials.get("status"))
    records = ((financials.get("value") or {}).get("records") or []) if financials.get("status") == "available" else []
    records = sorted(records, key=lambda item: str((item.get("period") or {}).get("tilDato") or ""), reverse=True)
    if records:
        for record in records:
            b.claim("annual_accounts", record, "available", "financials", confidence=0.98, period=_period_label(record))
    elif fin_state == "available":
        b.claim("annual_accounts", None, "not_available", "financials", confidence=0.95,
                note="The accounts endpoint returned no normalized records; missing figures are not interpreted as zero.")
    else:
        b.claim("annual_accounts", None, fin_state, "financials", confidence=0.9,
                note=financials.get("note") or "No annual-account record was returned.")
    history = _record(profile, "financial_history")
    hist_state = map_state(history.get("status"))
    hist_value = history.get("value") or {}
    if hist_state == "available" and hist_value.get("years"):
        b.claim("accounts_history", {"years": hist_value["years"], "filing_copy_urls": [item["url"] for item in hist_value.get("pdfs", [])]},
                "available", "financial_history", confidence=0.97)
    elif hist_state == "available":
        b.claim("accounts_history", None, "not_available", "financial_history", confidence=0.95,
                note="The filing-copy index returned no years for this organisation.")
    else:
        b.claim("accounts_history", None, hist_state, "financial_history", confidence=0.9, note=history.get("note"))
    b.module_claim("accounting_obligation", "accounting_obligation", confidence=0.8)

    # 3 — Leadership and registered workplaces ---------------------------
    roles = _record(profile, "roles")
    roles_state = map_state(roles.get("status"))
    role_items = ((roles.get("value") or {}).get("roles") or []) if roles.get("status") == "available" else []
    active_roles = [item for item in role_items if not item.get("inactive")]
    if active_roles:
        for item in active_roles[:MAX_LIST_CLAIMS]:
            b.claim("leadership_role", item, "available", "roles", confidence=0.97)
    elif roles_state == "available":
        b.claim("leadership_role", None, "not_available", "roles", confidence=0.95,
                note="The registry returned no active public role records for this organisation.")
    else:
        b.claim("leadership_role", None, roles_state, "roles", confidence=0.9, note=roles.get("note"))

    locations = _record(profile, "locations")
    loc_state = map_state(locations.get("status"))
    loc_items = ((locations.get("value") or {}).get("locations") or []) if locations.get("status") == "available" else []
    if loc_items:
        for item in loc_items[:MAX_LIST_CLAIMS]:
            b.claim("registered_workplace", item, "available", "locations", confidence=0.97)
    elif loc_state == "available":
        b.claim("registered_workplace", None, "not_available", "locations", confidence=0.95,
                note="The registry returned zero subunits; this does not prove the company has no physical presence.")
    else:
        b.claim("registered_workplace", None, loc_state, "locations", confidence=0.9, note=locations.get("note"))

    group = _record(profile, "group")
    group_state = map_state(group.get("status"))
    if group_state == "available":
        b.claim("group_structure", _group_summary(group.get("value"), org), "available", "group", confidence=0.95)
    else:
        b.claim("group_structure", None, group_state, "group", confidence=0.9, note=group.get("note"))

    # 4 — Verified website and company-owned profiles ----------------------
    registry_site = identity_value("website")
    b.claim("registry_listed_website", registry_site, "available" if registry_site else "not_available",
            identity_module, confidence=0.9,
            note=None if registry_site else "The registry row lists no website.")
    website = _record(profile, "website")
    web_state = map_state(website.get("status"))
    web_value = website.get("value") or {}
    assessment = web_value.get("identity_assessment") or {}
    publishable = bool(assessment.get("publishable"))
    if web_state == "available" and publishable:
        b.claim("official_website", web_value.get("final_url"), "available", "website", confidence=0.9,
                note=f"Identity gate: {assessment.get('status')} (score {assessment.get('score')}).")
        if web_value.get("description"):
            b.claim("website_description", web_value["description"], "available", "website", confidence=0.75,
                    note="Company-reported claim from its own website; not an official registry fact.")
        for link in web_value.get("social_links") or []:
            b.claim(f"company_profile_{link['platform']}", link["url"], "available", "website", confidence=0.85)
        if not web_value.get("social_links"):
            b.claim("company_profiles", None, "not_available", "website", confidence=0.9,
                    note="No company-owned social profile passed the identity gate on the verified site.")
    elif web_state == "available":
        b.claim("official_website", web_value.get("final_url"), "ambiguous", "website", confidence=0.4,
                note="A registry-linked site was fetched but exact legal-entity identity was not established; its claims are quarantined.")
    else:
        b.claim("official_website", None, web_state, "website", confidence=0.9, note=website.get("note"))
    for crawl_error in web_value.get("crawl_errors") or []:
        b.errors.append({"module": "website_page", "source_url": crawl_error.get("url"), "error": crawl_error.get("error")})

    # 5 — Hiring and dated public activity --------------------------------
    b.claim("job_postings", None, "not_available", None, confidence=0.9,
            note="No permitted jobs connector is configured in this run; checked state is 'not checked', not zero postings.")
    b.claim("dated_public_activity", None, "not_available", None, confidence=0.9,
            note="No permitted news/activity connector is configured in this run.")

    # 7 — Refresh metadata and changes --------------------------------------
    changes: list[dict[str, Any]] = []
    if previous_profile is not None:
        changes = diff_profile(previous_profile, profile)
    refresh_meta = {
        "previous_snapshot_supplied": previous_profile is not None,
        "material_changes": len(changes),
        "snapshot_hashes": {
            module: record.get("content_sha256")
            for module, record in sorted(profile.get("evidence", {}).items())
            if record.get("content_sha256")
        },
    }

    sections = {
        "legal_identity": "available" if name else identity_state,
        "annual_accounts": "available" if records else fin_state,
        "leadership_and_workplaces": "available" if (active_roles or loc_items) else roles_state,
        "website_and_profiles": "available" if (web_state == "available" and publishable) else web_state,
        "hiring_and_activity": "not_available",
        "evidence_and_availability": "available",
        "refresh": "available",
    }

    unknowns = [
        claim["field"] for claim in b.claims if claim["availability"] in {"not_available", "ambiguous", "blocked", "failed"}
    ]
    summary = _synthesise(profile, b.claims, records, active_roles, loc_items, changes, previous_profile is not None)
    failed = any(claim["availability"] == "failed" for claim in b.claims)
    return {
        "organisation_number": org,
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": "completed_with_errors" if (failed or b.errors) else "completed",
        },
        "sections": sections,
        "claims": b.claims,
        "evidence": b.evidence,
        "changes": changes,
        "errors": b.errors,
        "summary": summary,
        "unknowns": unknowns,
        "refresh": refresh_meta,
        "operations": {
            "requests": requests,
            "runtime_ms": runtime_ms,
            "third_party_cost_usd": third_party_cost_usd,
        },
    }


def _synthesise(
    profile: dict[str, Any],
    claims: list[dict[str, Any]],
    records: list[dict[str, Any]],
    active_roles: list[dict[str, Any]],
    locations: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    had_previous: bool,
) -> str:
    def first(field: str) -> Any:
        for claim in claims:
            if claim["field"] == field and claim["availability"] == "available":
                return claim["value"]
        return None

    name = first("legal_name") or profile.get("name") or "Unknown company"
    form = first("legal_form") or ""
    industry = first("industry") or {}
    municipality = profile.get("municipality") or "an unrecorded municipality"
    parts = [f"{name} ({form}) is a Norwegian-registered entity based in {municipality}"]
    if industry.get("label"):
        parts.append(f"operating in {industry['label']} (NACE {industry.get('code')})")
    employees = first("registry_employee_count")
    parts.append(f"with {employees} registered employees" if employees is not None else "with no registry employee count available")
    text = ", ".join(parts) + "."
    if records:
        latest = records[0]
        text += (
            f" Latest normalized annual accounts cover the period ending {_period_label(latest)}: "
            f"revenue {_money(latest.get('revenue'))}, annual result {_money(latest.get('annual_result'))}, "
            f"assets {_money(latest.get('assets'))}, debt {_money(latest.get('debt'))}."
        )
    else:
        text += " No normalized annual-account figures were returned; they are reported as not available, not zero."
    if active_roles:
        lead = active_roles[0]
        text += f" {len(active_roles)} active registered role records, including {lead.get('name')} ({lead.get('role') or lead.get('group')})."
    else:
        text += " No active public role records were returned."
    if locations:
        text += f" {len(locations)} registered subunit(s) are on record."
    site = first("official_website")
    text += f" Verified official website: {site}." if site else " No identity-verified official website was established."
    if had_previous:
        text += f" Refresh detected {len(changes)} material change(s) versus the previous snapshot."
    return text


def iter_envelope_fields(envelope: dict[str, Any]) -> Iterable[str]:
    return [claim["field"] for claim in envelope.get("claims", [])]
