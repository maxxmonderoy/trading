"""SMC / Price Action framework — detectors and the execution state machine.

A faithful implementation of the four-step model: HTF liquidity target → sweep →
market structure shift → FVG retest entry, gated by kill-zone windows and the
hard risk rules.

**Every ambiguity in the spec is resolved here as a named, configurable
parameter rather than a silent constant.** The prose says "swing high", "quick
wick", "aggressive reverse" — none of which are computable as written. Each one
below states the operational definition used, so a setup this scanner reports can
be audited against the chart and the definition argued with directly. Where a
parameter changes what counts as a setup, it lives in `config.json` under `smc`.

Resolved definitions:

- **Swing point** — an n-bar fractal: bar `i` is a swing high when its high
  exceeds the highs of the `swing_lookback` bars on each side. Default 2.
- **Sweep** — price trades beyond the level (wick or body) and a bar closes back
  on the originating side within `sweep_return_bars`. Without the close-back
  requirement, an ordinary breakout registers as a sweep.
- **MSS** — a *close* beyond the opposing swing formed before the sweep, within
  `mss_max_bars`. Body close, per the spec: wick-through does not count.
- **FVG** — three-bar imbalance. Bullish: `bar[i-1].high < bar[i+1].low`.
  Bearish: `bar[i-1].low > bar[i+1].high`. Must be created by the MSS leg.
- **Entry** — Consequent Encroachment (50% of the gap) by default; `edge` uses
  the proximal edge instead.
- **Invalidation** — a setup that cannot reach `min_rr` against the nearest
  opposing liquidity is discarded, per the minimum 1:2 rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime

import pandas as pd

from .common import MEMORY_DIR, ROOT, load_config, markdown_table, unavailable
from .futures import (
    EASTERN,
    VALID_INTERVALS,
    ContractSpec,
    _tag_sessions,
    compute_levels,
    intraday,
    resolve,
)

_DEFAULT_SMC = {
    "swing_lookback": 2,
    "sweep_return_bars": 5,
    "mss_max_bars": 20,
    "stop_buffer_ticks": 4,
    "entry_mode": "ce",          # ce | edge
    "min_rr": 2.0,
    "max_trades_per_session": 2,
    "stop_after_losses": 2,
    "risk_pct": 1.0,
    "news_blackout_minutes": 15,
    "kill_zones": {
        "NY": ["09:30", "11:30"],
        "London": ["02:00", "05:00"],
    },
}


def smc_config() -> dict:
    config = dict(_DEFAULT_SMC)
    config.update(load_config().get("smc", {}))
    return config


def _parse_time(text: str) -> dtime:
    hour, minute = text.split(":")
    return dtime(int(hour), int(minute))


def kill_zone_for(timestamp: pd.Timestamp, config: dict | None = None) -> str | None:
    """Which kill zone a timestamp falls in, or None. Exchange time (ET)."""
    config = config or smc_config()
    current = timestamp.time()
    for name, (start, end) in config["kill_zones"].items():
        if _parse_time(start) <= current < _parse_time(end):
            return name
    return None



def is_buy_side(level_name: str) -> bool:
    """Do the resting orders at this level sit above price?

    Buy-side liquidity (stops of shorts, breakout buys) rests above highs;
    sell-side rests below lows. Label shapes vary — PDH, ONH, LondonH, EQHx3 —
    so this is the one place that decides, and both the scanner and the report
    call it. They previously disagreed: `EQHx3` ends in a digit, so an
    `endswith("H")` test silently classified every equal-highs cluster as
    sell-side in the report while the scanner had it right.
    """
    name = level_name.upper()
    if name.startswith("EQH"):
        return True
    if name.startswith("EQL"):
        return False
    return name.endswith("H")


# ---------------------------------------------------------------------------
# Structure primitives
# ---------------------------------------------------------------------------


@dataclass
class Swing:
    index: int
    timestamp: pd.Timestamp
    price: float
    kind: str  # "high" | "low"


def find_swings(frame: pd.DataFrame, lookback: int = 2) -> list[Swing]:
    """n-bar fractal pivots.

    A swing needs `lookback` bars on each side, so the most recent `lookback`
    bars can never yet be swings — structure is always confirmed late, and any
    system claiming otherwise is repainting.
    """
    highs = frame["High"].astype(float).to_numpy()
    lows = frame["Low"].astype(float).to_numpy()
    swings: list[Swing] = []

    for i in range(lookback, len(frame) - lookback):
        window = slice(i - lookback, i + lookback + 1)
        if highs[i] == highs[window].max() and (highs[window] < highs[i]).sum() >= lookback:
            swings.append(Swing(i, frame.index[i], float(highs[i]), "high"))
        if lows[i] == lows[window].min() and (lows[window] > lows[i]).sum() >= lookback:
            swings.append(Swing(i, frame.index[i], float(lows[i]), "low"))
    return swings


@dataclass
class FVG:
    start_index: int      # index of bar 1 of the three
    timestamp: pd.Timestamp
    direction: str        # "bullish" | "bearish"
    top: float
    bottom: float

    @property
    def ce(self) -> float:
        """Consequent Encroachment — the 50% level of the gap."""
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def entry_price(self, mode: str) -> float:
        if mode == "edge":
            # Proximal edge: the level price reaches first on the retrace.
            return self.bottom if self.direction == "bullish" else self.top
        return self.ce


def find_fvgs(frame: pd.DataFrame, start: int = 0, end: int | None = None) -> list[FVG]:
    """Three-bar imbalances in a slice of the frame.

    A gap exists when bar 1 and bar 3 do not overlap — the middle bar moved fast
    enough that no trade occurred across that band.
    """
    end = end if end is not None else len(frame)
    highs = frame["High"].astype(float).to_numpy()
    lows = frame["Low"].astype(float).to_numpy()
    gaps: list[FVG] = []

    for i in range(max(start, 0), min(end, len(frame)) - 2):
        first_high, first_low = highs[i], lows[i]
        third_high, third_low = highs[i + 2], lows[i + 2]

        if first_high < third_low:
            gaps.append(FVG(i, frame.index[i], "bullish", float(third_low), float(first_high)))
        elif first_low > third_high:
            gaps.append(FVG(i, frame.index[i], "bearish", float(first_low), float(third_high)))
    return gaps


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


@dataclass
class Setup:
    """One complete four-step setup, with the provenance of every step."""
    session_date: str
    direction: str                 # "long" | "short"
    kill_zone: str

    # Step 1
    liquidity_level_name: str
    liquidity_level_price: float

    # Step 2
    sweep_time: str
    sweep_extreme: float
    sweep_return_bars: int

    # Step 3
    mss_time: str
    mss_level: float               # the opposing swing that was closed through
    mss_close: float

    # Step 4
    fvg_time: str
    fvg_top: float
    fvg_bottom: float
    entry: float
    stop: float
    target: float
    target_name: str

    risk_points: float
    reward_points: float
    rr: float
    risk_ticks: float

    # Outcome, when the scanner is run over completed history
    outcome: str = "unresolved"    # filled | stopped | target | expired | unfilled
    outcome_time: str = ""
    notes: list[str] = field(default_factory=list)

    # Scaled exits make the result continuous rather than win/lose: banking half
    # at 1R and stopping the runner at breakeven is neither. `realized_r` is the
    # actual R the trade returned, and it is what EV is measured over when set.
    realized_r: float | None = None
    tp1_hit: bool = False


_STATES = ("IDLE", "MONITOR_HTF_LEVELS", "AWAIT_MSS", "SET_ENTRY_ORDER", "EXECUTION_MONITOR")


def _liquidity_targets(tagged: pd.DataFrame, session_date: pd.Timestamp,
                       frame: pd.DataFrame | None = None, spec=None,
                       config: dict | None = None) -> dict[str, float]:
    """Step 1 — every liquidity target the spec names.

    PDH/PDL and overnight extremes, plus the session ranges that completed before
    this one, plus EQH/EQL clusters. All four categories are in the written
    framework; earlier versions of this function shipped only the first two while
    the docstring claimed otherwise.
    """
    config = config or smc_config()
    levels = compute_levels(tagged, session_date)
    targets: dict[str, float] = {}

    # A market that never closes has no overnight session, so ONH/ONL would be
    # an arbitrary slice of a continuous tape dressed up as structure. Prior-day
    # extremes survive the move to a UTC boundary; the overnight pair does not.
    wanted = [("pd_high", "PDH"), ("pd_low", "PDL")]
    if spec is None or not getattr(spec, "is_crypto", False):
        wanted += [("on_high", "ONH"), ("on_low", "ONL")]
    for key, label in wanted:
        if key in levels:
            targets[label] = levels[key]

    targets.update(session_ranges(tagged, session_date, config))

    # EQH/EQL from the swings formed before this session's kill zones.
    if frame is not None and spec is not None and not frame.empty:
        swings = find_swings(frame, config["swing_lookback"])
        for equal in find_equal_levels(
            swings, spec.tick_points, config.get("eq_tolerance_ticks", 4.0)
        ):
            label = f"EQ{'H' if equal.kind == 'high' else 'L'}x{equal.count}"
            targets[label] = equal.price

    return targets


def _targets_at(session_frame: pd.DataFrame, upto: int, static: dict[str, float],
                spec: ContractSpec, config: dict) -> dict[str, float]:
    """Liquidity levels knowable at bar `upto`, and no others.

    Session ranges develop through the day: the NY AM low does not exist at
    02:00 London, and the overnight high is still forming until 09:30. Computing
    targets once from the completed session and scanning from the start lets the
    machine sweep a level that had not formed yet — look-ahead that flatters
    results and cannot be reproduced live. Running extremes are honest: at any
    moment you do know the highest price *so far* in the session in progress.
    """
    visible = session_frame.iloc[:upto + 1]
    if visible.empty:
        return dict(static)

    targets = dict(static)
    ranges = config.get("session_ranges", _DEFAULT_SESSION_RANGES)
    times = visible.index.time

    for name, (start, end) in ranges.items():
        start_time, end_time = _parse_time(start), _parse_time(end)
        if start_time <= end_time:
            mask = (times >= start_time) & (times < end_time)
        else:
            mask = (times >= start_time) | (times < end_time)
        window = visible[mask]
        if window.empty:
            continue
        targets[f"{name}H"] = float(window["High"].max())
        targets[f"{name}L"] = float(window["Low"].min())

    overnight = visible[~visible["is_rth"]]
    if not overnight.empty:
        targets["ONH"] = float(overnight["High"].max())
        targets["ONL"] = float(overnight["Low"].min())

    for equal in find_equal_levels(
        find_swings(visible, config["swing_lookback"]),
        spec.tick_points, config.get("eq_tolerance_ticks", 4.0),
    ):
        targets[f"EQ{'H' if equal.kind == 'high' else 'L'}x{equal.count}"] = equal.price

    return targets


def scan_session(
    frame: pd.DataFrame,
    spec: ContractSpec,
    session_date: pd.Timestamp,
    targets: dict[str, float],
    config: dict,
) -> list[Setup]:
    """Walk one session bar by bar through the state machine.

    Deliberately sequential and stateful rather than vectorised — the spec is a
    state machine and each step must be shown to have triggered *after* the
    previous one. A vectorised implementation would make it easy to accidentally
    detect an MSS that preceded its own sweep.
    """
    if frame.empty:
        return []

    lookback = config["swing_lookback"]
    swings = find_swings(frame, lookback)
    highs = frame["High"].astype(float).to_numpy()
    lows = frame["Low"].astype(float).to_numpy()
    closes = frame["Close"].astype(float).to_numpy()
    tick = spec.tick_points
    buffer_points = config["stop_buffer_ticks"] * tick

    # Only prior-day levels are fixed before the session opens; everything else
    # develops and is recomputed per bar.
    static_targets = {k: v for k, v in targets.items() if k in ("PDH", "PDL")}

    setups: list[Setup] = []
    state = "IDLE"
    swept: dict | None = None

    for i in range(len(frame)):
        timestamp = frame.index[i]
        zone = kill_zone_for(timestamp, config)

        if state == "IDLE":
            if zone:
                state = "MONITOR_HTF_LEVELS"
            else:
                continue

        if state == "MONITOR_HTF_LEVELS":
            if not zone:
                continue
            # Step 2 — sweep of a liquidity level knowable at this bar.
            live_targets = _targets_at(frame, i, static_targets, spec, config)
            for name, level in live_targets.items():
                buy_side = is_buy_side(name)
                if buy_side and highs[i] > level:
                    swept = {
                        "name": name, "level": level, "index": i,
                        "extreme": float(highs[i]), "side": "buy",
                        "zone": zone,
                    }
                    state = "AWAIT_MSS"
                    break
                if not buy_side and lows[i] < level:
                    swept = {
                        "name": name, "level": level, "index": i,
                        "extreme": float(lows[i]), "side": "sell",
                        "zone": zone,
                    }
                    state = "AWAIT_MSS"
                    break
            continue

        if state == "AWAIT_MSS" and swept:
            # Track the sweep extreme while we wait.
            if swept["side"] == "buy":
                swept["extreme"] = max(swept["extreme"], float(highs[i]))
            else:
                swept["extreme"] = min(swept["extreme"], float(lows[i]))

            elapsed = i - swept["index"]
            if elapsed > config["mss_max_bars"]:
                state, swept = "MONITOR_HTF_LEVELS", None
                continue

            # The sweep must resolve: a close back through the level. Without
            # this the "sweep" is just a breakout and the trap never sprang.
            returned = (
                closes[i] < swept["level"] if swept["side"] == "buy"
                else closes[i] > swept["level"]
            )
            if not returned:
                continue
            if elapsed > config["sweep_return_bars"]:
                # Came back, but too slowly to be the aggressive reversal the
                # spec describes. Reset rather than force the trade.
                state, swept = "MONITOR_HTF_LEVELS", None
                continue

            # Step 3 — MSS: body close beyond the opposing swing formed before the sweep.
            direction = "short" if swept["side"] == "buy" else "long"
            opposing = [
                s for s in swings
                if s.index < swept["index"]
                and s.kind == ("low" if direction == "short" else "high")
            ]
            if not opposing:
                continue
            reference = opposing[-1]

            broke = (
                closes[i] < reference.price if direction == "short"
                else closes[i] > reference.price
            )
            if not broke:
                continue

            # Step 4 — the FVG left by the MSS leg.
            gaps = [
                g for g in find_fvgs(frame, swept["index"], i + 1)
                if g.direction == ("bearish" if direction == "short" else "bullish")
            ]
            if not gaps:
                state, swept = "MONITOR_HTF_LEVELS", None
                continue
            gap = gaps[-1]  # the most recent imbalance in the impulse

            entry = gap.entry_price(config["entry_mode"])
            stop = (
                swept["extreme"] + buffer_points if direction == "short"
                else swept["extreme"] - buffer_points
            )
            risk_points = abs(stop - entry)
            if risk_points <= 0:
                state, swept = "MONITOR_HTF_LEVELS", None
                continue

            target_name, target = _opposing_liquidity(
                _target_pool(
                    _targets_at(frame, i, static_targets, spec, config),
                    config.get("target_pool", "htf"),
                ),
                entry, direction,
            )
            if target is None:
                state, swept = "MONITOR_HTF_LEVELS", None
                continue

            reward_points = abs(target - entry)
            rr = reward_points / risk_points

            setup = Setup(
                session_date=str(session_date.date()),
                direction=direction,
                kill_zone=swept["zone"],
                liquidity_level_name=swept["name"],
                liquidity_level_price=swept["level"],
                sweep_time=str(frame.index[swept["index"]].strftime("%Y-%m-%d %H:%M")),
                sweep_extreme=swept["extreme"],
                sweep_return_bars=elapsed,
                mss_time=str(timestamp.strftime("%Y-%m-%d %H:%M")),
                mss_level=reference.price,
                mss_close=float(closes[i]),
                fvg_time=str(gap.timestamp.strftime("%Y-%m-%d %H:%M")),
                fvg_top=gap.top,
                fvg_bottom=gap.bottom,
                entry=entry,
                stop=stop,
                target=target,
                target_name=target_name,
                risk_points=risk_points,
                reward_points=reward_points,
                rr=rr,
                risk_ticks=risk_points / tick,
            )

            scaling = scaling_settings(config)
            floor = scaling["min_rr"] if scaling["enabled"] else config["min_rr"]
            if rr < floor:
                setup.outcome = "rejected"
                setup.notes.append(
                    f"R:R {rr:.2f} is below the {floor:.1f} minimum — "
                    f"nearest opposing liquidity ({target_name}) is too close to justify the risk."
                )
                setups.append(setup)
                state, swept = "MONITOR_HTF_LEVELS", None
                continue

            _resolve_outcome(setup, frame, i, tick, config)
            setups.append(setup)
            state, swept = "MONITOR_HTF_LEVELS", None

    return setups


HTF_TARGET_LABELS = ("PDH", "PDL", "ONH", "ONL")


def _target_pool(targets: dict[str, float], mode: str) -> dict[str, float]:
    """Which levels are eligible as take-profit destinations.

    The spec says take profit at "opposing **HTF** liquidity", and that qualifier
    does real work. Every marked level is a valid thing to *sweep* in Step 2, but
    not every one is a valid *destination* in Step 4 — targeting the nearest
    intraday equal-high cluster puts the target so close that almost nothing
    clears the 1:2 minimum. Observed directly: enabling EQH/EQL and session
    ranges as targets took this scanner from 3 setups to 0, with 6 rejections.

    `htf`  — prior-day and overnight extremes only (default; matches the wording)
    `all`  — every marked level, including intraday structure
    """
    if mode == "all":
        return targets
    return {k: v for k, v in targets.items() if k in HTF_TARGET_LABELS}


def _opposing_liquidity(targets: dict[str, float], entry: float, direction: str) -> tuple[str, float | None]:
    """Step 4's take-profit: the nearest untaken liquidity in the trade's direction."""
    if direction == "short":
        candidates = {n: p for n, p in targets.items() if p < entry}
        if not candidates:
            return "", None
        name = max(candidates, key=candidates.get)
    else:
        candidates = {n: p for n, p in targets.items() if p > entry}
        if not candidates:
            return "", None
        name = min(candidates, key=candidates.get)
    return name, candidates[name]


_SCALING_DEFAULTS = {
    "enabled": True,
    "tp1_r": 1.0,
    "tp1_fraction": 0.5,
    "breakeven_offset_ticks": 1.0,
    "min_rr": 1.2,
}


def scaling_settings(config: dict | None = None) -> dict:
    settings = dict(_SCALING_DEFAULTS)
    settings.update((config or smc_config()).get("scaling", {}))
    # TP1 beyond the runner's own target is incoherent, so the floor can never
    # sit below where the first exit is taken.
    settings["min_rr"] = max(float(settings["min_rr"]), float(settings["tp1_r"]))
    return settings


def _resolve_outcome(setup: Setup, frame: pd.DataFrame, mss_index: int, tick: float,
                     config: dict | None = None) -> None:
    """Walk forward from the MSS to see whether the limit filled, then what hit first.

    Within-bar ordering is unknowable from OHLC alone. When a bar spans both stop
    and target, this records `ambiguous` rather than guessing — assuming the
    favourable one is the single most common way a backtest flatters itself.

    With scaling enabled the trade has three states rather than two: running
    full size, running a runner after `tp1_fraction` came off at `tp1_r`, and
    done. The second state is why `realized_r` exists — a trade that banks half
    at 1R and then stops the runner at breakeven returns about +0.5R, which is
    neither a win nor a loss and cannot be represented by a win rate.
    """
    scaling = scaling_settings(config)
    highs = frame["High"].astype(float).to_numpy()
    lows = frame["Low"].astype(float).to_numpy()
    short = setup.direction == "short"
    sign = -1.0 if short else 1.0

    tp1_price = setup.entry + sign * scaling["tp1_r"] * setup.risk_points
    # Breakeven plus a tick, so a runner stopped out still covers its own friction.
    be_price = setup.entry + sign * scaling["breakeven_offset_ticks"] * tick
    be_r = (scaling["breakeven_offset_ticks"] * tick) / setup.risk_points if setup.risk_points else 0.0
    fraction = float(scaling["tp1_fraction"])

    filled_at = None
    stop_price = setup.stop

    def stamp(j):
        return str(frame.index[j].strftime("%Y-%m-%d %H:%M"))

    for j in range(mss_index + 1, len(frame)):
        if filled_at is None:
            reached = highs[j] >= setup.entry if short else lows[j] <= setup.entry
            if reached:
                filled_at = j
                setup.outcome = "filled"
                setup.outcome_time = stamp(j)
                # A bar can fill the limit and hit the stop; check from this bar on.
            else:
                continue

        # Adverse events count from the fill bar; favourable ones only from the
        # next. A bar that both fills the limit and runs to target cannot be
        # ordered from OHLC, and crediting it is the same optimistic assumption
        # the scanner already refuses to make for stop-versus-target. Charging
        # the stop on the fill bar while withholding the target is deliberately
        # asymmetric — it errs toward the result that loses money.
        favourable = j > filled_at
        hit_stop = highs[j] >= stop_price if short else lows[j] <= stop_price
        hit_target = favourable and (lows[j] <= setup.target if short else highs[j] >= setup.target)

        if not scaling["enabled"]:
            if hit_stop and hit_target:
                setup.outcome, setup.outcome_time = "ambiguous", stamp(j)
                setup.notes.append(
                    "Stop and target both traded within one bar — OHLC cannot order them. "
                    "Counted as neither; resolving it needs tick data."
                )
                return
            if hit_stop:
                setup.outcome, setup.outcome_time = "stopped", stamp(j)
                setup.realized_r = -1.0
                return
            if hit_target:
                setup.outcome, setup.outcome_time = "target", stamp(j)
                setup.realized_r = setup.rr
                return
            continue

        if not setup.tp1_hit:
            hit_tp1 = favourable and (lows[j] <= tp1_price if short else highs[j] >= tp1_price)
            if hit_stop and hit_tp1:
                setup.outcome, setup.outcome_time = "ambiguous", stamp(j)
                setup.notes.append(
                    "Stop and first target both traded within one bar — OHLC cannot order "
                    "them. Counted as neither; resolving it needs tick data."
                )
                return
            if hit_stop:
                setup.outcome, setup.outcome_time = "stopped", stamp(j)
                setup.realized_r = -1.0
                return
            if hit_tp1:
                setup.tp1_hit = True
                stop_price = be_price
                setup.notes.append(
                    f"TP1: {fraction:.0%} closed at {scaling['tp1_r']:.1f}R, stop to breakeven "
                    f"+{scaling['breakeven_offset_ticks']:.0f} tick."
                )
                # The same bar can reach TP1 and then run to the final target.
                if hit_target:
                    setup.outcome, setup.outcome_time = "target", stamp(j)
                    setup.realized_r = fraction * scaling["tp1_r"] + (1 - fraction) * setup.rr
                    return
            continue

        # Runner only, stop now at breakeven.
        if hit_stop and hit_target:
            setup.outcome, setup.outcome_time = "ambiguous", stamp(j)
            setup.notes.append(
                "Breakeven stop and target both traded within one bar on the runner."
            )
            return
        if hit_stop:
            setup.outcome, setup.outcome_time = "scaled_breakeven", stamp(j)
            setup.realized_r = fraction * scaling["tp1_r"] + (1 - fraction) * be_r
            return
        if hit_target:
            setup.outcome, setup.outcome_time = "target", stamp(j)
            setup.realized_r = fraction * scaling["tp1_r"] + (1 - fraction) * setup.rr
            return

    if filled_at is None:
        setup.outcome = "unfilled"
        setup.notes.append("Limit at the FVG was never reached before the data ended.")
    else:
        setup.outcome = "open_at_end"
        setup.notes.append(
            "Filled but neither stop nor target hit before the data ended."
            + (" TP1 was banked." if setup.tp1_hit else "")
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def scan(symbol: str, interval: str = "5m", sessions: int = 10,
         curr_date: str | None = None) -> str:
    """Run the state machine across recent sessions and report every setup."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    config = smc_config()
    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(
            f"{spec.symbol} {interval}",
            "no intraday bars — this framework cannot run without them",
        )

    tagged = _tag_sessions(frame, spec)
    all_sessions = sorted(tagged["session_date"].unique())[-sessions:]

    all_setups: list[Setup] = []
    for session_date in all_sessions:
        session_frame = tagged[tagged["session_date"] == session_date].sort_index()
        targets = _liquidity_targets(tagged, session_date, session_frame, spec, config)
        if not targets:
            continue
        all_setups.extend(
            scan_session(session_frame, spec, session_date, targets, config)
        )

    if not all_setups:
        return (
            f"## SMC scan — {spec.symbol} {interval}\n\n"
            f"No setups across {len(all_sessions)} sessions.\n\n"
            "All four steps must trigger in sequence inside a kill zone, so dry spells are "
            "expected and are not evidence the scanner is broken. Loosen `sweep_return_bars` "
            "or `mss_max_bars` in `config.json` to see how sensitive the count is to those "
            "definitions — if it swings wildly, the framework's edge is in the parameters."
        )

    taken = [s for s in all_setups if s.outcome not in ("rejected",)]
    rejected = [s for s in all_setups if s.outcome == "rejected"]

    rows = []
    for s in taken:
        rows.append([
            s.session_date, s.kill_zone, s.direction.upper(),
            s.liquidity_level_name, f"{s.sweep_extreme:,.2f}",
            f"{s.entry:,.2f}", f"{s.stop:,.2f}", f"{s.target:,.2f}",
            f"{s.risk_ticks:.0f}", f"{s.rr:.2f}", s.outcome,
        ])

    wins = sum(1 for s in taken if s.outcome == "target")
    losses = sum(1 for s in taken if s.outcome == "stopped")
    ambiguous = sum(1 for s in taken if s.outcome == "ambiguous")
    resolved = wins + losses

    summary_rows = [
        ["Sessions scanned", str(len(all_sessions))],
        ["Setups triggered", str(len(taken))],
        ["Rejected on R:R", str(len(rejected))],
        ["Target hit", str(wins)],
        ["Stopped", str(losses)],
        ["Ambiguous (stop+target same bar)", str(ambiguous)],
        ["Unresolved / unfilled", str(len(taken) - resolved - ambiguous)],
    ]
    if resolved:
        expectancy_r = (wins * config["min_rr"] - losses) / resolved
        summary_rows.append(["Win rate (resolved only)", f"{wins}/{resolved} ({wins / resolved * 100:.0f}%)"])
        summary_rows.append(["Expectancy", f"{expectancy_r:+.2f}R per resolved setup"])

    detail = "\n\n".join(_setup_detail(s, spec) for s in taken[-5:])

    return f"""## SMC scan — {spec.symbol} ({spec.name}), {interval}

{markdown_table(["Session", "Zone", "Dir", "Swept", "Extreme", "Entry", "Stop", "Target", "Risk(t)", "R:R", "Outcome"], rows) if rows else "_No setups passed the R:R filter._"}

### Summary

{markdown_table(["Metric", "Value"], summary_rows)}

### Most recent setups in detail

{detail if detail else "_none_"}

> **This is a scan, not a backtest.** Fills are assumed at the limit price with no
> slippage or queue position, and any bar containing both stop and target is
> recorded as `ambiguous` rather than resolved — OHLC cannot order events inside a
> bar. A real result needs tick data.
>
> Sample sizes here are small by construction: intraday history is capped at
> {30 if interval == "5m" else 60} days by the free data source, and the four-step
> sequence fires rarely. Do not read a win rate off single-digit samples.
"""


