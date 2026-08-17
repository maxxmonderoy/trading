---
description: Review the paper trading book — positions, P&L, stop/target alerts, cost drag, and alpha vs buy-and-hold.
argument-hint: [status|mark|review|close TICKER]
allowed-tools: Bash, Read, Agent
---

Manage the paper book. Requested action: **$ARGUMENTS** (default: full review).

## Actions

**`status`** — print `bin/ta paper status` and stop. Positions, equity, P&L, cost
drag, alpha vs benchmark.

**`mark`** — run `bin/ta paper mark`, which records an equity point and checks every
stop and target against the session's actual high and low (not just the close —
stops trigger intraday). Report any alerts and act on them per the review rules below.

**`close TICKER`** — sell the position:
```bash
bin/ta paper sell TICKER --reason "<why>"
```
Always give a reason; it is the only record of why an exit happened.

**`review`** (default) — the full pass:

1. `bin/ta paper mark` — check stops and targets first. A hit stop is not a
   discussion; exit it, then explain.
2. `bin/ta paper status` — current book.
3. `bin/ta paper history` and `bin/ta paper equity` — trade record and drawdown.
4. For **every open position**, check whether the thesis still holds:
   - `bin/ta snapshot TICKER` — where price actually is now
   - `bin/ta news TICKER --days 7` — anything that breaks the thesis
   - Read the original `final_decision.md` (its path is in the position note or
     findable via `bin/ta memory show --id <memory_id>`)
   - Judge: **hold**, **trim**, or **exit** — and say which observable decided it
5. If a position needs a genuine re-analysis rather than a check-in, run
   `/analyze TICKER` instead of guessing.

## Rules

- **Act on a hit stop.** The stop was set when the thesis was written and nobody
  was in the position. Talking yourself out of it now is the single most expensive
  habit available to a discretionary trader. Exit, then discuss.
- **Report cost drag whenever it is material.** The status output computes costs as
  a share of total P&L. If that ratio is high, say so plainly — an edge smaller
  than round-trip costs is not an edge.
- **Compare to buy-and-hold every time.** The book's alpha vs SPY is the only number
  that says whether any of this activity was worth doing. A book up 4% while SPY is
  up 7% is a losing book, however good the individual write-ups read.
- **Never invent fills.** Prices come from `bin/ta`, and `--price` is only for
  recording a fill that genuinely happened at a different level.
- **Do not overtrade.** Most reviews should end in "no action". Churn is how a paper
  book converts a real edge into slippage.
