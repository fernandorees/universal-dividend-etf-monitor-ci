"""P01 offline session-finality and dual-feed fail-closed orchestration.

Research-only guard. Calendar completion is scheduled XNYS close + 30 minutes,
NOT source publication finality. Provider identities, adjustment basis and
split/dividend completeness are SELF DECLARED until separately authenticated.
Never returns VERIFIED, generates orders, or reads/changes a portfolio ledger.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from .exchange_calendar import completed_xnys_sessions
from .feed_integrity import (
    FeedDeclaration, MAX_RELATIVE_OHLC_DIFFERENCE, compare_offline_feeds,
)
from .models import Bar, MonitorDataError, validate_expected_sessions

SESSION_FEED_GATE_SCHEMA = 1


@dataclass(frozen=True)
class OfflineSessionFeedGate:
    status: str
    schema_version: int
    symbol: str
    as_of: str
    first_session: str
    last_completed_session: str
    calendar_complete_sessions: int
    feeds_agree_on_session_coverage: bool
    max_relative_ohlc_difference: float
    research_only_ohlc_tolerance: float
    primary_bars_sha256: str
    secondary_bars_sha256: str
    distinct_declared_providers: bool
    scheduled_session_close_buffer_minutes: int
    provider_identity_verified: bool
    feed_independence_verified: bool
    vendor_bar_finality_verified: bool
    split_and_dividend_basis_verified: bool
    manual_position_reconciled: bool
    production_signals_verified: bool
    trading_status: str

    def as_json(self) -> dict:
        return asdict(self)


def audit_offline_completed_dual_feeds(
    *, primary: list[Bar], secondary: list[Bar],
    primary_declaration: FeedDeclaration,
    secondary_declaration: FeedDeclaration,
    expected_symbol: str, as_of: datetime,
) -> OfflineSessionFeedGate:
    """Require EXACT XNYS expected dates on both feeds AND strict OHLC check.

    The caller supplies an explicit timezone-aware as_of: no implicit current
    clock and no assertion that as_of is a trusted data-publication timestamp.
    This function makes NO external requests; calendar library is offline.
    Mismatched, stale, weekend/holiday or in-progress sessions raise
    MonitorDataError. Metadata remains unverified external declarations.
    """
    if (not isinstance(as_of, datetime)
            or as_of.tzinfo is None or as_of.utcoffset() is None):
        raise MonitorDataError("Offline session gate requires timezone-aware as_of")
    if (not isinstance(primary, list) or not primary
            or not isinstance(secondary, list) or not secondary):
        raise MonitorDataError("Offline gate requires both complete OHLC histories")
    # Existing calendar refuses nonexistent/unsupported XNYS first sessions,
    # and calculates early closes and DST using exchange schedules.
    expected = completed_xnys_sessions(primary[0].session, as_of)
    validate_expected_sessions(primary, expected)
    validate_expected_sessions(secondary, expected)
    comparison = compare_offline_feeds(
        primary, secondary, primary_declaration, secondary_declaration,
        expected_symbol=expected_symbol,
    )
    # Both declarations are merely claims, including last_complete_session.
    # A pass does NOT prove independently owned sources or bar publication.
    return OfflineSessionFeedGate(
        status="CALENDAR_COMPLETE_DUAL_FEED_CONSISTENT_UNATTESTED",
        schema_version=SESSION_FEED_GATE_SCHEMA,
        symbol=comparison.symbol,
        as_of=as_of.isoformat(),
        first_session=expected[0].isoformat(),
        last_completed_session=expected[-1].isoformat(),
        calendar_complete_sessions=len(expected),
        feeds_agree_on_session_coverage=True,
        max_relative_ohlc_difference=comparison.max_relative_ohlc_difference,
        research_only_ohlc_tolerance=MAX_RELATIVE_OHLC_DIFFERENCE,
        primary_bars_sha256=comparison.primary_bars_sha256,
        secondary_bars_sha256=comparison.secondary_bars_sha256,
        distinct_declared_providers=True,
        scheduled_session_close_buffer_minutes=30,
        provider_identity_verified=False,
        feed_independence_verified=False,
        vendor_bar_finality_verified=False,
        split_and_dividend_basis_verified=False,
        manual_position_reconciled=False,
        production_signals_verified=False,
        trading_status="UNVERIFIED_DO_NOT_TRADE",
    )
