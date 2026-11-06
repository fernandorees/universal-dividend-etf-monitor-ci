"""Opt-in, exclusive-write LOCAL receipt for an offline snapshot-chain audit.

No collection, network, broker connection, orders or portfolio state.
The receipt contains metadata (including claimed symbol/lineage), not OHLC
rows. Keep any real receipt under a private, gitignored local directory.
Not a signed, immutable, comprehensive or tamper-proof event journal.
"""
from __future__ import annotations

import os
from pathlib import Path

from .models import MonitorDataError
from .snapshot_chain_guard import (
    SnapshotChainAudit, audit_unrevised_snapshot_chain,
)
from .snapshot_revision_guard import HistorySnapshot

# Bound reading and writing: a proper metadata-only receipt is tiny.
MAX_LOCAL_RECEIPT_BYTES = 65536


def _receipt_bytes(audit: SnapshotChainAudit) -> bytes:
    """Canonical versioned metadata-only receipt; never serialize OHLC."""
    import json

    if type(audit) is not SnapshotChainAudit:
        raise MonitorDataError("Typed snapshot-chain audit required")
    return (json.dumps(
        audit.as_json(), sort_keys=True, ensure_ascii=True,
        indent=2, allow_nan=False,
    ) + "\n").encode("utf-8")


def _expected(snapshots: tuple[HistorySnapshot, ...]) -> tuple[SnapshotChainAudit, bytes]:
    # Validate EVERY adjacent pair before opening, creating or reading a file.
    audit = audit_unrevised_snapshot_chain(snapshots=snapshots)
    encoded = _receipt_bytes(audit)
    if len(encoded) > MAX_LOCAL_RECEIPT_BYTES:
        raise MonitorDataError("Local snapshot-chain receipt exceeds size limit")
    return audit, encoded


def write_local_snapshot_chain_receipt(
    *, snapshots: tuple[HistorySnapshot, ...], path: str | os.PathLike[str],
) -> SnapshotChainAudit:
    """Create a new local receipt only; NEVER replace an existing path.

    The directory must already exist. Exclusive creation refuses existing
    regular files and symlinks. fsync requests durable file contents but
    neither protects against OS crashes at all points nor validates a vendor.
    A crash may leave an incomplete receipt; re-reading must then fail closed.
    File permission protections depend on the operator's operating system.
    """
    audit, encoded = _expected(snapshots)
    try:
        target = Path(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except (OSError, TypeError, ValueError) as exc:
        raise MonitorDataError(
            "Local receipt not written; existing files are never overwritten "
            f"({type(exc).__name__})"
        ) from exc
    return audit


def verify_local_snapshot_chain_receipt(
    *, snapshots: tuple[HistorySnapshot, ...], path: str | os.PathLike[str],
) -> SnapshotChainAudit:
    """Fail closed unless saved bytes exactly match re-audited source snapshots.

    This is NOT third-party authentication or a guarantee that the operator
    supplied all genuine historical captures. No snapshot data is written.
    """
    audit, expected = _expected(snapshots)
    try:
        with Path(path).open("rb") as source:
            saved = source.read(MAX_LOCAL_RECEIPT_BYTES + 1)
    except (OSError, TypeError, ValueError) as exc:
        raise MonitorDataError(
            "Local snapshot-chain receipt unavailable or unreadable "
            f"({type(exc).__name__})"
        ) from exc
    if len(saved) > MAX_LOCAL_RECEIPT_BYTES or saved != expected:
        raise MonitorDataError(
            "Local snapshot-chain receipt mismatch, truncated, or altered; "
            "reconciliation required"
        )
    return audit
