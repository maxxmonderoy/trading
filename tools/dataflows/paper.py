"""Paper trading book — simulated execution, positions, and mark-to-market P&L.

Not in upstream TradingAgents, which stops at the decision. But a decision that
is never executed is never really graded: sizing, entry timing, stops, and the
cost of getting in and out are where a plausible thesis turns into a real result
or a real loss, and none of them show up in a report.

Two design commitments:

**Costs are explicit and always shown.** Every fill carries slippage, and the
status report surfaces cumulative cost drag as its own line. A strategy whose
edge is smaller than its round-trip cost is not a strategy, and the only way to
notice that is to keep the number in front of you.

**No look-ahead.** Fills price off `close_on(ticker, date)`, which truncates at
the trade date like every other dataflow. A dated backfill cannot fill at a price
that had not printed yet.
"""

from __future__ import annotations

import json
from datetime import datetime

from .common import (
    ROOT,
    asset_type,
    fmt_num,
    load_config,
    markdown_table,
    normalize_symbol,
    today,
)

PAPER_DIR = ROOT / "paper"
PORTFOLIO_PATH = PAPER_DIR / "portfolio.json"
TRADES_PATH = PAPER_DIR / "trades.jsonl"
EQUITY_PATH = PAPER_DIR / "equity.jsonl"

_DEFAULT_PAPER = {
    "starting_cash": 100000.0,
    "slippage_bps": 5.0,
    "commission_per_trade": 0.0,
    "max_position_pct": 20.0,
}


def _settings() -> dict:
    settings = dict(_DEFAULT_PAPER)
    settings.update(load_config().get("paper", {}))
    return settings


def _load() -> dict | None:
    if not PORTFOLIO_PATH.exists():
        return None
    try:
        return json.loads(PORTFOLIO_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save(book: dict) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_PATH.write_text(json.dumps(book, indent=2))


def _append(path, record: dict) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def _trades() -> list[dict]:
    if not TRADES_PATH.exists():
        return []
    out = []
    for line in TRADES_PATH.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _round_shares(symbol: str, shares: float) -> float:
    """Whole shares for equities, fractional for crypto."""
    if asset_type(symbol) == "crypto":
        return round(shares, 6)
    return float(int(shares))


def _price(symbol: str, date: str) -> float | None:
    from .market import close_on

    return close_on(symbol, date)


def _fill_price(price: float, side: str, slippage_bps: float) -> float:
    """Slippage always works against you — that is the whole point of modeling it."""
    adjustment = 1 + (slippage_bps / 10_000) * (1 if side == "buy" else -1)
    return round(price * adjustment, 4)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def init(cash: float | None = None, force: bool = False) -> str:
    existing = _load()
    if existing and not force:
        return (
            f"<error: a paper book already exists with {fmt_num(existing['cash'])} cash and "
            f"{len(existing['positions'])} open position(s). Pass --force to wipe and restart — "
            "this deletes all trade history.>"
        )

    settings = _settings()
    starting = float(cash if cash is not None else settings["starting_cash"])
    book = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "inception_date": today(),
        "starting_cash": starting,
        "cash": starting,
        "positions": {},
        "realized_pnl": 0.0,
        "total_costs": 0.0,
        "settings": settings,
    }
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    if force:
        TRADES_PATH.unlink(missing_ok=True)
        EQUITY_PATH.unlink(missing_ok=True)
    _save(book)

    return (
        f"Paper book opened.\n\n"
        f"  starting cash:   {fmt_num(starting)}\n"
        f"  inception:       {book['inception_date']}\n"
        f"  slippage:        {settings['slippage_bps']} bps per side\n"
        f"  commission:      {fmt_num(settings['commission_per_trade'])} per trade\n"
        f"  max position:    {settings['max_position_pct']}% of equity\n\n"
        f"Book: {PORTFOLIO_PATH}"
    )


