# Meridian — builderr Trading Agents (Round 2) submission notes

**File to submit:** `agent.py` (this folder). Nothing has been submitted — no PR, no email.

## Strategy summary
- Ranks the **whole tradable universe** (~1000 names) by blended momentum (63d/21d-led, 126d stabilizer, + SMA50 gap, − vol penalty); holds the **top 7** names that are above their 20d & 50d averages with positive score. The Round 1 winner only ranks a fixed 36-name pool — a full scan finds leaders outside it.
- Sizing = geometric mean of rank-linear conviction and inverse-volatility, capped at **24%** per name; anti-churn buffer keeps incumbents ranked in the top 10.
- Regime layer on SPY/QQQ: **FULL** (both indices trending, breadth ≥45%, calm vol) adds a ≤30% QLD/SSO 2x sleeve; **NEUTRAL** holds an 85% core; **OFF** (index below SMA50, 3-day index drop < −5%, or vol spike) goes to cash, with hysteresis + a momentum-thrust V-recovery re-entry.
- Exposure is **volatility-targeted** (20% target), a **soft brake** halves the budget on a −3.5% 5-day index drop (days before the SMA50 break confirms), a **drawdown taper** cuts size at −5%/−9%/−13% peak drawdown, and each position has an **8.5% trailing stop** with a 3-day re-entry block.
- Long-only, dollar gross ≤ 1.0x, beta gross self-clamped ≤ 1.40x, ≤ 45 orders/call, stdlib only, no network/LLM, deterministic, all timing bar-date based.

## Validation — commands run and outputs

### 1. Admission preview (the exact gate admission uses)
`cd ~/workspace/builderr/trading/template && python3 preview.py ~/workspace/builderr/trading/final/agent.py`

| window | Ret | MaxDD | Sharpe | Calmar | Trades |
|---|---|---|---|---|---|
| calm_uptrend | +6.51% | 2.99% | 2.25 | 9.43 | 109 |
| moderate_selloff | −4.69% | 5.42% | −4.64 | −5.97 | 30 |
| vol_spike_snapback | −6.77% | 7.53% | −2.94 | −3.96 | 26 |

Safety bar: **[PASS] runs clean · [PASS] leverage (peak gross 1.00x ≤ 1.5x) · [PASS] concentration (peak 24%, 0 days ≥30%) · [PASS] no blow-up (worst DD 7.5% < 50%)** → "✓ You clear admission's safety bar".

Same preview, same windows, for comparison (sample windows are diagnostics, not the score):
| agent | calm | selloff | vol spike | worst DD |
|---|---|---|---|---|
| **Meridian (this entry)** | +6.51% | −4.69% | **−6.77%** | **7.5%** |
| Round 1 winner (`winner/round_1_winner_agent.py`) | +10.48% | −4.39% | −9.38% | 10.2% |
| Template shipped `agent.py` | +14.65% | −3.36% | −15.77% | 33.5% |

### 2. Constraint + performance self-check (synthetic 1000-ticker × 220-bar universe)
`cd ~/workspace/builderr/trading/final && python3 check_constraints.py` → **ALL CONSTRAINT CHECKS PASSED**
- worst `decide()` runtime over 12 simulated days: **0.635s** (limit 5s)
- max orders in one call: **7** (limit 50); day-1: 7 buys
- peak beta-adjusted gross: **0.934x** (limit 1.5x); peak single-name concentration: **17.4%** (limit <30%)
- determinism verified (fresh module, same inputs → identical orders); robustness probes ran (empty market → `[]`; 40-bar history → no crash; missing SPY → trades on QQQ; missing both indices → liquidates holdings, no crash)

### 3. Generic smoke test
`cd ~/workspace/builderr/trading/template && python3 selfcheck.py ~/workspace/builderr/trading/final/agent.py`
→ "✓ loaded and ran 12 steps cleanly." It emitted 0 orders because selfcheck's tape is a pure random walk — the agent correctly stays in cash with no trend (on trending synthetic data it trades; see check #2).

### 4. Parameter robustness (±20% perturbations, preview re-run)
VOL_TARGET 0.16/0.24 → calm +6.66%/+6.49%; W_MOM_MID 0.36/0.54 → calm +6.22%/+7.37%, selloff −4.60%/−4.63%; BRAKE_RET3 −0.06 and TRAIL_STOP 0.068 → unchanged. Admission PASS in all variants; returns move ≤ ~0.9pt — no cliff, no overfit signature.

### Not run (and why)
- `local_test.py` / `full_test.py`: require the private builderr engine + a Polygon API key — unavailable locally (documented as reference-only in the template README).
- `strategy_selftest.py`: hard-coded to the template agent's internal function names (`target_weights`, `BETA_MULTIPLE`, …); not applicable to a different implementation. `check_constraints.py` (included) covers the equivalent contract/cap checks generically.
- `endpoint_harness_selftest.py` / `live_runner_selftest.py`: for endpoint/live-runner modes, not the `decide()` submission path.

## Known weaknesses
- **Calm-window return lags the winner on the sample data** (+6.5% vs +10.5%): the winner's 5-name, up-to-28% concentration wins when one or two mega-caps rip for weeks. This entry trades some of that upside for a materially better crash profile (−6.8% vs −9.4%, worst DD 7.5% vs 10.2%). In a strong, uninterrupted forward tape it may finish behind Arnav; in any tape with a real dip it should finish ahead.
- The scoring window is short (~7 sessions from the 15 Nov close to 25 Nov), so results will be dominated by the market's path that week — no daily system can control that; the design goal was positive participation with a hard floor under losses.
- The 2x sleeve (QLD/SSO) never activates on the sample windows because those datasets don't include QLD/SSO (they ship TQQQ/SOXL instead); the sleeve path is exercised only in the synthetic constraint check. It is conservatively gated (FULL state + QQQ vol < 28% + SMA20 > SMA50) and capped, but it is the least field-tested part of the code.
- Rebalance cadence is 3 days; in a slow, choppy sideways tape the book can lag index turns by a few sessions (hysteresis is deliberate — it is what keeps whipsaw costs down).