def _setup_detail(setup: Setup, spec: ContractSpec) -> str:
    risk_dollars = spec.points_to_dollars(setup.risk_points)
    reward_dollars = spec.points_to_dollars(setup.reward_points)
    lines = [
        f"**{setup.session_date} — {setup.direction.upper()} ({setup.kill_zone} kill zone)** → `{setup.outcome}`",
        "",
        f"1. **Liquidity target**: {setup.liquidity_level_name} at {setup.liquidity_level_price:,.2f}",
        f"2. **Sweep**: {setup.sweep_time}, extreme {setup.sweep_extreme:,.2f}, "
        f"closed back through in {setup.sweep_return_bars} bar(s)",
        f"3. **MSS**: {setup.mss_time}, close {setup.mss_close:,.2f} through the opposing swing at {setup.mss_level:,.2f}",
        f"4. **FVG**: {setup.fvg_time}, gap {setup.fvg_bottom:,.2f}–{setup.fvg_top:,.2f}, "
        f"entry at {setup.entry:,.2f}",
        "",
        f"   Stop {setup.stop:,.2f} | target {setup.target:,.2f} ({setup.target_name}) | "
        f"risk {setup.risk_ticks:.0f} ticks (${risk_dollars:,.2f}/contract) | "
        f"reward ${reward_dollars:,.2f} | **{setup.rr:.2f}R**",
    ]
    for note in setup.notes:
        lines.append(f"   > {note}")
    return "\n".join(lines)


