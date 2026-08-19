"""Free historical tick data from Dukascopy's public feed.

Dukascopy publishes hourly tick files at a predictable URL and has done for
years. That makes it the one free source with enough history to actually
validate an intraday strategy — yfinance caps at 30 days of 5-minute bars,
which is not a sample, it is an anecdote.

**These are index CFDs, not the futures contract.** `USATECHIDXUSD` tracks the
Nasdaq 100 and `USA500IDXUSD` the S&P 500. Consequences worth holding onto:

- No quarterly roll, so no roll gaps to stitch — an advantage over raw futures.
- Prices sit at a basis to the futures and differ by carry, so exact levels do
  not transfer. A strategy validated here is validated on the *index*, and NQ's
  prior-day high is not this series' prior-day high.
- Spreads are the broker's, not the exchange's.

Good enough to answer "does sweep → MSS → FVG have an edge on the Nasdaq".
Not a substitute for NQ futures data if the answer is yes and you intend to
trade the contract.

**Be polite.** This is a free public endpoint. The default pacing is deliberately
slow; a burst gets the IP throttled, which is exactly what happened during
development. Downloads are cached and resumable, so a slow run is cheap.
"""

from __future__ import annotations

import datetime as dt
import lzma
import struct
import time
from pathlib import Path

from .common import ROOT

FEED = "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m:02d}/{d:02d}/{h:02d}h_ticks.bi5"
RAW_DIR = ROOT / "data_cache" / "dukascopy"

# Instrument -> (symbol, price divisor). Index CFDs quote with 3 implied
# decimals; FX majors use 5.
INSTRUMENTS = {
    "NAS100": ("USATECHIDXUSD", 1000.0, "Nasdaq 100 index CFD (NQ proxy)"),
    "SPX500": ("USA500IDXUSD", 1000.0, "S&P 500 index CFD (ES proxy)"),
    "EURUSD": ("EURUSD", 100000.0, "Euro / US Dollar"),
    "GBPUSD": ("GBPUSD", 100000.0, "British Pound / US Dollar"),
}

TICK_STRUCT = struct.Struct(">IIIff")   # ms offset, ask, bid, ask vol, bid vol


def _raw_path(symbol: str, day: dt.date, hour: int) -> Path:
    return RAW_DIR / symbol / f"{day:%Y%m%d}_{hour:02d}.bi5"


def fetch_hour(symbol: str, day: dt.date, hour: int, delay: float = 2.5,
               retries: int = 3, timeout: float = 45.0) -> bytes | None:
    """One hour of ticks, cached on disk. None means no data for that hour.

    A 503 is ambiguous here: it means both "market closed, nothing to serve" and
    "you are going too fast". They are distinguished by persistence — a closed
    hour 503s forever, throttling clears. Retries with growing backoff separate
    the two without treating a weekend as an outage.
    """
    path = _raw_path(symbol, day, hour)
    if path.exists():
        blob = path.read_bytes()
        return blob or None

    import requests

    url = FEED.format(sym=symbol, y=day.year, m=day.month - 1, d=day.day, h=hour)
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

    for attempt in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
        except Exception:
            time.sleep(delay * (attempt + 2) * 3)
            continue

        if response.status_code == 200 and not response.content[:6].startswith(b"<html"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
            time.sleep(delay)
            return response.content or None

        if response.status_code == 404:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")          # remember the gap; do not refetch
            time.sleep(delay)
            return None

        time.sleep(delay * (attempt + 2) * 3)

    return None


def decode(blob: bytes, day: dt.date, hour: int, divisor: float) -> list[tuple]:
    """`.bi5` -> [(timestamp, bid, ask, bid_vol, ask_vol)].

    The payload is LZMA1 in the legacy 'alone' container, not xz — the default
    decoder rejects it, which reads as corrupt data rather than wrong format.
    """
    if not blob:
        return []
    try:
        raw = lzma.decompress(blob, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError:
        try:
            raw = lzma.decompress(blob, format=lzma.FORMAT_AUTO)
        except lzma.LZMAError:
            return []

    base = dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.timezone.utc)
    out = []
    for offset in range(0, len(raw) - TICK_STRUCT.size + 1, TICK_STRUCT.size):
        ms, ask, bid, ask_vol, bid_vol = TICK_STRUCT.unpack_from(raw, offset)
        out.append((
            base + dt.timedelta(milliseconds=ms),
            bid / divisor, ask / divisor, bid_vol, ask_vol,
        ))
    return out


def ticks_to_bars(ticks: list[tuple], interval: str = "1min") -> "pd.DataFrame":
    """Aggregate ticks into OHLCV bars at the mid price, in exchange time.

    Mid rather than bid or ask: a bar built from bids sits systematically below
    one built from asks, and every level in this framework would shift by half a
    spread depending on which side happened to be used. Volume is tick count —
    Dukascopy's CFD volumes are broker-side and not comparable to exchange
    volume, so counting prints is the more honest field.
    """
    import pandas as pd

    if not ticks:
        return pd.DataFrame()

    frame = pd.DataFrame(ticks, columns=["ts", "bid", "ask", "bid_vol", "ask_vol"])
    frame["mid"] = (frame["bid"] + frame["ask"]) / 2
    frame = frame.set_index("ts").tz_convert("America/New_York")

    bars = frame["mid"].resample(interval, label="left", closed="left").ohlc()
    bars["volume"] = frame["mid"].resample(interval, label="left", closed="left").count()
    return bars.dropna(subset=["open", "high", "low", "close"])


def build(instrument: str = "NAS100") -> str:
    """Build the CSV from whatever hours are already cached.

    Separate from `download` on purpose. A long download can be interrupted —
    process exit, network, an impatient ctrl-C — and an earlier version of this
    module only wrote the CSV after the whole range finished, so 162 successfully
    fetched hours produced nothing. Cached bytes should always be convertible
    into usable data without touching the network.
    """
    import pandas as pd

    if instrument not in INSTRUMENTS:
        return f"<error: unknown instrument {instrument!r}. Options: {', '.join(INSTRUMENTS)}>"
    symbol, divisor, label = INSTRUMENTS[instrument]

    cache = RAW_DIR / symbol
    if not cache.exists():
        return f"<error: nothing cached for {instrument}. Run `bin/ta duka download {instrument}` first.>"

    all_ticks: list[tuple] = []
    files = sorted(cache.glob("*.bi5"))
    for path in files:
        if path.stat().st_size == 0:
            continue
        stem = path.stem                       # YYYYMMDD_HH
        try:
            day = dt.date(int(stem[0:4]), int(stem[4:6]), int(stem[6:8]))
            hour = int(stem[9:11])
        except (ValueError, IndexError):
            continue
        all_ticks.extend(decode(path.read_bytes(), day, hour, divisor))

    if not all_ticks:
        return f"<error: {len(files)} cached files but no decodable ticks for {instrument}.>"

    all_ticks.sort(key=lambda t: t[0])
    bars = ticks_to_bars(all_ticks, "1min")
    out = ROOT / "data" / f"{instrument}_1m.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    frame = bars.reset_index().rename(columns={"ts": "timestamp"})
    frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)
    frame.to_csv(out, index=False)

    sessions = bars.index.normalize().nunique()
    return (
        f"Built `{out.relative_to(ROOT)}` from {len(files)} cached hours: "
        f"**{len(all_ticks):,} ticks → {len(bars):,} 1m bars over {sessions} sessions** "
        f"({bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d} ET)."
    )


