"""Offline-only audit of a complete chronological history-snapshot chain.

Every adjacent history pair is checked; first/last-only comparison can miss an
intermediate correction later reversed. Self-declared evidence, not provenance.
No HTTP, brokerage, portfolio records, trade actions or production promotion.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import timezone

from .models import MonitorDataError
from .snapshot_revision_guard import HistorySnapshot, check_unrevised_history

SNAPSHOT_CHAIN_SCHEMA = 1


@dataclass(frozen=True)
class SnapshotChainAudit:
    schema_version: int
    status: str
    provider_claim: str
    source_lineage_claim: str
    symbol: str
    snapshots_checked: int
    adjacent_links_checked: int
    first_sessions: int
    last_sessions: int
    sessions_appended_over_chain: int
    first_captured_at_utc: str
    last_captured_at_utc: str
    first_bars_sha256: str
    last_bars_sha256: str
    chain_metadata_sha256: str
    source_authentication_verified: bool = False
    vendor_publication_finality_verified: bool = False
    production_signals_verified: bool = False
    trading_status: str = "UNVERIFIED_DO_NOT_TRADE"

    def as_json(self) -> dict:
        """Audit metadata only; never raw price histories."""
        return asdict(self)


def audit_unrevised_snapshot_chain(*, snapshots: tuple[HistorySnapshot, ...]) -> SnapshotChainAudit:
    """Validate every adjacent capture; any changed historic OHLC fails closed.

    Input ordering represents acquisition chronology and is never sorted for
    the caller. Require >= 2 immutable snapshots and increasing capture times.
    A SHA-256 of deterministic, non-secret claimed metadata is an audit
    fingerprint, NOT a vendor signature, trusted clock or tamper-proof ledger.
    """
    if type(snapshots) is not tuple or len(snapshots) < 2:
        raise MonitorDataError("Snapshot chain requires at least two immutable captures")
    if any(type(item) is not HistorySnapshot for item in snapshots):
        raise MonitorDataError("Snapshot chain requires typed history snapshots")

    for old, recent in zip(snapshots, snapshots[1:]):
        check_unrevised_history(prior=old, newer=recent)

    metadata = []
    for index, snapshot in enumerate(snapshots):
        claim = snapshot.claim
        metadata.append({
            "index": index,
            "provider_claim": claim.provider,
            "source_lineage_claim": claim.source_lineage,
            "symbol": claim.symbol,
            "adjustment_claim": claim.adjustment,
            "last_session": claim.last_session.isoformat(),
            "sessions": len(snapshot.bars),
            "bars_sha256": claim.bars_sha256,
            "published_at_utc": claim.published_at.astimezone(timezone.utc).isoformat(),
            "captured_at_utc": claim.captured_at.astimezone(timezone.utc).isoformat(),
        })
    encoded = json.dumps(
        {"schema_version": SNAPSHOT_CHAIN_SCHEMA, "captures": metadata},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    first, last = snapshots[0], snapshots[-1]
    return SnapshotChainAudit(
        schema_version=SNAPSHOT_CHAIN_SCHEMA,
        status="UNREVISED_SNAPSHOT_CHAIN_UNATTESTED",
        provider_claim=first.claim.provider,
        source_lineage_claim=first.claim.source_lineage,
        symbol=first.claim.symbol,
        snapshots_checked=len(snapshots),
        adjacent_links_checked=len(snapshots) - 1,
        first_sessions=len(first.bars),
        last_sessions=len(last.bars),
        sessions_appended_over_chain=len(last.bars) - len(first.bars),
        first_captured_at_utc=metadata[0]["captured_at_utc"],
        last_captured_at_utc=metadata[-1]["captured_at_utc"],
        first_bars_sha256=first.claim.bars_sha256,
        last_bars_sha256=last.claim.bars_sha256,
        chain_metadata_sha256=hashlib.sha256(encoded).hexdigest(),
    )
