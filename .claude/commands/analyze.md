---
description: Run the full trading-desk pipeline on a ticker — four analysts, bull/bear debate, research manager, trader, three-way risk debate, portfolio manager.
argument-hint: <TICKER> [YYYY-MM-DD]
allowed-tools: Bash, Read, Write, Agent
---

Run the full trading desk on **$1**, analysis date **$2** (default: today).

You are the **graph runner**. Upstream TradingAgents is a LangGraph state machine;
here you are the state machine — you sequence the stages, carry state through files,
and enforce the loop conditions. Do not do the analysis yourself. Every stage runs in
its subagent, whose definition holds the prompt that makes it good at its seat.

---

## Stage 0 — Initialise

```bash
bin/ta run init $1 --date $2
```

Note `RUN_DIR` from the output (`results/<TICKER>/<DATE>/`). Read `config.json` for
`max_debate_rounds`, `max_risk_rounds`, and `analysts`. Announce the plan to the user
in two lines: ticker, date, rounds configured, and which analysts will run.

If `bin/ta run status` shows stage files already present for this ticker and date, tell
the user and ask whether to resume from the first missing stage or start fresh. Do not
silently overwrite a completed run.

---

## Stage 1 — Analyst team (parallel)

Invoke all four in **one message** so they run concurrently. They touch different
files and share nothing, so there is no reason to serialise them.

| Subagent | Writes |
| --- | --- |
| `market-analyst` | `RUN_DIR/market_report.md` |
| `sentiment-analyst` | `RUN_DIR/sentiment_report.md` |
| `news-analyst` | `RUN_DIR/news_report.md` |
| `fundamentals-analyst` | `RUN_DIR/fundamentals_report.md` |

Give each exactly: `TICKER=$1`, `DATE=$2`, `RUN_DIR=<path>`, and its output path.

Skip any analyst not listed in `config.json`'s `analysts` array.

**Gate before proceeding**: confirm all four files exist and are non-trivial. If one is
missing or empty, re-invoke that analyst once. If it fails again, tell the user which
evidence base is missing and ask whether to continue — the debate stages will be
weaker and everyone downstream needs to know.

---

## Stage 2 — Investment debate (bull vs bear)

Sequential and alternating. `max_debate_rounds` in config; **one round = one bull turn
plus one bear turn** (upstream's `count >= 2 * max_debate_rounds`).

For each round `N` from 1 to `max_debate_rounds`:

1. `bull-researcher` — pass `ROUND=N` and, for N > 1, the transcript path.
   Writes `RUN_DIR/debate/round-N-bull.md`.
2. `bear-researcher` — pass `ROUND=N` and the bull's file from this round so it
   answers the actual argument. Writes `RUN_DIR/debate/round-N-bear.md`.

After the final round, concatenate the turns in order into
`RUN_DIR/investment_debate.md` so downstream agents read one file:

```bash
cat "$RUN_DIR"/debate/round-*-{bull,bear}.md > "$RUN_DIR/investment_debate.md"
```

Order matters — verify the concatenation is chronological (round 1 bull, round 1 bear,
round 2 bull, ...) and fix it by hand if the glob ordering is wrong.

Print each side's short reply as it arrives so the user can watch the debate develop.

---

## Stage 3 — Research Manager

Invoke `research-manager` with `TICKER`, `DATE`, `RUN_DIR`. It judges the debate and
writes `RUN_DIR/investment_plan.md` with a rating from
{Buy, Overweight, Hold, Underweight, Sell}.

Show the user the rating and the one-line rationale.

---

## Stage 4 — Trader

Invoke `trader`. Writes `RUN_DIR/trader_proposal.md` with action, entry, stop, size,
horizon, and a `FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**` marker line.

---

## Stage 5 — Risk debate (three-way)

Sequential, in fixed order. `max_risk_rounds` in config; **one round = one turn each
from all three analysts** (upstream's `count >= 3 * max_risk_rounds`).

For each round `N` from 1 to `max_risk_rounds`:

1. `aggressive-risk-analyst` → `RUN_DIR/debate/risk-round-N-aggressive.md`
2. `conservative-risk-analyst` → `RUN_DIR/debate/risk-round-N-conservative.md`
3. `neutral-risk-analyst` → `RUN_DIR/debate/risk-round-N-neutral.md`

Each speaker must receive the paths of everything said so far this debate, so it is
rebutting real arguments rather than imagined ones. The order is not arbitrary:
aggressive opens, conservative answers, neutral adjudicates what it just heard.

Concatenate in speaking order into `RUN_DIR/risk_debate.md`.

---

## Stage 6 — Portfolio Manager

Invoke `portfolio-manager`. It recalls lessons from memory, weighs the risk debate,
writes `RUN_DIR/final_decision.md`, and logs the decision to the memory store.

---

## Stage 7 — Paper execution (if a book exists)

Run `bin/ta paper status`. If there is no book, skip this stage silently.

If a book exists and the decision is actionable (**Buy**, **Overweight**, **Sell**, or
**Underweight**), show the user the exact command that would execute it and **ask
before running it** — executing a trade is the user's call, not yours:

```bash
bin/ta paper buy TICKER --size-pct <PM's size> --stop <PM's stop> --target <PM's target> \
  --memory-id <id from stage 6> --note "<one-line thesis>"
```

For a Sell or Underweight on a name already in the book, use `paper sell` with
`--pct` matching the PM's trim. For a Sell with no position open, there is nothing
to execute — say so rather than shorting, which this book does not model.

On **Hold**, take no action and say what the desk is waiting for.

## Stage 8 — Report to the user

Print, in this order:

1. **The decision** — rating, action, size, entry, stop, horizon, conviction.
2. **The one-line reason.**
3. **Where the desk disagreed** — any place the four analysts, the two researchers, or
   the three risk seats reached materially different conclusions. This is the highest-
   value output of a multi-agent desk and it is lost if you only report the consensus.
   Never smooth it over.
4. **Data quality** — every `<unavailable>` source or stale-data warning that appeared,
   and how much it constrains the call.
5. **File paths** — `RUN_DIR`, and the memory id for later scoring.
6. **The standing disclaimer** (see CLAUDE.md): research output, not financial advice.

---

## Rules for you as graph runner

- **Never write analysis yourself.** If a stage fails, re-invoke it or report the
  failure. Filling in for a failed agent silently destroys the property that makes
  this system worth running — that each conclusion was reached by a seat with a
  defined mandate and contestable output.
- **Never let a later stage start before its inputs exist on disk.** Check the files.
- **Do not summarise away disagreement.** Contradiction between seats is signal.
- **Pass full paths, not pasted content.** State lives in files; that is what makes a
  run resumable and auditable after the fact.
- **Report honestly.** If three sources were unavailable, the headline is a low-
  confidence call on partial data — say that first, not last.
