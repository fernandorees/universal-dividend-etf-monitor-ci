"""P01 synthetic CHAMPION signal-to-PAPER fill schedule AUDIT only.

The frozen signal engine remains authoritative for signal conditions. The
existing paper accounting module replays ONLY explicit hypothetical fills.
This bridge cross-checks their timing; it never generates a fill or order.
NOT the historical source simulator, operational fills, or VERIFIED signals.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from .indicators import calculate
from .models import Bar, MonitorDataError, Position, validate_bars
from .paper_accounting import (
    PaperDay, PaperDividend, PaperFill, replay_explicit_paper_events,
)
from .signals import Strategy, evaluate
from .split_audit import SplitLedger

BRIDGE_SCHEMA = 1


@dataclass(frozen=True)
class SyntheticChampionBridgeAudit:
    status: str
    schema_version: int
    symbol: str
    sessions_checked: int
    completed_close_decisions_checked: int
    hypothetical_fills_checked: int
    explicit_initial_opening_buy: bool
    first_evaluable_signal_session: str
    last_signal_session: str
    last_unexecuted_end_of_history_candidate: str
    historical_order_sizing_parity_verified: bool
    original_peak_initialization_parity_verified: bool
    historical_reference_parity_verified: bool
    price_sources_independently_verified: bool
    trading_status: str

    def as_json(self) -> dict:
        return asdict(self)


def audit_synthetic_champion_fill_schedule(
    *, symbol: str, bars: list[Bar], paper_days: list[PaperDay],
    hypothetical_fills: list[PaperFill],
    dividends: list[PaperDividend], split_ledger: SplitLedger,
    initial_cash: Decimal,
) -> SyntheticChampionBridgeAudit:
    """Check ALL explicit paper fills against prior-close CHAMPION decisions.

    Research-only: opening hypothetical fill *quantities* and fees must be
    supplied by fixtures. No real or hypothetical fill is ever inferred from
    a candidate. Full-sale sizing is checked; buy sizing is NOT yet verified
    against the definitive private historical simulator.

    First 65 bars warm up the signal engine. Only an OPTIONAL explicit
    initial opening BUY is allowed before that, at session index zero.
    Thereafter each actionable prior-close candidate must have one matching
    explicit paper fill on the NEXT supplied session opening. A candidate
    on the final bar is just a pending RESEARCH intention, not an order.
    """
    validate_bars(bars)
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
        raise MonitorDataError("Synthetic bridge requires uppercase symbol")
    # Do not duplicate the eligible-instrument registry here. The frozen
    # signal evaluator owns its own current supported-ticker validation.
    # Future optional ETF universes must not require editing this bridge.
    if (not isinstance(paper_days, list) or len(paper_days) != len(bars)
            or any(type(d) is not PaperDay for d in paper_days)):
        raise MonitorDataError("Synthetic bridge paper days must align with all OHLC sessions")
    for bar, day in zip(bars, paper_days):
        if (bar.session != day.session
                or Decimal(str(bar.open)) != day.open
                or Decimal(str(bar.close)) != day.close):
            raise MonitorDataError("Synthetic bridge price dates/open/close disagree with paper ledger")
    if (not isinstance(hypothetical_fills, list)
            or any(type(f) is not PaperFill for f in hypothetical_fills)):
        raise MonitorDataError("Synthetic bridge requires explicitly typed paper fills")
    fills_by_day = {fill.session: fill for fill in hypothetical_fills}
    if len(fills_by_day) != len(hypothetical_fills):
        raise MonitorDataError("Synthetic bridge cannot infer multiple same-day executions")
    allowed_initial = fills_by_day.get(bars[0].session)
    if allowed_initial is not None and allowed_initial.side != "BUY":
        raise MonitorDataError("Only an explicit initial opening BUY is permitted on first session")
    # Signal readiness is explicitly NOT backfilled: before session index 64,
    # there is no fully warmed strategy decision. Refuse made-up other fills.
    if any(day.session in fills_by_day for day in paper_days[1:65]):
        raise MonitorDataError("Synthetic bridge refuses unsignaled paper fills during indicator warmup")

    # Paper ledger is independent and immutable. The next-open price, fee,
    # dividends and available cash come from the tested accounting component,
    # not from a new homegrown strategy/backtest implementation.
    accounting = replay_explicit_paper_events(
        symbol=symbol, days=paper_days, fills=hypothetical_fills,
        dividends=dividends, split_ledger=split_ledger, initial_cash=initial_cash,
    )
    indicators = calculate(bars)
    final_action = ""
    for i in range(64, len(bars)):
        row = accounting.rows[i]
        held_peak = (float(row.held_peak_close_only)
                     if row.held_peak_close_only is not None else None)
        position = Position(
            symbol=symbol, shares=float(row.shares_at_close),
            available_cash=float(row.cash_at_close),
            peak_close=held_peak, reconciled_through=bars[i].session,
        )
        decision = evaluate(
            bars[:i + 1], indicators[:i + 1], position,
            strategy=Strategy.CHAMPION, data_verified=False,
        )
        if (decision.verification != "UNVERIFIED_DO_NOT_TRADE"
                or decision.execution != "NEXT_REGULAR_US_TRADING_OPEN"):
            raise MonitorDataError("Synthetic bridge refuses nonprovisional or mistimed signal")
        final_action = decision.candidate_action
        if i == len(bars) - 1:
            break
        following = bars[i + 1].session
        fill = fills_by_day.get(following)
        actual = fill.side if fill is not None else "NONE"
        expected = (decision.candidate_action if decision.candidate_action in {"BUY", "SELL"}
                    else "NONE")
        if actual != expected:
            raise MonitorDataError(
                f"Synthetic champion candidate/fill mismatch at {following}: "
                "no assumed or missing next-opening execution permitted"
            )
        # Frozen champion sells the ENTIRE invested position at next open.
        if actual == "SELL" and fill.quantity != Decimal(row.shares_at_close):
            raise MonitorDataError("Synthetic champion SELL must liquidate all paper shares")

    return SyntheticChampionBridgeAudit(
        status="SYNTHETIC_CHAMPION_TO_EXPLICIT_PAPER_FILLS_CONSISTENT_UNVERIFIED",
        schema_version=BRIDGE_SCHEMA, symbol=symbol,
        sessions_checked=len(bars),
        completed_close_decisions_checked=len(bars) - 64,
        hypothetical_fills_checked=len(hypothetical_fills),
        explicit_initial_opening_buy=allowed_initial is not None,
        first_evaluable_signal_session=bars[64].session.isoformat(),
        last_signal_session=bars[-1].session.isoformat(),
        last_unexecuted_end_of_history_candidate=final_action,
        historical_order_sizing_parity_verified=False,
        original_peak_initialization_parity_verified=False,
        historical_reference_parity_verified=False,
        price_sources_independently_verified=False,
        trading_status="UNVERIFIED_DO_NOT_TRADE",
    )
