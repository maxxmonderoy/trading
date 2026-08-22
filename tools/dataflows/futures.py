"""Futures dataflows — contract specs, intraday sessions, and liquidity levels.

Index futures break most assumptions the equity side of this desk was built on.
An index future has no balance sheet, no earnings, and no cashtag community; what
it has instead is a 23-hour session with a clear internal structure, and a set of
reference levels that traders' stops cluster around. Those levels are what
"liquidity pool" actually means mechanically: a price where a predictable
population of stop orders sits, which is why price so often trades *through* one
and immediately reverses.

This module supplies three things the rest of the desk needs:

1. **Contract specs** — multiplier, tick size, tick value. Futures P&L is
   ticks × tick value × contracts, so sizing in "% of portfolio" is meaningless
   here and every downstream agent needs the real arithmetic.
2. **Session structure** — RTH vs overnight, and the reference levels each
   session produces (prior-day high/low/close, overnight high/low, RTH open, VWAP).
3. **Measured behaviour at those levels** — how often each level is touched,
   swept and reversed, or broken and held. Base rates, not assertions.

Micros and minis track the same underlying and therefore share price data; they
differ only in multiplier. `MNQ` and `NQ` both read `NQ=F`.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

import pandas as pd

from .common import (
    cache_read,
    cache_write,
    fmt_num,
    load_config,
    markdown_table,
    raise_if_fetch_failed,
    today,
    unavailable,
)

EASTERN = "America/New_York"

# RTH for the equity index products, in exchange time.
RTH_OPEN = (9, 30)
RTH_CLOSE = (16, 0)
# Globex reopens at 18:00 ET; the overnight session runs from there to the RTH open.
GLOBEX_OPEN = (18, 0)


@dataclass(frozen=True)
class ContractSpec:
    symbol: str          # what a trader types: NQ, MNQ, ES, MES
    data_symbol: str     # what the vendor serves: NQ=F, ES=F
    name: str
    multiplier: float    # dollars per index point
    tick_points: float   # minimum price increment, in index points
    typical_spread_ticks: float
    commission_round_turn: float  # ASSUMED retail all-in; verify with your broker
    exchange: str = "CME"
    # Crypto venues charge a share of notional rather than a flat ticket, and at
    # BTC prices that dominates every other cost term. Zero for CME contracts.
    fee_bps_per_side: float = 0.0
    # "globex" tags sessions on the 18:00 ET open; "utc_day" is one UTC calendar
    # day, for markets that never close.
    session_model: str = "globex"

    @property
    def tick_value(self) -> float:
        return self.multiplier * self.tick_points

    @property
    def is_micro(self) -> bool:
        return self.symbol.startswith("M")

    def notional(self, price: float) -> float:
        return price * self.multiplier

    @property
    def is_crypto(self) -> bool:
        return self.session_model == "utc_day"

    def round_trip_cost(self, price: float | None = None) -> float:
        """Spread crossed once plus commission — the real cost of a round turn.

        A percentage fee needs a price to become a number. Callers that have one
        (every setup knows its entry) should pass it; without it the notional
        component is omitted, which understates crypto costs rather than
        inventing a price to charge against.
        """
        cost = self.typical_spread_ticks * self.tick_value + self.commission_round_turn
        if self.fee_bps_per_side and price:
            notional = price * self.multiplier
            cost += notional * (self.fee_bps_per_side / 10_000) * 2
        return cost

    def ticks_to_dollars(self, ticks: float, contracts: int = 1) -> float:
        return ticks * self.tick_value * contracts

    def points_to_dollars(self, points: float, contracts: int = 1) -> float:
        return points * self.multiplier * contracts


def _build_specs() -> dict[str, ContractSpec]:
    """Contract specs, with costs overridable from config.json.

    Multiplier and tick size are exchange facts and stay fixed. Commission and
    typical spread are *your* broker's numbers, and they feed every R:R and
    cost-drag figure the system reports — so they belong in config where you can
    set them, not in a constant only I can see. Defaults are an assumed retail
    all-in round turn until you replace them.
    """
    costs = load_config().get("futures", {}).get("costs", {})

    def cost(symbol: str, default_commission: float) -> tuple[float, float]:
        entry = costs.get(symbol, {})
        return (
            float(entry.get("spread_ticks", 1.0)),
            float(entry.get("commission_round_turn", default_commission)),
        )

    built = {}
    for symbol, data_symbol, name, multiplier, tick, default_commission in [
        ("ES", "ES=F", "E-mini S&P 500", 50.0, 0.25, 4.00),
        ("MES", "ES=F", "Micro E-mini S&P 500", 5.0, 0.25, 1.40),
        ("NQ", "NQ=F", "E-mini Nasdaq-100", 20.0, 0.25, 4.00),
        ("MNQ", "NQ=F", "Micro E-mini Nasdaq-100", 2.0, 0.25, 1.40),
        # Eurex. Same session-based structure as the CME index products, traded
        # on a European clock; yfinance does not serve it, so it is reachable
        # only through a local history file.
        ("FESX", "FESX", "EURO STOXX 50 (Eurex)", 10.0, 1.0, 2.00),
    ]:
        spread_ticks, commission = cost(symbol, default_commission)
        built[symbol] = ContractSpec(
            symbol, data_symbol, name, multiplier, tick, spread_ticks, commission
        )

    # Binance USDT-M perpetuals. One contract is one coin, and the venue charges
    # a share of notional per side rather than a ticket — at BTC prices that fee
    # dwarfs every other cost term, which is exactly why it is modelled and not
    # folded into a flat number.
    crypto_costs = load_config().get("crypto", {}).get("costs", {})
    for symbol, name, multiplier, tick, default_bps in [
        ("BTCUSDT", "Binance BTC/USDT perpetual", 1.0, 0.1, 4.5),
        ("ETHUSDT", "Binance ETH/USDT perpetual", 1.0, 0.01, 4.5),
        ("SOLUSDT", "Binance SOL/USDT perpetual", 1.0, 0.01, 4.5),
    ]:
        entry = crypto_costs.get(symbol, {})
        built[symbol] = ContractSpec(
            symbol=symbol, data_symbol=symbol, name=name,
            multiplier=multiplier, tick_points=tick,
            typical_spread_ticks=float(entry.get("spread_ticks", 1.0)),
            commission_round_turn=0.0,
            exchange="Binance",
            fee_bps_per_side=float(entry.get("fee_bps_per_side", default_bps)),
            session_model="utc_day",
        )
    return built


SPECS: dict[str, ContractSpec] = _build_specs()

# Aliases a trader might reasonably type.
_ALIASES = {
    "ES=F": "ES", "NQ=F": "NQ", "MES=F": "MES", "MNQ=F": "MNQ",
    "SPX": "ES", "NDX": "NQ", "SP500": "ES", "NASDAQ": "NQ",
}

VALID_INTERVALS = {
    "1m": 5, "2m": 5, "5m": 30, "15m": 60, "30m": 60, "60m": 180, "1h": 180,
}


def is_futures(symbol: str) -> bool:
    key = (symbol or "").strip().upper()
    return key in SPECS or key in _ALIASES


def resolve(symbol: str) -> ContractSpec:
    key = (symbol or "").strip().upper()
    key = _ALIASES.get(key, key)
    if key not in SPECS:
        raise ValueError(
            f"unknown futures contract {symbol!r}. Supported: {', '.join(sorted(SPECS))}"
        )
    return SPECS[key]


# ---------------------------------------------------------------------------
# Intraday data
# ---------------------------------------------------------------------------


def intraday(spec: ContractSpec, interval: str = "5m", days: int | None = None,
             curr_date: str | None = None) -> pd.DataFrame:
    """Intraday bars in exchange time, truncated at `curr_date`.

    yfinance serves intraday history only for a rolling window (see
    VALID_INTERVALS), so a dated run far in the past cannot be served at all.
    Callers must surface that as a gap rather than silently falling back to
    daily bars — the whole point of a futures run is the intraday structure.
    """
    import yfinance as yf

    if interval not in VALID_INTERVALS:
        raise ValueError(
            f"unsupported interval {interval!r}. Supported: {', '.join(VALID_INTERVALS)}"
        )
    max_days = VALID_INTERVALS[interval]
    days = min(days or max_days, max_days)

    cache_key = f"fut_{spec.data_symbol}_{interval}_{days}_{today()}.csv"
    cached = cache_read(cache_key, ttl_minutes=15)
    if cached:
        frame = pd.read_csv(io.StringIO(cached), index_col=0, parse_dates=True)
        frame.index = pd.to_datetime(frame.index, utc=True).tz_convert(EASTERN)
    else:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            frame = yf.download(
                spec.data_symbol, period=f"{days}d", interval=interval,
                progress=False, auto_adjust=False, multi_level_index=False,
            )
        if frame is None or frame.empty:
            # An empty intraday frame is expected outside the rolling window
            # yfinance serves, but it is also what a network failure looks like.
            # Only the captured noise can tell those apart.
            raise_if_fetch_failed(buffer.getvalue(), f"{spec.data_symbol} {interval} bars")
            return pd.DataFrame()
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        frame.index = frame.index.tz_convert(EASTERN)
        cache_write(cache_key, frame.to_csv())

    frame = frame.dropna(subset=["Close"])
    if curr_date:
        cutoff = pd.Timestamp(curr_date, tz=EASTERN) + pd.Timedelta(days=1)
        frame = frame[frame.index < cutoff]
    return frame.sort_index()


def _session_date(timestamp: pd.Timestamp, model: str = "globex") -> pd.Timestamp:
    """The trade date a bar belongs to.

    Globex opens at 18:00 ET for the *next* trade date, so an 8pm Sunday bar is
    Monday's session. Getting this wrong silently corrupts every overnight level.

    A 24/7 market has no such boundary. `utc_day` cuts on the UTC calendar day
    instead — which is the convention crypto desks actually use, and makes
    "prior day high" mean the previous UTC day's extreme. It is a chosen
    boundary, not a structural one, and the reports say so.
    """
    if model == "utc_day":
        return timestamp.tz_convert("UTC").normalize().tz_localize(None)
    if (timestamp.hour, timestamp.minute) >= GLOBEX_OPEN:
        return (timestamp + pd.Timedelta(days=1)).normalize()
    return timestamp.normalize()


def _tag_sessions(frame: pd.DataFrame, spec: ContractSpec | None = None) -> pd.DataFrame:
    """Label each bar with its trade date and whether it is RTH or overnight.

    On a market that never closes there is no regular-trading-hours window to
    separate out, so every bar is in-session. Faking an RTH block for crypto
    would make `rth_high` and the overnight levels look like real structure when
    they are an arbitrary slice of a continuous tape.
    """
    model = spec.session_model if spec is not None else "globex"
    out = frame.copy()
    out["session_date"] = [_session_date(t, model) for t in out.index]
    if model == "utc_day":
        out["is_rth"] = True
        return out
    minutes = out.index.hour * 60 + out.index.minute
    rth_start = RTH_OPEN[0] * 60 + RTH_OPEN[1]
    rth_end = RTH_CLOSE[0] * 60 + RTH_CLOSE[1]
    out["is_rth"] = (minutes >= rth_start) & (minutes < rth_end)
    return out


def _vwap(frame: pd.DataFrame) -> float | None:
    if frame.empty or "Volume" not in frame or frame["Volume"].sum() <= 0:
        return None
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3
    return float((typical * frame["Volume"]).sum() / frame["Volume"].sum())


# ---------------------------------------------------------------------------
# Reference levels
# ---------------------------------------------------------------------------


def compute_levels(tagged: pd.DataFrame, session_date: pd.Timestamp) -> dict:
    """The reference levels for one session — the stop clusters, mechanically.

    Each of these is a price a large, predictable population of orders sits at:
    breakout buyers above the prior-day high, protective stops below the
    overnight low, and so on. That is what makes them worth naming.
    """
    sessions = sorted(tagged["session_date"].unique())
    if session_date not in sessions:
        return {}
    index = sessions.index(session_date)

    current = tagged[tagged["session_date"] == session_date]
    overnight = current[~current["is_rth"]]
    rth = current[current["is_rth"]]

    levels: dict[str, float | None] = {}

    if index > 0:
        prior = tagged[tagged["session_date"] == sessions[index - 1]]
        prior_rth = prior[prior["is_rth"]]
        source = prior_rth if not prior_rth.empty else prior
        if not source.empty:
            levels["pd_high"] = float(source["High"].max())
            levels["pd_low"] = float(source["Low"].min())
            levels["pd_close"] = float(source["Close"].iloc[-1])

    if not overnight.empty:
        levels["on_high"] = float(overnight["High"].max())
        levels["on_low"] = float(overnight["Low"].min())

    if not rth.empty:
        levels["rth_open"] = float(rth["Open"].iloc[0])
        levels["rth_high"] = float(rth["High"].max())
        levels["rth_low"] = float(rth["Low"].min())
        levels["rth_close"] = float(rth["Close"].iloc[-1])
        vwap = _vwap(rth)
        if vwap is not None:
            levels["rth_vwap"] = vwap

    return levels


LEVEL_LABELS = {
    "pd_high": "Prior day high", "pd_low": "Prior day low", "pd_close": "Prior day close",
    "on_high": "Overnight high", "on_low": "Overnight low",
    "rth_open": "RTH open", "rth_high": "RTH high", "rth_low": "RTH low",
    "rth_close": "RTH close", "rth_vwap": "RTH VWAP",
}

# Which side of each level the resting stops sit on.
_UPSIDE_LEVELS = ("pd_high", "on_high")
_DOWNSIDE_LEVELS = ("pd_low", "on_low")


def levels_report(symbol: str, curr_date: str | None = None, interval: str = "5m") -> str:
    """Current session's reference levels, and which are still untested."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(
            f"{spec.symbol} intraday",
            f"no {interval} bars available. Intraday history is limited to "
            f"{VALID_INTERVALS[interval]} days — a dated run older than that cannot be served",
        )

    tagged = _tag_sessions(frame, spec)
    sessions = sorted(tagged["session_date"].unique())
    session_date = sessions[-1]
    levels = compute_levels(tagged, session_date)
    if not levels:
        return unavailable(f"{spec.symbol} levels", "could not resolve a complete session")

    current = tagged[tagged["session_date"] == session_date]
    last_price = float(current["Close"].iloc[-1])
    session_high = float(current["High"].max())
    session_low = float(current["Low"].min())

    rows = []
    untested: dict[str, float] = {}
    for key, value in levels.items():
        distance_points = last_price - value
        distance_ticks = distance_points / spec.tick_points
        # "Tested" means this session has already traded to it — its resting
        # orders are at least partly consumed, so it is no longer a full pool.
        tested = session_low <= value <= session_high
        if not tested:
            untested[key] = value
        rows.append([
            LEVEL_LABELS.get(key, key),
            fmt_num(value),
            f"{distance_points:+.2f}",
            f"{distance_ticks:+.0f}",
            f"${spec.points_to_dollars(abs(distance_points)):,.0f}",
            "tested" if tested else "**untested**",
        ])

    untested_above = sorted(v for v in untested.values() if v > last_price)
    untested_below = sorted((v for v in untested.values() if v < last_price), reverse=True)

    table = markdown_table(
        ["Level", "Price", "Δ points", "Δ ticks", "Δ $/contract", "Status"], rows
    )

    def _describe(value: float | None, direction: str) -> str:
        if value is None:
            return f"none — every level {direction} has already been traded to this session"
        names = ", ".join(LEVEL_LABELS.get(k, k) for k, v in untested.items() if v == value)
        distance = abs(value - last_price)
        return (
            f"{value:,.2f} ({names}) — {distance:.2f} points / "
            f"{distance / spec.tick_points:.0f} ticks away, "
            f"${spec.points_to_dollars(distance):,.0f} per {spec.symbol} contract"
        )

    nearest_above = _describe(untested_above[0] if untested_above else None, "above")
    nearest_below = _describe(untested_below[0] if untested_below else None, "below")

    return f"""## Session levels — {spec.symbol} ({spec.name})

**Session** {session_date.date()} | **last** {last_price:,.2f} | **interval** {interval}
**Session range so far** {session_low:,.2f} – {session_high:,.2f}
({(session_high - session_low) / spec.tick_points:.0f} ticks = ${spec.points_to_dollars(session_high - session_low):,.0f} per {spec.symbol} contract)

{table}

**Nearest resting liquidity above**: {nearest_above}
**Nearest resting liquidity below**: {nearest_below}

> These levels are reference points where stop and breakout orders concentrate —
> that concentration is what "liquidity pool" denotes mechanically. An untested
> level is one this session has not yet traded to. Proximity is not a signal on
> its own: use `fut level-stats` for how often each level is swept versus broken
> on this contract, and do not assert a reaction the base rates do not support.
"""


