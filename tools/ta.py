#!/usr/bin/env python3
"""`ta` — the whole data layer of the trading desk, as one CLI.

Upstream TradingAgents binds ~15 LangChain tools to its LLM nodes. Claude Code
agents already have Bash, so the equivalent surface here is a set of subcommands
that print rendered markdown to stdout. One command == one upstream tool.

    bin/ta snapshot NVDA                  # verified ground truth (market analyst)
    bin/ta sentiment-bundle NVDA          # news + StockTwits + Reddit
    bin/ta fundamentals NVDA
    bin/ta macro cpi
    bin/ta memory recall --ticker NVDA

Every command takes `--date YYYY-MM-DD` (default: today) and truncates its data
at that date, so a dated re-run cannot see the future.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataflows import (  # noqa: E402
    common, doctor, fundamentals, futures, macro, market, memory, news, paper, smc, social,
)


def _add_date(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--date", default=None,
        help="Analysis date YYYY-MM-DD (default: today). Data is truncated here — no look-ahead.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ta", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- market -----------------------------------------------------------
    p = sub.add_parser("ohlcv", help="Price history table")
    p.add_argument("ticker")
    p.add_argument("--days", type=int, default=60, help="Sessions to display (default 60)")
    _add_date(p)

    p = sub.add_parser("indicators", help="Technical indicator values")
    p.add_argument("ticker")
    p.add_argument(
        "--names", required=True,
        help="Comma-separated, max 8. Choose complementary ones — do not stack redundant momentum indicators.",
    )
    p.add_argument("--days", type=int, default=30)
    _add_date(p)

    p = sub.add_parser("snapshot", help="VERIFIED ground-truth snapshot — source of truth for exact numbers")
    p.add_argument("ticker")
    _add_date(p)

    p = sub.add_parser("indicator-list", help="Supported indicators and what each is for")

    sub.add_parser("doctor", help="Check deps, subagent definitions, commands, and config")

    # ---- fundamentals -----------------------------------------------------
    p = sub.add_parser("fundamentals", help="Company profile, valuation, health, street view")
    p.add_argument("ticker")
    _add_date(p)

    for name, help_text in [
        ("balance-sheet", "Balance sheet"),
        ("cashflow", "Cash flow statement"),
        ("income-statement", "Income statement"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("ticker")
        p.add_argument("--annual", action="store_true", help="Annual instead of quarterly")
        _add_date(p)

    p = sub.add_parser("earnings", help="Earnings calendar and recent surprises")
    p.add_argument("ticker")
    _add_date(p)

    p = sub.add_parser("estimates", help="Consensus EPS/revenue by period, revisions, and reconciled forward P/E")
    p.add_argument("ticker")
    p.add_argument("--price", type=float, default=None, help="Price to compute multiples against (default: last close)")
    _add_date(p)

    p = sub.add_parser("earnings-reactions", help="How the stock actually moved on past prints — base rate for gap risk")
    p.add_argument("ticker")
    p.add_argument("--limit", type=int, default=12)
    _add_date(p)

    # ---- news -------------------------------------------------------------
    p = sub.add_parser("news", help="Ticker-specific news")
    p.add_argument("ticker")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=25)
    _add_date(p)

    p = sub.add_parser("global-news", help="Macro / world news relevant to trading")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=20)
    _add_date(p)

    p = sub.add_parser("news-search", help="Free-form news search for a specific catalyst")
    p.add_argument("query")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=20)
    _add_date(p)

    # ---- sentiment --------------------------------------------------------
    p = sub.add_parser("stocktwits", help="StockTwits cashtag messages with bull/bear labels")
    p.add_argument("ticker")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("reddit", help="Reddit posts from the retail investing subs")
    p.add_argument("ticker")
    p.add_argument("--limit", type=int, default=8, help="Posts per subreddit")

    p = sub.add_parser("sentiment-bundle", help="News + StockTwits + Reddit in one block")
    p.add_argument("ticker")
    p.add_argument("--days", type=int, default=7)
    _add_date(p)

    # ---- macro ------------------------------------------------------------
    p = sub.add_parser("macro", help="FRED macro series (needs FRED_API_KEY)")
    p.add_argument("indicator", help="Alias (cpi, core_pce, unemployment, fed_funds_rate, 10y_treasury, yield_curve, vix, ...) or a raw FRED series id")
    p.add_argument("--days", type=int, default=365)
    _add_date(p)

    p = sub.add_parser("macro-list", help="Known macro indicator aliases")

    p = sub.add_parser("predictions", help="Polymarket implied probabilities for forward-looking events")
    p.add_argument("topic")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("regime", help="Deterministic macro backdrop: indices, VIX, rates, dollar, commodities")
    _add_date(p)

    # ---- run scaffolding --------------------------------------------------
    p = sub.add_parser("run", help="Create and inspect analysis run directories")
    run_sub = p.add_subparsers(dest="run_command", required=True)

    q = run_sub.add_parser("init", help="Create the results directory and state file for a run")
    q.add_argument("ticker")
    _add_date(q)

    q = run_sub.add_parser("status", help="Which stage artifacts exist for a run")
    q.add_argument("ticker")
    _add_date(q)

    # ---- futures ----------------------------------------------------------
    p = sub.add_parser("fut", help="Futures: contract specs, session levels, liquidity base rates, sizing")
    fut_sub = p.add_subparsers(dest="fut_command", required=True)

    q = fut_sub.add_parser("specs", help="Contract specifications and live cost arithmetic")
    q.add_argument("symbol", nargs="?", default=None, help="ES, MES, NQ, MNQ (omit for all)")
    q.add_argument("--price", type=float, default=None)
    _add_date(q)

    q = fut_sub.add_parser("levels", help="Session reference levels and untested liquidity")
    q.add_argument("symbol")
    q.add_argument("--interval", default="5m", help="1m, 2m, 5m, 15m, 30m, 1h (default 5m)")
    _add_date(q)

    q = fut_sub.add_parser("level-stats", help="Measured base rates: touched / swept / broken")
    q.add_argument("symbol")
    q.add_argument("--level", default="pd_high", help="pd_high, pd_low, on_high, on_low")
    q.add_argument("--lookback", type=int, default=60, help="Sessions to measure")
    q.add_argument("--interval", default="15m")
    q.add_argument("--threshold-ticks", type=float, default=2.0, help="Ticks beyond a level that count as a sweep")
    q.add_argument("--window-bars", type=int, default=4, help="Bars after first penetration to measure the excursion over — this defines what 'sweep' means")
    _add_date(q)

    q = fut_sub.add_parser("bars", help="Intraday bars in exchange time")
    q.add_argument("symbol")
    q.add_argument("--interval", default="5m")
    q.add_argument("--limit", type=int, default=40)
    _add_date(q)

    q = fut_sub.add_parser("size", help="Contracts permitted by an account, risk budget, and stop distance")
    q.add_argument("symbol")
    q.add_argument("--account", type=float, required=True)
    q.add_argument("--risk-pct", type=float, default=1.0)
    q.add_argument("--stop-ticks", type=float, required=True)
    _add_date(q)

    # ---- SMC framework ----------------------------------------------------
    p = sub.add_parser("smc", help="SMC/price-action framework: sweep → MSS → FVG state machine")
    smc_sub = p.add_subparsers(dest="smc_command", required=True)

    q = smc_sub.add_parser("explain", help="Operational definitions and current parameters")
    q.add_argument("symbol", nargs="?", default="NQ")

    q = smc_sub.add_parser("scan", help="Run the four-step state machine over recent sessions")
    q.add_argument("symbol")
    q.add_argument("--interval", default="5m")
    q.add_argument("--sessions", type=int, default=10)
    _add_date(q)

    q = smc_sub.add_parser("swings", help="Fractal swing points on the chosen timeframe")
    q.add_argument("symbol")
    q.add_argument("--interval", default="5m")
    q.add_argument("--lookback", type=int, default=None)
    q.add_argument("--limit", type=int, default=20)
    _add_date(q)

    q = smc_sub.add_parser("fvg", help="Unmitigated fair value gaps")
    q.add_argument("symbol")
    q.add_argument("--interval", default="5m")
    q.add_argument("--limit", type=int, default=20)
    _add_date(q)

    q = smc_sub.add_parser("bias", help="Step 1: HTF structure and draw on liquidity (1H + 15m)")
    q.add_argument("symbol", nargs="?", default="NQ")
    _add_date(q)

    q = smc_sub.add_parser("liquidity", help="Step 1: every liquidity target — PDH/PDL, ONH/ONL, session ranges, EQH/EQL")
    q.add_argument("symbol")
    q.add_argument("--interval", default="5m")
    _add_date(q)

    q = smc_sub.add_parser("replay", help="Replay a session to a point in time with the future withheld, then --reveal")
    q.add_argument("symbol", nargs="?", default="NQ")
    q.add_argument("--session", default=None, help="Trade date YYYY-MM-DD (default: most recent)")
    q.add_argument("--until", default="10:15", help="Cutoff time ET (default 10:15)")
    q.add_argument("--interval", default="5m")
    q.add_argument("--reveal", action="store_true", help="Show what happened after the cutoff")
    q.add_argument("--bars", type=int, default=24)
    _add_date(q)

    q = smc_sub.add_parser("rules", help="Scan with section 4 enforced and every setup sized to an account")
    q.add_argument("symbol")
    q.add_argument("--interval", default="5m")
    q.add_argument("--sessions", type=int, default=20)
    q.add_argument("--account", type=float, default=10000.0)
    _add_date(q)

    q = smc_sub.add_parser("sensitivity", help="How the setup count moves with the ambiguous parameters")
    q.add_argument("symbol")
    q.add_argument("--interval", default="5m")
    q.add_argument("--sessions", type=int, default=20)
    _add_date(q)

    # ---- paper trading ----------------------------------------------------
    p = sub.add_parser("paper", help="Paper trading book: execute, mark, and track a simulated portfolio")
    paper_sub = p.add_subparsers(dest="paper_command", required=True)

    q = paper_sub.add_parser("init", help="Open a paper book")
    q.add_argument("--cash", type=float, default=None, help="Starting cash (default from config.json)")
    q.add_argument("--force", action="store_true", help="Wipe an existing book and its trade history")

    q = paper_sub.add_parser("buy", help="Open or add to a position")
    q.add_argument("ticker")
    q.add_argument("--size-pct", type=float, default=None, help="Position size as %% of total equity")
    q.add_argument("--shares", type=float, default=None, help="Explicit share count instead of a %%")
    q.add_argument("--price", type=float, default=None, help="Override the fill reference price")
    q.add_argument("--stop", type=float, default=None)
    q.add_argument("--target", type=float, default=None)
    q.add_argument("--memory-id", default="", help="Link this fill to a logged decision")
    q.add_argument("--note", default="")
    _add_date(q)

    q = paper_sub.add_parser("sell", help="Close or trim a position")
    q.add_argument("ticker")
    q.add_argument("--shares", type=float, default=None)
    q.add_argument("--pct", type=float, default=None, help="Percent of the position to sell (default 100)")
    q.add_argument("--price", type=float, default=None)
    q.add_argument("--reason", default="")
    _add_date(q)

    q = paper_sub.add_parser("status", help="Positions, equity, P&L, cost drag, and alpha vs benchmark")
    _add_date(q)

    q = paper_sub.add_parser("mark", help="Mark to market, record an equity point, check stops and targets")
    _add_date(q)

    q = paper_sub.add_parser("history", help="Trade history with slippage and realized P&L")
    q.add_argument("--limit", type=int, default=40)

    q = paper_sub.add_parser("equity", help="Equity curve and drawdown")
    q.add_argument("--limit", type=int, default=30)

    # ---- memory -----------------------------------------------------------
    p = sub.add_parser("memory", help="Decision log, outcome scoring, lesson recall")
    mem_sub = p.add_subparsers(dest="memory_command", required=True)

    q = mem_sub.add_parser("log", help="Record a final decision")
    q.add_argument("--ticker", required=True)
    q.add_argument("--rating", required=True, help="Buy / Overweight / Hold / Underweight / Sell")
    q.add_argument("--action", required=True, help="BUY / HOLD / SELL")
    q.add_argument("--situation", required=True, help="2-3 sentence setup description — this is what recall matches on")
    q.add_argument("--report-dir", default="")
    q.add_argument("--entry-price", type=float, default=None)
    q.add_argument("--stop-loss", type=float, default=None)
    q.add_argument("--conviction", default="")
    _add_date(q)

    q = mem_sub.add_parser("recall", help="Lessons from similar past decisions")
    q.add_argument("--ticker", default="")
    q.add_argument("--query", default="")
    q.add_argument("--limit", type=int, default=5)

    q = mem_sub.add_parser("score", help="Score matured decisions against their benchmark")
    q.add_argument("--id", default="", help="Score one decision; omit to score every matured one")
    q.add_argument("--horizon", type=int, default=None)

    q = mem_sub.add_parser("lesson", help="Attach a lesson to a scored decision")
    q.add_argument("--id", required=True)
    q.add_argument("--text", required=True)

    q = mem_sub.add_parser("pending", help="Decisions that have matured and await scoring")
    q.add_argument("--horizon", type=int, default=None)

    q = mem_sub.add_parser("history", help="The decision log")
    q.add_argument("--ticker", default="")
    q.add_argument("--limit", type=int, default=30)

    mem_sub.add_parser("stats", help="Track record by rating")

    q = mem_sub.add_parser("show", help="Full JSON for one decision")
    q.add_argument("--id", required=True)

    return parser


def _run_dir(ticker: str, date: str) -> Path:
    return common.RESULTS_DIR / common.normalize_symbol(ticker) / date


STAGE_FILES = [
    ("market_report.md", "Market Analyst"),
    ("sentiment_report.md", "Sentiment Analyst"),
    ("news_report.md", "News Analyst"),
    ("fundamentals_report.md", "Fundamentals Analyst"),
    ("investment_debate.md", "Bull vs Bear debate"),
    ("investment_plan.md", "Research Manager"),
    ("trader_proposal.md", "Trader"),
    ("risk_debate.md", "Risk debate"),
    ("final_decision.md", "Portfolio Manager"),
]


def run_init(ticker: str, date: str) -> str:
    symbol = common.normalize_symbol(ticker)
    directory = _run_dir(symbol, date)
    (directory / "debate").mkdir(parents=True, exist_ok=True)

    state = {
        "ticker": symbol,
        "asset_type": common.asset_type(symbol),
        "date": date,
        "benchmark": common.benchmark_for(symbol, common.load_config()["benchmark_ticker"]),
        "config": common.load_config(),
        "created_at": common.datetime.now().isoformat(timespec="seconds"),
    }
    (directory / "run.json").write_text(json.dumps(state, indent=2))

    return (
        f"Run initialised.\n\n"
        f"  ticker:     {symbol} ({state['asset_type']})\n"
        f"  date:       {date}\n"
        f"  benchmark:  {state['benchmark']}\n"
        f"  directory:  {directory}\n\n"
        f"Stage artifacts go in that directory; debate turns in {directory / 'debate'}."
    )


def run_status(ticker: str, date: str) -> str:
    directory = _run_dir(ticker, date)
    if not directory.exists():
        return f"No run at {directory}. Start one with: bin/ta run init {ticker} --date {date}"
    rows = []
    for filename, stage in STAGE_FILES:
        path = directory / filename
        rows.append([
            stage, filename,
            "done" if path.exists() else "pending",
            f"{path.stat().st_size:,}b" if path.exists() else "—",
        ])
    return (
        f"## Run status — {common.normalize_symbol(ticker)} {date}\n\n"
        + common.markdown_table(["Stage", "File", "Status", "Size"], rows)
        + f"\n\nDirectory: {directory}"
    )


def dispatch(args: argparse.Namespace) -> str:
    date = getattr(args, "date", None) or common.today()
    if getattr(args, "date", None) and common.is_future(args.date):
        return f"<error: --date {args.date} is in the future. Analysis dates must be today or earlier.>"

    command = args.command

    if command == "doctor":
        return doctor.run()

    if command == "ohlcv":
        return market.price_history(args.ticker, date, args.days)
    if command == "indicators":
        names = [n.strip() for n in args.names.split(",") if n.strip()]
        if len(names) > 8:
            return "<error: at most 8 indicators — pick complementary ones, not redundant ones>"
        return market.indicators(args.ticker, date, names, args.days)
    if command == "snapshot":
        return market.snapshot(args.ticker, date)
    if command == "indicator-list":
        rows = [[k, v] for k, v in market.SUPPORTED_INDICATORS.items()]
        return "## Supported indicators\n\n" + common.markdown_table(["Name", "Use"], rows)

    if command == "fundamentals":
        return fundamentals.overview(args.ticker, date)
    if command == "balance-sheet":
        return fundamentals.balance_sheet(args.ticker, date, not args.annual)
    if command == "cashflow":
        return fundamentals.cashflow(args.ticker, date, not args.annual)
    if command == "income-statement":
        return fundamentals.income_statement(args.ticker, date, not args.annual)
    if command == "earnings":
        return fundamentals.earnings_calendar(args.ticker, date)
    if command == "estimates":
        return fundamentals.estimates(args.ticker, date, args.price)
    if command == "earnings-reactions":
        return fundamentals.earnings_reactions(args.ticker, date, args.limit)

    if command == "news":
        return news.ticker_news(args.ticker, date, args.days, args.limit)
    if command == "global-news":
        return news.global_news(date, args.days, args.limit)
    if command == "news-search":
        return news.search_news(args.query, date, args.days, args.limit)

    if command == "stocktwits":
        return social.stocktwits(args.ticker, args.limit)
    if command == "reddit":
        return social.reddit(args.ticker, args.limit)
    if command == "sentiment-bundle":
        return social.sentiment_bundle(args.ticker, date, args.days)

    if command == "macro":
        return macro.macro_indicators(args.indicator, date, args.days)
    if command == "macro-list":
        rows = sorted({(alias, series) for alias, series in macro.MACRO_SERIES.items()})
        return "## FRED indicator aliases\n\n" + common.markdown_table(["Alias", "Series id"], [list(r) for r in rows])
    if command == "predictions":
        return macro.prediction_markets(args.topic, args.limit)
    if command == "regime":
        return macro.market_regime(date)

    if command == "run":
        if args.run_command == "init":
            return run_init(args.ticker, date)
        return run_status(args.ticker, date)

    if command == "fut":
        sub = args.fut_command
        if sub == "specs":
            return futures.contract_reference(args.symbol, args.price, date)
        if sub == "levels":
            return futures.levels_report(args.symbol, date, args.interval)
        if sub == "level-stats":
            return futures.level_stats(
                args.symbol, args.level, args.lookback, args.interval,
                args.threshold_ticks, args.window_bars, date,
            )
        if sub == "bars":
            return futures.bars_report(args.symbol, args.interval, args.limit, date)
        if sub == "size":
            return futures.position_size(args.symbol, args.account, args.risk_pct, args.stop_ticks, date)

    if command == "smc":
        sub = args.smc_command
        if sub == "explain":
            return smc.explain(args.symbol)
        if sub == "scan":
            return smc.scan(args.symbol, args.interval, args.sessions, date)
        if sub == "swings":
            return smc.swings_report(args.symbol, args.interval, args.lookback, args.limit, date)
        if sub == "fvg":
            return smc.fvg_report(args.symbol, args.interval, args.limit, date)
        if sub == "bias":
            return smc.bias_report(args.symbol, date)
        if sub == "liquidity":
            return smc.liquidity_report(args.symbol, args.interval, date)
        if sub == "replay":
            return smc.replay(args.symbol, args.session, args.until, args.interval, args.reveal, args.bars, date)
        if sub == "rules":
            return smc.rules_report(args.symbol, args.interval, args.sessions, args.account, date)
        if sub == "sensitivity":
            return smc.sensitivity(args.symbol, args.interval, args.sessions, date)

    if command == "paper":
        sub = args.paper_command
        if sub == "init":
            return paper.init(args.cash, args.force)
        if sub == "buy":
            return paper.buy(
                args.ticker, date, size_pct=args.size_pct, shares=args.shares,
                price=args.price, stop=args.stop, target=args.target,
                memory_id=args.memory_id, note=args.note,
            )
        if sub == "sell":
            return paper.sell(
                args.ticker, date, shares=args.shares, pct=args.pct,
                price=args.price, reason=args.reason,
            )
        if sub == "status":
            return paper.status(date)
        if sub == "mark":
            return paper.mark(date)
        if sub == "history":
            return paper.history(args.limit)
        if sub == "equity":
            return paper.equity_curve(args.limit)

    if command == "memory":
        sub = args.memory_command
        if sub == "log":
            record_id = memory.log_decision(
                ticker=args.ticker, date=date, rating=args.rating, action=args.action,
                situation=args.situation, report_dir=args.report_dir,
                entry_price=args.entry_price, stop_loss=args.stop_loss,
                conviction=args.conviction,
            )
            # Bare id on line 1 so callers can capture it without parsing prose.
            return (
                f"{record_id}\n"
                f"Logged {args.rating} on {common.normalize_symbol(args.ticker)} ({date}).\n"
                f"Score after the horizon with: bin/ta memory score --id {record_id}"
            )
        if sub == "recall":
            return memory.recall(args.ticker, args.query, args.limit)
        if sub == "score":
            return memory.score(args.id, args.horizon)
        if sub == "lesson":
            return memory.add_lesson(args.id, args.text)
        if sub == "pending":
            return memory.pending(args.horizon)
        if sub == "history":
            return memory.history(args.ticker, args.limit)
        if sub == "stats":
            return memory.stats()
        if sub == "show":
            return memory.show(args.id)

    return f"<error: unhandled command {command}>"


def main() -> int:
    args = build_parser().parse_args()
    try:
        output = dispatch(args)
    except KeyboardInterrupt:
        return 130
    except common.FetchError as exc:
        # A source that could not be reached is a gap, not an error: render it in
        # the shape agents are instructed to surface, so it lands in the report
        # instead of being routed around. Zero exit code for the same reason
        # every other `unavailable` result has one.
        print(common.unavailable("data source", str(exc)))
        return 0
    except Exception as exc:  # noqa: BLE001 — a traceback on stdout would be read as data
        print(f"<error: {type(exc).__name__}: {exc}>")
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
