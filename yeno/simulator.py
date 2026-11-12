#!/usr/bin/env python3
"""Local evaluator simulator for the YeNo volume bot.

Synthesizes 24h of BTC 5-minute Up/Down markets: BTC follows a regime-
switching random walk; the fair win probability is Phi(gap/(sigma*sqrt(tau)));
market-maker books lag the BTC reference by 1-3 s, carry a probability-
dependent spread, depth, and quote noise. The bot under test is called
once per second; its orders execute against the book 0.5 s later (the
contract's 250 ms latency, modelled conservatively). Fees, the $5
fee-inclusive BUY cap, min-order rejection, partial sells, cycle/volume
accounting, and settlement-voids follow yeno-evaluator-contract.md and
reuse the exact fee helpers from yeno_bot.py.

Usage:
  python3 simulator.py                 # grid search + best-variant confirm
  python3 simulator.py --quick         # fewer paths
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import yeno_bot as yb  # noqa: E402

KIT = HERE.parent / "kit"


def _load_house_bot():
    spec = importlib.util.spec_from_file_location("house_bot", KIT / "yeno-house-bot-v1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DT = 0.5               # book/BTC step (seconds)
CALL_EVERY = 2         # bot called every CALL_EVERY steps (1.0 s)
MARKET_SECONDS = 300
N_MARKETS = 288        # 24 h
EPOCH0 = 1_789_880_000.0
MIN_ORDER = 5.0
TARGET_VOLUME = 1000.0
START_CASH = 10.0


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class PathGen:
    """Pre-generates the BTC path and per-market regimes for one 24h run."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        annual_vol = rng.uniform(0.30, 0.55)
        self.sigma_year = annual_vol / math.sqrt(365.25 * 86400.0)
        n_steps = int(N_MARKETS * MARKET_SECONDS / DT) + 64
        btc = [80250.0]
        self.sigma_usd = []          # instantaneous USD vol per sqrt-second
        price = 80250.0
        for m in range(N_MARKETS):
            mult = rng.choices([0.7, 1.0, 1.7], weights=[0.35, 0.40, 0.25])[0]
            drift = rng.uniform(-0.00002, 0.00002)  # per-second relative drift
            steps = int(MARKET_SECONDS / DT)
            for _ in range(steps):
                s = price * self.sigma_year * mult
                self.sigma_usd.append(s)
                price += price * drift * DT + s * math.sqrt(DT) * rng.gauss(0.0, 1.0)
                btc.append(price)
        self.btc = btc

    def at(self, t: float) -> float:
        idx = int(t / DT)
        idx = max(0, min(idx, len(self.btc) - 1))
        return self.btc[idx]

    def sigma_at(self, t: float) -> float:
        idx = int(t / DT)
        idx = max(0, min(idx, len(self.sigma_usd) - 1))
        return self.sigma_usd[idx]


class BookMaker:
    """Builds lagged, noisy L2 books around the fair probability."""

    def __init__(self, rng: random.Random, path: PathGen, market_idx: int):
        self.rng = rng
        self.path = path
        self.market_idx = market_idx
        self.t0 = market_idx * MARKET_SECONDS
        self.target = path.at(self.t0)
        self.lag = rng.uniform(1.0, 3.0)
        self.mm_sigma_mult = rng.uniform(0.9, 1.1)
        self.noise = 0.0

    def _book(self, mid: float, tau: float) -> dict:
        rng = self.rng
        mid = min(max(mid, 0.002), 0.998)
        spread = 0.005 + 0.016 * (1.0 - abs(2.0 * mid - 1.0))
        if tau < 30.0:
            spread *= 1.0 + (30.0 - tau) / 30.0 * 0.5
        spread *= rng.uniform(0.85, 1.2)
        bid = mid - spread / 2.0
        ask = mid + spread / 2.0
        bid = round(min(max(bid, 0.001), 0.998), 3)
        ask = round(min(max(ask, bid + 0.001), 0.999), 3)
        depth_scale = 0.35 if tau < 25.0 else 1.0

        def side(levels_mid: float, is_bid: bool):
            rows = []
            price = bid if is_bid else ask
            for lvl in range(3):
                if lvl:
                    step = rng.uniform(0.001, 0.004)
                    price = price - step if is_bid else price + step
                    price = round(min(max(price, 0.001), 0.999), 3)
                size = math.exp(rng.gauss(math.log(120.0), 0.6)) * depth_scale
                rows.append([price, round(size, 2)])
            return rows

        bids = side(bid, True)
        asks = side(ask, False)
        # Occasionally one-sided near expiry.
        if tau < 12.0 and rng.random() < 0.08:
            asks = []
        if tau < 8.0 and rng.random() < 0.05:
            bids = []
        return {"bids": bids, "asks": asks, "minOrderSize": MIN_ORDER}

    def books(self, t_abs: float) -> dict:
        rng = self.rng
        tau = max(0.5, self.t0 + MARKET_SECONDS - t_abs)
        lag_t = max(self.t0, t_abs - self.lag)
        gap = self.path.at(lag_t) - self.target
        sigma = self.path.sigma_at(lag_t) * self.mm_sigma_mult
        fair_yes = phi(gap / (sigma * math.sqrt(tau)))
        self.noise = 0.7 * self.noise + rng.gauss(0.0, 0.004)
        mid_yes = fair_yes + self.noise
        mid_no = 1.0 - fair_yes + rng.gauss(0.0, 0.002)
        return {"YES": self._book(mid_yes, tau), "NO": self._book(mid_no, tau)}


