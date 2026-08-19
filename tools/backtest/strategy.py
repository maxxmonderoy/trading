"""The SMC framework as a backtrader Strategy.

Detectors (swings, FVGs, kill zones, buy/sell-side classification) are imported
from ``dataflows.smc`` rather than reimplemented — one definition of "what is a
fair value gap", used by the scanner, the replay tool, and the backtest. Two
implementations of the same concept drift, and the drift shows up as a backtest
that disagrees with the tool you traded from.

What backtrader adds over the standalone scanner:

- **Structural look-ahead protection.** ``next()`` cannot see future bars, so
  the "levels knowable at this moment" discipline is enforced by the framework
  instead of by my own care.
- **Real order mechanics** — limit fills, stop fills, and margin.
- **A live path.** Its broker feeds bars to ``next()`` the same way, so this
  class is what would eventually trade.
"""

from __future__ import annotations

import sys
from pathlib import Path

import backtrader as bt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataflows.smc import (  # noqa: E402
    find_equal_levels, find_fvgs, find_swings, is_buy_side, kill_zone_for,
)

RTH_START, RTH_END = (9, 30), (16, 0)
GLOBEX_OPEN = (18, 0)


def _session_date(dt) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    if (ts.hour, ts.minute) >= GLOBEX_OPEN:
        return (ts + pd.Timedelta(days=1)).normalize()
    return ts.normalize()


def _in_rth(dt) -> bool:
    ts = pd.Timestamp(dt)
    minutes = ts.hour * 60 + ts.minute
    return RTH_START[0] * 60 + RTH_START[1] <= minutes < RTH_END[0] * 60 + RTH_END[1]


