"""Price, indicator, and verified-snapshot dataflows.

Port of upstream `dataflows/y_finance.py`, `stockstats_utils.py`, and
`market_data_validator.py`.

Two invariants carried over from upstream, both of which exist because an LLM
sits downstream of this code:

1. **No look-ahead.** Every frame is truncated at `curr_date`. A backtest or a
   dated re-analysis must not see bars the analyst could not have seen.
2. **A verified snapshot exists.** `snapshot()` computes ground truth
   deterministically so the market analyst has something to check its own
   numeric claims against (upstream issue #830 — analysts confabulating exact
   price levels and "historically validated bounces").
"""

from __future__ import annotations

import io
import warnings
from contextlib import redirect_stderr, redirect_stdout

import pandas as pd

from .common import (
    FetchError,
    cache_path,
    days_before,
    fmt_num,
    load_config,
    markdown_table,
    normalize_symbol,
    parse_date,
    raise_if_fetch_failed,
    today,
    unavailable,
    unavailable_empty,
)

warnings.filterwarnings("ignore", category=FutureWarning)

# Fixed indicator set for the verified snapshot, so its shape is identical
# every run regardless of what the analyst chose to look at.
SNAPSHOT_INDICATORS = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr", "vwma",
)

SUPPORTED_INDICATORS = {
    "close_50_sma": "50 SMA — medium-term trend; dynamic support/resistance. Lags price.",
    "close_200_sma": "200 SMA — long-term trend benchmark; golden/death crosses. Slow.",
    "close_10_ema": "10 EMA — short-term momentum shifts. Noisy in chop.",
    "macd": "MACD — momentum via EMA differences; crossovers and divergence.",
    "macds": "MACD Signal — EMA of MACD; crossovers trigger trades.",
    "macdh": "MACD Histogram — momentum strength, early divergence. Volatile.",
    "rsi": "RSI — overbought/oversold at 70/30; watch divergence. Stays extreme in trends.",
    "boll": "Bollinger Middle — 20 SMA basis for the bands.",
    "boll_ub": "Bollinger Upper — ~2σ above; overbought / breakout zone.",
    "boll_lb": "Bollinger Lower — ~2σ below; oversold zone.",
    "atr": "ATR — volatility; size stops and positions off it.",
    "vwma": "VWMA — volume-weighted MA; confirms trend with participation.",
}


def _download(symbol: str, start: str, end: str) -> pd.DataFrame:
    """yfinance download with its console chatter captured, not discarded.

    yfinance writes progress bars and error banners to stdout/stderr. This CLI's
    stdout is an agent's tool result, so the chatter has to be kept off it — but
    it is also the only place a transport failure is reported, since yfinance
    catches the error and returns an empty frame. Capture it, then let
    `raise_if_fetch_failed` decide whether the empty frame is an outage or an
    answer.
    """
    import yfinance as yf

    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        data = yf.download(
            symbol,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
            multi_level_index=False,
        )
    if data is None or data.empty:
        raise_if_fetch_failed(buffer.getvalue(), f"yfinance OHLCV for {symbol}")
        return pd.DataFrame()
    data = data.reset_index()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [c[0] for c in data.columns]
    return data