def _shift_book(book: dict, adverse: float) -> dict:
    """Stress helper: worsen execution prices by `adverse` against us."""
    if adverse <= 0:
        return book
    out = {}
    for side, rows in book.items():
        if side == "minOrderSize":
            out[side] = rows
            continue
        new_rows = []
        for price, size in rows:
            if side == "asks":
                p = min(0.999, price + adverse)
            else:
                p = max(0.001, price - adverse)
            new_rows.append([p, size])
        out[side] = new_rows
    return out


def run_path(bot, seed: int, adverse: float = 0.0) -> dict:
    rng = random.Random(seed)
    path = PathGen(rng)
    cash = START_CASH
    volume = 0.0
    cycles = 0
    fees_paid = 0.0
    position = None            # dict in evaluator position shape
    settled_void = 0
    cash_curve = [cash]
    max_dd = 0.0
    peak = cash
    crossed_at = None
    total_time = N_MARKETS * MARKET_SECONDS

    for m in range(N_MARKETS):
        maker = BookMaker(rng, path, m)
        market_id = f"sim-btc-5m-{m}"
        steps = int(MARKET_SECONDS / DT)
        pending = None         # (action dict) to execute next step
        for s in range(steps):
            t_abs = m * MARKET_SECONDS + s * DT
            tau = MARKET_SECONDS - s * DT
            # --- execute pending order against this step's book ---
            if pending is not None:
                books_exec = maker.books(t_abs)
                if pending["action"] == "BUY" and position is None:
                    book = _shift_book(books_exec[pending["outcome"]], adverse)
                    budget = min(float(pending["maxCashUsd"]), cash)
                    got = yb._buy_with_cash(book["asks"], budget)
                    if got is not None:
                        fills, gross, fees = got
                        shares = sum(q for _, q in fills)
                        if shares >= MIN_ORDER - 1e-9 and gross + fees <= budget + 1e-6:
                            cash -= gross + fees
                            fees_paid += fees
                            position = {
                                "market_id": market_id,
                                "outcome": pending["outcome"],
                                "shares": shares,
                                "buy_gross_usd": gross,
                                "buy_fees_usd": fees,
                                "sell_gross_usd": 0.0,
                                "sell_fees_usd": 0.0,
                            }
                elif pending["action"] == "SELL" and position is not None \
                        and position["market_id"] == market_id:
                    book = _shift_book(books_exec[position["outcome"]], adverse)
                    got = yb._sell_proceeds(book["bids"], position["shares"], allow_partial=True)
                    if got is not None:
                        gross, fees, filled = got
                        cash += gross - fees
                        fees_paid += fees
                        position["sell_gross_usd"] += gross
                        position["sell_fees_usd"] += fees
                        position["shares"] -= filled
                        if position["shares"] <= 1e-9:
                            volume += position["buy_gross_usd"] + position["sell_gross_usd"]
                            cycles += 1
                            position = None
                            if volume >= TARGET_VOLUME:
                                crossed_at = t_abs
                pending = None
                peak = max(peak, cash)
                max_dd = max(max_dd, peak - cash)
                cash_curve.append(cash)
                if crossed_at is not None:
                    break
            # --- call the bot ---
            if s % CALL_EVERY == 0 and pending is None:
                ref_lag = rng.uniform(0.1, 0.9)
                ref_t = max(0.0, t_abs - ref_lag)
                obs = {
                    "schemaVersion": 1,
                    "timestamp": EPOCH0 + t_abs,
                    "market": {"id": market_id, "secondsToClose": tau},
                    "account": {
                        "cashUsd": round(cash, 6),
                        "eligibleVolumeUsd": round(volume, 6),
                        "completedCycles": cycles,
                        "position": position,
                    },
                    "rules": {
                        "maximumBuyCashUsd": 5,
                        "targetVolumeUsd": 1000,
                        "evaluationWindowHours": 24,
                        "onePositionAtATime": True,
                        "buyFeesIncludedInMaximum": True,
                    },
                    "reference": {
                        "btcMidUsd": path.at(ref_t),
                        "openingTargetUsd": maker.target,
                        "observedAt": EPOCH0 + ref_t,
                        "targetObservedAt": EPOCH0 + maker.t0,
                        "targetProvisional": False,
                    },
                    "books": maker.books(t_abs),
                }
                decision = bot.decide(obs)
                if isinstance(decision, dict) and decision.get("action") in ("BUY", "SELL"):
                    if decision["action"] == "SELL" and position is None:
                        pass
                    elif decision["action"] == "BUY" and position is not None:
                        pass
                    else:
                        pending = decision
        if crossed_at is not None:
            break
        # --- market settles: open position voids its cycle volume ---
        if position is not None and position["market_id"] == market_id:
            end_price = path.at((m + 1) * MARKET_SECONDS)
            won = (end_price > maker.target) if position["outcome"] == "YES" else (end_price <= maker.target)
            cash += position["shares"] * (1.0 if won else 0.0)
            settled_void += 1
            position = None
            peak = max(peak, cash)
            max_dd = max(max_dd, peak - cash)

    return {
        "qualified": crossed_at is not None,
        "cross_time_h": (crossed_at / 3600.0) if crossed_at is not None else None,
        "cash_at_cross": round(cash, 2) if crossed_at is not None else None,
        "final_cash": round(cash, 2),
        "final_volume": round(volume, 2),
        "cycles": cycles,
        "fees_paid": round(fees_paid, 2),
        "max_drawdown": round(max_dd, 2),
        "settled_void_cycles": settled_void,
        "ended_flat": position is None,
    }


