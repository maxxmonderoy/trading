# TradingAgents — Claude Code edition

A multi-agent LLM trading desk, rebuilt on Claude Code primitives.

Twelve agents fill twelve seats. Four analysts gather evidence in parallel. A bull
and a bear debate it. A research manager rules and issues a rated plan. A trader
turns it into a transaction with an entry, a stop, and a size. Three risk analysts
— aggressive, conservative, neutral — argue over that transaction. A portfolio
manager decides, and logs the decision. Weeks later, `/reflect` scores it against
its benchmark and writes a lesson the next run will recall.

Ported from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents).
The roles, prompts, debate structure, and reflection loop come from upstream; the
LangGraph/Python runtime is replaced by subagents, slash commands, and files.

## Setup

```bash
./setup.sh          # creates .venv, installs yfinance/stockstats/pandas/requests
```

Then open the directory in Claude Code. That is the whole install — **no API keys
required**. FRED macro series are the one optional extra:

```bash
cp .env.example .env    # add a free FRED_API_KEY for CPI/PCE/payrolls series
```

Check the wiring any time with `bin/ta doctor` — it verifies dependencies, every
subagent definition, the slash commands, and that no documentation references a
`bin/ta` subcommand that does not exist.

The data layer's decision logic has its own tests, which run offline:

```bash
.venv/bin/python -m unittest discover tests -v
```

They cover the parts that must not drift — look-ahead truncation, the position
cap, outcome scoring, and the distinction between a source that failed and a
source that had nothing. Vendor response shapes are not asserted; `doctor` and a
live run are what catch those.

## Use

```
/analyze NVDA               # full pipeline — both debates
/analyze NVDA 2026-06-02    # dated run; every source truncates at that date
/quick NVDA                 # analysts → decision, debates skipped
/reflect                    # score matured decisions, write lessons, show record
/paper                      # review the book: stops, P&L, cost drag, alpha
```

Decisions can be executed into a simulated portfolio. `/analyze` offers the fill
command when a book exists; it never trades without asking.

```bash
bin/ta paper init --cash 100000
bin/ta paper buy NVDA --size-pct 5 --stop 205 --target 250
bin/ta paper mark        # checks stops against session lows, not just closes
bin/ta paper status      # equity, P&L, cost drag, alpha vs SPY buy-and-hold
```

Everything a run produces lands in `results/<TICKER>/<DATE>/`: four analyst
reports, both debate transcripts, the investment plan, the trader's proposal, and
the final decision. It is all plain markdown — readable, diffable, auditable.

The data layer is usable on its own:

```bash
bin/ta snapshot NVDA                     # verified ground truth
bin/ta indicators NVDA --names rsi,macd,atr
bin/ta sentiment-bundle NVDA             # news + StockTwits + Reddit
bin/ta estimates NVDA                    # forward P/E ladder, every EPS basis shown
bin/ta earnings-reactions NVDA           # gap-risk base rate from past prints
bin/ta regime                            # indices, VIX, rates, dollar, commodities
bin/ta predictions "fed rate cut"        # Polymarket implied odds
bin/ta memory stats                      # the desk's track record
bin/ta --help
```

## How it is built

| Upstream | Here |
| --- | --- |
| LangGraph state machine | `/analyze` orchestrating subagents |
| `AgentState` dict | markdown files in the run directory |
| LangChain tool bindings | `bin/ta` subcommands over Bash |
| ChromaDB vector memory | `memory/decisions.jsonl` + TF-IDF recall |
| `deep_think` / `quick_think` models | `model: opus` / `model: sonnet` frontmatter |
| Sequential analyst nodes | four analysts in parallel |

Full architecture, state table, and design rationale: [CLAUDE.md](CLAUDE.md).

## Design properties worth knowing

**The researchers have no data tools.** Bull and bear argue over the same fixed
evidence base. If either could fetch mid-debate, the winner would be whoever
searched hardest for confirming evidence.

**One command is deterministic ground truth.** `bin/ta snapshot` computes prices
and indicators with no model involved, and every agent is told its numbers must
match. This is the guard against the failure mode that makes LLM trading systems
dangerous: fluent, confident, invented price levels.

**A failed source is loud.** Sources return `<unavailable: reason>`, agents are
required to report the gap, and "no posts found" is kept distinct from "fetch
failed" — they mean opposite things about retail interest.

**Dated runs cannot see the future.** Every command truncates at `--date`, and web
search is banned outright on historical runs.

**Scoring is alpha, not return.** A long that fell 2% while the index fell 6% was
a good call, and the memory log grades it that way.

## What this system's market model actually is

Worth stating plainly, because twelve agents arguing carefully can imply analytical
coverage that is not there. The desk reads markets through **daily-bar indicator
technical analysis** — moving averages, MACD, RSI, Bollinger bands, ATR, VWMA —
plus fundamentals, news, and retail sentiment.

It does **not** model, and cannot currently see:

- **Candlestick structure** — no pattern recognition, no bar-by-bar reading of
  rejection, absorption, or exhaustion. Indicators summarise bars; they discard shape.
- **Liquidity pools and order flow** — stop clusters above swing highs and below
  swing lows, order blocks, fair value gaps, sweeps. None of it is represented.
- **Market depth and the real cost of size** — the paper book charges flat
  basis-point slippage, not slippage as a function of actual book depth.
- **Anything intraday.** The data layer is daily OHLCV end to end. Liquidity
  concepts are largely intraday concepts, so this is a structural limit, not a
  missing feature — adding them means a different data source and a different
  cost model, not a new prompt.

An agent asked about liquidity pools with these tools will reason plausibly and
without evidence, which is the exact failure mode the rest of this design works to
prevent. Treat the framework boundary as real.

## Limitations

- **Not backtested.** Upstream reports favourable backtest results; this port
  inherits the architecture, not the evidence. Its track record is whatever
  `bin/ta memory stats` shows after you have run it, and below ~30 scored
  decisions that number is descriptive, not predictive.
- **No live execution path, by design.** There is no broker or wallet integration
  and nothing that places a real order. The paper book is the only execution
  surface.
- **Free data sources.** yfinance quotes are delayed, Reddit rate-limits
  anonymous clients, and fundamentals are vendor-computed. Good enough for
  research; not an institutional feed.
- **Reflection needs patience.** Lessons only accrue after decisions mature past
  the 21-day horizon. The first weeks are a cold start with an empty memory.
- **LLM agents fabricate.** Every structural guard here exists because of that,
  and none of them is complete.

## Disclaimer

Research output from an experimental multi-agent system. Not financial advice.
LLM agents fabricate, data sources fail silently, and a plausible-sounding thesis
is not a validated one. Verify independently before risking capital.
