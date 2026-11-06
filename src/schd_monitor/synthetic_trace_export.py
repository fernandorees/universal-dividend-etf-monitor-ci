"""P01 synthetic-only normalized 24-field trace exporter.

Builds a trace from already-declared fictional opening fills and the UNCHANGED
champion evaluator. It cannot attest original historical parity or source data.
Never infer real/paper fills, connect to brokers or promote VERIFIED status.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from .indicators import calculate
from .models import Bar, MonitorDataError, Position
from .paper_accounting import (
    PaperDay, PaperDividend, PaperFill, replay_explicit_paper_events,
)
from .reference_parity import TRACE_COLUMNS
from .signals import Strategy, evaluate
from .split_audit import SplitLedger
from .synthetic_champion_bridge import audit_synthetic_champion_fill_schedule

SYNTHETIC_TRACE_SCHEMA = 1


@dataclass(frozen=True)
class SyntheticTrace:
    status: str
    schema_version: int
    symbol: str
    historical_reference_parity_verified: bool
    source_prices_independently_verified: bool
    original_peak_initialization_verified: bool
    fee_and_buy_sizing_parity_verified: bool
    rows: tuple[dict[str, str], ...]


def _fixed(value: float | str | Decimal | None) -> str:
    """Fixed-point decimal representation, preserving underlying values."""
    if value is None:
        return ""
    return format(Decimal(str(value)), "f")


def build_synthetic_champion_trace(
    *, symbol: str, bars: list[Bar], paper_days: list[PaperDay],
    hypothetical_fills: list[PaperFill], dividends: list[PaperDividend],
    split_ledger: SplitLedger, initial_cash: Decimal,
) -> SyntheticTrace:
    """Assemble a synthetic row per completed bar in parity schema-v1 order.

    The bridge must pass FIRST, to refuse invented or missing executions.
    Prior to signal warmup, signal_action=NO_SIGNAL and flags are false.
    All later decisions are the frozen champion's unverified candidates.
    The accounting rows represent ONLY supplied fictional fills.
    """
    audit = audit_synthetic_champion_fill_schedule(
        symbol=symbol, bars=bars, paper_days=paper_days,
        hypothetical_fills=hypothetical_fills, dividends=dividends,
        split_ledger=split_ledger, initial_cash=initial_cash,
    )
    if (audit.trading_status != "UNVERIFIED_DO_NOT_TRADE"
            or audit.historical_reference_parity_verified):
        raise MonitorDataError("Synthetic trace export cannot use attested/production status")
    paper = replay_explicit_paper_events(
        symbol=symbol, days=paper_days, fills=hypothetical_fills,
        dividends=dividends, split_ledger=split_ledger, initial_cash=initial_cash,
    )
    indicators = calculate(bars)
    if len(paper.rows) != len(bars) or len(indicators) != len(bars):
        raise MonitorDataError("Synthetic trace accounting/indicator session mismatch")
    rows: list[dict[str, str]] = []
    for i, (bar, ind, accounting) in enumerate(zip(bars, indicators, paper.rows)):
        if accounting.session != bar.session.isoformat() or ind.date != accounting.session:
            raise MonitorDataError("Synthetic trace session mismatch")
        if i >= 64:
            peak = (float(accounting.held_peak_close_only)
                    if accounting.held_peak_close_only is not None else None)
            position = Position(
                symbol=symbol, shares=float(accounting.shares_at_close),
                available_cash=float(accounting.cash_at_close),
                peak_close=peak, reconciled_through=bar.session,
            )
            decision = evaluate(
                bars[:i + 1], indicators[:i + 1], position,
                strategy=Strategy.CHAMPION, data_verified=False,
            )
            if decision.verification != "UNVERIFIED_DO_NOT_TRADE":
                raise MonitorDataError("Synthetic trace refuses verified candidate")
            action = decision.candidate_action
        else:
            action = "NO_SIGNAL"
        values = {
            "session": bar.session.isoformat(),
            "open": _fixed(bar.open),
            "high": _fixed(bar.high),
            "low": _fixed(bar.low),
            "close": _fixed(bar.close),
            "sma60": _fixed(ind.sma60),
            "atr14": _fixed(ind.atr14),
            "macd": _fixed(ind.macd),
            "macd_signal": _fixed(ind.macd_signal),
            "bearish_cross": "true" if ind.bearish_cross else "false",
            "highest_held_close": _fixed(accounting.held_peak_close_only),
            "buy_eligible": "true" if action == "BUY" else "false",
            "sell_eligible": "true" if action == "SELL" else "false",
            "signal_action": action,
            "fill_action": accounting.paper_fill,
            # The paper kernel reports friction-adjusted executed price.
            # The original archive's fill_price definition is NOT mapped.
            "fill_price": _fixed(accounting.paper_execution_price_after_friction),
            "shares": _fixed(accounting.shares_at_close),
            "cash": _fixed(accounting.cash_at_close),
            "monthly_deposit": _fixed(accounting.contribution_before_open),
            "dividend_ex_accrual": _fixed(accounting.dividend_ex_entitlement),
            "dividend_payment": _fixed(accounting.dividend_payment_before_open),
            "interest_credit": _fixed(accounting.explicit_interest_credit_before_open),
            "equity_value": _fixed(accounting.marked_equity_at_close),
            "ending_wealth": _fixed(accounting.cash_plus_equity_excluding_unpaid_entitlements),
        }
        if tuple(values) != TRACE_COLUMNS or any(not isinstance(v, str) for v in values.values()):
            raise MonitorDataError("Synthetic trace versioned 24-field schema mismatch")
        rows.append(values)
    return SyntheticTrace(
        status="SYNTHETIC_NORMALIZED_TRACE_ONLY_UNVERIFIED",
        schema_version=SYNTHETIC_TRACE_SCHEMA, symbol=symbol,
        historical_reference_parity_verified=False,
        source_prices_independently_verified=False,
        original_peak_initialization_verified=False,
        fee_and_buy_sizing_parity_verified=False,
        rows=tuple(rows),
    )


def write_synthetic_trace_csv(trace: SyntheticTrace, path: str | Path) -> None:
    """Explicit local export, creates a NEW file; never overwrites existing."""
    if (type(trace) is not SyntheticTrace
            or trace.status != "SYNTHETIC_NORMALIZED_TRACE_ONLY_UNVERIFIED"
            or trace.historical_reference_parity_verified):
        raise MonitorDataError("Only unverified synthetic traces may be exported")
    if not trace.rows or any(type(row) is not dict or tuple(row) != TRACE_COLUMNS
                             for row in trace.rows):
        raise MonitorDataError("Synthetic trace schema mismatch")
    target = Path(path)
    # Creation is intentionally exclusive. The tool does not import/archive
    # any original reference files or touch user portfolios.
    with target.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=TRACE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(trace.rows)
