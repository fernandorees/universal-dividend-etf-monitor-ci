"""Local-only exclusive receipt tests with invented OHLC and temp folders."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from schd_monitor.feed_integrity import _bar_digest
from schd_monitor.local_snapshot_receipt import (
    MAX_LOCAL_RECEIPT_BYTES,
    verify_local_snapshot_chain_receipt,
    write_local_snapshot_chain_receipt,
)
from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.publication_claim_gate import PublicationClaim
from schd_monitor.snapshot_revision_guard import HistorySnapshot

UTC = timezone.utc


def fictional_bars(n: int = 70) -> tuple[Bar, ...]:
    days = []
    day = date(2038, 1, 4)
    while len(days) < n:
        if day.weekday() < 5:
            value = 90 + len(days) * 0.125
            days.append(Bar(day, value, value + .6, value - .7, value + .05))
        day += timedelta(days=1)
    return tuple(days)


def snapshot(bars: tuple[Bar, ...], day: int) -> HistorySnapshot:
    stamp = datetime(2038, 9, 1, 12, tzinfo=UTC) + timedelta(days=day)
    claim = PublicationClaim(
        provider="SYNTHETIC_A", source_lineage="SYNTHETIC_STREAM",
        symbol="MOCK", adjustment="SPLIT_ONLY",
        last_session=bars[-1].session,
        bars_sha256=_bar_digest(list(bars)),
        published_at=stamp - timedelta(hours=1),
        captured_at=stamp, claimed_final=True,
    )
    return HistorySnapshot(bars, claim)


def good_chain() -> tuple[HistorySnapshot, ...]:
    return (snapshot(fictional_bars(70), 0),
            snapshot(fictional_bars(72), 1),
            snapshot(fictional_bars(74), 2))


class LocalSnapshotReceiptTests(unittest.TestCase):
    def test_exclusive_local_receipt_roundtrip_is_only_unverified_metadata(self):
        captures = good_chain()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fictional_audit.json"
            self.assertFalse(path.exists())
            written = write_local_snapshot_chain_receipt(snapshots=captures, path=path)
            checked = verify_local_snapshot_chain_receipt(snapshots=captures, path=path)
            self.assertEqual(written, checked)
            self.assertEqual(written.snapshots_checked, 3)
            self.assertEqual(written.adjacent_links_checked, 2)
            self.assertEqual(written.trading_status, "UNVERIFIED_DO_NOT_TRADE")
            self.assertFalse(written.production_signals_verified)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload, written.as_json())
            self.assertEqual(payload["chain_metadata_sha256"], written.chain_metadata_sha256)
            self.assertNotIn("bars", payload)
            self.assertNotIn("open", payload)
            self.assertNotIn("close", payload)
            self.assertNotIn("shares", payload)
            self.assertNotIn("cash", payload)
            self.assertNotIn("ohlc", path.read_text(encoding="utf-8").lower())

    def test_existing_file_not_overwritten_even_if_bytes_differ(self):
        captures = good_chain()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "do_not_replace.json"
            path.write_bytes(b"operator's existing private file")
            with self.assertRaisesRegex(MonitorDataError, "never overwritten"):
                write_local_snapshot_chain_receipt(snapshots=captures, path=path)
            self.assertEqual(path.read_bytes(), b"operator's existing private file")

    def test_repeated_write_refused_after_success(self):
        captures = good_chain()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "once.json"
            write_local_snapshot_chain_receipt(snapshots=captures, path=path)
            before = path.read_bytes()
            with self.assertRaises(MonitorDataError):
                write_local_snapshot_chain_receipt(snapshots=captures, path=path)
            self.assertEqual(path.read_bytes(), before)

    def test_changed_receipt_fails_exact_readback(self):
        captures = good_chain()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "changed.json"
            write_local_snapshot_chain_receipt(snapshots=captures, path=path)
            edited = path.read_text(encoding="utf-8").replace(
                "UNVERIFIED_DO_NOT_TRADE", "VERIFIED_OK_TO_TRADE"
            )
            path.write_text(edited, encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "mismatch"):
                verify_local_snapshot_chain_receipt(snapshots=captures, path=path)

    def test_truncated_or_oversize_receipt_is_rejected(self):
        captures = good_chain()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "truncated.json"
            write_local_snapshot_chain_receipt(snapshots=captures, path=path)
            body = path.read_bytes()
            for content in (body[:20], body + b"X" * MAX_LOCAL_RECEIPT_BYTES):
                with self.subTest(size=len(content)):
                    path.write_bytes(content)
                    with self.assertRaisesRegex(MonitorDataError, "mismatch"):
                        verify_local_snapshot_chain_receipt(snapshots=captures, path=path)

    def test_changed_input_chain_does_not_match_saved_receipt(self):
        captures = good_chain()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "baseline.json"
            write_local_snapshot_chain_receipt(snapshots=captures, path=path)
            different = (captures[0], captures[2])
            with self.assertRaisesRegex(MonitorDataError, "mismatch"):
                verify_local_snapshot_chain_receipt(snapshots=different, path=path)
            self.assertEqual(
                verify_local_snapshot_chain_receipt(snapshots=captures, path=path)
                .snapshots_checked, 3
            )

    def test_invalid_source_chain_fails_before_creating_any_receipt(self):
        captures = good_chain()
        rows = list(fictional_bars(72))
        rows[70] = replace(rows[70], close=rows[70].close + .05)
        invalid_middle = snapshot(tuple(rows), 1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "should_not_exist.json"
            with self.assertRaisesRegex(MonitorDataError, "Historical OHLC revision"):
                write_local_snapshot_chain_receipt(
                    snapshots=(captures[0], invalid_middle, captures[2]), path=path
                )
            self.assertFalse(path.exists())

    def test_nonexistent_parent_or_receipt_fails_without_creating_directories(self):
        captures = good_chain()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "not_created" / "receipt.json"
            with self.assertRaisesRegex(MonitorDataError, "not written"):
                write_local_snapshot_chain_receipt(snapshots=captures, path=path)
            self.assertFalse(path.parent.exists())
            with self.assertRaisesRegex(MonitorDataError, "unavailable"):
                verify_local_snapshot_chain_receipt(snapshots=captures, path=path)


if __name__ == "__main__":
    unittest.main()
