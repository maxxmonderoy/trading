"""Retail-sentiment dataflows — StockTwits and Reddit.

Port of upstream `dataflows/stocktwits.py` and `dataflows/reddit.py`, including
the design lesson that motivated them: the original social analyst had a prompt
demanding social-media analysis but only a news tool, and models filled the gap
by inventing Reddit and StockTwits posts (upstream issues #557, #796). The fix
there and here is the same — fetch the real posts, and make an empty fetch look
unmistakably empty.

Reddit is read through the public Atom search feed rather than the JSON API:
the JSON endpoint rate-limits anonymous clients aggressively, the RSS one does not.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from .common import cache_read, cache_write, normalize_symbol, today, unavailable

_UA = "trading-agents-cc/1.0 (+https://github.com/TauricResearch/TradingAgents)"
_STOCKTWITS_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_REDDIT_RSS = "https://www.reddit.com/r/{sub}/search.rss?{qs}"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")


def _stocktwits_symbol(ticker: str) -> str:
    """StockTwits indexes crypto as `BTC.X`, equities as the bare symbol."""
    symbol = normalize_symbol(ticker)
    if symbol.endswith("-USD"):
        return f"{symbol[:-4]}.X"
    return symbol


def stocktwits(ticker: str, limit: int = 30) -> str:
    """Recent cashtag messages with their user-applied Bullish/Bearish labels.

    The labels are the point: they give a directly countable retail
    bull/bear ratio that does not depend on an LLM scoring free text.
    """
    import requests

    symbol = _stocktwits_symbol(ticker)
    url = _STOCKTWITS_API.format(ticker=symbol)
    try:
        response = requests.get(url, headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return unavailable("StockTwits", f"{type(exc).__name__}: {exc}")

    messages = payload.get("messages", [])[:limit]
    if not messages:
        return unavailable("StockTwits", f"no messages for ${symbol}")

    bullish = bearish = unlabeled = 0
    lines = []
    for message in messages:
        entities = message.get("entities") or {}
        sentiment = (entities.get("sentiment") or {}) if isinstance(entities, dict) else {}
        label = (sentiment or {}).get("basic") if isinstance(sentiment, dict) else None
        if label == "Bullish":
            bullish += 1
        elif label == "Bearish":
            bearish += 1
        else:
            unlabeled += 1
        created = (message.get("created_at") or "")[:10]
        body = " ".join((message.get("body") or "").split())[:280]
        user = (message.get("user") or {}).get("username", "anon")
        followers = (message.get("user") or {}).get("followers", 0)
        lines.append(f"[{created}] ({label or 'no label'}) @{user} ({followers} followers): {body}")

    total_labeled = bullish + bearish
    ratio = (
        f"{bullish / total_labeled * 100:.0f}% bullish / {bearish / total_labeled * 100:.0f}% bearish"
        if total_labeled else "no labeled messages"
    )
    header = (
        f"## StockTwits — ${symbol} ({len(messages)} most recent messages)\n\n"
        f"**Labeled sentiment**: {bullish} bullish, {bearish} bearish, {unlabeled} unlabeled "
        f"→ {ratio} (of {total_labeled} labeled)\n"
    )
    return header + "\n" + "\n".join(lines)


def _search_qs(ticker: str, limit: int) -> str:
    return urlencode({
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",
        "limit": limit,
    })


def _parse_atom(payload: bytes, sub: str) -> list[dict]:
    root = ET.fromstring(payload)
    posts = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        title = (entry.findtext("atom:title", "", _ATOM_NS) or "").strip()
        updated = entry.findtext("atom:updated", "", _ATOM_NS) or ""
        author = entry.find("atom:author/atom:name", _ATOM_NS)
        content = entry.findtext("atom:content", "", _ATOM_NS) or ""
        # Atom content is HTML-escaped markup; strip tags for a readable excerpt.
        text = ET.tostring(ET.fromstring(f"<r>{content}</r>"), method="text", encoding="unicode") \
            if content.strip().startswith("<") else content
        handle = (author.text if author is not None else "unknown").lstrip("/").removeprefix("u/")
        posts.append({
            "title": title,
            "date": updated[:10],
            "author": handle,
            "excerpt": " ".join(text.split())[:400],
            "sub": sub,
        })
    return posts


def _fetch_subreddit(ticker: str, sub: str, limit: int, timeout: float = 15.0) -> list[dict]:
    """One subreddit's search feed, with backoff.

    Reddit rate-limits anonymous clients hard and returns 429 for bursts, so
    each subreddit gets two retries with growing delays. A 429 that survives
    the retries is reported as throttling, never as "no posts" — those two
    states mean opposite things to a sentiment analyst.
    """
    import requests

    url = _REDDIT_RSS.format(sub=sub, qs=_search_qs(ticker, limit))
    delays = [0.0, 3.0, 8.0]
    last_status = None
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        response = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        last_status = response.status_code
        if response.status_code == 200 and response.content:
            return _parse_atom(response.content, sub)
        if response.status_code != 429:
            response.raise_for_status()
            return []
    raise RuntimeError(f"rate-limited by Reddit (HTTP {last_status}) after {len(delays)} attempts")


def reddit(ticker: str, limit_per_sub: int = 8, subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS) -> str:
    """Recent posts mentioning the ticker across the retail investing subreddits.

    Cached per ticker per day: reruns during one analysis session (and the
    sentiment bundle, which calls this) must not each spend a fresh burst of
    requests against an endpoint that throttles at this volume.
    """
    symbol = normalize_symbol(ticker)
    cache_key = f"reddit_{symbol}_{today()}.md"
    cached = cache_read(cache_key)
    if cached:
        return cached

    blocks = []
    errors = []
    for i, sub in enumerate(subreddits):
        if i:
            time.sleep(2.0)  # pace requests; Reddit throttles bursts from one IP
        try:
            posts = _fetch_subreddit(symbol, sub, limit_per_sub)
        except Exception as exc:
            errors.append(f"r/{sub}: {exc}")
            continue
        if not posts:
            blocks.append(f"### r/{sub}\n\n_No posts mentioning {symbol} in the past week._")
            continue
        lines = [f"### r/{sub} ({len(posts)} posts)", ""]
        for post in posts:
            lines.append(f"**[{post['date']}] {post['title']}** — u/{post['author']}")
            if post["excerpt"]:
                lines.append(f"  {post['excerpt']}")
            lines.append("")
        blocks.append("\n".join(lines))

    if not blocks:
        return unavailable(
            "Reddit",
            f"no subreddit returned data ({'; '.join(errors)}). This is a fetch failure, "
            "NOT an absence of discussion — do not read it as low retail interest",
        )

    note = (
        f"\n_Partial fetch failure: {'; '.join(errors)}. Those subreddits were not read — "
        "their silence here is not evidence of anything._"
        if errors else ""
    )
    subs = ", ".join(f"r/{s}" for s in subreddits)
    rendered = f"## Reddit — {symbol} (past week, {subs})\n\n" + "\n".join(blocks) + note
    if not errors:
        cache_write(cache_key, rendered)
    return rendered


def sentiment_bundle(ticker: str, curr_date: str, look_back_days: int = 7) -> str:
    """All three sentiment sources in one call.

    Upstream pre-fetches news + StockTwits + Reddit and injects them into the
    sentiment analyst's prompt from turn 0, specifically so the model never has
    an incentive to imagine a source it could not reach. Same idea here: one
    command, three labeled blocks, placeholders where a source failed.
    """
    from .news import ticker_news

    symbol = normalize_symbol(ticker)
    news_block = ticker_news(symbol, curr_date, look_back_days)
    stocktwits_block = stocktwits(symbol)
    reddit_block = reddit(symbol)

    return f"""# Sentiment sources — {symbol} (as of {curr_date})

Three complementary sources, fetched deterministically. Anything marked
`<unavailable: ...>` was NOT retrieved — report that gap explicitly and lower
your stated confidence. Never substitute recalled or imagined posts for a
missing block.

<start_of_news>
{news_block}
<end_of_news>

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

<start_of_reddit>
{reddit_block}
<end_of_reddit>
"""

