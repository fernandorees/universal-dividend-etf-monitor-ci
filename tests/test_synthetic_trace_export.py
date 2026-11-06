"""Fictional end-to-end candidate trace creation and normalized comparator tests."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.paper_accounting import PaperDay, PaperDividend, PaperFill
from schd_monitor.reference_parity import TRACE_COLUMNS, compare_local_traces
from schd_monitor.split_audit import SplitEvent, SplitLedger
from schd_monitor.synthetic_trace_export import (
    build_synthetic_champion_trace, write_synthetic_trace_csv,
)


def dates(n=80):
    out = []
    current = date(2024, 1, 2)
    while len(out) < n:
        if current.weekday() < 5:
            out.append(current)
        current += timedelta(days=1)
    return out


DATES = dates()


def bars(n=72, *, drop=True):
    result = []
    for i, session in enumerate(DATES[:n]):
        close = 90.0 if drop and i >= 70 else 100.0
        result.append(Bar(session, 100, max(close, 100) + 1,
                          min(close, 100) - 1, close))
    return result


def papers(prices):
    return [PaperDay(b.session, D(str(b.open)), D(str(b.close))) for b in prices]


def fill(i, action, qty="1", fee="0"):
    return PaperFill(DATES[i], action, D(qty), D(fee))


def trace(*, prices=None, days=None, fills=None, dividends=None,
          split=None, cash="1000", symbol="SCHD"):
    b = bars() if prices is None else prices
    return build_synthetic_champion_trace(
        symbol=symbol, bars=b, paper_days=papers(b) if days is None else days,
        hypothetical_fills=[fill(0, "BUY"), fill(71, "SELL")] if fills is None else fills,
        dividends=[] if dividends is None else dividends,
        split_ledger=SplitLedger(symbol, ()) if split is None else split,
        initial_cash=D(cash),
    )


class SyntheticTraceTests(unittest.TestCase):
    def test_report_unverified_and_exact_schema_from_original_components(self):
        t = trace()
        self.assertEqual(t.status, "SYNTHETIC_NORMALIZED_TRACE_ONLY_UNVERIFIED")
        self.assertEqual(t.schema_version, 1)
        self.assertEqual(t.symbol, "SCHD")
        self.assertEqual(len(t.rows), 72)
        self.assertEqual(tuple(t.rows[0]), TRACE_COLUMNS)
        self.assertTrue(all(tuple(r) == TRACE_COLUMNS for r in t.rows))
        self.assertTrue(all(len(r) == 24 for r in t.rows))
        self.assertFalse(t.historical_reference_parity_verified)
        self.assertFalse(t.original_peak_initialization_verified)
        self.assertFalse(t.fee_and_buy_sizing_parity_verified)
        self.assertFalse(t.source_prices_independently_verified)

    def test_first_initial_buy_fill_is_explicit_and_no_warmup_signal(self):
        first = trace().rows[0]
        self.assertEqual(first["signal_action"], "NO_SIGNAL")
        self.assertEqual(first["fill_action"], "BUY")
        self.assertEqual(first["fill_price"], "100.0500")
        self.assertEqual(first["shares"], "1")
        self.assertEqual(first["cash"], "899.9500")
        self.assertEqual(D(first["highest_held_close"]), D("100"))
        self.assertEqual(first["ending_wealth"], "999.9500")
        self.assertEqual(first["buy_eligible"], "false")
        self.assertEqual(first["sell_eligible"], "false")

    def test_indicator_seed_rows_and_warmed_decision_are_from_frozen_engine(self):
        t = trace()
        self.assertEqual(t.rows[13]["atr14"], "")
        self.assertNotEqual(t.rows[14]["atr14"], "")
        self.assertEqual(t.rows[33]["macd"], "")
        self.assertNotEqual(t.rows[34]["macd"], "")
        self.assertEqual(t.rows[58]["sma60"], "")
        self.assertNotEqual(t.rows[59]["sma60"], "")
        self.assertEqual(t.rows[63]["signal_action"], "NO_SIGNAL")
        self.assertEqual(t.rows[64]["signal_action"], "HOLD")

    def test_bearish_and_atr_stop_candidate_separate_from_next_open_fill(self):
        t = trace()
        self.assertEqual(t.rows[70]["signal_action"], "SELL")
        self.assertEqual(t.rows[70]["sell_eligible"], "true")
        self.assertEqual(t.rows[70]["fill_action"], "NONE")
        self.assertEqual(t.rows[71]["fill_action"], "SELL")
        self.assertEqual(t.rows[71]["signal_action"], "BUY")
        self.assertEqual(t.rows[71]["highest_held_close"], "")
        self.assertEqual(t.rows[71]["shares"], "0")

    def test_signal_buy_after_warmup_requires_explicit_next_open_paper_fill(self):
        t = trace(fills=[fill(65, "BUY"), fill(71, "SELL")])
        self.assertEqual(t.rows[64]["signal_action"], "BUY")
        self.assertEqual(t.rows[64]["fill_action"], "NONE")
        self.assertEqual(t.rows[65]["fill_action"], "BUY")
        self.assertEqual(t.rows[65]["buy_eligible"], "false")
        self.assertEqual(t.rows[71]["fill_action"], "SELL")

    def test_deposit_cash_and_interest_are_explicit_not_derived(self):
        p = papers(bars())
        p[68] = replace(p[68], contribution_before_open=D("2000"),
                        explicit_interest_credit_before_open=D("0.03"))
        t = trace(days=p)
        self.assertEqual(t.rows[68]["monthly_deposit"], "2000")
        self.assertEqual(t.rows[68]["interest_credit"], "0.03")
        self.assertEqual(t.rows[67]["monthly_deposit"], "0")
        self.assertEqual(t.rows[67]["interest_credit"], "0")
        self.assertEqual(t.rows[68]["cash"], "2899.9800")

    def test_dividend_ex_entitlement_and_payment_are_separate_fields(self):
        t = trace(dividends=[
            PaperDividend("FICTIONAL_A", DATES[68], DATES[69], D("1.25"))
        ])
        self.assertEqual(t.rows[68]["dividend_ex_accrual"], "1.25")
        self.assertEqual(t.rows[68]["dividend_payment"], "0")
        self.assertEqual(t.rows[69]["dividend_ex_accrual"], "0")
        self.assertEqual(t.rows[69]["dividend_payment"], "1.25")
        self.assertEqual(t.rows[69]["cash"], "901.2000")

    def test_fictional_fee_and_net_sell_friction_are_not_hidden(self):
        t = trace(fills=[
            fill(0, "BUY", "1", "1"), fill(71, "SELL", "1", "1")
        ])
        self.assertEqual(t.rows[0]["cash"], "898.9500")
        self.assertEqual(t.rows[71]["fill_price"], "99.9500")
        self.assertEqual(t.rows[71]["cash"], "997.9000")

    def test_float_exponent_is_exported_as_decimal_literal(self):
        self.assertEqual(
            __import__("schd_monitor.synthetic_trace_export", fromlist=["_fixed"])
            ._fixed(0.0000001), "0.0000001"
        )

    def test_missing_and_mismatched_paper_fill_fail_without_trace(self):
        for events in ([fill(0, "BUY")],
                       [fill(0, "BUY"), fill(71, "BUY")]):
            with self.subTest(events=events):
                with self.assertRaisesRegex(MonitorDataError, "candidate/fill mismatch"):
                    trace(fills=events)

    def test_paper_insufficient_cash_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "exceed available cash"):
            trace(fills=[fill(0, "BUY", "500"), fill(71, "SELL", "500")])

    def test_paper_dates_and_market_prices_must_match(self):
        b = bars()
        p = papers(b)
        p[20] = replace(p[20], open=D("101"))
        with self.assertRaisesRegex(MonitorDataError, "disagree"):
            trace(prices=b, days=p)

    def test_split_in_paper_window_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "refuses stock splits"):
            trace(split=SplitLedger("SCHD", (SplitEvent(DATES[30], 3, 1),)))

    def test_export_rejects_overwrite_and_retains_original_file(self):
        t = trace()
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "synthetic.csv"
            write_synthetic_trace_csv(t, target)
            original = target.read_bytes()
            self.assertTrue(original.startswith(b"session,open,high,low,close,"))
            with self.assertRaises(FileExistsError):
                write_synthetic_trace_csv(t, target)
            self.assertEqual(target.read_bytes(), original)

    def test_export_is_rejected_for_forged_verified_trace(self):
        fake = replace(trace(), historical_reference_parity_verified=True)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "not_written.csv"
            with self.assertRaisesRegex(MonitorDataError, "Only unverified"):
                write_synthetic_trace_csv(fake, target)
            self.assertFalse(target.exists())

    def test_exported_trace_is_parseable_by_existing_strict_parity_comparator(self):
        t = trace()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            reference = root / "fictional_reference.csv"
            candidate = root / "fictional_candidate.csv"
            session_file = root / "fictional_sessions.csv"
            manifest = root / "fictional_manifest.json"
            write_synthetic_trace_csv(t, candidate)
            # Identical copy used ONLY to exercise parsing, never to claim
            # independent reference export or historical parity.
            reference.write_bytes(candidate.read_bytes())
            with session_file.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["date"])
                writer.writerows([[r["session"]] for r in t.rows])
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "symbol": "SCHD", "strategy": "CHAMPION",
                "first_session": t.rows[0]["session"],
                "last_session": t.rows[-1]["session"],
                "expected_session_count": len(t.rows),
                "hypothetical_cash_interest_rate": "0",
                "reference_origin_claim": "FICTIONAL_COPY_NOT_INDEPENDENT",
                "candidate_origin_claim": "FICTIONAL_PAPER_EXPORT_UNATTESTED",
            }), encoding="utf-8")
            r = compare_local_traces(reference, candidate, manifest, session_file)
            self.assertEqual(r.status, "TRACE_MATCH_UNATTESTED")
            self.assertEqual(r.differences, 0)
            self.assertFalse(r.original_simulator_parity_verified)

    def test_dgro_fictional_example_does_not_enable_allocation_or_rebalancing(self):
        report = trace(symbol="DGRO")
        self.assertEqual(report.symbol, "DGRO")
        self.assertFalse(report.historical_reference_parity_verified)
        self.assertNotIn("target_weight", report.rows[0])
        self.assertNotIn("rebalance_action", report.rows[0])


if __name__ == "__main__":
    unittest.main()
