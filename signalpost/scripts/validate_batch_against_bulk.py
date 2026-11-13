#!/usr/bin/env python3
"""Ensure every smoke-batch organisation exists in the current bulk snapshot.

The public universe is a frozen snapshot; entities deleted since then are
absent from a fresh bulk download and ``profiles_from_bulk`` would abort the
whole run. This validator streams the bulk file once, and swaps any missing
selection for a same-stratum alternate that is present.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.sampling import iter_bulk  # noqa: E402


def read_jsonl(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bulk", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--alternates", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    batch = read_jsonl(args.batch)
    alternates = read_jsonl(args.alternates)
    wanted = {row["organisation_number"] for row in batch} | {row["organisation_number"] for row in alternates}
    found: set[str] = set()
    scanned = 0
    for record in iter_bulk(args.bulk):
        scanned += 1
        org = record["organisation_number"]
        if org in wanted:
            found.add(org)
            if len(found) == len(wanted):
                break

    alternates_by_stratum: dict[str, list[dict]] = {}
    for row in alternates:
        if row["organisation_number"] in found:
            alternates_by_stratum.setdefault(row.get("smoke_stratum", "rest"), []).append(row)

    final: list[dict] = []
    replaced: list[dict] = []
    for row in batch:
        org = row["organisation_number"]
        if org in found:
            final.append(row)
            continue
        pool = alternates_by_stratum.get(row.get("smoke_stratum", "rest"), [])
        if not pool:
            raise SystemExit(f"No present alternate for missing organisation {org}")
        substitute = pool.pop(0)
        replaced.append({"removed": org, "stratum": row.get("smoke_stratum"), "substitute": substitute["organisation_number"]})
        final.append(substitute)

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in final:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    report = {
        "bulk_rows_scanned": scanned,
        "requested": len(batch),
        "present": sum(1 for row in batch if row["organisation_number"] in found),
        "replaced": replaced,
        "final_count": len(final),
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
