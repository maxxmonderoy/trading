---
description: Score matured past decisions against their benchmark, write lessons into memory, and show the desk's track record.
argument-hint: [TICKER]
allowed-tools: Bash, Read, Agent
---

Run reflection over past decisions$ARGUMENTS.

This closes the loop that makes the desk more than a report generator: a decision
is logged, held for the configured horizon, scored against its benchmark, and
turned into a lesson that future runs recall.

## Steps

1. **Find matured decisions**

```bash
bin/ta memory pending
```

Decisions become scorable once `reflection_horizon_days` (config.json, default 21)
have passed since the analysis date. If nothing is pending, say so, show
`bin/ta memory history`, and stop — do not score anything early. A truncated
horizon measures noise.

2. **Score them**

```bash
bin/ta memory score
```

This computes raw return, benchmark return, and **alpha** for each matured
decision, and assigns a verdict (correct / neutral / wrong). Alpha is the grade:
a long that fell 2% while the index fell 6% was a good call.

Regional listings and crypto are scored against an appropriate benchmark
automatically (`^N225` for `.T`, `BTC-USD` for crypto pairs, and so on).

3. **Write a lesson for each newly scored decision**

Invoke the `reflection-analyst` subagent once per decision, passing the memory
`ID`, the outcome figures from step 2, and the original run directory. It writes a
2-4 sentence lesson and stores it with `bin/ta memory lesson`.

Run these in parallel when several matured at once — they are independent.

4. **Show the track record**

```bash
bin/ta memory stats
bin/ta memory history --limit 15
```

## Report

- What was scored, with each decision's alpha and verdict.
- The lessons written, verbatim.
- **Patterns across decisions** — this is the real payoff. Look for: a rating the
  desk is systematically wrong on, a regime where it does worse, a recurring
  reasoning failure (over-weighting sentiment, ignoring earnings gap risk,
  conviction untracked by evidence quality).
- Sample-size honesty: below ~30 scored decisions, the track record is descriptive,
  not predictive. Say so rather than reading a trend into six data points.

If a filter argument was given, scope the report to that ticker but keep the
track-record numbers whole-desk — a per-ticker hit rate on four decisions means
nothing.