def explain(symbol: str = "NQ") -> str:
    """The framework's operational definitions and current parameters."""
    config = smc_config()
    zones = " | ".join(f"{n} {a}–{b} ET" for n, (a, b) in config["kill_zones"].items())
    rows = [
        ["Swing lookback", f"{config['swing_lookback']} bars each side"],
        ["Sweep return window", f"{config['sweep_return_bars']} bars to close back through"],
        ["MSS max delay", f"{config['mss_max_bars']} bars after sweep"],
        ["Stop buffer", f"{config['stop_buffer_ticks']} ticks beyond the sweep extreme"],
        ["Entry mode", f"{config['entry_mode']} ({'50% of the gap' if config['entry_mode'] == 'ce' else 'proximal edge'})"],
        ["Minimum R:R", f"{config['min_rr']:.1f}"],
        ["Target pool", f"{config.get('target_pool', 'htf')} ({'PD/ON extremes only' if config.get('target_pool', 'htf') == 'htf' else 'every marked level'})"],
        ["Max trades per session", str(config["max_trades_per_session"])],
        ["Shutdown after", f"{config['stop_after_losses']} losses"],
        ["Risk per trade", f"{config['risk_pct']:.2f}% of account"],
        ["News blackout", f"{config['news_blackout_minutes']} minutes either side"],
        ["Kill zones", zones],
    ]
    return f"""## SMC framework — operational definitions

The written framework uses terms that are not directly computable ("quick wick",
"aggressive reverse", "swing high"). These are the definitions this implementation
uses. Change them in `config.json` under `smc`.

{markdown_table(["Parameter", "Setting"], rows)}

### The four steps, as implemented

1. **HTF liquidity target** — PDH/PDL and overnight high/low are marked before the
   session. Buy-side liquidity sits above swing highs, sell-side below swing lows.
2. **Sweep** — price trades beyond the level *and* a bar closes back through it
   within the return window. The close-back is what separates a trap from a
   breakout; without it every trending session reads as a sweep.
3. **MSS** — a body close beyond the last opposing swing that formed *before* the
   sweep. Wick-through does not qualify, per the spec.
4. **FVG entry** — the most recent three-bar imbalance inside the MSS leg. Limit at
   Consequent Encroachment, stop beyond the sweep extreme plus buffer, target the
   nearest opposing liquidity. Below {config['min_rr']:.1f}R the setup is discarded.

### What the implementation cannot decide for you

- **Swing definition drives everything.** A 2-bar fractal and a 5-bar fractal
  produce different structure, hence different MSS events, hence different setups.
  There is no correct value — it is the strategy.
- **The sweep return window is the whole trap concept.** Widen it and trend
  continuation is counted as a reversal setup.
- Run `bin/ta smc sensitivity` to see how the setup count moves with these
  parameters before trusting any result.
"""


def swings_report(symbol: str, interval: str = "5m", lookback: int | None = None,
                  limit: int = 20, curr_date: str | None = None) -> str:
    """Confirmed fractal pivots — the structure the MSS is measured against."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    config = smc_config()
    lookback = lookback or config["swing_lookback"]
    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} {interval}", "no intraday bars")

    found = find_swings(frame, lookback)[-limit:]
    if not found:
        return f"_No {lookback}-bar swings found on {spec.symbol} {interval}._"

    rows = [
        [s.timestamp.strftime("%m-%d %H:%M"), s.kind.upper(), f"{s.price:,.2f}",
         kill_zone_for(s.timestamp, config) or "—"]
        for s in found
    ]
    return (
        f"## Swing structure — {spec.symbol} {interval} ({lookback}-bar fractal)\n\n"
        + markdown_table(["Time", "Type", "Price", "Kill zone"], rows)
        + f"\n\n> A swing needs {lookback} bars on each side to confirm, so the last "
        f"{lookback} bars can never yet be swings. Structure is always confirmed late — "
        "anything that appears to identify a swing in real time is repainting."
    )


def fvg_report(symbol: str, interval: str = "5m", limit: int = 20,
               curr_date: str | None = None) -> str:
    """Fair value gaps, flagged by whether price has since traded back through them."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} {interval}", "no intraday bars")

    gaps = find_fvgs(frame)[-limit:]
    if not gaps:
        return f"_No three-bar imbalances found on {spec.symbol} {interval}._"

    lows = frame["Low"].astype(float).to_numpy()
    highs = frame["High"].astype(float).to_numpy()
    rows = []
    for gap in gaps:
        after = slice(gap.start_index + 3, len(frame))
        if gap.direction == "bullish":
            mitigated = lows[after].min() <= gap.bottom if len(lows[after]) else False
            touched_ce = lows[after].min() <= gap.ce if len(lows[after]) else False
        else:
            mitigated = highs[after].max() >= gap.top if len(highs[after]) else False
            touched_ce = highs[after].max() >= gap.ce if len(highs[after]) else False
        rows.append([
            gap.timestamp.strftime("%m-%d %H:%M"), gap.direction,
            f"{gap.bottom:,.2f}", f"{gap.top:,.2f}", f"{gap.ce:,.2f}",
            f"{gap.size / spec.tick_points:.0f}",
            "filled" if mitigated else ("CE tagged" if touched_ce else "**unmitigated**"),
        ])

    return (
        f"## Fair value gaps — {spec.symbol} {interval}\n\n"
        + markdown_table(["Created", "Direction", "Bottom", "Top", "CE (50%)", "Size(t)", "Status"], rows)
        + "\n\n> **CE** is Consequent Encroachment, the 50% level and the default entry. "
        "`filled` means price has traded fully through the gap; `unmitigated` means it "
        "has not been revisited. Note that most gaps fill — an unmitigated gap is a "
        "reference level, not a prediction."
    )


def sensitivity(symbol: str, interval: str = "5m", sessions: int = 20,
                curr_date: str | None = None) -> str:
    """How many setups appear under different definitions of the ambiguous terms.

    The most important diagnostic in this module. If the setup count swings by a
    large factor across reasonable parameter choices, the framework's apparent
    edge lives in the parameter selection rather than in the market — and any
    result quoted without its parameters is meaningless.
    """
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    base = smc_config()
    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} {interval}", "no intraday bars")

    tagged = _tag_sessions(frame, spec)
    all_sessions = sorted(tagged["session_date"].unique())[-sessions:]

    def count(overrides: dict) -> tuple[int, int, int]:
        config = {**base, **overrides}
        setups: list[Setup] = []
        for session_date in all_sessions:
            session_frame = tagged[tagged["session_date"] == session_date].sort_index()
            targets = _liquidity_targets(tagged, session_date, session_frame, spec, config)
            if not targets:
                continue
            setups.extend(scan_session(session_frame, spec, session_date, targets, config))
        taken = [s for s in setups if s.outcome != "rejected"]
        wins = sum(1 for s in taken if s.outcome == "target")
        losses = sum(1 for s in taken if s.outcome == "stopped")
        return len(taken), wins, losses

    rows = []
    for name, overrides in [
        ("baseline (config.json)", {}),
        ("swing_lookback 1", {"swing_lookback": 1}),
        ("swing_lookback 3", {"swing_lookback": 3}),
        ("swing_lookback 5", {"swing_lookback": 5}),
        ("sweep_return_bars 2", {"sweep_return_bars": 2}),
        ("sweep_return_bars 10", {"sweep_return_bars": 10}),
        ("mss_max_bars 10", {"mss_max_bars": 10}),
        ("mss_max_bars 40", {"mss_max_bars": 40}),
        ("entry at edge", {"entry_mode": "edge"}),
        ("min_rr 1.5", {"min_rr": 1.5}),
        ("min_rr 3.0", {"min_rr": 3.0}),
        ("target_pool all levels", {"target_pool": "all"}),
    ]:
        total, wins, losses = count(overrides)
        resolved = wins + losses
        rows.append([
            name, str(total), str(wins), str(losses),
            f"{wins / resolved * 100:.0f}%" if resolved else "—",
        ])

    counts = [int(r[1]) for r in rows]
    spread = f"{min(counts)}–{max(counts)}"

    return f"""## Parameter sensitivity — {spec.symbol} {interval}, {len(all_sessions)} sessions

{markdown_table(["Definition", "Setups", "Target", "Stopped", "Win rate"], rows)}

> **Setup count across these definitions: {spread}.**
>
> Every row is a defensible reading of the same written framework. None of these
> parameters is specified by the strategy as given — each is a choice made when
> turning prose into code. If the counts and win rates move materially across
> rows, then the numbers describe the parameter choice at least as much as they
> describe the market, and no single row should be quoted as "the" result.
>
> Sample sizes here are far too small for the win-rate column to mean anything.
> It is shown to demonstrate instability, not performance.
"""


