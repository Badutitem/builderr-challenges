"""Constraint + performance self-check for final/agent.py.

Builds a synthetic 1000-ticker x 220-bar universe (mixed trends), then:
  * times decide() (limit: 5s per call),
  * simulates fills day by day for 12 trading days and verifies, on every
    day: <= 50 orders, positive integer quantities, sells never exceed
    holdings (long-only), post-fill single-name weight < 30%, and
    beta-adjusted gross <= 1.5x,
  * checks determinism (fresh module, same inputs -> same orders),
  * checks robustness: empty market, short history, missing SPY, missing
    both indices (must de-risk, not crash).

Run:  python3 check_constraints.py
"""
from __future__ import annotations

import importlib.util
import math
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
BETA_3X = {"TQQQ", "SOXL", "UPRO", "SPXL", "TNA", "FAS", "TECL", "LABU",
           "CURE", "DRN", "UDOW", "NAIL"}
BETA_2X = {"QLD", "SSO", "DDM", "ROM", "UWM", "AGQ"}


def beta(t: str) -> float:
    return 3.0 if t in BETA_3X else 2.0 if t in BETA_2X else 1.0


def load_agent():
    spec = importlib.util.spec_from_file_location("agent_final", HERE / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_bars(rng: random.Random, days: int, start: float, drift: float,
              vol: float, day0: int = 0) -> list:
    bars = []
    px = start
    for i in range(days):
        px *= math.exp(drift + rng.gauss(0.0, vol))
        d = day0 + i
        bars.append({
            "ts": f"2026-{(d // 28) + 1:02d}-{(d % 28) + 1:02d}",
            "open": px, "high": px * 1.005, "low": px * 0.995,
            "close": px, "volume": 1_000_000,
        })
    return bars


def make_universe(n: int = 1000, days: int = 220, seed: int = 7) -> dict:
    rng = random.Random(seed)
    market = {}
    # Indices: healthy uptrend (risk-on tape).
    market["SPY"] = make_bars(rng, days, 500.0, 0.0006, 0.008)
    market["QQQ"] = make_bars(rng, days, 450.0, 0.0008, 0.010)
    market["QLD"] = make_bars(rng, days, 80.0, 0.0016, 0.020)
    market["SSO"] = make_bars(rng, days, 70.0, 0.0012, 0.016)
    market["TQQQ"] = make_bars(rng, days, 60.0, 0.0024, 0.030)
    market["SOXL"] = make_bars(rng, days, 40.0, 0.0020, 0.045)
    for i in range(n - 6):
        t = f"T{i:04d}"
        kind = rng.random()
        if kind < 0.45:      # uptrend
            drift, vol = rng.uniform(0.0005, 0.0022), rng.uniform(0.008, 0.035)
        elif kind < 0.70:    # downtrend
            drift, vol = -rng.uniform(0.0005, 0.0020), rng.uniform(0.008, 0.030)
        else:                # chop
            drift, vol = rng.uniform(-0.0002, 0.0002), rng.uniform(0.010, 0.045)
        market[t] = make_bars(rng, days, rng.uniform(5.0, 800.0), drift, vol)
    return market


def advance(market: dict, rng: random.Random, day: int) -> dict:
    out = {}
    for t, bars in market.items():
        last = bars[-1]["close"]
        drift = 0.0016 if t in ("QLD",) else 0.0008 if t in ("SPY", "QQQ") else 0.0
        px = last * math.exp(drift + rng.gauss(0.0, 0.012))
        out[t] = bars + [{
            "ts": f"2026-{(day // 28) + 1:02d}-{(day % 28) + 1:02d}",
            "open": px, "high": px * 1.005, "low": px * 0.995,
            "close": px, "volume": 1_000_000,
        }]
    return out


def main() -> int:
    mod = load_agent()
    market = make_universe()
    rng = random.Random(99)
    cash = 100_000.0
    positions: dict[str, float] = {}
    avg_cost: dict[str, float] = {}
    worst = {"runtime": 0.0, "gross": 0.0, "conc": 0.0, "orders": 0}
    failures = []

    for day in range(12):
        prices = {t: b[-1]["close"] for t, b in market.items()}
        ps = {
            "cash": cash,
            "positions": [
                {"ticker": t, "quantity": q, "avg_cost": avg_cost.get(t, 0.0)}
                for t, q in positions.items() if q > 0
            ],
            "last_prices": prices,
        }
        t0 = time.perf_counter()
        orders = mod.decide(market, ps, cash)
        worst["runtime"] = max(worst["runtime"], time.perf_counter() - t0)
        worst["orders"] = max(worst["orders"], len(orders))

        if len(orders) > 50:
            failures.append(f"day {day}: {len(orders)} orders > 50")
        for o in orders:
            if o["side"] not in ("buy", "sell") or not isinstance(o["quantity"], int) \
                    or o["quantity"] <= 0:
                failures.append(f"day {day}: malformed order {o}")
            if o["ticker"] not in market:
                failures.append(f"day {day}: order for unknown ticker {o['ticker']}")
            if o["side"] == "sell" and o["quantity"] > positions.get(o["ticker"], 0.0):
                failures.append(f"day {day}: oversell {o}")
        # Apply fills at last close.
        for o in orders:
            px = prices[o["ticker"]]
            if o["side"] == "buy":
                cost = px * o["quantity"]
                if cost > cash:  # engine would scale down; flag if wildly over
                    if cost > cash * 1.05:
                        failures.append(f"day {day}: buy exceeds cash by >5%: {o}")
                    qty = int(cash // px)
                    cost = px * qty
                else:
                    qty = o["quantity"]
                if qty > 0:
                    held = positions.get(o["ticker"], 0.0)
                    avg_cost[o["ticker"]] = (
                        avg_cost.get(o["ticker"], 0.0) * held + cost) / (held + qty)
                    positions[o["ticker"]] = held + qty
                    cash -= cost
            else:
                qty = min(o["quantity"], positions.get(o["ticker"], 0.0))
                positions[o["ticker"]] = positions.get(o["ticker"], 0.0) - qty
                cash += px * qty
        equity = cash + sum(positions.get(t, 0.0) * prices[t] for t in positions)
        if equity > 0:
            gross = sum(abs(positions.get(t, 0.0) * prices[t]) * beta(t)
                        for t in positions) / equity
            conc = max((abs(positions.get(t, 0.0) * prices[t]) / equity
                        for t in positions), default=0.0)
            worst["gross"] = max(worst["gross"], gross)
            worst["conc"] = max(worst["conc"], conc)
            if gross > 1.5 + 1e-9:
                failures.append(f"day {day}: gross {gross:.3f} > 1.5")
            if conc >= 0.30:
                failures.append(f"day {day}: concentration {conc:.3f} >= 0.30")
        market = advance(market, rng, 220 + day)

    # Determinism: fresh module, identical first-day inputs -> identical orders.
    m1, m2 = load_agent(), load_agent()
    uni = make_universe()
    ps0 = {"cash": 100_000.0, "positions": [],
           "last_prices": {t: b[-1]["close"] for t, b in uni.items()}}
    o1 = m1.decide(uni, ps0, 100_000.0)
    o2 = m2.decide(uni, ps0, 100_000.0)
    if o1 != o2:
        failures.append("determinism: same inputs produced different orders")
    if not o1:
        failures.append("determinism check: agent placed no orders on day 1")

    # Robustness probes.
    probes = load_agent()
    assert probes.decide({}, {"cash": 1.0, "positions": [], "last_prices": {}}, 1.0) == []
    short = {t: b[:40] for t, b in list(uni.items())[:50]}
    probes.decide(short, {"cash": 100_000.0, "positions": [], "last_prices": {}}, 100_000.0)
    no_spy = {t: b for t, b in uni.items() if t != "SPY"}
    probes.decide(no_spy, ps0, 100_000.0)
    no_idx = {t: b for t, b in uni.items() if t not in ("SPY", "QQQ")}
    held_ps = {"cash": 50_000.0,
               "positions": [{"ticker": "T0001", "quantity": 10, "avg_cost": 100.0}],
               "last_prices": {"T0001": 100.0}}
    out = probes.decide(no_idx, held_ps, 50_000.0)
    if not any(o["side"] == "sell" and o["ticker"] == "T0001" for o in out):
        failures.append("data-guard: missing indices did not liquidate holdings")

    print(f"orders day-1: {len(o1)}  (sample: {o1[:3]})")
    print(f"worst decide() runtime over 12 days (1000 tickers): {worst['runtime']:.3f}s  (limit 5s)")
    print(f"max orders in one call: {worst['orders']}  (limit 50)")
    print(f"peak beta-adjusted gross: {worst['gross']:.3f}x  (limit 1.5x)")
    print(f"peak single-name concentration: {worst['conc']*100:.1f}%  (limit <30%)")
    print("determinism + robustness probes: ran")
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CONSTRAINT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
