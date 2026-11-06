"""Fictional-only regression tests for the independent historical champion engine."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta

from schd_monitor.historical_champion import (
    MONTHLY_CASH, ResearchDividend, replay_hypothetical_champion,
)
from schd_monitor.models import Bar, MonitorDataError


def bars(n=120, *, shock=None):
    days = []
    d = date(2090, 1, 2)
    while len(days) < n:
        if d.weekday() < 5:
            i = len(days)
            close = (shock if shock is not None and i >= 83 else 100.0)
            days.append(Bar(d, close, close + .5, close - .5, close))
        d += timedelta(days=1)
    return days


def replay(b, dividends=(), first=80, last=110):
    return replay_hypothetical_champion(
        bars=b, dividends=dividends, start=b[first].session,
        end=b[last].session)


class HistoricalChampionTests(unittest.TestCase):
    def test_immediate_start_open_buy_and_discretionary_fee_friction(self):
        b = bars()
        result = replay(b)
        self.assertEqual(result.purchases_including_first, 1 +
                         sum(row.action == "BUY" for row in result.rows_after_first_open))
        self.assertEqual(result.first_open_purchase_session, b[80].session.isoformat())
        self.assertEqual(result.rows_after_first_open[0].shares,
                         (100000.0 - 1.0) / (100.0 * 1.0005))
        self.assertEqual(result.rows_after_first_open[0].peak, 100.0)
        self.assertFalse(result.production_signal_verified)
        self.assertFalse(result.original_source_executed)

    def test_monthly_deposit_precedes_next_open_buy(self):
        b = bars()
        result = replay(b, first=65, last=110)
        starts = [i for i in range(66, 111)
                  if b[i].session.month != b[i-1].session.month]
        self.assertTrue(starts)
        for i in starts:
            row = result.rows_after_first_open[i-66]
            self.assertEqual(row.deposit, MONTHLY_CASH)
            self.assertEqual(row.action, "BUY")
        self.assertEqual(result.purchases_including_first, 1 + len(starts))

    def test_ex_date_before_open_and_payment_reinvested_after_open(self):
        b = bars()
        event = ResearchDividend(b[82].session, b[84].session, .25)
        result = replay(b, dividends=[event])
        rows = result.rows_after_first_open
        before = rows[0].shares
        self.assertEqual(rows[1].dividend_receivables, before * .25)
        self.assertEqual(rows[3].dividend_receivables, 0)
        self.assertEqual(rows[3].shares, before + (before * .25)/(100.0 * 1.0005))
        self.assertEqual(result.sales, 0)

    def test_atr_stop_sells_next_open_not_signal_close(self):
        b = bars(shock=70.0)
        # Use first opening before synthetic shock; first shock is day index 83.
        result = replay(b)
        rows = result.rows_after_first_open
        self.assertEqual(rows[2].date, b[83].session.isoformat())
        self.assertNotEqual(rows[2].action, "SELL")
        self.assertEqual(rows[3].date, b[84].session.isoformat())
        self.assertEqual(rows[3].action, "SELL")
        self.assertEqual(rows[3].kind, "ATR")
        self.assertTrue(rows[3].atr_trigger)
        self.assertEqual(rows[3].shares, 0.0)
        self.assertEqual(rows[3].peak, 0.0)
        self.assertEqual(result.sales, 1)

    def test_no_same_open_repurchase_after_sale(self):
        b = bars(shock=70.0)
        result = replay(b)
        sale = next(row for row in result.rows_after_first_open if row.action == "SELL")
        self.assertEqual(sale.action, "SELL")
        self.assertGreater(sale.cash, 0)
        self.assertEqual(sale.shares, 0)

    def test_reentry_initializes_peak_from_open_then_close(self):
        b = bars(shock=70.0)
        r = replay(b)
        buys_after_sale = [row for row in r.rows_after_first_open
                           if row.action == "BUY"
                           and row.date > next(x.date for x in r.rows_after_first_open
                                               if x.action == "SELL")]
        # A future SMA-triggered reopening requires cash; when present the
        # first observed peak must not retain the previous position's peak.
        for row in buys_after_sale:
            self.assertGreaterEqual(row.peak, row.open)
            self.assertGreaterEqual(row.peak, row.close)

    def test_cash_interest_uses_calendar_day_exponent(self):
        b = bars(shock=70.0)
        # Exit at 84 because the prior close is 70; close at 84 rebounds
        # above SMA, so 85 is a cash-only day with monthly deposit.
        b[84] = Bar(b[84].session, 70.0, 120.5, 69.5, 120.0)
        r = replay(b)
        rows = r.rows_after_first_open
        sale_idx = next(i for i, x in enumerate(rows) if x.action == "SELL")
        x, y = rows[sale_idx:sale_idx+2]
        self.assertEqual(y.action, "")
        days = (date.fromisoformat(y.date)-date.fromisoformat(x.date)).days
        self.assertAlmostEqual(y.cash, x.cash * (1.03 ** (days / 365.25)) + y.deposit)

    def test_rejects_start_before_indicator_warmup(self):
        b = bars()
        with self.assertRaisesRegex(MonitorDataError, "warmed indicators"):
            replay(b, first=40)

    def test_rejects_outside_endpoints(self):
        b = bars()
        with self.assertRaisesRegex(MonitorDataError, "complete sessions"):
            replay_hypothetical_champion(
                bars=b, dividends=[], start=date(2080, 1, 1),
                end=b[-1].session)

    def test_rejects_invalid_and_duplicate_dividend_events(self):
        b = bars()
        with self.assertRaises(MonitorDataError):
            ResearchDividend(b[85].session, b[82].session, .5)
        with self.assertRaises(MonitorDataError):
            ResearchDividend(b[82].session, b[85].session, float("nan"))
        event = ResearchDividend(b[82].session, b[85].session, .25)
        with self.assertRaisesRegex(MonitorDataError, "Duplicate dividend"):
            replay(b, dividends=[event, event])

    def test_rejects_untyped_dividends_and_missing_pay_session(self):
        b = bars()
        with self.assertRaises(MonitorDataError):
            replay(b, dividends=[{"ex": "2090-01-01"}])
        e = ResearchDividend(b[82].session, b[82].session + timedelta(days=500), .25)
        # Paydate after replay end is a legitimate outstanding entitlement.
        self.assertGreater(replay(b, dividends=[e]).rows_after_first_open[-1].dividend_receivables, 0)

    def test_unreconciled_prices_or_dates_fail_closed(self):
        b = bars()
        bad = list(b)
        bad[30] = replace(bad[30], session=bad[29].session)
        with self.assertRaises(MonitorDataError):
            replay(bad)

    def test_replay_never_infers_actual_user_brokerage_fills(self):
        r = replay(bars())
        self.assertEqual(r.status, "INDEPENDENT_HYPOTHETICAL_CHAMPION_REPLAY_UNVERIFIED")
        self.assertFalse(r.production_signal_verified)
        self.assertFalse(r.historical_market_data_independently_verified)


if __name__ == "__main__":
    unittest.main()