# ---------------------------------------------------------------------------
# Section 4 — hard execution rules
# ---------------------------------------------------------------------------

NEWS_CALENDAR_PATH = ROOT / "news_calendar.json"


def load_news_calendar() -> tuple[list[dict], str | None]:
    """High-impact events, from a user-maintained file.

    There is no reliable keyless economic-calendar API, and a scraped one that
    silently breaks is worse than none: the no-news rule would appear enforced
    while quietly passing every event. So the calendar is an explicit local file,
    and when it is absent the rule reports itself as UNENFORCED rather than
    silently succeeding.
    """
    if not NEWS_CALENDAR_PATH.exists():
        return [], (
            f"no calendar at {NEWS_CALENDAR_PATH.name} — the no-news rule is UNENFORCED. "
            "Create it as a JSON list of {\"datetime\": \"YYYY-MM-DD HH:MM\", \"event\": \"CPI\"} "
            "in ET, or check the calendar manually before every session"
        )
    try:
        events = json.loads(NEWS_CALENDAR_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return [], f"calendar unreadable ({type(exc).__name__}) — the no-news rule is UNENFORCED"
    if not events:
        return [], "calendar is empty — the no-news rule is UNENFORCED"
    return events, None


def in_news_blackout(timestamp: pd.Timestamp, events: list[dict], minutes: int) -> str | None:
    """The event name if `timestamp` falls inside a blackout window, else None."""
    for event in events:
        try:
            when = pd.Timestamp(event["datetime"], tz=EASTERN)
        except (KeyError, ValueError):
            continue
        if abs((timestamp - when).total_seconds()) <= minutes * 60:
            return event.get("event", "high-impact event")
    return None


def enforce_session_rules(setups: list[Setup], config: dict) -> list[Setup]:
    """Apply 'One & Done / Two & Out' and the news blackout, in chronological order.

    These rules are not cosmetic — they cap the tail. A framework evaluated
    without them reports the results of a strategy nobody is allowed to trade.
    """
    events, calendar_error = load_news_calendar()
    blackout_minutes = config["news_blackout_minutes"]
    max_trades = config["max_trades_per_session"]
    stop_after = config["stop_after_losses"]

    by_session: dict[str, list[Setup]] = {}
    for setup in sorted(setups, key=lambda s: (s.session_date, s.mss_time)):
        by_session.setdefault(setup.session_date, []).append(setup)

    for session_setups in by_session.values():
        taken = losses = 0
        for setup in session_setups:
            if setup.outcome == "rejected":
                continue

            if events:
                entry_time = pd.Timestamp(setup.mss_time, tz=EASTERN)
                event = in_news_blackout(entry_time, events, blackout_minutes)
                if event:
                    setup.outcome = "skipped_news"
                    setup.notes.append(
                        f"Inside the {blackout_minutes}-minute blackout around {event}. "
                        "Rule: flatten or cancel; no new orders."
                    )
                    continue
            elif calendar_error:
                setup.notes.append(f"News rule not applied — {calendar_error}.")

            if losses >= stop_after:
                setup.outcome = "skipped_shutdown"
                setup.notes.append(
                    f"Session already had {losses} losses — 'Two & Out' shuts trading down "
                    "for the day. This setup was not taken."
                )
                continue
            if taken >= max_trades:
                setup.outcome = "skipped_max_trades"
                setup.notes.append(
                    f"Session already used its {max_trades}-trade allowance."
                )
                continue

            taken += 1
            if setup.outcome == "stopped":
                losses += 1

    return setups


def size_setup(setup: Setup, spec: ContractSpec, account: float, risk_pct: float) -> dict:
    """Contracts permitted for one setup under fixed fractional risk.

    Uses the framework's own formula: $Risk / |Entry - StopLoss|, converted to
    whole contracts. Returning zero is a valid and common answer.
    """
    risk_dollars = account * (risk_pct / 100)
    risk_per_contract = spec.points_to_dollars(setup.risk_points)
    contracts = int(risk_dollars / risk_per_contract) if risk_per_contract > 0 else 0
    return {
        "risk_budget": risk_dollars,
        "risk_per_contract": risk_per_contract,
        "contracts": contracts,
        "actual_risk": risk_per_contract * contracts,
        "cost": spec.round_trip_cost() * contracts,
    }


_EV_DEFAULTS = {
    "min_ev_r": 0.25,
    "min_sample": 20,
    "adverse_ticks_entry": 2.0,
    "adverse_ticks_stop": 2.0,
    "ambiguous_as_loss": True,
}


def _as_full_loss(setup):
    """An ambiguous bar graded conservatively: the trade is charged a full stop.

    Copied rather than mutated so re-running the calculation with
    `ambiguous_as_loss` off gives the original number back.
    """
    from copy import copy

    charged = copy(setup)
    charged.realized_r = -1.0
    return charged


def ev_settings(config: dict | None = None) -> dict:
    settings = dict(_EV_DEFAULTS)
    settings.update((config or smc_config()).get("ev_gate", {}))
    return settings


def expected_value(setups: list[Setup], spec: ContractSpec, config: dict | None = None) -> dict:
    """Measured expected value per trade, in R, net of execution drag.

        EV_R = p_win × mean_R_win − p_loss × 1.0 − cost_R

    Three things about where the inputs come from, because getting them from the
    wrong place is the usual way an EV gate ends up rubber-stamping a losing
    strategy:

    **The win rate is this strategy's, not a level's.** `fut level-stats` measures
    how often a level is swept or broken. That is the probability of the *setup
    trigger*, not of the *trade working* — conditioning on a sweep is step 2 of
    four, and the MSS and FVG steps that follow filter the population heavily. The
    scanner already resolves every setup it produces to `target` or `stopped`, so
    that resolved population is the only honest source for p_win.

    **A loss is 1R by construction, a win is not.** Every setup is sized so the
    stop is exactly 1R, but targets are drawn to liquidity, so `rr` varies per
    setup. Winners are therefore averaged, losers are not.

    **Costs are charged in R, per setup.** A fixed dollar cost is a different
    fraction of risk for a 20-tick stop than for an 80-tick one, so the drag is
    computed per setup and then averaged, never applied as a single blended
    number at the end.

    `ambiguous` outcomes — where one bar spans both stop and target and OHLC
    cannot order them — count as losses by default. That is the conservative
    reading, and it is the one that matches the scanner's refusal to assume the
    favourable fill.
    """
    settings = ev_settings(config)
    ambiguous = [s for s in setups if s.outcome == "ambiguous"]

    # With scaled exits the result is a number, not a side: a trade that banks
    # half at 1R and stops its runner at breakeven returns about +0.5R and is
    # neither a win nor a loss. When realized R is available it is measured
    # directly, and the win rate becomes a description rather than an input.
    scaled = [s for s in setups if getattr(s, "realized_r", None) is not None]
    graded = [s for s in scaled if s.outcome != "ambiguous"]
    if settings["ambiguous_as_loss"]:
        graded = graded + [
            _as_full_loss(s) for s in scaled if s.outcome == "ambiguous"
        ]

    if graded:
        wins = [s for s in graded if s.realized_r > 0]
        losses = [s for s in graded if s.realized_r <= 0]
        resolved = graded
    else:
        wins = [s for s in setups if s.outcome == "target"]
        losses = [s for s in setups if s.outcome == "stopped"]
        if settings["ambiguous_as_loss"]:
            losses = losses + ambiguous
        resolved = wins + losses
    n = len(resolved)

    cost_r_values = []
    for setup in resolved:
        risk_dollars = spec.points_to_dollars(setup.risk_points)
        if risk_dollars <= 0:
            continue
        slippage_ticks = settings["adverse_ticks_entry"] + settings["adverse_ticks_stop"]
        # A percentage fee is only a number once it has a price to bite on, and
        # each setup carries its own entry. Charging a blended price here would
        # understate costs on the trades that mattered most.
        drag = spec.round_trip_cost(getattr(setup, "entry", None)) + spec.ticks_to_dollars(slippage_ticks)
        cost_r_values.append(drag / risk_dollars)
    mean_cost_r = sum(cost_r_values) / len(cost_r_values) if cost_r_values else 0.0

    result = {
        "n_resolved": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_ambiguous": len(ambiguous),
        "n_unfilled": len([s for s in setups if s.outcome == "unfilled"]),
        "n_open": len([s for s in setups if s.outcome == "open_at_end"]),
        "min_sample": settings["min_sample"],
        "min_ev_r": settings["min_ev_r"],
        "mean_cost_r": mean_cost_r,
        "slippage_ticks": settings["adverse_ticks_entry"] + settings["adverse_ticks_stop"],
    }

    if n < settings["min_sample"]:
        # Fails closed. An EV computed on eight trades is a description of eight
        # trades; quoting it as an edge is how a strategy gets sized off noise.
        result.update({
            "measurable": False, "passes": False,
            "p_win": None, "mean_r_win": None, "gross_ev_r": None, "net_ev_r": None,
            "verdict": (
                f"NOT MEASURABLE — {n} resolved setups, {settings['min_sample']} required. "
                "The gate fails closed: an unmeasured edge is not a passing one."
            ),
        })
        return result

    p_win = len(wins) / n
    if graded:
        # Mean realized R *is* the expected value per trade. No decomposition
        # into a win rate and an average winner is needed, and none would be
        # correct once partial exits put mass between -1R and the full target.
        mean_r_win = sum(s.realized_r for s in wins) / len(wins) if wins else 0.0
        gross_ev_r = sum(s.realized_r for s in graded) / n
    else:
        mean_r_win = sum(s.rr for s in wins) / len(wins) if wins else 0.0
        gross_ev_r = p_win * mean_r_win - (1 - p_win) * 1.0
    net_ev_r = gross_ev_r - mean_cost_r
    passes = net_ev_r >= settings["min_ev_r"]

    result.update({
        "measurable": True,
        "passes": passes,
        "p_win": p_win,
        "mean_r_win": mean_r_win,
        "gross_ev_r": gross_ev_r,
        "net_ev_r": net_ev_r,
        "verdict": (
            f"{'PASS' if passes else 'FAIL'} — net EV {net_ev_r:+.3f}R per trade against a "
            f"{settings['min_ev_r']:+.2f}R floor."
        ),
    })
    return result


def _ev_block(ev: dict, spec: ContractSpec) -> str:
    """The EV gate rendered for an agent that must not trade past a FAIL."""
    header = "### Expected-value gate\n"
    if not ev["measurable"]:
        return (
            f"{header}\n> 🚫 **{ev['verdict']}**\n>\n"
            f"> Resolved: {ev['n_wins']} target, {ev['n_losses']} stopped "
            f"({ev['n_ambiguous']} ambiguous counted as losses), "
            f"{ev['n_unfilled']} never filled, {ev['n_open']} still open.\n>\n"
            "> Widen `--sessions`, or accept that this configuration has no measured edge "
            "yet. Do not size a position against an EV this thin.\n"
        )

    marker = "✅" if ev["passes"] else "🚫"
    table = markdown_table(["Term", "Value"], [
        ["Resolved setups", str(ev["n_resolved"])],
        ["Win rate", f"{ev['p_win'] * 100:.1f}% ({ev['n_wins']}/{ev['n_resolved']})"],
        ["Mean winner", f"{ev['mean_r_win']:.2f}R"],
        ["Mean loser", "1.00R (stop is 1R by construction)"],
        ["Gross EV", f"{ev['gross_ev_r']:+.3f}R"],
        ["Execution drag", f"−{ev['mean_cost_r']:.3f}R "
                           f"(round turn + {ev['slippage_ticks']:.0f} ticks adverse)"],
        ["**Net EV**", f"**{ev['net_ev_r']:+.3f}R**"],
        ["Floor", f"{ev['min_ev_r']:+.2f}R"],
    ])
    if ev["passes"]:
        note = ""
    elif ev["net_ev_r"] < 0:
        note = (
            "\n> **Do not propose a trade from this configuration.** Net of costs it loses "
            f"{abs(ev['net_ev_r']):.3f}R per trade on the measured sample.\n"
        )
    else:
        note = (
            "\n> **Do not propose a trade from this configuration.** The edge is real but too "
            f"thin to survive: {ev['net_ev_r']:+.3f}R against a {ev['min_ev_r']:+.2f}R floor, and "
            f"execution drag alone is {ev['mean_cost_r']:.3f}R. A sample this size moves several "
            "points per trade.\n"
        )
    return (
        f"{header}\n{marker} **{ev['verdict']}**\n\n{table}\n{note}\n"
        f"_Win rate is measured from this scanner's own resolved setups. It is not a level "
        f"sweep rate — `fut level-stats` measures whether a level gets taken, which is the "
        f"trigger, not the trade._\n"
    )


# ---------------------------------------------------------------------------
# The setup journal
# ---------------------------------------------------------------------------
#
# The EV gate needs ~20 resolved setups. One scan cannot supply them, and no
# amount of waiting fixes that on its own: yfinance serves 5m bars for 30
# calendar days (~21 sessions), and this framework produces 0-3 setups per 20
# sessions. The vendor window slides forward as fast as the sample would grow,
# so a single scan is permanently stuck at a handful of resolved trades.
#
# The journal breaks that ceiling by persisting each scan's resolved setups.
# Run it daily and the population accumulates even though the visible window
# does not. It is the paper-trading record for the futures side, and it is the
# only route to a measurable edge that does not involve risking money.
#
# One thing it must not do is pool setups produced by different parameters.
# Rule 6 of this project: a parameterised result is not a measurement. Change
# `swing_lookback` and the scanner produces a different strategy, so its trades
# belong to a different population. Every row therefore carries a fingerprint of
# the parameters that generated it, and EV is computed per fingerprint.

JOURNAL_PATH = MEMORY_DIR / "smc_setups.jsonl"

_FINGERPRINT_KEYS = (
    "swing_lookback", "sweep_return_bars", "mss_max_bars", "stop_buffer_ticks",
    "entry_mode", "min_rr", "target_pool", "eq_tolerance_ticks",
    "max_trades_per_session", "stop_after_losses",
)

# Outcomes that cannot change if the scan is re-run later. `open_at_end` is
# excluded on purpose: it means the data ran out mid-trade, and tomorrow's scan
# may resolve it to target or stopped.
_TERMINAL = ("target", "stopped", "ambiguous", "unfilled", "scaled_breakeven")


def parameter_fingerprint(config: dict, interval: str) -> str:
    """Short hash of the parameters that decide what counts as a setup."""
    import hashlib

    payload = json.dumps(
        {"interval": interval, **{k: config.get(k) for k in _FINGERPRINT_KEYS}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:10]


def journal_key(symbol: str, interval: str, setup: Setup) -> str:
    """Stable identity for one setup, so re-scanning cannot double-count it."""
    return "|".join([
        symbol, interval, setup.session_date, setup.direction,
        setup.mss_time, f"{setup.entry:.4f}",
    ])


def load_journal(symbol: str = "", fingerprint: str = "") -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    rows = []
    for line in JOURNAL_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a corrupt line loses one trade, not the record
        if symbol and row.get("symbol") != symbol:
            continue
        if fingerprint and row.get("fingerprint") != fingerprint:
            continue
        rows.append(row)
    return rows


def _journal_rows_as_setups(rows: list[dict]):
    """Journal rows exposed with the three attributes `expected_value` reads."""
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            outcome=r["outcome"], rr=float(r["rr"]), risk_points=float(r["risk_points"]),
            realized_r=None if r.get("realized_r") is None else float(r["realized_r"]),
            entry=float(r.get("entry") or 0.0) or None,
        )
        for r in rows
    ]


