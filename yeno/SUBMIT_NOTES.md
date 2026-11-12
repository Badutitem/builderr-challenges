# YeNo Volume Bot — submit notes

**Deliverable:** `yeno_bot.py` — a self-contained, stdlib-only Python
`POST /decide` server (same scaffold/entry point as the kit starter bot).
Run: `python3 yeno_bot.py` (listens on 127.0.0.1:8080). Nothing submitted
yet — parent agent handles submission (email submit@builderr.ai with bot
name, contact, repo/endpoint, immutable commit).

## Strategy (entry / exit)

- Fair-value every market from the BTC reference: fair = Phi(z),
  z = (btcMid − openingTarget) / (sigma * sqrt(tau)), with sigma estimated
  online from the reference stream (EWMA of squared returns, default
  5.7 USD/sqrt(s), clamped 2–18) and z haircut 0.85 for model error.
  Stale (>1.5 s), provisional, or future-stamped references are rejected.
- Fee math drives everything: round-trip drag is 1% + 0.07(1−p) per leg,
  i.e. ~8.6% of notional at p=0.5 but only ~0.2–0.5% when buying a
  favourite at 0.95 that converges to 0.97–0.99. Pure churn cannot work
  (~100 cycles × even 2% drag > the $10 stake); the bot only trades when
  the book lags fair value, and prices the *tau-decay drift*: as seconds
  to close shrink, the favourite's fair value rises mechanically, so the
  expected exit is projected 35 s forward and the bot requires the
  projected cycle PnL (after both legs' fees) ≥ $0.02 with the current
  fair − ask ≥ 0.005.
- Every BUY deploys the full fee-inclusive cap (min(cash, $5)) → ~$9.8–10
  of eligible volume per closed cycle → ~101 cycles to $1,000.
- Exits: take-profit at +$0.03 full-cycle PnL; exit when the bid reaches
  fair (convergence captured); stop-loss −$0.50; max hold 75 s; forced
  exit from 30 s to close and unconditional exit from 18 s (partial if
  needed) so no cycle ever rides into settlement (settlements void the
  cycle's volume). Max 5 cycles per market, 1 s re-entry cooldown.

## Chosen parameters

MIN_EDGE 0.005 · MIN_EST_PNL_USD 0.02 · CONV_FRAC 0.60 ·
HOLD_PROJ_SECONDS 35 · Z_SHRINK 0.85 · MAX_INSTANT_LOSS_USD 0.60 ·
TAKE_PROFIT 0.03 · STOP_LOSS 0.50 · FORCED_EXIT_TAU 30 · HARD_EXIT_TAU 18 ·
MAX_CYCLES_PER_MARKET 5 · entry window 22–260 s to close.
(Selected over 11 variants on a 12-path grid + 30-path confirmation;
tighter gates preserved more cash per crossing but qualified far less
often, and qualification is the binding constraint — the house bot's
published best was $942.59 with $0 crossings in 30 paths.)

## Test evidence

- Kit smoke test (`yeno-smoke-test.py` against the running final bot):
  **PASS** (`contractSmokePassed: true`, valid BUY within the $5 cap).
- decide() latency ~0.002 ms/call locally (limit 200 ms). Pure Python,
  no network, no LLM.
- Local simulator (`simulator.py`, synthetic lagged-book paths, contract
  fee/fill rules): grid 10/12 qualified, median cash at crossing $11.30;
  **confirmation on 30 fresh seeds: 23/30 qualified (76.7%), median cash
  at crossing $7.58, median time to crossing 16.8 h, 0 settled-void
  cycles.** House Bot v1 in the same simulator: 0/12, median volume $327.

## Known risks

- Adverse execution is the main threat: worsening execution prices by
  +1¢ on both legs drops the sim qualification rate to ~0–8% (median
  volume ~$450–590) — the same failure mode the official benchmark
  documents for the house bot under its 2¢ stress. Mitigations in place:
  projected-PnL gating, instant-loss sanity cap, fast take-profit exits.
- The simulator's books are synthetic (1–3 s MM lag, ~0.5–2.1¢ spreads);
  real YeNo book behaviour on the hidden forward run will differ, in
  either direction.
- If the evaluator's per-market call cadence is much slower than 1/s,
  entry/exit timing granularity coarsens and results degrade.
- Positions in a market different from the current observation are left
  to settle (cannot be priced against the wrong book); forced exits make
  this a near-impossible path in testing (0 occurrences in 42 paths).
