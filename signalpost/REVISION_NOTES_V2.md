# Signalpost v2 — local test report (2026-11-20, NOT submitted)

Built and tested locally on the user's explicit order ("Mulai bangun + test
v2 lokal sekarang, jangan submit"). The submitted v1 (commit `1ac2c75f`,
repo github.com/Badutitem/builderr-challenges) is untouched: no commit, no
push, no email. v2 lives only in `~/workspace/builderr/signalpost/v2/agent/`.

## What changed vs v1 (safe scope only)

1. `website.py` — crawler priority terms now include careers/jobs labels
   (karriere, ledige-stillinger, stillinger, jobb, career(s), jobs,
   vacancies); secondary-page limit 4 → 6.
2. `contract.py` — `job_postings` / `dated_public_activity` are no longer
   hard-coded `not_available`. They are derived **only** from bounded pages
   crawled on the identity-verified (publishable) company website:
   careers-path pages → `job_postings` (records the page, never invents
   postings/counts), news-path pages → `dated_public_activity`. Quarantined
   sites contribute nothing; no external connector was wired in; the
   identity gate is unchanged.
3. `contract.py` — synthesis mentions a found careers page / news pages;
   `sections.hiring_and_activity` reflects real availability.
4. `scripts/serve.py` — minimal HTML UX: `/` find + list page and
   `/view/{org}` claim table with verify-source links and labelled
   unknowns. JSON routes unchanged.

## Test results

- **Unit tests:** 111 passed, 5 subtests passed (v1: 108 + 5). The 3 new
  tests pin the gate behaviour: publishable site + career/news pages →
  claims `available`; quarantined site → both stay `not_available`;
  publishable site without matching pages → `not_available` ("not found").
- **Offline re-conversion, same 100 smoke profiles, v1 code vs v2 code:**
  100/100 envelopes, unique orgs, terminal split identical (92 completed /
  8 completed_with_errors), 0 broken evidence references in either,
  total claims 2,601 → 2,602. The **only** availability change anywhere:
  `dated_public_activity` 0 → 3 companies / 4 claims (Timeit ×2, JHP ×1,
  Netlight newsroom ×1). `job_postings` stayed 0 on the old crawl data —
  the v1 crawler had never fetched a careers page.
- **Live run, 6 companies, 2026-11-20 21:31–21:33 WIB** (run id
  `v2-live6-20261120`, outputs in `v2/test-output/`): 6/6 envelopes,
  official batch validation **PASSED** (exact count, unique, all terminal,
  claims+evidence present, no missing-converted-to-zero), all 6 terminal
  `completed`, wall 135.5 s at 2 workers, mean 31.4 s/company (v1 smoke:
  34.6 s/company at 4 workers — no runtime blow-up), cost $0.
  - NETLIGHT AS: **`job_postings` found** — https://www.netlight.com/career
    (the v1 crawl of the same site never fetched it) + newsroom activity.
  - TIMEIT AS: 3 activity claims (aktuelt archive, one article, category).
  - Fjord Tours, Acconor, K-U-K, Ngopi: no careers/news page inside the
    bounded crawl → honestly `not_available`, as in v1.

## What this does and does not prove

Proves: the v2 code path works end-to-end live, fills the two zero-recall
families where the verified site actually publishes such pages, and does
not touch any other claim family or the precision gate.
Does not prove: an official score. On the full 1,000-company batch the
gain depends on how many publishable sites expose careers/news pages
inside a 6-page crawl; the 100-company offline data suggests the effect
is real but modest (activity) and careers coverage is bounded by the
~12% publishable-site rate.

## Decision pending

Submit decision is the user's, before the 22 Nov 2026 revision close
(timezone not stated by Builderr). Nothing has been submitted.
