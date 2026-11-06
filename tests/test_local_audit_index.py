"""Synthetic-only, local temp-file SQLite audit index regression tests."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from schd_monitor.feed_integrity import _bar_digest
from schd_monitor.local_audit_index import (
    append_local_snapshot_audit_index,
    backup_local_snapshot_audit_index,
    create_local_snapshot_audit_index,
    verify_local_snapshot_audit_backup_prefix,
    verify_local_snapshot_audit_index,
)
from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.publication_claim_gate import PublicationClaim
from schd_monitor.snapshot_revision_guard import HistorySnapshot

UTC = timezone.utc


def invented_bars(n=75) -> tuple[Bar, ...]:
    rows = []
    day = date(2038, 1, 4)
    while len(rows) < n:
        if day.weekday() < 5:
            px = 75 + len(rows) * .25
            rows.append(Bar(day, px, px + 1, px - 1, px + .1))
        day += timedelta(days=1)
    return tuple(rows)


def capture(bars: tuple[Bar, ...], day: int) -> HistorySnapshot:
    stamp = datetime(2038, 9, 1, 12, tzinfo=UTC) + timedelta(days=day)
    claim = PublicationClaim(
        provider="FICTIONAL_DATA", source_lineage="FICTIONAL_STREAM",
        symbol="MOCK", adjustment="SPLIT_ONLY", last_session=bars[-1].session,
        bars_sha256=_bar_digest(list(bars)),
        published_at=stamp - timedelta(hours=1),
        captured_at=stamp, claimed_final=True,
    )
    return HistorySnapshot(bars, claim)


def examples():
    return (capture(invented_bars(70), 0),
            capture(invented_bars(72), 1),
            capture(invented_bars(75), 2))


class LocalAuditIndexTests(unittest.TestCase):
    def test_create_empty_index_and_verify_without_production_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            result = create_local_snapshot_audit_index(path=path)
            self.assertTrue(path.is_file())
            self.assertEqual(result.entries_checked, 0)
            self.assertEqual(result.last_snapshots_checked, 0)
            self.assertEqual(result.last_entry_sha256, "0" * 64)
            self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")
            self.assertFalse(result.production_signals_verified)
            self.assertFalse(result.provider_identity_verified)
            self.assertEqual(verify_local_snapshot_audit_index(path=path), result)

    def test_two_cumulative_entries_survive_reopen_and_contain_only_metadata(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            create_local_snapshot_audit_index(path=path)
            first = append_local_snapshot_audit_index(snapshots=(a, b), path=path)
            self.assertEqual(first.entries_checked, 1)
            self.assertEqual(first.last_snapshots_checked, 2)
            second = append_local_snapshot_audit_index(snapshots=(a, b, c), path=path)
            self.assertEqual(second.entries_checked, 2)
            self.assertEqual(second.last_snapshots_checked, 3)
            self.assertNotEqual(first.last_entry_sha256, second.last_entry_sha256)
            self.assertEqual(verify_local_snapshot_audit_index(path=path), second)
            with closing(sqlite3.connect(path)) as db, db:
                rows = db.execute("SELECT audit_json FROM audit_entries ORDER BY sequence").fetchall()
            self.assertEqual(len(rows), 2)
            for (audit_json,) in rows:
                self.assertNotIn('"open":', audit_json)
                self.assertNotIn('"close":', audit_json)
                self.assertNotIn('"shares":', audit_json)
                self.assertNotIn('"cash":', audit_json)
                self.assertNotIn('"bars":', audit_json)
                self.assertIn('UNVERIFIED_DO_NOT_TRADE', audit_json)

    def test_replay_and_shortened_captures_do_not_append(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            create_local_snapshot_audit_index(path=path)
            before = append_local_snapshot_audit_index(snapshots=(a, b), path=path)
            for snapshots in ((a, b), (a,)):
                with self.subTest(length=len(snapshots)):
                    with self.assertRaises(MonitorDataError):
                        append_local_snapshot_audit_index(snapshots=snapshots, path=path)
                    self.assertEqual(verify_local_snapshot_audit_index(path=path), before)
            after = append_local_snapshot_audit_index(snapshots=(a, b, c), path=path)
            self.assertEqual(after.entries_checked, 2)

    def test_rewritten_valid_historical_prefix_rejected_before_insert(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            create_local_snapshot_audit_index(path=path)
            baseline = append_local_snapshot_audit_index(snapshots=(a, b), path=path)
            # A changed first captured OHLC value is made consistent in ALL
            # later captures, so the internal new snapshot chain itself passes.
            def revised(snapshot):
                rows = list(snapshot.bars)
                rows[5] = replace(rows[5], close=rows[5].close + .025)
                return capture(tuple(rows), (snapshot.claim.captured_at -
                    datetime(2038, 9, 1, 12, tzinfo=UTC)).days)
            changed = tuple(revised(s) for s in (a, b, c))
            with self.assertRaisesRegex(MonitorDataError, "previous capture prefix changed"):
                append_local_snapshot_audit_index(snapshots=changed, path=path)
            self.assertEqual(verify_local_snapshot_audit_index(path=path), baseline)

    def test_omitted_intermediate_capture_cannot_extend_old_index(self):
        a, b, c = examples()
        d = capture(invented_bars(76), 3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            create_local_snapshot_audit_index(path=path)
            baseline = append_local_snapshot_audit_index(snapshots=(a, b), path=path)
            with self.assertRaisesRegex(MonitorDataError, "previous capture prefix changed"):
                append_local_snapshot_audit_index(snapshots=(a, c, d), path=path)
            self.assertEqual(verify_local_snapshot_audit_index(path=path), baseline)

    def test_corrupt_entry_blocks_readback_and_new_appends(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            create_local_snapshot_audit_index(path=path)
            append_local_snapshot_audit_index(snapshots=(a, b), path=path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE audit_entries SET entry_sha256=? WHERE sequence=1",
                           ("f" * 64,))
            with self.assertRaisesRegex(MonitorDataError, "digest"):
                verify_local_snapshot_audit_index(path=path)
            with self.assertRaisesRegex(MonitorDataError, "digest"):
                append_local_snapshot_audit_index(snapshots=(a, b, c), path=path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_entries").fetchone()[0], 1)

    def test_unsupported_schema_and_missing_database_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            with self.assertRaises(MonitorDataError):
                verify_local_snapshot_audit_index(path=path)
            with self.assertRaises(MonitorDataError):
                append_local_snapshot_audit_index(snapshots=examples()[:2], path=path)
            create_local_snapshot_audit_index(path=path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("PRAGMA user_version=200")
            with self.assertRaisesRegex(MonitorDataError, "version unsupported"):
                verify_local_snapshot_audit_index(path=path)

    def test_existing_file_or_wrong_extension_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing.sqlite3"
            existing.write_bytes(b"original-private-data")
            with self.assertRaisesRegex(MonitorDataError, "already exists"):
                create_local_snapshot_audit_index(path=existing)
            self.assertEqual(existing.read_bytes(), b"original-private-data")
            with self.assertRaisesRegex(MonitorDataError, "nonsymlink"):
                create_local_snapshot_audit_index(path=Path(tmp) / "invalid.txt")
            with self.assertRaisesRegex(MonitorDataError, "parent directory"):
                create_local_snapshot_audit_index(
                    path=Path(tmp) / "nonexistent_folder" / "index.sqlite3"
                )

    def test_invalid_intermediate_revision_causes_no_database_write(self):
        a, b, c = examples()
        rows = list(b.bars)
        rows[70] = replace(rows[70], close=rows[70].close + .05)
        broken = capture(tuple(rows), 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            create_local_snapshot_audit_index(path=path)
            empty = verify_local_snapshot_audit_index(path=path)
            with self.assertRaisesRegex(MonitorDataError, "Historical OHLC revision"):
                append_local_snapshot_audit_index(snapshots=(a, broken, c), path=path)
            self.assertEqual(verify_local_snapshot_audit_index(path=path), empty)


    def test_interrupted_initialization_is_not_silently_recreated(self):
        # An empty placeholder or a schema-version-only SQLite file must be
        # inspected by the operator, never overwritten or auto-repaired.
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ("empty_placeholder", "version_only"):
                with self.subTest(kind=kind):
                    path = Path(tmp) / (kind + ".sqlite3")
                    if kind == "empty_placeholder":
                        path.write_bytes(b"")
                    else:
                        with closing(sqlite3.connect(path)) as db, db:
                            db.execute("PRAGMA user_version=1")
                    before = path.read_bytes()
                    with self.assertRaises(MonitorDataError):
                        verify_local_snapshot_audit_index(path=path)
                    with self.assertRaisesRegex(MonitorDataError, "already exists"):
                        create_local_snapshot_audit_index(path=path)
                    self.assertEqual(path.read_bytes(), before)

    def test_same_columns_with_weakened_constraints_are_rejected_without_append(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fictional.sqlite3"
            create_local_snapshot_audit_index(path=path)
            append_local_snapshot_audit_index(snapshots=(a, b), path=path)
            # Keep the same column names, all rows, version and valid hashes,
            # but remove PRIMARY KEY, CHECK and NOT NULL constraints.
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("ALTER TABLE audit_entries RENAME TO old_audit_entries")
                db.execute(
                    "CREATE TABLE audit_entries ("
                    "sequence INTEGER,"
                    "prior_entry_sha256 TEXT,"
                    "audit_json TEXT,"
                    "entry_sha256 TEXT)"
                )
                db.execute(
                    "INSERT INTO audit_entries SELECT sequence, prior_entry_sha256, "
                    "audit_json, entry_sha256 FROM old_audit_entries"
                )
                db.execute("DROP TABLE old_audit_entries")
            with self.assertRaisesRegex(MonitorDataError, "schema objects or constraints"):
                verify_local_snapshot_audit_index(path=path)
            with self.assertRaisesRegex(MonitorDataError, "schema objects or constraints"):
                append_local_snapshot_audit_index(snapshots=(a, b, c), path=path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM audit_entries").fetchone()[0], 1
                )


    def test_verified_backup_copies_history_and_is_independent(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite3"
            backup = Path(tmp) / "backup.sqlite3"
            create_local_snapshot_audit_index(path=source)
            expected = append_local_snapshot_audit_index(snapshots=(a, b), path=source)
            result = backup_local_snapshot_audit_index(
                source_path=source, backup_path=backup
            )
            self.assertEqual(result, expected)
            self.assertEqual(verify_local_snapshot_audit_index(path=backup), expected)
            # Later source appends must not rewrite an earlier backup.
            updated = append_local_snapshot_audit_index(snapshots=(a, b, c), path=source)
            self.assertEqual(updated.entries_checked, 2)
            self.assertEqual(verify_local_snapshot_audit_index(path=backup), expected)
            with closing(sqlite3.connect(backup)) as db:
                record = db.execute("SELECT audit_json FROM audit_entries").fetchone()[0]
            for sensitive_field in ('"open":', '"close":', '"shares":', '"cash":'):
                self.assertNotIn(sensitive_field, record)
            self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")

    def test_empty_valid_index_can_be_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite3"
            backup = Path(tmp) / "backup.sqlite3"
            expected = create_local_snapshot_audit_index(path=source)
            self.assertEqual(
                backup_local_snapshot_audit_index(
                    source_path=source, backup_path=backup
                ), expected
            )
            self.assertEqual(verify_local_snapshot_audit_index(path=backup), expected)

    def test_existing_or_incomplete_backup_never_overwritten(self):
        a, b, _ = examples()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite3"
            create_local_snapshot_audit_index(path=source)
            original_status = append_local_snapshot_audit_index(
                snapshots=(a, b), path=source
            )
            for contents in (b"", b"fictional-private-placeholder"):
                with self.subTest(contents=contents):
                    backup = Path(tmp) / "backup.sqlite3"
                    backup.write_bytes(contents)
                    with self.assertRaisesRegex(MonitorDataError, "already exists"):
                        backup_local_snapshot_audit_index(
                            source_path=source, backup_path=backup
                        )
                    self.assertEqual(backup.read_bytes(), contents)
            with self.assertRaisesRegex(MonitorDataError, "already exists"):
                backup_local_snapshot_audit_index(
                    source_path=source, backup_path=source
                )
            self.assertEqual(verify_local_snapshot_audit_index(path=source), original_status)

    def test_corrupt_source_rejected_without_reserving_backup(self):
        a, b, _ = examples()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite3"
            backup = Path(tmp) / "backup.sqlite3"
            create_local_snapshot_audit_index(path=source)
            append_local_snapshot_audit_index(snapshots=(a, b), path=source)
            with closing(sqlite3.connect(source)) as db, db:
                db.execute("UPDATE audit_entries SET entry_sha256 = ?",
                           ("a" * 64,))
            with self.assertRaisesRegex(MonitorDataError, "digest"):
                backup_local_snapshot_audit_index(
                    source_path=source, backup_path=backup
                )
            self.assertFalse(backup.exists())


    def test_backup_prefix_preflight_identical_and_older_backup_is_read_only(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current.sqlite3"
            backup = Path(tmp) / "backup.sqlite3"
            create_local_snapshot_audit_index(path=current)
            earlier = append_local_snapshot_audit_index(snapshots=(a, b), path=current)
            backup_local_snapshot_audit_index(source_path=current, backup_path=backup)
            before_current = current.read_bytes()
            before_backup = backup.read_bytes()
            equal = verify_local_snapshot_audit_backup_prefix(
                current_path=current, backup_path=backup
            )
            self.assertEqual(equal.backup_entries_checked, 1)
            self.assertEqual(equal.current_entries_checked, 1)
            self.assertEqual(equal.backup_last_entry_sha256, earlier.last_entry_sha256)
            self.assertEqual(equal.current_last_entry_sha256, earlier.last_entry_sha256)
            self.assertFalse(equal.production_signals_verified)
            self.assertEqual(equal.trading_status, "UNVERIFIED_DO_NOT_TRADE")
            self.assertIn("UNATTESTED", equal.status)
            self.assertEqual(current.read_bytes(), before_current)
            self.assertEqual(backup.read_bytes(), before_backup)
            new = append_local_snapshot_audit_index(snapshots=(a, b, c), path=current)
            older = verify_local_snapshot_audit_backup_prefix(
                current_path=current, backup_path=backup
            )
            self.assertEqual(older.backup_entries_checked, 1)
            self.assertEqual(older.current_entries_checked, 2)
            self.assertEqual(older.current_last_entry_sha256, new.last_entry_sha256)
            self.assertEqual(older.backup_last_entry_sha256, earlier.last_entry_sha256)

    def test_backup_prefix_preflight_rejects_independently_valid_divergence(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current.sqlite3"
            backup = Path(tmp) / "backup.sqlite3"
            create_local_snapshot_audit_index(path=current)
            create_local_snapshot_audit_index(path=backup)
            append_local_snapshot_audit_index(snapshots=(a, b), path=current)
            append_local_snapshot_audit_index(snapshots=(a, c), path=backup)
            self.assertEqual(verify_local_snapshot_audit_index(path=current).entries_checked, 1)
            self.assertEqual(verify_local_snapshot_audit_index(path=backup).entries_checked, 1)
            before = (current.read_bytes(), backup.read_bytes())
            with self.assertRaisesRegex(MonitorDataError, "not an exact prefix"):
                verify_local_snapshot_audit_backup_prefix(
                    current_path=current, backup_path=backup
                )
            self.assertEqual((current.read_bytes(), backup.read_bytes()), before)

    def test_backup_prefix_preflight_rejects_backup_ahead_or_empty(self):
        a, b, c = examples()
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current.sqlite3"
            backup = Path(tmp) / "backup.sqlite3"
            create_local_snapshot_audit_index(path=current)
            create_local_snapshot_audit_index(path=backup)
            append_local_snapshot_audit_index(snapshots=(a, b), path=current)
            with self.assertRaisesRegex(MonitorDataError, "no recorded history"):
                verify_local_snapshot_audit_backup_prefix(
                    current_path=current, backup_path=backup
                )
            append_local_snapshot_audit_index(snapshots=(a, b), path=backup)
            append_local_snapshot_audit_index(snapshots=(a, b, c), path=backup)
            with self.assertRaisesRegex(MonitorDataError, "not an exact prefix"):
                verify_local_snapshot_audit_backup_prefix(
                    current_path=current, backup_path=backup
                )

    def test_backup_prefix_preflight_rejects_corrupt_or_same_file(self):
        a, b, _ = examples()
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current.sqlite3"
            backup = Path(tmp) / "backup.sqlite3"
            create_local_snapshot_audit_index(path=current)
            append_local_snapshot_audit_index(snapshots=(a, b), path=current)
            backup_local_snapshot_audit_index(source_path=current, backup_path=backup)
            with self.assertRaisesRegex(MonitorDataError, "distinct files"):
                verify_local_snapshot_audit_backup_prefix(
                    current_path=current, backup_path=current
                )
            with closing(sqlite3.connect(backup)) as db, db:
                db.execute("UPDATE audit_entries SET entry_sha256 = ?",
                           ("f" * 64,))
            with self.assertRaisesRegex(MonitorDataError, "digest"):
                verify_local_snapshot_audit_backup_prefix(
                    current_path=current, backup_path=backup
                )
            self.assertEqual(verify_local_snapshot_audit_index(path=current).entries_checked, 1)


if __name__ == "__main__":
    unittest.main()
