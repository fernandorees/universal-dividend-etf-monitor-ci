"""P01 offline gross-dividend entitlement/payment consistency audit.

Entirely self-declared; never infers fills, changes cash, or modifies indicators.
Only synthetic examples belong in GitHub; real portfolio records stay local.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import MonitorDataError

DIVIDEND_SCHEMA = 1


def _money_decimal(value: object, name: str, *, allow_zero: bool) -> Decimal:
    # Decimal strings only: float conversion would silently change cash arithmetic.
    if not isinstance(value, str) or not value or value.strip() != value:
        raise MonitorDataError(f"{name} must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise MonitorDataError(f"{name} is not a valid decimal") from exc
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise MonitorDataError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    return number


def _iso_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise MonitorDataError(f"{name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MonitorDataError(f"{name} must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise MonitorDataError(f"{name} must be YYYY-MM-DD")
    return parsed


@dataclass(frozen=True)
class DividendEntry:
    event_id: str
    ex_session: date
    payment_session: date
    cash_per_share: Decimal
    eligible_shares_before_ex_open: Decimal
    gross_cash_recorded_at_payment_open: Decimal | None

    def __post_init__(self) -> None:
        if (not isinstance(self.event_id, str) or not self.event_id
                or self.event_id.strip() != self.event_id
                or any(character.isspace() for character in self.event_id)):
            raise MonitorDataError("Dividend event_id must be nonempty and contain no whitespace")
        if type(self.ex_session) is not date or type(self.payment_session) is not date:
            raise MonitorDataError("Dividend ex/payment sessions must be dates")
        if self.payment_session < self.ex_session:
            raise MonitorDataError("Dividend payment session cannot precede ex session")
        for value, name, zero in (
            (self.cash_per_share, "cash_per_share", False),
            (self.eligible_shares_before_ex_open, "eligible_shares_before_ex_open", True),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (not zero and value == 0):
                raise MonitorDataError(f"Dividend {name} must be a valid Decimal")
        cash = self.gross_cash_recorded_at_payment_open
        if cash is not None and (not isinstance(cash, Decimal) or not cash.is_finite() or cash < 0):
            raise MonitorDataError("Dividend recorded gross cash must be a finite nonnegative Decimal")


@dataclass(frozen=True)
class DividendLedger:
    symbol: str
    events: tuple[DividendEntry, ...]
    schema_version: int = DIVIDEND_SCHEMA

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != DIVIDEND_SCHEMA:
            raise MonitorDataError("Unsupported dividend-ledger schema")
        if (not isinstance(self.symbol, str) or not self.symbol
                or self.symbol != self.symbol.strip().upper()):
            raise MonitorDataError("Dividend-ledger symbol must be an uppercase ticker")
        if not isinstance(self.events, tuple) or any(type(e) is not DividendEntry for e in self.events):
            raise MonitorDataError("Dividend events must be a tuple of DividendEntry")
        if len({e.event_id for e in self.events}) != len(self.events):
            raise MonitorDataError("Duplicate dividend event_id")
        if any(a.ex_session > b.ex_session for a, b in zip(self.events, self.events[1:])):
            raise MonitorDataError("Dividend events must be ordered by ex session")


def dividend_ledger_from_json(path: str | Path) -> DividendLedger:
    """Load an explicitly supplied versioned local JSON, not provider attestation."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorDataError("Invalid dividend-ledger JSON") from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "symbol", "events"}:
        raise MonitorDataError("Dividend ledger requires exactly schema_version, symbol, events")
    if not isinstance(data["events"], list):
        raise MonitorDataError("Dividend events must be a JSON list")
    required = {
        "event_id", "ex_session", "payment_session", "cash_per_share",
        "eligible_shares_before_ex_open", "gross_cash_recorded_at_payment_open",
    }
    events = []
    for index, raw in enumerate(data["events"]):
        if not isinstance(raw, dict) or set(raw) != required:
            raise MonitorDataError(f"Malformed dividend entry at index {index}")
        events.append(DividendEntry(
            event_id=raw["event_id"],
            ex_session=_iso_date(raw["ex_session"], "ex_session"),
            payment_session=_iso_date(raw["payment_session"], "payment_session"),
            cash_per_share=_money_decimal(raw["cash_per_share"], "cash_per_share", allow_zero=False),
            eligible_shares_before_ex_open=_money_decimal(
                raw["eligible_shares_before_ex_open"], "eligible_shares_before_ex_open",
                allow_zero=True,
            ),
            gross_cash_recorded_at_payment_open=(
                None if raw["gross_cash_recorded_at_payment_open"] is None
                else _money_decimal(
                    raw["gross_cash_recorded_at_payment_open"],
                    "gross_cash_recorded_at_payment_open", allow_zero=True,
                )
            ),
        ))
    return DividendLedger(data["symbol"], tuple(events), data["schema_version"])


