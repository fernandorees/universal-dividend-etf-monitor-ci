"""Versioned domain types. Price input MUST be split-adjusted, not dividend-adjusted."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from math import isfinite


class MonitorDataError(ValueError):
    """Fail closed on invalid/insufficient/corporate-action-uncertain data."""


@dataclass(frozen=True)
class Bar:
    session: date
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self):
        if not all(isfinite(x) and x > 0 for x in (self.open, self.high, self.low, self.close)):
            raise MonitorDataError(f"Invalid nonpositive or nonfinite OHLC for {self.session}")
        tol = 1e-7 * max(self.open, self.high, self.low, self.close)
        if self.high + tol < max(self.open, self.close, self.low) or self.low - tol > min(self.open, self.close, self.high):
            raise MonitorDataError(f"Inconsistent OHLC for {self.session}")


@dataclass(frozen=True)
class Position:
    symbol: str
    shares: float
    available_cash: float
    peak_close: float | None
    reconciled_through: date
    # Peak is the last known held maximum; today's close is included by the signal engine.
    # Holdings include all manually confirmed fills at the current session opening.
    # For real money, the user must enter this from audited manual ledger, NEVER infer from account age.

    def __post_init__(self):
        if self.shares < 0 or self.available_cash < 0 or not isfinite(self.shares) or not isfinite(self.available_cash):
            raise MonitorDataError("Position shares and cash must be finite/nonnegative")
        if self.shares > 1e-8 and (
            self.peak_close is None
            or isinstance(self.peak_close, bool)
            or not isinstance(self.peak_close, (int, float))
            or not isfinite(self.peak_close)
            or self.peak_close <= 0
        ):
            raise MonitorDataError("Invested position requires finite positive highest held closing price")
        if self.shares <= 1e-8 and self.peak_close is not None:
            raise MonitorDataError("Flat position must have null peak_close")


def validate_bars(bars: list[Bar], minimum: int = 65) -> None:
    if len(bars) < minimum:
        raise MonitorDataError(f"Need >= {minimum} complete sessions; received {len(bars)}")
    if any(a.session >= b.session for a, b in zip(bars, bars[1:])):
        raise MonitorDataError("Missing sort order or duplicate sessions")
    if any(x.session.weekday() >= 5 for x in bars):
        raise MonitorDataError("Weekend date in US equities OHLC input")
    # Legitimate market holidays are allowed. Unexpected gaps are reviewed at ingestion level.
    if any((b.session - a.session).days > 7 for a, b in zip(bars, bars[1:])):
        raise MonitorDataError("Unexpected >7-day history gap; refuse calculation")


def validate_expected_sessions(bars: list[Bar], expected_sessions: list[date]) -> None:
    """Fail closed when OHLC dates differ from a supplied complete session manifest.

    This compares dates only. The caller must obtain expected sessions from an
    independently validated US exchange calendar and ensure it spans precisely
    the requested bar history, including its latest completed session.
    Passing here does NOT establish data-source, split, or session-finality trust.
    """
    validate_bars(bars)
    expected = list(expected_sessions)
    if not expected:
        raise MonitorDataError("Expected-session manifest is empty")
    if any(type(session) is not date for session in expected):
        raise MonitorDataError("Expected-session manifest must contain date values only")
    if any(a >= b for a, b in zip(expected, expected[1:])):
        raise MonitorDataError("Expected-session manifest must be strictly ascending and unique")
    if any(session.weekday() >= 5 for session in expected):
        raise MonitorDataError("Weekend in expected-session manifest")

    actual = [bar.session for bar in bars]
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))

        def describe(sessions: list[date]) -> str:
            sample = ", ".join(session.isoformat() for session in sessions[:3])
            return f"{len(sessions)} [{sample}{', ...' if len(sessions) > 3 else ''}]"

        raise MonitorDataError(
            "Session coverage mismatch: "
            f"missing expected sessions={describe(missing)}; "
            f"unexpected bar sessions={describe(unexpected)}"
        )
