#!/usr/bin/env python3
"""YeNo x Builderr Volume Bot Challenge entry.

Strategy: late-market convergence capture on the favourite side.
The bot fair-values each 5-minute market from the timestamped BTC
reference (gap to the opening target, normalised by an online realised-
volatility estimate), and only buys a side when its best ask sits far
enough below fair value to cover the full round-trip fee drag (1%
platform + 0.07*shares*p*(1-p) protocol fee on BOTH legs) plus spread
and a safety margin. It deploys the full fee-inclusive $5 cap per BUY
(~$9.8 of volume per closed cycle, so ~102 cycles reach $1,000) and
exits as soon as the bid converges toward fair value, on a stop-loss,
on a maximum hold time, or on a forced schedule well before expiry
(so every cycle closes and counts, and the account ends flat).

Pure Python standard library. No network, no LLM. decide() is O(book
depth) plus O(1) state updates: far below the 200 ms timeout.
"""

from __future__ import annotations

import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --------------------------------------------------------------------------
# Tunable parameters (simulator monkeypatches these on the module).
# --------------------------------------------------------------------------
SIGMA_DEFAULT_PER_SQRT_S = 5.7      # BTC ~40% annualised vol at ~$80k
SIGMA_MIN = 2.0
SIGMA_MAX = 18.0
SIGMA_EWMA_ALPHA = 0.04
Z_SHRINK = 0.85                     # haircut on model z (model-error guard)

REF_MAX_AGE_SECONDS = 1.5           # reject stale reference rows

ENTRY_TAU_MIN = 22.0                # never enter inside this many seconds
ENTRY_TAU_MAX = 260.0               # entry window (seconds to close)
MIN_EDGE = 0.005                    # current fair - ask required
MIN_EST_PNL_USD = 0.02              # projected cycle PnL required
MAX_INSTANT_LOSS_USD = 0.60         # sanity cap on instant round-trip loss
MAX_ASK = 0.985                     # above this, $5 cannot clear min order
MAX_SPREAD = 0.06                   # skip one-sided / very wide entry books
CONV_FRAC = 0.60                    # fraction of projected gap we expect to capture
HOLD_PROJ_SECONDS = 35.0            # horizon for the tau-decay projection
MAX_CYCLES_PER_MARKET = 5
REENTRY_COOLDOWN_SECONDS = 1.0

TAKE_PROFIT_USD = 0.03              # lock in at this full-cycle PnL
CONVERGED_BID_EPS = 0.015           # bid within this of fair = converged
CONVERGED_MIN_PNL_USD = -0.05
STOP_LOSS_USD = 0.50                # cut at this full-cycle loss
MAX_HOLD_SECONDS = 75.0             # age-based exit (if loss is bearable)
MAX_HOLD_MAX_LOSS_USD = 0.25
FORCED_EXIT_TAU = 30.0              # below this tau, exit if loss <= cap
FORCED_EXIT_MAX_LOSS_USD = 1.20
HARD_EXIT_TAU = 18.0                # below this tau, exit at any price