@dataclass(frozen=True)
class DividendAudit:
    status: str
    schema_version: int
    symbol: str
    through_session: str
    events_checked: int
    paid_events: int
    pending_events: int
    gross_entitlement: str
    gross_payment_recorded: str
    gross_pending: str
    ledger_sha256: str

    def as_json(self) -> dict:
        return asdict(self)


def audit_dividend_ledger(
    ledger: DividendLedger, *, expected_symbol: str, sessions: list[date]
) -> DividendAudit:
    """Check declared entitlement arithmetic and payment-open timing, not actual fills.

    Every eligible share amount is an explicit user-supplied claim for holdings
    *before* ex-date opening. Never derive it from today's position or buy/sell
    candidates. Gross amounts are exact Decimal math: NO implicit rounding.
    """
    if not isinstance(ledger, DividendLedger):
        raise MonitorDataError("A versioned dividend ledger is required")
    if (not isinstance(expected_symbol, str) or not expected_symbol
            or expected_symbol != expected_symbol.strip().upper()
            or expected_symbol != ledger.symbol):
        raise MonitorDataError("Dividend-ledger symbol does not match position ticker")
    if not sessions or any(type(day) is not date for day in sessions):
        raise MonitorDataError("Dividend audit requires a nonempty session history")
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise MonitorDataError("Dividend audit requires strictly ascending unique sessions")
    asof = sessions[-1]
    covered = set(sessions)
    due, paid, unpaid = Decimal(0), Decimal(0), Decimal(0)
    paid_count = pending_count = 0
    for entry in ledger.events:
        if entry.ex_session not in covered:
            raise MonitorDataError(f"Dividend ex session absent from supplied history: {entry.event_id}")
        try:
            expected = entry.cash_per_share * entry.eligible_shares_before_ex_open
            due += expected
        except ArithmeticError as exc:
            raise MonitorDataError(f"Dividend amount exceeds supported decimal range: {entry.event_id}") from exc
        if not expected.is_finite() or not due.is_finite():
            raise MonitorDataError(f"Dividend amount is not finite: {entry.event_id}")
        if entry.payment_session <= asof:
            if entry.payment_session not in covered:
                raise MonitorDataError(f"Dividend payment session absent from supplied history: {entry.event_id}")
            if entry.gross_cash_recorded_at_payment_open is None:
                raise MonitorDataError(f"Dividend due payment has no gross cash record: {entry.event_id}")
            if entry.gross_cash_recorded_at_payment_open != expected:
                raise MonitorDataError(f"Dividend gross payment mismatch: {entry.event_id}")
            try:
                paid += expected
            except ArithmeticError as exc:
                raise MonitorDataError(f"Dividend paid total exceeds decimal range: {entry.event_id}") from exc
            paid_count += 1
        else:
            if entry.gross_cash_recorded_at_payment_open is not None:
                raise MonitorDataError(f"Dividend future payment cannot be recorded early: {entry.event_id}")
            try:
                unpaid += expected
            except ArithmeticError as exc:
                raise MonitorDataError(f"Dividend pending total exceeds decimal range: {entry.event_id}") from exc
            pending_count += 1

    canonical = {
        "schema_version": ledger.schema_version, "symbol": ledger.symbol,
        "events": [
            [e.event_id, e.ex_session.isoformat(), e.payment_session.isoformat(),
             str(e.cash_per_share), str(e.eligible_shares_before_ex_open),
             None if e.gross_cash_recorded_at_payment_open is None
             else str(e.gross_cash_recorded_at_payment_open)]
            for e in ledger.events
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DividendAudit(
        status="CONSISTENT_UNATTESTED", schema_version=1,
        symbol=ledger.symbol, through_session=asof.isoformat(),
        events_checked=len(ledger.events), paid_events=paid_count,
        pending_events=pending_count, gross_entitlement=str(due),
        gross_payment_recorded=str(paid), gross_pending=str(unpaid),
        ledger_sha256=fingerprint,
    )
