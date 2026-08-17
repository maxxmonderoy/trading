"""Macro dataflows — FRED series and Polymarket implied probabilities.

Port of upstream `dataflows/fred.py` and `dataflows/polymarket.py`.

FRED needs a free API key (`FRED_API_KEY`); when it is absent the command says
so plainly instead of failing, and the news analyst is instructed to write its
macro section without hard numbers rather than inventing them. Polymarket's
Gamma API is keyless.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .common import (
    days_before,
    env,
    fmt_num,
    http_get_json,
    markdown_table,
    parse_date,
    unavailable,
)

FRED_API_BASE = "https://api.stlouisfed.org/fred"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# Alias → FRED series id. Aliases are what an agent naturally types; the series
# ids are what FRED accepts. Unknown aliases fall through as raw series ids.
MACRO_SERIES = {
    "fed_funds_rate": "FEDFUNDS", "federal_funds_rate": "FEDFUNDS", "fed_funds": "FEDFUNDS",
    "2y_treasury": "DGS2", "10y_treasury": "DGS10", "30y_treasury": "DGS30",
    "10y_2y_spread": "T10Y2Y", "yield_curve": "T10Y2Y",
    "cpi": "CPIAUCSL", "core_cpi": "CPILFESL", "pce": "PCEPI", "core_pce": "PCEPILFE",
    "inflation_expectations": "T10YIE",
    "real_gdp": "GDPC1", "gdp": "GDP", "industrial_production": "INDPRO",
    "unemployment_rate": "UNRATE", "unemployment": "UNRATE",
    "nonfarm_payrolls": "PAYEMS", "payrolls": "PAYEMS", "initial_claims": "ICSA",
    "m2": "M2SL", "money_supply": "M2SL",
    "vix": "VIXCLS", "dollar_index": "DTWEXBGS",
    "consumer_sentiment": "UMCSENT", "housing_starts": "HOUST", "retail_sales": "RSAFS",
}

MAX_ROWS = 24


def _resolve_series(indicator: str) -> str:
    key = (indicator or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in MACRO_SERIES:
        return MACRO_SERIES[key]
    candidate = (indicator or "").strip().upper()
    if not candidate or len(candidate) > 30 or any(c.isspace() for c in candidate):
        raise ValueError(
            f"unknown indicator {indicator!r}. Known aliases: {', '.join(sorted(MACRO_SERIES))}"
        )
    return candidate


def macro_indicators(indicator: str, curr_date: str, look_back_days: int = 365) -> str:
    """Observations for one FRED series, ending at the analysis date."""
    api_key = env("FRED_API_KEY")
    if not api_key:
        return unavailable(
            "FRED",
            "FRED_API_KEY is not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html and add it to .env. "
            "Write your macro commentary qualitatively and state that hard series "
            "values were unavailable — do not recall figures from memory",
        )

    try:
        series_id = _resolve_series(indicator)
    except ValueError as exc:
        return f"<error: {exc}>"

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": days_before(curr_date, look_back_days),
        "observation_end": curr_date,
        "sort_order": "desc",
        "limit": MAX_ROWS,
    }
    try:
        payload = http_get_json(f"{FRED_API_BASE}/series/observations", params)
        meta = http_get_json(f"{FRED_API_BASE}/series", {
            "series_id": series_id, "api_key": api_key, "file_type": "json"
        })
    except Exception as exc:
        return unavailable("FRED", f"{type(exc).__name__}: {exc}")

    observations = [o for o in payload.get("observations", []) if o.get("value") not in (".", None)]
    if not observations:
        return unavailable("FRED", f"no observations for {series_id} in the window")

    info = (meta.get("seriess") or [{}])[0]
    rows = [[o["date"], o["value"]] for o in observations]
    latest = float(observations[0]["value"])
    oldest = float(observations[-1]["value"])
    change = latest - oldest

    return f"""## FRED — {info.get('title', series_id)} ({series_id})

**Units**: {info.get('units', 'n/a')} | **Frequency**: {info.get('frequency', 'n/a')} | **Last updated**: {info.get('last_updated', 'n/a')[:10]}

**Latest**: {fmt_num(latest)} ({observations[0]['date']}) | **Change over window**: {fmt_num(change)} (from {fmt_num(oldest)} on {observations[-1]['date']})