# ---------------------------------------------------------------------------
# Measured behaviour at levels
# ---------------------------------------------------------------------------


def level_stats(symbol: str, level_key: str = "pd_high", lookback: int = 60,
                interval: str = "15m", threshold_ticks: float = 2.0,
                window_bars: int = 4, curr_date: str | None = None) -> str:
    """Base rates: how often a level is touched, swept and reversed, or broken.

    Definitions, stated because every trading community uses these words
    differently:

    - **touched**   — the RTH session traded to the level at all
    - **swept**     — traded beyond it by more than `threshold_ticks`, then
                      closed the session back on the originating side
    - **broke**     — closed the session beyond the level

    "Swept" is the mechanically interesting case: price reached the stops, took
    them, and left. The threshold exists so a one-tick brush is not counted as a
    sweep.
    """
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"
    if level_key not in LEVEL_LABELS:
        return f"<error: unknown level {level_key!r}. Options: {', '.join(LEVEL_LABELS)}>"
    if level_key not in _UPSIDE_LEVELS + _DOWNSIDE_LEVELS:
        return (
            f"<error: {level_key} is a reference price, not a stop cluster. "
            f"Statistics are defined for: {', '.join(_UPSIDE_LEVELS + _DOWNSIDE_LEVELS)}>"
        )

    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(f"{spec.symbol} intraday", f"no {interval} bars available")

    tagged = _tag_sessions(frame, spec)
    sessions = sorted(tagged["session_date"].unique())[-lookback:]
    upside = level_key in _UPSIDE_LEVELS
    threshold = threshold_ticks * spec.tick_points

    touched = broke = gapped = 0
    # Kept separate on purpose. A day that breaks the level and trends runs
    # hundreds of ticks beyond it; a day that sweeps the stops and reverses pokes
    # a few ticks through. Averaging the two produces a number that describes
    # neither, and it is the sweep distribution — not the blend — that tells you
    # where a stop survives.
    sweep_excursions: list[float] = []
    break_extensions: list[float] = []
    measured = 0

    for session_date in sessions:
        levels = compute_levels(tagged, session_date)
        level = levels.get(level_key)
        if level is None:
            continue
        rth = tagged[(tagged["session_date"] == session_date) & tagged["is_rth"]].sort_index()
        if rth.empty:
            continue
        measured += 1

        # Find the first bar that penetrates the level, then measure only the
        # `window_bars` immediately after it.
        #
        # The window is not a detail — it is the whole definition. Without one,
        # a penetration at 09:40 that runs all session and closes back through at
        # 15:55 registers as a single enormous "sweep", and the median comes out
        # at a quarter of the daily range. There is no window-free fact of the
        # matter about what counts as a sweep; it is a strategy parameter, so it
        # is exposed as one rather than buried in the statistic.
        highs = rth["High"].astype(float).to_numpy()
        lows = rth["Low"].astype(float).to_numpy()
        closes = rth["Close"].astype(float).to_numpy()
        rth_open = float(rth["Open"].iloc[0])

        # A session that opens beyond the level never approached it. Overnight
        # gapped over the resting orders instead of trading into them, so there
        # is no penetration event to measure and no stops were taken on the way.
        # Counting these as sweeps is what produced 600-tick "stop raids": the
        # figure was really the size of the opening gap.
        if (upside and rth_open > level) or (not upside and rth_open < level):
            gapped += 1
            continue

        penetration = None
        for i in range(len(rth)):
            if (upside and highs[i] > level) or (not upside and lows[i] < level):
                penetration = i
                break
        if penetration is None:
            continue
        touched += 1

        end = min(penetration + window_bars, len(rth))
        window_extreme = highs[penetration:end].max() if upside else lows[penetration:end].min()
        excursion_ticks = abs(window_extreme - level) / spec.tick_points

        # Resolution is judged at the session close: did it hold beyond, or come back?
        final_close = closes[-1]
        held = final_close > level if upside else final_close < level

        if held:
            broke += 1
            break_extensions.append(abs(final_close - level) / spec.tick_points)
        elif excursion_ticks > threshold_ticks:
            sweep_excursions.append(excursion_ticks)

    if not measured:
        return unavailable(f"{spec.symbol} level stats", "no complete sessions in window")
    if not touched:
        return (f"## {LEVEL_LABELS[level_key]} — {spec.symbol}\n\nNever approached and penetrated in "
                f"the last {measured} sessions ({gapped} gapped beyond at the open).")

    def _quantile(values: list[float], q: float) -> float:
        ordered = sorted(values)
        return ordered[min(int(q * len(ordered)), len(ordered) - 1)]

    swept = len(sweep_excursions)
    rows = [
        ["Sessions measured", str(measured)],
        ["Gapped beyond at RTH open (excluded)", f"{gapped} ({gapped / measured * 100:.0f}% of sessions)"],
        ["Approached and penetrated", f"{touched} ({touched / measured * 100:.0f}% of sessions)"],
        ["Broke and held (closed beyond)", f"{broke} ({broke / touched * 100:.0f}% of penetrations)"],
        ["Swept and reversed", f"{swept} ({swept / touched * 100:.0f}% of penetrations)"],
        ["Penetrated below sweep threshold", str(touched - broke - swept)],
    ]

    # Small-n warning up front, not buried. These windows routinely yield fewer
    # than 20 penetrations, and an agent reading a "71% sweep rate" off n=12
    # will state it as a property of the market.
    sample_warning = ""
    if touched < 30:
        sample_warning = (
            f"\n> ⚠️ **Sample too small to infer from: {touched} penetrations"
            + (f", {swept} of them sweeps" if swept else "")
            + f".** Percentages on this base move several points per event. Treat "
            "every rate below as a description of these specific sessions, not as a "
            "base rate for the contract, and do not let a position size depend on it.\n"
        )

    sweep_block = "_No swept-and-reversed sessions in this window._"
    if sweep_excursions:
        median = _quantile(sweep_excursions, 0.5)
        p75 = _quantile(sweep_excursions, 0.75)
        worst = max(sweep_excursions)
        sweep_block = markdown_table(["Sweep excursion", "Ticks", "Per contract"], [
            ["Median", f"{median:.1f}", f"${spec.ticks_to_dollars(median):,.2f}"],
            ["75th percentile", f"{p75:.1f}", f"${spec.ticks_to_dollars(p75):,.2f}"],
            ["Worst observed", f"{worst:.1f}", f"${spec.ticks_to_dollars(worst):,.2f}"],
        ])

    break_block = "_No break-and-hold sessions in this window._"
    if break_extensions:
        median = _quantile(break_extensions, 0.5)
        p75 = _quantile(break_extensions, 0.75)
        break_block = markdown_table(["Close beyond level", "Ticks", "Per contract"], [
            ["Median", f"{median:.1f}", f"${spec.ticks_to_dollars(median):,.2f}"],
            ["75th percentile", f"{p75:.1f}", f"${spec.ticks_to_dollars(p75):,.2f}"],
        ])

    return f"""## {LEVEL_LABELS[level_key]} behaviour — {spec.symbol}

Last {measured} RTH sessions | {interval} bars | sweep window {window_bars} bars after first
penetration | minimum {threshold_ticks:.0f} ticks to count as a sweep.

{markdown_table(["Statistic", "Value"], rows)}
{sample_warning}
### Sweep excursions — extension within {window_bars} bars of first penetration

{sweep_block}

### Break extensions — how far past the level on a close beyond

{break_block}

> **"Sweep" is a parameterised measurement, not an observed fact.** It depends
> entirely on the `--window-bars` window above: widen it and ordinary trend
> extension gets counted as sweeping, narrow it and real stop raids are missed.
> Re-run with different windows before trusting any number here, and never quote
> one of these figures without the window that produced it.
>
> Given that, sweep excursion is stop-placement guidance — a stop inside the 75th
> percentile is taken out by ordinary post-penetration extension rather than by a
> broken thesis. Break extension is target guidance for when the level gives way.
>
> Sessions that gapped beyond the level at the RTH open are excluded outright:
> price never traded into the resting orders, so nothing was swept.
>
> Sample is {measured} sessions from one regime on a continuous front-month
> series, so a contract roll sits inside this window and shifts price by the
> carry basis. These are descriptive base rates, not an edge, and they say
> nothing about what happens *after* a sweep — only how far it typically ran.
"""


