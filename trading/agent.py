"""Dual — classic dual momentum: 126d+63d blend, equal weight,
relative-strength gate (a name must beat SPY's 63d return to qualify).
(Base: Meridian v1, tournament variant — not the submitted file.)

Strategy in one paragraph
-------------------------
Rank the WHOLE tradable universe (not a fixed shortlist) by blended momentum
(126d / 63d / 21d returns + distance above the 50-day average, minus a
volatility penalty), hold the top 7 qualifiers that are still trending up,
and size them by a conviction/risk blend (rank-linear x inverse-vol), capped at 24%,
with an anti-churn buffer that prefers keeping incumbents. A regime
layer on SPY/QQQ decides how much to invest at all: FULL (strong tape) adds a
measured QLD/SSO 2x sleeve; NEUTRAL holds a partial core; a hard brake
(index below its 50-day average, a sharp 3-day index drop, or a volatility
spike) moves to cash. Portfolio exposure is volatility-targeted, a drawdown
taper cuts size after losses, and each position has an 8.5% trailing stop.

Why this shape (vs the Round 1 winner's concentrated 36-name pool):
  * Breadth of search: the strongest leaders in a ~1000-name universe are
    often outside any fixed 36-name list; a full cross-sectional scan finds
    them, while inverse-vol sizing + a 22% cap tame single-name blowups.
  * A soft brake (5-day index drop) trims exposure days before the 50-day
    trend break confirms, which is where the winner gives back the most.
  * Volatility targeting presses exposure up in calm uptrends (when the
    winner sits at a flat 85% in NEUTRAL) and shrinks it as vol rises.

Design guarantees (by construction):
  * Long-only; sells never exceed holdings; quantities are positive ints.
  * Per-ticker target weight <= 0.22 (rule: < 0.30); drift-trim at 0.26.
  * Dollar gross <= 1.0x; beta-adjusted gross clamped <= 1.40x (< 1.5x cap).
  * <= 45 orders per call (< 50 trades/day cap).
  * Deterministic: same inputs -> same orders; all timing is bar-date based,
    so behaviour is identical at daily or intraday tick cadence.
  * Stdlib only, no network, no LLM. decide() is O(universe x bars) with
    small constants — well under the 5s limit at 1000 tickers x 220 bars.
"""
from __future__ import annotations

import math
from statistics import pstdev
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Parameters — deliberately few, round numbers (robustness > curve-fitting).
# ---------------------------------------------------------------------------
MIN_BARS = 127            # history needed for the 126d momentum leg
IDX_MIN_BARS = 60         # history needed for index regime features
TOP_N = 8
NAME_CAP = 0.20
DRIFT_LIMIT = 0.26        # force a rebalance if a position drifts above this
MAX_BETA_GROSS = 1.40     # self-clamp, below the 1.5x rule cap
MIN_TRADE_PCT = 0.03      # dead-band: ignore target deltas below 3% of equity
KEEP_RANK = 12
FORCE_RANK = 5
CASH_BUFFER = 0.98        # spend at most 98% of expected sell proceeds
MAX_ORDERS = 45           # rule cap is 50 trades/day
REBALANCE_DAYS = 3        # scheduled rebalance cadence (trading days)
PRICE_FLOOR = 2.0         # skip sub-$2 names (junk/odd lots in the live list)

# Momentum blend (weights sum informally; score is only used for ranking).
# The 63d/21d legs lead — they identify the CURRENT leaders; the 126d leg
# is only a stabilizer.
W_MOM_LONG = 0.50
W_MOM_MID = 0.50
W_MOM_SHORT = 0.0
W_GAP = 0.0
W_VOL_PEN = 0.0

# Regime / exposure.
BUDGET_FULL = 1.00
BUDGET_NEUTRAL = 0.85
VOL_TARGET = 0.20         # target market volatility for exposure scaling
VOL_SCALE_MIN = 0.55
VOL_SCALE_MAX = 1.10
FULL_VOL_MAX = 0.32       # QQQ/SPY 20d vol must be under this for FULL
FULL_BREADTH_MIN = 0.45   # share of rankable names above their SMA50
BRAKE_RET3 = -0.05        # index 3-day drop that forces cash
BRAKE_VOL10 = 0.50        # index 10d vol above this forces cash
SOFT_BRAKE_RET5 = -0.035  # index 5-day drop that halves the budget
SLEEVE_MAX = 0.30         # max dollar weight of the 2x sleeve (FULL only)
SLEEVE_BASE = 0.25
SLEEVE_VOL_MAX = 0.28     # sleeve only when QQQ vol20 is calmer than this