def download(instrument: str = "NAS100", start: str = "", end: str = "",
             delay: float = 2.5, hours: tuple[int, ...] | None = None) -> str:
    """Download a date range and write `data/<instrument>_1m.csv`.

    Resumable: hours already on disk are skipped, so an interrupted run costs
    nothing to restart. Expect this to be slow by design — roughly `delay`
    seconds per hour of market data, so a year of 24h coverage is several hours
    of wall time. Run it in the background and leave it alone.
    """
    import pandas as pd

    if instrument not in INSTRUMENTS:
        return f"<error: unknown instrument {instrument!r}. Options: {', '.join(INSTRUMENTS)}>"
    symbol, divisor, label = INSTRUMENTS[instrument]

    first = dt.date.fromisoformat(start) if start else dt.date.today() - dt.timedelta(days=30)
    last = dt.date.fromisoformat(end) if end else dt.date.today() - dt.timedelta(days=1)
    hours = hours or tuple(range(24))

    all_ticks: list[tuple] = []
    days = fetched = empty = 0
    day = first
    while day <= last:
        if day.weekday() < 5:          # the feed has nothing on weekends
            days += 1
            for hour in hours:
                blob = fetch_hour(symbol, day, hour, delay=delay)
                if blob:
                    decoded = decode(blob, day, hour, divisor)
                    if decoded:
                        all_ticks.extend(decoded)
                        fetched += 1
                        continue
                empty += 1
            # Flush to CSV periodically. A run this long will sometimes be
            # interrupted, and partial output beats none.
            if days % 10 == 0:
                try:
                    build(instrument)
                except Exception:
                    pass
        day += dt.timedelta(days=1)

    if not all_ticks:
        return (
            f"<error: no ticks retrieved for {instrument} between {first} and {last}. "
            "Either the range is entirely non-trading, or the feed is throttling — "
            "it returns 503 for both. Wait and rerun; cached hours are not refetched.>"
        )

    bars = ticks_to_bars(all_ticks, "1min")
    out = ROOT / "data" / f"{instrument}_1m.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    frame = bars.reset_index().rename(columns={"ts": "timestamp"})
    frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)
    frame.to_csv(out, index=False)

    sessions = bars.index.normalize().nunique()
    return f"""## Downloaded {instrument} — {label}

**{len(all_ticks):,} ticks** → **{len(bars):,} 1-minute bars** over **{sessions} sessions**
{bars.index[0]:%Y-%m-%d %H:%M} → {bars.index[-1]:%Y-%m-%d %H:%M} (America/New_York)

Hours fetched {fetched} | empty or unavailable {empty} | weekdays scanned {days}

Written to `{out.relative_to(ROOT)}`. It is picked up automatically by
`bin/ta bt` and resampled to every timeframe the framework needs.

> These are index CFD prices, not {('NQ' if instrument == 'NAS100' else 'ES')}
> futures. No roll gaps, but the series sits at a basis to the contract, so
> levels do not transfer tick-for-tick. Validate the *logic* here; re-validate on
> futures data before trading the contract.
"""
