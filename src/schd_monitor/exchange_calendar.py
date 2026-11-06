"""Offline NYSE session dates and closing times; never attest market-data validity.

No network requests or brokerage integrations. Requires optional exchange-calendars.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import MonitorDataError

# Conservative calendar buffer only: NOT a guarantee of finalized vendor bars.
SESSION_CLOSE_BUFFER = timedelta(minutes=30)
NEW_YORK = ZoneInfo("America/New_York")


def completed_xnys_sessions(first_session: date, as_of: datetime) -> list[date]:
    """Return every completed XNYS regular session from first_session through as_of.

    ``as_of`` must be timezone-aware. Calendar-complete means exchange close
    plus 30 minutes has elapsed in UTC (including scheduled early closes).
    This does NOT verify data publication/finality, split adjustments,
    independent source agreement or manual holdings.
    """
    if type(first_session) is not date:
        raise MonitorDataError("XNYS first_session must be a date")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise MonitorDataError("XNYS as_of must be a timezone-aware datetime")

    local_day = as_of.astimezone(NEW_YORK).date()
    if first_session > local_day:
        raise MonitorDataError("XNYS first session cannot be later than the New York as_of date")

    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise MonitorDataError(
            "XNYS calendar dependency unavailable; install optional calendar extra"
        ) from exc

    try:
        # exchange-calendars requires start < end even for one calendar day.
        end_day = max(local_day, first_session + timedelta(days=1))
        calendar = xcals.get_calendar(
            "XNYS", start=first_session.isoformat(), end=end_day.isoformat()
        )
        if not calendar.is_session(first_session.isoformat()):
            raise MonitorDataError("XNYS first_session is not a regular exchange session")
        as_of_utc = as_of.astimezone(timezone.utc)
        completed = [
            session.date()
            for session in calendar.sessions
            if calendar.session_close(session).to_pydatetime() + SESSION_CLOSE_BUFFER
            <= as_of_utc
        ]
    except MonitorDataError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        raise MonitorDataError(
            f"XNYS calendar cannot cover requested dates: {type(exc).__name__}"
        ) from exc

    if not completed:
        raise MonitorDataError("XNYS has no completed sessions in requested range at as_of")
    return completed


def xnys_sessions_within_inclusive_dates(start: date, end: date) -> list[date]:
    """Resolve inclusive calendar *request dates* to actual XNYS sessions.

    Unlike completed_xnys_sessions(), date bounds may be exchange holidays or
    weekends. No source bar finality or current-session completion is inferred:
    those are separately checked against an explicit timezone-aware as_of.
    """
    if type(start) is not date or type(end) is not date or start > end:
        raise MonitorDataError("XNYS inclusive request dates invalid or reversed")
    try:
        import exchange_calendars as xcals
        from exchange_calendars.errors import NoSessionsError
    except ImportError as exc:
        raise MonitorDataError(
            "XNYS calendar dependency unavailable; install optional calendar extra"
        ) from exc
    try:
        calendar = xcals.get_calendar(
            "XNYS", start=start.isoformat(),
            end=max(end, start + timedelta(days=1)).isoformat(),
        )
        expected = [
            session.date() for session in calendar.sessions
            if start <= session.date() <= end
        ]
    except (ValueError, TypeError, OverflowError, NoSessionsError) as exc:
        raise MonitorDataError(
            f"XNYS calendar cannot resolve inclusive dates: {type(exc).__name__}"
        ) from exc
    if not expected:
        raise MonitorDataError("XNYS inclusive request range has no exchange sessions")
    return expected