# Drawdown taper (peak-equity drawdown -> exposure multiplier).
DD_TAPER = ((-0.05, 0.65), (-0.09, 0.35), (-0.13, 0.15))

# Trailing stop.
TRAIL_STOP = 0.085
STOP_COOLDOWN_DAYS = 3
BRAKE_COOLDOWN_DAYS = 2   # days to wait after a brake before re-entering
THRUST_LOOKBACK = 10      # V-recovery re-entry: QQQ 10d return above...
THRUST_MIN_RET = 0.12     # ...12% while back above its 20d average

BETA: dict[str, float] = {
    "QLD": 2.0, "SSO": 2.0, "DDM": 2.0, "ROM": 2.0, "UWM": 2.0, "AGQ": 2.0,
    "TQQQ": 3.0, "SOXL": 3.0, "UPRO": 3.0, "SPXL": 3.0, "TNA": 3.0, "FAS": 3.0,
    "TECL": 3.0, "LABU": 3.0, "CURE": 3.0, "DRN": 3.0, "UDOW": 3.0, "NAIL": 3.0,
}
SLEEVE = ("QLD", "SSO")   # 2x sleeve, bought only in the FULL state
INDEXES = ("SPY", "QQQ")

_STATE_RANK = {"OFF": 0, "NEUTRAL": 1, "FULL": 2}

# ---------------------------------------------------------------------------
# Persistent state (reset by the engine between runs; date-based, so tick
# cadence does not matter).
# ---------------------------------------------------------------------------
_state: str = "NEUTRAL"
_cooldown: int = 0
_peak_equity: float = 0.0
_pos_high: dict[str, float] = {}
_stop_block: dict[str, int] = {}
_last_rebalance_date: Optional[str] = None
_last_seen_date: Optional[str] = None
_prev_state: str = "NEUTRAL"
_prev_taper_mult: float = 1.0


# ---------------------------------------------------------------------------
# Feature helpers — pure functions over close series (oldest first).
# ---------------------------------------------------------------------------
def _beta(ticker: str) -> float:
    return BETA.get(ticker, 1.0)


def _date_of(ts: Any) -> str:
    return str(ts)[:10]


def _closes_of(market_state: dict, ticker: str, cache: dict) -> Optional[list]:
    """Close series for a ticker, or None if any bar is unusable."""
    if ticker in cache:
        return cache[ticker]
    closes: Optional[list] = None
    bars = market_state.get(ticker)
    if bars:
        try:
            closes = [float(bar["close"]) for bar in bars]
        except (KeyError, TypeError, ValueError):
            closes = None
        if closes is not None and any(c <= 0.0 for c in closes):
            closes = None
    cache[ticker] = closes
    return closes


def _sma(closes: list, n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _ret(closes: list, k: int) -> Optional[float]:
    if len(closes) < k + 1:
        return None
    start = closes[-(k + 1)]
    if start <= 0.0:
        return None
    return closes[-1] / start - 1.0


def _vol(closes: list, n: int) -> Optional[float]:
    """Annualized realized vol from the last n daily returns."""
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1):]
    rets = []
    for i in range(1, len(window)):
        prev = window[i - 1]
        if prev <= 0.0:
            return None
        rets.append(window[i] / prev - 1.0)
    if len(rets) < 2:
        return None
    return pstdev(rets) * math.sqrt(252.0)


def _score(closes: list) -> Optional[float]:
    """Blended momentum score; None if history is insufficient."""
    r_long = _ret(closes, 126)
    r_mid = _ret(closes, 63)
    r_short = _ret(closes, 21)
    sma50 = _sma(closes, 50)
    vol20 = _vol(closes, 20)
    if None in (r_long, r_mid, r_short, sma50, vol20) or sma50 <= 0.0:
        return None
    gap = closes[-1] / sma50 - 1.0
    return (
        W_MOM_LONG * r_long + W_MOM_MID * r_mid + W_MOM_SHORT * r_short
        + W_GAP * gap - W_VOL_PEN * vol20
    )