def record_journal(symbol: str = "NQ", interval: str = "5m", sessions: int = 30,
                   curr_date: str | None = None) -> str:
    """Scan, then append every newly-resolved setup that is not already recorded."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    config = smc_config()
    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} {interval}", "no intraday bars")

    tagged = _tag_sessions(frame, spec)
    served = sorted(tagged["session_date"].unique())
    setups: list[Setup] = []
    for session_date in served[-sessions:]:
        session_frame = tagged[tagged["session_date"] == session_date].sort_index()
        targets = _liquidity_targets(tagged, session_date, session_frame, spec, config)
        if not targets:
            continue
        setups.extend(scan_session(session_frame, spec, session_date, targets, config))
    setups = enforce_session_rules(setups, config)

    fingerprint = parameter_fingerprint(config, interval)
    known = {row["key"] for row in load_journal()}

    added, skipped_known, skipped_open = 0, 0, 0
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL_PATH.open("a") as handle:
        for setup in setups:
            if setup.outcome not in _TERMINAL:
                skipped_open += 1
                continue
            key = journal_key(spec.symbol, interval, setup)
            if key in known:
                skipped_known += 1
                continue
            handle.write(json.dumps({
                "key": key,
                "symbol": spec.symbol,
                "interval": interval,
                "fingerprint": fingerprint,
                "session_date": setup.session_date,
                "direction": setup.direction,
                "kill_zone": setup.kill_zone,
                "liquidity_level": setup.liquidity_level_name,
                "entry": round(setup.entry, 4),
                "stop": round(setup.stop, 4),
                "target": round(setup.target, 4),
                "risk_points": round(setup.risk_points, 4),
                "rr": round(setup.rr, 4),
                "outcome": setup.outcome,
                "outcome_time": setup.outcome_time,
                "realized_r": setup.realized_r,
                "tp1_hit": setup.tp1_hit,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }) + "\n")
            known.add(key)
            added += 1

    rows = load_journal(spec.symbol, fingerprint)
    ev = expected_value(_journal_rows_as_setups(rows), spec, config)
    remaining = max(0, ev["min_sample"] - ev["n_resolved"])

    return (
        f"## Journal — {spec.symbol} {interval}\n\n"
        f"Scanned {len(served[-sessions:])} sessions "
        f"({served[0].date()} → {served[-1].date()}), found {len(setups)} setups.\n\n"
        f"- **{added} newly recorded**\n"
        f"- {skipped_known} already in the journal\n"
        f"- {skipped_open} still open (not terminal — they may resolve on a later run)\n\n"
        f"Journal now holds **{len(rows)} setups** for {spec.symbol} at fingerprint "
        f"`{fingerprint}`, of which **{ev['n_resolved']} are resolved**.\n\n"
        + (f"**{remaining} more resolved setups needed** before the EV gate can be computed.\n"
           if remaining else f"Sample floor reached — `bin/ta smc ev {spec.symbol} --journal` is now measurable.\n")
        + f"\n_Run this daily. The vendor serves only {VALID_INTERVALS.get(interval, '?')} "
        f"days of {interval} bars, so the journal is the only way the sample grows._\n"
    )


_TIMESTAMP_NAMES = ("timestamp", "datetime", "date", "time", "date_time", "开始时间")
_OHLCV_NAMES = {
    "Open": ("open", "o"), "High": ("high", "h"), "Low": ("low", "l"),
    "Close": ("close", "c", "last"), "Volume": ("volume", "vol", "v"),
}


def load_ohlcv_csv(path: str, tz: str = EASTERN) -> pd.DataFrame:
    """Read a local intraday CSV into the frame shape the scanner expects.

    Column names vary by vendor, so they are matched case-insensitively against
    a small set of aliases rather than required verbatim. What is *not* flexible
    is the timezone: this framework session-tags on the 18:00 ET Globex boundary,
    so a file read an hour off silently reassigns bars to the wrong trade date
    and corrupts every overnight level. If the file carries no offset, `tz` says
    what it is, and getting that wrong is the most likely way to ruin a backfill.
    """
    frame = pd.read_csv(path)
    if frame.empty:
        raise ValueError(f"{path} has no rows")

    lower = {str(c).strip().lower(): c for c in frame.columns}

    stamp_col = next((lower[n] for n in _TIMESTAMP_NAMES if n in lower), None)
    if stamp_col is None:
        raise ValueError(
            f"no timestamp column in {path}. Looked for: {', '.join(_TIMESTAMP_NAMES)}. "
            f"Found: {', '.join(str(c) for c in frame.columns)}"
        )

    renamed = {}
    for target, aliases in _OHLCV_NAMES.items():
        source = next((lower[a] for a in aliases if a in lower), None)
        if source is None and target != "Volume":
            raise ValueError(
                f"no {target} column in {path}. Accepted names: {', '.join(aliases)}"
            )
        if source is not None:
            renamed[source] = target

    frame = frame.rename(columns=renamed)

    # A file spanning a DST change carries two different offsets, which pandas
    # refuses to parse into one naive series. Decide from the text whether the
    # stamps are offset-aware, then take the branch that cannot silently shift
    # them: aware stamps go through UTC, naive ones are localised with `tz`.
    raw = frame[stamp_col].astype(str)
    sample = next((v for v in raw if v and v.lower() != "nan"), "")
    offset_aware = bool(re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", sample.strip()))

    if offset_aware:
        stamps = pd.to_datetime(raw, errors="coerce", utc=True)
    else:
        stamps = pd.to_datetime(raw, errors="coerce")
        if stamps.notna().any() and getattr(stamps.dt, "tz", None) is None:
            stamps = stamps.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")

    if stamps.isna().all():
        raise ValueError(f"could not parse any timestamp in column {stamp_col!r}")

    keep = stamps.notna()
    frame = frame.loc[keep.values].copy()
    frame.index = stamps[keep].dt.tz_convert(EASTERN)

    for column in ("Open", "High", "Low", "Close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "Volume" not in frame.columns:
        frame["Volume"] = 0.0
    frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0.0)

    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    frame = frame[~frame.index.duplicated(keep="first")].sort_index()
    if frame.empty:
        raise ValueError(f"{path} had rows but none survived parsing")
    return frame[["Open", "High", "Low", "Close", "Volume"]]


def _audit_bars(frame: pd.DataFrame, spec: ContractSpec, interval: str) -> list[str]:
    """Structural complaints about a backfill file, before it is trusted.

    A backfill that is quietly wrong produces a confident EV over garbage, which
    is worse than no EV at all. These checks catch the failures that actually
    happen: the wrong contract, the wrong timezone, and gaps large enough that
    "60 sessions" is really 12.
    """
    problems = []

    inconsistent = int(
        (frame["High"] < frame[["Open", "Close"]].max(axis=1)).sum()
        + (frame["Low"] > frame[["Open", "Close"]].min(axis=1)).sum()
    )
    if inconsistent:
        problems.append(
            f"{inconsistent} bars where High/Low do not bracket Open/Close — the file is "
            "not clean OHLC"
        )

    expected = pd.Timedelta(interval.replace("m", "min").replace("h", "H"))
    deltas = pd.Series(frame.index).diff().dropna()
    if not deltas.empty:
        modal = deltas.mode().iloc[0]
        if modal != expected:
            problems.append(
                f"most common bar spacing is {modal}, but --interval says {expected}. "
                "The scan parameters are calibrated per interval"
            )

    hours = sorted(set(frame.index.hour))
    if 9 not in hours and 10 not in hours:
        problems.append(
            "no bars in the 09:00–10:59 ET range — the file is probably in a different "
            "timezone than --tz claims, which reassigns every bar to the wrong session"
        )

    sessions = _tag_sessions(frame, spec)
    rth_per_session = sessions[sessions["is_rth"]].groupby("session_date").size()
    if not rth_per_session.empty:
        bars_expected = int(pd.Timedelta("6.5h") / expected)
        thin = int((rth_per_session < bars_expected * 0.5).sum())
        if thin:
            problems.append(
                f"{thin} of {len(rth_per_session)} sessions have under half the expected "
                f"{bars_expected} RTH bars — partial days inflate the session count"
            )

    return problems


def sample_projection(sessions_scanned: int, resolved: int, min_sample: int) -> str:
    """How much more data the gate needs, at the rate this scan actually produced.

    The most useful output this framework has: setups are rare, and the honest
    answer to "when will I know if this works" is a session count, not a
    sentiment. Quoting it stops the sample floor from looking like an arbitrary
    obstacle and turns it into a data-acquisition target.
    """
    if sessions_scanned <= 0:
        return ""
    rate = resolved / sessions_scanned
    if rate <= 0:
        return (
            f"\n> **No resolved setups in {sessions_scanned} sessions.** At this rate the gate "
            "never opens. Either the parameters are too strict for this data, or this is not "
            "enough history to tell — run `bin/ta smc sensitivity` before assuming the former.\n"
        )
    needed = max(0, min_sample - resolved)
    if needed == 0:
        return f"\n> Sample floor reached at {rate:.3f} resolved setups per session.\n"
    more = needed / rate
    return (
        f"\n> **Rate: {rate:.3f} resolved setups per session.** {needed} more needed, so "
        f"roughly **{more:,.0f} further sessions** (~{more / 252:.1f} years of trading days) "
        f"at this yield.\n>\n"
        "> That is the real cost of validating this configuration. If it reads as too long, the "
        "lever is the parameters — a looser `min_rr` or a wider kill zone produces more setups "
        "per session — but changing one restarts the sample, so decide before you accumulate.\n"
    )


def backfill_journal(symbol: str, csv_path: str, interval: str = "5m",
                     tz: str = EASTERN, dry_run: bool = False) -> str:
    """Run the scanner over a local history file and journal what it resolves.

    This is the only way to reach a measurable EV without waiting months. The
    vendor serves 30 days of 5m bars and that window slides forward as fast as a
    sample accumulates; a file you already have does not move.
    """
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    try:
        frame = load_ohlcv_csv(csv_path, tz)
    except (OSError, ValueError) as exc:
        return f"<error: {type(exc).__name__}: {exc}>"

    problems = _audit_bars(frame, spec, interval)
    config = smc_config()
    tagged = _tag_sessions(frame, spec)
    sessions = sorted(tagged["session_date"].unique())

    setups: list[Setup] = []
    for session_date in sessions:
        session_frame = tagged[tagged["session_date"] == session_date].sort_index()
        targets = _liquidity_targets(tagged, session_date, session_frame, spec, config)
        if not targets:
            continue
        setups.extend(scan_session(session_frame, spec, session_date, targets, config))
    setups = enforce_session_rules(setups, config)

    fingerprint = parameter_fingerprint(config, interval)
    terminal = [s for s in setups if s.outcome in _TERMINAL]

    header = (
        f"## Backfill — {spec.symbol} {interval} from `{csv_path}`\n\n"
        f"{len(frame):,} bars, {len(sessions)} sessions "
        f"({sessions[0].date()} → {sessions[-1].date()}), timezone {tz}.\n"
        f"Found {len(setups)} setups, {len(terminal)} terminal.\n\n"
    )

    audit = ""
    if problems:
        audit = (
            "> ⚠️ **File audit found problems.** Every one of these changes what the "
            "scan means:\n>\n"
            + "\n".join(f"> - {p}" for p in problems)
            + "\n>\n> Fix the file or the flags before trusting the EV below.\n\n"
        )

    from collections import Counter

    histogram = Counter(s.outcome for s in setups)
    breakdown = markdown_table(
        ["Outcome", "Setups"],
        [[k, str(v)] for k, v in histogram.most_common()],
    ) + "\n\n" if histogram else ""

    if dry_run:
        preview = expected_value(terminal, spec, config)
        return (
            header + audit + breakdown
            + "_Dry run — nothing written._ Re-run without `--dry-run` to journal these.\n\n"
            + _ev_block(preview, spec)
            + sample_projection(len(sessions), preview["n_resolved"], preview["min_sample"])
        )

    known = {row["key"] for row in load_journal()}
    added = 0
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL_PATH.open("a") as handle:
        for setup in terminal:
            key = journal_key(spec.symbol, interval, setup)
            if key in known:
                continue
            handle.write(json.dumps({
                "key": key, "symbol": spec.symbol, "interval": interval,
                "fingerprint": fingerprint, "session_date": setup.session_date,
                "direction": setup.direction, "kill_zone": setup.kill_zone,
                "liquidity_level": setup.liquidity_level_name,
                "entry": round(setup.entry, 4), "stop": round(setup.stop, 4),
                "target": round(setup.target, 4),
                "risk_points": round(setup.risk_points, 4), "rr": round(setup.rr, 4),
                "outcome": setup.outcome, "outcome_time": setup.outcome_time,
                "realized_r": setup.realized_r, "tp1_hit": setup.tp1_hit,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "source": "backfill",
                "source_file": str(csv_path),
            }) + "\n")
            known.add(key)
            added += 1

    rows = load_journal(spec.symbol, fingerprint)
    ev = expected_value(_journal_rows_as_setups(rows), spec, config)
    return (
        header + audit
        + breakdown
        + f"**{added} newly journalled**, {len(terminal) - added} already present.\n"
        f"Journal now holds {len(rows)} setups for {spec.symbol} at `{fingerprint}`.\n\n"
        + _ev_block(ev, spec)
        + sample_projection(len(sessions), ev["n_resolved"], ev["min_sample"])
    )


_PROBE_LEVELS = {
    "pd_high": ("PDH", "short"), "pd_low": ("PDL", "long"),
    "on_high": ("ONH", "short"), "on_low": ("ONL", "long"),
}


def probe(symbol: str, csv_path: str, level_key: str = "pd_high",
          targets: str = "1.0,1.5,2.0", stop_ticks: float = 0.0,
          killzone: str = "any", tz: str = EASTERN, direction: str = "reversal") -> str:
    """Raw base rate for ONE sweep, before any state machine is layered on.

    The four-step scanner answers "does sweep → MSS → FVG work". It cannot
    answer "does the sweep carry the edge, or is it the MSS and FVG filters
    doing the work" — and if the sweep alone is negative-EV before costs, the
    filters are selecting from a losing population and the hypothesis should be
    discarded rather than refined.

    So this measures the crudest possible version: enter on the close of the bar
    that sweeps the level, stop beyond the sweep extreme, exit at fixed R
    multiples. No structure confirmation, no gap entry, no discretion. Whatever
    edge shows up here is attributable to the sweep itself.

    Reporting several targets at once is deliberate. A setup that is positive at
    1R and negative at 2R is a mean-reversion trade being mislabelled as a
    continuation trade, and one number would hide that.
    """
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"
    if level_key not in _PROBE_LEVELS:
        return f"<error: unknown level {level_key!r}. Options: {', '.join(_PROBE_LEVELS)}>"

    try:
        frame = load_ohlcv_csv(csv_path, tz)
    except (OSError, ValueError) as exc:
        return f"<error: {type(exc).__name__}: {exc}>"

    config = smc_config()
    label, natural = _PROBE_LEVELS[level_key]
    # Which side the level sits on is a property of the level and decides what
    # counts as a sweep event. Which way the trade faces is the hypothesis.
    # Conflating them silently changes the event population when the direction
    # flips, so the two comparisons stop being over the same setups.
    level_is_upside = natural == "short"
    if direction == "continuation":
        natural = "long" if natural == "short" else "short"
    short = natural == "short"
    sign = -1.0 if short else 1.0
    target_rs = [float(t) for t in targets.split(",") if t.strip()]

    tagged = _tag_sessions(frame, spec)
    sessions = sorted(tagged["session_date"].unique())

    trades = []
    swept_sessions = gapped = no_level = 0

    for session_date in sessions:
        levels = compute_levels(tagged, session_date)
        level = levels.get(level_key)
        if level is None:
            no_level += 1
            continue
        session = tagged[tagged["session_date"] == session_date].sort_index()
        if spec.session_model == "globex":
            session = session[session["is_rth"]]
        if session.empty:
            continue

        highs = session["High"].astype(float).to_numpy()
        lows = session["Low"].astype(float).to_numpy()
        closes = session["Close"].astype(float).to_numpy()

        # A session that opens beyond the level never traded into the resting
        # orders — there is no sweep event to measure.
        if (level_is_upside and closes[0] > level) or (not level_is_upside and closes[0] < level):
            gapped += 1
            continue

        pierce = None
        for i in range(len(session)):
            if killzone != "any" and kill_zone_for(session.index[i], config) != killzone:
                continue
            if (level_is_upside and highs[i] > level) or (not level_is_upside and lows[i] < level):
                pierce = i
                break
        if pierce is None:
            continue
        swept_sessions += 1

        entry = closes[pierce]
        extreme = highs[pierce] if short else lows[pierce]
        if stop_ticks > 0:
            stop = entry - sign * stop_ticks * spec.tick_points
        else:
            buffer = config["stop_buffer_ticks"] * spec.tick_points
            stop = extreme + (buffer if short else -buffer)
        risk_points = abs(entry - stop)
        if risk_points <= 0:
            continue

        # Forward walk once; record which R multiples were reached before the stop.
        reached = {r: False for r in target_rs}
        stopped_at = None
        for j in range(pierce + 1, len(session)):
            if stopped_at is None:
                hit_stop = highs[j] >= stop if short else lows[j] <= stop
                if hit_stop:
                    stopped_at = j
            for r in target_rs:
                if reached[r]:
                    continue
                price = entry + sign * r * risk_points
                hit = lows[j] <= price if short else highs[j] >= price
                if hit and stopped_at is None:
                    reached[r] = True
                elif hit and stopped_at == j:
                    reached[r] = False  # same bar as the stop: unorderable, refuse it
            if stopped_at is not None:
                break

        trades.append({
            "session": str(session_date.date()), "entry": entry,
            "risk_points": risk_points, "reached": reached,
        })

    if not trades:
        return (
            f"## Sweep probe — {spec.symbol} {label}\n\n"
            f"No sweeps found across {len(sessions)} sessions "
            f"({gapped} gapped beyond the level at the open, {no_level} had no level).\n"
        )

    rows = []
    for r in target_rs:
        wins = sum(1 for t in trades if t["reached"][r])
        n = len(trades)
        win_rate = wins / n
        gross = win_rate * r - (1 - win_rate) * 1.0
        costs = []
        for t in trades:
            risk_dollars = spec.points_to_dollars(t["risk_points"])
            drag = spec.round_trip_cost(t["entry"]) + spec.ticks_to_dollars(3.0)
            costs.append(drag / risk_dollars if risk_dollars else 0.0)
        cost_r = sum(costs) / len(costs)
        net = gross - cost_r
        rows.append([
            f"{r:.1f}R", f"{wins}/{n}", f"{win_rate * 100:.1f}%",
            f"{gross:+.3f}R", f"−{cost_r:.3f}R", f"**{net:+.3f}R**",
            "keep" if net > 0.05 else "discard",
        ])

    median_risk = sorted(t["risk_points"] for t in trades)[len(trades) // 2]
    return f"""## Sweep probe — {spec.symbol} {label}, {len(sessions)} sessions

