"""P01 bridge tests: fictional OHLC, explicit simulated fills, no broker data."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.paper_accounting import PaperDay, PaperDividend, PaperFill
from schd_monitor.split_audit import SplitEvent, SplitLedger
from schd_monitor.synthetic_champion_bridge import audit_synthetic_champion_fill_schedule


def trading_dates(n=75):
    output = []
    day = date(2024, 1, 2)
    while len(output) < n:
        if day.weekday() < 5:
            output.append(day)
        day += timedelta(days=1)
    return output


DATES = trading_dates()


def price_rows(count=72, drop=True):
    result = []
    for i, day in enumerate(DATES[:count]):
        close = 90.0 if drop and i >= 70 else 100.0
        op = 100.0
        result.append(Bar(day, op, max(op, close) + 1,
                          min(op, close) - 1, close))
    return result


def fill(index, side, quantity="1", fee="0"):
    return PaperFill(DATES[index], side, D(quantity), D(fee))


def paper_rows(prices):
    return [PaperDay(b.session, D(str(b.open)), D(str(b.close))) for b in prices]


def verify(*, bars=None, papers=None, fills=None, dividends=None,
           split_ledger=None, cash="1000", symbol="SCHD"):
    b = price_rows() if bars is None else bars
    return audit_synthetic_champion_fill_schedule(
        symbol=symbol, bars=b, paper_days=paper_rows(b) if papers is None else papers,
        hypothetical_fills=[fill(0, "BUY"), fill(71, "SELL")] if fills is None else fills,
        dividends=[] if dividends is None else dividends,
        split_ledger=SplitLedger(symbol, ()) if split_ledger is None else split_ledger,
        initial_cash=D(cash),
    )


class SyntheticChampionBridgeTests(unittest.TestCase):
    def test_atr_stop_on_close_matches_explicit_next_open_full_sale(self):
        r = verify()
        self.assertEqual(r.status, "SYNTHETIC_CHAMPION_TO_EXPLICIT_PAPER_FILLS_CONSISTENT_UNVERIFIED")
        self.assertEqual(r.sessions_checked, 72)
        self.assertEqual(r.completed_close_decisions_checked, 8)
        self.assertEqual(r.hypothetical_fills_checked, 2)
        self.assertTrue(r.explicit_initial_opening_buy)
        self.assertEqual(r.last_unexecuted_end_of_history_candidate, "BUY")
        self.assertEqual(r.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(r.original_peak_initialization_parity_verified)
        self.assertFalse(r.historical_order_sizing_parity_verified)
        self.assertFalse(r.historical_reference_parity_verified)
        self.assertFalse(r.price_sources_independently_verified)

    def test_first_signal_buy_after_warmup_and_later_sell(self):
        r = verify(fills=[fill(65, "BUY"), fill(71, "SELL")])
        self.assertFalse(r.explicit_initial_opening_buy)
        self.assertEqual(r.hypothetical_fills_checked, 2)
        self.assertEqual(r.first_evaluable_signal_session, DATES[64].isoformat())

    def test_missing_required_next_open_sell_is_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "candidate/fill mismatch"):
            verify(fills=[fill(0, "BUY")])

    def test_missing_required_next_open_buy_is_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "candidate/fill mismatch"):
            verify(fills=[])

    def test_wrong_side_at_required_next_open_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "candidate/fill mismatch"):
            verify(fills=[fill(0, "BUY"), fill(71, "BUY")])

    def test_one_session_early_sell_refused(self):
        with self.assertRaisesRegex(MonitorDataError, "candidate/fill mismatch"):
            verify(fills=[fill(0, "BUY"), fill(70, "SELL")])

    def test_partial_sale_refused_for_frozen_full_exit(self):
        with self.assertRaisesRegex(MonitorDataError, "SELL must liquidate"):
            verify(fills=[fill(0, "BUY"), fill(71, "SELL", "0.5")])

    def test_unsignaled_fill_during_warmup_refused(self):
        with self.assertRaisesRegex(MonitorDataError, "warmup"):
            verify(fills=[fill(0, "BUY"), fill(10, "BUY"), fill(71, "SELL")])

    def test_unverified_sell_attempt_at_first_open_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "initial opening BUY"):
            verify(fills=[fill(0, "SELL")])

    def test_last_day_candidate_is_not_assumed_filled(self):
        b = price_rows(count=71, drop=True)
        report = verify(bars=b, fills=[fill(0, "BUY")])
        self.assertEqual(report.last_unexecuted_end_of_history_candidate, "SELL")
        self.assertEqual(report.hypothetical_fills_checked, 1)

    def test_new_deposit_next_open_cannot_retroactively_trigger_yesterday_buy(self):
        b = price_rows()
        p = paper_rows(b)
        p[71] = replace(p[71], contribution_before_open=D(1000))
        r = verify(bars=b, papers=p, fills=[], cash="0")
        self.assertEqual(r.hypothetical_fills_checked, 0)
        self.assertEqual(r.last_unexecuted_end_of_history_candidate, "BUY")

    def test_dividend_event_keeps_entitlements_separate_from_signal_logic(self):
        r = verify(dividends=[
            PaperDividend("FICTIONAL_D", DATES[68], DATES[69], D("1.5"))
        ])
        self.assertEqual(r.completed_close_decisions_checked, 8)
        self.assertEqual(r.hypothetical_fills_checked, 2)

    def test_price_difference_between_signal_and_accounting_is_rejected(self):
        b = price_rows()
        p = paper_rows(b)
        p[15] = replace(p[15], close=D("101"))
        with self.assertRaisesRegex(MonitorDataError, "price dates/open/close disagree"):
            verify(bars=b, papers=p)

    def test_missing_aligned_paper_session_is_rejected(self):
        b = price_rows()
        with self.assertRaisesRegex(MonitorDataError, "align"):
            verify(bars=b, papers=paper_rows(b)[:-1])

    def test_declared_split_in_window_refused_not_silently_adjusted(self):
        split = SplitLedger("SCHD", (SplitEvent(DATES[65], 3, 1),))
        with self.assertRaisesRegex(MonitorDataError, "refuses stock splits"):
            verify(split_ledger=split)

    def test_double_fills_same_day_refused(self):
        with self.assertRaisesRegex(MonitorDataError, "multiple same-day"):
            verify(fills=[fill(0, "BUY"), fill(71, "SELL"),
                          fill(71, "BUY")])

    def test_hypothetical_fee_and_insufficient_cash_propagate_safely(self):
        with self.assertRaisesRegex(MonitorDataError, "exceed available cash"):
            verify(fills=[fill(0, "BUY", "100000", "1"), fill(71, "SELL", "100000")])

    def test_missing_postsignal_history_does_not_generate_any_fill(self):
        b = price_rows(count=65, drop=False)
        report = verify(bars=b, fills=[fill(0, "BUY")])
        self.assertEqual(report.completed_close_decisions_checked, 1)
        self.assertEqual(report.last_unexecuted_end_of_history_candidate, "HOLD")

    def test_unsupported_symbol_fails_closed_without_allocation(self):
        with self.assertRaisesRegex(MonitorDataError, "Unsupported ticker"):
            verify(symbol="OTHER")

    def test_same_universal_champion_mechanics_are_available_for_dgro(self):
        r = verify(symbol="DGRO")
        self.assertEqual(r.symbol, "DGRO")
        self.assertEqual(r.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(r.historical_reference_parity_verified)

    def test_actual_champion_decision_logic_remains_separate_from_accounting(self):
        r = verify()
        data = r.as_json()
        self.assertFalse(data["historical_reference_parity_verified"])
        self.assertTrue(data["explicit_initial_opening_buy"])
        self.assertNotIn("order_id", data)
        self.assertNotIn("brokerage", data)


if __name__ == "__main__":
    unittest.main()
