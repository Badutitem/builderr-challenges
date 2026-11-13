#!/usr/bin/env python3
"""Deterministic stratified 100-company smoke batch from the public universe.

The plain kit selector samples uniformly, which in this universe means ~92%
AS companies, ~89% without a website and ~85% without an employee count. For a
smoke test that must exercise every pipeline path we instead draw disjoint
strata (first matching predicate wins, reservoir sampling with a fixed seed):

  big    employees >= 100 .................... 10
  web    registry website present ............ 30
  nonas  legal form other than AS ............ 20
  rest   everything else ..................... 40

Extra per-stratum alternates are written alongside so any organisation that
has disappeared from the current bulk registry snapshot can be replaced from
the same stratum (see validate_batch_against_bulk.py).
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
from pathlib import Path
from typing import Any

QUOTAS = {"big": 10, "web": 30, "nonas": 20, "rest": 40}
ALTERNATES = 6


def stratum_of(row: dict[str, Any]) -> str:
    employees = row.get("employees")
    if isinstance(employees, int) and employees >= 100:
        return "big"
    if str(row.get("website") or "").strip():
        return "web"
    if str(row.get("legal_form") or "") != "AS":
        return "nonas"
    return "rest"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--alternates-output", required=True)
    parser.add_argument("--meta-output", required=True)
    parser.add_argument("--seed", type=int, default=20261113)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    reservoirs: dict[str, list[dict[str, Any]]] = {key: [] for key in QUOTAS}
    seen: dict[str, int] = {key: 0 for key in QUOTAS}
    sizes = {key: QUOTAS[key] + ALTERNATES for key in QUOTAS}
    with gzip.open(args.universe, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            key = stratum_of(row)
            seen[key] += 1
            bucket = reservoirs[key]
            if len(bucket) < sizes[key]:
                bucket.append(row)
            else:
                position = rng.randrange(seen[key])
                if position < sizes[key]:
                    bucket[position] = row

    selected: list[dict[str, Any]] = []
    alternates: list[dict[str, Any]] = []
    counts: dict[str, Any] = {}
    for key, quota in QUOTAS.items():
        bucket = reservoirs[key]
        rng.shuffle(bucket)
        chosen, spare = bucket[:quota], bucket[quota:]
        for row in chosen:
            row = {**row, "smoke_stratum": key}
            selected.append(row)
        for row in spare:
            alternates.append({**row, "smoke_stratum": key})
        counts[key] = {"universe_rows": seen[key], "selected": len(chosen), "alternates": len(spare)}
    rng.shuffle(selected)

    for target, rows in ((args.output, selected), (args.alternates_output, alternates)):
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    meta = {"seed": args.seed, "quotas": QUOTAS, "strata": counts, "selected": len(selected), "alternates": len(alternates)}
    Path(args.meta_output).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
