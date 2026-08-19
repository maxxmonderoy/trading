"""Economic calendar — scraped from primary sources with Scrapling.

The framework's hard rule is "flatten or cancel 15 minutes before high-impact
USD news (CPI, NFP, FOMC)". That rule was unenforceable because nothing supplied
the dates.

**Primary sources, not an aggregator.** BLS publishes its own release schedule
and the Federal Reserve publishes its own meeting calendar. Those are the bodies
that decide the dates, so they cannot be stale or wrong relative to some other
site's copy — and they are plain government HTML with no anti-bot layer and no
terms problem. Scraping ForexFactory or Investing.com would mean fighting
Cloudflare for a second-hand copy of this.

Release times are fixed by long-standing convention and are not on the page in a
machine-readable form for FOMC:

- BLS releases (CPI, Employment Situation): 08:30 ET, stated on the schedule page
- FOMC statement: 14:00 ET; press conference 14:30 ET

All times are Eastern, matching every other timestamp in this project.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

from .common import ROOT, cache_read, cache_write, markdown_table

CALENDAR_PATH = ROOT / "news_calendar.json"

BLS_SOURCES = {
    "CPI": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "NFP": "https://www.bls.gov/schedule/news_release/empsit.htm",
    "PPI": "https://www.bls.gov/schedule/news_release/ppi.htm",
}
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _fetch(url: str, ttl_minutes: int = 720) -> str:
    """Fetch with a long cache. These schedules change a few times a year."""
    key = "cal_" + re.sub(r"[^a-z0-9]+", "_", url.lower())[-80:] + ".html"
    cached = cache_read(key, ttl_minutes=ttl_minutes)
    if cached:
        return cached

    from scrapling.fetchers import Fetcher

    page = Fetcher.get(url, timeout=30)
    if page.status != 200:
        raise RuntimeError(f"HTTP {page.status} from {url}")
    cache_write(key, page.html_content)
    return page.html_content


def _parse_bls(html: str, event: str) -> list[dict]:
    """Rows of `Reference Month | Release Date | Release Time` into events."""
    from scrapling import Selector

    events = []
    for table in Selector(html).css("table"):
        rows = table.css("tr")
        # The page renders the same table twice; the second copy has no stray
        # concatenated first row, but parsing both and de-duplicating is simpler
        # than depending on which one comes first.
        for row in rows:
            cells = [c.get_all_text().strip() for c in row.css("th, td")]
            if len(cells) != 3 or cells[1].lower().startswith("release"):
                continue
            parsed = _parse_bls_date(cells[1], cells[2])
            if parsed:
                events.append({"datetime": parsed, "event": event, "reference": cells[0]})
    return events


def _parse_bls_date(date_text: str, time_text: str) -> str | None:
    """'Aug. 12, 2026' + '08:30 AM' -> '2026-08-12 08:30'."""
    match = re.match(r"([A-Za-z]{3})\.?\s+(\d{1,2}),\s*(\d{4})", date_text.strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower()[:3])
    if not month:
        return None
    hour_match = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", time_text.strip(), re.I)
    hour, minute = (8, 30)
    if hour_match:
        hour, minute = int(hour_match.group(1)), int(hour_match.group(2))
        if hour_match.group(3).upper() == "PM" and hour != 12:
            hour += 12
        if hour_match.group(3).upper() == "AM" and hour == 12:
            hour = 0
    return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d} {hour:02d}:{minute:02d}"


def _parse_fomc(html: str) -> list[dict]:
    """FOMC statement dates, taken from the statement document links.

    The visible calendar shows a day *range* ("27-28") with the month and year in
    separate elements, which is fiddly and easy to misassemble. Every concluded
    meeting also links its statement as `monetary20260128a.htm`, which carries
    the exact release date unambiguously — so the links are the better source for
    past and current meetings.
    """
    dates = sorted(set(re.findall(r"/monetary(\d{8})a\d?\.(?:htm|pdf)", html)))
    events = []
    for stamp in dates:
        try:
            day = datetime.strptime(stamp, "%Y%m%d")
        except ValueError:
            continue
        events.append({
            "datetime": f"{day:%Y-%m-%d} 14:00",
            "event": "FOMC statement",
            "reference": f"{day:%B %Y} meeting",
        })
    return events


def _parse_fomc_scheduled(html: str) -> list[dict]:
    """Future meetings, from the visible calendar rather than statement links.

    A meeting only has a statement document once it has happened, so the
    link-based parse covers history and nothing ahead — exactly backwards for a
    blackout rule that has to know about *next* month's FOMC. The rendered
    calendar carries scheduled meetings, so both parsers are needed: links for
    exact past dates, the calendar for everything upcoming.

    Meetings run two days and the statement lands on the second, so the range
    "27-28" resolves to the 28th. An asterisk marks a Summary of Economic
    Projections release, which historically moves more than a bare statement.
    """
    from scrapling import Selector

    events = []
    for panel in Selector(html).css("div.panel"):
        heading = panel.css("h4")
        if not heading:
            continue
        year_match = re.search(r"(20\d{2})", heading[0].get_all_text())
        if not year_match:
            continue
        year = int(year_match.group(1))

        for meeting in panel.css("div.fomc-meeting"):
            month_el = meeting.css("div.fomc-meeting__month")
            day_el = meeting.css("div.fomc-meeting__date")
            if not month_el or not day_el:
                continue
            month_text = month_el[0].get_all_text().strip()
            day_text = day_el[0].get_all_text().strip()

            projections = "*" in day_text
            days = re.findall(r"\d{1,2}", day_text)
            if not days:
                continue
            last_day = int(days[-1])

            # "January/February" style headings mean the meeting straddles a
            # month boundary; the statement belongs to the later month.
            month_name = month_text.split("/")[-1].strip().lower()[:3]
            month = _MONTHS.get(month_name)
            if not month:
                continue
            try:
                day = datetime(year, month, last_day)
            except ValueError:
                continue
            events.append({
                "datetime": f"{day:%Y-%m-%d} 14:00",
                "event": "FOMC statement" + (" + projections" if projections else ""),
                "reference": f"{day:%B %Y} meeting",
            })
    return events


def fetch_calendar(include: tuple[str, ...] = ("CPI", "NFP", "FOMC")) -> tuple[list[dict], list[str]]:
    """All requested events, sorted. Returns (events, errors)."""
    events: list[dict] = []
    errors: list[str] = []

    for i, (name, url) in enumerate(BLS_SOURCES.items()):
        if name not in include:
            continue
        if i:
            time.sleep(1.0)  # be a polite client on a government server
        try:
            events.extend(_parse_bls(_fetch(url), name))
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    if "FOMC" in include:
        try:
            html = _fetch(FOMC_URL)
            # Links give exact dates for concluded meetings; the rendered
            # calendar is the only source for scheduled ones.
            events.extend(_parse_fomc(html))
            events.extend(_parse_fomc_scheduled(html))
        except Exception as exc:
            errors.append(f"FOMC: {type(exc).__name__}: {exc}")

    # Same meeting can arrive from both FOMC parsers; keep the richer label.
    best: dict[tuple, dict] = {}
    for event in events:
        key = (event["datetime"], event["event"].split(" + ")[0])
        if key not in best or len(event["event"]) > len(best[key]["event"]):
            best[key] = event
    unique = sorted(best.values(), key=lambda e: e["datetime"])
    return unique, errors


def update(include: tuple[str, ...] = ("CPI", "NFP", "FOMC"), future_only: bool = False) -> str:
    """Scrape and write news_calendar.json."""
    events, errors = fetch_calendar(include)
    if not events:
        return (
            "<error: no events scraped"
            + (f" — {'; '.join(errors)}" if errors else "")
            + ". The no-news rule stays UNENFORCED.>"
        )

    if future_only:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        events = [e for e in events if e["datetime"] >= now]

    CALENDAR_PATH.write_text(json.dumps(events, indent=2) + "\n")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    upcoming = [e for e in events if e["datetime"] >= now][:12]
    rows = [[e["datetime"], e["event"], e.get("reference", "")] for e in upcoming]

    by_type: dict[str, int] = {}
    for e in events:
        by_type[e["event"]] = by_type.get(e["event"], 0) + 1

    note = f"\n> Partial failures: {'; '.join(errors)}\n" if errors else ""
    return f"""## Economic calendar updated