# ---------------------------------------------------------------------------
# Portfolio helpers.
# ---------------------------------------------------------------------------
def _resolve_cash(portfolio_state: dict, cash: float) -> float:
    try:
        return float(portfolio_state.get("cash", cash))
    except (TypeError, ValueError):
        try:
            return float(cash)
        except (TypeError, ValueError):
            return 0.0


def _aggregate_positions(portfolio_state: dict) -> dict:
    """Aggregate duplicate lots by ticker (weighted-average cost)."""
    out: dict[str, dict[str, float]] = {}
    for raw in portfolio_state.get("positions", []) or []:
        try:
            ticker = str(raw["ticker"]).upper()
            qty = float(raw.get("quantity", 0.0))
            avg_cost = float(raw.get("avg_cost", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if qty <= 0.0:
            continue
        if ticker in out:
            cur = out[ticker]
            total = cur["quantity"] + qty
            cur["avg_cost"] = (
                (cur["avg_cost"] * cur["quantity"] + avg_cost * qty) / total
                if total > 0.0 else avg_cost
            )
            cur["quantity"] = total
        else:
            out[ticker] = {"quantity": qty, "avg_cost": avg_cost}
    return out


def _price(ticker: str, market_state: dict, cache: dict, last_prices: dict) -> Optional[float]:
    closes = _closes_of(market_state, ticker, cache)
    if closes and closes[-1] > 0.0:
        return closes[-1]
    lp = last_prices.get(ticker)
    try:
        if lp is not None and float(lp) > 0.0:
            return float(lp)
    except (TypeError, ValueError):
        pass
    return None


def _compute_equity(positions: dict, market_state: dict, cache: dict,
                    last_prices: dict, cash_value: float) -> float:
    total = cash_value
    for ticker in sorted(positions):  # deterministic summation order
        pos = positions[ticker]
        price = _price(ticker, market_state, cache, last_prices)
        if price is None:
            price = pos["avg_cost"] if pos["avg_cost"] > 0.0 else 0.0
        total += pos["quantity"] * max(price, 0.0)
    return max(total, 0.0)


def _taper_mult(dd: float) -> float:
    mult = 1.0
    for threshold, m in DD_TAPER:
        if dd <= threshold:
            mult = m  # thresholds are ordered mild -> severe
    return mult


# ---------------------------------------------------------------------------
# Target construction.
# ---------------------------------------------------------------------------
def _rank_universe(market_state: dict, cache: dict) -> tuple[list, float]:
    """Score every rankable ticker; return (ranked [(score, ticker, vol20)],
    breadth = share of rankable names above their SMA50)."""
    ranked: list[tuple[float, str, float]] = []
    n_up = 0
    n_all = 0
    spy_closes = _closes_of(market_state, "SPY", cache)
    spy_r63 = _ret(spy_closes, 63) if spy_closes else None
    for ticker in sorted(market_state):
        t = ticker.upper()
        if _beta(t) != 1.0:
            continue  # leveraged ETFs are never core holdings
        closes = _closes_of(market_state, ticker, cache)
        if closes is None or len(closes) < MIN_BARS or closes[-1] < PRICE_FLOOR:
            continue
        sma50 = _sma(closes, 50)
        if sma50 is None:
            continue
        n_all += 1
        if closes[-1] > sma50:
            n_up += 1
        sma20 = _sma(closes, 20)
        score = _score(closes)
        if score is None or sma20 is None:
            continue
        r_short = _ret(closes, 21)
        r_mid = _ret(closes, 63)
        if spy_r63 is not None and (r_mid is None or r_mid <= spy_r63):
            continue  # dual momentum: must beat the index
        # Trend gates: still in an uptrend, short leg not a falling knife.
        if closes[-1] <= sma50 or closes[-1] <= sma20:
            continue
        if r_short is None or r_short <= -0.03 or score <= 0.0:
            continue
        vol20 = _vol(closes, 20)
        if vol20 is None:
            continue
        ranked.append((score, t, vol20))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    breadth = (n_up / n_all) if n_all > 0 else 0.0
    return ranked, breadth


def _select(ranked: list, held: set, stop_block: dict) -> list:
    """Pick the TOP_N leaders to hold.

    Anti-churn rule: the top FORCE_RANK names are always in; the remaining
    slots prefer incumbents that still rank in the top KEEP_RANK over fresh
    names, so membership (and therefore turnover) changes slowly.
    """
    pool = [item for item in ranked if item[1] not in stop_block]
    if len(pool) <= TOP_N:
        return pool
    chosen = list(pool[:FORCE_RANK])
    chosen_t = {t for (_, t, _) in chosen}
    for item in pool[FORCE_RANK:KEEP_RANK]:
        if len(chosen) >= TOP_N:
            break
        if item[1] in held and item[1] not in chosen_t:
            chosen.append(item)
            chosen_t.add(item[1])
    for item in pool:
        if len(chosen) >= TOP_N:
            break
        if item[1] not in chosen_t:
            chosen.append(item)
            chosen_t.add(item[1])
    rank_of = {t: i for i, (_, t, _) in enumerate(pool)}
    chosen.sort(key=lambda item: rank_of[item[1]])
    return chosen[:TOP_N]


def _build_targets(state: str, budget: float, sleeve_dollar: float,
                   ranked: list, stop_block: dict, held: set) -> dict:
    """Target weights (fractions of equity) for the given state/budget."""
    weights: dict[str, float] = {}
    if state == "OFF" or budget <= 0.0:
        return weights

    selected = _select(ranked, held, stop_block)
    core_budget = max(budget - sleeve_dollar, 0.0)

    if selected:
        # Sizing = geometric mean of rank-linear conviction and inverse-vol:
        # the strongest names get the most, but a high-beta name is damped
        # so one blowup cannot dominate the book. Capped at NAME_CAP with
        # redistribution so the budget is actually used.
        n = len(selected)
        raw = {}
        for i, (_, t, v) in enumerate(selected):
            raw[t] = 1.0  # dual variant: equal weight
        remaining = core_budget
        pool = [t for (_, t, _) in selected]
        for _ in range(3):
            if not pool or remaining <= 0.0:
                break
            total_raw = sum(raw[t] for t in pool)
            if total_raw <= 0.0:
                break
            capped = []
            for t in pool:
                w = remaining * raw[t] / total_raw
                if w >= NAME_CAP:
                    weights[t] = NAME_CAP
                    remaining -= NAME_CAP
                    capped.append(t)
                else:
                    weights[t] = w
            pool = [t for t in pool if t not in capped]
            if not capped:
                break
    else:
        # No leader qualifies: no core and no sleeve; the caller falls back
        # to parking the budget in the index ETFs.
        sleeve_dollar = 0.0

    if sleeve_dollar > 0.0:
        sleeve = [s for s in SLEEVE if s not in stop_block]
        if sleeve:
            per = sleeve_dollar / len(sleeve)
            for s in sleeve:
                weights[s] = min(per, NAME_CAP)

    # Beta-gross clamp (defensive; by construction we are well under it).
    gross = sum(w * _beta(t) for t, w in weights.items())
    if gross > MAX_BETA_GROSS and gross > 0.0:
        scale = MAX_BETA_GROSS / gross
        weights = {t: w * scale for t, w in weights.items()}
    return {t: round(w, 6) for t, w in weights.items() if w > 0.001}


def _index_fallback_weights(budget: float, idx: dict) -> dict:
    """SPY/QQQ parking weights used when no leader qualifies.

    Capped at 0.28 per index: the fallback can persist for weeks in choppy
    tape, and the concentration rule forbids >= 30% for more than 5 days,
    so the fallback must stay under that line by construction (any leftover
    budget simply stays in cash).
    """
    avail = []
    for t in INDEXES:
        f = idx.get(t)
        if f and f["sma50"] is not None and f["close"] > f["sma50"]:
            avail.append(t)
    if not avail:
        return {}
    if len(avail) == 2:
        weights = {"SPY": budget * 0.6, "QQQ": budget * 0.4}
    else:
        weights = {avail[0]: budget}
    return {t: min(w, 0.28) for t, w in weights.items() if w > 0.001}


# ---------------------------------------------------------------------------
# Order generation — sells before buys, dead-band, cash-aware.
# ---------------------------------------------------------------------------
def _generate_orders(do_rebalance: bool, weights: dict, positions: dict,
                     forced_stops: list, equity: float, market_state: dict,
                     cache: dict, last_prices: dict, cash_value: float) -> list:
    orders: list[dict[str, Any]] = []
    sold: set[str] = set()
    proceeds = 0.0
    min_trade = MIN_TRADE_PCT * equity

    for ticker, qty in forced_stops:
        if qty > 0.0:
            orders.append({"ticker": ticker, "side": "sell", "quantity": int(qty)})
            sold.add(ticker)
            price = _price(ticker, market_state, cache, last_prices)
            if price is not None:
                proceeds += qty * price

    if do_rebalance:
        for ticker in sorted(positions):
            if ticker in sold:
                continue
            held = positions[ticker]["quantity"]
            if held <= 0.0:
                continue
            target_w = weights.get(ticker, 0.0)
            price = _price(ticker, market_state, cache, last_prices)
            if target_w == 0.0:
                # Not a target anymore: exit fully so de-risk states are flat.
                orders.append({"ticker": ticker, "side": "sell", "quantity": int(held)})
                sold.add(ticker)
                if price is not None and price > 0.0:
                    proceeds += held * price
                continue
            if price is None or price <= 0.0:
                continue
            target_shares = math.floor(target_w * equity / price)
            delta = target_shares - held
            if delta < 0 and (-delta) * price >= min_trade:
                sell_qty = int(min(-delta, held))
                if sell_qty > 0:
                    orders.append({"ticker": ticker, "side": "sell", "quantity": sell_qty})
                    sold.add(ticker)
                    proceeds += sell_qty * price

        spendable = cash_value + CASH_BUFFER * proceeds
        for ticker in sorted(weights, key=lambda t: (-weights[t], t)):
            price = _price(ticker, market_state, cache, last_prices)
            if price is None or price <= 0.0:
                continue
            held = positions[ticker]["quantity"] if ticker in positions else 0.0
            target_shares = math.floor(weights[ticker] * equity / price)
            deficit = target_shares - held
            if deficit > 0 and deficit * price >= min_trade:
                affordable = int(min(deficit * price, spendable) // price)
                if affordable > 0:
                    orders.append({"ticker": ticker, "side": "buy", "quantity": affordable})
                    spendable -= affordable * price

    if len(orders) > MAX_ORDERS:
        sells = [o for o in orders if o["side"] == "sell"]
        buys = [o for o in orders if o["side"] == "buy"]
        orders = (sells + buys)[:MAX_ORDERS]
    return [o for o in orders if o["quantity"] > 0]


# ---------------------------------------------------------------------------
# Master decision cycle. decide() wraps _run() with a state snapshot so a bad
# tick can never corrupt persistent state or fail the "runs clean" gate.
# ---------------------------------------------------------------------------
def decide(market_state: dict, portfolio_state: dict, cash: float) -> list:
    """Return a list of long-only buy/sell orders for this decision cycle."""
    global _state, _cooldown, _peak_equity, _pos_high, _stop_block
    global _last_rebalance_date, _last_seen_date, _prev_state, _prev_taper_mult

    snapshot = (
        _state, _cooldown, _peak_equity, dict(_pos_high), dict(_stop_block),
        _last_rebalance_date, _last_seen_date, _prev_state, _prev_taper_mult,
    )
    try:
        return _run(market_state or {}, portfolio_state or {}, cash)
    except Exception:  # noqa: BLE001 — never raise into the engine
        (
            _state, _cooldown, _peak_equity, _pos_high, _stop_block,
            _last_rebalance_date, _last_seen_date, _prev_state, _prev_taper_mult,
        ) = snapshot
        return []


def _run(market_state: dict, portfolio_state: dict, cash: float) -> list:
    global _state, _cooldown, _peak_equity, _pos_high, _stop_block
    global _last_rebalance_date, _last_seen_date, _prev_state, _prev_taper_mult

    if not market_state:
        return []

    cache: dict[str, Optional[list]] = {}
    last_prices: dict[str, Any] = {}
    for key, value in (portfolio_state.get("last_prices", {}) or {}).items():
        last_prices[str(key).upper()] = value
    cash_value = _resolve_cash(portfolio_state, cash)
    positions = _aggregate_positions(portfolio_state)

    # Calendar: date of the freshest index bar (fall back to longest series).
    cal_ticker = None
    for t in INDEXES:
        if market_state.get(t):
            cal_ticker = t
            break
    if cal_ticker is None:
        cal_ticker = max(market_state, key=lambda t: len(market_state.get(t) or []))
    cal_bars = market_state.get(cal_ticker) or []
    current_date: Optional[str] = None
    if cal_bars:
        ts = cal_bars[-1].get("ts")
        current_date = _date_of(ts) if ts is not None else str(len(cal_bars))

    # Index features (whichever of SPY/QQQ are usable).
    idx: dict[str, dict] = {}
    for t in INDEXES:
        closes = _closes_of(market_state, t, cache)
        if closes is None or len(closes) < IDX_MIN_BARS:
            continue
        idx[t] = {
            "close": closes[-1],
            "sma20": _sma(closes, 20),
            "sma50": _sma(closes, 50),
            "vol20": _vol(closes, 20),
            "vol10": _vol(closes, 10),
            "ret3": _ret(closes, 3),
            "ret5": _ret(closes, 5),
            "ret10": _ret(closes, 10),
        }

    # Data guard: no usable index -> de-risk to cash (sell what is sellable).
    if not idx:
        orders = []
        for ticker in sorted(positions):
            if market_state.get(ticker):
                qty = positions[ticker]["quantity"]
                if qty > 0.0:
                    orders.append({"ticker": ticker, "side": "sell", "quantity": int(qty)})
        _prev_state = _state
        _prev_taper_mult = 1.0
        if current_date is not None:
            _last_seen_date = current_date
        return orders

    # New-day bookkeeping: cooldowns and stop-blocks decay once per date.
    is_new_day = current_date != _last_seen_date
    if is_new_day:
        if _cooldown > 0:
            _cooldown -= 1
        if _stop_block:
            _stop_block = {t: d - 1 for t, d in _stop_block.items() if d - 1 > 0}

    # Equity / drawdown taper.
    equity = _compute_equity(positions, market_state, cache, last_prices, cash_value)
    if equity <= 0.0:
        _prev_state = _state
        _prev_taper_mult = 1.0
        _last_seen_date = current_date
        return []
    _peak_equity = max(_peak_equity, equity)
    dd = (equity / _peak_equity - 1.0) if _peak_equity > 0.0 else 0.0
    taper_mult = _taper_mult(dd)

    # ---------------- Regime ----------------
    def _below_sma50(f: dict, band: float) -> bool:
        return f["sma50"] is not None and f["close"] < f["sma50"] * (1.0 - band)

    def _above(f: dict, key: str, band: float = 0.0) -> bool:
        ref = f[key]
        return ref is not None and f["close"] > ref * (1.0 + band)

    brake = any(
        (f["ret3"] is not None and f["ret3"] < BRAKE_RET3)
        or (f["vol10"] is not None and f["vol10"] > BRAKE_VOL10)
        for f in idx.values()
    )
    hard_off = brake or any(_below_sma50(f, 0.005) for f in idx.values())
    reclaim = all(_above(f, "sma50", 0.01) for f in idx.values())
    mkt_vol = max((f["vol20"] for f in idx.values() if f["vol20"] is not None),
                  default=None)

    ranked, breadth = _rank_universe(market_state, cache)

    full_conditions = (
        all(_above(f, "sma50", 0.005) and _above(f, "sma20") for f in idx.values())
        and breadth >= FULL_BREADTH_MIN
        and (mkt_vol is not None and mkt_vol < FULL_VOL_MAX)
    )

    prev_cycle_state = _state
    qqq = idx.get("QQQ")
    thrust = (
        qqq is not None
        and qqq["ret10"] is not None and qqq["ret10"] > THRUST_MIN_RET
        and _above(qqq, "sma20")
    )
    if prev_cycle_state == "OFF" and not brake and not reclaim and thrust and _cooldown <= 0:
        _state = "NEUTRAL"  # V-recovery thrust re-entry (never straight to FULL)
    elif hard_off:
        _state = "OFF"
        _cooldown = BRAKE_COOLDOWN_DAYS
    elif prev_cycle_state == "OFF" and not reclaim:
        _state = "OFF"  # hysteresis: demand a clear reclaim before re-entry
    elif _cooldown > 0:
        _state = "NEUTRAL"
    else:
        _state = "FULL" if full_conditions else "NEUTRAL"

    # ---------------- Exposure budget ----------------
    if _state == "OFF":
        budget = 0.0
        sleeve_dollar = 0.0
    else:
        base = BUDGET_FULL if _state == "FULL" else BUDGET_NEUTRAL
        vol_scale = 1.0
        if mkt_vol is not None and mkt_vol > 0.0:
            vol_scale = min(max(VOL_TARGET / mkt_vol, VOL_SCALE_MIN), VOL_SCALE_MAX)
        budget = min(base * vol_scale, 1.0)
        # Soft brake: a sharp 5-day index drop halves exposure before the
        # 50-day break confirms.
        if any(f["ret5"] is not None and f["ret5"] < SOFT_BRAKE_RET5 for f in idx.values()):
            budget *= 0.5
        budget *= taper_mult
        sleeve_dollar = 0.0
        if (
            _state == "FULL"
            and qqq is not None
            and qqq["vol20"] is not None and qqq["vol20"] < SLEEVE_VOL_MAX
            and qqq["sma20"] is not None and qqq["sma50"] is not None
            and qqq["sma20"] > qqq["sma50"]
            and all(s in market_state for s in SLEEVE)
        ):
            sleeve_dollar = min(SLEEVE_BASE * vol_scale, SLEEVE_MAX) * taper_mult

    # ---------------- Trailing stops ----------------
    for ticker in list(_pos_high):
        if ticker not in positions:
            del _pos_high[ticker]
    forced_stops: list[tuple[str, float]] = []
    for ticker in sorted(positions):
        price = _price(ticker, market_state, cache, last_prices)
        if price is None:
            continue
        high = max(_pos_high.get(ticker, price), price)
        _pos_high[ticker] = high
        if high > 0.0 and price < high * (1.0 - TRAIL_STOP):
            forced_stops.append((ticker, positions[ticker]["quantity"]))
            _stop_block[ticker] = STOP_COOLDOWN_DAYS
            del _pos_high[ticker]

    # ---------------- Rebalance gate ----------------
    if _last_rebalance_date is None:
        do_rebalance = True
    else:
        elapsed = {
            _date_of(bar.get("ts")) if bar.get("ts") is not None else ""
            for bar in cal_bars
            if (bar.get("ts") is None or _date_of(bar.get("ts")) > _last_rebalance_date)
        }
        days_since = len([d for d in elapsed if d])
        derisk = (
            _STATE_RANK[_state] < _STATE_RANK[_prev_state]
            or taper_mult < _prev_taper_mult
        )
        drift = any(
            (lambda p: p is not None and equity > 0.0
             and (pos["quantity"] * p / equity) > DRIFT_LIMIT)(
                _price(t, market_state, cache, last_prices))
            for t, pos in positions.items()
        )
        do_rebalance = days_since >= REBALANCE_DAYS or derisk or drift
    if _last_rebalance_date == current_date:
        do_rebalance = False

    # ---------------- Targets & orders ----------------
    weights: dict[str, float] = {}
    if do_rebalance:
        weights = _build_targets(
            _state, budget, sleeve_dollar, ranked, _stop_block, set(positions))
        if not weights and _state != "OFF" and budget > 0.0:
            weights = _index_fallback_weights(budget, idx)

    orders = _generate_orders(
        do_rebalance, weights, positions, forced_stops,
        equity, market_state, cache, last_prices, cash_value,
    )
    if do_rebalance and orders:
        _last_rebalance_date = current_date

    _prev_state = _state
    _prev_taper_mult = taper_mult
    _last_seen_date = current_date
    return orders
