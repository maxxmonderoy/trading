# TradingAgents — Claude Code edition

A multi-agent LLM trading desk: four analysts gather evidence, a bull and a bear
debate it, a research manager rules, a trader proposes a transaction, three risk
analysts argue it, and a portfolio manager decides. Decisions are logged, scored
against a benchmark after a holding horizon, and turned into lessons that future
runs recall.

This is a rebuild of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
on Claude Code primitives. The agent roles, prompt logic, debate structure, and
reflection loop are ported from upstream. The Python/LangGraph runtime is not —
subagents replace graph nodes, files replace the state dict, slash commands
replace the CLI, and a keyless Python CLI replaces the LangChain tool bindings.

## Layout

```
.claude/agents/        12 subagents — one per desk seat
.claude/commands/      /analyze, /quick, /reflect
tools/ta.py            the data layer CLI (argparse dispatcher)
tools/dataflows/       market, fundamentals, news, social, macro, memory, paper, futures, smc
tools/backtest/        backtrader harness: feeds, strategy, engine
data/                  vendor CSVs (gitignored) — see data/README.md
bin/ta                 → entry point (activates .venv, loads .env)
config.json            debate rounds, analyst roster, benchmark, horizon
results/<TICKER>/<DATE>/   run artifacts (the state dict, as files)
memory/decisions.jsonl     decision log with outcomes and lessons
paper/                     paper trading book: portfolio.json, trades.jsonl, equity.jsonl
data_cache/            OHLCV and fetch cache
```

## The pipeline

```
                    ┌──────────────────────────────────────┐
                    │  Stage 1 — Analysts (parallel)       │
                    │  market · sentiment · news · fund.   │
                    └───────────────────┬──────────────────┘
                                        │  4 report files
                    ┌───────────────────▼──────────────────┐
                    │  Stage 2 — Investment debate         │
                    │  bull ⇄ bear  × max_debate_rounds    │
                    └───────────────────┬──────────────────┘
                    ┌───────────────────▼──────────────────┐
                    │  Stage 3 — Research Manager          │
                    │  rating + investment plan            │
                    └───────────────────┬──────────────────┘
                    ┌───────────────────▼──────────────────┐
                    │  Stage 4 — Trader                    │
                    │  entry · stop · size · horizon       │
                    └───────────────────┬──────────────────┘
                    ┌───────────────────▼──────────────────┐
                    │  Stage 5 — Risk debate               │
                    │  aggressive → conservative → neutral │
                    │  × max_risk_rounds                   │
                    └───────────────────┬──────────────────┘
                    ┌───────────────────▼──────────────────┐
                    │  Stage 6 — Portfolio Manager         │
                    │  recall memory → decide → log        │
                    └──────────────────────────────────────┘
                                        │
                            ...horizon elapses...
                                        │
                    ┌───────────────────▼──────────────────┐
                    │  /reflect — score vs benchmark,      │
                    │  write lesson, recall it next time   │
                    └──────────────────────────────────────┘
```

Loop conditions match upstream: one investment round is a bull turn plus a bear
turn (`count >= 2 * max_debate_rounds`); one risk round is a turn from each of the
three seats (`count >= 3 * max_risk_rounds`).

## State lives in files

There is no shared memory between subagents — Claude Code subagents each get a
clean context and cannot call each other. State is therefore the run directory:

| File | Written by | Read by |
| --- | --- | --- |
| `market_report.md` | market-analyst | researchers, risk seats, trader, PM |
| `sentiment_report.md` | sentiment-analyst | researchers, risk seats |
| `news_report.md` | news-analyst | researchers, risk seats |
| `fundamentals_report.md` | fundamentals-analyst | researchers, risk seats |
| `debate/round-N-{bull,bear}.md` | researchers | the other side, research manager |
| `investment_debate.md` | graph runner (concat) | research manager |
| `investment_plan.md` | research-manager | trader, risk seats, PM |
| `trader_proposal.md` | trader | risk seats, PM |
| `debate/risk-round-N-*.md` | risk seats | later speakers, PM |
| `final_decision.md` | portfolio-manager | user, `/reflect` |

Agents are passed **paths**, never pasted content. That keeps context small and
makes any run resumable and auditable after the fact.

## Non-negotiable rules

These apply to every agent and to the orchestrator.

1. **No fabricated data.** Every number, headline, post, or price level must come
   from tool output in the current run. Not from memory, not from what is
   typical, not from what a reasonable value would be. This is the failure mode
   that makes an LLM trading system actively dangerous — the output is fluent and
   confident either way, and downstream agents have no way to detect it.

2. **`<unavailable: ...>` means the source failed.** Report the gap prominently
   and lower confidence. Never treat missing data as neutral data, and never
   substitute a plausible stand-in. Note that "no posts found" and "fetch failed"
   are different findings.

3. **The verified snapshot is the source of truth.** `bin/ta snapshot` is computed
   deterministically. Any exact price or indicator value that conflicts with it is
   wrong. Flag conflicts; do not reconcile them by inventing a figure.

4. **No look-ahead.** Every `bin/ta` command truncates at `--date`. For a dated
   historical run, web search is banned outright — it returns today's knowledge
   about that period, which is precisely the leak the date filter prevents.

5. **Disagreement is the product.** A four-analyst desk that always agrees has
   wasted its structure. Surface contradictions between seats; never smooth them
   into a consensus that nobody actually argued.

6. **A parameterised result is not a measurement.** The SMC framework's prose
   ("quick wick", "aggressive reverse", "swing high") is not computable; every
   such term became a named parameter in `config.json`. Quote no scan result
   without the parameters that produced it, and run `bin/ta smc sensitivity`
   before believing any of them — on the current sample, changing one
   unspecified parameter moves the setup count between 0 and 3.

