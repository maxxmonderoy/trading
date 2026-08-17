"""Decision log, outcome scoring, and lesson recall — the reflection layer.

Upstream stores per-agent reflections in ChromaDB and retrieves them by
embedding similarity. That needs an embedding endpoint, an API key, and a
vector store daemon — three dependencies for what is, at realistic volumes
(hundreds of decisions, not millions), a text-search problem.

This port keeps the same *loop* and drops the machinery:

    analyse → log the decision → wait out the horizon → score it against the
    benchmark → write a lesson → recall that lesson on the next similar setup

Storage is one JSONL file. Recall is TF-IDF cosine over the stored situation
text, with an exact-ticker boost. Everything is inspectable with `cat`, diffable
in git, and portable.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime

from .common import (
    MEMORY_DIR,
    benchmark_for,
    load_config,
    markdown_table,
    normalize_symbol,
    parse_date,
    today,
)

LOG_PATH = MEMORY_DIR / "decisions.jsonl"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "to", "of", "in", "on", "for", "with", "at", "by", "from", "as", "that", "this",
    "it", "its", "has", "have", "had", "will", "would", "could", "should", "may",
    "than", "then", "there", "their", "we", "our", "you", "your", "not", "no",
}


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 2]


def _load() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    records = []
    for line in LOG_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip a corrupted line rather than losing the whole log
    return records


def _save_all(records: list[dict]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def log_decision(
    ticker: str,
    date: str,
    rating: str,
    action: str,
    situation: str,
    report_dir: str = "",
    entry_price: float | None = None,
    stop_loss: float | None = None,
    conviction: str = "",
) -> str:
    """Append one decision. Returns the record id used by scoring and lessons."""
    symbol = normalize_symbol(ticker)
    record = {
        "id": uuid.uuid4().hex[:10],
        "ticker": symbol,
        "date": date,
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "rating": rating,
        "action": action,
        "conviction": conviction,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "situation": situation,
        "report_dir": report_dir,
        "scored": False,
        "outcome": None,
        "lesson": None,
    }
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    return record["id"]


def recall(ticker: str, query: str = "", limit: int = 5) -> str:
    """Lessons from past decisions, most relevant first.

    Ranking is IDF-weighted cosine over the situation text, doubled when the
    ticker matches — a prior call on the same name is almost always more
    instructive than a similar-looking setup elsewhere.
    """
    records = [r for r in _load() if r.get("lesson")]
    if not records:
        return (
            "_No scored decisions in memory yet. This is a cold start: the desk has no "
            "prior lessons to apply. Run `/reflect` once past decisions reach their "
            "scoring horizon._"
        )

    symbol = normalize_symbol(ticker) if ticker else ""
    corpus = [_tokens(f"{r.get('situation', '')} {r.get('lesson', '')}") for r in records]
    document_frequency = Counter()
    for doc in corpus:
        document_frequency.update(set(doc))
    total = len(corpus)

    def idf(term: str) -> float:
        return math.log((total + 1) / (document_frequency.get(term, 0) + 1)) + 1

    query_tokens = _tokens(f"{symbol} {query}")
    query_vector = {t: c * idf(t) for t, c in Counter(query_tokens).items()}
    query_norm = math.sqrt(sum(v * v for v in query_vector.values())) or 1.0

    scored = []
    for record, doc in zip(records, corpus):
        doc_vector = {t: c * idf(t) for t, c in Counter(doc).items()}
        doc_norm = math.sqrt(sum(v * v for v in doc_vector.values())) or 1.0
        dot = sum(weight * doc_vector.get(term, 0.0) for term, weight in query_vector.items())
        score = dot / (query_norm * doc_norm)
        if symbol and record["ticker"] == symbol:
            score = score * 2 + 0.1  # same-name priors always surface
        scored.append((score, record))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    selected = [r for s, r in scored[:limit] if s > 0]
    if not selected:
        return "_No past decisions matched this situation._"

    lines = ["## Lessons from past decisions", ""]
    for record in selected:
        outcome = record.get("outcome") or {}
        alpha = outcome.get("alpha_pct")
        alpha_text = f"{alpha:+.1f}% alpha vs {outcome.get('benchmark', 'benchmark')}" if alpha is not None else "unscored"
        verdict = outcome.get("verdict", "")
        lines.append(
            f"**{record['ticker']} — {record['date']} — called {record['rating']}** "
            f"({alpha_text}{', ' + verdict if verdict else ''})"
        )
        lines.append(f"> {record['lesson']}")
        lines.append("")
    lines.append(
        "_Apply these as priors, not rules. A lesson from one regime can mislead in another — "
        "say so if you judge a past lesson inapplicable here._"
    )
    return "\n".join(lines)


def _directional_verdict(rating: str, alpha_pct: float) -> str:
    """Was the call right, given what the benchmark did?

    Bullish calls are graded on positive alpha, bearish on negative; Hold is
    graded on the position being roughly flat, which is the only honest reading
    of "we told you to do nothing".
    """
    rating_lower = (rating or "").lower()
    if rating_lower in ("buy", "overweight"):
        return "correct" if alpha_pct > 1 else ("wrong" if alpha_pct < -1 else "neutral")
    if rating_lower in ("sell", "underweight"):
        return "correct" if alpha_pct < -1 else ("wrong" if alpha_pct > 1 else "neutral")
    return "correct" if abs(alpha_pct) <= 3 else "wrong"


def score(record_id: str = "", horizon_days: int | None = None) -> str:
    """Score matured decisions against their benchmark and store the outcome.

    A decision is scorable once `horizon_days` have elapsed since its analysis
    date. Alpha — not raw return — is the grade: being long a name that fell 2%
    while the index fell 6% was a good call.
    """
    from .market import close_on

    config = load_config()
    horizon = horizon_days or config["reflection_horizon_days"]
    records = _load()
    if not records:
        return "_Decision log is empty._"

    targets = [r for r in records if (not record_id or r["id"] == record_id) and not r["scored"]]
    if not targets:
        return "_Nothing to score: every logged decision already has an outcome._"

    now = datetime.now()
    scored_rows, pending_rows = [], []

    for record in targets:
        decision_date = parse_date(record["date"])
        elapsed = (now - decision_date).days
        if elapsed < horizon:
            pending_rows.append([
                record["id"], record["ticker"], record["date"], record["rating"],
                f"{horizon - elapsed} more days",
            ])
            continue

        end_date = (decision_date + __import__("datetime").timedelta(days=horizon)).strftime("%Y-%m-%d")
        benchmark = benchmark_for(record["ticker"], config["benchmark_ticker"])

        start_price = close_on(record["ticker"], record["date"])
        end_price = close_on(record["ticker"], end_date)
        bench_start = close_on(benchmark, record["date"])
        bench_end = close_on(benchmark, end_date)

        if None in (start_price, end_price, bench_start, bench_end) or not start_price or not bench_start:
            scored_rows.append([record["id"], record["ticker"], record["date"], record["rating"],
                                "price data unavailable", "—", "—"])
            continue

        raw_pct = (end_price - start_price) / start_price * 100
        bench_pct = (bench_end - bench_start) / bench_start * 100
        alpha_pct = raw_pct - bench_pct
        verdict = _directional_verdict(record["rating"], alpha_pct)

        record["scored"] = True
        record["outcome"] = {
            "horizon_days": horizon,
            "end_date": end_date,
            "start_price": round(start_price, 4),
            "end_price": round(end_price, 4),
            "raw_pct": round(raw_pct, 2),
            "benchmark": benchmark,
            "benchmark_pct": round(bench_pct, 2),
            "alpha_pct": round(alpha_pct, 2),
            "verdict": verdict,
            "scored_at": now.isoformat(timespec="seconds"),
        }
        scored_rows.append([
            record["id"], record["ticker"], record["date"], record["rating"],
            f"{raw_pct:+.1f}%", f"{bench_pct:+.1f}% ({benchmark})", f"{alpha_pct:+.1f}% → {verdict}",
        ])

    _save_all(records)

    output = []
    if scored_rows:
        output.append("## Newly scored decisions\n")
        output.append(markdown_table(
            ["ID", "Ticker", "Date", "Rating", "Raw return", "Benchmark", "Alpha → verdict"],
            scored_rows,
        ))
        output.append(
            "\nWrite a lesson for each with:\n"
            "`bin/ta memory lesson --id <ID> --text \"...\"`"
        )
    if pending_rows:
        output.append("\n## Not yet mature\n")
        output.append(markdown_table(["ID", "Ticker", "Date", "Rating", "Wait"], pending_rows))
    return "\n".join(output) or "_Nothing to score._"


def add_lesson(record_id: str, text: str) -> str:
    records = _load()
    for record in records:
        if record["id"] == record_id:
            record["lesson"] = text.strip()
            _save_all(records)
            return f"Lesson stored for {record['ticker']} ({record['date']}, id {record_id})."
    return f"<error: no decision with id {record_id}>"


def show(record_id: str) -> str:
    for record in _load():
        if record["id"] == record_id:
            return json.dumps(record, indent=2)
    return f"<error: no decision with id {record_id}>"


def history(ticker: str = "", limit: int = 30) -> str:
    """The decision log, newest first."""
    records = _load()
    if ticker:
        symbol = normalize_symbol(ticker)
        records = [r for r in records if r["ticker"] == symbol]
    if not records:
        return "_No decisions logged yet._"

    records.sort(key=lambda r: r["date"], reverse=True)
    rows = []
    for record in records[:limit]:
        outcome = record.get("outcome") or {}
        alpha = outcome.get("alpha_pct")
        rows.append([
            record["id"], record["ticker"], record["date"], record["rating"],
            f"{alpha:+.1f}%" if alpha is not None else "unscored",
            outcome.get("verdict", "—"),
            "yes" if record.get("lesson") else "no",
        ])
    return "## Decision log\n\n" + markdown_table(
        ["ID", "Ticker", "Date", "Rating", "Alpha", "Verdict", "Lesson"], rows
    )


def stats() -> str:
    """Track record by rating — the only honest read on whether the desk works."""
    records = [r for r in _load() if r.get("scored") and r.get("outcome", {}).get("alpha_pct") is not None]
    if not records:
        return "_No scored decisions yet — no track record to report._"

    by_rating: dict[str, list[dict]] = {}
    for record in records:
        by_rating.setdefault(record["rating"], []).append(record)

    rows = []
    for rating, group in sorted(by_rating.items()):
        alphas = [r["outcome"]["alpha_pct"] for r in group]
        correct = sum(1 for r in group if r["outcome"]["verdict"] == "correct")
        rows.append([
            rating, str(len(group)),
            f"{sum(alphas) / len(alphas):+.2f}%",
            f"{max(alphas):+.1f}% / {min(alphas):+.1f}%",
            f"{correct}/{len(group)} ({correct / len(group) * 100:.0f}%)",
        ])

    all_alphas = [r["outcome"]["alpha_pct"] for r in records]
    all_correct = sum(1 for r in records if r["outcome"]["verdict"] == "correct")
    rows.append([
        "**ALL**", str(len(records)),
        f"{sum(all_alphas) / len(all_alphas):+.2f}%",
        f"{max(all_alphas):+.1f}% / {min(all_alphas):+.1f}%",
        f"{all_correct}/{len(records)} ({all_correct / len(records) * 100:.0f}%)",
    ])

    return (
        "## Track record (scored decisions only)\n\n"
        + markdown_table(["Rating", "N", "Mean alpha", "Best / worst", "Directional hit rate"], rows)
        + "\n\n_Small samples say little. Below ~30 scored decisions per rating, treat "
        "these numbers as descriptive, not predictive._"
    )


def pending(horizon_days: int | None = None) -> str:
    """Decisions that have matured and are waiting to be scored."""
    horizon = horizon_days or load_config()["reflection_horizon_days"]
    now = datetime.now()
    rows = [
        [r["id"], r["ticker"], r["date"], r["rating"], str((now - parse_date(r["date"])).days)]
        for r in _load()
        if not r.get("scored") and (now - parse_date(r["date"])).days >= horizon
    ]
    if not rows:
        return f"_No decisions have reached the {horizon}-day scoring horizon yet._"
    return (
        f"## Ready to score ({horizon}-day horizon)\n\n"
        + markdown_table(["ID", "Ticker", "Date", "Rating", "Days elapsed"], rows)
    )


def default_situation(ticker: str, date: str | None = None) -> str:
    return f"{normalize_symbol(ticker)} analysis on {date or today()}"
