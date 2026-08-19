"""Data feeds — yfinance now, external vendor CSV when you buy history.

The adapter boundary exists because the free source is a placeholder. yfinance
caps intraday at 30 days of 5m bars, which is far too little to validate an
intraday strategy; a real answer needs years. Everything downstream reads a
plain DataFrame, so swapping in Databento or a broker export is a loader change
and nothing else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"

sys.path.insert(0, str(ROOT / "tools"))

REQUIRED = ["open", "high", "low", "close", "volume"]


def from_yfinance(symbol: str = "NQ", interval: str = "5m") -> pd.DataFrame:
    """Free source. Enough to prove the harness runs, not enough to trust a result."""
    from dataflows import futures as F

    spec = F.resolve(symbol)
    frame = F.intraday(spec, interval)
    if frame.empty:
        return frame
    out = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
    out.columns = [c.lower() for c in out.columns]
    # backtrader wants naive timestamps; we keep everything in exchange time.
    out.index = out.index.tz_localize(None)
    return out


def from_csv(path: str | Path) -> pd.DataFrame:
    """Vendor export. Expects a timestamp column plus OHLCV, any capitalisation.

    Timestamps must be **exchange time (ET)**. A vendor that ships UTC will
    silently shift every session boundary and kill zone by 4-5 hours, which
    produces a backtest that looks fine and means nothing.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such data file: {path}")

    frame = pd.read_csv(path)
    frame.columns = [c.strip().lower() for c in frame.columns]

    time_col = next(
        (c for c in ("timestamp", "datetime", "date", "time", "ts") if c in frame.columns),
        None,
    )
    if time_col is None:
        raise ValueError(f"{path.name}: no timestamp column found (looked for timestamp/datetime/date/time/ts)")

    missing = [c for c in REQUIRED if c not in frame.columns]
    if missing:
        raise ValueError(f"{path.name}: missing column(s) {', '.join(missing)}")

    frame[time_col] = pd.to_datetime(frame[time_col])
    frame = frame.set_index(time_col).sort_index()
    if frame.index.tz is not None:
        frame.index = frame.index.tz_convert("America/New_York").tz_localize(None)
    return frame[REQUIRED].dropna()


def load(symbol: str = "NQ", interval: str = "5m", csv: str | None = None) -> tuple[pd.DataFrame, str]:
    """Return (bars, provenance). Provenance is printed with every result.

    A backtest number without its data source attached is unusable — the whole
    question is whether the sample was big enough to mean anything.
    """
    if csv:
        return from_csv(csv), f"csv:{Path(csv).name}"
    candidate = DATA_DIR / f"{symbol.upper()}_{interval}.csv"
    if candidate.exists():
        return from_csv(candidate), f"csv:{candidate.name}"
    return from_yfinance(symbol, interval), f"yfinance:{symbol}:{interval}"


def describe(bars: pd.DataFrame, provenance: str) -> str:
    if bars.empty:
        return f"**No data** ({provenance})"
    sessions = bars.index.normalize().nunique()
    span_days = (bars.index[-1] - bars.index[0]).days
    warning = ""
    if sessions < 250:
        warning = (
            f"\n\n> ⚠️ **{sessions} sessions is not enough to validate anything.** "
            "Expect roughly 250 sessions per year; a credible out-of-sample test wants "
            "multiple years across different regimes. Any result below is a smoke test "
            "of the machinery, not evidence about the strategy."
        )
    return (
        f"**Source** {provenance} | **{len(bars):,} bars** | **{sessions} sessions** | "
        f"{bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d} ({span_days} days)" + warning
    )


RESAMPLE_RULES = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}


def resample(bars: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Aggregate finer bars up to a coarser interval.

    Buy 1-minute history once and every timeframe the framework needs is
    derivable from it. The reverse is impossible, which is why the purchase
    decision matters more than it looks: 5-minute data permanently forecloses
    the 1-minute execution the spec calls for.

    Bars are labelled by their opening time and closed on the left, matching how
    every charting package and the yfinance feed already behave — get this
    backwards and a 09:30 bar silently becomes the 09:25 bar, shifting every
    session boundary and kill-zone edge by one bar.
    """
    if interval not in RESAMPLE_RULES:
        raise ValueError(f"cannot resample to {interval!r}. Options: {', '.join(RESAMPLE_RULES)}")
    if bars.empty:
        return bars

    out = bars.resample(RESAMPLE_RULES[interval], label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    })
    return out.dropna(subset=["open", "high", "low", "close"])


def load_multi(symbol: str = "NQ", intervals: tuple[str, ...] = ("5m", "15m", "1h"),
               csv: str | None = None) -> tuple[dict[str, pd.DataFrame], str]:
    """All timeframes the framework needs, from the finest source available.

    Prefers a local 1-minute file and derives the rest; falls back to fetching
    each interval separately from yfinance, which is the only option while the
    free source is in play.
    """
    base = Path(csv) if csv else DATA_DIR / f"{symbol.upper()}_1m.csv"
    if base.exists():
        fine = from_csv(base)
        frames = {"1m": fine}
        for interval in intervals:
            frames[interval] = fine if interval == "1m" else resample(fine, interval)
        return frames, f"csv:{base.name} (resampled)"

    frames = {}
    for interval in intervals:
        frame, _ = load(symbol, interval)
        if not frame.empty:
            frames[interval] = frame
    return frames, f"yfinance:{symbol} (per-interval)"
