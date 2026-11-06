"""Fail-closed comparison of two *locally captured* OHLC history snapshots.

A later vendor revision, historic split restatement or altered daily price
must never silently replace previously used history. This is purely offline:
the timestamp, lineage and bars are self-declared, not authenticated.

No brokerage, market-data HTTP request, portfolio mutation, or VERIFIED signal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .feed_integrity import _bar_digest
from .models import Bar, MonitorDataError, validate_bars
from .publication_claim_gate import PublicationClaim, _aware

REVISION_GUARD_SCHEMA = 1
_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class HistorySnapshot:
    """Binds an entire captured OHLC history to its declared provenance.

    The same vendor dataset may restate an old session on a future fetch.
    Those revised records require manual investigation, not silent acceptance.
    """
    bars: tuple[Bar, ...]
    claim: PublicationClaim

    def __post_init__(self) -> None:
        if type(self.bars) is not tuple or not self.bars:
            raise MonitorDataError("Snapshot requires immutable nonempty bars tuple")
        validate_bars(list(self.bars))
        if type(self.claim) is not PublicationClaim:
            raise MonitorDataError("Snapshot requires typed publication claim")
        # A captured snapshot cannot predate the publication it claims to
        # contain. Compare UTC instants rather than timezone display strings.
        # Both clocks remain caller-declared, not vendor authenticated.
        if _aware(self.claim.published_at, "published_at") > _aware(
            self.claim.captured_at, "captured_at"
        ):
            raise MonitorDataError("Snapshot publication/capture chronology inconsistent")
        if self.bars[-1].session != self.claim.last_session:
            raise MonitorDataError("Snapshot last-session claim differs from OHLC")
        if _bar_digest(list(self.bars)) != self.claim.bars_sha256:
            raise MonitorDataError("Snapshot bars do not match declared fingerprint")


@dataclass(frozen=True)
class RevisionGateResult:
    schema_version: int
    status: str
    provider_claim: str
    source_lineage_claim: str
    symbol: str
    prior_sessions: int
    newer_sessions: int
    unchanged_prior_sessions: int
    new_sessions_appended: int
    prior_sha256: str
    newer_sha256: str
    historical_revisions_detected: int
    source_authentication_verified: bool
    vendor_publication_finality_verified: bool
    production_signals_verified: bool
    trading_status: str

    def as_json(self) -> dict:
        return asdict(self)


def check_unrevised_history(*, prior: HistorySnapshot, newer: HistorySnapshot) -> RevisionGateResult:
    """Refuse ANY modification to previously captured historic daily OHLC.

    Growth by append-only sessions is allowed; the calendar, feed independence,
    actual vendor publication and corporate actions must be separately checked.
    An unexpected split restatement FAILS, even if legitimately revised: the
    operator must reconcile the source and establish a new audited baseline.
    """
    if type(prior) is not HistorySnapshot or type(newer) is not HistorySnapshot:
        raise MonitorDataError("Two typed local snapshots are required")
    a, b = prior.claim, newer.claim
    if (a.provider != b.provider or a.source_lineage != b.source_lineage
            or a.symbol != b.symbol or a.adjustment != b.adjustment):
        raise MonitorDataError("Snapshot source identity or adjustment basis changed")
    if _aware(b.captured_at, "newer captured_at") <= _aware(a.captured_at, "prior captured_at"):
        raise MonitorDataError("Snapshot capture chronology must increase")
    if len(newer.bars) < len(prior.bars):
        raise MonitorDataError("Later OHLC snapshot unexpectedly lost sessions")
    for old, current in zip(prior.bars, newer.bars):
        if old.session != current.session:
            raise MonitorDataError("Historical session index changed; reconciliation required")
        for field in _FIELDS:
            if getattr(old, field) != getattr(current, field):
                raise MonitorDataError(
                    "Historical OHLC revision: session=" + old.session.isoformat()
                    + " field=" + field + "; reconciliation required"
                )
    # A change in source SHA is expected only when genuinely new daily sessions
    # are appended. No hidden tolerance may disguise a corrected past price.
    appended = len(newer.bars) - len(prior.bars)
    if appended == 0 and a.bars_sha256 != b.bars_sha256:
        raise MonitorDataError("Same-date OHLC history SHA changed")
    return RevisionGateResult(
        schema_version=REVISION_GUARD_SCHEMA,
        status="HISTORICAL_SNAPSHOT_UNCHANGED_OR_EXTENDED_UNATTESTED",
        provider_claim=a.provider, source_lineage_claim=a.source_lineage,
        symbol=a.symbol, prior_sessions=len(prior.bars), newer_sessions=len(newer.bars),
        unchanged_prior_sessions=len(prior.bars), new_sessions_appended=appended,
        prior_sha256=a.bars_sha256, newer_sha256=b.bars_sha256,
        historical_revisions_detected=0, source_authentication_verified=False,
        vendor_publication_finality_verified=False, production_signals_verified=False,
        trading_status="UNVERIFIED_DO_NOT_TRADE",
    )
