"""Run backtests and report results you can't easily fool yourself with.

Three deliberate design choices:

**Statistics before conclusions.** Every result carries n, standard error, and a
t-statistic. A mean R of +0.4 over 12 trades and over 400 trades are different
claims about the world, and reporting only the mean hides which one you have.

**Out-of-sample is the only number that counts.** ``walk_forward`` fits
parameters on one slice and reports performance on the next, which the fit never
saw. In-sample results are printed only so the gap between them is visible.

**A null model.** ``null`` keeps entry timing, stop distance, and target
selection identical and randomises only the directional call. If the real
strategy cannot beat that, the reading adds nothing and whatever performance
exists belongs to the risk geometry.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from itertools import product
from pathlib import Path

import backtrader as bt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataflows import futures as F  # noqa: E402
from dataflows.common import load_config, markdown_table  # noqa: E402
from dataflows.smc import smc_config  # noqa: E402

from .feeds import describe, load  # noqa: E402
from .strategy import SMCStrategy  # noqa: E402


def _params(symbol: str, overrides: dict | None = None) -> dict:
    spec = F.resolve(symbol)
    cfg = smc_config()
    params = dict(
        swing_lookback=cfg["swing_lookback"],
        sweep_return_bars=cfg["sweep_return_bars"],
        mss_max_bars=cfg["mss_max_bars"],
        stop_buffer_ticks=cfg["stop_buffer_ticks"],
        entry_mode=cfg["entry_mode"],
        min_rr=cfg["min_rr"],
        max_trades_per_session=cfg["max_trades_per_session"],
        stop_after_losses=cfg["stop_after_losses"],
        risk_pct=cfg["risk_pct"],
        target_pool=cfg.get("target_pool", "htf"),
        eq_tolerance_ticks=cfg.get("eq_tolerance_ticks", 4.0),
        kill_zones=cfg["kill_zones"],
        session_ranges=cfg.get("session_ranges"),
        cancel_at_zone_end=cfg.get("cancel_at_zone_end", False),
        tick_points=spec.tick_points,
        multiplier=spec.multiplier,
    )
    params.update(overrides or {})
    return params


def run_once(bars: pd.DataFrame, symbol: str, overrides: dict | None = None) -> list[dict]:
    """One pass. Returns the trade log."""
    if bars.empty:
        return []
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=bars))
    cerebro.addstrategy(SMCStrategy, **_params(symbol, overrides))
    cerebro.broker.setcash(100_000)
    strategies = cerebro.run()
    return strategies[0].trade_log


def _cost_in_r(trade: dict, symbol: str) -> float:
    """Round-trip cost expressed in R, so it can be subtracted from the R multiple.

    Costs matter more the tighter the stop: a 20-tick stop on MNQ risks $10 and
    pays ~$1.90 to trade, which is 19% of the risk gone before the market moves.
    Reporting gross R would flatter exactly the tight-stop setups most likely to
    fail in practice.
    """
    spec = F.resolve(symbol)
    risk_dollars = trade["risk_points"] * spec.multiplier
    if risk_dollars <= 0:
        return 0.0
    return spec.round_trip_cost() / risk_dollars


def stats(trades: list[dict], symbol: str) -> dict:
    """Honest summary. Net R, dispersion, and whether n supports any claim."""
    resolved = [t for t in trades if t["outcome"] not in ("unfilled",)]
    if not resolved:
        return {"n": 0, "unfilled": len(trades)}

    net = [t["r_gross"] - _cost_in_r(t, symbol) for t in resolved]
    n = len(net)
    mean = statistics.fmean(net)
    stdev = statistics.stdev(net) if n > 1 else 0.0
    se = stdev / math.sqrt(n) if n > 1 and stdev else 0.0
    # With every trade at exactly -1R (all stopped, no partials) the variance is
    # zero and t explodes to a meaningless magnitude. Suppress it rather than
    # print a number that looks like overwhelming significance.
    usable_t = n >= 5 and stdev > 1e-6
    t_stat = (mean / se) if (se and usable_t) else 0.0

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in net:
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    # How many trades would it take to call this edge real, at the observed size?
    needed = int((2 * stdev / abs(mean)) ** 2) if mean and stdev else None

    return {
        "n": n,
        "unfilled": sum(1 for t in trades if t["outcome"] == "unfilled"),
        "wins": sum(1 for r in net if r > 0),
        "win_rate": sum(1 for r in net if r > 0) / n,
        "mean_r": mean,
        "median_r": statistics.median(net),
        "stdev_r": stdev,
        "se": se,
        "t_stat": t_stat,
        "total_r": sum(net),
        "max_dd_r": max_dd,
        "trades_for_significance": needed,
        "usable_t": usable_t,
        "significant": abs(t_stat) > 2 and n >= 30,
    }


def format_stats(s: dict, label: str = "") -> str:
    if not s.get("n"):
        return f"_{label}: no resolved trades ({s.get('unfilled', 0)} unfilled)._"
    rows = [
        ["Trades (resolved)", str(s["n"])],
        ["Unfilled", str(s["unfilled"])],
        ["Win rate", f"{s['wins']}/{s['n']} ({s['win_rate']*100:.0f}%)"],
        ["**Mean R (net of costs)**", f"**{s['mean_r']:+.3f}**"],
        ["Median R", f"{s['median_r']:+.2f}"],
        ["Std dev", f"{s['stdev_r']:.2f}"],
        ["Standard error", f"{s['se']:.3f}" if s["se"] else "n/a"],
        ["t-statistic", f"{s['t_stat']:+.2f}" if s.get("usable_t") else "n/a — too few trades or no variation"],
        ["Total R", f"{s['total_r']:+.1f}"],
        ["Max drawdown", f"{s['max_dd_r']:.1f}R"],
    ]
    if s["trades_for_significance"]:
        rows.append(["Trades needed for significance", f"~{s['trades_for_significance']:,}"])

    if s["significant"]:
        verdict = "**Statistically distinguishable from zero.**"
    elif s["n"] < 30:
        verdict = (f"**No conclusion possible at n={s['n']}.** Below ~30 trades this is a "
                   "description of what happened, not evidence about what will.")
    else:
        verdict = "**NOT distinguishable from zero.** Consistent with random."
    return f"{('### ' + label) if label else ''}\n\n{markdown_table(['Metric', 'Value'], rows)}\n\n{verdict}"


# ---------------------------------------------------------------------------
# Walk-forward and the null model
# ---------------------------------------------------------------------------

DEFAULT_GRID = {
    "sweep_return_bars": [2, 5, 10],
    "swing_lookback": [2, 3, 5],
    "stop_buffer_ticks": [4, 12, 24],
    "min_rr": [2.0],
}


def walk_forward(bars: pd.DataFrame, symbol: str, folds: int = 4,
                 grid: dict | None = None, partial: bool = False) -> str:
    """Fit on each fold, test on the next. Only the out-of-sample column counts.

    The in-sample column is shown solely so the gap is visible. A large gap is
    the signature of curve-fitting, and it is the single most common way a
    backtest lies — the parameters were chosen *because* they fit that slice.
    """
    grid = grid or DEFAULT_GRID
    sessions = sorted(bars.index.normalize().unique())
    if len(sessions) < folds * 2:
        return (
            f"<error: {len(sessions)} sessions cannot support {folds} folds. "
            f"Walk-forward needs at least {folds * 2}; realistically hundreds.>"
        )

    edges = [sessions[i * len(sessions) // folds] for i in range(folds)] + [sessions[-1]]
    combos = [dict(zip(grid, v)) for v in product(*grid.values())]

    rows, oos_all = [], []
    for i in range(folds - 1):
        is_bars = bars[(bars.index >= edges[i]) & (bars.index < edges[i + 1])]
        oos_bars = bars[(bars.index >= edges[i + 1]) & (bars.index < edges[i + 2])]
        if is_bars.empty or oos_bars.empty:
            continue

        best, best_stats = None, None
        for combo in combos:
            o = {**combo, "partial_at_1r": partial}
            s = stats(run_once(is_bars, symbol, o), symbol)
            if s.get("n", 0) < 3:
                continue
            if best_stats is None or s["mean_r"] > best_stats["mean_r"]:
                best, best_stats = o, s
        if best is None:
            rows.append([f"{i+1}", str(edges[i].date()), "—", "no viable fit", "—", "—"])
            continue

        oos = stats(run_once(oos_bars, symbol, best), symbol)
        # A fold that produced no trades carries no mean to pool. Keeping it out
        # of the average is correct; silently treating it as 0.0 would drag the
        # pooled figure toward zero and read as "no edge" rather than "no data".
        if oos.get("n"):
            oos_all.append(oos)
        params_txt = ", ".join(f"{k}={v}" for k, v in best.items() if k != "partial_at_1r")
        rows.append([
            f"{i+1}", str(edges[i + 1].date()), params_txt,
            f"{best_stats['mean_r']:+.2f} (n={best_stats['n']})",
            f"{oos['mean_r']:+.2f} (n={oos['n']})" if oos.get("n") else "no trades",
            f"{oos['mean_r'] - best_stats['mean_r']:+.2f}" if oos.get("n") else "—",
        ])

    table = markdown_table(
        ["Fold", "OOS starts", "Best in-sample params", "IS mean R", "OOS mean R", "Gap"], rows
    )

    if oos_all:
        total_n = sum(x["n"] for x in oos_all)
        # Weight by trade count, not by fold — a fold with 8 trades says more
        # than one with 1.
        weighted = sum(x["mean_r"] * x["n"] for x in oos_all) / total_n
        summary = (
            f"\n**Pooled out-of-sample**: {total_n} trades across {len(oos_all)} fold(s), "
            f"mean {weighted:+.3f}R per trade.\n"
        )
    else:
        summary = "\n**No out-of-sample trades in any fold.** Nothing to conclude.\n"

    return f"""## Walk-forward — {symbol}

