---
description: Fast read on a ticker — four analysts in parallel, then straight to a portfolio-manager decision. Skips both debates.
argument-hint: <TICKER> [YYYY-MM-DD]
allowed-tools: Bash, Read, Write, Agent
---

Quick analysis of **$1**, analysis date **$2** (default: today).

This is the full pipeline with both debate stages removed: evidence in, decision out.
Use it for triage across several names, or when the question is "is there anything
here worth the full run?"

## Stages

1. **Init** — `bin/ta run init $1 --date $2`, note `RUN_DIR`.

2. **Analysts (parallel, one message)** — `market-analyst`, `sentiment-analyst`,
   `news-analyst`, `fundamentals-analyst`. Each gets `TICKER`, `DATE`, `RUN_DIR`,
   and its output path.

3. **Decision** — invoke `portfolio-manager` with `TICKER`, `DATE`, `RUN_DIR` and this
   explicit note in the prompt:

   > This is a QUICK run: there was no bull/bear debate and no risk debate. Decide
   > directly from the four analyst reports. Because no adversarial process tested
   > these conclusions, cap your conviction at **medium** and say in the decision
   > that it came from a quick run. Log it with `--conviction` set accordingly.

## Report

Rating, action, size, entry, stop, the one-line reason, any data-quality caveats,
`RUN_DIR`, and the memory id.

Then tell the user plainly: **this call skipped the adversarial stages.** The debates
are where a superficially attractive thesis usually breaks. If the answer matters,
follow up with `/analyze $1`.

Include the standing disclaimer from CLAUDE.md.