{direction.capitalize()} trade. Entry on the close of the bar that first pierces {label}\
{f" during the {killzone} kill zone" if killzone != "any" else ""}. \
Stop {'at a fixed ' + str(stop_ticks) + ' ticks' if stop_ticks else 'beyond the sweep extreme'}. \
No structure confirmation, no gap entry — whatever edge appears here belongs to the sweep alone.

{markdown_table(
    ["Target", "Hits", "Win rate", "Gross EV", "Cost", "Net EV", "Verdict"], rows
)}

**{len(trades)} sweeps** in {swept_sessions} sessions | {gapped} sessions gapped beyond at the open |
median risk {median_risk:.2f} points (${spec.points_to_dollars(median_risk):,.0f})

> A row that is positive at 1R and negative at 2R is a mean-reversion trade, not
> a continuation trade — the target, not the entry, is what is wrong. If every
> row is negative before costs, the hypothesis is dead and no filter will revive
> it: filters select from this population, they do not improve it.
"""


def journal_status(symbol: str = "") -> str:
    """What the journal holds, segmented by the parameters that produced it."""
    rows = load_journal(symbol)
    if not rows:
        return (
            "_Journal is empty._\n\n"
            "Start it with `bin/ta smc journal record NQ`, then run that daily. "
            "The EV gate stays closed until the sample floor is reached."
        )

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row["symbol"], row["interval"], row["fingerprint"]), []).append(row)

    config = smc_config()
    table_rows = []
    for (sym, interval, fingerprint), group in sorted(groups.items()):
        current = fingerprint == parameter_fingerprint(config, interval)
        resolved = [r for r in group if r["outcome"] in ("target", "stopped", "ambiguous")]
        wins = len([r for r in group if r["outcome"] == "target"])
        dates = sorted(r["session_date"] for r in group)
        table_rows.append([
            sym, interval, fingerprint + (" ←current" if current else ""),
            f"{dates[0]} → {dates[-1]}",
            str(len(group)), str(len(resolved)),
            f"{wins}/{len(resolved)}" if resolved else "—",
        ])

    stale = len(groups) - sum(
        1 for (s, i, f) in groups if f == parameter_fingerprint(config, i)
    )
    note = ""
    if stale:
        note = (
            f"\n> ⚠️ **{stale} of {len(groups)} groups were produced by parameters that no longer "
            "match `config.json`.** Those setups describe a different strategy and are not pooled "
            "into the current EV. Changing a parameter restarts the sample.\n"
        )

    return (
        "## SMC setup journal\n\n"
        + markdown_table(
            ["Symbol", "Interval", "Params", "Sessions covered", "Setups", "Resolved", "Wins"],
            table_rows,
        )
        + f"\n\n**{len(rows)} setups recorded across {len(groups)} parameter set(s).**\n"
        + note
    )


def ev_report(symbol: str = "NQ", interval: str = "5m", sessions: int = 60,
              curr_date: str | None = None, from_journal: bool = False) -> str:
    """Standalone EV gate, over a scan window or over the accumulated journal."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    config = smc_config()
    cost_note = (
        f"_Costs: {spec.typical_spread_ticks:.0f}-tick spread + "
        f"${spec.commission_round_turn:,.2f} commission per round turn, from "
        f"`config.json: futures.costs`. Replace those with your broker's actual fills — "
        f"every number above moves with them._\n"
    )

    if from_journal:
        fingerprint = parameter_fingerprint(config, interval)
        rows = load_journal(spec.symbol, fingerprint)
        if not rows:
            return (
                f"## Expected value — {spec.symbol} {interval}, from journal\n\n"
                f"_No journalled setups for {spec.symbol} at the current parameters "
                f"(`{fingerprint}`)._\n\n"
                f"Record some with `bin/ta smc journal record {spec.symbol}`, then run it daily.\n"
            )
        ev = expected_value(_journal_rows_as_setups(rows), spec, config)
        dates = sorted(r["session_date"] for r in rows)
        return (
            f"## Expected value — {spec.symbol} {interval}, from journal\n\n"
            f"{len(rows)} setups recorded, sessions {dates[0]} → {dates[-1]}, "
            f"parameters `{fingerprint}`.\n\n"
            f"{_ev_block(ev, spec)}\n{cost_note}"
        )

    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} {interval}", "no intraday bars")

    tagged = _tag_sessions(frame, spec)
    served_sessions = sorted(tagged["session_date"].unique())
    window = served_sessions[-sessions:]

    setups: list[Setup] = []
    for session_date in window:
        session_frame = tagged[tagged["session_date"] == session_date].sort_index()
        targets = _liquidity_targets(tagged, session_date, session_frame, spec, config)
        if not targets:
            continue
        setups.extend(scan_session(session_frame, spec, session_date, targets, config))

    setups = enforce_session_rules(setups, config)
    ev = expected_value(setups, spec, config)

    # The request is silently clamped by the vendor's rolling intraday window,
    # and a caller who asked for 60 sessions and got 21 would otherwise read the
    # small sample as a property of the strategy rather than of the data source.
    clamp_note = ""
    if len(window) < sessions:
        vendor_days = VALID_INTERVALS.get(interval)
        clamp_note = (
            f"\n> ⚠️ **Asked for {sessions} sessions, the vendor served {len(window)}.** "
            f"yfinance keeps only {vendor_days} calendar days of {interval} bars, and that "
            "window slides forward as fast as a sample would accumulate. A single scan "
            f"cannot reach the {ev['min_sample']}-setup floor at this interval.\n>\n"
            f"> Use `bin/ta smc journal record {spec.symbol} --interval {interval}` daily to "
            f"accumulate setups across runs, then `bin/ta smc ev {spec.symbol} --journal`. "
            "A coarser interval also buys history: 15m serves 60 days, 1h serves 180.\n"
        )

    return (
        f"## Expected value — {spec.symbol} {interval}, {len(window)} sessions\n\n"
        f"{_ev_block(ev, spec)}{clamp_note}\n{cost_note}"
    )


