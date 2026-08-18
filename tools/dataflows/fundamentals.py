"""Company fundamentals — port of upstream `alpha_vantage_fundamentals` / yfinance paths.

Alpha Vantage is optional upstream and key-gated; this port uses yfinance as the
sole free source so a fresh clone works with no API keys at all. Statements are
filtered to periods ending on or before the analysis date for the same
no-look-ahead reason the price data is.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

import pandas as pd

from .common import (
    asset_type,
    fmt_num,
    markdown_table,
    normalize_symbol,
    parse_date,
    unavailable,
    unavailable_empty,
)


def _ticker(symbol: str):
    import yfinance as yf

    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        return yf.Ticker(symbol)


def _statement(symbol: str, kind: str, quarterly: bool, curr_date: str) -> str:
    attr = {
        "balance_sheet": "quarterly_balance_sheet" if quarterly else "balance_sheet",
        "cashflow": "quarterly_cashflow" if quarterly else "cashflow",
        "income_statement": "quarterly_income_stmt" if quarterly else "income_stmt",
    }[kind]

    try:
        frame = getattr(_ticker(symbol), attr)
    except Exception as exc:
        return unavailable(f"yfinance {kind}", f"{type(exc).__name__}: {exc}")
    if frame is None or frame.empty:
        return unavailable_empty(f"yfinance {kind}", f"no data for {symbol}")

    # Columns are period-end dates; drop any period ending after the analysis date.
    cutoff = pd.to_datetime(curr_date)
    keep = [c for c in frame.columns if pd.to_datetime(c, errors="coerce") <= cutoff]
    if not keep:
        return unavailable(f"yfinance {kind}", f"no periods ending on or before {curr_date}")
    frame = frame[keep[:4]]

    headers = ["Line item", *[pd.to_datetime(c).strftime("%Y-%m-%d") for c in frame.columns]]
    rows = [
        [str(index), *[fmt_num(frame.loc[index, c], 0) for c in frame.columns]]
        for index in frame.index
        if not all(pd.isna(frame.loc[index, c]) for c in frame.columns)
    ]
    period = "Quarterly" if quarterly else "Annual"
    title = kind.replace("_", " ").title()
    return f"## {period} {title} — {symbol}\n\n{markdown_table(headers, rows)}"


def balance_sheet(symbol: str, curr_date: str, quarterly: bool = True) -> str:
    return _statement(normalize_symbol(symbol), "balance_sheet", quarterly, curr_date)


def cashflow(symbol: str, curr_date: str, quarterly: bool = True) -> str:
    return _statement(normalize_symbol(symbol), "cashflow", quarterly, curr_date)


def income_statement(symbol: str, curr_date: str, quarterly: bool = True) -> str:
    return _statement(normalize_symbol(symbol), "income_statement", quarterly, curr_date)


_PROFILE_FIELDS = [
    ("longName", "Name"), ("sector", "Sector"), ("industry", "Industry"),
    ("country", "Country"), ("fullTimeEmployees", "Employees"),
    ("marketCap", "Market cap"), ("enterpriseValue", "Enterprise value"),
]

_VALUATION_FIELDS = [
    ("trailingPE", "Trailing P/E"), ("forwardPE", "Forward P/E"),
    ("priceToBook", "Price / book"), ("priceToSalesTrailing12Months", "Price / sales (TTM)"),
    ("enterpriseToEbitda", "EV / EBITDA"), ("pegRatio", "PEG ratio"),
    ("dividendYield", "Dividend yield"), ("beta", "Beta"),
]

_HEALTH_FIELDS = [
    ("totalRevenue", "Revenue (TTM)"), ("grossMargins", "Gross margin"),
    ("operatingMargins", "Operating margin"), ("profitMargins", "Profit margin"),
    ("returnOnEquity", "Return on equity"), ("returnOnAssets", "Return on assets"),
    ("revenueGrowth", "Revenue growth (YoY)"), ("earningsGrowth", "Earnings growth (YoY)"),
    ("totalCash", "Total cash"), ("totalDebt", "Total debt"),
    ("debtToEquity", "Debt / equity"), ("currentRatio", "Current ratio"),
    ("freeCashflow", "Free cash flow"), ("operatingCashflow", "Operating cash flow"),
]

_MARKET_FIELDS = [
    ("currentPrice", "Current price"), ("targetMeanPrice", "Analyst target (mean)"),
    ("targetHighPrice", "Analyst target (high)"), ("targetLowPrice", "Analyst target (low)"),
    ("recommendationKey", "Analyst consensus"), ("numberOfAnalystOpinions", "Analyst count"),
    ("fiftyTwoWeekHigh", "52-week high"), ("fiftyTwoWeekLow", "52-week low"),
    ("sharesOutstanding", "Shares outstanding"), ("heldPercentInstitutions", "Institutional ownership"),
    ("shortPercentOfFloat", "Short % of float"),
]


def _section(title: str, info: dict, fields: list[tuple[str, str]]) -> str:
    rows = [[label, fmt_num(info[key]) if isinstance(info.get(key), (int, float)) else str(info[key])]
            for key, label in fields if info.get(key) not in (None, "")]
    if not rows:
        return ""
    return f"### {title}\n\n{markdown_table(['Metric', 'Value'], rows)}\n"


def overview(symbol: str, curr_date: str) -> str:
    """Company profile, valuation, health, and street view — upstream's `get_fundamentals`."""
    symbol = normalize_symbol(symbol)
    if asset_type(symbol) == "crypto":
        return unavailable(
            "fundamentals",
            f"{symbol} is a crypto pair — traditional fundamentals do not apply. "
            "Base the fundamentals report on tokenomics, network activity, and flows instead, "
            "and state clearly that statement-level fundamentals are unavailable",
        )

    try:
        info = _ticker(symbol).get_info()
    except Exception as exc:
        return unavailable("yfinance fundamentals", f"{type(exc).__name__}: {exc}")
    if not info or len(info) < 5:
        return unavailable_empty("yfinance fundamentals", f"empty profile for {symbol}")

    sections = [
        _section("Company profile", info, _PROFILE_FIELDS),
        _section("Valuation", info, _VALUATION_FIELDS),
        _section("Financial health & growth", info, _HEALTH_FIELDS),
        _section("Market & street view", info, _MARKET_FIELDS),
    ]
    summary = str(info.get("longBusinessSummary", "")).strip()
    business = f"### Business summary\n\n{summary}\n" if summary else ""

    return (
        f"## Fundamentals — {symbol} (as of {curr_date})\n\n"
        + "\n".join(s for s in sections if s)
        + "\n"
        + business
        + "\n_Source: yfinance. Ratios are vendor-computed TTM figures; period-end "
        "statement data is in the balance-sheet / cashflow / income-statement commands._"
    )