{table}
{summary}
> **Read the Gap column.** Consistently negative means the parameters were fit to
> noise: they described the slice they were chosen on and did not survive contact
> with data they had not seen. That is the expected result for most strategies,
> and finding it here costs nothing.
>
> The in-sample column is not a result. It is the thing being controlled for.
"""


def null_model(bars: pd.DataFrame, symbol: str, trials: int = 20,
               partial: bool = False) -> str:
    """Same setups, coin-flip direction. The bar the real strategy must clear."""
    import random

    real = stats(run_once(bars, symbol, {"partial_at_1r": partial}), symbol)

    means, ns = [], []
    for i in range(trials):
        random.seed(1000 + i)
        s = stats(run_once(bars, symbol, {"partial_at_1r": partial,
                                          "randomize_direction": True}), symbol)
        if s.get("n"):
            means.append(s["mean_r"])
            ns.append(s["n"])
    if not means:
        return "_Null model produced no trades._"

    null_mean = statistics.fmean(means)
    null_sd = statistics.stdev(means) if len(means) > 1 else 0.0
    beat = sum(1 for m in means if real.get("mean_r", 0) > m)

    rows = [
        ["Real strategy mean R", f"{real.get('mean_r', 0):+.3f} (n={real.get('n', 0)})"],
        [f"Null mean R (avg of {len(means)} runs)", f"{null_mean:+.3f}"],
        ["Null spread (sd across runs)", f"{null_sd:.3f}"],
        ["Null range", f"{min(means):+.3f} to {max(means):+.3f}"],
        ["Real beats null in", f"{beat}/{len(means)} runs"],
    ]

    verdict = (
        "**The real strategy is inside the null's range.** Reading the sweep is adding "
        "nothing measurable here — any performance is coming from the stop and target "
        "geometry, which a coin flip gets for free."
        if real.get("mean_r", 0) <= max(means) else
        "**The real strategy sits above every null run.** Weak evidence the directional "
        "read carries information — weak because the sample is small, not because the "
        "test is."
    )

    return f"""## Null model — {symbol}