def _equity(book: dict, date: str) -> tuple[float, dict[str, float], list[str]]:
    """Total equity, per-symbol marks, and any symbols that could not be priced."""
    marks: dict[str, float] = {}
    unpriced: list[str] = []
    holdings = 0.0
    for symbol, position in book["positions"].items():
        price = _price(symbol, date)
        if price is None:
            unpriced.append(symbol)
            # Fall back to cost basis so equity stays defined; flagged in the report.
            price = position["avg_price"]
        marks[symbol] = price
        holdings += price * position["shares"]
    return book["cash"] + holdings, marks, unpriced


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def buy(
    ticker: str,
    date: str,
    size_pct: float | None = None,
    shares: float | None = None,
    price: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    memory_id: str = "",
    note: str = "",
) -> str:
    book = _load()
    if book is None:
        return "<error: no paper book. Run `bin/ta paper init` first.>"

    symbol = normalize_symbol(ticker)
    settings = book["settings"]

    market_price = price if price is not None else _price(symbol, date)
    if market_price is None:
        return f"<error: no price available for {symbol} on or before {date}>"

    fill = _fill_price(market_price, "buy", settings["slippage_bps"])
    equity, _, _ = _equity(book, date)

    max_pct = float(settings["max_position_pct"])
    if shares is None:
        if size_pct is None:
            return "<error: pass either --size-pct or --shares>"
        if size_pct > max_pct:
            return (
                f"<error: {size_pct}% exceeds the {max_pct}% max position size. "
                "Raise `paper.max_position_pct` in config.json if this is intentional.>"
            )
        shares = _round_shares(symbol, equity * (size_pct / 100) / fill)

    if shares <= 0:
        return f"<error: computed size is 0 shares — equity {fmt_num(equity)} is too small for a {size_pct}% position at {fmt_num(fill)}>"

    # The cap is on the resulting position, not on the order. Checking only the
    # order lets two 20% buys build a 40% position, and lets `--shares` past the
    # limit entirely — which is exactly the concentration the cap exists to stop.
    existing = book["positions"].get(symbol)
    existing_shares = existing["shares"] if existing else 0.0
    resulting_pct = (existing_shares + shares) * fill / equity * 100 if equity > 0 else 0.0
    if resulting_pct > max_pct + 1e-9:
        return (
            f"<error: this fill would take {symbol} to {resulting_pct:.1f}% of equity, past the "
            f"{max_pct}% max position size"
            + (f" ({existing_shares:g} shares already held)" if existing_shares else "")
            + ". Reduce the size, or raise `paper.max_position_pct` in config.json if this is "
            "intentional.>"
        )

    commission = float(settings["commission_per_trade"])
    cost = shares * fill + commission
    if cost > book["cash"]:
        return (
            f"<error: insufficient cash. Need {fmt_num(cost)} for {fmt_num(shares, 4)} shares "
            f"at {fmt_num(fill)}, have {fmt_num(book['cash'])}.>"
        )

    slippage_cost = shares * abs(fill - market_price)
    position = book["positions"].get(symbol)
    if position:
        # Weighted average cost basis across the adds.
        total_shares = position["shares"] + shares
        position["avg_price"] = round(
            (position["avg_price"] * position["shares"] + fill * shares) / total_shares, 4
        )
        position["shares"] = round(total_shares, 6)
        if stop is not None:
            position["stop"] = stop
        if target is not None:
            position["target"] = target
    else:
        book["positions"][symbol] = {
            "shares": round(shares, 6),
            "avg_price": fill,
            "opened_at": date,
            "stop": stop,
            "target": target,
            "memory_id": memory_id,
            "note": note,
        }

    book["cash"] = round(book["cash"] - cost, 4)
    book["total_costs"] = round(book["total_costs"] + slippage_cost + commission, 4)
    _save(book)
    _append(TRADES_PATH, {
        "date": date, "side": "buy", "ticker": symbol, "shares": shares,
        "price": fill, "reference_price": market_price,
        "slippage": round(slippage_cost, 4), "commission": commission,
        "memory_id": memory_id, "note": note,
        "logged_at": datetime.now().isoformat(timespec="seconds"),
    })

    stop_text = f" | stop {fmt_num(stop)}" if stop else ""
    target_text = f" | target {fmt_num(target)}" if target else ""
    return (
        f"BOUGHT {fmt_num(shares, 4)} {symbol} @ {fmt_num(fill)} "
        f"(ref {fmt_num(market_price)}, slippage {fmt_num(slippage_cost)}){stop_text}{target_text}\n"
        f"Cost {fmt_num(cost)} | cash now {fmt_num(book['cash'])}"
    )


