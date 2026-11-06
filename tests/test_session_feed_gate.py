"""Fictional-only XNYS calendar plus two declared OHLC feeds gate tests."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from schd_monitor.feed_integrity import FeedDeclaration
from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.session_feed_gate import audit_offline_completed_dual_feeds


def invented_bars(n=90) -> list[Bar]:
    rows = []
    day = date(2040, 1, 2)
    while len(rows) < n:
        if day.weekday() < 5:
            value = 100 + len(rows) * .01
            rows.append(Bar(day, value, value + .6, value - .6, value))
        day += timedelta(days=1)
    return rows


class OfflineSessionFeedGateTests(unittest.TestCase):
    def setUp(self):
        self.primary = invented_bars()
        self.secondary = list(self.primary)
        self.as_of = datetime(2040, 5, 10, 23, 30, tzinfo=timezone.utc)
        self.expected = [r.session for r in self.primary]

    def declaration(self, bars, provider="FABRICATED_A", adjustment="SPLIT_ONLY",
                    through=None, symbol="SCHD"):
        return FeedDeclaration(provider=provider, symbol=symbol, adjustment=adjustment,
            last_complete_session=through or bars[-1].session)

    def audit(self, *, primary=None, secondary=None, expected=None, as_of=None,
              first=None, second=None):
        a = self.primary if primary is None else primary
        b = self.secondary if secondary is None else secondary
        p = first or self.declaration(a)
        q = second or self.declaration(b, provider="FABRICATED_B")
        manifest = self.expected if expected is None else expected
        with patch("schd_monitor.session_feed_gate.completed_xnys_sessions",
                   return_value=manifest) as calendar:
            result = audit_offline_completed_dual_feeds(
                primary=a, secondary=b,
                primary_declaration=p, secondary_declaration=q,
                expected_symbol="SCHD", as_of=self.as_of if as_of is None else as_of)
        calendar.assert_called_once_with(a[0].session,
                                        self.as_of if as_of is None else as_of)
        return result

    def test_consistent_fictional_feeds_never_authenticate_or_promote(self):
        r = self.audit()
        self.assertEqual(r.status, "CALENDAR_COMPLETE_DUAL_FEED_CONSISTENT_UNATTESTED")
        self.assertEqual(r.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertEqual(r.schema_version, 1)
        self.assertEqual(r.calendar_complete_sessions, 90)
        self.assertEqual(r.last_completed_session, self.expected[-1].isoformat())
        self.assertEqual(r.max_relative_ohlc_difference, 0)
        self.assertEqual(r.primary_bars_sha256, r.secondary_bars_sha256)
        self.assertTrue(r.distinct_declared_providers)
        self.assertTrue(r.feeds_agree_on_session_coverage)
        for field in ("provider_identity_verified", "feed_independence_verified",
                      "vendor_bar_finality_verified", "split_and_dividend_basis_verified",
                      "manual_position_reconciled", "production_signals_verified"):
            with self.subTest(field=field):
                self.assertFalse(getattr(r, field))
                self.assertFalse(r.as_json()[field])

    def test_calendar_reports_newer_complete_session_both_stale(self):
        next_day = self.expected[-1] + timedelta(days=1)
        if next_day.weekday() >= 5:
            next_day += timedelta(days=7-next_day.weekday())
        with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
            self.audit(expected=self.expected+[next_day])

    def test_extra_uncompleted_session_in_either_feed_fails_closed(self):
        with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
            self.audit(expected=self.expected[:-1])
        with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
            self.audit(secondary=self.secondary[:-1])

    def test_missing_internal_primary_or_secondary_session_fails(self):
        with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
            self.audit(primary=self.primary[:36]+self.primary[37:])
        with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
            self.audit(secondary=self.secondary[:36]+self.secondary[37:])

    def test_altered_ohlc_in_second_feed_fails(self):
        for field, change in (("open", .07), ("high", .07),
                              ("low", -.07), ("close", .07)):
            with self.subTest(field=field):
                modified = self.secondary[:]
                bar = modified[22]
                modified[22] = replace(bar, **{field: getattr(bar, field) + change})
                with self.assertRaisesRegex(MonitorDataError, "Feed OHLC mismatch"):
                    self.audit(secondary=modified)

    def test_small_ohlc_difference_is_not_source_finality(self):
        modified = self.secondary[:]
        bar = modified[22]
        modified[22] = replace(bar, close=bar.close + .0001)
        result = self.audit(secondary=modified)
        self.assertNotEqual(result.primary_bars_sha256, result.secondary_bars_sha256)
        self.assertGreater(result.max_relative_ohlc_difference, 0)
        self.assertFalse(result.vendor_bar_finality_verified)

    def test_invalid_provider_independence_claims_do_not_pass(self):
        with self.assertRaisesRegex(MonitorDataError, "Distinct claimed providers"):
            self.audit(second=self.declaration(self.secondary,
                                              provider="fabricated_a"))

    def test_wrong_source_symbol_stopped(self):
        with self.assertRaisesRegex(MonitorDataError, "Feed symbol"):
            self.audit(second=self.declaration(self.secondary, "FABRICATED_B",
                                              symbol="VYM"))

    def test_bad_self_declared_completed_through_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "Secondary claimed completed-through"):
            self.audit(second=self.declaration(self.secondary,
                          "FABRICATED_B", through=self.expected[-2]))

    def test_dividend_adjusted_feed_not_interchangeable(self):
        with self.assertRaisesRegex(MonitorDataError, "SPLIT_ONLY"):
            self.declaration(self.primary, adjustment="DIVIDEND_ADJUSTED")

    def test_naive_clock_and_absent_feeds_fail_closed(self):
        with self.assertRaisesRegex(MonitorDataError, "timezone-aware"):
            self.audit(as_of=datetime(2040, 5, 10, 23, 30))
        with self.assertRaisesRegex(MonitorDataError, "both complete"):
            self.audit(primary=[], first=self.declaration(self.primary))

    def test_missing_calendar_or_unsupported_session_blocks_all(self):
        with patch("schd_monitor.session_feed_gate.completed_xnys_sessions",
                   side_effect=MonitorDataError("calendar unavailable")):
            with self.assertRaisesRegex(MonitorDataError, "calendar unavailable"):
                audit_offline_completed_dual_feeds(
                    primary=self.primary, secondary=self.secondary,
                    primary_declaration=self.declaration(self.primary),
                    secondary_declaration=self.declaration(self.secondary, "FABRICATED_B"),
                    expected_symbol="SCHD", as_of=self.as_of)


if __name__ == "__main__":
    unittest.main()
