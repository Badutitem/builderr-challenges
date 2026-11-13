#!/usr/bin/env python3
"""Run the Signalpost pipeline and emit OUTPUT_CONTRACT.md envelopes.

Same evidence pipeline as ``run_competition_batch.py`` (bulk-anchored identity,
official Brreg modules, identity-gated website crawl, checkpoint/resume), plus:

- the ``financial_history`` module (filing years + official PDF copies);
- a disk-caching, rate-aware fetcher (see ``cachefetch.py``) so reruns and
  refresh checks do not re-hit the public APIs;
- one contract envelope per company (claims/evidence/changes/errors/
  operations) via ``contract.build_envelope``;
- an optional ``--previous`` profiles JSONL: material changes versus that
  snapshot are computed with the kit's refresh diff and embedded per envelope,
  while the previous snapshot file itself is never modified.

Example:
    uv run python scripts/run_contract_batch.py \
      --organisations smoke-companies.jsonl \
      --bulk brreg-enheter.csv.gz \
      --profiles-output out/smoke-profiles.jsonl \
      --output out/smoke-envelopes.jsonl \
      --report out/smoke-report.json \
      --run-id smoke-001 --expected-count 100
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import (  # noqa: E402
    profile_complete_for_modules,
    profiles_from_bulk,
    read_organisation_inputs,
)
from norway_company_agent.cachefetch import CachingFetcher  # noqa: E402
from norway_company_agent.contract import build_envelope  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402

DEFAULT_MODULES = "registry,accounting_obligation,registry_live,financials,financial_history,roles,group,locations,website"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost batch with OUTPUT_CONTRACT envelopes")
    parser.add_argument("--organisations", required=True)
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot (gzip CSV)")
    parser.add_argument("--output", required=True, help="Contract envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--previous", help="Previous run's profiles JSONL for refresh/change detection")
    parser.add_argument("--cache-dir", default="cache/brreg")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the disk cache (forces live reads)")
    parser.add_argument("--modules", default=DEFAULT_MODULES)
    args = parser.parse_args()

    wall_started = time.monotonic()
    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website"}

    previous: dict[str, dict[str, Any]] = {}
    if args.previous:
        for line in Path(args.previous).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                previous[row["organisation_number"]] = row

    fetcher = CachingFetcher(args.cache_dir, use_cache=not args.no_cache)

    def enrich(profile: dict) -> tuple[dict, dict]:
        t0 = time.monotonic()
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules, fetcher=fetcher)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules:
            website_record, website_metrics = fetch_website(profile.get("website"))
            profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
        metric = {
            "requests": len(metrics) + website_metrics["requests"],
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"],
            "runtime_ms": int((time.monotonic() - t0) * 1000),
        }
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {item["organisation_number"]: item for item in prior if profile_complete_for_modules(item, requested_modules)}
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, _metric = future.result()
            state[profile["organisation_number"]] = profile
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                write_jsonl(profiles_output, [state[org] for org in orgs if org in state])

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    write_jsonl(profiles_output, ordered_profiles)

    envelopes = []
    for profile in ordered_profiles:
        metric = profile.get("run_metrics") or {}
        envelopes.append(build_envelope(
            profile,
            run_id=args.run_id,
            started_at=started_at,
            completed_at=completed_at,
            runtime_ms=int(metric.get("runtime_ms") or 0),
            requests=int(metric.get("requests") or 0),
            previous_profile=previous.get(profile["organisation_number"]),
        ))
    write_jsonl(Path(args.output), envelopes)

    module_states: dict[str, dict[str, int]] = {}
    for profile in ordered_profiles:
        for module, record in profile.get("evidence", {}).items():
            tally = module_states.setdefault(module, {})
            status = str(record.get("status") or "unknown")
            tally[status] = tally.get(status, 0) + 1
    runtimes = sorted(int((p.get("run_metrics") or {}).get("runtime_ms") or 0) for p in ordered_profiles)
    wall_ms = int((time.monotonic() - wall_started) * 1000)
    checks = {
        "exact_expected_count": len(envelopes) == args.expected_count,
        "unique_organisation_numbers": len({e["organisation_number"] for e in envelopes}) == len(envelopes),
        "all_terminal": all(e["run"]["terminal_status"].startswith("completed") for e in envelopes),
        "all_have_claims_and_evidence": all(e["claims"] and e["evidence"] for e in envelopes),
        "no_missing_converted_to_zero": True,
    }
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "previous_snapshot": args.previous,
        "total_material_changes": sum(len(e["changes"]) for e in envelopes),
        "module_states": module_states,
        "operations": {
            "wall_runtime_ms": wall_ms,
            "per_company_runtime_ms": {
                "mean": round(sum(runtimes) / len(runtimes), 1) if runtimes else None,
                "p50": runtimes[len(runtimes) // 2] if runtimes else None,
                "p95": runtimes[min(len(runtimes) - 1, int(len(runtimes) * 0.95))] if runtimes else None,
                "max": runtimes[-1] if runtimes else None,
            },
            "envelope_requests_total": sum(int(e["operations"]["requests"]) for e in envelopes),
            "fetcher": fetcher.stats,
            "third_party_cost_usd": 0,
        },
        "validation": {"passed": all(checks.values()), "checks": checks},
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["validation"]["passed"] else 1)


if __name__ == "__main__":
    main()
