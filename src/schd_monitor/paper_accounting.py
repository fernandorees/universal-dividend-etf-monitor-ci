"""P01 synthetic-only, single-symbol PAPER accounting kernel.

NOT a champion strategy simulator; does not generate signals or fills.
All opening fills and day-level contributions/interest are explicitly supplied.
Stock splits, historical source parity, monthly scheduling, interest accrual
policy, withholding, and any broker connectivity are OUT OF SCOPE.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, Inexact, Rounded, Overflow, localcontext

from .models import MonitorDataError
from .split_audit import SplitLedger

PAPER_ACCOUNTING_SCHEMA = 1
FRICTION_PER_SIDE = Decimal("0.0005")  # Frozen scenario's 0.05% modeled execution friction.


def _valid_decimal(value: object, label: str, *, strictly_positive: bool = False) -> Decimal:
    if (type(value) is not Decimal or not value.is_finite()
            or value < 0 or (strictly_positive and value == 0)):
        raise MonitorDataError(f"{label} must be a finite {'positive' if strictly_positive else 'nonnegative'} Decimal")
    if len(value.as_tuple().digits) > 55 or abs(value.as_tuple().exponent) > 55:
        raise MonitorDataError(f"{label} exceeds synthetic accounting precision bounds")
    return value


def _date(value: object, label: str) -> date:
    if type(value) is not date or value.weekday() >= 5:
        raise MonitorDataError(f"{label} must be a weekday date")
    return value


@dataclass(frozen=True)
class PaperDay:
    session: date
    open: Decimal
    close: Decimal
    contribution_before_open: Decimal = Decimal("0")
    explicit_interest_credit_before_open: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        _date(self.session, "Paper day")
        _valid_decimal(self.open, "Paper open", strictly_positive=True)
        _valid_decimal(self.close, "Paper close", strictly_positive=True)
        _valid_decimal(self.contribution_before_open, "Paper contribution")
        _valid_decimal(self.explicit_interest_credit_before_open, "Paper interest credit")


@dataclass(frozen=True)
class PaperFill:
    session: date
    side: str
    quantity: Decimal
    discretionary_fee: Decimal

    def __post_init__(self) -> None:
        _date(self.session, "Paper fill session")
        if type(self.side) is not str or self.side not in {"BUY", "SELL"}:
            raise MonitorDataError("Paper fill side must be BUY or SELL")
        _valid_decimal(self.quantity, "Paper fill quantity", strictly_positive=True)
        _valid_decimal(self.discretionary_fee, "Paper discretionary fee")


@dataclass(frozen=True)
class PaperDividend:
    event_id: str
    ex_session: date
    payment_session: date
    cash_per_share: Decimal

    def __post_init__(self) -> None:
        if (type(self.event_id) is not str or not self.event_id
                or len(self.event_id) > 80 or any(c.isspace() for c in self.event_id)):
            raise MonitorDataError("Paper dividend event_id must be nonempty without whitespace")
        _date(self.ex_session, "Paper dividend ex_session")
        _date(self.payment_session, "Paper dividend payment_session")
        if self.payment_session <= self.ex_session:
            raise MonitorDataError("Paper dividend payment must follow ex session")
        _valid_decimal(self.cash_per_share, "Paper dividend cash_per_share", strictly_positive=True)


@dataclass(frozen=True)
class PaperDaySnapshot:
    session: str
    contribution_before_open: str
    explicit_interest_credit_before_open: str
    dividend_ex_entitlement: str
    dividend_payment_before_open: str
    paper_fill: str
    paper_execution_price_after_friction: str | None
    paper_discretionary_fee: str
    shares_at_close: str
    cash_at_close: str
    held_peak_close_only: str | None
    unpaid_dividend_entitlement: str
    marked_equity_at_close: str
    cash_plus_equity_excluding_unpaid_entitlements: str

    def as_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PaperAccountingReport:
    status: str
    schema_version: int
    symbol: str
    assumed_opening_fills: bool
    trades_are_explicit_paper_fixtures_not_signals: bool
    original_champion_parity_verified: bool
    independently_verified_corporate_actions: bool
    rows: tuple[PaperDaySnapshot, ...]

    def as_json(self) -> dict:
        result = asdict(self)
        result["rows"] = [row.as_json() for row in self.rows]
        return result


def replay_explicit_paper_events(
    *, symbol: str, days: list[PaperDay], fills: list[PaperFill],
    dividends: list[PaperDividend], split_ledger: SplitLedger,
    initial_cash: Decimal,
) -> PaperAccountingReport:
    """Read-only synthetic bookkeeping: NO generated fills, strategy or live cash.

    Event order per session:
      (1) accrue ex-date entitlement from PRE-OPEN holdings,
      (2) explicit deposit/interest plus due dividend payments at opening,
      (3) supplied hypothetical fill at opening + per-side friction/fee,
      (4) close-only peak and marked-equity snapshot.
    No split event may fall inside the replay window; stock-split share
    conversions require a separately reviewed future implementation.
    """
    if type(symbol) is not str or not symbol or symbol != symbol.strip().upper():
        raise MonitorDataError("Paper replay symbol must be an uppercase ticker")
    _valid_decimal(initial_cash, "Paper initial cash")
    if type(split_ledger) is not SplitLedger or split_ledger.symbol != symbol:
        raise MonitorDataError("Paper replay requires a matching versioned split ledger")
    if not isinstance(days, list) or len(days) < 2 or any(type(x) is not PaperDay for x in days):
        raise MonitorDataError("Paper replay needs at least two typed sessions")
    sessions = [day.session for day in days]
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise MonitorDataError("Paper sessions must be strictly ordered without duplicates")
    if any((b-a).days > 7 for a, b in zip(sessions, sessions[1:])):
        raise MonitorDataError("Paper session history has a gap exceeding seven days")
    if any(days[0].session <= e.effective_session <= days[-1].session
           for e in split_ledger.events):
        raise MonitorDataError("Paper replay refuses stock splits; share/price conversion unimplemented")
    coverage = set(sessions)
    if not isinstance(fills, list) or any(type(x) is not PaperFill for x in fills):
        raise MonitorDataError("Paper fills must be a list of typed explicit hypothetical fills")
    if not isinstance(dividends, list) or any(type(x) is not PaperDividend for x in dividends):
        raise MonitorDataError("Paper dividends must be a list of typed events")
    if any(fill.session not in coverage for fill in fills):
        raise MonitorDataError("Paper fill occurs outside supplied session history")
    if len({fill.session for fill in fills}) != len(fills):
        raise MonitorDataError("Paper replay refuses ambiguous multiple opening fills per session")
    if (len({event.event_id for event in dividends}) != len(dividends)
            or len({(event.ex_session, event.payment_session) for event in dividends}) != len(dividends)):
        raise MonitorDataError("Duplicate or ambiguous paper dividend events")
    if any(e.ex_session not in coverage or e.payment_session not in coverage for e in dividends):
        raise MonitorDataError("Paper dividend ex and payment sessions must both be covered")
    fill_by_day = {fill.session: fill for fill in fills}
    ex_by_day = {}
    pay_by_day = {}
    for div in dividends:
        ex_by_day.setdefault(div.ex_session, []).append(div)
        pay_by_day.setdefault(div.payment_session, []).append(div)
    shares = Decimal(0)
    cash = initial_cash
    peak = None
    receivable: dict[str, Decimal] = {}
    snapshots: list[PaperDaySnapshot] = []
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            for trap in (Overflow, InvalidOperation, Rounded, Inexact):
                ctx.traps[trap] = True
            for day in days:
                entitled_today = Decimal(0)
                paid_today = Decimal(0)
                for event in ex_by_day.get(day.session, []):
                    owed = event.cash_per_share * shares
                    receivable[event.event_id] = owed
                    entitled_today += owed
                for event in pay_by_day.get(day.session, []):
                    if event.event_id not in receivable:
                        raise MonitorDataError("Paper dividend payment missing ex-date entitlement")
                    paid_today += receivable.pop(event.event_id)
                cash += day.contribution_before_open
                cash += day.explicit_interest_credit_before_open
                cash += paid_today
                fill = fill_by_day.get(day.session)
                paid_fee = Decimal(0)
                effective_price = None
                if fill is not None:
                    paid_fee = fill.discretionary_fee
                    if fill.side == "BUY":
                        effective_price = day.open * (1 + FRICTION_PER_SIDE)
                        total = effective_price * fill.quantity + paid_fee
                        if total > cash:
                            raise MonitorDataError("Paper BUY would exceed available cash")
                        cash -= total
                        shares += fill.quantity
                    else:
                        if fill.quantity > shares:
                            raise MonitorDataError("Paper SELL exceeds held shares")
                        effective_price = day.open * (1 - FRICTION_PER_SIDE)
                        total = effective_price * fill.quantity - paid_fee
                        if total < 0:
                            raise MonitorDataError("Paper SELL net proceeds cannot be negative")
                        cash += total
                        shares -= fill.quantity
                        if shares == 0:
                            peak = None
                if shares > 0:
                    peak = day.close if peak is None else max(peak, day.close)
                unpaid = sum(receivable.values(), Decimal(0))
                equity = shares * day.close
                marked = cash + equity
                snapshots.append(PaperDaySnapshot(
                    session=day.session.isoformat(),
                    contribution_before_open=str(day.contribution_before_open),
                    explicit_interest_credit_before_open=str(day.explicit_interest_credit_before_open),
                    dividend_ex_entitlement=str(entitled_today),
                    dividend_payment_before_open=str(paid_today),
                    paper_fill=fill.side if fill is not None else "NONE",
                    paper_execution_price_after_friction=(
                        str(effective_price) if effective_price is not None else None
                    ),
                    paper_discretionary_fee=str(paid_fee),
                    shares_at_close=str(shares),
                    cash_at_close=str(cash),
                    held_peak_close_only=str(peak) if peak is not None else None,
                    unpaid_dividend_entitlement=str(unpaid),
                    marked_equity_at_close=str(equity),
                    cash_plus_equity_excluding_unpaid_entitlements=str(marked),
                ))
    except ArithmeticError as exc:
        raise MonitorDataError("Paper accounting precision exceeded exact Decimal limits") from exc

    return PaperAccountingReport(
        status="SYNTHETIC_EVENT_ACCOUNTING_ONLY_UNVERIFIED",
        schema_version=PAPER_ACCOUNTING_SCHEMA,
        symbol=symbol,
        assumed_opening_fills=True,
        trades_are_explicit_paper_fixtures_not_signals=True,
        original_champion_parity_verified=False,
        independently_verified_corporate_actions=False,
        rows=tuple(snapshots),
    )
