"""Entirely fictional offline tests: revisions fail closed, no live data."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from schd_monitor.feed_integrity import _bar_digest
from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.publication_claim_gate import PublicationClaim
from schd_monitor.snapshot_revision_guard import (
    HistorySnapshot, check_unrevised_history,
)

UTC = timezone.utc


def fictional_bars(n: int = 70) -> tuple[Bar, ...]:
    rows = []
    current = date(2038, 1, 4)
    while len(rows) < n:
        if current.weekday() < 5:
            px = 70.0 + .125 * len(rows)
            rows.append(Bar(current, px + .05, px + .6, px - .7, px))
        current += timedelta(days=1)
    return tuple(rows)


def make_snapshot(bars, *, days_after: int = 0,
                  provider: str = "MOCK_A", lineage: str = "UPSTREAM_A",
                  symbol: str = "SCHD", adjustment: str = "SPLIT_ONLY"):
    stamp = datetime(2038, 9, 1, 0, 0, tzinfo=UTC) + timedelta(days=days_after)
    claim = PublicationClaim(
        provider=provider, source_lineage=lineage, symbol=symbol,
        adjustment=adjustment, last_session=bars[-1].session,
        bars_sha256=_bar_digest(list(bars)),
        published_at=stamp - timedelta(minutes=60),
        captured_at=stamp, claimed_final=True,
    )
    return HistorySnapshot(tuple(bars), claim)


class OfflineRevisionGuardTests(unittest.TestCase):
    def setUp(self):
        self.original = fictional_bars()

    def test_unchanged_second_capture_never_implies_verified_signal(self):
        x = make_snapshot(self.original, days_after=0)
        y = make_snapshot(self.original, days_after=1)
        result = check_unrevised_history(prior=x, newer=y)
        self.assertEqual(result.status,
                         "HISTORICAL_SNAPSHOT_UNCHANGED_OR_EXTENDED_UNATTESTED")
        self.assertEqual(result.new_sessions_appended, 0)
        self.assertEqual(result.unchanged_prior_sessions, 70)
        self.assertEqual(result.prior_sha256, result.newer_sha256)
        self.assertEqual(result.historical_revisions_detected, 0)
        self.assertFalse(result.source_authentication_verified)
        self.assertFalse(result.vendor_publication_finality_verified)
        self.assertFalse(result.production_signals_verified)
        self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(result.as_json()["production_signals_verified"])

    def test_new_completed_sessions_can_append_without_revision(self):
        all_rows = fictional_bars(74)
        x = make_snapshot(all_rows[:70], days_after=0)
        y = make_snapshot(all_rows, days_after=1)
        result = check_unrevised_history(prior=x, newer=y)
        self.assertEqual(result.unchanged_prior_sessions, 70)
        self.assertEqual(result.new_sessions_appended, 4)
        self.assertEqual(result.newer_sessions, 74)
        self.assertNotEqual(result.prior_sha256, result.newer_sha256)

    def test_any_modified_past_ohlc_price_fails_closed_even_tiny_change(self):
        original = self.original
        for field in ("open", "high", "low", "close"):
            with self.subTest(field=field):
                revised = list(original)
                prev = revised[26]
                revised[26] = replace(prev, **{field: getattr(prev, field) + 1e-8})
                with self.assertRaisesRegex(MonitorDataError,
                                            "Historical OHLC revision"):
                    check_unrevised_history(
                        prior=make_snapshot(original),
                        newer=make_snapshot(tuple(revised), days_after=1),
                    )

    def test_changed_old_ohlc_detected_even_when_new_sessions_appended(self):
        original = self.original
        extended = list(fictional_bars(73))
        extended[7] = replace(extended[7], close=extended[7].close + .01)
        with self.assertRaisesRegex(MonitorDataError, "Historical OHLC revision"):
            check_unrevised_history(
                prior=make_snapshot(original),
                newer=make_snapshot(tuple(extended), days_after=1))

    def test_missing_old_session_or_reordered_input_fails(self):
        original = self.original
        missing = original[:21] + original[22:]
        with self.assertRaises(MonitorDataError):
            check_unrevised_history(prior=make_snapshot(original),
                                    newer=make_snapshot(missing, days_after=1))
        reordered = original[:20] + (original[21], original[20]) + original[22:]
        with self.assertRaises(MonitorDataError):
            make_snapshot(reordered)

    def test_inserted_session_at_start_or_middle_not_accepted(self):
        original = self.original
        earlier = Bar(date(2038, 1, 1), 70, 71, 69, 70)
        inserted = (earlier,) + original
        with self.assertRaisesRegex(MonitorDataError, "index changed"):
            check_unrevised_history(
                prior=make_snapshot(original),
                newer=make_snapshot(inserted, days_after=1))

    def test_provider_lineage_instrument_or_basis_must_not_change(self):
        x = make_snapshot(self.original)
        for opts in (
            {"provider": "MOCK_B"},
            {"lineage": "UPSTREAM_B"},
            {"symbol": "VYM"},
        ):
            with self.subTest(**opts):
                with self.assertRaisesRegex(MonitorDataError, "source identity"):
                    check_unrevised_history(
                        prior=x, newer=make_snapshot(self.original, days_after=1, **opts))

    def test_equal_or_reversed_capture_time_fails_closed(self):
        prior = make_snapshot(self.original)
        for days_after in (0, -1):
            with self.subTest(days_after=days_after):
                with self.assertRaisesRegex(MonitorDataError, "chronology"):
                    check_unrevised_history(
                        prior=prior,
                        newer=make_snapshot(self.original, days_after=days_after))

    def test_changed_bar_with_stale_original_sha_rejected_at_snapshot_creation(self):
        prior = make_snapshot(self.original)
        corrected = list(self.original)
        corrected[11] = replace(corrected[11], high=corrected[11].high + .5)
        with self.assertRaisesRegex(MonitorDataError, "fingerprint"):
            HistorySnapshot(tuple(corrected), prior.claim)

    def test_claimed_session_mismatch_fails_before_history_compare(self):
        prior = make_snapshot(self.original)
        wrong = replace(prior.claim, last_session=self.original[-2].session)
        with self.assertRaisesRegex(MonitorDataError, "last-session"):
            HistorySnapshot(self.original, wrong)

    def test_empty_or_untyped_snapshots_fail_closed(self):
        x = make_snapshot(self.original)
        with self.assertRaises(MonitorDataError):
            HistorySnapshot((), x.claim)
        with self.assertRaises(MonitorDataError):
            HistorySnapshot(list(self.original), x.claim)
        with self.assertRaisesRegex(MonitorDataError, "typed"):
            check_unrevised_history(prior=x, newer={})

    def test_snapshot_rejects_publication_claimed_after_its_capture(self):
        snapshot = make_snapshot(self.original)
        future = replace(
            snapshot.claim,
            published_at=snapshot.claim.captured_at + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(MonitorDataError, "publication/capture chronology"):
            HistorySnapshot(self.original, future)

    def test_snapshot_compares_publication_and_capture_as_utc_instants(self):
        snapshot = make_snapshot(self.original)
        dubai = timezone(timedelta(hours=4))
        # Same instant in UTC and Dubai; equality must remain admissible.
        matching = replace(
            snapshot.claim,
            published_at=snapshot.claim.captured_at.astimezone(dubai),
        )
        self.assertEqual(
            HistorySnapshot(self.original, matching).claim.bars_sha256,
            snapshot.claim.bars_sha256,
        )
        # The display clock is local Dubai, but the actual publication
        # instant is now later than capture; fail regardless of offset.
        future = replace(
            snapshot.claim,
            published_at=(snapshot.claim.captured_at + timedelta(seconds=1))
            .astimezone(dubai),
        )
        with self.assertRaisesRegex(MonitorDataError, "publication/capture chronology"):
            HistorySnapshot(self.original, future)

    def test_prior_snapshot_is_immutable_and_not_modified(self):
        prev = make_snapshot(self.original)
        curr = make_snapshot(fictional_bars(72), days_after=1)
        before = prev.claim.bars_sha256
        check_unrevised_history(prior=prev, newer=curr)
        self.assertEqual(prev.claim.bars_sha256, before)
        self.assertEqual(len(prev.bars), 70)


if __name__ == "__main__":
    unittest.main()
