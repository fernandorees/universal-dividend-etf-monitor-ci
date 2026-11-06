"""Independent frozen SCHD champion historical PAPER replay, no broker interaction.

Takes typed, split-only bars and separately supplied ex/pay-date distributions.
Uses this package's independently verified indicator calculator; never loads
archived research Python or guesses actual investment fills. Historical
execution is strictly HYPOTHETICAL, not an operational trade instruction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite
from typing import Sequence

from .indicators import calculate
from .models import Bar, MonitorDataError, validate_bars

INITIAL_CASH = 100000.0
MONTHLY_CASH = 2000.0
DISCRETIONARY_FEE = 1.0
FRICTION = 0.0005
HYPOTHETICAL_CASH_RATE = 0.03
CASH_DAY_DENOMINATOR = 365.25
MACD_PEAK_DECLINE = 0.995
ATR_STOP_MULTIPLIER = 2.7
FLAT_SHARE_EPSILON = 1e-8
MIN_BUY_CASH = DISCRETIONARY_FEE + 1e-6


@dataclass(frozen=True)
class ResearchDividend:
    ex_session: date
    payment_session: date
    split_adjusted_amount: float

    def __post_init__(self) -> None:
        if (type(self.ex_session) is not date or type(self.payment_session) is not date
                or self.payment_session <= self.ex_session
                or type(self.split_adjusted_amount) not in (float, int)
                or not isfinite(self.split_adjusted_amount)
                or self.split_adjusted_amount <= 0):
            raise MonitorDataError("Research dividend requires ex < payment and positive finite amount")


@dataclass(frozen=True)
class HistoricalDay:
    date: str
    action: str
    kind: str
    shares: float
    cash: float
    peak: float
    wealth: float
    open: float
    close: float
    sma: float
    atr: float
    bear: bool
    atr_trigger: bool
    macd_trigger: bool
    dividend_receivables: float
    deposit: float

    def as_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class HistoricalReplay:
    status: str
    first_open_purchase_session: str
    last_session: str
    purchases_including_first: int
    sales: int
    ending_wealth: float
    maximum_flow_adjusted_drawdown: float
    rows_after_first_open: tuple[HistoricalDay, ...]
    original_source_executed: bool = False
    historical_market_data_independently_verified: bool = False
    production_signal_verified: bool = False


def replay_hypothetical_champion(
    *, bars: list[Bar], dividends: Sequence[ResearchDividend],
    start: date, end: date,
) -> HistoricalReplay:
    """Calculate ALL original frozen accounting mechanics independently.

    Event order for each next opening: prior-close interest accrual, pre-open
    ex-date entitlement on existing shares, monthly cash, next-open SELL or
    BUY based on previous completed close, payment-date dividend handling
    after the opening trade, closing-price peak and receivable-inclusive NAV.
    Matches the archived reference's IEEE-754 float operation ordering.
    Exactly one deterministic hypothetical history, NO real portfolio fills.
    """
    validate_bars(bars)
    if type(start) is not date or type(end) is not date or start >= end:
        raise MonitorDataError("Research history requires start < end dates")
    sessions = {row.session: i for i, row in enumerate(bars)}
    if start not in sessions or end not in sessions:
        raise MonitorDataError("Research dates must be complete sessions in OHLC data")
    a, z = sessions[start], sessions[end]
    if a < 65 or z <= a:
        raise MonitorDataError("Original research requires warmed indicators before starting")
    if not isinstance(dividends, (tuple, list)) or any(type(x) is not ResearchDividend for x in dividends):
        raise MonitorDataError("Research distributions must be typed records")
    if len({d.ex_session for d in dividends}) != len(dividends):
        raise MonitorDataError("Duplicate dividend ex session")
    if any((start < e.ex_session <= end and
            (e.ex_session not in sessions or (e.payment_session <= end and e.payment_session not in sessions)))
           for e in dividends):
        raise MonitorDataError("Dividend dates missing from historical session series")
    ex_by_date = {event.ex_session: event for event in dividends}
    metrics = calculate(bars)
    initial = bars[a]
    held_shares = (INITIAL_CASH - DISCRETIONARY_FEE) / (
        initial.open * (1.0 + FRICTION))
    cash = 0.0
    held_peak = initial.close
    pending: dict[date, float] = {}
    buys = 1
    sales = 0
    time_weighted_index = 1.0
    highest_time_weighted = 1.0
    max_drawdown = 0.0
    previous_net = INITIAL_CASH - DISCRETIONARY_FEE
    history: list[HistoricalDay] = []

    for i in range(a + 1, z + 1):
        previous = bars[i-1]
        current = bars[i]
        previous_indicator = metrics[i-1]
        days = (current.session - previous.session).days
        cash *= ((1.0 + HYPOTHETICAL_CASH_RATE) ** (days / CASH_DAY_DENOMINATOR))

        dividend = ex_by_date.get(current.session)
        if dividend is not None:
            pending[dividend.payment_session] = (
                pending.get(dividend.payment_session, 0.0)
                + held_shares * dividend.split_adjusted_amount
            )

        contribution = (MONTHLY_CASH
                        if (previous.session.year, previous.session.month) !=
                           (current.session.year, current.session.month)
                        else 0.0)
        cash += contribution
        sale_today = False
        action = ""
        kind = ""
        atr_trigger = False
        macd_trigger = False
        if held_shares > FLAT_SHARE_EPSILON:
            atr_trigger = (previous_indicator.atr14 is not None
                           and previous.close < held_peak
                           - ATR_STOP_MULTIPLIER * previous_indicator.atr14)
            macd_trigger = (previous_indicator.bearish_cross
                            and previous.close <= held_peak * MACD_PEAK_DECLINE)
            if atr_trigger or macd_trigger:
                net_sale_proceeds = held_shares * current.open * (1.0 - FRICTION) - DISCRETIONARY_FEE
                if net_sale_proceeds > 0:
                    held_shares = 0.0
                    cash += net_sale_proceeds
                    sales += 1
                    sale_today = True
                    action = "SELL"
                    kind = "ATR" if atr_trigger else "MACD"
                    held_peak = 0.0
        if (not sale_today and previous_indicator.sma60 is not None
                and previous.close <= previous_indicator.sma60
                and cash > MIN_BUY_CASH):
            allocation = cash
            held_shares += (allocation - DISCRETIONARY_FEE) / (
                current.open * (1.0 + FRICTION))
            cash -= allocation
            buys += 1
            action = "BUY"
            if held_peak == 0.0:
                held_peak = current.open

        if current.session in pending:
            amount_paid = pending.pop(current.session)
            if held_shares > 0:
                held_shares += amount_paid / (current.open * (1.0 + FRICTION))
            else:
                cash += amount_paid

        if held_shares > FLAT_SHARE_EPSILON:
            held_peak = max(held_peak, current.close)
        else:
            held_peak = 0.0
        dividend_receivables = sum(pending.values())
        wealth = held_shares * current.close + cash + dividend_receivables
        time_weighted_index *= (wealth - contribution) / previous_net
        highest_time_weighted = max(highest_time_weighted, time_weighted_index)
        max_drawdown = min(max_drawdown, time_weighted_index / highest_time_weighted - 1.0)
        previous_net = wealth
        if (previous_indicator.sma60 is None or previous_indicator.atr14 is None
                or not isfinite(wealth)):
            raise MonitorDataError("Historical accounting/indicator calculation invalid")
        history.append(HistoricalDay(
            date=current.session.isoformat(), action=action, kind=kind,
            shares=held_shares, cash=cash, peak=held_peak, wealth=wealth,
            open=current.open, close=current.close,
            sma=previous_indicator.sma60, atr=previous_indicator.atr14,
            bear=previous_indicator.bearish_cross,
            atr_trigger=bool(atr_trigger), macd_trigger=bool(macd_trigger),
            dividend_receivables=dividend_receivables, deposit=contribution,
        ))
    if not history:
        raise MonitorDataError("No historical research output produced")
    return HistoricalReplay(
        status="INDEPENDENT_HYPOTHETICAL_CHAMPION_REPLAY_UNVERIFIED",
        first_open_purchase_session=start.isoformat(),
        last_session=end.isoformat(),
        purchases_including_first=buys, sales=sales,
        ending_wealth=previous_net,
        maximum_flow_adjusted_drawdown=max_drawdown,
        rows_after_first_open=tuple(history),
    )
