"""Fictional-only publication/provenance claim chronology and SHA tests."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from schd_monitor.feed_integrity import FeedDeclaration, _bar_digest
from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.publication_claim_gate import (
    PublicationClaim, check_declared_publication_gate,
)


def fictional_history() -> list[Bar]:
    rows = []
    day = date(2040, 1, 2)
    while len(rows) < 90:
        if day.weekday() < 5:
            value = 80.0 + len(rows) * .02
            rows.append(Bar(day, value, value + .8, value - .7, value))
        day += timedelta(days=1)
    return rows


class PublicationClaimGateTests(unittest.TestCase):
    def setUp(self):
        self.a = fictional_history()
        self.b = list(self.a)
        self.last = self.a[-1].session
        self.as_of = datetime(2040, 5, 10, 23, 0, tzinfo=timezone.utc)
        self.publication = datetime(2040, 5, 8, 22, 0, tzinfo=timezone.utc)
        self.capture = datetime(2040, 5, 9, 0, 0, tzinfo=timezone.utc)
        self.da = FeedDeclaration("MOCK_A", "SCHD", "SPLIT_ONLY", self.last)
        self.db = FeedDeclaration("MOCK_B", "SCHD", "SPLIT_ONLY", self.last)

    def claim(self, bars, provider, lineage):
        return PublicationClaim(provider=provider, source_lineage=lineage,
            symbol="SCHD", adjustment="SPLIT_ONLY", last_session=self.last,
            bars_sha256=_bar_digest(bars), published_at=self.publication,
            captured_at=self.capture, claimed_final=True)

    def check(self, a=None, b=None, ca=None, cb=None, as_of=None,
              expected=None, completed=True):
        a = self.a if a is None else a
        b = self.b if b is None else b
        ca = self.claim(a, "MOCK_A", "UPSTREAM_A") if ca is None else ca
        cb = self.claim(b, "MOCK_B", "UPSTREAM_B") if cb is None else cb
        dates = [row.session for row in self.a] if expected is None else expected
        clock = self.as_of if as_of is None else as_of

        def fake_calendar(first, as_of):
            if first == self.a[0].session:
                return dates
            if first == self.last:
                return [self.last] if completed else []
            raise AssertionError("Unexpected calendar request")
        with patch("schd_monitor.session_feed_gate.completed_xnys_sessions",
                   side_effect=fake_calendar), patch(
                   "schd_monitor.publication_claim_gate.completed_xnys_sessions",
                   side_effect=fake_calendar):
            return check_declared_publication_gate(
                primary=a, secondary=b, primary_declaration=self.da,
                secondary_declaration=self.db, primary_claim=ca,
                secondary_claim=cb, expected_symbol="SCHD", as_of=clock,
            )

    def test_consistent_claims_stay_explicitly_unattested(self):
        report = self.check()
        self.assertEqual(report.status,
                         "PUBLICATION_AND_PROVENANCE_CLAIMS_CONSISTENT_UNATTESTED")
        self.assertEqual(report.complete_sessions_compared, 90)
        self.assertEqual(report.checked_declared_sources, 2)
        self.assertEqual(report.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertTrue(report.snapshot_fingerprints_bound_to_bars)
        self.assertTrue(report.publication_timestamps_after_scheduled_completion)
        for flag in ("provider_identity_authenticated",
                     "underlying_vendor_independence_authenticated",
                     "publication_timestamps_authenticated",
                     "vendor_bar_finality_verified",
                     "corporate_action_basis_verified",
                     "manual_fill_reconciled",
                     "production_signals_verified"):
            with self.subTest(flag=flag):
                self.assertFalse(getattr(report, flag))
                self.assertFalse(report.as_json()[flag])

    def test_requires_source_specific_bar_sha_binding(self):
        incorrect = replace(self.claim(self.a, "MOCK_A", "UPSTREAM_A"),
                            bars_sha256="a"*64)
        with self.assertRaisesRegex(MonitorDataError, "not bound"):
            self.check(ca=incorrect)

    def test_no_same_underlying_lineage_even_with_distinct_provider_names(self):
        same = replace(self.claim(self.b, "MOCK_B", "UPSTREAM_B"),
                       source_lineage="upstream_a".upper())
        with self.assertRaisesRegex(MonitorDataError, "Duplicate"):
            self.check(cb=same)

    def test_publication_and_capture_must_be_chronological(self):
        a = self.claim(self.a, "MOCK_A", "UPSTREAM_A")
        cases = [
            replace(a, published_at=self.capture + timedelta(minutes=1)),
            replace(a, captured_at=self.publication - timedelta(minutes=1)),
            replace(a, captured_at=self.as_of + timedelta(minutes=1)),
        ]
        for incorrect in cases:
            with self.subTest(incorrect=incorrect):
                with self.assertRaisesRegex(MonitorDataError, "chronology"):
                    self.check(ca=incorrect)

    def test_before_close_or_before_buffer_claim_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "before scheduled"):
            self.check(completed=False)

    def test_claims_must_match_declared_provider_symbol_and_session(self):
        claim = self.claim(self.a, "MOCK_A", "UPSTREAM_A")
        for altered in (
            replace(claim, provider="MOCK_C"),
            replace(claim, symbol="DGRO"),
            replace(claim, last_session=self.last - timedelta(days=1)),
        ):
            with self.subTest(altered=altered):
                with self.assertRaisesRegex(MonitorDataError, "not bound"):
                    self.check(ca=altered)

    def test_invalid_claimed_final_status_fails_closed(self):
        claim = self.claim(self.a, "MOCK_A", "UPSTREAM_A")
        for bad in (False, 1, "True"):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(MonitorDataError, "claimed final"):
                    replace(claim, claimed_final=bad)

    def test_reject_unsupported_version_checksum_or_source_id(self):
        claim = self.claim(self.a, "MOCK_A", "UPSTREAM_A")
        for field, value in (
            ("schema_version", 2), ("schema_version", True),
            ("bars_sha256", "ABC"), ("source_lineage", ""),
            ("source_lineage", "ALPACA https://key=secret"),
            ("provider", "PROVIDER/OTHER"), ("symbol", "invalid space"),
            ("adjustment", "DIVIDEND_ADJUSTED"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(MonitorDataError):
                    replace(claim, **{field: value})

    def test_naive_datetimes_rejected(self):
        claim = self.claim(self.a, "MOCK_A", "UPSTREAM_A")
        with self.assertRaisesRegex(MonitorDataError, "aware"):
            replace(claim, published_at=datetime(2040, 5, 8))
        with self.assertRaisesRegex(MonitorDataError, "aware"):
            replace(claim, captured_at=datetime(2040, 5, 8))
        with self.assertRaisesRegex(MonitorDataError, "aware"):
            self.check(as_of=datetime(2040, 5, 10))

    def test_evidence_is_still_rejected_if_session_calendar_stale(self):
        wrong_dates = [row.session for row in self.a][:-1]
        with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
            self.check(expected=wrong_dates)

    def test_changed_ohlc_without_new_bound_hash_is_rejected(self):
        changed = list(self.b)
        changed[35] = replace(changed[35], close=changed[35].close + .0005)
        original_claim = self.claim(self.b, "MOCK_B", "UPSTREAM_B")
        with self.assertRaisesRegex(MonitorDataError, "not bound"):
            self.check(b=changed, cb=original_claim)

    def test_bad_feed_comparison_blocks_even_matching_declarations(self):
        changed = list(self.b)
        changed[35] = replace(changed[35], close=changed[35].close + .08)
        with self.assertRaisesRegex(MonitorDataError, "Feed OHLC mismatch"):
            self.check(b=changed)

    def test_rejects_untyped_or_missing_publication_claims(self):
        with self.assertRaisesRegex(MonitorDataError, "Two typed"):
            self.check(ca={"provider": "MOCK_A"})


if __name__ == "__main__":
    unittest.main()