Wrote **{len(events)} events** to `news_calendar.json`
({', '.join(f'{k} {v}' for k, v in sorted(by_type.items()))}).

Sources: BLS release schedules and the Federal Reserve FOMC calendar — the
bodies that set the dates, not a third-party copy of them.

### Next {len(rows)} events

{markdown_table(["When (ET)", "Event", "Reference"], rows) if rows else "_none upcoming in the scraped range_"}
{note}
> Times are Eastern. BLS releases at 08:30; the FOMC statement lands at 14:00
> with the press conference at 14:30 — the second is often the bigger mover, so
> a 15-minute blackout around 14:00 alone does not cover the whole event.
>
> Re-run periodically. BLS publishes roughly a year ahead; the Fed publishes two.
"""


def show(limit: int = 20) -> str:
    """What the blackout rule will actually act on."""
    if not CALENDAR_PATH.exists():
        return "_No calendar. Run `bin/ta calendar update` to scrape one._"
    try:
        events = json.loads(CALENDAR_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return f"<error: calendar unreadable ({type(exc).__name__})>"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    upcoming = [e for e in events if e["datetime"] >= now][:limit]
    past = len(events) - len([e for e in events if e["datetime"] >= now])
    rows = [[e["datetime"], e["event"], e.get("reference", "")] for e in upcoming]
    return (
        f"## Economic calendar — {len(events)} events ({past} past, "
        f"{len(events) - past} upcoming)\n\n"
        + (markdown_table(["When (ET)", "Event", "Reference"], rows)
           if rows else "_Nothing upcoming — the calendar needs refreshing._")
    )