# ---------------------------------------------------------------------------
# Contract reference and sizing
# ---------------------------------------------------------------------------


def contract_reference(symbol: str | None = None, price: float | None = None,
                       curr_date: str | None = None) -> str:
    """Specs and live cost arithmetic for one or all supported contracts."""
    from .market import close_on

    specs = [resolve(symbol)] if symbol else list(SPECS.values())
    rows = []
    for spec in specs:
        last = price if price is not None else close_on(spec.data_symbol, curr_date or today())
        if last is None:
            rows.append([spec.symbol, spec.name, "N/A", fmt_num(spec.multiplier),
                         f"{spec.tick_points}", f"${spec.tick_value:.2f}", "N/A", "N/A"])
            continue
        rows.append([
            spec.symbol, spec.name, f"{last:,.2f}", fmt_num(spec.multiplier),
            f"{spec.tick_points}", f"${spec.tick_value:.2f}",
            f"${spec.notional(last):,.0f}", f"${spec.round_trip_cost():.2f}",
        ])

    return f"""## Contract specifications

{markdown_table(["Symbol", "Name", "Last", "$/point", "Tick", "$/tick", "Notional", "Round-trip cost"], rows)}

> **Round-trip cost** assumes crossing a {SPECS['ES'].typical_spread_ticks:.0f}-tick spread once plus
> assumed retail all-in commission (${SPECS['ES'].commission_round_turn:.2f} minis /
> ${SPECS['MES'].commission_round_turn:.2f} micros per round turn). **Verify commissions against your
> own broker** — it is the one input here that is an assumption rather than a measurement.
> Spreads widen outside RTH and around scheduled data.
>
> Micros and minis track the same index and share price data; only the multiplier
> differs. Notional is the full contract value — a $60k MNQ position on a $10k
> account is 6x leverage, and the day-trade margin your broker allows is not a
> measure of the risk you are taking.
"""