def load_ohlcv(symbol: str, curr_date: str, look_back_days: int | None = None) -> pd.DataFrame:
    """OHLCV frame ending at `curr_date` (inclusive), cached on disk per day.

    The cache key includes `curr_date` so a same-day rerun is free while a new
    trading day always refetches.
    """
    config = load_config()
    look_back_days = look_back_days or config["ohlcv_lookback_days"]
    symbol = normalize_symbol(symbol)

    start = days_before(curr_date, look_back_days)
    # yfinance treats `end` as exclusive; pad so curr_date's bar is included.
    end = (parse_date(curr_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    cache_file = cache_path(f"ohlcv_{symbol}_{curr_date}_{look_back_days}.csv")
    if cache_file.exists() and curr_date != today():
        # Historical windows are immutable; only today's window can change.
        data = pd.read_csv(cache_file)
    else:
        data = _download(symbol, start, end)
        if not data.empty:
            data.to_csv(cache_file, index=False)

    if data.empty:
        return data

    date_col = "Date" if "Date" in data.columns else data.columns[0]
    data = data.rename(columns={date_col: "Date"})
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce", utc=True).dt.tz_localize(None)
    data = data.dropna(subset=["Date"])

    # Look-ahead guard: never return a bar dated after the analysis date.
    cutoff = pd.to_datetime(curr_date)
    data = data[data["Date"] <= cutoff].sort_values("Date").reset_index(drop=True)
    return data


def _staleness_note(data: pd.DataFrame, curr_date: str) -> str:
    """Warn when the last bar is far behind the analysis date.

    A delisted ticker, a typo, or a vendor outage all present as a frame whose
    last row is months old. Upstream added this guard after analysts wrote
    confident reports off year-old prices.
    """
    if data.empty:
        return ""
    last = data["Date"].iloc[-1]
    gap = (pd.to_datetime(curr_date) - last).days
    if gap > 7:
        return (
            f"\n\n> **STALE DATA WARNING**: the most recent bar is {last.date()} — "
            f"{gap} days before the analysis date ({curr_date}). Treat every price "
            "level below as out of date and say so explicitly in your report."
        )
    return ""


def price_history(symbol: str, curr_date: str, look_back_days: int = 60) -> str:
    """Rendered OHLCV window — upstream's `get_stock_data`."""
    symbol = normalize_symbol(symbol)
    try:
        data = load_ohlcv(symbol, curr_date, max(look_back_days, 250))
    except FetchError as exc:
        return unavailable("yfinance OHLCV", str(exc))
    except Exception as exc:  # parse or symbol errors
        return unavailable("yfinance OHLCV", f"{type(exc).__name__}: {exc}")
    if data.empty:
        return unavailable_empty(
            "yfinance OHLCV", f"no rows returned for {symbol} on or before {curr_date}"
        )

    window = data.tail(look_back_days)
    rows = [
        [
            row["Date"].strftime("%Y-%m-%d"),
            fmt_num(row.get("Open")),
            fmt_num(row.get("High")),
            fmt_num(row.get("Low")),
            fmt_num(row.get("Close")),
            fmt_num(row.get("Volume"), 0),
        ]
        for _, row in window.iterrows()
    ]
    header = f"## OHLCV — {symbol} ({window['Date'].iloc[0].date()} → {window['Date'].iloc[-1].date()})"
    table = markdown_table(["Date", "Open", "High", "Low", "Close", "Volume"], rows)
    return f"{header}\n\n{table}{_staleness_note(data, curr_date)}"


def indicators(symbol: str, curr_date: str, names: list[str], look_back_days: int = 30) -> str:
    """Recent values for the named stockstats indicators — upstream's `get_indicators`."""
    from stockstats import wrap

    symbol = normalize_symbol(symbol)
    unknown = [n for n in names if n not in SUPPORTED_INDICATORS]
    if unknown:
        return (
            f"Unsupported indicator(s): {', '.join(unknown)}.\n"
            f"Supported: {', '.join(sorted(SUPPORTED_INDICATORS))}"
        )

    try:
        data = load_ohlcv(symbol, curr_date)
    except FetchError as exc:
        return unavailable("yfinance OHLCV", str(exc))
    except Exception as exc:
        return unavailable("yfinance OHLCV", f"{type(exc).__name__}: {exc}")
    if data.empty:
        return unavailable_empty(
            "indicators", f"no OHLCV rows for {symbol} on or before {curr_date}"
        )

    frame = wrap(data.copy())
    columns: dict[str, list[str]] = {}
    for name in names:
        try:
            frame[name]  # triggers stockstats computation
            columns[name] = [fmt_num(v) for v in frame[name].tail(look_back_days)]
        except Exception as exc:
            columns[name] = [f"error: {type(exc).__name__}"] * min(look_back_days, len(frame))

    dates = [d.strftime("%Y-%m-%d") for d in data["Date"].tail(look_back_days)]
    closes = [fmt_num(c) for c in data["Close"].tail(look_back_days)]
    rows = [
        [dates[i], closes[i], *[columns[n][i] for n in names]]
        for i in range(len(dates))
    ]
    table = markdown_table(["Date", "Close", *names], rows)
    legend = "\n".join(f"- **{n}**: {SUPPORTED_INDICATORS[n]}" for n in names)
    return f"## Indicators — {symbol} (last {len(rows)} sessions)\n\n{table}\n\n### What these mean\n{legend}"


def snapshot(symbol: str, curr_date: str, look_back_days: int = 30) -> str:
    """Deterministic ground-truth snapshot — upstream's `get_verified_market_snapshot`.

    The market analyst is told to treat this as authoritative for every exact
    numeric claim it makes. No LLM is involved in producing it.
    """
    from stockstats import wrap

    symbol = normalize_symbol(symbol)
    try:
        data = load_ohlcv(symbol, curr_date)
    except FetchError as exc:
        return unavailable("verified snapshot", str(exc))
    except Exception as exc:
        return unavailable("verified snapshot", f"{type(exc).__name__}: {exc}")
    if data.empty:
        return unavailable_empty(
            "verified snapshot", f"no OHLCV rows for {symbol} on or before {curr_date}"
        )

    latest = data.iloc[-1]
    frame = wrap(data.copy())

    values = {}
    for name in SNAPSHOT_INDICATORS:
        try:
            frame[name]
            values[name] = fmt_num(frame.iloc[-1][name])
        except Exception:
            values[name] = "N/A"

    recent = data.tail(look_back_days)
    period_high = recent["High"].max()
    period_low = recent["Low"].min()
    first_close = recent["Close"].iloc[0]
    last_close = recent["Close"].iloc[-1]
    pct_change = (last_close - first_close) / first_close * 100 if first_close else float("nan")

    closes_table = markdown_table(
        ["Date", "Close", "Volume"],
        [
            [r["Date"].strftime("%Y-%m-%d"), fmt_num(r["Close"]), fmt_num(r["Volume"], 0)]
            for _, r in data.tail(10).iterrows()
        ],
    )
    indicator_table = markdown_table(
        ["Indicator", "Value"], [[k, v] for k, v in values.items()]
    )

    return f"""## VERIFIED MARKET SNAPSHOT — {symbol} (source of truth)

Computed deterministically from yfinance OHLCV. Any exact price, level, or
indicator value in your report must match this block. If another source
disagrees, flag the discrepancy — do not reconcile it by inventing a number.

**Latest bar on or before {curr_date}**: {latest['Date'].strftime('%Y-%m-%d')}

| Field | Value |
| --- | --- |
| Open | {fmt_num(latest.get('Open'))} |
| High | {fmt_num(latest.get('High'))} |
| Low | {fmt_num(latest.get('Low'))} |
| Close | {fmt_num(latest.get('Close'))} |
| Volume | {fmt_num(latest.get('Volume'), 0)} |

**Indicator values at that bar**

{indicator_table}

**Last {len(recent)} sessions**

| Field | Value |
| --- | --- |
| Period high | {fmt_num(period_high)} |
| Period low | {fmt_num(period_low)} |
| Period change | {fmt_num(pct_change)}% |
| Sessions in window | {len(recent)} |

**Last 10 closes**

{closes_table}{_staleness_note(data, curr_date)}
"""


def close_on(symbol: str, date: str) -> float | None:
    """Adjusted close on or before `date` — used by outcome scoring."""
    try:
        data = load_ohlcv(symbol, date, 400)
    except Exception:
        return None
    if data.empty:
        return None
    return float(data["Close"].iloc[-1])
