# Signalpost Company Research — submission notes

## Entry

- **Challenge:** Builderr — Signalpost Company Research, Round 1 (`signalpost-company-research-round-1`)
- **Agent name (suggested):** `signalpost-norway-agent`
- **Repository:** the `agent/` tree in this folder is the complete, runnable source (starter kit + additions listed below). Copy it to the submission repository as-is.
- **Exact commit hash:** fill in after committing `agent/` (submission field `exact_commit_hash`).
- **Contact for results:** fill in at submission.
- **Smoke test report (100 companies):** `smoke_report.md` in this folder; raw outputs in `smoke-output/` (submission field `smoke_test_report_100`).

## Run command

```bash
# Prereqs: Python 3.12+, uv
cd agent
uv sync

# 1) Fresh bulk registry snapshot (identity anchor, ~155 MB)
curl -o brreg-enheter.csv.gz https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv

# 2) Official-style batch run (writes contract envelopes + profiles + report)
SIGNALPOST_KOPI_SPACING_SECONDS=8.4 uv run python scripts/run_contract_batch.py \
  --organisations organisations.txt \        # one 9-digit org number per line
  --bulk brreg-enheter.csv.gz \
  --output out/envelopes.jsonl \
  --profiles-output out/profiles.jsonl \
  --report out/report.json \
  --expected-count 100 --workers 4

# Refresh / change detection against a previous run (live re-reads, no cache):
#   add:  --previous out/profiles.jsonl --no-cache --profiles-output out/profiles2.jsonl

# Interactive routes (stdlib HTTP server):
uv run python scripts/serve.py --store out/server-store --port 8765
#   GET  /companies/{organisation_number}
#   GET  /research?org={orgnr}&q={question}        (or POST /research)
#   POST /refresh {"organisation_number": "..."}   (appends snapshots, never overwrites)

# Tests:  uv run --with pytest pytest -q     → 108 passed, 5 subtests passed
```

## Models / APIs used

- **Models:** none. No LLMs anywhere in the pipeline — extraction, normalisation and synthesis are deterministic, so every sentence in a summary traces to an evidence-backed claim.
- **APIs (all free, no key):** Brønnøysundregistrene open APIs — Enhetsregisteret (bulk CSV snapshot + live entity, subunits, roles and group-structure endpoints) and Regnskapsregisteret (normalised annual accounts + filing-history/copy endpoints). Company websites are fetched directly with robots.txt compliance.
- **Expected cost per official run: USD 0.**

## Architecture (8 lines)

1. Identity is anchored in the official bulk Enhetsregisteret snapshot, then cross-checked against the live entity record — disagreements quarantine the website instead of publishing wrong-company facts.
2. A caching fetcher (disk cache, retries, global rate spacing; the filing-copy family is held to ~30 request starts/minute in aggregate) feeds the kit's official-module collectors: registry, accounts, filing history, roles, group structure, workplaces, accounting obligation.
3. The registry-listed website is crawled only after SSRF guards and an identity gate pass; social profiles are accepted only when linked from the gated site.
4. Profiles are converted claim-by-claim into the OUTPUT_CONTRACT envelope (`run`/`sections`/`claims[]`/`evidence[]`/`changes[]`/`errors[]`/`operations{}`), with missing values labelled `not_available`/`blocked`/`failed` — never zero-filled.
5. Summaries are synthesised deterministically from available claims only.
6. Refresh re-reads live sources and diffs tracked fields against the previous snapshot; snapshots are append-only, so history is never overwritten.
7. `/research` answers strictly from the stored profile plus same-company official fallbacks and names every unsupported part of a question.
8. The batch runner checkpoints profiles atomically, supports resume, and validates the finished batch (exact count, uniqueness, terminal states, claims+evidence presence, no missing-converted-to-zero).

## Coverage headline (live 100-company smoke run)

100/100 companies completed with full identity, industry, address, roles, workplaces, accounts history and obligation assessment; 99/100 with normalised annual accounts (282 records); employee counts available for 29 and explicitly labelled missing for 71; group structure for 9 (91 genuinely none); identity-gated websites for 12 with 15 more quarantined as ambiguous rather than risked. Mean 34.6 s/company at 4 workers (incl. rate-limit politeness spacing), $0 external cost. Full report: `smoke_report.md`.

## What was added on top of the starter kit

- `src/norway_company_agent/cachefetch.py` — caching/retrying/rate-spaced fetcher (also fixes dropped-connection handling: `RemoteDisconnected` now retries instead of crashing).
- `src/norway_company_agent/contract.py` — OUTPUT_CONTRACT envelope converter + deterministic synthesis.
- `src/norway_company_agent/live_profile.py` — single-company live profile builder for the HTTP routes.
- `scripts/run_contract_batch.py` — batch runner emitting contract envelopes with validation + refresh diffing (the kit runner's native output predates the minimal envelope shape in OUTPUT_CONTRACT.md).
- `scripts/serve.py` — `/companies`, `/research`, `/refresh` over stdlib HTTP with append-only snapshot store.
- `scripts/select_smoke_batch.py`, `scripts/validate_batch_against_bulk.py` — stratified smoke-batch selection + presence validation against a fresh bulk snapshot.
- `tests/test_contract.py` — offline tests for the converter and cache fetcher.
- One minimal robustness fix in the kit's `website.py` SSRF guard: the RFC 2544 benchmarking range (198.18.0.0/15) is treated as routable because some sandboxed/proxied networks map public DNS into it (fake-IP DNS); it is unroutable on the public Internet and never hosts internal services, while loopback, link-local, multicast and all RFC 1918 space remain blocked.

## Known limitations (stated honestly)

- **Hiring/activity:** no permitted job-posting or news connector is configured, so `job_postings` and `dated_public_activity` are always explicit `not_available` claims — never inferred, never zero.
- **Website gate strictness:** 15/100 real company sites were quarantined as `ambiguous` (redirect/domain mismatches). This is deliberate precision-over-recall: the harness treats wrong-company publication as disqualifying.
- **Registry outages surface, not hide:** a brreg HTTP 500 produced one `failed` accounts claim and a `completed_with_errors` terminal state (org 970896856).
