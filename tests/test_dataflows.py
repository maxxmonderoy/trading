"""Offline tests for the data layer's decision logic.

Everything here runs with no network and no API keys. The vendor calls are not
under test — their *shapes* change without notice and a test that pins them
would fail for the wrong reason. What is under test is the reasoning this
project layers on top of them: whether a missing source is reported as missing,
whether a look-ahead guard actually truncates, whether a risk cap can be walked
around, and whether an outcome verdict matches the rule it claims to apply.

    .venv/bin/python -m unittest discover tests -v
    .venv/bin/python tests/test_dataflows.py

pytest runs these too, but it is deliberately absent from requirements.txt: a
fresh clone of this project should need four packages and no API keys.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from dataflows import common, memory, paper  # noqa: E402


# ---------------------------------------------------------------------------
# Rule 2: a source that failed is not a source that returned nothing
# ---------------------------------------------------------------------------


class FetchFailureIsNotAbsence(unittest.TestCase):
    """The distinction the whole no-fabrication guarantee rests on.

    yfinance catches transport errors internally and hands back an empty frame,
    so without these checks an outage reads to an analyst as "this ticker has no
    data" — a finding it will happily reason from.
    """

    def test_transport_noise_raises(self):
        noise = (
            "Failed to get ticker 'SPY' reason: Failed to perform, curl: (7) "
            "CONNECT tunnel failed, response 403."
        )
        with self.assertRaises(common.FetchError) as caught:
            common.raise_if_fetch_failed(noise, "yfinance OHLCV")
        self.assertIn("fetch failed", str(caught.exception))

    def test_delisting_notice_passes_through(self):
        """A completed request whose answer is "nothing here" is a real answer."""
        common.raise_if_fetch_failed(
            "ZZZZ: possibly delisted; no price data found", "yfinance OHLCV"
        )

    def test_silence_passes_through(self):
        common.raise_if_fetch_failed("", "yfinance OHLCV")

    def test_rate_limit_is_a_fetch_failure(self):
        with self.assertRaises(common.FetchError):
            common.raise_if_fetch_failed("YFRateLimitError: Too Many Requests", "yfinance")

    def test_empty_result_upgraded_when_vendor_unreachable(self):
        with mock.patch.object(common, "source_reachable", return_value=False):
            message = common.unavailable_empty("yfinance fundamentals", "no data for AAPL")
        self.assertIn("unreachable", message)
        self.assertNotIn("no data for AAPL", message)

    def test_empty_result_kept_when_vendor_is_up(self):
        with mock.patch.object(common, "source_reachable", return_value=True):
            message = common.unavailable_empty("yfinance fundamentals", "no data for ZZZZ")
        self.assertIn("no data for ZZZZ", message)

    def test_unavailable_shape_is_stable(self):
        """Agents are told to surface this verbatim, so the shape is a contract."""
        self.assertEqual(
            common.unavailable("src", "why"), "<unavailable: src — why>"
        )


# ---------------------------------------------------------------------------
# Rule 4: no look-ahead
# ---------------------------------------------------------------------------


class NoLookAhead(unittest.TestCase):
    def test_future_dates_are_rejected_by_the_cli(self):
        self.assertTrue(common.is_future("2999-01-01"))
        self.assertFalse(common.is_future("2020-01-01"))

    def test_ohlcv_truncates_at_the_analysis_date(self):
        import pandas as pd

        from dataflows import market

        frame = pd.DataFrame({
            "Date": pd.to_datetime(
                ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
            ),
            "Open": [1.0, 2.0, 3.0, 4.0],
            "High": [1.0, 2.0, 3.0, 4.0],
            "Low": [1.0, 2.0, 3.0, 4.0],
            "Close": [1.0, 2.0, 3.0, 4.0],
            "Volume": [10, 20, 30, 40],
        })
        with mock.patch.object(market, "_download", return_value=frame), \
                tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(common, "CACHE_DIR", Path(tmp)):
            visible = market.load_ohlcv("TEST", "2024-01-03", 30)

        self.assertEqual(len(visible), 2)
        self.assertEqual(visible["Date"].iloc[-1].strftime("%Y-%m-%d"), "2024-01-03")


# ---------------------------------------------------------------------------
# Symbols and benchmarks
# ---------------------------------------------------------------------------


class Symbols(unittest.TestCase):
    def test_normalize_strips_what_users_type(self):
        for raw in (" nvda ", "$NVDA", "nvda", "NVDA"):
            self.assertEqual(common.normalize_symbol(raw), "NVDA")

    def test_empty_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            common.normalize_symbol("  ")

    def test_crypto_is_benchmarked_against_bitcoin_not_spy(self):
        self.assertEqual(common.benchmark_for("ETH-USD", "SPY"), "BTC-USD")

    def test_regional_listings_get_a_local_index(self):
        self.assertEqual(common.benchmark_for("7203.T", "SPY"), "^N225")
        self.assertEqual(common.benchmark_for("BP.L", "SPY"), "^FTSE")

    def test_plain_us_listing_uses_the_configured_benchmark(self):
        self.assertEqual(common.benchmark_for("NVDA", "QQQ"), "QQQ")


# ---------------------------------------------------------------------------
# Paper book: costs and caps
# ---------------------------------------------------------------------------


class PaperExecution(unittest.TestCase):
    """Rule 7: fills are simulated and their costs are real."""

    def test_slippage_always_works_against_you(self):
        self.assertGreater(paper._fill_price(100.0, "buy", 5.0), 100.0)
        self.assertLess(paper._fill_price(100.0, "sell", 5.0), 100.0)

    def test_equities_are_whole_shares_crypto_is_fractional(self):
        self.assertEqual(paper._round_shares("NVDA", 10.9), 10.0)
        self.assertEqual(paper._round_shares("BTC-USD", 0.123456789), 0.123457)


class PaperPositionCap(unittest.TestCase):
    """The cap is on the resulting position, not on the order.

    Checking only the incoming order let two 20% buys build a 40% position, and
    let `--shares` bypass the limit outright.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(paper, "PAPER_DIR", base),
            mock.patch.object(paper, "PORTFOLIO_PATH", base / "portfolio.json"),
            mock.patch.object(paper, "TRADES_PATH", base / "trades.jsonl"),
            mock.patch.object(paper, "EQUITY_PATH", base / "equity.jsonl"),
            mock.patch.object(paper, "_price", return_value=100.0),
        ]
        for patch in self._patches:
            patch.start()
        paper.init(cash=100_000.0, force=True)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_a_single_oversized_order_is_refused(self):
        self.assertIn("max position size", paper.buy("NVDA", "2024-01-02", size_pct=25))

    def test_adds_cannot_walk_past_the_cap(self):
        self.assertIn("BOUGHT", paper.buy("NVDA", "2024-01-02", size_pct=15))
        second = paper.buy("NVDA", "2024-01-03", size_pct=15)
        self.assertIn("max position size", second)
        # The refusal names the resulting weight, not the order's, so the reason
        # is legible: neither leg breached 20% on its own.
        self.assertIn("shares already held", second)

    def test_explicit_share_counts_are_capped_too(self):
        self.assertIn("max position size", paper.buy("NVDA", "2024-01-02", shares=500))

    def test_an_add_within_the_cap_still_fills(self):
        paper.buy("NVDA", "2024-01-02", size_pct=10)
        self.assertIn("BOUGHT", paper.buy("NVDA", "2024-01-03", size_pct=5))

    def test_costs_are_recorded_on_every_fill(self):
        paper.buy("NVDA", "2024-01-02", size_pct=10)
        book = paper._load()
        self.assertGreater(book["total_costs"], 0)

    def test_status_uses_the_configured_benchmark(self):
        """A book must be graded against the benchmark in config.json, not a constant."""
        with mock.patch.object(paper, "load_config", return_value={"benchmark_ticker": "QQQ"}), \
                mock.patch.object(paper, "_benchmark_comparison") as comparison:
            comparison.return_value = ""
            paper.status("2024-01-02")
        self.assertEqual(comparison.call_args.args[3], "QQQ")


