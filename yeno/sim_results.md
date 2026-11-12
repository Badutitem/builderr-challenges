# Simulator results — YeNo volume bot

Simulator: `simulator.py` (synthetic 24h paths; BTC regime-switching random
walk, fair = Phi(gap/(sigma*sqrt(tau))), market-maker books lagging the BTC
reference 1–3 s with probability-dependent spreads ~0.5–2.1¢, depth, quote
noise; bot called 1/s; orders execute on the book 0.5 s later; fees, $5 cap,
min-order rejection, partial sells, cycle accounting and settlement-voids
follow the evaluator contract exactly). Full machine-readable results:
`sim_results.json`.

## Chosen configuration ("mine-loose", baked into yeno_bot.py defaults)

Grid, 12 paths (same seeds for all variants):

| variant | qualified | median cash at cross | median volume | median cycles |
|---|---|---|---|---|
| house-bot-v1 (kit baseline) | 0/12 | — | $327 | 39 |
| mine-default | 5/12 | $12.85 | $988 | 99 |
| mine-tight | 2/12 | $20.38 | $703 | 70 |
| **mine-loose (chosen)** | **10/12** | **$11.30** | **$1,002** | **100** |
| mine-conv-0.45 | 0/12 | — | $542 | 54 |
| mine-conv-0.75 | 7/12 | $7.78 | $1,004 | 101 |
| mine-instant-0.35 | 0/12 | — | $511 | 51 |
| mine-holdproj-20 | 0/12 | — | $563 | 56 |
| mine-holdproj-60 | 6/12 | $8.07 | $1,006 | 101 |
| mine-cycles-8 | 5/12 | $12.85 | $988 | 99 |
| mine-noshrink | 10/12 | $5.36 | $1,005 | 105 |
| mine-shrink-0.7 | 0/12 | — | $267 | 27 |
| loose+shrink-0.92 | 10/12 | $7.61 | $1,007 | 102 |

Confirmation, 30 fresh seeds, chosen config:
**23/30 qualified (76.7%); median cash at crossing $7.58 (min $4.08);
median time to $1,000 = 16.8 h; median 101 cycles; median fees paid $26.19;
0 settled-void cycles in all 30 paths** (the bot never let a position ride
into settlement).

## Adverse-execution stress (execution prices worsened on both legs)

| config | stress | qualified | median volume |
|---|---|---|---|
| mine-loose | +0.5¢ | 1/12 | $697 |
| mine-loose | +1¢ | 0/12 | $446 |
| mine-default | +1¢ | 1/12 | $561 |
| mine-tight | +1¢ | 0/12 | $589 |

All configs degrade sharply under a full +1¢/leg adverse shift (a 2¢
round-trip penalty); for scale, the published house-bot benchmark under its
own 2¢ stress fell to a $385 maximum / $70 median volume. The base simulator
already executes on a book 0.5 s after decision (contract latency is
250 ms), so part of this adversity is priced into the base case.

## Caveats

- The simulator is a model, not the evaluator: real YeNo books, call
  cadence, and fill behaviour will differ. Numbers are for variant
  selection, not a score prediction.
- "Max drawdown" in sim_results.json is measured on the raw cash curve,
  which dips mechanically whenever a position is open (cash deployed);
  it overstates settled-equity drawdown.
- Settlement cash treatment for a position held into market close is an
  assumption (shares pay $1/$0, cycle volume void). The chosen config
  produced zero such events in 42 base paths, so the assumption is
  immaterial to the headline stats.
