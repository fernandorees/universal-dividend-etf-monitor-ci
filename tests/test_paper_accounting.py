"""Synthetic fictional paper events; no real market data, holdings, signals or fills."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

from schd_monitor.models import MonitorDataError
from schd_monitor.paper_accounting import (
    PaperDay, PaperDividend, PaperFill, replay_explicit_paper_events,
)
from schd_monitor.split_audit import SplitEvent, SplitLedger


def sessions(n=80):
    rows = []
    d = date(2024, 1, 2)
    while len(rows) < n:
        if d.weekday() < 5:
            rows.append(d)
        d += timedelta(days=1)
    return rows


DAYS = sessions()


def days(n=80, *, opens="100", closes="100"):
    return [PaperDay(d, D(opens), D(closes)) for d in DAYS[:n]]


def fill(i, side, quantity, fee="0"):
    return PaperFill(DAYS[i], side, D(quantity), D(fee))


def dividend(event_id="A", *, ex=2, pay=5, per="1"):
    return PaperDividend(event_id, DAYS[ex], DAYS[pay], D(per))


def replay(*, day_rows=None, fills=None, dividends=None, start="100000",
           split_ledger=None, symbol="SCHD"):
    return replay_explicit_paper_events(
        symbol=symbol,
        days=days() if day_rows is None else day_rows,
        fills=[] if fills is None else fills,
        dividends=[] if dividends is None else dividends,
        split_ledger=SplitLedger("SCHD", ()) if split_ledger is None else split_ledger,
        initial_cash=D(start),
    )


class PaperAccountingTests(unittest.TestCase):
    def test_initial_hundred_thousand_immediate_explicit_buy_and_fee_friction(self):
        report = replay(fills=[fill(0, "BUY", "100", "1")])
        first = report.rows[0]
        self.assertEqual(first.paper_fill, "BUY")
        self.assertEqual(D(first.paper_execution_price_after_friction), D("100.0500"))
        self.assertEqual(D(first.shares_at_close), D("100"))
        self.assertEqual(D(first.cash_at_close), D("89994.0000"))
        self.assertEqual(D(first.cash_plus_equity_excluding_unpaid_entitlements), D("99994"))
        self.assertEqual(report.status, "SYNTHETIC_EVENT_ACCOUNTING_ONLY_UNVERIFIED")
        self.assertFalse(report.original_champion_parity_verified)
        self.assertTrue(report.trades_are_explicit_paper_fixtures_not_signals)

    def test_two_thousand_deposit_credited_before_opening_buy(self):
        d = days(3)
        d[1] = replace(d[1], contribution_before_open=D("2000"))
        report = replay(day_rows=d, fills=[fill(1, "BUY", "19", "1")], start="0")
        r = report.rows[1]
        self.assertEqual(D(r.contribution_before_open), D(2000))
        self.assertEqual(D(r.cash_at_close), D("98.0500"))
        self.assertEqual(D(r.shares_at_close), D(19))

    def test_no_buy_is_inferred_from_cash_signal_or_monthly_calendar(self):
        d = days(3)
        d[1] = replace(d[1], contribution_before_open=D("2000"))
        result = replay(day_rows=d, start="100000")
        self.assertEqual(D(result.rows[1].cash_at_close), D("102000"))
        self.assertEqual(result.rows[1].paper_fill, "NONE")
        self.assertEqual(D(result.rows[1].shares_at_close), D(0))

    def test_buy_at_ex_open_is_not_entitled_to_that_distribution(self):
        r = replay(day_rows=days(7), fills=[fill(2, "BUY", "10")],
                   dividends=[dividend()], start="2000")
        self.assertEqual(D(r.rows[2].dividend_ex_entitlement), D(0))
        self.assertEqual(D(r.rows[5].dividend_payment_before_open), D(0))
        self.assertEqual(D(r.rows[5].cash_at_close), D("999.5000"))

    def test_sale_at_ex_open_preserves_prior_shares_entitlement(self):
        report = replay(day_rows=days(7),
                        fills=[fill(0, "BUY", "10"), fill(2, "SELL", "10")],
                        dividends=[dividend(per="0.25")], start="2000")
        self.assertEqual(D(report.rows[2].dividend_ex_entitlement), D("2.50"))
        self.assertEqual(D(report.rows[2].unpaid_dividend_entitlement), D("2.50"))
        self.assertEqual(D(report.rows[5].dividend_payment_before_open), D("2.50"))
        self.assertEqual(D(report.rows[5].unpaid_dividend_entitlement), D(0))
        self.assertEqual(report.rows[2].held_peak_close_only, None)

    def test_pending_entitlement_not_available_as_buying_cash(self):
        with self.assertRaisesRegex(MonitorDataError, "exceed available cash"):
            replay(day_rows=days(7), fills=[
                fill(0, "BUY", "1"),
                fill(3, "BUY", "1"),
            ], dividends=[dividend(per="100")], start="100.05")

    def test_payday_receipt_is_available_before_open_buy(self):
        result = replay(day_rows=days(7), fills=[
            fill(0, "BUY", "1"), fill(5, "BUY", "1"),
        ], dividends=[dividend(per="100.05")], start="100.05")
        self.assertEqual(D(result.rows[5].dividend_payment_before_open), D("100.05"))
        self.assertEqual(D(result.rows[5].shares_at_close), D("2"))
        self.assertEqual(D(result.rows[5].cash_at_close), D("0"))

    def test_dividends_do_not_reduce_split_only_market_price(self):
        d = days(7, opens="100", closes="100")
        result = replay(day_rows=d, fills=[fill(0, "BUY", "2")],
                        dividends=[dividend(per="2")], start="1000")
        self.assertEqual(D(result.rows[2].held_peak_close_only), D("100"))
        self.assertEqual(D(result.rows[2].marked_equity_at_close), D("200"))
        self.assertEqual(D(result.rows[2].unpaid_dividend_entitlement), D("4"))

    def test_partial_sale_preserves_peak_and_full_exit_resets_it(self):
        d = days(7)
        d[1] = replace(d[1], close=D("130"))
        d[2] = replace(d[2], close=D("90"))
        d[3] = replace(d[3], close=D("80"))
        result = replay(day_rows=d, fills=[
            fill(0, "BUY", "4"), fill(2, "SELL", "1"), fill(3, "SELL", "3"),
        ], start="1000")
        self.assertEqual(D(result.rows[2].held_peak_close_only), D("130"))
        self.assertIsNone(result.rows[3].held_peak_close_only)
        self.assertEqual(D(result.rows[3].shares_at_close), D(0))

    def test_reentry_after_flat_resets_close_only_peak(self):
        d = days(7)
        d[1] = replace(d[1], close=D("150"))
        result = replay(day_rows=d, fills=[
            fill(0, "BUY", "1"), fill(2, "SELL", "1"),
            fill(4, "BUY", "1"),
        ], start="500")
        self.assertEqual(D(result.rows[1].held_peak_close_only), D(150))
        self.assertIsNone(result.rows[2].held_peak_close_only)
        self.assertEqual(D(result.rows[4].held_peak_close_only), D(100))

    def test_sale_cash_receives_lower_side_of_friction_and_fee(self):
        result = replay(day_rows=days(3), fills=[
            fill(0, "BUY", "2", "1"), fill(2, "SELL", "2", "1"),
        ], start="1000")
        self.assertEqual(D(result.rows[2].paper_execution_price_after_friction), D("99.9500"))
        self.assertEqual(D(result.rows[2].cash_at_close), D("997.8000"))
        self.assertEqual(D(result.rows[2].paper_discretionary_fee), D(1))

    def test_oversized_buy_sell_and_cash_fee_fail_closed(self):
        with self.assertRaisesRegex(MonitorDataError, "exceed available cash"):
            replay(day_rows=days(2), start="100", fills=[fill(0, "BUY", "1")])
        with self.assertRaisesRegex(MonitorDataError, "exceeds held shares"):
            replay(day_rows=days(2), fills=[fill(0, "SELL", "1")])
        with self.assertRaisesRegex(MonitorDataError, "net proceeds cannot be negative"):
            replay(day_rows=days(2), fills=[
                fill(0, "BUY", "1"), fill(1, "SELL", "1", "150"),
            ], start="200")

    def test_explicit_interest_credit_not_computed_from_yield_assumption(self):
        d = days(3)
        d[1] = replace(d[1], explicit_interest_credit_before_open=D("0.03"))
        result = replay(day_rows=d, start="0")
        self.assertEqual(D(result.rows[0].cash_at_close), D(0))
        self.assertEqual(D(result.rows[1].cash_at_close), D("0.03"))
        self.assertEqual(D(result.rows[2].cash_at_close), D("0.03"))

    def test_decimal_fractional_cash_remains_exact(self):
        d = days(3)
        d[0] = replace(d[0], contribution_before_open=D("0.1"))
        d[1] = replace(d[1], contribution_before_open=D("0.2"))
        result = replay(day_rows=d, start="0")
        self.assertEqual(D(result.rows[2].cash_at_close), D("0.3"))

    def test_rejects_split_in_window_without_silently_changing_shares(self):
        with self.assertRaisesRegex(MonitorDataError, "refuses stock splits"):
            replay(day_rows=days(7), split_ledger=SplitLedger(
                "SCHD", (SplitEvent(DAYS[3], 3, 1),)
            ))

    def test_rejects_missing_or_mismatched_split_ledger(self):
        for value in (None, "wrong", SplitLedger("DGRO", ())):
            if value is None:
                with self.assertRaisesRegex(MonitorDataError, "matching versioned split ledger"):
                    replay_explicit_paper_events(
                        symbol="SCHD", days=days(3), fills=[], dividends=[],
                        split_ledger=None, initial_cash=D("100"),
                    )
            elif isinstance(value, str):
                with self.assertRaises(MonitorDataError):
                    replay(split_ledger=value)
            else:
                with self.assertRaises(MonitorDataError):
                    replay(split_ledger=value)

    def test_rejects_uncovered_dividend_dates_and_duplicate_events(self):
        with self.assertRaisesRegex(MonitorDataError, "both be covered"):
            replay(day_rows=days(3), dividends=[dividend(ex=2,pay=5)])
        with self.assertRaisesRegex(MonitorDataError, "Duplicate or ambiguous"):
            replay(day_rows=days(7), dividends=[dividend(), dividend()])
        with self.assertRaisesRegex(MonitorDataError, "Duplicate or ambiguous"):
            replay(day_rows=days(7), dividends=[
                dividend(event_id="A"), dividend(event_id="B")
            ])

    def test_rejects_multiple_same_day_fills_and_uncovered_fill(self):
        with self.assertRaisesRegex(MonitorDataError, "ambiguous multiple"):
            replay(day_rows=days(3), fills=[fill(0, "BUY", "1"), fill(0, "SELL", "1")])
        with self.assertRaisesRegex(MonitorDataError, "outside supplied"):
            replay(day_rows=days(3), fills=[fill(10, "BUY", "1")])

    def test_rejects_invalid_inputs_and_nonfinite_decimals(self):
        with self.assertRaises(MonitorDataError):
            PaperDay(DAYS[0], D("NaN"), D("100"))
        with self.assertRaises(MonitorDataError):
            PaperDay(DAYS[0], D("-1"), D("100"))
        with self.assertRaises(MonitorDataError):
            PaperFill(DAYS[0], "PROPOSED", D("2"), D("1"))
        with self.assertRaises(MonitorDataError):
            PaperFill(DAYS[0], "BUY", D("0"), D("1"))
        with self.assertRaises(MonitorDataError):
            PaperDividend("A", DAYS[2], DAYS[2], D(".25"))
        with self.assertRaises(MonitorDataError):
            PaperDividend("A", DAYS[2], DAYS[1], D(".25"))
        with self.assertRaises(MonitorDataError):
            replay(start="-1")
        with self.assertRaises(MonitorDataError):
            replay(start="NaN")
        with self.assertRaises(MonitorDataError):
            replay(start="1E+56")

    def test_rejects_unordered_dates_and_missing_sessions(self):
        with self.assertRaisesRegex(MonitorDataError, "strictly ordered"):
            replay(day_rows=days(3)[::-1])
        rows = days(8)
        with self.assertRaisesRegex(MonitorDataError, "strictly ordered"):
            replay(day_rows=rows[:3] + [rows[2]] + rows[3:])
        with self.assertRaisesRegex(MonitorDataError, "gap exceeding seven"):
            replay(day_rows=[rows[0], PaperDay(DAYS[-1], D(100), D(100))])

    def test_two_symbol_allocation_is_not_implicitly_enabled(self):
        self.assertEqual(replay(day_rows=days(3), symbol="SCHD").symbol, "SCHD")
        with self.assertRaisesRegex(MonitorDataError, "matching versioned split ledger"):
            replay(day_rows=days(3), symbol="DGRO")
        self.assertFalse(replay(day_rows=days(3)).original_champion_parity_verified)


if __name__ == "__main__":
    unittest.main()
