"""Shared plumbing for the dataflow modules: paths, config, cache, formatting.

Every dataflow function returns a *string* — a rendered markdown/plaintext block
meant to be read by an agent, not a Python object. That mirrors upstream
TradingAgents (whose LangChain tools also returned strings) and keeps the CLI
surface uniform: one command, one readable block on stdout.

Failures degrade to a clearly-marked `<unavailable: ...>` placeholder rather
than raising. An agent that sees the placeholder knows the source is missing and
must say so in its report; an agent that sees a traceback tends to invent
numbers instead.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data_cache"
RESULTS_DIR = ROOT / "results"
MEMORY_DIR = ROOT / "memory"

DATE_FMT = "%Y-%m-%d"

_DEFAULT_CONFIG = {
    "max_debate_rounds": 1,
    "max_risk_rounds": 1,
    "analysts": ["market", "sentiment", "news", "fundamentals"],
    "benchmark_ticker": "SPY",
    "reflection_horizon_days": 21,
    "cache_ttl_minutes": 60,
    "ohlcv_lookback_days": 365,
}


def load_config() -> dict:
    """Project config, with defaults filled in for any missing key."""
    config = dict(_DEFAULT_CONFIG)
    path = ROOT / "config.json"
    if path.exists():
        try:
            config.update(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            pass  # a broken config must not take the whole data layer down
    return config


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def today() -> str:
    return datetime.now().strftime(DATE_FMT)


def parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    return datetime.strptime(value, DATE_FMT)


def days_before(curr_date: str, days: int) -> str:
    return (parse_date(curr_date) - timedelta(days=days)).strftime(DATE_FMT)


def is_future(curr_date: str) -> bool:
    return parse_date(curr_date).date() > datetime.now().date()


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

_CRYPTO_HINTS = ("-USD", "-USDT", "-EUR")


def normalize_symbol(ticker: str) -> str:
    """Uppercase and strip the decorations users type ($NVDA, nvda, ' NVDA ')."""
    symbol = (ticker or "").strip().upper().lstrip("$")
    if not symbol:
        raise ValueError("empty ticker symbol")
    return symbol


def asset_type(symbol: str) -> str:
    """`crypto` or `stock` — decides which reports are expected to be thin."""
    return "crypto" if any(symbol.endswith(h) for h in _CRYPTO_HINTS) else "stock"


def benchmark_for(symbol: str, configured: str = "SPY") -> str:
    """Benchmark used for alpha in reflection. Regional listings get a local index."""
    if asset_type(symbol) == "crypto":
        return "BTC-USD"
    suffix_map = {
        ".T": "^N225", ".HK": "^HSI", ".L": "^FTSE", ".DE": "^GDAXI",
        ".PA": "^FCHI", ".AX": "^AXJO", ".TO": "^GSPTSE", ".SS": "000001.SS",
        ".SZ": "399001.SZ", ".NS": "^NSEI", ".KS": "^KS11",
    }
    for suffix, index in suffix_map.items():
        if symbol.endswith(suffix):
            return index
    return configured


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return CACHE_DIR / safe


def cache_read(name: str, ttl_minutes: int | None = None) -> str | None:
    """Return cached text if it exists and is younger than the TTL."""
    if ttl_minutes is None:
        ttl_minutes = load_config()["cache_ttl_minutes"]
    path = cache_path(name)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > ttl_minutes * 60:
        return None
    try:
        return path.read_text()
    except OSError:
        return None


def cache_write(name: str, content: str) -> None:
    try:
        cache_path(name).write_text(content)
    except OSError:
        pass  # cache is an optimization; never fail the request over it


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def unavailable(source: str, reason: str) -> str:
    """The single failure shape every dataflow uses.

    Agents are instructed to surface this verbatim in their reports, so the
    wording matters: it must read as missing data, never as neutral data.
    """
    return f"<unavailable: {source} — {reason}>"


def fmt_num(value, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    try:
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, int) and abs(value) < 1_000_000:
            return f"{value:,}"  # counts (employees, share counts) read wrong with decimals
        number = float(value)
        if number != number:  # NaN
            return "N/A"
        if abs(number) >= 1e9:
            return f"{number / 1e9:,.2f}B"
        if abs(number) >= 1e6:
            return f"{number / 1e6:,.2f}M"
        return f"{number:,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_(no rows)_"
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join([line, sep, *body])


def http_get_json(url: str, params: dict | None = None, timeout: float = 15.0):
    """GET returning parsed JSON, with the UA upstream uses for public endpoints."""
    import requests

    headers = {
        "User-Agent": "trading-agents-cc/1.0 (+https://github.com/TauricResearch/TradingAgents)",
        "Accept": "application/json",
    }
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None