_PERIOD_LABELS = {
    "0q": "Current quarter",
    "+1q": "Next quarter",
    "0y": "Current fiscal year",
    "+1y": "Next fiscal year",
}


def estimates(symbol: str, curr_date: str, price: float | None = None) -> str:
    """Consensus estimates with their periods made explicit — plus a reconciled forward P/E.

    Added after a live run where the whole decision turned on an unresolvable
    dispute: one researcher computed forward P/E as (next-quarter estimate × 4)
    and got 27x, the vendor reported 17.6x, and no tool exposed which period the
    vendor used. The debate scored the gap as evidence rather than as a missing
    datum. A multiple is meaningless without its denominator's period, so this
    command always prints the period alongside the number and derives the
    multiple itself rather than trusting a vendor field.
    """
    symbol = normalize_symbol(symbol)
    ticker = _ticker(symbol)

    def _fetch(name):
        try:
            frame = getattr(ticker, name)()
            return frame if frame is not None and not frame.empty else None
        except Exception:
            return None

    earnings = _fetch("get_earnings_estimate")
    revenue = _fetch("get_revenue_estimate")
    trend = _fetch("get_eps_trend")
    revisions = _fetch("get_eps_revisions")

    if earnings is None and revenue is None:
        return unavailable_empty("yfinance analyst estimates", f"no estimate data for {symbol}")

    sections = []

    if earnings is not None:
        rows = [
            [
                _PERIOD_LABELS.get(str(period), str(period)), str(period),
                fmt_num(row.get("avg")), fmt_num(row.get("low")), fmt_num(row.get("high")),
                fmt_num(row.get("yearAgoEps")),
                f"{row['growth'] * 100:+.1f}%" if row.get("growth") == row.get("growth") else "N/A",
                fmt_num(row.get("numberOfAnalysts")),
            ]
            for period, row in earnings.iterrows()
        ]
        sections.append(
            "### EPS consensus\n\n"
            + markdown_table(
                ["Period", "Code", "Mean", "Low", "High", "Year ago", "Growth", "Analysts"], rows
            )
        )

    if revenue is not None:
        rows = [
            [
                _PERIOD_LABELS.get(str(period), str(period)),
                fmt_num(row.get("avg")), fmt_num(row.get("low")), fmt_num(row.get("high")),
                fmt_num(row.get("yearAgoRevenue")),
                f"{row['growth'] * 100:+.1f}%" if row.get("growth") == row.get("growth") else "N/A",
            ]
            for period, row in revenue.iterrows()
        ]
        sections.append(
            "### Revenue consensus\n\n"
            + markdown_table(["Period", "Mean", "Low", "High", "Year ago", "Growth"], rows)
        )

    if trend is not None:
        rows = [
            [
                _PERIOD_LABELS.get(str(period), str(period)),
                fmt_num(row.get("current")), fmt_num(row.get("7daysAgo")),
                fmt_num(row.get("30daysAgo")), fmt_num(row.get("90daysAgo")),
            ]
            for period, row in trend.iterrows()
        ]
        sections.append(
            "### Estimate revisions over time\n\n"
            + markdown_table(["Period", "Current", "7d ago", "30d ago", "90d ago"], rows)
            + "\n\n_Rising estimates into a print are a different setup from falling ones._"
        )

    if revisions is not None:
        rows = [
            [
                _PERIOD_LABELS.get(str(period), str(period)),
                fmt_num(row.get("upLast7days")), fmt_num(row.get("downLast7Days")),
                fmt_num(row.get("upLast30days")), fmt_num(row.get("downLast30days")),
            ]
            for period, row in revisions.iterrows()
        ]
        sections.append(
            "### Analyst revision counts\n\n"
            + markdown_table(["Period", "Up 7d", "Down 7d", "Up 30d", "Down 30d"], rows)
        )

    # Derive the multiples ourselves so the denominator is never ambiguous.
    multiples = ""
    if earnings is not None:
        from .market import close_on

        last_price = price if price is not None else close_on(symbol, curr_date)
        if last_price:
            rows = []
            for period, row in earnings.iterrows():
                mean = row.get("avg")
                if mean is None or mean != mean or mean <= 0:
                    continue
                code = str(period)
                if code in ("0y", "+1y"):
                    basis, annual = f"{_PERIOD_LABELS[code]} consensus", mean
                elif code in ("0q", "+1q"):
                    basis, annual = f"{_PERIOD_LABELS[code]} estimate × 4 (naive annualisation)", mean * 4
                else:
                    continue
                rows.append([basis, fmt_num(annual), f"{last_price / annual:.2f}x"])
            if rows:
                multiples = (
                    f"### Forward multiples at {fmt_num(last_price)}\n\n"
                    + markdown_table(["EPS basis", "Annual EPS", "P/E"], rows)
                    + "\n\n> **Read this before quoting a forward P/E.** Vendors (including the "
                    "`fundamentals` command's `Forward P/E`) conventionally use the **next fiscal "
                    "year** consensus. Annualising a single quarter gives a different, usually much "
                    "higher, number. Both appear above — state which basis you are using, and do not "
                    "treat a mismatch between two bases as evidence of mispricing."
                )

    return (
        f"## Analyst estimates — {symbol} (as of {curr_date})\n\n"
        + "\n\n".join(sections)
        + ("\n\n" + multiples if multiples else "")
        + "\n\n_Source: yfinance consensus. Estimates are a sentiment and expectations "
        "datapoint, not a forecast — the bar the company must clear, not the outcome._"
    )