# ---------------------------------------------------------------------------
# Memory: scoring and recall
# ---------------------------------------------------------------------------


class OutcomeVerdicts(unittest.TestCase):
    """Alpha, not raw return, is the grade — and Hold is graded on being flat."""

    def test_bullish_calls_are_graded_on_positive_alpha(self):
        self.assertEqual(memory._directional_verdict("Buy", 5.0), "correct")
        self.assertEqual(memory._directional_verdict("Buy", -5.0), "wrong")
        self.assertEqual(memory._directional_verdict("Overweight", 0.5), "neutral")

    def test_bearish_calls_invert(self):
        self.assertEqual(memory._directional_verdict("Sell", -5.0), "correct")
        self.assertEqual(memory._directional_verdict("Underweight", 5.0), "wrong")

    def test_hold_is_graded_on_staying_flat(self):
        self.assertEqual(memory._directional_verdict("Hold", 1.0), "correct")
        self.assertEqual(memory._directional_verdict("Hold", 9.0), "wrong")


class MemoryRecall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(memory, "MEMORY_DIR", base),
            mock.patch.object(memory, "LOG_PATH", base / "decisions.jsonl"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def _write(self, records):
        memory.LOG_PATH.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def test_cold_start_says_so_rather_than_returning_nothing(self):
        message = memory.recall("NVDA", "momentum")
        self.assertIn("cold start", message)

    def test_only_decisions_with_lessons_are_recalled(self):
        self._write([{
            "id": "a", "ticker": "NVDA", "date": "2024-01-02", "rating": "Buy",
            "situation": "momentum breakout", "lesson": None, "scored": False,
        }])
        self.assertIn("cold start", memory.recall("NVDA", "momentum"))

    def test_same_ticker_priors_surface_despite_no_text_overlap(self):
        """The ticker boost exists so a prior on the same name is never dropped.

        It is a boost, not an override: a much closer text match can still rank
        above it. What must not happen is the same-name prior falling off the
        list entirely because the wording differed.
        """
        self._write([
            {"id": "a", "ticker": "AMD", "date": "2024-01-02", "rating": "Buy",
             "situation": "momentum breakout above the range", "lesson": "amd lesson",
             "scored": True, "outcome": {"alpha_pct": 1.0, "verdict": "correct"}},
            {"id": "b", "ticker": "NVDA", "date": "2024-01-02", "rating": "Buy",
             "situation": "quiet consolidation", "lesson": "nvda lesson",
             "scored": True, "outcome": {"alpha_pct": 2.0, "verdict": "correct"}},
        ])
        out = memory.recall("NVDA", "momentum breakout above the range")
        self.assertIn("nvda lesson", out)

    def test_an_unrelated_ticker_is_still_ranked_by_text(self):
        self._write([
            {"id": "a", "ticker": "AMD", "date": "2024-01-02", "rating": "Buy",
             "situation": "momentum breakout above the range", "lesson": "amd lesson",
             "scored": True, "outcome": {"alpha_pct": 1.0, "verdict": "correct"}},
            {"id": "b", "ticker": "INTC", "date": "2024-01-02", "rating": "Sell",
             "situation": "dividend cut and guidance reset", "lesson": "intc lesson",
             "scored": True, "outcome": {"alpha_pct": -2.0, "verdict": "correct"}},
        ])
        out = memory.recall("", "momentum breakout above the range")
        # A lesson with no overlap at all is withheld rather than padded in —
        # an irrelevant prior is worse than none, since agents treat recalled
        # lessons as applicable to the setup in front of them.
        self.assertIn("amd lesson", out)
        self.assertNotIn("intc lesson", out)

    def test_log_then_lesson_round_trips(self):
        record_id = memory.log_decision(
            ticker="nvda", date="2024-01-02", rating="Buy", action="BUY",
            situation="breakout on volume",
        )
        memory.add_lesson(record_id, "sized too large into earnings")
        stored = json.loads(memory.show(record_id))
        self.assertEqual(stored["ticker"], "NVDA")
        self.assertEqual(stored["lesson"], "sized too large into earnings")

    def test_a_corrupt_line_does_not_lose_the_whole_log(self):
        memory.LOG_PATH.write_text(
            '{"id":"a","ticker":"NVDA","date":"2024-01-02","rating":"Buy","scored":false}\n'
            "{ this is not json\n"
            '{"id":"b","ticker":"AMD","date":"2024-01-03","rating":"Sell","scored":false}\n'
        )
        self.assertEqual(len(memory._load()), 2)


# ---------------------------------------------------------------------------
# The EV gate
# ---------------------------------------------------------------------------


def _setup(outcome: str, rr: float = 2.0, risk_points: float = 20.0):
    """A Setup carrying only the fields the EV calculation reads."""
    from dataflows.smc import Setup

    return Setup(
        session_date="2024-01-02", direction="long", kill_zone="NY",
        liquidity_level_name="pd_low", liquidity_level_price=0.0,
        sweep_time="09:40", sweep_extreme=0.0, sweep_return_bars=2,
        mss_time="09:50", mss_level=0.0, mss_close=0.0,
        fvg_time="09:55", fvg_top=0.0, fvg_bottom=0.0,
        entry=0.0, stop=0.0, target=0.0, target_name="pd_high",
        risk_points=risk_points, reward_points=risk_points * rr, rr=rr,
        risk_ticks=risk_points * 4,
        outcome=outcome,
    )


class ExpectedValueGate(unittest.TestCase):
    """A proposal must clear a measured, cost-inclusive edge — not a narrative one."""

    def setUp(self):
        from dataflows.futures import resolve

        self.spec = resolve("MNQ")
        self.config = {"ev_gate": {
            "min_ev_r": 0.25, "min_sample": 20,
            "adverse_ticks_entry": 2.0, "adverse_ticks_stop": 2.0,
            "ambiguous_as_loss": True,
        }}

    def _ev(self, setups):
        from dataflows.smc import expected_value

        return expected_value(setups, self.spec, self.config)

    def test_below_the_sample_floor_the_gate_fails_closed(self):
        """An unmeasured edge is not a passing one — this is the default state."""
        ev = self._ev([_setup("target")] * 8 + [_setup("stopped")] * 2)
        self.assertFalse(ev["measurable"])
        self.assertFalse(ev["passes"])
        self.assertIsNone(ev["net_ev_r"])
        self.assertIn("NOT MEASURABLE", ev["verdict"])

    def test_a_genuine_edge_passes(self):
        ev = self._ev([_setup("target", rr=3.0)] * 12 + [_setup("stopped")] * 12)
        self.assertTrue(ev["measurable"])
        self.assertTrue(ev["passes"])
        # 0.5 x 3R - 0.5 x 1R = +1.00R gross, less a small drag.
        self.assertAlmostEqual(ev["gross_ev_r"], 1.0, places=6)
        self.assertLess(ev["net_ev_r"], ev["gross_ev_r"])

    def test_a_coin_flip_at_2r_fails_once_costs_are_charged(self):
        """The case the gate exists for: positive gross, negative after drag."""
        wins, losses = 8, 22
        ev = self._ev([_setup("target", rr=2.0)] * wins + [_setup("stopped")] * losses)
        self.assertTrue(ev["measurable"])
        self.assertLess(ev["gross_ev_r"], 0.25)
        self.assertFalse(ev["passes"])

    def test_costs_are_charged_and_always_reduce_ev(self):
        setups = [_setup("target", rr=2.5)] * 15 + [_setup("stopped")] * 15
        ev = self._ev(setups)
        self.assertGreater(ev["mean_cost_r"], 0)
        self.assertAlmostEqual(
            ev["net_ev_r"], ev["gross_ev_r"] - ev["mean_cost_r"], places=9
        )

    def test_a_tighter_stop_carries_more_cost_per_r(self):
        """Fixed dollar costs are a bigger fraction of a small risk budget."""
        tight = self._ev([_setup("target", rr=2.0, risk_points=5.0)] * 15
                         + [_setup("stopped", risk_points=5.0)] * 15)
        wide = self._ev([_setup("target", rr=2.0, risk_points=50.0)] * 15
                        + [_setup("stopped", risk_points=50.0)] * 15)
        self.assertGreater(tight["mean_cost_r"], wide["mean_cost_r"])

    def test_ambiguous_bars_count_against_the_strategy(self):
        """OHLC cannot order a bar that spans both; assuming the win is how
        backtests flatter themselves."""
        setups = [_setup("target", rr=2.0)] * 15 + [_setup("stopped")] * 10 + [_setup("ambiguous")] * 5
        strict = self._ev(setups)
        self.assertEqual(strict["n_losses"], 15)
        self.assertEqual(strict["n_ambiguous"], 5)

        lenient_config = {"ev_gate": dict(self.config["ev_gate"], ambiguous_as_loss=False)}
        from dataflows.smc import expected_value

        lenient = expected_value(setups, self.spec, lenient_config)
        self.assertEqual(lenient["n_losses"], 10)
        self.assertGreater(lenient["net_ev_r"], strict["net_ev_r"])

    def test_unfilled_and_open_setups_are_not_counted_as_outcomes(self):
        setups = ([_setup("target", rr=2.0)] * 12 + [_setup("stopped")] * 12
                  + [_setup("unfilled")] * 30 + [_setup("open_at_end")] * 10)
        ev = self._ev(setups)
        self.assertEqual(ev["n_resolved"], 24)
        self.assertEqual(ev["n_unfilled"], 30)
        self.assertEqual(ev["n_open"], 10)

    def test_the_floor_is_read_from_config(self):
        setups = [_setup("target", rr=2.0)] * 15 + [_setup("stopped")] * 15
        strict = {"ev_gate": dict(self.config["ev_gate"], min_ev_r=0.90)}
        from dataflows.smc import expected_value

        self.assertFalse(expected_value(setups, self.spec, strict)["passes"])
        self.assertTrue(self._ev(setups)["passes"])


class ScaledExits(unittest.TestCase):
    """Partial take-profit, and the continuous outcomes it produces.

    Banking half at 1R and stopping the runner at breakeven returns about
    +0.5R — neither a win nor a loss. That is the whole reason `realized_r`
    exists, and why EV is measured as its mean rather than decomposed into a
    win rate and an average winner.
    """

    def setUp(self):
        from dataflows import smc
        from dataflows.futures import resolve

        self.smc = smc
        self.spec = resolve("MNQ")
        self.config = {"scaling": {
            "enabled": True, "tp1_r": 1.0, "tp1_fraction": 0.5,
            "breakeven_offset_ticks": 1.0, "min_rr": 1.2,
        }}

    def _frame(self, bars):
        """bars: list of (high, low). Index is irrelevant to the resolver."""
        return pd.DataFrame(
            {"High": [b[0] for b in bars], "Low": [b[1] for b in bars]},
            index=pd.date_range("2024-01-02 09:30", periods=len(bars), freq="5min",
                                tz="America/New_York"),
        )

    def _long(self, rr=2.0):
        setup = _setup("unresolved", rr=rr, risk_points=10.0)
        setup.direction = "long"
        setup.entry, setup.stop = 100.0, 90.0
        setup.target = 100.0 + rr * 10.0
        setup.outcome = "unresolved"
        return setup

    def test_tp1_then_breakeven_is_a_partial_win(self):
        setup = self._long()
        # fill at 100, run to 110 (1R), come back and take the BE stop.
        frame = self._frame([(101, 99), (111, 105), (106, 95)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, self.config)
        self.assertEqual(setup.outcome, "scaled_breakeven")
        self.assertTrue(setup.tp1_hit)
        # 0.5 x 1R banked, runner out at breakeven + 1 tick.
        self.assertAlmostEqual(setup.realized_r, 0.5 * 1.0 + 0.5 * (0.25 / 10.0), places=6)

    def test_stopped_before_tp1_is_a_full_loss(self):
        setup = self._long()
        frame = self._frame([(101, 99), (100, 89)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, self.config)
        self.assertEqual(setup.outcome, "stopped")
        self.assertFalse(setup.tp1_hit)
        self.assertEqual(setup.realized_r, -1.0)

    def test_running_to_target_blends_both_exits(self):
        setup = self._long(rr=2.0)
        frame = self._frame([(101, 99), (111, 105), (121, 112)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, self.config)
        self.assertEqual(setup.outcome, "target")
        self.assertAlmostEqual(setup.realized_r, 0.5 * 1.0 + 0.5 * 2.0, places=6)

    def test_a_bar_spanning_stop_and_tp1_stays_ambiguous(self):
        setup = self._long()
        frame = self._frame([(101, 99), (111, 89)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, self.config)
        self.assertEqual(setup.outcome, "ambiguous")

    def test_shorts_resolve_symmetrically(self):
        setup = self._long()
        setup.direction, setup.entry, setup.stop = "short", 100.0, 110.0
        setup.target = 80.0
        frame = self._frame([(101, 99), (95, 89), (105, 94)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, self.config)
        self.assertEqual(setup.outcome, "scaled_breakeven")
        self.assertGreater(setup.realized_r, 0)

    def test_disabling_scaling_restores_binary_outcomes(self):
        setup = self._long()
        off = {"scaling": dict(self.config["scaling"], enabled=False)}
        frame = self._frame([(101, 99), (111, 105), (106, 95)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, off)
        self.assertFalse(setup.tp1_hit)
        self.assertNotEqual(setup.outcome, "scaled_breakeven")

    def test_the_fill_bar_never_credits_a_favourable_exit(self):
        """A bar that both fills the limit and runs 1R cannot be ordered from OHLC.

        Crediting it is the same optimistic assumption the scanner refuses to
        make for stop-versus-target, and on a 365-session sample it was worth
        roughly 0.15R per trade.
        """
        setup = self._long()
        # One bar dips to the 100 limit AND reaches the 110 TP1.
        frame = self._frame([(111, 99), (104, 102)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, self.config)
        self.assertFalse(setup.tp1_hit)

    def test_the_fill_bar_still_charges_the_stop(self):
        """Deliberately asymmetric: adverse events count from the fill bar."""
        setup = self._long()
        frame = self._frame([(101, 89)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, self.config)
        self.assertEqual(setup.outcome, "stopped")
        self.assertEqual(setup.realized_r, -1.0)

    def test_a_later_bar_credits_tp1_normally(self):
        setup = self._long()
        frame = self._frame([(101, 99), (111, 105), (106, 95)])
        self.smc._resolve_outcome(setup, frame, -1, 0.25, self.config)
        self.assertTrue(setup.tp1_hit)

    def test_the_floor_can_never_sit_below_tp1(self):
        """TP1 beyond the runner's own target would be incoherent."""
        settings = self.smc.scaling_settings(
            {"scaling": {"enabled": True, "tp1_r": 1.5, "min_rr": 1.0}}
        )
        self.assertGreaterEqual(settings["min_rr"], settings["tp1_r"])

    def test_ev_measures_mean_realized_r(self):
        from types import SimpleNamespace

        setups = [
            SimpleNamespace(outcome="scaled_breakeven", rr=2.0, risk_points=20.0, realized_r=0.5)
            for _ in range(20)
        ]
        ev = self.smc.expected_value(setups, self.spec, None)
        self.assertTrue(ev["measurable"])
        self.assertAlmostEqual(ev["gross_ev_r"], 0.5, places=6)

    def test_partial_wins_are_not_counted_as_full_wins(self):
        from types import SimpleNamespace

        partial = [
            SimpleNamespace(outcome="scaled_breakeven", rr=3.0, risk_points=20.0, realized_r=0.5)
            for _ in range(20)
        ]
        full = [
            SimpleNamespace(outcome="target", rr=3.0, risk_points=20.0, realized_r=3.0)
            for _ in range(20)
        ]
        self.assertLess(
            self.smc.expected_value(partial, self.spec, None)["gross_ev_r"],
            self.smc.expected_value(full, self.spec, None)["gross_ev_r"],
        )


class SetupJournal(unittest.TestCase):
    """Accumulating a sample across runs, without pooling different strategies.

    One scan can never reach the EV floor: the vendor serves 30 days of 5m bars
    and the window slides forward as fast as a sample would grow. The journal is
    the only route to a measurable edge that does not involve risking money.
    """

    def setUp(self):
        from dataflows import smc

        self.smc = smc
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(smc, "MEMORY_DIR", base),
            mock.patch.object(smc, "JOURNAL_PATH", base / "smc_setups.jsonl"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def _write(self, rows):
        self.smc.JOURNAL_PATH.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def _row(self, key, outcome="target", rr=2.0, fingerprint="aaaa111111", symbol="NQ"):
        return {
            "key": key, "symbol": symbol, "interval": "5m", "fingerprint": fingerprint,
            "session_date": "2024-01-02", "direction": "long", "kill_zone": "NY",
            "liquidity_level": "pd_low", "entry": 1.0, "stop": 0.0, "target": 2.0,
            "risk_points": 20.0, "rr": rr, "outcome": outcome, "outcome_time": "10:00",
            "recorded_at": "2024-01-02T10:00:00",
        }

    def test_a_setup_has_a_stable_key_across_rescans(self):
        setup = _setup("target")
        first = self.smc.journal_key("NQ", "5m", setup)
        second = self.smc.journal_key("NQ", "5m", setup)
        self.assertEqual(first, second)

    def test_different_setups_get_different_keys(self):
        a = self.smc.journal_key("NQ", "5m", _setup("target"))
        b = _setup("target")
        b.mss_time = "11:30"
        self.assertNotEqual(a, self.smc.journal_key("NQ", "5m", b))

    def test_changing_a_parameter_changes_the_fingerprint(self):
        base = {"swing_lookback": 2, "min_rr": 2.0, "entry_mode": "ce"}
        moved = dict(base, swing_lookback=3)
        self.assertNotEqual(
            self.smc.parameter_fingerprint(base, "5m"),
            self.smc.parameter_fingerprint(moved, "5m"),
        )

    def test_interval_is_part_of_the_fingerprint(self):
        config = {"swing_lookback": 2, "min_rr": 2.0}
        self.assertNotEqual(
            self.smc.parameter_fingerprint(config, "5m"),
            self.smc.parameter_fingerprint(config, "15m"),
        )

    def test_journal_filters_by_symbol_and_fingerprint(self):
        self._write([
            self._row("a", fingerprint="aaaa111111"),
            self._row("b", fingerprint="bbbb222222"),
            self._row("c", symbol="ES", fingerprint="aaaa111111"),
        ])
        self.assertEqual(len(self.smc.load_journal()), 3)
        self.assertEqual(len(self.smc.load_journal("NQ")), 2)
        self.assertEqual(len(self.smc.load_journal("NQ", "aaaa111111")), 1)

    def test_setups_from_different_parameters_are_never_pooled(self):
        """Rule 6: a parameterised result is not a measurement."""
        self._write(
            [self._row(f"old{i}", fingerprint="oldddddddd") for i in range(40)]
            + [self._row(f"new{i}", fingerprint="newwwwwwww") for i in range(3)]
        )
        current = self.smc.load_journal("NQ", "newwwwwwww")
        self.assertEqual(len(current), 3)

    def test_a_corrupt_line_loses_one_trade_not_the_record(self):
        self.smc.JOURNAL_PATH.write_text(
            json.dumps(self._row("a")) + "\n{ broken\n" + json.dumps(self._row("b")) + "\n"
        )
        self.assertEqual(len(self.smc.load_journal()), 2)

    def test_ev_reads_the_journal_population(self):
        from dataflows.futures import resolve

        self._write(
            [self._row(f"w{i}", outcome="target", rr=3.0) for i in range(12)]
            + [self._row(f"l{i}", outcome="stopped") for i in range(12)]
        )
        rows = self.smc.load_journal("NQ", "aaaa111111")
        ev = self.smc.expected_value(
            self.smc._journal_rows_as_setups(rows), resolve("NQ"), None
        )
        self.assertEqual(ev["n_resolved"], 24)
        self.assertTrue(ev["measurable"])

    def test_empty_journal_reports_how_to_start_one(self):
        self.assertIn("smc journal record", self.smc.journal_status())


class BackfillLoader(unittest.TestCase):
    """Reading a local history file — the route to a sample that does not take years.

    The timezone branch is the dangerous part: this framework tags sessions on
    the 18:00 ET Globex boundary, so a file read an hour off silently reassigns
    bars to the wrong trade date and corrupts every overnight level.
    """

    def setUp(self):
        from dataflows import smc

        self.smc = smc
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _csv(self, text, name="bars.csv"):
        path = self.dir / name
        path.write_text(text)
        return str(path)

    def test_naive_timestamps_are_localised_to_the_given_zone(self):
        path = self._csv(
            "timestamp,open,high,low,close,volume\n"
            "2024-01-02 09:30:00,100,101,99,100.5,10\n"
            "2024-01-02 09:35:00,100.5,102,100,101,12\n"
        )
        frame = self.smc.load_ohlcv_csv(path, "America/New_York")
        self.assertEqual(str(frame.index[0]), "2024-01-02 09:30:00-05:00")

    def test_offset_aware_timestamps_survive_a_dst_change(self):
        """Two offsets in one file is what breaks a naive parse."""
        path = self._csv(
            "timestamp,open,high,low,close\n"
            "2024-01-02 09:30:00-05:00,100,101,99,100.5\n"
            "2024-07-02 09:30:00-04:00,100.5,102,100,101\n"
        )
        frame = self.smc.load_ohlcv_csv(path)
        self.assertEqual(len(frame), 2)
        self.assertEqual([t.hour for t in frame.index], [9, 9])

    def test_column_aliases_are_accepted(self):
        path = self._csv(
            "Date,O,H,L,C,Vol\n2024-01-02 09:30:00,100,101,99,100.5,10\n"
        )
        frame = self.smc.load_ohlcv_csv(path, "America/New_York")
        self.assertEqual(list(frame.columns), ["Open", "High", "Low", "Close", "Volume"])

    def test_missing_volume_defaults_rather_than_failing(self):
        path = self._csv(
            "timestamp,open,high,low,close\n2024-01-02 09:30:00,100,101,99,100.5\n"
        )
        self.assertEqual(self.smc.load_ohlcv_csv(path, "America/New_York")["Volume"].iloc[0], 0.0)

    def test_a_missing_price_column_is_a_hard_error(self):
        path = self._csv("timestamp,open,high,close\n2024-01-02 09:30:00,100,101,100.5\n")
        with self.assertRaises(ValueError) as caught:
            self.smc.load_ohlcv_csv(path, "America/New_York")
        self.assertIn("Low", str(caught.exception))

    def test_a_missing_timestamp_column_names_what_it_looked_for(self):
        path = self._csv("open,high,low,close\n100,101,99,100.5\n")
        with self.assertRaises(ValueError) as caught:
            self.smc.load_ohlcv_csv(path, "America/New_York")
        self.assertIn("timestamp", str(caught.exception))

    def test_duplicate_timestamps_are_collapsed(self):
        path = self._csv(
            "timestamp,open,high,low,close\n"
            "2024-01-02 09:30:00,100,101,99,100.5\n"
            "2024-01-02 09:30:00,100,101,99,100.5\n"
        )
        self.assertEqual(len(self.smc.load_ohlcv_csv(path, "America/New_York")), 1)

    def test_rows_are_sorted_even_when_the_file_is_not(self):
        path = self._csv(
            "timestamp,open,high,low,close\n"
            "2024-01-02 09:35:00,100.5,102,100,101\n"
            "2024-01-02 09:30:00,100,101,99,100.5\n"
        )
        frame = self.smc.load_ohlcv_csv(path, "America/New_York")
        self.assertTrue(frame.index.is_monotonic_increasing)


class CryptoSessionModel(unittest.TestCase):
    """A 24/7 market measured with a session-based framework.

    The session concepts are redefined rather than borrowed. Faking an RTH block
    or an overnight range on a continuous tape would dress an arbitrary slice up
    as market structure, which is the one thing this framework must not do.
    """

    def setUp(self):
        from dataflows import futures

        self.futures = futures
        self.btc = futures.resolve("BTCUSDT")
        self.nq = futures.resolve("NQ")

    def test_crypto_specs_resolve(self):
        self.assertTrue(self.btc.is_crypto)
        self.assertFalse(self.nq.is_crypto)
        self.assertEqual(self.btc.session_model, "utc_day")

    def test_sessions_cut_on_the_utc_day_not_the_globex_open(self):
        evening = pd.Timestamp("2024-01-02 20:00", tz="America/New_York")
        # Globex: an 8pm ET bar belongs to the NEXT trade date.
        self.assertEqual(
            self.futures._session_date(evening, "globex").date().isoformat(), "2024-01-03"
        )
        # UTC day: 20:00 ET is 01:00 UTC on the 3rd, so the same bar is the 3rd.
        self.assertEqual(
            self.futures._session_date(evening, "utc_day").date().isoformat(), "2024-01-03"
        )
        # And a bar that is mid-afternoon ET is still the same UTC day.
        noon = pd.Timestamp("2024-01-02 12:00", tz="America/New_York")
        self.assertEqual(
            self.futures._session_date(noon, "utc_day").date().isoformat(), "2024-01-02"
        )

    def test_every_crypto_bar_is_in_session(self):
        frame = pd.DataFrame(
            {"Open": [1.0] * 6, "High": [1.0] * 6, "Low": [1.0] * 6, "Close": [1.0] * 6},
            index=pd.date_range("2024-01-02 00:00", periods=6, freq="4h", tz="UTC"),
        )
        tagged = self.futures._tag_sessions(frame, self.btc)
        self.assertTrue(tagged["is_rth"].all())

    def test_futures_still_separate_rth_from_overnight(self):
        frame = pd.DataFrame(
            {"Open": [1.0] * 6, "High": [1.0] * 6, "Low": [1.0] * 6, "Close": [1.0] * 6},
            index=pd.date_range("2024-01-02 00:00", periods=6, freq="4h",
                                tz="America/New_York"),
        )
        tagged = self.futures._tag_sessions(frame, self.nq)
        self.assertFalse(tagged["is_rth"].all())

    def test_notional_fees_scale_with_price(self):
        """A percentage fee is a different number at $30k than at $90k."""
        self.assertLess(
            self.btc.round_trip_cost(30_000), self.btc.round_trip_cost(90_000)
        )

    def test_flat_commission_products_ignore_price(self):
        self.assertEqual(self.nq.round_trip_cost(18_000), self.nq.round_trip_cost(1))

    def test_omitting_price_omits_the_notional_component(self):
        """Better to understate than to invent a price to charge against."""
        self.assertLess(self.btc.round_trip_cost(), self.btc.round_trip_cost(42_000))

    def test_overnight_levels_are_dropped_for_crypto(self):
        from dataflows import smc

        frame = pd.DataFrame(
            {"Open": [100.0] * 576, "High": [101.0] * 576,
             "Low": [99.0] * 576, "Close": [100.0] * 576},
            index=pd.date_range("2024-01-01", periods=576, freq="5min", tz="UTC"),
        )
        tagged = self.futures._tag_sessions(frame, self.btc)
        session = sorted(tagged["session_date"].unique())[-1]
        targets = smc._liquidity_targets(tagged, session, spec=self.btc)
        self.assertNotIn("ONH", targets)
        self.assertNotIn("ONL", targets)


class BinanceLoader(unittest.TestCase):
    """Parsing the bulk archives. The network half is not under test."""

    def setUp(self):
        from dataflows import crypto

        self.crypto = crypto

    def test_epoch_units_are_inferred_not_assumed(self):
        """Binance switched some archives from ms to µs; guessing puts bars in 1970."""
        ms = pd.Series([1704067200000])
        us = pd.Series([1704067200000000])
        self.assertEqual(self.crypto._to_utc(ms).iloc[0].year, 2024)
        self.assertEqual(self.crypto._to_utc(us).iloc[0].year, 2024)

    def test_mixed_epoch_units_in_one_download(self):
        """A multi-month pull can straddle Binance's ms-to-us switch.

        Inferring one unit for the whole concatenation put the archives on the
        far side of it in the year 58000.
        """
        mixed = pd.Series([1704067200000, 1704067200000000, 1706745600000])
        out = self.crypto._to_utc(mixed)
        self.assertTrue((out.dt.year == 2024).all(), out.tolist())

    def test_months_back_excludes_the_current_month(self):
        """Binance publishes a month's archive only after it closes."""
        months = self.crypto._months_back(3)
        self.assertEqual(len(months), 3)
        self.assertNotIn(date.today().strftime("%Y-%m"), months)
        self.assertEqual(months, sorted(months))

    def test_archive_url_shape(self):
        url = self.crypto._archive_url("BTCUSDT", "5m", "2024-01", "futures")
        self.assertIn("futures/um/monthly/klines/BTCUSDT/5m/", url)
        self.assertTrue(url.endswith("BTCUSDT-5m-2024-01.zip"))
        spot = self.crypto._archive_url("BTCUSDT", "5m", "2024-01", "spot")
        self.assertIn("/spot/monthly/", spot)

    def test_headerless_archives_are_parsed(self):
        import io as _io
        import zipfile

        row = "1704067200000,42000.0,42100.0,41900.0,42050.0,1.5,1704067499999,63000,10,0.7,29000,0\n"
        buffer = _io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("BTCUSDT-5m-2024-01.csv", row)
        frame = self.crypto._parse_archive(buffer.getvalue())
        self.assertEqual(list(frame.columns)[:6], self.crypto.KLINE_COLUMNS[:6])
        self.assertEqual(float(frame["high"].iloc[0]), 42100.0)

    def test_archives_with_headers_are_parsed(self):
        import io as _io
        import zipfile

        text = (",".join(self.crypto.KLINE_COLUMNS) + "\n"
                "1704067200000,42000.0,42100.0,41900.0,42050.0,1.5,1704067499999,63000,10,0.7,29000,0\n")
        buffer = _io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("BTCUSDT-5m-2024-01.csv", text)
        frame = self.crypto._parse_archive(buffer.getvalue())
        self.assertEqual(float(frame["low"].iloc[0]), 41900.0)


class SweepProbe(unittest.TestCase):
    """Step 2 of hypothesis testing: does the sweep alone carry the edge?

    If the raw sweep is negative-EV before costs, filters are selecting from a
    losing population and the hypothesis is dead. The probe exists so that gets
    answered before anyone builds a state machine on top of it.
    """

    def setUp(self):
        from dataflows import smc

        self.smc = smc
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, bars):
        """bars: (iso_timestamp, open, high, low, close)."""
        lines = ["timestamp,open,high,low,close,volume"]
        lines += [f"{t},{o},{h},{l},{c},100" for t, o, h, l, c in bars]
        path = self.dir / "bars.csv"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def _two_sessions(self, second_day):
        """Day one sets PDH at 110; day two is supplied by the caller."""
        day1 = [(f"2024-01-02 {h:02d}:{m:02d}:00", 100, 110, 90, 100)
                for h in range(10, 12) for m in (0, 30)]
        return self._write(day1 + second_day)

    def test_an_unswept_level_yields_no_trades(self):
        quiet = [(f"2024-01-03 {h:02d}:{m:02d}:00", 100, 105, 95, 100)
                 for h in range(10, 12) for m in (0, 30)]
        out = self.smc.probe("NQ", self._two_sessions(quiet), "pd_high")
        self.assertIn("No sweeps found", out)

    def test_a_gap_beyond_the_level_is_excluded_not_counted_as_a_sweep(self):
        """The session never traded into the resting orders."""
        gapped = [(f"2024-01-03 {h:02d}:{m:02d}:00", 130, 135, 125, 130)
                  for h in range(10, 12) for m in (0, 30)]
        out = self.smc.probe("NQ", self._two_sessions(gapped), "pd_high")
        self.assertIn("gapped beyond", out)

    def test_every_requested_target_is_reported(self):
        swept = [("2024-01-03 10:00:00", 100, 112, 99, 105),
                 ("2024-01-03 10:30:00", 105, 106, 80, 82),
                 ("2024-01-03 11:00:00", 82, 84, 70, 72)]
        out = self.smc.probe("NQ", self._two_sessions(swept), "pd_high",
                             targets="1.0,2.0,3.0")
        for label in ("1.0R", "2.0R", "3.0R"):
            self.assertIn(label, out)

    def test_an_unknown_level_is_rejected(self):
        self.assertIn("unknown level", self.smc.probe("NQ", "x.csv", "rth_vwap"))

    def test_a_wider_stop_lowers_cost_per_r(self):
        """Fixed costs are a smaller share of a bigger risk budget."""
        swept = [("2024-01-03 10:00:00", 100, 112, 99, 105),
                 ("2024-01-03 10:30:00", 105, 106, 80, 82),
                 ("2024-01-03 11:00:00", 82, 84, 70, 72)]
        path = self._two_sessions(swept)
        tight = self.smc.probe("NQ", path, "pd_high", stop_ticks=4)
        wide = self.smc.probe("NQ", path, "pd_high", stop_ticks=40)

        def cost_of(text):
            import re as _re
            return float(_re.search(r"−(\d+\.\d+)R", text).group(1))

        self.assertGreater(cost_of(tight), cost_of(wide))


class SampleProjection(unittest.TestCase):
    """Answering "when will I know if this works" with a session count."""

    def setUp(self):
        from dataflows import smc

        self.project = smc.sample_projection

    def test_it_reports_how_many_more_sessions_are_needed(self):
        out = self.project(sessions_scanned=100, resolved=5, min_sample=20)
        self.assertIn("0.050 resolved setups per session", out)
        self.assertIn("300 further sessions", out)

    def test_a_zero_rate_says_the_gate_never_opens(self):
        out = self.project(sessions_scanned=200, resolved=0, min_sample=20)
        self.assertIn("never opens", out)
        self.assertIn("sensitivity", out)

    def test_reaching_the_floor_says_so(self):
        out = self.project(sessions_scanned=100, resolved=25, min_sample=20)
        self.assertIn("floor reached", out)

    def test_no_sessions_produces_no_claim(self):
        self.assertEqual(self.project(0, 0, 20), "")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class Formatting(unittest.TestCase):
    def test_missing_values_read_as_missing(self):
        self.assertEqual(common.fmt_num(None), "N/A")
        self.assertEqual(common.fmt_num(float("nan")), "N/A")

    def test_large_numbers_are_scaled(self):
        self.assertEqual(common.fmt_num(2_500_000_000), "2.50B")
        self.assertEqual(common.fmt_num(2_500_000), "2.50M")

    def test_counts_keep_their_integer_shape(self):
        self.assertEqual(common.fmt_num(12_345), "12,345")

    def test_an_empty_table_says_so(self):
        self.assertEqual(common.markdown_table(["A"], []), "_(no rows)_")


if __name__ == "__main__":
    unittest.main(verbosity=2)
