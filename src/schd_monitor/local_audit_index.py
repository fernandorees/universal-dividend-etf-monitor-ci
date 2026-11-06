"""Opt-in LOCAL SQLite index of unattested snapshot-chain audit metadata.

Only SHA-bound chain summaries are stored, never OHLC, fills, cash or keys.
No automatic capture, no market-data HTTP, no broker access or trading.
A hash chain checks internal consistency but is NOT an external trust anchor:
a malicious rewrite of the entire database can forge consistent hashes.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
import os
import sqlite3
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import quote

from .models import MonitorDataError
from .snapshot_chain_guard import SnapshotChainAudit, audit_unrevised_snapshot_chain
from .snapshot_revision_guard import HistorySnapshot

LOCAL_AUDIT_INDEX_SCHEMA = 1
_GENESIS = "0" * 64
_MAX_ENTRIES = 10000
_MAX_METADATA_BYTES = 8192
# Pin the exact version-1 table definition used by previously created indexes.
# Column names alone cannot detect removed PRIMARY KEY, CHECK or NOT NULL rules.
_AUDIT_TABLE_SQL = (
    "CREATE TABLE audit_entries ("
    "sequence INTEGER PRIMARY KEY CHECK(sequence > 0),"
    "prior_entry_sha256 TEXT NOT NULL,"
    "audit_json TEXT NOT NULL,"
    "entry_sha256 TEXT NOT NULL)"
)


@dataclass(frozen=True)
class LocalAuditIndexStatus:
    schema_version: int
    status: str
    entries_checked: int
    last_entry_sha256: str
    last_snapshots_checked: int
    provider_identity_verified: bool = False
    vendor_finality_verified: bool = False
    production_signals_verified: bool = False
    trading_status: str = "UNVERIFIED_DO_NOT_TRADE"

    def as_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LocalAuditBackupPrefixStatus:
    schema_version: int
    status: str
    backup_entries_checked: int
    current_entries_checked: int
    backup_last_entry_sha256: str
    current_last_entry_sha256: str
    production_signals_verified: bool = False
    trading_status: str = "UNVERIFIED_DO_NOT_TRADE"

    def as_json(self) -> dict:
        return asdict(self)


def _wire(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _digest(*, sequence: int, prior: str, audit_json: str) -> str:
    return hashlib.sha256(_wire({
        "schema_version": LOCAL_AUDIT_INDEX_SCHEMA,
        "sequence": sequence, "prior_entry_sha256": prior,
        "audit_json": audit_json,
    }).encode("utf-8")).hexdigest()


def _target(path: str | os.PathLike[str], *, create: bool) -> Path:
    try:
        target = Path(path)
        if target.suffix != ".sqlite3" or target.is_symlink():
            raise MonitorDataError("Local audit index needs a nonsymlink .sqlite3 path")
        if not target.parent.is_dir():
            raise MonitorDataError("Local audit index parent directory must already exist")
        if not create and not target.is_file():
            raise MonitorDataError("Local audit index file is absent or not regular")
        return target
    except MonitorDataError:
        raise
    except (TypeError, ValueError, OSError) as exc:
        raise MonitorDataError("Local audit index path is invalid or inaccessible") from exc


def _open_existing(target: Path, *, readonly: bool) -> sqlite3.Connection:
    # SQLite URI mode=rw/ro refuses to auto-create missing databases.
    # Resolve to an absolute file URI; quote() also escapes ? and # in paths.
    url = "file:" + quote(target.resolve().as_posix(), safe="/:") + (
        "?mode=ro" if readonly else "?mode=rw"
    )
    conn = sqlite3.connect(url, uri=True, timeout=5.0)
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def _read_checked(conn: sqlite3.Connection) -> list[tuple[int, dict, str]]:
    if conn.execute("PRAGMA user_version").fetchone()[0] != LOCAL_AUDIT_INDEX_SCHEMA:
        raise MonitorDataError("Local audit index version unsupported")
    # A matching name/column list is insufficient: constraints may have
    # been dropped after an interrupted repair or incompatible migration.
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    if objects != [("table", "audit_entries", _AUDIT_TABLE_SQL)]:
        raise MonitorDataError("Local audit index schema objects or constraints changed")
    columns = conn.execute("PRAGMA table_info(audit_entries)").fetchall()
    if columns != [
        (0, "sequence", "INTEGER", 0, None, 1),
        (1, "prior_entry_sha256", "TEXT", 1, None, 0),
        (2, "audit_json", "TEXT", 1, None, 0),
        (3, "entry_sha256", "TEXT", 1, None, 0),
    ]:
        raise MonitorDataError("Local audit index schema columns changed")
    if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise MonitorDataError("Local audit index SQLite integrity check failed")

    checked: list[tuple[int, dict, str]] = []
    previous_digest = _GENESIS
    previous_payload = None
    expected_keys = {field.name for field in fields(SnapshotChainAudit)}
    rows = conn.execute(
        "SELECT sequence, prior_entry_sha256, audit_json, entry_sha256 "
        "FROM audit_entries ORDER BY sequence"
    ).fetchall()
    if len(rows) > _MAX_ENTRIES:
        raise MonitorDataError("Local audit index size limit reached")
    for expected_seq, (sequence, prior, encoded, digest) in enumerate(rows, start=1):
        if (type(sequence) is not int or sequence != expected_seq
                or prior != previous_digest or type(encoded) is not str
                or len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES
                or type(digest) is not str
                or digest != _digest(sequence=sequence, prior=prior, audit_json=encoded)):
            raise MonitorDataError("Local audit index entry digest or ordering mismatch")
        try:
            payload = json.loads(encoded)
        except (ValueError, TypeError) as exc:
            raise MonitorDataError("Local audit index entry JSON invalid") from exc
        if (type(payload) is not dict or set(payload) != expected_keys
                or encoded != _wire(payload)
                or payload["schema_version"] != 1
                or payload["status"] != "UNREVISED_SNAPSHOT_CHAIN_UNATTESTED"
                or payload["trading_status"] != "UNVERIFIED_DO_NOT_TRADE"
                or payload["production_signals_verified"] is not False
                or payload["source_authentication_verified"] is not False
                or payload["vendor_publication_finality_verified"] is not False
                or type(payload["snapshots_checked"]) is not int
                or payload["snapshots_checked"] < 2):
            raise MonitorDataError("Local audit index entry contract invalid")
        if previous_payload is not None:
            if (payload["snapshots_checked"] <= previous_payload["snapshots_checked"]
                    or payload["first_bars_sha256"] != previous_payload["first_bars_sha256"]
                    or payload["first_captured_at_utc"] != previous_payload["first_captured_at_utc"]
                    or payload["provider_claim"] != previous_payload["provider_claim"]
                    or payload["source_lineage_claim"] != previous_payload["source_lineage_claim"]
                    or payload["symbol"] != previous_payload["symbol"]):
                raise MonitorDataError("Local audit index entry continuity mismatch")
        checked.append((sequence, payload, digest))
        previous_digest, previous_payload = digest, payload
    return checked


def _status(checked: list[tuple[int, dict, str]]) -> LocalAuditIndexStatus:
    return LocalAuditIndexStatus(
        schema_version=LOCAL_AUDIT_INDEX_SCHEMA,
        status="LOCAL_SNAPSHOT_AUDIT_INDEX_CONSISTENT_UNATTESTED",
        entries_checked=len(checked),
        last_entry_sha256=checked[-1][2] if checked else _GENESIS,
        last_snapshots_checked=checked[-1][1]["snapshots_checked"] if checked else 0,
    )


def create_local_snapshot_audit_index(*, path: str | os.PathLike[str]) -> LocalAuditIndexStatus:
    """Create a new, empty local SQLite audit index; never replace a file.

    Parent must exist. Exclusive-create rejects existing files and symlinks.
    Interrupted schema creation may leave an unusable file; never silently
    delete or reinitialize it. Mode 0600 is advisory on Windows.
    """
    target = _target(path, create=True)
    try:
        descriptor = os.open(
            target, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.close(descriptor)
    except (OSError, ValueError) as exc:
        raise MonitorDataError("Local audit index already exists or cannot be created") from exc
    try:
        with closing(_open_existing(target, readonly=False)) as conn:
            with conn:
                conn.execute(_AUDIT_TABLE_SQL)
                conn.execute("PRAGMA user_version=1")
        return verify_local_snapshot_audit_index(path=target)
    except sqlite3.Error as exc:
        raise MonitorDataError("Local audit index initialization failed; inspect file") from exc


def verify_local_snapshot_audit_index(
    *, path: str | os.PathLike[str],
) -> LocalAuditIndexStatus:
    """Read-only structural/hash verification; does not prove completeness."""
    target = _target(path, create=False)
    try:
        with closing(_open_existing(target, readonly=True)) as conn:
            return _status(_read_checked(conn))
    except sqlite3.Error as exc:
        raise MonitorDataError("Local audit index unreadable or corrupt") from exc


def append_local_snapshot_audit_index(
    *, snapshots: tuple[HistorySnapshot, ...], path: str | os.PathLike[str],
) -> LocalAuditIndexStatus:
    """Atomically append one cumulative audit, checking the stored prefix.

    New input must extend the *exact supplied snapshot chain* represented by
    the last stored entry. Changed/dropped old captures fail before insertion.
    No time-based automatic capture, network, broker or portfolio integration.
    """
    audit = audit_unrevised_snapshot_chain(snapshots=snapshots)
    encoded = _wire(audit.as_json())
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise MonitorDataError("Local audit metadata exceeds size limit")
    target = _target(path, create=False)
    try:
        with closing(_open_existing(target, readonly=False)) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                checked = _read_checked(conn)
                if len(checked) >= _MAX_ENTRIES:
                    raise MonitorDataError("Local audit index size limit reached")
                if checked:
                    prior_capture_count = checked[-1][1]["snapshots_checked"]
                    if len(snapshots) <= prior_capture_count:
                        raise MonitorDataError("Local audit index requires newly appended captures")
                    historical_prefix = audit_unrevised_snapshot_chain(
                        snapshots=snapshots[:prior_capture_count]
                    )
                    if historical_prefix.as_json() != checked[-1][1]:
                        raise MonitorDataError(
                            "Local audit index previous capture prefix changed; reconciliation required"
                        )
                sequence = len(checked) + 1
                previous_digest = checked[-1][2] if checked else _GENESIS
                digest = _digest(sequence=sequence, prior=previous_digest, audit_json=encoded)
                conn.execute(
                    "INSERT INTO audit_entries "
                    "(sequence, prior_entry_sha256, audit_json, entry_sha256) "
                    "VALUES (?, ?, ?, ?)",
                    (sequence, previous_digest, encoded, digest),
                )
        return verify_local_snapshot_audit_index(path=target)
    except sqlite3.Error as exc:
        raise MonitorDataError("Local audit index append failed; entry not verified") from exc


def backup_local_snapshot_audit_index(
    *, source_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
) -> LocalAuditIndexStatus:
    """Create one verified, non-overwriting LOCAL metadata-index backup.

    SQLite's online backup API copies a transactionally consistent database,
    avoiding unsafe raw-file copies when a journal exists. Existing backup
    paths are never replaced. Failed/partial destinations are left untouched
    for operator inspection, not silently retried or promoted as valid.
    This is manual and opt-in, NOT automatic recovery or authenticated storage.
    """
    source = _target(source_path, create=False)
    destination = _target(backup_path, create=True)

    # Refuse untrusted/corrupt source before reserving any destination.
    baseline = verify_local_snapshot_audit_index(path=source)
    try:
        descriptor = os.open(
            destination, os.O_CREAT | os.O_EXCL | os.O_RDWR
            | getattr(os, "O_BINARY", 0), 0o600,
        )
        os.close(descriptor)
    except (OSError, ValueError) as exc:
        raise MonitorDataError(
            "Local audit backup already exists or cannot be created"
        ) from exc

    try:
        with closing(_open_existing(source, readonly=True)) as original:
            with closing(_open_existing(destination, readonly=False)) as copy:
                original.backup(copy)
    except sqlite3.Error as exc:
        raise MonitorDataError(
            "Local audit backup failed; inspect incomplete destination"
        ) from exc

    # Ensure backup survived reopen and matches the pre-backup snapshot.
    # A concurrent source change is not silently promoted as a good backup.
    copied = verify_local_snapshot_audit_index(path=destination)
    if (copied != baseline
            or verify_local_snapshot_audit_index(path=source) != baseline):
        raise MonitorDataError(
            "Local audit backup/source changed; reconciliation required"
        )
    return copied


def verify_local_snapshot_audit_backup_prefix(
    *, current_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
) -> LocalAuditBackupPrefixStatus:
    """Read-only audit: backup must be an exact recorded prefix of current.

    Both independently validate their complete v1 structure and SHA chain.
    Matching only counts or last hashes is insufficient: compare every row.
    An older valid prefix is allowed; an unrelated, divergent or ahead backup
    fails closed. No restoration, mutation, trading or external attestation.
    """
    current = _target(current_path, create=False)
    backup = _target(backup_path, create=False)
    try:
        if os.path.samefile(current, backup):
            raise MonitorDataError(
                "Local audit current index and backup must be distinct files"
            )
        with closing(_open_existing(current, readonly=True)) as current_db:
            with closing(_open_existing(backup, readonly=True)) as backup_db:
                # Pin each view during its full multi-query validation.
                current_db.execute("BEGIN")
                backup_db.execute("BEGIN")
                current_rows = _read_checked(current_db)
                backup_rows = _read_checked(backup_db)
                if not backup_rows:
                    raise MonitorDataError(
                        "Local audit backup has no recorded history to recover"
                    )
                if (len(backup_rows) > len(current_rows)
                        or backup_rows != current_rows[:len(backup_rows)]):
                    raise MonitorDataError(
                        "Local audit backup is not an exact prefix of current index"
                    )
                current_status = _status(current_rows)
                backup_status = _status(backup_rows)
    except sqlite3.Error as exc:
        raise MonitorDataError(
            "Local audit backup prefix verification failed; database unreadable"
        ) from exc
    except OSError as exc:
        raise MonitorDataError(
            "Local audit current or backup path cannot be compared"
        ) from exc
    return LocalAuditBackupPrefixStatus(
        schema_version=LOCAL_AUDIT_INDEX_SCHEMA,
        status="LOCAL_SNAPSHOT_AUDIT_BACKUP_PREFIX_CONSISTENT_UNATTESTED",
        backup_entries_checked=backup_status.entries_checked,
        current_entries_checked=current_status.entries_checked,
        backup_last_entry_sha256=backup_status.last_entry_sha256,
        current_last_entry_sha256=current_status.last_entry_sha256,
    )
