"""P01 offline split-only normalization audit; source/ledger claims are UNATTESTED.

This module NEVER changes signal bars or the frozen strategy. It only checks that
a separately provided raw series can explain supplied split-only OHLC through a
self-declared split-event ledger. No dividends are used as adjustment factors.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite
from pathlib import Path

from .models import Bar, MonitorDataError, validate_bars

SPLIT_LEDGER_SCHEMA = 1
MAX_RELATIVE_SPLIT_PRICE_DIFFERENCE = 0.0001  # P01-only 0.01%; NOT production calibrated.


@dataclass(frozen=True)
class SplitEvent:
    effective_session: date
    new_shares: int
    old_shares: int

    def __post_init__(self) -> None:
        if type(self.effective_session) is not date:
            raise MonitorDataError("Split effective_session must be a date")
        if (type(self.new_shares) is not int or type(self.old_shares) is not int
                or not (1 <= self.new_shares <= 1000000)
                or not (1 <= self.old_shares <= 1000000)
                or self.new_shares == self.old_shares):
            raise MonitorDataError("Split ratio must have distinct positive integer new_shares and old_shares")


@dataclass(frozen=True)
class SplitLedger:
    symbol: str
    events: tuple[SplitEvent, ...]
    schema_version: int = SPLIT_LEDGER_SCHEMA

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SPLIT_LEDGER_SCHEMA:
            raise MonitorDataError("Unsupported split-ledger schema")
        if (not isinstance(self.symbol, str) or not self.symbol
                or self.symbol != self.symbol.strip().upper()):
            raise MonitorDataError("Split-ledger symbol must be an uppercase ticker")
        if not isinstance(self.events, tuple) or any(type(event) is not SplitEvent for event in self.events):
            raise MonitorDataError("Split events must be a tuple of SplitEvent entries")
        if any(a.effective_session >= b.effective_session for a, b in zip(self.events, self.events[1:])):
            raise MonitorDataError("Split events must be strictly ordered without duplicate dates")


def split_ledger_from_json(path: str | Path) -> SplitLedger:
    """Read a locally prepared event ledger; NOT an authenticated action feed."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorDataError("Invalid split-ledger JSON") from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "symbol", "events"}:
        raise MonitorDataError("Split ledger requires exactly schema_version, symbol and events")
    raw_events = data["events"]
    if not isinstance(raw_events, list):
        raise MonitorDataError("Split events must be a JSON array")
    events = []
    for index, item in enumerate(raw_events):
        if not isinstance(item, dict) or set(item) != {"effective_session", "new_shares", "old_shares"}:
            raise MonitorDataError(f"Malformed split event at index {index}")
        raw_date = item["effective_session"]
        if not isinstance(raw_date, str):
            raise MonitorDataError(f"Invalid split effective_session at index {index}")
        try:
            session = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise MonitorDataError(f"Invalid split effective_session at index {index}") from exc
        if session.isoformat() != raw_date:
            raise MonitorDataError(f"Split effective_session must be YYYY-MM-DD at index {index}")
        events.append(SplitEvent(session, item["new_shares"], item["old_shares"]))
    return SplitLedger(symbol=data["symbol"], events=tuple(events),
                       schema_version=data["schema_version"])


@dataclass(frozen=True)
class SplitAudit:
    status: str
    schema_version: int
    symbol: str
    sessions_compared: int
    split_events_applied: int
    last_session: str
    largest_relative_difference: float
    tolerance: float
    raw_bars_sha256: str
    adjusted_bars_sha256: str
    split_ledger_sha256: str

    def as_json(self) -> dict:
        return asdict(self)


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()


def _bar_array(bars: list[Bar]) -> list[list]:
    return [[b.session.isoformat(), b.open, b.high, b.low, b.close] for b in bars]


def audit_split_only_bars(
    raw: list[Bar], adjusted: list[Bar], ledger: SplitLedger,
    *, expected_symbol: str,
) -> SplitAudit:
    """Fail closed unless declared splits transform raw into supplied adjusted OHLC.

    Raw/adjusted data and events must originate externally; agreement does not
    establish actual split-event completeness, upstream origin or dividend handling.
    """
    validate_bars(raw)
    validate_bars(adjusted)
    if not isinstance(ledger, SplitLedger):
        raise MonitorDataError("Versioned split ledger is required")
    if (not isinstance(expected_symbol, str) or not expected_symbol
            or expected_symbol != expected_symbol.strip().upper()
            or ledger.symbol != expected_symbol):
        raise MonitorDataError("Split-ledger symbol does not match position ticker")
    if len(raw) != len(adjusted) or any(a.session != b.session for a, b in zip(raw, adjusted)):
        raise MonitorDataError("Raw and split-only OHLC session histories differ")
    actual_dates = {bar.session for bar in raw}
    first = raw[0].session
    for event in ledger.events:
        if event.effective_session <= first or event.effective_session not in actual_dates:
            raise MonitorDataError(
                "Split effective session must occur after the first bar and within both price histories"
            )

    # Traverse backwards: the event's split multiplier affects ONLY earlier prices.
    multiplier = 1.0
    next_event = len(ledger.events) - 1
    maximum = 0.0
    for raw_bar, adjusted_bar in zip(reversed(raw), reversed(adjusted)):
        while next_event >= 0 and ledger.events[next_event].effective_session > raw_bar.session:
            event = ledger.events[next_event]
            multiplier *= event.new_shares / event.old_shares
            if not isfinite(multiplier) or multiplier <= 0:
                raise MonitorDataError("Split cumulative factor is invalid")
            next_event -= 1
        for field in ("open", "high", "low", "close"):
            expected = getattr(raw_bar, field) / multiplier
            observed = getattr(adjusted_bar, field)
            if not isfinite(expected) or expected <= 0:
                raise MonitorDataError("Split-adjusted expected OHLC is nonfinite or nonpositive")
            difference = abs(expected - observed) / max(expected, observed)
            if difference > MAX_RELATIVE_SPLIT_PRICE_DIFFERENCE:
                raise MonitorDataError(
                    f"Split-only OHLC mismatch {raw_bar.session.isoformat()} field={field}: "
                    "adjusted prices inconsistent with declared splits"
                )
            maximum = max(maximum, difference)

    event_array = [[e.effective_session.isoformat(), e.new_shares, e.old_shares]
                   for e in ledger.events]
    return SplitAudit(
        status="CONSISTENT_UNATTESTED", schema_version=1, symbol=expected_symbol,
        sessions_compared=len(raw), split_events_applied=len(ledger.events),
        last_session=raw[-1].session.isoformat(),
        largest_relative_difference=maximum,
        tolerance=MAX_RELATIVE_SPLIT_PRICE_DIFFERENCE,
        raw_bars_sha256=_hash(_bar_array(raw)),
        adjusted_bars_sha256=_hash(_bar_array(adjusted)),
        split_ledger_sha256=_hash({
            "schema_version": ledger.schema_version,
            "symbol": ledger.symbol,
            "events": event_array,
        }),
    )
