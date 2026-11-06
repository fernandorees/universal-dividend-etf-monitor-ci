"""P01 read-only held-close peak cross-check, NOT historical champion parity.

Safety limits: starts flat, manually declared fills are ASSUMED to have occurred
at the regular-session opening, and the replay window cannot contain splits.
No signal calculation, holding mutation, brokerage access or VERIFIED status.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, Overflow, Rounded, Inexact, localcontext

from .manual_ledger import ManualLedger, audit_manual_ledger
from .models import Bar, MonitorDataError, Position, validate_bars
from .split_audit import SplitLedger


@dataclass(frozen=True)
class HeldPeakAudit:
    status: str
    schema_version: int
    symbol: str
    through_session: str
    holding_periods_started: int
    held_closing_sessions: int
    latest_holding_entry: str | None
    reconstructed_highest_held_close: float | None
    position_claimed_peak_close: float | None
    assumed_fills_at_regular_open: bool
    fill_timing_independently_attested: bool
    split_events_in_replay: int
    original_peak_initialization_parity_verified: bool
    source_prices_independently_attested: bool
    manual_ledger_sha256: str
    adjusted_bars_sha256: str

    def as_json(self) -> dict:
        return asdict(self)


def audit_held_close_peak(
    bars: list[Bar], ledger: ManualLedger, position: Position,
    *, split_ledger: SplitLedger,
) -> HeldPeakAudit:
    """Reconstruct highest held *closing* price from self-declared opening fills.

    This is an independent close-only check. The definitive original strategy's
    entry/peak seed parity cannot be proven without the private archived replay.
    Do NOT use its calculated value to modify a real holding or signal.
    """
    validate_bars(bars)
    if not isinstance(split_ledger, SplitLedger):
        raise MonitorDataError("Peak audit requires an explicit versioned split ledger")
    if split_ledger.symbol != position.symbol.upper():
        raise MonitorDataError("Peak split ledger symbol must agree with position")
    # This call validates event chronology, exact balance replay, latest review,
    # claimed confirmations and all interim nonnegative cash/share balances.
    manual = audit_manual_ledger(
        ledger, position, sessions=[bar.session for bar in bars]
    )
    if ledger.checkpoint_shares != Decimal(0):
        raise MonitorDataError("Peak audit requires a flat zero-share checkpoint")
    if position.reconciled_through != bars[-1].session:
        raise MonitorDataError("Peak audit requires latest-session position reconciliation")

    checkpoint = ledger.checkpoint_session
    for split in split_ledger.events:
        if checkpoint < split.effective_session <= bars[-1].session:
            raise MonitorDataError(
                "Peak replay crosses an unmodeled stock split; share conversion reconciliation required"
            )

    # Original event schema contains a date but no time-of-day. At most one
    # DIRECTION of opening fill is admissible per session; an intraday flip is
    # ambiguous and is refused instead of inventing a held closing state.
    directions: dict[date, set[str]] = {}
    for event in ledger.events:
        if event.kind in {"BUY_FILL", "SELL_FILL"}:
            directions.setdefault(event.session, set()).add(event.kind)
    if any(len(kinds) > 1 for kinds in directions.values()):
        raise MonitorDataError("Peak audit refuses same-session buy-and-sell fills without timing evidence")

    event_index = 0
    shares = Decimal(0)
    peak: float | None = None
    latest_entry: date | None = None
    held_sessions = holding_periods = 0
    events = ledger.events
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            for trap in (InvalidOperation, Overflow, Rounded, Inexact):
                ctx.traps[trap] = True
            for bar in bars:
                if bar.session <= checkpoint:
                    continue
                # Replay *declared* fills before the session close. This is an
                # explicitly documented unverified REGULAR_OPEN assumption.
                while event_index < len(events) and events[event_index].session == bar.session:
                    event = events[event_index]
                    new_shares = shares + event.shares_delta
                    if shares == 0 and new_shares > 0:
                        holding_periods += 1
                        latest_entry = bar.session
                        peak = None
                    if new_shares == 0:
                        peak = None
                    if new_shares < 0:
                        raise MonitorDataError("Peak audit encountered negative held shares")
                    shares = new_shares
                    event_index += 1
                if shares > 0:
                    held_sessions += 1
                    peak = bar.close if peak is None else max(peak, bar.close)
    except ArithmeticError as exc:
        raise MonitorDataError("Peak audit share arithmetic exceeded exact decimal limits") from exc

    if event_index != len(events):
        raise MonitorDataError("Peak audit did not consume all manual events")
    if shares != Decimal(str(position.shares)):
        raise MonitorDataError("Peak audit replayed shares differ from declared position")
    if peak != position.peak_close:
        raise MonitorDataError(
            "Highest held closing price mismatch: reconstructed peak does not match position peak_close"
        )

    canonical = [
        [bar.session.isoformat(), bar.open, bar.high, bar.low, bar.close]
        for bar in bars if bar.session > checkpoint
    ]
    price_sha = hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return HeldPeakAudit(
        status="CLOSE_PEAK_MATCHES_SELF_DECLARED_OPENING_FILLS_UNATTESTED",
        schema_version=1, symbol=position.symbol.upper(),
        through_session=bars[-1].session.isoformat(),
        holding_periods_started=holding_periods, held_closing_sessions=held_sessions,
        latest_holding_entry=latest_entry.isoformat() if latest_entry else None,
        reconstructed_highest_held_close=peak,
        position_claimed_peak_close=position.peak_close,
        assumed_fills_at_regular_open=True,
        fill_timing_independently_attested=False,
        split_events_in_replay=0,
        original_peak_initialization_parity_verified=False,
        source_prices_independently_attested=False,
        manual_ledger_sha256=manual.ledger_sha256,
        adjusted_bars_sha256=price_sha,
    )
