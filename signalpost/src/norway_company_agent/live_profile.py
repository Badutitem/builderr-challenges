"""Build one kit-shaped profile for a single organisation from the live APIs.

The batch runner anchors identity in the frozen bulk snapshot. The HTTP layer
(``scripts/serve.py``) cannot scan a multi-GB snapshot per request, so it
anchors identity in the live Enhetsregisteret record instead and labels that
evidence ``official_registry_live``. The resulting profile dict has the same
shape the contract converter, research layer and refresh diff consume.
"""

from __future__ import annotations

import time
from typing import Any

from .evidence import evidence
from .identity import apply_website_identity_gate
from .official import BRREG_ENTITY, accounting_obligation_assessment, fetch_official_modules
from .website import fetch_website

LIVE_MODULES = {"registry_live", "financials", "financial_history", "roles", "group", "locations"}


def build_profile_live(org: str, fetcher) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (profile, metrics) for one organisation number using live reads."""
    started = time.monotonic()
    entity_result = fetcher(BRREG_ENTITY.format(org=org))
    body = entity_result.body if entity_result.status == 200 and isinstance(entity_result.body, dict) else {}
    address = body.get("forretningsadresse") or {}
    industry = body.get("naeringskode1") or {}
    profile: dict[str, Any] = {
        "organisation_number": org,
        "name": body.get("navn"),
        "legal_form": (body.get("organisasjonsform") or {}).get("kode"),
        "employees": body.get("antallAnsatte"),
        "bankrupt": body.get("konkurs"),
        "liquidating": body.get("underAvvikling"),
        "municipality": address.get("kommune"),
        "municipality_number": address.get("kommunenummer"),
        "industry_code": industry.get("kode"),
        "industry_label": industry.get("beskrivelse"),
        "website": body.get("hjemmeside") or "",
        "latest_submitted_accounts": body.get("sisteInnsendteAarsregnskap"),
        "evidence": {},
    }
    if entity_result.status == 200:
        registry_record = evidence(
            "registry", "available", "official_registry_live", entity_result.url,
            value=body, content_sha256=entity_result.content_sha256,
            retrieved_at=entity_result.retrieved_at, source_row_key=org,
        )
    elif entity_result.status in {404, 410}:
        registry_record = evidence(
            "registry", "not_found", "official_registry_live", entity_result.url,
            note=f"HTTP {entity_result.status}", content_sha256=entity_result.content_sha256,
            retrieved_at=entity_result.retrieved_at, source_row_key=org,
        )
    else:
        registry_record = evidence(
            "registry", "source_error", "official_registry_live", entity_result.url,
            note=entity_result.error, content_sha256=entity_result.content_sha256,
            retrieved_at=entity_result.retrieved_at, source_row_key=org,
        )
    profile["evidence"]["registry"] = registry_record
    if entity_result.status == 200:
        profile["evidence"]["accounting_obligation"] = accounting_obligation_assessment(profile)

    records, metrics = fetch_official_modules(org, LIVE_MODULES, fetcher=fetcher)
    profile["evidence"].update(records)
    website_record, website_metrics = fetch_website(profile.get("website"))
    profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
    metric = {
        "requests": 1 + len(metrics) + website_metrics["requests"],
        "bytes": entity_result.bytes_received + sum(item.bytes_received for item in metrics) + website_metrics["bytes"],
        "runtime_ms": int((time.monotonic() - started) * 1000),
    }
    profile["run_metrics"] = metric
    return profile, metric
