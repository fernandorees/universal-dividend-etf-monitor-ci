"""P01 inert Alpaca/Massive historical-bar composition and XNYS audit.

No networking, credentials, vendor authentication, broker, fills, or orders.
This checks fictional/external *untrusted* payload consistency ONLY.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime

from .alpaca_offline_contract import AlpacaDailyRequest, check_alpaca_offline_page
from .feed_integrity import FeedDeclaration
from .exchange_calendar import (
    completed_xnys_sessions, xnys_sessions_within_inclusive_dates,
)
from .massive_offline_contract import MassiveDailyRequest, check_massive_offline_response
from .models import MonitorDataError
from .session_feed_gate import audit_offline_completed_dual_feeds

OFFLINE_PROVIDER_PAIR_SCHEMA = 1


@dataclass(frozen=True)
class OfflineAlpacaMassiveAudit:
    status: str
    schema_version: int
    symbol: str
    first_session: str
    last_completed_session: str
    as_of_claim: str
    sessions_compared: int
    max_relative_ohlc_difference: float
    alpaca_request_sha256: str
    alpaca_response_sha256: str
    massive_request_sha256: str
    massive_response_sha256: str
    massive_claimed_server_request_id: str
    calendar_and_two_feed_gate_status: str
    provider_identity_verified: bool = False
    feed_independence_verified: bool = False
    vendor_bar_finality_verified: bool = False
    split_and_dividend_basis_verified: bool = False
    manual_position_reconciled: bool = False
    production_signals_verified: bool = False
    trading_status: str = "UNVERIFIED_DO_NOT_TRADE"

    def as_json(self) -> dict:
        return asdict(self)


def preflight_offline_alpaca_massive_requests(
    *, alpaca_request: AlpacaDailyRequest, massive_request: MassiveDailyRequest,
    as_of: datetime,
) -> tuple[date, ...]:
    """Check both inert request definitions and the closed-session schedule.

    Caller may invoke this BEFORE any future opt-in market-data GET.  A
    calendar pass is not vendor publication, feed independence, entitlement,
    split correctness, real-market-data validity, or signal qualification.
    No HTTP, credential, portfolio state, order, or side effect is involved.
    """
    if type(alpaca_request) is not AlpacaDailyRequest or type(massive_request) is not MassiveDailyRequest:
        raise MonitorDataError("Typed Alpaca and Massive requests are required")
    if (alpaca_request.symbol != massive_request.symbol
            or alpaca_request.start != massive_request.start
            or alpaca_request.end != massive_request.end):
        raise MonitorDataError("Alpaca and Massive requested symbol/date bounds differ")
    if (not isinstance(as_of, datetime)
            or as_of.tzinfo is None or as_of.utcoffset() is None):
        raise MonitorDataError("Offline request preflight requires timezone-aware as_of")

    requested = xnys_sessions_within_inclusive_dates(
        alpaca_request.start, alpaca_request.end
    )
    completed = completed_xnys_sessions(requested[0], as_of)
    if requested != completed:
        raise MonitorDataError(
            "Session coverage mismatch: requested XNYS range is not exactly "
            "the closed sessions at as_of"
        )
    return tuple(requested)


def audit_offline_alpaca_massive_pair(
    *, alpaca_request: AlpacaDailyRequest, alpaca_http_status: int,
    alpaca_payload: object, massive_request: MassiveDailyRequest,
    massive_http_status: int, massive_payload: object, as_of: datetime,
) -> OfflineAlpacaMassiveAudit:
    """Compose both strict vendor shapes and existing closed-XNYS two-feed gate.

    Resolve matching inclusive request dates to the complete set of
    scheduled XNYS trading sessions, allowing weekend/holiday endpoints
    without accepting shared leading/trailing/interior missing sessions.
    The existing as_of completion/freshness gate remains mandatory.

    The caller supplies every status, payload and as_of value: none are
    vendor-attested or trustworthy proof of data publication or independence.
    """
    # Request/calendar-only failure must be detectable BEFORE any future
    # vendor acquisition; an already supplied fictional payload still undergoes
    # its independent strict offline shape checks immediately afterward.
    requested_sessions = list(preflight_offline_alpaca_massive_requests(
        alpaca_request=alpaca_request, massive_request=massive_request,
        as_of=as_of,
    ))
    alpaca = check_alpaca_offline_page(
        request=alpaca_request, http_status=alpaca_http_status, payload=alpaca_payload
    )
    massive = check_massive_offline_response(
        request=massive_request, http_status=massive_http_status, payload=massive_payload
    )
    # Reject shared omissions on *any* requested scheduled XNYS session.
    # This includes a leading/trailing session when the raw request date is
    # a holiday/weekend. Do not just compare the two vendor payloads.
    for name, page in (("Alpaca", alpaca), ("Massive", massive)):
        if [bar.session for bar in page.bars] != requested_sessions:
            raise MonitorDataError(
                f"{name} offline request calendar coverage mismatch"
            )

    first, last = alpaca.bars[0].session, alpaca.bars[-1].session
    left = FeedDeclaration(
        provider="ALPACA_DECLARED_OFFLINE", symbol=alpaca_request.symbol,
        adjustment="SPLIT_ONLY", last_complete_session=last,
    )
    right = FeedDeclaration(
        provider="MASSIVE_DECLARED_OFFLINE", symbol=massive_request.symbol,
        adjustment="SPLIT_ONLY", last_complete_session=massive.bars[-1].session,
    )
    gate = audit_offline_completed_dual_feeds(
        primary=list(alpaca.bars), secondary=list(massive.bars),
        primary_declaration=left, secondary_declaration=right,
        expected_symbol=alpaca_request.symbol, as_of=as_of,
    )
    return OfflineAlpacaMassiveAudit(
        status="ALPACA_MASSIVE_OFFLINE_COMPOSED_UNATTESTED",
        schema_version=OFFLINE_PROVIDER_PAIR_SCHEMA,
        symbol=alpaca_request.symbol,
        first_session=first.isoformat(),
        last_completed_session=last.isoformat(),
        as_of_claim=gate.as_of,
        sessions_compared=gate.calendar_complete_sessions,
        max_relative_ohlc_difference=gate.max_relative_ohlc_difference,
        alpaca_request_sha256=alpaca.request_sha256,
        alpaca_response_sha256=alpaca.response_sha256,
        massive_request_sha256=massive.request_sha256,
        massive_response_sha256=massive.response_sha256,
        massive_claimed_server_request_id=massive.claimed_server_request_id,
        calendar_and_two_feed_gate_status=gate.status,
    )