def sell(
    ticker: str,
    date: str,
    shares: float | None = None,
    pct: float | None = None,
    price: float | None = None,
    reason: str = "",
) -> str:
    book = _load()
    if book is None:
        return "<error: no paper book. Run `bin/ta paper init` first.>"

    symbol = normalize_symbol(ticker)
    position = book["positions"].get(symbol)
    if not position:
        return f"<error: no open position in {symbol}>"

    settings = book["settings"]
    market_price = price if price is not None else _price(symbol, date)
    if market_price is None:
        return f"<error: no price available for {symbol} on or before {date}>"

    fill = _fill_price(market_price, "sell", settings["slippage_bps"])

    if shares is None:
        shares = position["shares"] * ((pct if pct is not None else 100.0) / 100.0)
    shares = min(round(shares, 6), position["shares"])
    if shares <= 0:
        return "<error: nothing to sell>"

    commission = float(settings["commission_per_trade"])
    proceeds = shares * fill - commission
    realized = round((fill - position["avg_price"]) * shares - commission, 4)
    slippage_cost = shares * abs(market_price - fill)

    position["shares"] = round(position["shares"] - shares, 6)
    closed = position["shares"] <= 1e-9
    if closed:
        del book["positions"][symbol]

    book["cash"] = round(book["cash"] + proceeds, 4)
    book["realized_pnl"] = round(book["realized_pnl"] + realized, 4)
    book["total_costs"] = round(book["total_costs"] + slippage_cost + commission, 4)
    _save(book)
    _append(TRADES_PATH, {
        "date": date, "side": "sell", "ticker": symbol, "shares": shares,
        "price": fill, "reference_price": market_price,
        "slippage": round(slippage_cost, 4), "commission": commission,
        "realized_pnl": realized, "reason": reason, "closed": closed,
        "logged_at": datetime.now().isoformat(timespec="seconds"),
    })

    # `position` is still live after deletion from the book, so the cost basis
    # is available for the return calculation either way.
    pct_return = (fill / position["avg_price"] - 1) * 100
    remaining = (
        "position closed"
        if closed
        else f"{fmt_num(position['shares'], 4)} shares remaining"
    )
    return (
        f"SOLD {fmt_num(shares, 4)} {symbol} @ {fmt_num(fill)} "
        f"(ref {fmt_num(market_price)}, slippage {fmt_num(slippage_cost)})\n"
        f"Realized P&L {fmt_num(realized)} ({pct_return:+.1f}%) | {remaining} | "
        f"cash now {fmt_num(book['cash'])}"
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def status(date: str | None = None) -> str:
    book = _load()
    if book is None:
        return "<error: no paper book. Run `bin/ta paper init` first.>"

    date = date or today()
    equity, marks, unpriced = _equity(book, date)
    starting = book["starting_cash"]
    total_return = (equity / starting - 1) * 100

    rows = []
    unrealized_total = 0.0
    for symbol, position in sorted(book["positions"].items()):
        mark = marks[symbol]
        value = mark * position["shares"]
        unrealized = (mark - position["avg_price"]) * position["shares"]
        unrealized_total += unrealized
        stop_distance = (
            f"{(mark / position['stop'] - 1) * 100:+.1f}%" if position.get("stop") else "—"
        )
        rows.append([
            symbol,
            fmt_num(position["shares"], 4),
            fmt_num(position["avg_price"]),
            fmt_num(mark),
            fmt_num(value),
            f"{value / equity * 100:.1f}%",
            f"{fmt_num(unrealized)} ({(mark / position['avg_price'] - 1) * 100:+.1f}%)",
            fmt_num(position.get("stop")) if position.get("stop") else "—",
            stop_distance,
        ])

    lines = [f"# Paper book — {date}", ""]
    lines.append(markdown_table(
        ["Ticker", "Shares", "Cost", "Mark", "Value", "Weight", "Unrealized", "Stop", "To stop"],
        rows,
    ) if rows else "_No open positions._")

    # The book's own settings block holds execution parameters only; the
    # benchmark is a project-level setting.
    benchmark = load_config().get("benchmark_ticker", "SPY")
    bench_line = _benchmark_comparison(book, date, total_return, benchmark)

    lines.extend([
        "",
        "## Account",
        "",
        markdown_table(["Item", "Value"], [
            ["Starting cash", fmt_num(starting)],
            ["Cash", fmt_num(book["cash"])],
            ["Positions value", fmt_num(equity - book["cash"])],
            ["**Total equity**", f"**{fmt_num(equity)}**"],
            ["Unrealized P&L", fmt_num(unrealized_total)],
            ["Realized P&L", fmt_num(book["realized_pnl"])],
            ["**Total return**", f"**{total_return:+.2f}%**"],
            ["Cumulative costs", f"{fmt_num(book['total_costs'])} ({book['total_costs'] / starting * 100:.2f}% of starting capital)"],
            ["Open positions", str(len(book["positions"]))],
            ["Trades executed", str(len(_trades()))],
        ]),
    ])

    if bench_line:
        lines.extend(["", "## vs benchmark", "", bench_line])

    if unpriced:
        lines.append(
            f"\n> **Pricing gap**: {', '.join(unpriced)} could not be marked on {date} and are "
            "held at cost basis. Equity above is therefore stale for those names."
        )

    # Cost drag is the number most likely to quietly invalidate a strategy.
    if book["total_costs"] > 0 and abs(equity - starting) > 0:
        drag = book["total_costs"] / abs(equity - starting) * 100
        lines.append(
            f"\n> **Cost drag**: transaction costs are {drag:.0f}% the size of total P&L "
            f"({fmt_num(book['total_costs'])} of costs against {fmt_num(equity - starting)} of P&L). "
            + ("Trading costs are consuming the edge." if drag > 50 else "")
        )

    return "\n".join(lines)


def _benchmark_comparison(book: dict, date: str, total_return: float, benchmark: str) -> str:
    """Buy-and-hold the benchmark from inception — the bar any active book must clear."""
    start_price = _price(benchmark, book["inception_date"])
    end_price = _price(benchmark, date)
    if not start_price or not end_price:
        return ""
    bench_return = (end_price / start_price - 1) * 100
    alpha = total_return - bench_return
    # A flat book on inception day is level with the benchmark, not losing to it.
    if abs(alpha) < 0.005:
        verdict = "in line with"
    else:
        verdict = "ahead of" if alpha > 0 else "behind"
    return markdown_table(["Item", "Value"], [
        [f"Book return (since {book['inception_date']})", f"{total_return:+.2f}%"],
        [f"{benchmark} buy-and-hold", f"{bench_return:+.2f}%"],
        ["**Alpha**", f"**{alpha:+.2f}%** — {verdict} simply holding {benchmark}"],
    ])


def mark(date: str | None = None) -> str:
    """Mark to market, record an equity point, and check every stop and target.

    Uses the day's low/high rather than the close: a stop is hit intraday, and
    marking only on closes systematically understates how often stops trigger.
    """
    from .market import load_ohlcv

    book = _load()
    if book is None:
        return "<error: no paper book. Run `bin/ta paper init` first.>"

    date = date or today()
    equity, marks, unpriced = _equity(book, date)
    _append(EQUITY_PATH, {
        "date": date,
        "equity": round(equity, 2),
        "cash": book["cash"],
        "positions": len(book["positions"]),
    })

    alerts = []
    for symbol, position in book["positions"].items():
        stop, target = position.get("stop"), position.get("target")
        if not stop and not target:
            continue
        try:
            data = load_ohlcv(symbol, date, 10)
        except Exception:
            continue
        if data.empty:
            continue
        last = data.iloc[-1]
        low, high, close = float(last["Low"]), float(last["High"]), float(last["Close"])

        if stop and low <= stop:
            alerts.append(
                f"- **STOP HIT — {symbol}**: the {last['Date'].date()} session traded to "
                f"{fmt_num(low)}, through the {fmt_num(stop)} stop (closed {fmt_num(close)}). "
                f"Exit with `bin/ta paper sell {symbol} --reason \"stop hit\"`."
            )
        if target and high >= target:
            alerts.append(
                f"- **TARGET HIT — {symbol}**: the {last['Date'].date()} session traded to "
                f"{fmt_num(high)}, through the {fmt_num(target)} target (closed {fmt_num(close)}). "
                f"Take profit or raise the stop."
            )

    output = [f"Marked {date}: equity {fmt_num(equity)}, {len(book['positions'])} position(s)."]
    if alerts:
        output.append("\n## Alerts\n")
        output.extend(alerts)
    else:
        output.append("No stops or targets triggered.")
    if unpriced:
        output.append(f"\n> Could not price: {', '.join(unpriced)} — held at cost basis.")
    return "\n".join(output)


def history(limit: int = 40) -> str:
    trades = _trades()
    if not trades:
        return "_No trades executed yet._"

    rows = [
        [
            t["date"], t["side"].upper(), t["ticker"],
            fmt_num(t["shares"], 4), fmt_num(t["price"]),
            fmt_num(t.get("slippage", 0)),
            fmt_num(t["realized_pnl"]) if t.get("realized_pnl") is not None else "—",
            (t.get("reason") or t.get("note") or "")[:40],
        ]
        for t in trades[-limit:]
    ]
    total_slippage = sum(t.get("slippage", 0) for t in trades)
    realized = [t["realized_pnl"] for t in trades if t.get("realized_pnl") is not None]

    lines = [
        "## Trade history",
        "",
        markdown_table(
            ["Date", "Side", "Ticker", "Shares", "Fill", "Slippage", "Realized", "Note"], rows
        ),
        "",
        f"**{len(trades)} trades** | cumulative slippage {fmt_num(total_slippage)}",
    ]
    if realized:
        wins = sum(1 for r in realized if r > 0)
        lines.append(
            f" | {len(realized)} closed: {wins} winners, {len(realized) - wins} losers "
            f"({wins / len(realized) * 100:.0f}% hit rate), net {fmt_num(sum(realized))}"
        )
    return "\n".join(lines)


def equity_curve(limit: int = 30) -> str:
    if not EQUITY_PATH.exists():
        return "_No equity marks yet. Run `bin/ta paper mark` to record one._"
    points = [json.loads(line) for line in EQUITY_PATH.read_text().splitlines() if line.strip()]
    if not points:
        return "_No equity marks yet._"

    book = _load() or {}
    starting = book.get("starting_cash", points[0]["equity"])
    rows = [
        [p["date"], fmt_num(p["equity"]), f"{(p['equity'] / starting - 1) * 100:+.2f}%",
         fmt_num(p["cash"]), str(p["positions"])]
        for p in points[-limit:]
    ]
    peak = max(p["equity"] for p in points)
    current = points[-1]["equity"]
    drawdown = (current / peak - 1) * 100
    return (
        "## Equity curve\n\n"
        + markdown_table(["Date", "Equity", "Return", "Cash", "Positions"], rows)
        + f"\n\n**Peak equity** {fmt_num(peak)} | **current drawdown** {drawdown:+.2f}%"
    )