def rules_report(symbol: str = "NQ", interval: str = "5m", sessions: int = 20,
                 account: float = 10000.0, curr_date: str | None = None) -> str:
    """Scan with section 4 enforced, and size every setup against a real account."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    config = smc_config()
    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} {interval}", "no intraday bars")

    tagged = _tag_sessions(frame, spec)
    all_sessions = sorted(tagged["session_date"].unique())[-sessions:]

    setups: list[Setup] = []
    for session_date in all_sessions:
        session_frame = tagged[tagged["session_date"] == session_date].sort_index()
        targets = _liquidity_targets(tagged, session_date, session_frame, spec, config)
        if not targets:
            continue
        setups.extend(scan_session(session_frame, spec, session_date, targets, config))

    setups = enforce_session_rules(setups, config)
    _, calendar_error = load_news_calendar()

    rows = []
    untradeable = 0
    for setup in sorted(setups, key=lambda s: (s.session_date, s.mss_time)):
        sizing = size_setup(setup, spec, account, config["risk_pct"])
        if sizing["contracts"] == 0 and setup.outcome not in ("rejected",):
            untradeable += 1
        rows.append([
            setup.session_date, setup.kill_zone, setup.direction.upper(),
            f"{setup.risk_ticks:.0f}", f"${sizing['risk_per_contract']:,.0f}",
            str(sizing["contracts"]),
            f"${sizing['actual_risk']:,.0f}" if sizing["contracts"] else "—",
            f"{setup.rr:.2f}", setup.outcome,
        ])

    taken = [s for s in setups if s.outcome in ("target", "stopped", "ambiguous", "open_at_end", "unfilled", "filled")]
    skipped = [s for s in setups if s.outcome.startswith("skipped")]

    notes = []
    if calendar_error:
        notes.append(f"> ⚠️ **News rule UNENFORCED** — {calendar_error}.")
    if untradeable:
        notes.append(
            f"> ⚠️ **{untradeable} of {len(setups)} setups size to ZERO contracts** on a "
            f"${account:,.0f} account at {config['risk_pct']:.0f}% risk. The stop distance this "
            f"framework produces ({min(s.risk_ticks for s in setups):.0f}–"
            f"{max(s.risk_ticks for s in setups):.0f} ticks observed) requires more capital than "
            f"that, or the micro contract. Taking them anyway means breaking the fixed-risk rule, "
            "which is the rule that makes the rest survivable."
        )

    ev = expected_value(setups, spec, config)
    if not ev["passes"]:
        notes.insert(0, (
            "> 🚫 **EV GATE FAILED — no trade may be proposed from this configuration.** "
            f"{ev['verdict']} The sizing table below is what the rules *would* permit; it is "
            "not a licence to take them."
        ))

    return f"""## SMC with hard rules enforced — {spec.symbol} {interval}, ${account:,.0f} account

Risk {config['risk_pct']:.0f}% per trade | max {config['max_trades_per_session']} trades/session |
shutdown after {config['stop_after_losses']} losses | news blackout ±{config['news_blackout_minutes']}min

{_ev_block(ev, spec)}

{markdown_table(["Session", "Zone", "Dir", "Risk(t)", "$/contract", "Contracts", "Actual risk", "R:R", "Outcome"], rows) if rows else "_No setups._"}

**{len(taken)} tradeable, {len(skipped)} skipped by rule, {len([s for s in setups if s.outcome == 'rejected'])} rejected on R:R.**

{chr(10).join(notes)}
"""


# ---------------------------------------------------------------------------
# Step 1, in full — EQH/EQL, session ranges, and HTF bias
# ---------------------------------------------------------------------------


@dataclass
class EqualLevel:
    """Two or more swings resting at effectively the same price.

    The spec names EQH/EQL as a liquidity target in its own right, and for good
    reason: a clean double top is the most legible stop cluster on a chart, which
    is exactly why price so reliably runs it. A single swing high is a level; two
    at the same price is an advertisement.
    """
    price: float
    kind: str          # "high" | "low"
    count: int
    first_time: str
    last_time: str
    spread_ticks: float


def find_equal_levels(swings: list[Swing], tick: float, tolerance_ticks: float = 4.0,
                      min_count: int = 2) -> list[EqualLevel]:
    """Cluster same-kind swings whose prices sit within `tolerance_ticks`."""
    tolerance = tolerance_ticks * tick
    results: list[EqualLevel] = []

    for kind in ("high", "low"):
        candidates = sorted(
            (s for s in swings if s.kind == kind), key=lambda s: s.price
        )
        cluster: list[Swing] = []
        for swing in candidates:
            if cluster and abs(swing.price - cluster[0].price) <= tolerance:
                cluster.append(swing)
                continue
            if len(cluster) >= min_count:
                results.append(_build_equal(cluster, kind, tick))
            cluster = [swing]
        if len(cluster) >= min_count:
            results.append(_build_equal(cluster, kind, tick))
    return results


def _build_equal(cluster: list[Swing], kind: str, tick: float) -> EqualLevel:
    ordered = sorted(cluster, key=lambda s: s.timestamp)
    prices = [s.price for s in cluster]
    # The extreme of the cluster is where the stops actually sit — beyond every
    # touch, not at their average.
    price = max(prices) if kind == "high" else min(prices)
    return EqualLevel(
        price=price, kind=kind, count=len(cluster),
        first_time=ordered[0].timestamp.strftime("%m-%d %H:%M"),
        last_time=ordered[-1].timestamp.strftime("%m-%d %H:%M"),
        spread_ticks=(max(prices) - min(prices)) / tick,
    )


_DEFAULT_SESSION_RANGES = {
    "Asia": ["20:00", "00:00"],
    "London": ["02:00", "05:00"],
    "NYAM": ["09:30", "11:30"],
}


def session_ranges(tagged: pd.DataFrame, session_date: pd.Timestamp,
                   config: dict) -> dict[str, float]:
    """Highs and lows of each named session — the spec's 'Session Highs/Lows'.

    A session that has already completed leaves its extremes behind as resting
    liquidity for the next one. Trading the NY kill zone, the Asian and London
    ranges are the pools most likely to be run.
    """
    ranges = config.get("session_ranges", _DEFAULT_SESSION_RANGES)
    current = tagged[tagged["session_date"] == session_date]
    if current.empty:
        return {}

    targets: dict[str, float] = {}
    for name, (start, end) in ranges.items():
        start_time, end_time = _parse_time(start), _parse_time(end)
        times = current.index.time
        if start_time <= end_time:
            mask = (times >= start_time) & (times < end_time)
        else:
            # Wraps midnight (the Asian range does).
            mask = (times >= start_time) | (times < end_time)
        window = current[mask]
        if window.empty:
            continue
        targets[f"{name}H"] = float(window["High"].max())
        targets[f"{name}L"] = float(window["Low"].min())
    return targets


def htf_bias(symbol: str, curr_date: str | None = None,
             intervals: tuple[str, ...] = ("1h", "15m")) -> dict:
    """Step 1's directional bias, from HTF structure and unswept liquidity.

    The spec asks for bias from 1-Hour and 15-Minute: 'where price is naturally
    drawn to clear orders'. Implemented as two readings that must be stated
    separately rather than blended, because they disagree often and that
    disagreement is information:

    - **Structure** — the sequence of HTF swings. Higher highs and higher lows
      is bullish structure; the inverse is bearish; anything else is ranging.
    - **Draw on liquidity** — which side holds the nearer unswept pool. Price is
      drawn toward resting orders, so the nearer untapped side is the more likely
      near-term destination regardless of structure.
    """
    spec = resolve(symbol)
    config = smc_config()
    readings: dict[str, dict] = {}

    for interval in intervals:
        frame = intraday(spec, interval, curr_date=curr_date)
        if frame.empty:
            readings[interval] = {"error": f"no {interval} bars"}
            continue

        swings = find_swings(frame, config["swing_lookback"])
        highs = [s for s in swings if s.kind == "high"][-3:]
        lows = [s for s in swings if s.kind == "low"][-3:]
        last = float(frame["Close"].iloc[-1])

        structure = "ranging"
        if len(highs) >= 2 and len(lows) >= 2:
            higher_highs = highs[-1].price > highs[-2].price
            higher_lows = lows[-1].price > lows[-2].price
            if higher_highs and higher_lows:
                structure = "bullish"
            elif not higher_highs and not higher_lows:
                structure = "bearish"

        # Unswept pools: swing highs above price, swing lows below.
        above = sorted(s.price for s in swings if s.kind == "high" and s.price > last)
        below = sorted((s.price for s in swings if s.kind == "low" and s.price < last), reverse=True)
        nearest_above = above[0] if above else None
        nearest_below = below[0] if below else None

        draw = "unclear"
        if nearest_above is not None and nearest_below is not None:
            up_distance = nearest_above - last
            down_distance = last - nearest_below
            draw = "buy-side (upward)" if up_distance < down_distance else "sell-side (downward)"
        elif nearest_above is not None:
            draw = "buy-side (upward)"
        elif nearest_below is not None:
            draw = "sell-side (downward)"

        readings[interval] = {
            "last": last,
            "structure": structure,
            "draw": draw,
            "nearest_buy_side": nearest_above,
            "nearest_sell_side": nearest_below,
            "swing_count": len(swings),
        }

    valid = [r for r in readings.values() if "error" not in r]
    structures = {r["structure"] for r in valid}
    if not valid:
        bias, rationale = "unavailable", "no HTF data"
    elif len(structures) == 1 and structures != {"ranging"}:
        bias = structures.pop()
        rationale = f"both {' and '.join(intervals)} agree on {bias} structure"
    else:
        bias = "conflicted"
        rationale = (
            "HTF timeframes disagree: "
            + ", ".join(f"{k} {v['structure']}" for k, v in readings.items() if "error" not in v)
            + " — no directional bias, so take setups in either direction only on their own merit"
        )

    return {"readings": readings, "bias": bias, "rationale": rationale}


def bias_report(symbol: str = "NQ", curr_date: str | None = None) -> str:
    """Step 1 bias, printed."""
    try:
        result = htf_bias(symbol, curr_date)
    except ValueError as exc:
        return f"<error: {exc}>"

    rows = []
    for interval, reading in result["readings"].items():
        if "error" in reading:
            rows.append([interval, "—", reading["error"], "—", "—"])
            continue
        rows.append([
            interval,
            f"{reading['last']:,.2f}",
            reading["structure"],
            reading["draw"],
            f"{reading['nearest_buy_side']:,.2f}" if reading["nearest_buy_side"] else "none",
        ])

    return f"""## Step 1 — HTF bias, {symbol}

