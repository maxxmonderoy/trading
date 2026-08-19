# Market data

## What to buy

**1-minute bars, continuous front-month, 2+ years.**

Three decisions that are expensive to get wrong:

**1-minute, not 5-minute.** The loader resamples 1m up to 5m/15m/30m/1h, so one
file feeds every timeframe the framework uses. The reverse is impossible —
buying 5-minute data permanently forecloses the 1-minute execution the spec
calls for.

**Continuous front-month, not individual contract months.** NQ and ES expire
quarterly. Raw contract months have to be stitched with roll adjustment, and
doing it wrong inserts fake gaps at every roll — four per year, each one looking
like a tradable move that never happened. Databento's continuous symbols
(`NQ.c.0`) handle this.

**2+ years minimum.** Walk-forward needs multiple folds, and each fold needs
enough trades to mean anything. At roughly 250 sessions a year and a setup
frequency of a few per month, one year is a single usable fold.

Vendors: Databento (CME MDP3), CME DataMine, or a broker export (Tradovate,
IBKR). Budget a few hundred dollars — check current pricing, it changes.

## Format

Save as `SYMBOL_INTERVAL.csv` — e.g. `NQ_1m.csv`. Picked up automatically by
`bin/ta bt` in preference to yfinance.

Required columns, any capitalisation: a timestamp column named one of
`timestamp` / `datetime` / `date` / `time` / `ts`, plus `open`, `high`, `low`,
`close`, `volume`.

## The one that will silently ruin everything

**Timestamps must be exchange time (America/New_York).**

A vendor shipping UTC shifts every session boundary and kill zone by 4–5 hours.
The backtest will run, produce plausible numbers, and mean nothing — your "NY
kill zone" results will actually describe the London morning. The loader
converts tz-aware input correctly, but it cannot detect a *naive* UTC column, so
convert before saving if your export is UTC-naive.

Sanity check after dropping a file in:

```bash
bin/ta bt data NQ
```

It prints the session count and date range. If the session count is roughly
250/year and the range matches what you bought, the timestamps are right.

This directory is gitignored apart from this file — market data is large and
usually licensed.
