# Signalpost Company Research — 100-company smoke test report

- **Run ID:** `smoke-100-20261113`
- **When:** 2026-11-12T23:34:38Z → 23:50:01Z (live run against the production Brønnøysundregistrene APIs)
- **Agent:** starter-kit package `norway_company_agent` + added contract runner (`scripts/run_contract_batch.py`), caching fetcher, OUTPUT_CONTRACT envelope converter and HTTP routes (`scripts/serve.py`). Full machine-readable outputs in `smoke-output/`.
- **External cost:** **USD 0** — no paid APIs, no API keys, no LLMs. Only Brønnøysundregistrene open APIs and company websites.

## Batch selection

Deterministic stratified sample (seed 20261113) from the supplied 411,160-entity public universe (`resources/universe-2025.jsonl.gz`), disjoint strata chosen to exercise every pipeline path rather than mirror the universe's 92%-AS / 89%-no-website skew:

| Stratum | Quota | Universe rows |
| --- | ---: | ---: |
| ≥100 registered employees | 10 | 2,634 |
| Registry website present | 30 | 43,571 |
| Legal form other than AS | 20 | 22,510 |
| Remainder | 40 | 342,445 |

All 100 selected organisations (and 24 alternates) were verified present in a freshly downloaded bulk Enhetsregisteret snapshot (1,179,283 rows scanned) — **0 replacements needed** (`smoke-output/smoke-validation.json`).

## Headline results

| Metric | Value |
| --- | ---: |
| Companies requested | 100 |
| Envelopes emitted | **100 / 100** |
| Terminal `completed` | 92 |
| Terminal `completed_with_errors` | 8 (7 website fetch errors, 1 registry HTTP 500) |
| Contract validation | **PASSED** — exact count, unique org numbers, all terminal, every company has claims + evidence, no missing value converted to zero |
| Total claims emitted | 2,601 (incl. 282 annual-account records, 553 leadership roles, 122 workplace records) |
| Summaries generated | 100 / 100 (deterministic, built only from available claims) |
| Network requests | 600 (602 attempts, 2 retries, 1 hard error) |
| Wall runtime | 922.7 s (~15.4 min) with 4 workers |
| Runtime per company | mean **34.6 s**, p50 33.3 s, p95 58.1 s, max 68.0 s |
| Effective throughput | ~9.2 s/company of wall time at 4 workers |
| Third-party cost | **$0.00** |

Per-company time is dominated by (a) deliberate politeness spacing on the rate-limited filing-history endpoint (per-worker spacing raised to 8.4 s so the aggregate stays at ~30 request starts/minute, the documented limit), and (b) robots-aware website crawls for the 36 companies with a registry website.

## Module outcomes (per company)

| Module | Available | Not found | Source error | Blocked |
| --- | ---: | ---: | ---: | ---: |
| Registry identity (bulk snapshot) | 100 | 0 | 0 | 0 |
| Registry identity (live entity) | 100 | 0 | 0 | 0 |
| Annual accounts (normalised) | 99 | 0 | 1 | 0 |
| Accounts history (filing years + copy URLs) | 100 | 0 | 0 | 0 |
| Leadership roles | 100 | 0 | 0 | 0 |
| Registered workplaces (subunits) | 100 | 0 | 0 | 0 |
| Group structure | 9 | 91 | 0 | 0 |
| Accounting-obligation assessment | 100 | 0 | 0 | 0 |
| Company website | 27 | 65 | 7 | 1 |

"Not found" is a first-class, honest result: 91 companies genuinely have no konsern (group) record, and 65 have no usable registry website.

## Per-field claim coverage (companies with the field available)

| Field | Available | Explicitly not available | Failed / blocked / ambiguous |
| --- | ---: | ---: | ---: |
| Organisation number, legal name, legal form, registration status, business address, industry, latest accounts year, accounting obligation, accounts history | 100 / 100 | 0 | 0 |
| Annual accounts (≥1 normalised record) | 99 | 0 | 1 failed (HTTP 500) |
| Leadership roles | 100 | 0 | 0 |
| Registered workplaces | 85 | 15 | 0 |
| Registry employee count | 29 | **71 (labelled, never zero)** | 0 |
| Postal address | 31 | 69 | 0 |
| Group structure | 9 | 91 | 0 |
| Registry-listed website | 36 | 64 | 0 |
| Verified official website (identity-gated) | 12 | 65 | 15 ambiguous (quarantined), 7 failed, 1 blocked |
| Website description | 8 | — | — |
| Social profiles (14 claims: Facebook 5, Instagram 5, LinkedIn 3, YouTube 1) | 6 companies | 6 explicit fallback | — |
| Job postings / dated public activity | 0 | **100 (explicit — no permitted connector configured)** | 0 |

## Missing-data handling (examples)

- **Employee count:** org `936893872` — the registry carries `null`; the envelope emits `registry_employee_count` with `value: null`, `availability: "not_available"` and the note *"The registry row carries no employee count; reported as not available, never as zero."* The run validator independently re-checks that no unavailable claim carries the value `0` (`no_missing_converted_to_zero: true`).
- **Group structure:** a 404 from `konsernstruktur` becomes a `not_available` claim with the source URL attached — not an empty tree.
- **Accounts failure:** org `970896856` — Regnskapsregisteret returned HTTP 500 after retries; the claim is `failed`, the envelope terminal state is `completed_with_errors`, and the failure appears in `errors[]` with the module name. Nothing was fabricated to fill the gap.
- **Hiring/activity:** `job_postings` and `dated_public_activity` are always emitted as `not_available` with the note that no permitted connector is configured — "not checked" is never presented as "zero postings".

## Refresh / change detection

- **Kit fixture replay** (`scripts/run_refresh_replay.py`): 2 expected changes, 2 observed, **0 false positives**, evidence complete, rerun idempotent.
- **Live re-run:** 10 smoke companies re-researched with the HTTP cache disabled and diffed against the first run's profiles. **9 / 10 produced zero changes**; all registry, financial, role and location facts were perfectly stable. One company (`999008577`) produced 3 diffs, all explicitly labelled `status: "source_error"` because its website fetch failed transiently in the second run — no false *fact* changes were asserted.
- **HTTP surface** (`scripts/serve.py`, stdlib only): `GET /companies/{orgnr}` returned a 58-claim envelope for Equinor ASA (923609016) in 9.3 s cold; `GET /research?org=…&q=…` answered from 18 evidence-bounded facts; `POST /refresh` re-read live, reported 0 changes, and appended a new timestamped snapshot — history files are never overwritten (`latest.json` is a pointer copy).

## Errors and retries

- 602 network attempts for 600 logical requests: 2 automatic retries (both recovered), 1 hard error (the HTTP 500 above, surfaced in the envelope).
- 7 website `source_error` outcomes (timeouts / connection resets on small-company sites) and 1 SSRF-guard `blocked` outcome; each is recorded per-company in `errors[]` and reflected in the terminal state.
- No rate-limit responses (HTTP 429) were received from brreg at any point.

## Cost

| Item | Cost |
| --- | ---: |
| Brønnøysundregistrene APIs (Enhetsregisteret, Regnskapsregisteret) | $0 (open data, no key) |
| Company websites | $0 |
| Models / LLMs | none used |
| **Total per 100-company run** | **$0.00** |
