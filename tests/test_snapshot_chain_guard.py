"""Entirely fictional multi-capture revision audit, no HTTP or private data."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from schd_monitor.feed_integrity import _bar_digest
from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.publication_claim_gate import PublicationClaim
from schd_monitor.snapshot_chain_guard import audit_unrevised_snapshot_chain
from schd_monitor.snapshot_revision_guard import (
    HistorySnapshot, check_unrevised_history,
)

UTC = timezone.utc


def invented_bars(count: int = 70) -> tuple[Bar, ...]:
    rows = []
    current = date(2038, 1, 4)
    while len(rows) < count:
        if current.weekday() < 5:
            px = 90 + .125 * len(rows)
            rows.append(Bar(current, px, px + .6, px - .7, px + .05))
        current += timedelta(days=1)
    return tuple(rows)


def captured(bars: tuple[Bar, ...], day: int = 0, *,
             provider: str = "FICTIONAL_A", lineage: str = "FICTIONAL_STREAM"):
    stamp = datetime(2038, 9, 1, 12, 0, tzinfo=UTC) + timedelta(days=day)
    claim = PublicationClaim(
        provider=provider, source_lineage=lineage, symbol="MOCK",
        adjustment="SPLIT_ONLY", last_session=bars[-1].session,
        bars_sha256=_bar_digest(list(bars)),
        published_at=stamp - timedelta(hours=1),
        captured_at=stamp, claimed_final=True,
    )
    return HistorySnapshot(bars, claim)


class OfflineSnapshotChainTests(unittest.TestCase):
    def test_multiple_unrevised_captures_and_appends_still_unverified(self):
        a = captured(invented_bars(70), 0)
        b = captured(invented_bars(70), 1)
        c = captured(invented_bars(73), 2)
        result = audit_unrevised_snapshot_chain(snapshots=(a, b, c))
        self.assertEqual(result.status, "UNREVISED_SNAPSHOT_CHAIN_UNATTESTED")
        self.assertEqual(result.snapshots_checked, 3)
        self.assertEqual(result.adjacent_links_checked, 2)
        self.assertEqual(result.sessions_appended_over_chain, 3)
        self.assertEqual(result.first_sessions, 70)
        self.assertEqual(result.last_sessions, 73)
        self.assertEqual(len(result.chain_metadata_sha256), 64)
        self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(result.source_authentication_verified)
        self.assertFalse(result.vendor_publication_finality_verified)
        self.assertFalse(result.production_signals_verified)
        self.assertNotIn("bars", result.as_json())
        self.assertEqual(result.as_json()["first_bars_sha256"], a.claim.bars_sha256)

    def test_revised_intermediate_new_day_that_later_reverts_is_detected(self):
        # First vs last alone appears valid because both preserve the first
        # 70 sessions. Intermediate capture changed session 71; later capture
        # reverted it, so checking every consecutive capture is essential.
        a = captured(invented_bars(70), 0)
        mid = list(invented_bars(71))
        mid[70] = replace(mid[70], close=mid[70].close + .1)
        b = captured(tuple(mid), 1)
        c = captured(invented_bars(72), 2)
        check_unrevised_history(prior=a, newer=c)  # Would miss the revision.
        with self.assertRaisesRegex(MonitorDataError, "Historical OHLC revision"):
            audit_unrevised_snapshot_chain(snapshots=(a, b, c))

    def test_missing_middle_capture_cannot_be_verified_by_chain_checksum(self):
        a = captured(invented_bars(70), 0)
        b = captured(invented_bars(71), 1)
        c = captured(invented_bars(72), 2)
        complete = audit_unrevised_snapshot_chain(snapshots=(a, b, c))
        incomplete = audit_unrevised_snapshot_chain(snapshots=(a, c))
        # A valid shorter chain is not proof that no captures were omitted.
        self.assertNotEqual(complete.chain_metadata_sha256,
                            incomplete.chain_metadata_sha256)
        self.assertEqual(incomplete.snapshots_checked, 2)
        self.assertFalse(incomplete.production_signals_verified)

    def test_out_of_order_and_equal_capture_instants_fail(self):
        a = captured(invented_bars(70), 0)
        b = captured(invented_bars(71), 1)
        c = captured(invented_bars(72), 2)
        for snapshots in ((a, c, b), (a, b, b)):
            with self.subTest(order=[x.claim.captured_at.isoformat() for x in snapshots]):
                with self.assertRaisesRegex(MonitorDataError, "capture chronology"):
                    audit_unrevised_snapshot_chain(snapshots=snapshots)

    def test_mid_chain_lineage_drift_fails_even_if_final_matches_original(self):
        a = captured(invented_bars(70), 0)
        b = captured(invented_bars(71), 1, lineage="DIFFERENT_UPSTREAM")
        c = captured(invented_bars(72), 2)
        with self.assertRaisesRegex(MonitorDataError, "source identity"):
            audit_unrevised_snapshot_chain(snapshots=(a, b, c))

    def test_metadata_hash_uses_utc_instants_not_offset_presentation(self):
        a = captured(invented_bars(70), 0)
        b = captured(invented_bars(70), 1)
        dubai = timezone(timedelta(hours=4))
        converted_claim = replace(
            b.claim,
            published_at=b.claim.published_at.astimezone(dubai),
            captured_at=b.claim.captured_at.astimezone(dubai),
        )
        converted = HistorySnapshot(b.bars, converted_claim)
        first = audit_unrevised_snapshot_chain(snapshots=(a, b))
        identical = audit_unrevised_snapshot_chain(snapshots=(a, converted))
        self.assertEqual(first.chain_metadata_sha256, identical.chain_metadata_sha256)
        shifted_claim = replace(b.claim, captured_at=b.claim.captured_at + timedelta(seconds=1))
        moved = HistorySnapshot(b.bars, shifted_claim)
        changed = audit_unrevised_snapshot_chain(snapshots=(a, moved))
        self.assertNotEqual(first.chain_metadata_sha256, changed.chain_metadata_sha256)

    def test_refuse_empty_single_mutable_and_untyped_chains(self):
        a = captured(invented_bars(70), 0)
        for invalid in ((), (a,), [a, a], (a, {})):
            with self.subTest(invalid=str(type(invalid))):
                with self.assertRaises(MonitorDataError):
                    audit_unrevised_snapshot_chain(snapshots=invalid)


if __name__ == "__main__":
    unittest.main()
