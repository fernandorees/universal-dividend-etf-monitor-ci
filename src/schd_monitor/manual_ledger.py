"""P01 read-only replay of SELF-DECLARED manual transactions.

Never submits orders, persists a portfolio, assumes candidate fills, or sets
production VERIFIED status. This is NOT a brokerage or position attestation.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, Inexact, InvalidOperation, Overflow, Rounded, localcontext
from pathlib import Path

from .models import MonitorDataError, Position

MANUAL_LEDGER_SCHEMA = 1
EVENT_KINDS = frozenset({
    "BUY_FILL", "SELL_FILL", "CASH_CONTRIBUTION",
    "CASH_WITHDRAWAL", "DIVIDEND_CREDIT",
})
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
# Match reviewed offline provider ticker grammar; this is local input
# validation, not proof of symbol identity or brokerage holdings.
_TICKER = re.compile(r"[A-Z][A-Z0-9.]{0,14}\Z")


def _decimal(text: object, label: str) -> Decimal:
    """Require unambiguous fixed-point decimal strings, never JSON floats."""
    if not isinstance(text, str) or len(text) > 80 or not _DECIMAL.fullmatch(text):
        raise MonitorDataError(f"{label} must be a finite fixed-point decimal string")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise MonitorDataError(f"Invalid {label}") from exc
    if not value.is_finite():
        raise MonitorDataError(f"{label} must be finite")
    return value


def _date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise MonitorDataError(f"{label} must be YYYY-MM-DD")
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise MonitorDataError(f"Invalid {label}") from exc
    if day.isoformat() != value:
        raise MonitorDataError(f"{label} must be YYYY-MM-DD")
    return day


def _symbol(value: object) -> str:
    if type(value) is not str or not _TICKER.fullmatch(value):
        raise MonitorDataError("Manual ledger symbol must be a bounded uppercase ticker")
    return value


@dataclass(frozen=True)
class ManualEvent:
    sequence: int
    event_id: str
    session: date
    kind: str
    shares_delta: Decimal
    cash_delta: Decimal
    manually_confirmed: bool

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise MonitorDataError("Manual event sequence must be a positive integer")
        if (not isinstance(self.event_id, str) or not self.event_id
                or self.event_id.strip() != self.event_id
                or any(c.isspace() for c in self.event_id)):
            raise MonitorDataError("Manual event_id must be nonempty with no whitespace")
        if type(self.session) is not date:
            raise MonitorDataError("Manual event session must be a date")
        if type(self.manually_confirmed) is not bool or self.manually_confirmed is not True:
            raise MonitorDataError("Only explicitly manually_confirmed events are allowed")
        if not isinstance(self.kind, str) or self.kind not in EVENT_KINDS:
            raise MonitorDataError("Unsupported manual ledger event kind")
        if any(not isinstance(v, Decimal) or not v.is_finite()
               for v in (self.shares_delta, self.cash_delta)):
            raise MonitorDataError("Manual event deltas must be finite Decimal values")
        shares, cash = self.shares_delta, self.cash_delta
        if self.kind == "BUY_FILL" and not (shares > 0 and cash < 0):
            raise MonitorDataError("BUY_FILL requires positive shares and negative net cash")
        if self.kind == "SELL_FILL" and not (shares < 0 and cash > 0):
            raise MonitorDataError("SELL_FILL requires negative shares and positive net cash")
        if self.kind == "CASH_CONTRIBUTION" and not (shares == 0 and cash > 0):
            raise MonitorDataError("CASH_CONTRIBUTION requires zero shares and positive cash")
        if self.kind == "CASH_WITHDRAWAL" and not (shares == 0 and cash < 0):
            raise MonitorDataError("CASH_WITHDRAWAL requires zero shares and negative cash")
        if self.kind == "DIVIDEND_CREDIT" and not (shares == 0 and cash > 0):
            raise MonitorDataError("DIVIDEND_CREDIT requires zero shares and positive cash")


@dataclass(frozen=True)
class ManualLedger:
    symbol: str
    checkpoint_session: date
    checkpoint_shares: Decimal
    checkpoint_cash: Decimal
    reviewed_through: date
    activity_reviewed: bool
    events: tuple[ManualEvent, ...]
    schema_version: int = MANUAL_LEDGER_SCHEMA

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        if type(self.schema_version) is not int or self.schema_version != MANUAL_LEDGER_SCHEMA:
            raise MonitorDataError("Unsupported manual-ledger schema version")
        if type(self.checkpoint_session) is not date or type(self.reviewed_through) is not date:
            raise MonitorDataError("Manual ledger checkpoint/reviewed dates are required")
        if self.checkpoint_session > self.reviewed_through:
            raise MonitorDataError("Manual ledger checkpoint cannot follow review date")
        if self.activity_reviewed is not True:
            raise MonitorDataError("Explicit activity_reviewed=true required through latest session")
        if any(not isinstance(v, Decimal) or not v.is_finite() or v < 0
               for v in (self.checkpoint_shares, self.checkpoint_cash)):
            raise MonitorDataError("Manual ledger checkpoint shares/cash must be nonnegative Decimals")
        if not isinstance(self.events, tuple) or any(type(e) is not ManualEvent for e in self.events):
            raise MonitorDataError("Manual ledger events must be a tuple of ManualEvent")
        if len({e.event_id for e in self.events}) != len(self.events):
            raise MonitorDataError("Duplicate manual event_id")
        for number, event in enumerate(self.events, 1):
            if event.sequence != number:
                raise MonitorDataError("Manual event sequence must be contiguous starting at 1")
            if not (self.checkpoint_session < event.session <= self.reviewed_through):
                raise MonitorDataError("Manual event must be after checkpoint and through review date")
        if any(a.session > b.session for a, b in zip(self.events, self.events[1:])):
            raise MonitorDataError("Manual event dates must be chronological")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MonitorDataError("Duplicate manual-ledger JSON field")
        result[key] = value
    return result


def manual_ledger_from_json(path: str | Path) -> ManualLedger:
    """Read a local file without writing or authenticating any manual assertion."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise MonitorDataError("Invalid manual-ledger JSON") from exc
    required = {
        "schema_version", "symbol", "checkpoint_session", "checkpoint_shares",
        "checkpoint_cash", "reviewed_through", "activity_reviewed", "events",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise MonitorDataError("Manual ledger must contain exactly the versioned fields")
    if not isinstance(payload["events"], list):
        raise MonitorDataError("Manual ledger events must be a JSON array")
    event_fields = {
        "sequence", "event_id", "session", "kind",
        "shares_delta", "cash_delta", "manually_confirmed",
    }
    events = []
    for i, item in enumerate(payload["events"]):
        if not isinstance(item, dict) or set(item) != event_fields:
            raise MonitorDataError(f"Malformed manual ledger event at index {i}")
        events.append(ManualEvent(
            sequence=item["sequence"], event_id=item["event_id"],
            session=_date(item["session"], "manual event session"),
            kind=item["kind"],
            shares_delta=_decimal(item["shares_delta"], "shares_delta"),
            cash_delta=_decimal(item["cash_delta"], "cash_delta"),
            manually_confirmed=item["manually_confirmed"],
        ))
    return ManualLedger(
        symbol=payload["symbol"],
        checkpoint_session=_date(payload["checkpoint_session"], "checkpoint_session"),
        checkpoint_shares=_decimal(payload["checkpoint_shares"], "checkpoint_shares"),
        checkpoint_cash=_decimal(payload["checkpoint_cash"], "checkpoint_cash"),
        reviewed_through=_date(payload["reviewed_through"], "reviewed_through"),
        activity_reviewed=payload["activity_reviewed"],
        events=tuple(events), schema_version=payload["schema_version"],
    )


@dataclass(frozen=True)
class ManualLedgerAudit:
    status: str
    schema_version: int
    symbol: str
    reviewed_through: str
    events_replayed: int
    shares_from_declared_events: str
    cash_from_declared_events: str
    peak_reconciled: bool
    independently_attested: bool
    ledger_sha256: str

    def as_json(self) -> dict:
        return asdict(self)


def audit_manual_ledger(ledger: ManualLedger, position: Position, *, sessions: list[date]) -> ManualLedgerAudit:
    """Compare self-declared event replay to Position without mutating either.

    Cash deltas are ACTUAL *declared* net amounts, not modeled execution
    costs, estimated fills, assumed monthly contributions or dividend accruals.
    The position's held peak is deliberately NOT reconciled in this module.
    """
    if not isinstance(ledger, ManualLedger) or not isinstance(position, Position):
        raise MonitorDataError("Manual ledger and position are required")
    if not isinstance(sessions, list) or not sessions or any(type(s) is not date for s in sessions):
        raise MonitorDataError("Manual ledger requires complete session date history")
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise MonitorDataError("Manual ledger session history must be sorted and unique")
    if ledger.symbol != position.symbol.upper():
        raise MonitorDataError("Manual ledger symbol disagrees with position")
    if ledger.reviewed_through != sessions[-1] or position.reconciled_through != sessions[-1]:
        raise MonitorDataError("Manual ledger and position must be reviewed through latest session")
    available = set(sessions)
    if ledger.checkpoint_session not in available:
        raise MonitorDataError("Manual ledger checkpoint not within supplied sessions")
    if any(e.session not in available for e in ledger.events):
        raise MonitorDataError("Manual ledger contains event on absent market session")

    try:
        with localcontext() as context:
            context.prec = 60
            for trap in (Overflow, Inexact, Rounded, InvalidOperation):
                context.traps[trap] = True
            shares, cash = ledger.checkpoint_shares, ledger.checkpoint_cash
            for event in ledger.events:
                shares += event.shares_delta
                cash += event.cash_delta
                if shares < 0 or cash < 0:
                    raise MonitorDataError(f"Manual ledger negative shares/cash after {event.event_id}")
    except ArithmeticError as exc:
        raise MonitorDataError("Manual ledger amount exceeds exact arithmetic limits") from exc

    # P01 position model contains floats; str() is their stable displayed decimal.
    # Strict comparison fails closed rather than silently rounding away differences.
    if shares != Decimal(str(position.shares)) or cash != Decimal(str(position.available_cash)):
        raise MonitorDataError("Manual ledger replay does not match declared position shares/cash")

    canonical = {
        "schema_version": ledger.schema_version, "symbol": ledger.symbol,
        "checkpoint_session": ledger.checkpoint_session.isoformat(),
        "checkpoint_shares": str(ledger.checkpoint_shares),
        "checkpoint_cash": str(ledger.checkpoint_cash),
        "reviewed_through": ledger.reviewed_through.isoformat(),
        "activity_reviewed": ledger.activity_reviewed,
        "events": [
            [e.sequence, e.event_id, e.session.isoformat(), e.kind,
             str(e.shares_delta), str(e.cash_delta), e.manually_confirmed]
            for e in ledger.events
        ],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ManualLedgerAudit(
        status="MATCHES_SELF_DECLARED_LEDGER_UNATTESTED", schema_version=1,
        symbol=ledger.symbol, reviewed_through=ledger.reviewed_through.isoformat(),
        events_replayed=len(ledger.events), shares_from_declared_events=str(shares),
        cash_from_declared_events=str(cash), peak_reconciled=False,
        independently_attested=False, ledger_sha256=digest,
    )