def earnings_reactions(symbol: str, curr_date: str, limit: int = 12) -> str:
    """How this stock has actually moved on past earnings — the base rate for gap risk.

    Added after a live run in which the risk debate argued at length about sizing
    into a print eight sessions away, and the neutral analyst noted the run
    contained no post-earnings reaction data at all. Both sides then estimated the
    gap magnitude from intuition. This gives them the distribution instead.
    """
    from .market import load_ohlcv

    symbol = normalize_symbol(symbol)
    try:
        dates = _ticker(symbol).get_earnings_dates(limit=limit * 3)
    except Exception as exc:
        return unavailable("yfinance earnings dates", f"{type(exc).__name__}: {exc}")
    if dates is None or dates.empty:
        return unavailable_empty("earnings reactions", f"no earnings history for {symbol}")

    try:
        data = load_ohlcv(symbol, curr_date, 1500)
    except Exception as exc:
        return unavailable("earnings reactions", f"price history failed: {type(exc).__name__}: {exc}")
    if data.empty:
        return unavailable_empty("earnings reactions", f"no price history for {symbol}")

    cutoff = pd.to_datetime(curr_date)
    rows, moves = [], []
    for index in sorted(dates.index, reverse=True):
        when = pd.to_datetime(index).tz_localize(None) if pd.to_datetime(index).tzinfo else pd.to_datetime(index)
        if when > cutoff:
            continue  # not yet reported as of the analysis date

        before = data[data["Date"] < when.normalize()]
        after = data[data["Date"] >= when.normalize()].head(3)
        if before.empty or len(after) < 2:
            continue

        prior_close = float(before["Close"].iloc[-1])
        if not prior_close:
            continue
        # Two sessions covers both a before-open and an after-close release.
        window = after.head(2)
        reaction_close = float(window["Close"].iloc[-1])
        move = (reaction_close - prior_close) / prior_close * 100
        worst = (float(window["Low"].min()) - prior_close) / prior_close * 100
        best = (float(window["High"].max()) - prior_close) / prior_close * 100

        surprise = dates.loc[index].get("Surprise(%)")
        rows.append([
            when.strftime("%Y-%m-%d"),
            fmt_num(prior_close),
            fmt_num(reaction_close),
            f"{move:+.1f}%",
            f"{best:+.1f}%",
            f"{worst:+.1f}%",
            fmt_num(surprise) if surprise == surprise else "N/A",
        ])
        moves.append(move)
        if len(rows) >= limit:
            break

    if not rows:
        return unavailable("earnings reactions", f"no completed earnings events with price data for {symbol}")

    absolute = [abs(m) for m in moves]
    positive = sum(1 for m in moves if m > 0)
    mean_abs = sum(absolute) / len(absolute)
    ordered = sorted(absolute)
    median_abs = ordered[len(ordered) // 2]

    summary = markdown_table(["Statistic", "Value"], [
        ["Events measured", str(len(moves))],
        ["Mean absolute move", f"{mean_abs:.1f}%"],
        ["Median absolute move", f"{median_abs:.1f}%"],
        ["Largest up move", f"{max(moves):+.1f}%"],
        ["Largest down move", f"{min(moves):+.1f}%"],
        ["Positive reactions", f"{positive}/{len(moves)} ({positive / len(moves) * 100:.0f}%)"],
    ])

    return f"""## Earnings reactions — {symbol} (base rate for gap risk)

Move measured from the close before the release to the close two sessions after,
so it captures both before-open and after-close announcements. Best/worst are the
intraday extremes over that window — that is the range a stop actually faces.

{markdown_table(["Report date", "Prior close", "Close +2", "Move", "Best", "Worst"] + ["Surprise %"], rows)}

### Distribution

{summary}

> Size a position held through a print against the **mean absolute move**, not
> against a directional view. Note also that a stop inside this range will be
> taken out by a routine reaction rather than by a broken thesis.
"""


def earnings_calendar(symbol: str, curr_date: str) -> str:
    """Upcoming and recent earnings dates — the single most common catalyst."""
    symbol = normalize_symbol(symbol)
    try:
        dates = _ticker(symbol).get_earnings_dates(limit=12)
    except Exception as exc:
        return unavailable("yfinance earnings dates", f"{type(exc).__name__}: {exc}")
    if dates is None or dates.empty:
        return unavailable_empty("yfinance earnings dates", f"no earnings calendar for {symbol}")

    rows = []
    for index, row in dates.iterrows():
        when = pd.to_datetime(index)
        marker = "upcoming" if when.tz_localize(None) > parse_date(curr_date) else "reported"
        rows.append([
            when.strftime("%Y-%m-%d"),
            marker,
            fmt_num(row.get("EPS Estimate")),
            fmt_num(row.get("Reported EPS")),
            fmt_num(row.get("Surprise(%)")),
        ])
    table = markdown_table(
        ["Date", "Status", "EPS estimate", "Reported EPS", "Surprise %"], rows
    )
    return f"## Earnings calendar — {symbol}\n\n{table}"