# --------------------------------------------------------------------------
# Book / fee helpers (fee model mirrors the evaluator contract exactly:
# protocol fee 0.07 * shares * price * (1 - price) per side, rounded to
# five decimals, plus 1% of gross notional per side, rounded to five).
# --------------------------------------------------------------------------
def _levels(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            price, size = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            output.append((price, size))
    return sorted(output, reverse=reverse)


def _fees(fills: list[tuple[float, float]]) -> float:
    gross = sum(price * shares for price, shares in fills)
    protocol = sum(0.07 * shares * price * (1 - price) for price, shares in fills)
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


def _buy_with_cash(asks: Any, cash: float) -> tuple[list[tuple[float, float]], float, float] | None:
    """Walk the ask book spending at most `cash` (fees included).

    Returns (fills, gross, fees) or None if nothing is affordable.
    Per-share all-in cost at price p is p * (1.01 + 0.07 * (1 - p)).
    """
    fills: list[tuple[float, float]] = []
    remaining = cash
    for price, available in _levels(asks, reverse=False):
        if remaining <= 1e-9:
            break
        per_share = price * (1.01 + 0.07 * (1.0 - price))
        quantity = min(available, remaining / per_share)
        if quantity <= 1e-12:
            continue
        fills.append((price, quantity))
        remaining -= quantity * per_share
    if not fills:
        return None
    gross = sum(price * qty for price, qty in fills)
    return fills, gross, _fees(fills)


def _sell_proceeds(bids: Any, shares: float, *, allow_partial: bool = False):
    """Walk the bid book selling `shares`.

    Returns (gross, fees, filled_shares) or None when nothing can be sold
    (or, with allow_partial=False, when the full amount cannot be sold).
    """
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, available in _levels(bids, reverse=True):
        if remaining <= 1e-9:
            break
        quantity = min(remaining, available)
        fills.append((price, quantity))
        remaining -= quantity
    filled = shares - remaining
    if filled <= 1e-12:
        return None
    if remaining > 1e-9 and not allow_partial:
        return None
    gross = sum(price * qty for price, qty in fills)
    return gross, _fees(fills), filled


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class VolumeBot:
    """Stateful /decide strategy. One instance serves the whole run."""

    def __init__(self) -> None:
        self.market_id: str | None = None
        self.position_was_open = False
        self.completed_cycles = 0
        self.last_flat_at = -math.inf
        self.last_buy_attempt_at = -math.inf
        self.position_opened_at = -math.inf
        # Online BTC volatility estimation from reference observations.
        self._last_ref_ts: float | None = None
        self._last_ref_mid: float | None = None
        self._var_rate_ewma: float | None = None
        self._var_samples = 0

    # -- volatility ------------------------------------------------------
    def _update_vol(self, observed_at: float, mid: float) -> None:
        if self._last_ref_ts is not None and self._last_ref_mid is not None:
            dt = observed_at - self._last_ref_ts
            if 0.2 <= dt <= 20.0 and observed_at > self._last_ref_ts:
                r = mid - self._last_ref_mid
                inst = (r * r) / dt
                if self._var_rate_ewma is None:
                    self._var_rate_ewma = inst
                else:
                    a = SIGMA_EWMA_ALPHA
                    self._var_rate_ewma = (1.0 - a) * self._var_rate_ewma + a * inst
                self._var_samples += 1
        if self._last_ref_ts is None or observed_at > self._last_ref_ts:
            self._last_ref_ts = observed_at
            self._last_ref_mid = mid

    def _sigma(self) -> float:
        if self._var_rate_ewma is not None and self._var_samples >= 8:
            sigma = math.sqrt(max(self._var_rate_ewma, 1e-9))
        else:
            sigma = SIGMA_DEFAULT_PER_SQRT_S
        return min(max(sigma, SIGMA_MIN), SIGMA_MAX)

    # -- main entry --------------------------------------------------------
    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._decide(observation)
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
            return {"action": "HOLD"}

    def _decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        market = observation["market"]
        account = observation["account"]
        books = observation["books"]
        now = float(observation["timestamp"])
        seconds = float(market["secondsToClose"])
        market_id = str(market["id"])
        position = account.get("position")

        if market_id != self.market_id:
            self.market_id = market_id
            self.position_was_open = False
            self.completed_cycles = 0
            self.last_flat_at = -math.inf
            self.last_buy_attempt_at = -math.inf
            self.position_opened_at = -math.inf

        if position is None and self.position_was_open:
            self.completed_cycles += 1
            self.last_flat_at = now
        if position is not None and not self.position_was_open:
            self.position_opened_at = now
        self.position_was_open = position is not None

        # Reference validation + fair values (also feeds the vol estimator).
        fair = self._fair_values(observation, now, seconds)

        if position is not None:
            return self._exit_decision(observation, position, books, now, seconds, fair)
        return self._entry_decision(observation, books, now, seconds, fair)

    # -- fair value --------------------------------------------------------
    def _fair_values(self, observation, now, seconds):
        """Return (fair_yes, fair_no) or None when the reference is unusable."""
        reference = observation.get("reference")
        if not isinstance(reference, dict):
            return None
        if bool(reference.get("targetProvisional", True)):
            return None
        try:
            observed_at = float(reference["observedAt"])
            target_observed_at = float(reference["targetObservedAt"])
            mid = float(reference["btcMidUsd"])
            target = float(reference["openingTargetUsd"])
        except (KeyError, TypeError, ValueError):
            return None
        if target_observed_at > now + 1e-9:
            return None  # future-stamped target: not yet observable
        if now - observed_at > REF_MAX_AGE_SECONDS:
            return None  # stale midpoint
        self._update_vol(observed_at, mid)
        tau = max(1.0, seconds)
        gap = mid - target
        sigma = self._sigma()
        z = gap / (sigma * math.sqrt(tau))
        fair_yes = _phi(z * Z_SHRINK)
        # Projected fair after HOLD_PROJ_SECONDS of tau decay, assuming the
        # gap persists in expectation (BTC is a martingale): the favourite's
        # fair value rises mechanically as tau shrinks; the underdog's falls.
        tau_proj = max(tau - HOLD_PROJ_SECONDS, 12.0)
        z_proj = gap / (sigma * math.sqrt(tau_proj))
        proj_yes = _phi(z_proj * Z_SHRINK)
        return fair_yes, 1.0 - fair_yes, proj_yes, 1.0 - proj_yes

    # -- exits ---------------------------------------------------------------
    def _exit_decision(self, observation, position, books, now, seconds, fair):
        # A position belonging to another (already closed) market cannot be
        # priced against this market's book; leave it to settlement.
        if str(position.get("market_id")) != str(observation["market"]["id"]):
            return {"action": "HOLD"}
        side = str(position["outcome"])
        shares = float(position["shares"])
        book = books.get(side, {}) if isinstance(books, dict) else {}
        bids = book.get("bids", [])

        paid = float(position["buy_gross_usd"]) + float(position["buy_fees_usd"])
        received = float(position.get("sell_gross_usd", 0)) - float(position.get("sell_fees_usd", 0))

        full = _sell_proceeds(bids, shares)
        age = now - self.position_opened_at if self.position_opened_at > -math.inf else 0.0
        fair_side = None
        if fair is not None:
            fair_side = fair[0] if side == "YES" else fair[1]

        if full is not None:
            sell_gross, sell_fees, _ = full
            pnl = received + sell_gross - sell_fees - paid
            if pnl >= TAKE_PROFIT_USD - 1e-9:
                return {"action": "SELL"}
            if fair_side is not None:
                bid_levels = _levels(bids, reverse=True)
                if bid_levels and bid_levels[0][0] >= fair_side - CONVERGED_BID_EPS \
                        and pnl >= CONVERGED_MIN_PNL_USD - 1e-9:
                    return {"action": "SELL"}  # convergence captured
            if pnl <= -STOP_LOSS_USD + 1e-9:
                return {"action": "SELL"}
            if age >= MAX_HOLD_SECONDS and pnl >= -MAX_HOLD_MAX_LOSS_USD - 1e-9:
                return {"action": "SELL"}
            if seconds <= FORCED_EXIT_TAU and pnl >= -FORCED_EXIT_MAX_LOSS_USD - 1e-9:
                return {"action": "SELL"}
            if seconds <= HARD_EXIT_TAU:
                return {"action": "SELL"}
            return {"action": "HOLD"}

        # Full exit not currently executable. Near expiry, recovering cash
        # with a partial sale beats letting the whole cycle settle (which
        # would void the cycle's volume anyway and lock the cash).
        if seconds <= HARD_EXIT_TAU:
            partial = _sell_proceeds(bids, shares, allow_partial=True)
            if partial is not None:
                return {"action": "SELL"}
        return {"action": "HOLD"}

    # -- entries -------------------------------------------------------------
    def _entry_decision(self, observation, books, now, seconds, fair):
        if fair is None:
            return {"action": "HOLD"}
        if self.completed_cycles >= MAX_CYCLES_PER_MARKET:
            return {"action": "HOLD"}
        if now - self.last_flat_at < REENTRY_COOLDOWN_SECONDS - 1e-9:
            return {"action": "HOLD"}
        if now - self.last_buy_attempt_at < REENTRY_COOLDOWN_SECONDS - 1e-9:
            return {"action": "HOLD"}
        if seconds <= ENTRY_TAU_MIN:
            return {"action": "HOLD"}
        if seconds > ENTRY_TAU_MAX:
            return {"action": "HOLD"}

        account = observation["account"]
        cash = float(account["cashUsd"])
        ceiling = min(cash, float(observation["rules"]["maximumBuyCashUsd"]))
        if ceiling <= 0:
            return {"action": "HOLD"}

        best: tuple[float, str] | None = None  # (est_pnl, side)
        for side, fair_side, proj_side in (("YES", fair[0], fair[2]), ("NO", fair[1], fair[3])):
            book = books.get(side, {}) if isinstance(books, dict) else {}
            asks = book.get("asks", [])
            bids = book.get("bids", [])
            ask_levels = _levels(asks, reverse=False)
            if not ask_levels:
                continue
            best_ask = ask_levels[0][0]
            if best_ask > MAX_ASK + 1e-9:
                continue
            edge = fair_side - best_ask
            if edge < MIN_EDGE - 1e-9:
                continue
            bid_levels = _levels(bids, reverse=True)
            if not bid_levels:
                continue  # require an executable exit side for entries
            best_bid = bid_levels[0][0]
            if best_ask - best_bid > MAX_SPREAD + 1e-9:
                continue

            bought = _buy_with_cash(asks, ceiling)
            if bought is None:
                continue
            fills, gross, fees = bought
            shares = sum(qty for _, qty in fills)
            min_order = float(book.get("minOrderSize", 0) or 0)
            if shares < min_order - 1e-9:
                continue
            spend = gross + fees
            if spend > ceiling + 1e-6:
                continue

            instant = _sell_proceeds(bids, shares)
            if instant is None:
                continue
            inst_gross, inst_fees, _ = instant
            instant_pnl = inst_gross - inst_fees - spend
            if instant_pnl < -MAX_INSTANT_LOSS_USD - 1e-9:
                continue

            # Expected exit: while we hold, tau decays and the side's fair
            # value drifts toward the projected fair (up for the favourite,
            # down for the underdog). We expect the bid to close a fraction
            # of the gap between the current mid and the projected fair.
            mid = 0.5 * (best_ask + best_bid)
            est_exit = best_bid + CONV_FRAC * max(0.0, proj_side - mid)
            est_exit = min(est_exit, 0.999)
            est_proceeds = shares * est_exit * (1.0 - (0.01 + 0.07 * (1.0 - est_exit)))
            est_pnl = est_proceeds - spend
            if est_pnl < MIN_EST_PNL_USD - 1e-9:
                continue
            if best is None or est_pnl > best[0]:
                best = (est_pnl, side)

        if best is None:
            return {"action": "HOLD"}
        self.last_buy_attempt_at = now
        return {"action": "BUY", "outcome": best[1], "maxCashUsd": round(ceiling, 6)}


BOT = VolumeBot()


def decide(observation: dict[str, Any]) -> dict[str, Any]:
    return BOT.decide(observation)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            response = BOT.decide(json.loads(self.rfile.read(length)))
            body = json.dumps(response, separators=(",", ":")).encode()
        except Exception:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