def position_size(symbol: str, account: float, risk_pct: float, stop_ticks: float,
                  curr_date: str | None = None) -> str:
    """How many contracts a given stop distance and risk budget actually permit.

    This is the calculation that decides whether an account can trade a contract
    at all. It regularly returns zero, and that answer is the useful one.
    """
    from .market import close_on

    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"
    if stop_ticks <= 0 or account <= 0 or risk_pct <= 0:
        return "<error: account, risk_pct, and stop_ticks must all be positive>"

    risk_dollars = account * (risk_pct / 100)
    risk_per_contract = spec.ticks_to_dollars(stop_ticks)
    raw = risk_dollars / risk_per_contract
    contracts = int(raw)

    last = close_on(spec.data_symbol, curr_date or today())
    notional = spec.notional(last) * max(contracts, 1) if last else None
    cost = spec.round_trip_cost() * max(contracts, 1)

    rows = [
        ["Account", f"${account:,.2f}"],
        ["Risk budget", f"{risk_pct:.2f}% = ${risk_dollars:,.2f}"],
        ["Stop distance", f"{stop_ticks:.0f} ticks = {stop_ticks * spec.tick_points:.2f} points"],
        ["Risk per contract", f"${risk_per_contract:,.2f}"],
        ["**Contracts permitted**", f"**{contracts}**" + (f" (unrounded {raw:.2f})" if raw >= 1 else "")],
    ]
    if last:
        rows.append(["Notional at that size", f"${notional:,.0f}"])
        rows.append(["Leverage vs account", f"{notional / account:.1f}x"])
    rows.append(["Round-trip cost at that size", f"${cost:,.2f} ({cost / risk_dollars * 100:.1f}% of risk budget)"])

    verdict = ""
    if contracts == 0:
        smallest = spec.ticks_to_dollars(stop_ticks)
        needed = smallest / (risk_pct / 100)
        micro = "MNQ" if spec.symbol in ("NQ",) else ("MES" if spec.symbol == "ES" else None)
        verdict = (
            f"\n> **Zero contracts.** One {spec.symbol} with a {stop_ticks:.0f}-tick stop risks "
            f"${smallest:,.2f}, which exceeds a {risk_pct:.2f}% budget on ${account:,.2f}. "
            f"You would need roughly ${needed:,.0f} to take this trade at this stop and risk level"
            + (f", or trade {micro} instead." if micro else ".")
            + " Taking it anyway means exceeding your stated risk, which is the decision that "
            "ends accounts — not the entry."
        )
    elif last and notional / account > 10:
        verdict = (
            f"\n> **{notional / account:.0f}x leverage.** The stop controls the loss only while the "
            "market is continuous. A gap through it — a Sunday reopen, a data release — "
            "settles at whatever price prints, and at this leverage that is an account-level event."
        )

    return f"""## Position sizing — {spec.symbol} ({spec.name})

{markdown_table(["Input", "Value"], rows)}
{verdict}
"""


