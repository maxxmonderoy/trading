# Market data

Drop vendor CSVs here as `SYMBOL_INTERVAL.csv` — e.g. `NQ_5m.csv`. They are
picked up automatically by `bin/ta bt` in preference to yfinance.

**Required columns** (any capitalisation): a timestamp column named one of
`timestamp` / `datetime` / `date` / `time` / `ts`, plus `open`, `high`, `low`,
`close`, `volume`.

**Timestamps must be exchange time (America/New_York).** A vendor shipping UTC
will shift every session boundary and kill zone by 4-5 hours, producing a
backtest that looks fine and means nothing. If your export is UTC, convert
before saving — the loader converts tz-aware input but cannot detect a naive
UTC column.

Sources worth considering for CME intraday: Databento, CME DataMine, or a
broker export (Tradovate, IBKR). Budget a few hundred dollars for enough NQ/ES
history to make an out-of-sample test meaningful.

This directory is gitignored apart from this file — market data is large and
usually licensed.
