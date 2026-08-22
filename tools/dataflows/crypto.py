"""Binance bulk kline downloader — deep intraday history, free and keyless.

The SMC framework's binding constraint is sample size: yfinance serves 30 days
of 5m bars, this strategy resolves ~0.07 setups per session, and the EV gate
needs 20. That is a two-year wait on NQ.

Binance publishes its entire kline history as monthly ZIPs at
`data.binance.vision`, no key and no rate limit worth worrying about. Years of
5m bars arrive in one pass, which turns "measurable in two years" into
"measurable this afternoon" — on crypto rather than on CME index futures.

That substitution is not free, and the honest accounting is in `SESSION_CAVEAT`
below: this framework is built on exchange sessions, and crypto has none. What
the exercise buys is a real measurement of whether the sweep → MSS → FVG state
machine has any edge at all on real market data. If it shows nothing over
hundreds of resolved setups, that is cheap and important information before
anyone spends money on CME data.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta

import pandas as pd

from .common import CACHE_DIR, unavailable

VISION = "https://data.binance.vision/data"

# Binance kline CSVs carry these columns; older files ship without a header row.
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]

SESSION_CAVEAT = """\
> **This is a 24/7 market measured with a session-based framework.** Crypto has
> no exchange close, so the session concepts are redefined rather than borrowed:
> a "session" is one UTC calendar day, prior-day high/low are the previous UTC
> day's extremes, and overnight levels are dropped entirely because there is no
> overnight. Kill zones are kept — TradFi-hours participation in crypto is real
> — but they are no longer the structural boundaries they are on CME.
>
> Treat a result here as evidence about **this state machine on crypto**, not as
> a forecast for NQ. What transfers is the mechanism; what does not is the
> liquidity structure the mechanism was designed around."""


def _months_back(count: int) -> list[str]:
    """The last `count` complete months, oldest first, as YYYY-MM.

    The current month is excluded: Binance publishes a month's archive after it
    closes, so requesting it is a guaranteed 404.
    """
    cursor = date.today().replace(day=1) - timedelta(days=1)
    months = []
    for _ in range(count):
        months.append(cursor.strftime("%Y-%m"))
        cursor = cursor.replace(day=1) - timedelta(days=1)
    return list(reversed(months))


def _archive_url(symbol: str, interval: str, month: str, market: str) -> str:
    segment = "futures/um" if market == "futures" else "spot"
    return f"{VISION}/{segment}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def _parse_archive(payload: bytes) -> pd.DataFrame:
    """One monthly ZIP into a frame, tolerating both header and headerless CSVs."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        raw = archive.read(name).decode("utf-8", errors="replace")

    first = raw.split("\n", 1)[0]
    has_header = "open_time" in first.lower()
    frame = pd.read_csv(
        io.StringIO(raw),
        header=0 if has_header else None,
        names=None if has_header else KLINE_COLUMNS,
    )
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    return frame


def _to_utc(series: pd.Series) -> pd.Series:
    """Binance timestamps are epoch ms, but newer archives switched to µs.

    Guessing wrong puts every bar in 1970 or in the year 57000, so the unit is
    inferred from magnitude rather than assumed.
    """
    numeric = pd.to_numeric(series, errors="coerce")
    sample = numeric.dropna()
    if sample.empty:
        return pd.to_datetime(numeric, unit="ms", utc=True)
    unit = "us" if float(sample.iloc[0]) > 1e14 else "ms"
    return pd.to_datetime(numeric, unit=unit, utc=True)


def fetch_klines(symbol: str = "BTCUSDT", interval: str = "5m", months: int = 12,
                 market: str = "futures", out_path: str | None = None) -> str:
    """Download monthly kline archives and write one CSV the backfill can read.

    Missing months are reported, never silently skipped — a gap in the middle of
    a backfill window changes what the sample covers, and a scan that quietly
    spans a hole would misreport its own session count.
    """
    import requests

    symbol = (symbol or "").strip().upper()
    months_wanted = _months_back(months)
    frames, missing, failed = [], [], []

    for month in months_wanted:
        url = _archive_url(symbol, interval, month, market)
        try:
            response = requests.get(url, timeout=60)
        except Exception as exc:  # noqa: BLE001 — the reason is the report
            failed.append(f"{month} ({type(exc).__name__})")
            continue
        if response.status_code == 404:
            missing.append(month)
            continue
        if response.status_code != 200:
            failed.append(f"{month} (HTTP {response.status_code})")
            continue
        try:
            frames.append(_parse_archive(response.content))
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{month} ({type(exc).__name__}: {exc})")

    if not frames:
        return unavailable(
            f"Binance {symbol} {interval}",
            "no monthly archives downloaded — "
            + (f"missing: {', '.join(missing)}; " if missing else "")
            + (f"failed: {', '.join(failed)}" if failed else "check the symbol and market"),
        )

    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = _to_utc(frame["open_time"])
    frame = frame.dropna(subset=["timestamp"])
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = (
        frame[["timestamp", "open", "high", "low", "close", "volume"]]
        .drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    destination = out_path or str(CACHE_DIR / f"{symbol}_{interval}_{months}m.csv")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Timestamps are written offset-aware so the backfill loader never has to be
    # told a timezone — the commonest way a backfill is silently ruined.
    frame.to_csv(destination, index=False)

    span_days = (frame["timestamp"].iloc[-1] - frame["timestamp"].iloc[0]).days
    notes = []
    if missing:
        notes.append(f"> **{len(missing)} month(s) not published**: {', '.join(missing)}.")
    if failed:
        notes.append(f"> ⚠️ **{len(failed)} month(s) failed to download**: {', '.join(failed)}.")
    if missing or failed:
        notes.append(
            "> The written file spans the months that did arrive. A gap changes what the "
            "sample covers — re-run for the missing months before treating the span as continuous."
        )

    return (
        f"## Binance klines — {symbol} {interval} ({market})\n\n"
        f"{len(frame):,} bars, {span_days} days "
        f"({frame['timestamp'].iloc[0].date()} → {frame['timestamp'].iloc[-1].date()}), "
        f"{len(frames)} of {months} monthly archives.\n\n"
        f"Written to `{destination}`\n\n"
        + ("\n".join(notes) + "\n\n" if notes else "")
        + f"Next:\n```\nbin/ta smc journal backfill {symbol} --csv {destination} --interval {interval}\n```\n\n"
        + SESSION_CAVEAT + "\n"
    )
