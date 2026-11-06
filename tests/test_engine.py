import unittest
from dataclasses import replace
from datetime import date, timedelta
from schd_monitor.indicators import calculate
from schd_monitor.models import Bar, Position, MonitorDataError
from schd_monitor.signals import Strategy,evaluate


def bars(n=120, falling=False):
    start=date(2024,1,2);a=[];d=start
    while len(a)<n:
        if d.weekday()<5:
            c=100+len(a)*.1 if not falling else 100-len(a)*.1
            a.append(Bar(d,c,c+.5,c-.5,c))
        d+=timedelta(days=1)
    return a


class TestSignalSafety(unittest.TestCase):
    def test_flat_requires_current_reconciliation_and_buy_condition(self):
        b=bars(falling=True);p=Position('SCHD',0,2000,None,b[-1].session)
        o=evaluate(b,calculate(b),p)
        self.assertEqual(o.candidate_action,'BUY')
        self.assertEqual(o.verification,'UNVERIFIED_DO_NOT_TRADE')
        self.assertEqual(o.execution,'NEXT_REGULAR_US_TRADING_OPEN')

    def test_missing_position_peak_fails(self):
        b=bars()
        with self.assertRaises(MonitorDataError):
            Position('SCHD',50,0,None,b[-1].session)

    def test_nonfinite_or_malformed_held_peak_fails_closed(self):
        b = bars()
        for invalid_peak in (float('nan'), float('inf'), -float('inf'),
                             0.0, -0.5, '100', True):
            with self.subTest(peak=repr(invalid_peak)):
                with self.assertRaises(MonitorDataError):
                    Position('SCHD', 50, 0, invalid_peak, b[-1].session)

    def test_valid_held_peak_and_flat_null_peak_remain_accepted(self):
        b = bars()
        held = Position('SCHD', 50, 0, 110.0, b[-1].session)
        flat = Position('SCHD', 0, 2000, None, b[-1].session)
        self.assertEqual(held.peak_close, 110.0)
        self.assertIsNone(flat.peak_close)

    def test_out_of_date_manual_reconciliation_fails(self):
        b=bars()
        with self.assertRaises(MonitorDataError):
            evaluate(b,calculate(b),Position('SCHD',0,2000,None,b[-2].session))

    def test_atr_independent_stop_priority_and_no_cross_needed(self):
        b=bars();ind=calculate(b);p=Position('SCHD',50,0,200,b[-1].session)
        o=evaluate(b,ind,p)
        self.assertEqual(o.candidate_action,'SELL')
        self.assertIn('ATR_STOP_INDEPENDENT',o.signal_reasons)
        self.assertEqual(o.bearish_cross,False)

    def test_rs20_requires_spy_reference(self):
        b=bars();p=Position('SCHD',50,0,110,b[-1].session)
        with self.assertRaises(MonitorDataError):
            evaluate(b,calculate(b),p,strategy=Strategy.RS20)

    def test_stop_is_strictly_below(self):
        b=bars();ii=calculate(b);today=b[-1];atr=ii[-1].atr14
        # current close equal stop; use peak = close + 2.7*atr (and > close)
        p=Position('SCHD',50,0,today.close+2.7*atr,b[-1].session)
        o=evaluate(b,ii,p)
        self.assertNotIn('ATR_STOP_INDEPENDENT',o.signal_reasons)

    def test_unattested_verification_override_fails_closed(self):
        b = bars(falling=True)
        p = Position('SCHD', 0, 2000, None, b[-1].session)
        indicators = calculate(b)
        for override in (True, 1, 'verified', None):
            with self.subTest(override=override):
                with self.assertRaisesRegex(MonitorDataError, 'VERIFIED status is disabled'):
                    evaluate(b, indicators, p, data_verified=override)

    def test_every_strategy_stays_unverified_in_p01(self):
        b = bars()
        p = Position('SCHD', 50, 0, 110, b[-1].session)
        indicators = calculate(b)
        for strategy in Strategy:
            with self.subTest(strategy=strategy):
                decision = evaluate(b, indicators, p, strategy=strategy,
                                    rs20_underperformance=False)
                self.assertEqual(decision.verification, 'UNVERIFIED_DO_NOT_TRADE')

    def test_disallow_duplicate_and_weekend_bars(self):
        b=bars();b[20]=b[19]
        with self.assertRaises(MonitorDataError):calculate(b)
        b=bars();b[10]=replace(b[10],session=date(2024,1,13))
        with self.assertRaises(MonitorDataError):calculate(b)

if __name__=='__main__':unittest.main()
