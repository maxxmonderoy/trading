"""News dataflows — ticker news (yfinance) and macro news (Google News RSS).

Port of upstream `yfinance_news.py` and the Google-News path in `interface.py`.
Both sources are keyless. Every item carries its publish date so the agent can
respect the analysis window instead of quietly citing something published after
the fact.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from urllib.parse import quote_plus

from .common import days_before, normalize_symbol, parse_date, unavailable

_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def _fetch_rss(url: str, timeout: float = 15.0) -> list[dict]:
    import requests

    headers = {"User-Agent": "trading-agents-cc/1.0 (+https://github.com/TauricResearch/TradingAgents)"}
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    items = []
    for item in root.findall(".//item"):
        published = item.findtext("pubDate", "")
        try:
            when = datetime.strptime(published, "%a, %d %b %Y %H:%M:%S %Z")
        except ValueError:
            when = None
        items.append({
            "title": (item.findtext("title", "") or "").strip(),
            "source": (item.findtext("source", "") or "").strip(),
            "link": (item.findtext("link", "") or "").strip(),
            "date": when,
        })
    return items


def _archive_caveat(curr_date: str) -> str:
    """Why an older window comes back empty.

    yfinance's feed serves current headlines and Google News RSS only indexes
    roughly the last month. An empty result for a historical date therefore says
    nothing about what was published then — a distinction the news analyst has to
    make, since "no news" and "no archive" point to opposite conclusions.
    """
    age = (datetime.now() - parse_date(curr_date)).days
    if age > 30:
        return (
            f" — the analysis date is {age} days back, and these free feeds only "
            "serve recent items. This is an ARCHIVE LIMITATION, not evidence that "
            "nothing was published. Report the window as uncovered"
        )
    return ""


def _render(title: str, items: list[dict], note: str = "", curr_date: str | None = None) -> str:
    if not items:
        caveat = _archive_caveat(curr_date) if curr_date else ""
        return unavailable(title, f"no items returned{caveat}")
    lines = [f"## {title}", ""]
    for item in items:
        when = item["date"].strftime("%Y-%m-%d") if item.get("date") else "undated"
        source = f" — {item['source']}" if item.get("source") else ""
        lines.append(f"**[{when}]{source}** {item['title']}")
        if item.get("summary"):
            lines.append(f"  {item['summary']}")
        if item.get("link"):
            lines.append(f"  {item['link']}")
        lines.append("")
    if note:
        lines.append(note)
    return "\n".join(lines)


def ticker_news(symbol: str, curr_date: str, look_back_days: int = 7, limit: int = 25) -> str:
    """Company/asset news from yfinance, falling back to Google News.

    yfinance's news feed is the higher-signal source (it is already
    ticker-resolved) but returns nothing for some symbols; the Google News
    fallback keeps the news analyst from writing a report off an empty block.
    """
    import yfinance as yf

    symbol = normalize_symbol(symbol)
    start = parse_date(days_before(curr_date, look_back_days))
    end = parse_date(curr_date)

    items: list[dict] = []
    try:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            raw = yf.Ticker(symbol).get_news(count=limit)
        for entry in raw or []:
            content = entry.get("content", entry)
            published = content.get("pubDate") or content.get("displayTime") or ""
            when = None
            if published:
                try:
                    when = datetime.fromisoformat(published.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    when = None
            # Respect the analysis window: no articles published after curr_date.
            if when and not (start.date() <= when.date() <= end.date()):
                continue
            provider = content.get("provider") or {}
            items.append({
                "title": content.get("title", ""),
                "summary": (content.get("summary") or "").strip()[:400],
                "source": provider.get("displayName", "") if isinstance(provider, dict) else "",
                "link": (content.get("canonicalUrl") or {}).get("url", "") if isinstance(content.get("canonicalUrl"), dict) else "",
                "date": when,
            })
    except Exception:
        items = []

    if items:
        return _render(f"News — {symbol} ({start.date()} → {end.date()})", items)

    try:
        fallback = _fetch_rss(_GOOGLE_NEWS_RSS.format(query=quote_plus(f"{symbol} stock")))
    except Exception as exc:
        return unavailable("ticker news", f"yfinance empty and Google News failed: {type(exc).__name__}: {exc}")

    windowed = [
        i for i in fallback
        if i["date"] is None or start.date() <= i["date"].date() <= end.date()
    ][:limit]
    return _render(
        f"News — {symbol} ({start.date()} → {end.date()})",
        windowed,
        "_Source: Google News RSS (yfinance returned no items for this symbol)._",
        curr_date,
    )


_MACRO_QUERIES = [
    "federal reserve interest rates",
    "inflation CPI report",
    "stock market outlook",
    "recession economic growth",
]


def global_news(curr_date: str, look_back_days: int = 7, limit: int = 20) -> str:
    """Macro/world news relevant to trading — upstream's `get_global_news`."""
    start = parse_date(days_before(curr_date, look_back_days))
    end = parse_date(curr_date)

    collected: list[dict] = []
    errors: list[str] = []
    for query in _MACRO_QUERIES:
        try:
            for item in _fetch_rss(_GOOGLE_NEWS_RSS.format(query=quote_plus(query))):
                if item["date"] and not (start.date() <= item["date"].date() <= end.date()):
                    continue
                item["topic"] = query
                collected.append(item)
        except Exception as exc:
            errors.append(f"{query}: {type(exc).__name__}")

    if not collected:
        reason = "; ".join(errors) or f"no items in window{_archive_caveat(curr_date)}"
        return unavailable("global news", reason)

    seen: set[str] = set()
    unique = []
    for item in sorted(collected, key=lambda i: i["date"] or datetime.min, reverse=True):
        key = item["title"].lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    note = f"_Topics queried: {', '.join(_MACRO_QUERIES)}._"
    if errors:
        note += f"\n_Partial failures: {'; '.join(errors)}._"
    return _render(f"Global macro news ({start.date()} → {end.date()})", unique[:limit], note, curr_date)


def search_news(query: str, curr_date: str, look_back_days: int = 7, limit: int = 20) -> str:
    """Free-form news search — for following a specific catalyst or narrative."""
    start = parse_date(days_before(curr_date, look_back_days))
    end = parse_date(curr_date)
    try:
        items = _fetch_rss(_GOOGLE_NEWS_RSS.format(query=quote_plus(query)))
    except Exception as exc:
        return unavailable("news search", f"{type(exc).__name__}: {exc}")

    windowed = [
        i for i in items
        if i["date"] is None or start.date() <= i["date"].date() <= end.date()
    ][:limit]
    return _render(f'News search — "{query}" ({start.date()} → {end.date()})', windowed, "", curr_date)