class SMCStrategy(bt.Strategy):
    """Liquidity sweep → market structure shift → FVG retest entry."""

    params = dict(
        swing_lookback=2,
        sweep_return_bars=5,
        mss_max_bars=20,
        stop_buffer_ticks=4,
        entry_mode="ce",
        min_rr=2.0,
        max_trades_per_session=2,
        stop_after_losses=2,
        risk_pct=1.0,
        target_pool="htf",
        eq_tolerance_ticks=4.0,
        tick_points=0.25,
        multiplier=2.0,
        kill_zones=None,
        partial_at_1r=False,      # the management change under test
        randomize_direction=False,  # null model: same timing and geometry, coin-flip direction
        session_ranges=None,
        entry_expiry_bars=12,     # cancel an unfilled limit after this many bars
        # The spec gates the *sweep* on the kill zone ("during or right before"),
        # and says nothing about the MSS. A sweep at 04:50 whose structure shift
        # confirms at 05:10 is therefore valid on a literal reading. Cancelling at
        # the bell instead is defensible but is a different strategy — so it is a
        # parameter, not a silent choice.
        cancel_at_zone_end=False,
        verbose=False,
    )

    def __init__(self):
        self.trade_log: list[dict] = []
        self._bars: list[dict] = []          # current session, oldest first
        self._session = None
        self._prior_rth = None               # (high, low) of the prior session's RTH
        self._reset_session_state()
        self._pending = None                 # setup awaiting fill
        self._active = None                  # filled position bookkeeping
        self._state = "IDLE"
        self._swept = None

    # -- session bookkeeping -------------------------------------------------

    def _reset_session_state(self):
        self._bars = []
        self._on_hi = self._on_lo = None
        self._rth_hi = self._rth_lo = None
        self._range_hi: dict[str, float] = {}
        self._range_lo: dict[str, float] = {}
        self._trades_taken = 0
        self._losses = 0
        self._state = "IDLE"
        self._swept = None

    def _roll_session(self):
        if self._rth_hi is not None and self._rth_lo is not None:
            self._prior_rth = (self._rth_hi, self._rth_lo)
        self._reset_session_state()

    # -- levels --------------------------------------------------------------

    def _levels(self) -> dict[str, float]:
        """Every pool knowable at the current bar. Nothing forward-looking."""
        levels: dict[str, float] = {}
        if self._prior_rth:
            levels["PDH"], levels["PDL"] = self._prior_rth
        if self._on_hi is not None:
            levels["ONH"], levels["ONL"] = self._on_hi, self._on_lo
        for name in self._range_hi:
            levels[f"{name}H"] = self._range_hi[name]
            levels[f"{name}L"] = self._range_lo[name]

        if len(self._bars) > 2 * self.p.swing_lookback + 1:
            frame = pd.DataFrame(self._bars).set_index("dt")
            for eq in find_equal_levels(
                find_swings(frame, self.p.swing_lookback),
                self.p.tick_points, self.p.eq_tolerance_ticks,
            ):
                levels[f"EQ{'H' if eq.kind == 'high' else 'L'}x{eq.count}"] = eq.price
        return levels

    def _target_pool(self, levels: dict[str, float]) -> dict[str, float]:
        if self.p.target_pool == "all":
            return levels
        return {k: v for k, v in levels.items() if k in ("PDH", "PDL", "ONH", "ONL")}

    # -- main loop -----------------------------------------------------------

    def next(self):
        dt = self.data.datetime.datetime(0)
        o, h, l, c = (float(self.data.open[0]), float(self.data.high[0]),
                      float(self.data.low[0]), float(self.data.close[0]))

        session = _session_date(dt)
        if self._session is None:
            self._session = session
        elif session != self._session:
            self._session = session
            self._roll_session()

        self._bars.append({"dt": dt, "Open": o, "High": h, "Low": l, "Close": c})

        rth = _in_rth(dt)
        if rth:
            self._rth_hi = h if self._rth_hi is None else max(self._rth_hi, h)
            self._rth_lo = l if self._rth_lo is None else min(self._rth_lo, l)
        else:
            self._on_hi = h if self._on_hi is None else max(self._on_hi, h)
            self._on_lo = l if self._on_lo is None else min(self._on_lo, l)

        for name, (start, end) in (self.p.session_ranges or {}).items():
            sh, sm = map(int, start.split(":")); eh, em = map(int, end.split(":"))
            minutes = dt.hour * 60 + dt.minute
            inside = (sh * 60 + sm <= minutes < eh * 60 + em) if (sh, sm) <= (eh, em) \
                else (minutes >= sh * 60 + sm or minutes < eh * 60 + em)
            if inside:
                self._range_hi[name] = h if name not in self._range_hi else max(self._range_hi[name], h)
                self._range_lo[name] = l if name not in self._range_lo else min(self._range_lo[name], l)

        if self._active:
            self._manage(dt, h, l)
            return
        if self._pending:
            self._try_fill(dt, h, l)
            return

        zone = kill_zone_for(pd.Timestamp(dt), {"kill_zones": self.p.kill_zones})
        if not zone:
            # Outside a kill zone: no NEW sweeps are hunted, but a sweep already
            # in progress is allowed to resolve unless configured otherwise.
            if self._state != "AWAIT_MSS" or self.p.cancel_at_zone_end:
                self._state, self._swept = "IDLE", None
                return
            zone = self._swept["zone"]
        if self._losses >= self.p.stop_after_losses or self._trades_taken >= self.p.max_trades_per_session:
            return

        self._step(dt, h, l, c, zone)

    def _step(self, dt, h, l, c, zone):
        levels = self._levels()

        if self._state in ("IDLE", "MONITOR"):
            self._state = "MONITOR"
            for name, level in levels.items():
                buy_side = is_buy_side(name)
                if buy_side and h > level:
                    self._swept = {"name": name, "level": level, "extreme": h,
                                   "side": "buy", "bar": len(self._bars), "zone": zone}
                    self._state = "AWAIT_MSS"
                    return
                if not buy_side and l < level:
                    self._swept = {"name": name, "level": level, "extreme": l,
                                   "side": "sell", "bar": len(self._bars), "zone": zone}
                    self._state = "AWAIT_MSS"
                    return
            return

        # AWAIT_MSS
        s = self._swept
        s["extreme"] = max(s["extreme"], h) if s["side"] == "buy" else min(s["extreme"], l)
        elapsed = len(self._bars) - s["bar"]
        if elapsed > self.p.mss_max_bars:
            self._state, self._swept = "MONITOR", None
            return

        returned = c < s["level"] if s["side"] == "buy" else c > s["level"]
        if not returned:
            return
        if elapsed > self.p.sweep_return_bars:
            self._state, self._swept = "MONITOR", None
            return

        direction = "short" if s["side"] == "buy" else "long"
        if self.p.randomize_direction:
            # Null model. Keeps entry timing, stop distance, and target selection
            # identical, and flips only the directional call — so any surviving
            # performance belongs to the risk geometry, not to reading the sweep.
            import random
            direction = random.choice(["long", "short"])
        frame = pd.DataFrame(self._bars).set_index("dt")
        swings = find_swings(frame, self.p.swing_lookback)
        opposing = [x for x in swings
                    if x.index < s["bar"] - 1 and x.kind == ("low" if direction == "short" else "high")]
        if not opposing:
            return
        ref = opposing[-1]
        if not (c < ref.price if direction == "short" else c > ref.price):
            return

        gaps = [g for g in find_fvgs(frame, max(s["bar"] - 1, 0), len(frame))
                if g.direction == ("bearish" if direction == "short" else "bullish")]
        if not gaps:
            self._state, self._swept = "MONITOR", None
            return
        gap = gaps[-1]

        entry = gap.entry_price(self.p.entry_mode)
        buffer = self.p.stop_buffer_ticks * self.p.tick_points
        stop = s["extreme"] + buffer if direction == "short" else s["extreme"] - buffer
        risk = abs(stop - entry)
        if risk <= 0:
            self._state, self._swept = "MONITOR", None
            return

        pool = self._target_pool(levels)
        if direction == "short":
            below = {k: v for k, v in pool.items() if v < entry}
            name = max(below, key=below.get) if below else None
        else:
            above = {k: v for k, v in pool.items() if v > entry}
            name = min(above, key=above.get) if above else None
        if name is None:
            self._state, self._swept = "MONITOR", None
            return
        target = pool[name]
        rr = abs(target - entry) / risk
        if rr < self.p.min_rr:
            self._state, self._swept = "MONITOR", None
            return

        self._pending = {
            "dt": dt, "direction": direction, "entry": entry, "stop": stop,
            "target": target, "target_name": name, "risk": risk, "rr": rr,
            "swept": s["name"], "zone": s["zone"], "placed_bar": len(self._bars),
        }
        self._state, self._swept = "MONITOR", None

    # -- order handling ------------------------------------------------------

    def _try_fill(self, dt, h, l):
        p = self._pending
        if len(self._bars) - p["placed_bar"] > self.p.entry_expiry_bars:
            self._log(p, dt, "unfilled", 0.0)
            self._pending = None
            return
        touched = h >= p["entry"] if p["direction"] == "short" else l <= p["entry"]
        if not touched:
            return
        self._active = {**p, "filled_dt": dt, "half_out": False, "mfe": 0.0}
        self._pending = None
        self._trades_taken += 1

    def _manage(self, dt, h, l):
        a = self._active
        long = a["direction"] == "long"
        risk = a["risk"]

        # Max favourable excursion, in price. Long profits as price rises, short as it falls.
        excursion = (h - a["entry"]) if long else (a["entry"] - l)
        a["mfe"] = max(a["mfe"], excursion)

        one_r = a["entry"] + risk if long else a["entry"] - risk
        hit_stop = l <= a["stop"] if long else h >= a["stop"]
        hit_tgt = h >= a["target"] if long else l <= a["target"]
        hit_1r = h >= one_r if long else l <= one_r

        if hit_stop and hit_tgt:
            self._close(a, dt, "ambiguous", 0.0)
            return

        if self.p.partial_at_1r and not a["half_out"] and hit_1r and not hit_stop:
            a["half_out"] = True
            a["stop"] = a["entry"]          # breakeven on the remainder
            if hit_tgt:
                self._close(a, dt, "target", 0.5 * 1.0 + 0.5 * a["rr"])
            return

        if hit_stop:
            if a["half_out"]:
                self._close(a, dt, "partial_be", 0.5 * 1.0)   # banked 1R on half, BE on rest
            else:
                self._close(a, dt, "stopped", -1.0)
            return
        if hit_tgt:
            r = 0.5 * 1.0 + 0.5 * a["rr"] if a["half_out"] else a["rr"]
            self._close(a, dt, "target", r)
            return

    def _close(self, a, dt, outcome, r):
        if outcome == "stopped":
            self._losses += 1
        self._log(a, dt, outcome, r)
        self._active = None

    def _log(self, setup, dt, outcome, r):
        self.trade_log.append({
            "session": str(_session_date(setup["dt"]).date()),
            "entry_dt": str(setup["dt"]), "exit_dt": str(dt),
            "direction": setup["direction"], "zone": setup["zone"],
            "swept": setup["swept"], "entry": setup["entry"], "stop": setup["stop"],
            "target": setup["target"], "target_name": setup["target_name"],
            "risk_points": setup["risk"], "planned_rr": setup["rr"],
            "outcome": outcome, "r_gross": r,
        })

    def stop(self):
        if self.p.verbose:
            print(f"  trades logged: {len(self.trade_log)}")