{markdown_table(['Date', 'Value'], rows)}
"""


def _parse_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


def prediction_markets(topic: str, limit: int = 10) -> str:
    """Live market-implied probabilities for forward-looking events.

    Useful precisely where analysts are weakest: putting a number on "will the
    Fed cut in March" instead of describing the debate about it.
    """
    params = {
        "active": "true",
        "closed": "false",
        "limit": max(limit * 5, 40),
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        markets = http_get_json(f"{GAMMA_BASE}/markets", params)
    except Exception as exc:
        return unavailable("Polymarket", f"{type(exc).__name__}: {exc}")

    if not isinstance(markets, list):
        return unavailable("Polymarket", "unexpected response shape")

    terms = [t for t in (topic or "").lower().split() if len(t) > 2]
    now = datetime.now(timezone.utc)

    scored = []
    for market in markets:
        question = (market.get("question") or "")
        question_lower = question.lower()
        description_lower = (market.get("description") or "").lower()
        # The question is the market; the description is boilerplate that
        # mentions "rate", "close", "resolve" in nearly every listing. Weight
        # accordingly and require a hit in the question itself, or a topical
        # search returns whatever happens to have the highest 24h volume.
        question_hits = sum(1 for t in terms if t in question_lower)
        description_hits = sum(1 for t in terms if t in description_lower)
        hits = question_hits * 3 + description_hits
        if terms and not question_hits:
            continue
        end_date = market.get("endDate") or ""
        try:
            ends = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if ends < now:
                continue
        except (ValueError, AttributeError):
            ends = None

        prices = _parse_list(market.get("outcomePrices"))
        outcomes = _parse_list(market.get("outcomes"))
        if not prices or not outcomes:
            continue
        scored.append({
            "question": question,
            "hits": hits,
            "volume": float(market.get("volume24hr") or 0),
            "ends": ends.strftime("%Y-%m-%d") if ends else "n/a",
            "odds": ", ".join(
                f"{o}: {float(p) * 100:.1f}%" for o, p in zip(outcomes, prices)
            ),
        })

    if not scored:
        return unavailable("Polymarket", f"no active markets matched {topic!r}")

    scored.sort(key=lambda m: (m["hits"], m["volume"]), reverse=True)
    rows = [
        [m["question"][:90], m["odds"], m["ends"], fmt_num(m["volume"], 0)]
        for m in scored[:limit]
    ]
    table = markdown_table(["Market", "Implied odds", "Resolves", "24h volume"], rows)
    return (
        f"## Polymarket — implied probabilities for '{topic}'\n\n{table}\n\n"
        "_Prices are market-implied probabilities, not forecasts. Thin markets "
        "(low 24h volume) carry wide spreads — weight them accordingly._"
    )


def market_regime(curr_date: str) -> str:
    """A quick read on the macro backdrop: index trend, volatility, rates, dollar.

    Not in upstream — added because every analyst here otherwise re-derives the
    same regime picture from scratch, and a shared deterministic read keeps the
    four reports from contradicting each other on basic market conditions.
    """
    from .market import load_ohlcv

    probes = [
        ("SPY", "S&P 500 ETF"),
        ("QQQ", "Nasdaq 100 ETF"),
        ("^VIX", "Volatility index"),
        ("^TNX", "10-year Treasury yield"),
        ("DX-Y.NYB", "US dollar index"),
        ("GC=F", "Gold futures"),
        ("CL=F", "Crude oil futures"),
        ("BTC-USD", "Bitcoin"),
    ]

    rows = []
    for symbol, label in probes:
        try:
            # 200 *trading* days needs ~290 calendar days; 400 leaves headroom
            # for holidays so the 200-day trend column is never silently N/A.
            data = load_ohlcv(symbol, curr_date, 400)
        except Exception:
            data = None
        if data is None or data.empty:
            rows.append([label, symbol, "N/A", "N/A", "N/A", "N/A"])
            continue
        closes = data["Close"]
        last = float(closes.iloc[-1])

        def change(days: int) -> str:
            if len(closes) <= days:
                return "N/A"
            prior = float(closes.iloc[-1 - days])
            return f"{(last - prior) / prior * 100:+.1f}%" if prior else "N/A"

        sma200 = closes.rolling(200).mean().iloc[-1] if len(closes) >= 200 else None
        trend = "N/A" if sma200 is None or sma200 != sma200 else ("above 200d" if last > sma200 else "below 200d")
        rows.append([label, symbol, fmt_num(last), change(5), change(21), trend])

    table = markdown_table(
        ["Asset", "Symbol", "Last", "1w", "1m", "Trend"], rows
    )
    return (
        f"## Market regime snapshot — {curr_date}\n\n{table}\n\n"
        "_Deterministic read of the macro backdrop. Every agent in this run sees "
        "the same numbers; cite these rather than characterising the tape from memory._"
    )