7. **Paper fills are simulated, and their costs are real.** Every fill takes
   slippage against you, and `paper status` reports cumulative cost drag as a share
   of total P&L. Never suppress that line to make a book look better, and always
   compare the book to benchmark buy-and-hold — active management that trails the
   index is a losing book no matter how good the write-ups read.

8. **The disclaimer ships with every decision:**

   > This is research output from an experimental multi-agent system, not
   > financial advice. LLM agents fabricate, data sources fail silently, and a
   > backtested process is not a validated one. Verify independently before
   > risking capital.

## Data layer

One CLI, `bin/ta`, replaces upstream's ~15 LangChain tool bindings. Every command
takes `--date YYYY-MM-DD` and truncates its data there.

```bash
bin/ta snapshot NVDA              # VERIFIED ground truth — numbers must match this
bin/ta ohlcv NVDA --days 60
bin/ta indicators NVDA --names rsi,macd,close_50_sma
bin/ta regime                     # indices, VIX, rates, dollar, commodities
bin/ta fundamentals NVDA
bin/ta income-statement NVDA      # also balance-sheet, cashflow, earnings
bin/ta estimates NVDA             # consensus by period + reconciled forward P/E ladder
bin/ta earnings-reactions NVDA    # measured gap-risk base rate from past prints
bin/ta news NVDA                  # also global-news, news-search
bin/ta sentiment-bundle NVDA      # news + StockTwits + Reddit in one block
bin/ta macro cpi                  # FRED (needs FRED_API_KEY); see macro-list
bin/ta predictions "fed rate cut" # Polymarket implied odds

# Futures (NQ/ES/MNQ/MES)
bin/ta fut specs                  # multipliers, tick values, notional, round-trip cost
bin/ta fut levels NQ              # session reference levels, untested liquidity
bin/ta fut level-stats NQ --level pd_high   # measured touch/sweep/break base rates
bin/ta fut size MNQ --account 10000 --risk-pct 1 --stop-ticks 40
bin/ta fut bars NQ --interval 5m

# SMC framework (sweep -> MSS -> FVG state machine)
bin/ta smc explain                # operational definitions and parameters
bin/ta smc scan NQ --sessions 20  # run the four-step state machine
bin/ta smc rules NQ --account 10000  # with section 4 enforced and sized
bin/ta smc sensitivity NQ         # how setup count moves with the ambiguous params
bin/ta smc swings NQ / smc fvg NQ
bin/ta smc replay NQ --until 10:15   # calibration: future withheld, then --reveal

# Backtest (backtrader) — the gate on whether any of this is real
bin/ta bt data NQ                    # what data is loaded, and whether it is enough
bin/ta bt run NQ                     # single pass, net of costs, with n and t-stat
bin/ta bt walkforward NQ             # fit on a fold, report the NEXT fold's result
bin/ta bt null NQ                    # coin-flip-direction baseline it must beat
bin/ta memory recall --ticker NVDA --query "..."
bin/ta memory log|score|lesson|history|stats|pending|show
bin/ta paper init|buy|sell|status|mark|history|equity
bin/ta run init|status NVDA
bin/ta doctor                     # verify deps, subagents, commands, docs
```

Sources: yfinance (prices, statements, news), StockTwits, Reddit RSS, Google News
RSS, FRED, Polymarket Gamma. Only FRED needs a key; everything else is keyless.

Failures return `<unavailable: source — reason>` on stdout with a zero exit code,
by design: an agent that sees a placeholder reports a gap, while an agent that
sees a traceback tends to route around it and invent the number.

## What changed from upstream, and why

| Upstream | Here | Why |
| --- | --- | --- |
| LangGraph state machine | `/analyze` command driving subagents | Claude Code has no graph runtime; the orchestrator is the graph |
| `AgentState` TypedDict | files in `results/<TICKER>/<DATE>/` | subagents have isolated context; files are the only shared state |
| LangChain tool bindings | `bin/ta` subcommands via Bash | agents already have Bash; one CLI beats fifteen bindings |
| ChromaDB + embeddings | `memory/decisions.jsonl` + TF-IDF recall | at hundreds of decisions this is a text-search problem, and it drops three dependencies |
| Pydantic structured output | prescribed markdown sections in each agent file | no structured-output API for subagents; section headers do the same job |
| `deep_think_llm` / `quick_think_llm` | `model: opus` / `model: sonnet` in frontmatter | same split — cheap models gather, expensive models judge |
| Sequential analyst nodes | four analysts in parallel | they are independent; the graph serialised them only because it had to |
| Multi-provider LLM plumbing | (removed) | Claude Code supplies the model |
| Alpha Vantage / Finnhub keys | yfinance + keyless sources | a fresh clone should work with no API keys |
| (stops at the decision) | `bin/ta paper` book | a decision that is never executed is never really graded — sizing, stops, and costs only show up in a filled trade |

## Working on this project

- **Prompts live in the agent files.** `.claude/agents/*.md` is the product. Fixing
  a bad report means editing the seat's mandate, not adding orchestrator patches.
- **The data layer never guesses.** If a source fails, return `unavailable()` with
  a reason an agent can act on. Never return an empty string or a default value.
- **Test data commands live** before shipping them — vendor APIs change shape
  without notice, and a command that silently returns nothing is worse than one
  that errors.
- **Preserve the no-look-ahead guarantee** in any new dataflow. Filter on
  `curr_date`, and prefer sources that carry publication dates.
- Only FRED needs a key. Keep it that way if you add sources — the setup cost of
  this project is its main advantage over upstream.
