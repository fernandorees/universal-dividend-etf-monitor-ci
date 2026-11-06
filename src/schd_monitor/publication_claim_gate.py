"""P01 read-only PUBLICATION CLAIM review after the existing dual-feed gate.

A provider/dataset label, timestamp, finality flag and checksum are statements
from an external source. Their internal consistency can be checked locally but
this module CANNOT authenticate provenance, clock accuracy, source independence,
vendor publication finality or corporate-action correctness. Never promotes
a trading signal or writes a user position.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from .exchange_calendar import completed_xnys_sessions
from .feed_integrity import FeedDeclaration
from .models import Bar, MonitorDataError
from .session_feed_gate import audit_offline_completed_dual_feeds

PUBLICATION_CLAIM_SCHEMA = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_LABEL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,63}\Z")


def _aware(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise MonitorDataError(name + " must be an aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class PublicationClaim:
    """Typed, self-declared record for one locally received bar snapshot.

    No API key, endpoint URL, account data or confidential provider response
    is represented here. Source and lineage IDs are opaque human-readable
    identifiers; they do NOT establish independent vendor ownership.
    """
    provider: str
    source_lineage: str
    symbol: str
    adjustment: str
    last_session: date
    bars_sha256: str
    published_at: datetime
    captured_at: datetime
    claimed_final: bool
    schema_version: int = PUBLICATION_CLAIM_SCHEMA

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PUBLICATION_CLAIM_SCHEMA:
            raise MonitorDataError("Publication claim schema unsupported")
        if type(self.provider) is not str or not _SOURCE_LABEL.fullmatch(self.provider):
            raise MonitorDataError("Publication provider identifier invalid")
        if type(self.source_lineage) is not str or not _SOURCE_LABEL.fullmatch(self.source_lineage):
            raise MonitorDataError("Publication data-lineage identifier invalid")
        if type(self.symbol) is not str or not _SOURCE_LABEL.fullmatch(self.symbol):
            raise MonitorDataError("Publication symbol identifier invalid")
        if self.adjustment != "SPLIT_ONLY":
            raise MonitorDataError("Publication claim adjustment must be SPLIT_ONLY")
        if type(self.last_session) is not date:
            raise MonitorDataError("Publication claim last_session invalid")
        if type(self.bars_sha256) is not str or not _SHA256.fullmatch(self.bars_sha256):
            raise MonitorDataError("Publication bars SHA-256 invalid")
        _aware(self.published_at, "published_at")
        _aware(self.captured_at, "captured_at")
        if type(self.claimed_final) is not bool or not self.claimed_final:
            raise MonitorDataError("Publication not explicitly claimed final")


@dataclass(frozen=True)
class PublicationClaimGate:
    status: str
    schema_version: int
    symbol: str
    last_completed_session: str
    checked_declared_sources: int
    complete_sessions_compared: int
    source_lineage_labels_distinct: bool
    snapshot_fingerprints_bound_to_bars: bool
    publication_timestamps_after_scheduled_completion: bool
    capture_timestamps_not_future_of_as_of: bool
    provider_identity_authenticated: bool
    underlying_vendor_independence_authenticated: bool
    publication_timestamps_authenticated: bool
    vendor_bar_finality_verified: bool
    corporate_action_basis_verified: bool
    manual_fill_reconciled: bool
    production_signals_verified: bool
    trading_status: str

    def as_json(self) -> dict:
        return asdict(self)


def check_declared_publication_gate(
    *, primary: list[Bar], secondary: list[Bar],
    primary_declaration: FeedDeclaration,
    secondary_declaration: FeedDeclaration,
    primary_claim: PublicationClaim,
    secondary_claim: PublicationClaim,
    expected_symbol: str,
    as_of: datetime,
) -> PublicationClaimGate:
    """Reject contradictory self-declared evidence; never confer attestation.

    Full coverage of every scheduled complete XNYS session and both declared
    feeds is required by the existing offline gate. Each claimed publication
    time must fall after that session's scheduled close + calendar buffer.
    Neither publication timestamp nor captured_at is from a trusted clock.
    """
    clock = _aware(as_of, "as_of")
    base = audit_offline_completed_dual_feeds(
        primary=primary, secondary=secondary,
        primary_declaration=primary_declaration,
        secondary_declaration=secondary_declaration,
        expected_symbol=expected_symbol, as_of=as_of,
    )
    if (type(primary_claim) is not PublicationClaim
            or type(secondary_claim) is not PublicationClaim):
        raise MonitorDataError("Two typed publication claims required")
    if primary_claim.source_lineage.casefold() == secondary_claim.source_lineage.casefold():
        raise MonitorDataError("Duplicate claimed upstream data lineage")
    end_date = date.fromisoformat(base.last_completed_session)
    for claim, declared, checksum in (
        (primary_claim, primary_declaration, base.primary_bars_sha256),
        (secondary_claim, secondary_declaration, base.secondary_bars_sha256),
    ):
        if (claim.provider.casefold() != declared.provider.casefold()
                or claim.symbol != base.symbol
                or claim.adjustment != declared.adjustment
                or claim.last_session != end_date
                or claim.bars_sha256 != checksum):
            raise MonitorDataError("Publication claim not bound to feed declaration and bars")
        published = _aware(claim.published_at, "published_at")
        captured = _aware(claim.captured_at, "captured_at")
        if not (published <= captured <= clock):
            raise MonitorDataError("Publication/capture chronology inconsistent")
        # Reuse the actual XNYS scheduled-close-plus-30min rule. Checking only
        # a date would incorrectly permit a pre-close provisional daily bar.
        try:
            eligible = completed_xnys_sessions(end_date, published)
        except MonitorDataError as exc:
            raise MonitorDataError("Publication before scheduled session completion") from exc
        if end_date not in eligible:
            raise MonitorDataError("Publication before scheduled session completion")
    return PublicationClaimGate(
        status="PUBLICATION_AND_PROVENANCE_CLAIMS_CONSISTENT_UNATTESTED",
        schema_version=PUBLICATION_CLAIM_SCHEMA,
        symbol=base.symbol,
        last_completed_session=base.last_completed_session,
        checked_declared_sources=2,
        complete_sessions_compared=base.calendar_complete_sessions,
        source_lineage_labels_distinct=True,
        snapshot_fingerprints_bound_to_bars=True,
        publication_timestamps_after_scheduled_completion=True,
        capture_timestamps_not_future_of_as_of=True,
        provider_identity_authenticated=False,
        underlying_vendor_independence_authenticated=False,
        publication_timestamps_authenticated=False,
        vendor_bar_finality_verified=False,
        corporate_action_basis_verified=False,
        manual_fill_reconciled=False,
        production_signals_verified=False,
        trading_status="UNVERIFIED_DO_NOT_TRADE",
    )