Entry timing, stop distance, and target selection held identical; only the
long/short call is randomised.

{markdown_table(["Metric", "Value"], rows)}

{verdict}
"""


def report(symbol: str = "NQ", interval: str = "5m", csv: str | None = None,
           partial: bool = False) -> str:
    """Single-pass backtest with full provenance and honest statistics."""
    bars, provenance = load(symbol, interval, csv)
    if bars.empty:
        return f"<error: no data ({provenance})>"

    trades = run_once(bars, symbol, {"partial_at_1r": partial})
    s = stats(trades, symbol)
    spec = F.resolve(symbol)

    detail = ""
    if trades:
        rows = [[
            t["session"], t["zone"], t["direction"].upper(), t["swept"],
            f"{t['entry']:,.2f}", f"{t['stop']:,.2f}", f"{t['target']:,.2f}",
            f"{t['planned_rr']:.2f}", t["outcome"],
            "—" if t["outcome"] == "unfilled" else f"{t['r_gross'] - _cost_in_r(t, symbol):+.2f}",
        ] for t in trades[-25:]]
        detail = markdown_table(
            ["Session", "Zone", "Dir", "Swept", "Entry", "Stop", "Target", "R:R", "Outcome", "Net R"],
            rows,
        )

    return f"""# Backtest — {symbol} {interval}{' (partial at 1R)' if partial else ''}

{describe(bars, provenance)}

Costs applied: {spec.typical_spread_ticks:.0f}-tick spread + ${spec.commission_round_turn:.2f} commission
per round turn = ${spec.round_trip_cost():.2f}, converted to R against each trade's own risk.

{format_stats(s, "Results")}

### Trades

{detail or "_none_"}
"""