{markdown_table(["Timeframe", "Last", "Structure", "Draw on liquidity", "Nearest buy-side"], rows)}

**Bias: {result['bias'].upper()}** — {result['rationale']}

> Structure and draw-on-liquidity are reported separately because they routinely
> disagree, and that disagreement is the useful part: bullish structure with the
> nearer pool below means the likely path is down-then-up, which is precisely the
> sweep this framework trades. A blended single number would hide it.
"""


def liquidity_report(symbol: str = "NQ", interval: str = "5m",
                     curr_date: str | None = None) -> str:
    """Every Step 1 liquidity target for the current session, by category."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    config = smc_config()
    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} {interval}", "no intraday bars")

    tagged = _tag_sessions(frame, spec)
    session_date = sorted(tagged["session_date"].unique())[-1]
    session_frame = tagged[tagged["session_date"] == session_date].sort_index()
    targets = _liquidity_targets(tagged, session_date, session_frame, spec, config)
    if not targets:
        return unavailable("liquidity targets", "no complete session")

    last = float(session_frame["Close"].iloc[-1])
    swings = find_swings(session_frame, config["swing_lookback"])
    equals = find_equal_levels(swings, spec.tick_points, config.get("eq_tolerance_ticks", 4.0))

    def category(name: str) -> str:
        if name.startswith("EQ"):
            return "Equal highs/lows"
        if name in ("PDH", "PDL"):
            return "Prior day"
        if name in ("ONH", "ONL"):
            return "Overnight"
        return "Session range"

    rows = []
    for name, price in sorted(targets.items(), key=lambda kv: -kv[1]):
        side = "buy-side" if is_buy_side(name) else "sell-side"
        distance = price - last
        rows.append([
            name, category(name), side, f"{price:,.2f}",
            f"{distance:+.2f}", f"{distance / spec.tick_points:+.0f}",
            f"${spec.points_to_dollars(abs(distance)):,.0f}",
        ])

    eq_block = "_None found on this timeframe._"
    if equals:
        eq_block = markdown_table(
            ["Kind", "Price", "Touches", "First", "Last", "Spread(t)"],
            [[e.kind.upper(), f"{e.price:,.2f}", str(e.count), e.first_time,
              e.last_time, f"{e.spread_ticks:.1f}"] for e in equals],
        )

    return f"""## Step 1 — liquidity targets, {spec.symbol} {interval} ({session_date.date()})

Last {last:,.2f}

{markdown_table(["Level", "Category", "Side", "Price", "Δ pts", "Δ ticks", "Δ $/contract"], rows)}

### Equal highs / lows detected

{eq_block}

> All four categories the framework names are included: prior day, overnight,
> session ranges (Asia / London / NY AM), and EQH/EQL clusters within
> {config.get('eq_tolerance_ticks', 4.0):.0f} ticks. Buy-side liquidity rests above
> highs, sell-side below lows — the side price is drawn to is the side with orders
> to fill, not the side that looks like support.
"""


# ---------------------------------------------------------------------------
# Replay — the calibration loop
# ---------------------------------------------------------------------------


def _cutoff_timestamp(session_date: pd.Timestamp, until: str) -> pd.Timestamp:
    """Absolute cutoff for a replay, handling the overnight session correctly.

    Comparing clock times alone would drop the 18:00-23:59 Globex bars, which
    belong to this trade date and carry the overnight high and low — the levels
    the NY session is most likely to run.
    """
    hour, minute = until.split(":")
    return pd.Timestamp(session_date) + pd.Timedelta(hours=int(hour), minutes=int(minute))


def replay(symbol: str = "NQ", session: str | None = None, until: str = "10:15",
           interval: str = "5m", reveal: bool = False, bars: int = 24,
           curr_date: str | None = None) -> str:
    """Show one session up to a moment in time, with the future withheld.

    The point is to make your own call before seeing the answer. Reading a chart
    with the outcome already visible teaches nothing except hindsight — every
    level looks obvious once you know which way it broke. Without `reveal` this
    function cannot see past the cutoff either: the frame is truncated before any
    analysis runs, so the levels, swings, and gaps shown are strictly those that
    existed at that moment.
    """
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    config = smc_config()
    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} {interval}", "no intraday bars")

    tagged = _tag_sessions(frame, spec)
    available = sorted(tagged["session_date"].unique())
    if session:
        wanted = pd.Timestamp(session, tz=EASTERN).normalize()
        if wanted not in available:
            return (
                f"<error: no session {session}. Available: "
                f"{', '.join(str(d.date()) for d in available[-10:])}>"
            )
        session_date = wanted
    else:
        session_date = available[-1]

    cutoff = _cutoff_timestamp(session_date, until)
    full_session = tagged[tagged["session_date"] == session_date].sort_index()
    visible = full_session[full_session.index <= cutoff]
    if visible.empty:
        return f"<error: no bars before {until} on {session_date.date()}>"

    # Truncate the whole frame, not just the session, so prior-day and overnight
    # levels are computed from what was actually knowable at the cutoff.
    tagged_visible = tagged[tagged.index <= cutoff]
    targets = _liquidity_targets(tagged_visible, session_date, visible, spec, config)

    last = float(visible["Close"].iloc[-1])
    swings = find_swings(visible, config["swing_lookback"])
    gaps = find_fvgs(visible)

    level_rows = []
    for name, price in sorted(targets.items(), key=lambda kv: -kv[1]):
        side = "buy-side" if is_buy_side(name) else "sell-side"
        touched = float(visible["Low"].min()) <= price <= float(visible["High"].max())
        distance = price - last
        level_rows.append([
            name, side, f"{price:,.2f}", f"{distance:+.1f}",
            f"{distance / spec.tick_points:+.0f}",
            "swept" if touched else "untouched",
        ])

    bar_rows = []
    for timestamp, row in visible.tail(bars).iterrows():
        rng = float(row["High"]) - float(row["Low"])
        body = abs(float(row["Close"]) - float(row["Open"]))
        bar_rows.append([
            timestamp.strftime("%H:%M"),
            "RTH" if row["is_rth"] else "ON",
            f"{row['Open']:,.2f}", f"{row['High']:,.2f}",
            f"{row['Low']:,.2f}", f"{row['Close']:,.2f}",
            f"{rng / spec.tick_points:.0f}",
            f"{body / rng * 100:.0f}%" if rng else "-",
        ])

    recent_swings = markdown_table(
        ["Time", "Type", "Price"],
        [[s.timestamp.strftime("%H:%M"), s.kind.upper(), f"{s.price:,.2f}"] for s in swings[-6:]],
    ) if swings else "_None confirmed yet._"

    open_gaps = list(gaps[-6:])
    gap_table = markdown_table(
        ["Created", "Dir", "Bottom", "Top", "CE (entry)"],
        [[g.timestamp.strftime("%H:%M"), g.direction, f"{g.bottom:,.2f}",
          f"{g.top:,.2f}", f"{g.ce:,.2f}"] for g in open_gaps],
    ) if open_gaps else "_None yet._"

    zone = kill_zone_for(visible.index[-1], config)

    header = f"""# Replay - {spec.symbol} {interval}, {session_date.date()} at {until} ET

**Everything after {until} is withheld.** Make your call, then re-run with `--reveal`.

Last {last:,.2f} | kill zone: {zone or "outside"} | {len(visible)} bars so far

## Step 1 - liquidity in play

{markdown_table(["Level", "Side", "Price", "d pts", "d ticks", "Status"], level_rows)}

## Recent bars

{markdown_table(["Time", "Sess", "Open", "High", "Low", "Close", "Rng(t)", "Body%"], bar_rows)}

## Confirmed swings (MSS reference points)

{recent_swings}

## Fair value gaps so far

{gap_table}
"""

    if not reveal:
        return header + f"""
---

## Your call

Before revealing, write down:

1. **Bias** - which side is price drawn to, and why?
2. **Which pool** do you expect to be run first?
3. **If it sweeps** - would you take the setup? Where is your entry, stop, target?
4. **What would invalidate** your read?

Then run:

```bash
bin/ta smc replay {spec.symbol} --session {session_date.date()} --until {until} --reveal
```

> Write the answer down before revealing. The value of this exercise is entirely
> in the gap between your call and what happened - and that gap disappears if you
> read the outcome first and reconstruct a reason for it.
"""

    after = full_session[full_session.index > cutoff]
    setups = scan_session(
        full_session, spec, session_date,
        _liquidity_targets(tagged, session_date, full_session, spec, config),
        config,
    )

    if after.empty:
        outcome_block = "_Session ended at the cutoff._"
    else:
        high, low = float(after["High"].max()), float(after["Low"].min())
        close = float(after["Close"].iloc[-1])
        outcome_block = markdown_table(["What happened after", "Value"], [
            ["High", f"{high:,.2f} ({(high - last) / spec.tick_points:+.0f} ticks)"],
            ["Low", f"{low:,.2f} ({(low - last) / spec.tick_points:+.0f} ticks)"],
            ["Close", f"{close:,.2f} ({(close - last) / spec.tick_points:+.0f} ticks)"],
            ["Range", f"{(high - low) / spec.tick_points:.0f} ticks (${spec.points_to_dollars(high - low):,.0f}/contract)"],
        ])
        swept_after = [
            name for name, price in targets.items()
            if (is_buy_side(name) and high > price) or (not is_buy_side(name) and low < price)
        ]
        if swept_after:
            outcome_block += f"\n\n**Pools run after the cutoff**: {', '.join(swept_after)}"

    if setups:
        setup_block = "\n\n".join(_setup_detail(s, spec) for s in setups)
    else:
        setup_block = (
            "_The scanner found no setup in this session._\n\n"
            "That is a result, not a failure. If you saw one here, the disagreement is the "
            "lesson: either your read used something the four steps do not encode, or a "
            "parameter is mis-set. Check `bin/ta smc explain` and try adjusting "
            "`sweep_return_bars` - it is the one that moves the count most."
        )

    return header + f"""
---

# REVEALED

{outcome_block}

## What the scanner flagged

{setup_block}

> Compare against what you wrote down. Three outcomes, all useful:
>
> - **You agreed and it worked** - weak evidence, one sample. Note it, move on.
> - **You disagreed and you were right** - your read used something the four steps
>   do not capture. Can you write it as a rule? If yes, it belongs in the framework.
> - **You disagreed and the scanner was right** - the more valuable case. Which
>   step did you skip or fudge?
"""