def bars_report(symbol: str, interval: str = "5m", limit: int = 40,
                curr_date: str | None = None) -> str:
    """Recent intraday bars with session labels — the raw candle structure."""
    try:
        spec = resolve(symbol)
    except ValueError as exc:
        return f"<error: {exc}>"

    frame = intraday(spec, interval, curr_date=curr_date)
    if frame.empty:
        return unavailable(
            f"{spec.symbol} intraday",
            f"no {interval} bars. Intraday history is capped at {VALID_INTERVALS[interval]} days",
        )

    tagged = _tag_sessions(frame, spec).tail(limit)
    rows = []
    for timestamp, row in tagged.iterrows():
        rng = float(row["High"]) - float(row["Low"])
        body = abs(float(row["Close"]) - float(row["Open"]))
        # Body share separates decisive bars from rejection wicks without
        # committing to any particular pattern vocabulary.
        body_pct = body / rng * 100 if rng else 0.0
        rows.append([
            timestamp.strftime("%m-%d %H:%M"),
            "RTH" if row["is_rth"] else "ON",
            f"{row['Open']:,.2f}", f"{row['High']:,.2f}",
            f"{row['Low']:,.2f}", f"{row['Close']:,.2f}",
            f"{rng / spec.tick_points:.0f}",
            f"{body_pct:.0f}%",
            fmt_num(row.get("Volume"), 0),
        ])

    return (
        f"## {spec.symbol} — {interval} bars (exchange time, ET)\n\n"
        + markdown_table(
            ["Time", "Sess", "Open", "High", "Low", "Close", "Range(t)", "Body%", "Volume"], rows
        )
        + f"\n\n> `Sess` is RTH (09:30–16:00 ET) or ON (overnight Globex). `Range(t)` is the bar "
        f"range in ticks; `Body%` is body as a share of range — a low body share is a rejection "
        f"bar, a high one is a decisive bar. One tick = ${spec.tick_value:.2f} on {spec.symbol}."
    )
