# Revision candidate — "Dual" (would replace Meridian v1)

**File:** `agent.py` in this folder (= `variants/dual/agent.py`, tournament winner).
**Status:** NOT submitted. Parent agent submits if the revision is approved.
Full evidence: `../variants/TOURNAMENT.md` (+ `results.json`, `perturb_results.json`).

## What changed vs v1 (Meridian) — selection & sizing only

The chassis is untouched: same regime layer (FULL/NEUTRAL/OFF, brakes, hysteresis,
thrust re-entry), same volatility targeting (20%), same drawdown taper, same 8.5%
trailing stops, same QLD/SSO sleeve logic, same index fallback, same order engine.

1. **Momentum score simplified to classic dual momentum:**
   `score = 0.50·r126 + 0.50·r63` (v1: 0.15·r126 + 0.45·r63 + 0.30·r21 + 0.10·SMA50-gap
   − 0.10·vol20). The 21d leg, gap term and vol penalty are gone.
2. **Relative-strength gate added:** a name only qualifies if its 63d return beats
   SPY's 63d return (dual momentum's relative leg), on top of v1's absolute gates
   (price above SMA20/SMA50, 21d return > −3%, score > 0).
3. **Equal-weight sizing:** top **8** names (v1: top 7) at equal weight, per-name cap
   **20%** (v1: rank×inverse-vol geometric blend, cap 24%). Anti-churn buffer widened
   (KEEP_RANK 12, FORCE_RANK 5).

## Why it clears the "clearly better" bar (11 windows: 3 samples + 8 synthetic tapes)

| metric (11 windows) | Meridian v1 | Dual v2 |
|---|---|---|
| worst-case return | −12.24% | **−11.00%** |
| worst max drawdown | 12.24% | **11.00%** |
| average return | +7.41% | **+8.24%** |
| average Sharpe | +1.22 | **+1.74** |
| average Calmar (raw / capped@20) | +44.0 / +6.51 | **+69.2 / +6.61** |
| head-to-head | — | 7W / 2L / 2T on returns; lower DD in 7/11 |

Sample regimes (official engine): calm +7.99% vs +6.51% · selloff −4.38% vs −4.69% ·
vol spike −6.47% vs −6.77% · worst sample DD 7.34% vs 7.53%.
Fresh-seed tapes (never used in selection): wins the crash tape by +1.69 pts, ties the
grind-down, gives back −0.90/−2.27 pts in the two up-tapes — the edge is in the left
tail, which is what the short forward window punishes most.
±20% perturbations (8 variants, all 11 windows): worst return stays in
−9.95%…−12.02%, avg Sharpe +1.32…+1.82, zero errors — no overfit cliff.

## Admission preview (official gate) — `python3 preview.py final-v2/agent.py`

```
  window                   Ret   MaxDD  Sharpe  Calmar  Trades
  calm_uptrend           7.99%   3.39%    2.73   10.43     107
  moderate_selloff      -4.38%   5.11%   -4.35   -5.98      28
  vol_spike_snapback    -6.47%   7.34%   -2.85   -3.90      30

Safety bar (exactly what admission gates on):
  [PASS] runs clean, no errors
  [PASS] no leverage breach   (peak gross 1.00x  <= 1.5x)
  [PASS] no concentration breach (peak 20%; longest run >= 30% was 0d, limit is 5d)
  [PASS] no blow-up           (worst drawdown 7.3%  < 50%)
VERDICT: ✓ You clear admission's safety bar
```

## Constraint check (1000-ticker synthetic, adapted check_constraints.py)

```
orders day-1: 8
worst decide() runtime over 12 days (1000 tickers): 0.774s  (limit 5s)
max orders in one call: 8  (limit 50)
peak beta-adjusted gross: 0.902x  (limit 1.5x)
peak single-name concentration: 11.9%  (limit <30%)
determinism + robustness probes: ran
ALL CONSTRAINT CHECKS PASSED
```