def summarize(results: list[dict]) -> dict:
    n = len(results)
    qual = [r for r in results if r["qualified"]]
    cash_cross = sorted(r["cash_at_cross"] for r in qual)
    vols = sorted(r["final_volume"] for r in results)
    median = lambda xs: xs[len(xs) // 2] if xs else None  # noqa: E731
    return {
        "paths": n,
        "qualified": len(qual),
        "qual_rate": round(len(qual) / n, 3) if n else 0.0,
        "median_cash_at_cross": median(cash_cross),
        "min_cash_at_cross": min(cash_cross) if cash_cross else None,
        "median_final_volume": median(vols),
        "median_cycles": median(sorted(r["cycles"] for r in results)),
        "median_fees": median(sorted(r["fees_paid"] for r in results)),
        "median_max_dd": median(sorted(r["max_drawdown"] for r in results)),
        "median_cross_time_h": median(sorted(r["cross_time_h"] for r in qual)) if qual else None,
        "total_settled_void": sum(r["settled_void_cycles"] for r in results),
    }


VARIANTS = {
    "mine-default": {},
    "mine-tight": {"MIN_EDGE": 0.03, "MIN_EST_PNL_USD": 0.08},
    "mine-loose": {"MIN_EDGE": 0.005, "MIN_EST_PNL_USD": 0.02},
    "mine-conv-0.45": {"CONV_FRAC": 0.45},
    "mine-conv-0.75": {"CONV_FRAC": 0.75},
    "mine-instant-0.35": {"MAX_INSTANT_LOSS_USD": 0.35},
    "mine-holdproj-20": {"HOLD_PROJ_SECONDS": 20.0},
    "mine-holdproj-60": {"HOLD_PROJ_SECONDS": 60.0},
    "mine-cycles-8": {"MAX_CYCLES_PER_MARKET": 8},
    "mine-noshrink": {"Z_SHRINK": 1.0},
    "mine-shrink-0.7": {"Z_SHRINK": 0.7},
}


def run_variant(name: str, overrides: dict, n_paths: int, seed0: int, adverse: float = 0.0):
    saved = {k: getattr(yb, k) for k in overrides}
    for k, v in overrides.items():
        setattr(yb, k, v)
    try:
        results = [run_path(yb.VolumeBot(), seed0 + i, adverse=adverse) for i in range(n_paths)]
    finally:
        for k, v in saved.items():
            setattr(yb, k, v)
    return summarize(results), results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", type=str, default="")
    args = ap.parse_args()
    n_grid = 8 if args.quick else 12
    n_confirm = 15 if args.quick else 30
    seed0 = 20261113
    out: dict = {}

    t0 = time.time()
    house = _load_house_bot()
    house_results = [run_path(house.HouseBot(), seed0 + i) for i in range(n_grid)]
    out["house-bot-v1"] = summarize(house_results)
    print(f"[house-bot-v1] {out['house-bot-v1']}", flush=True)

    names = [args.only] if args.only else list(VARIANTS)
    best_name, best_key = None, None
    for name in names:
        summary, _ = run_variant(name, VARIANTS[name], n_grid, seed0)
        out[name] = summary
        print(f"[{name}] {summary}  ({time.time()-t0:.0f}s)", flush=True)
        key = (summary["qual_rate"], summary["median_cash_at_cross"] or 0.0)
        if best_key is None or key > best_key:
            best_key, best_name = key, name

    print(f"\nBest on grid: {best_name} -> confirming with {n_confirm} fresh-seed paths + stress")
    summary, results = run_variant(best_name, VARIANTS[best_name], n_confirm, seed0 + 100000)
    out[f"{best_name}-confirm"] = summary
    print(f"[{best_name}-confirm] {summary}", flush=True)
    stress, _ = run_variant(best_name, VARIANTS[best_name], n_grid, seed0 + 200000, adverse=0.01)
    out[f"{best_name}-stress-1c"] = stress
    print(f"[{best_name}-stress-1c] {stress}", flush=True)
    out["_best_variant"] = best_name
    out["_best_overrides"] = VARIANTS[best_name]
    out["_per_path_confirm"] = results
    (HERE / "sim_results.json").write_text(json.dumps(out, indent=2))
    print(f"\nWrote {HERE / 'sim_results.json'}  total {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
