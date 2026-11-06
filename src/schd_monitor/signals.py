"""Pure, stateless evaluation. Never places orders or updates real brokerage positions."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from .indicators import Indicator
from .models import Bar, MonitorDataError, Position, validate_bars


class Strategy(str, Enum):
    CHAMPION = "CHAMPION"
    RAPID_100 = "RAPID_100"
    RS20 = "RS20"


@dataclass(frozen=True)
class Decision:
    strategy: str
    symbol: str
    signal_session: str
    execution: str
    candidate_action: str
    verification: str
    signal_reasons: tuple[str, ...]
    latest_close: float
    sma60: float
    atr14: float
    held_peak: float | None
    stop_threshold: float | None
    macd: float
    macd_signal: float
    bearish_cross: bool
    macd_decline: bool
    rapid_five_session: bool | None
    atr_gate: bool | None
    rs20_underperformance: bool | None

    def as_json(self) -> dict:
        result = asdict(self)
        result["signal_reasons"] = list(self.signal_reasons)
        return result


def evaluate(bars: list[Bar], indicators: list[Indicator], position: Position,
             *, strategy: Strategy = Strategy.CHAMPION,
             rs20_underperformance: bool | None = None,
             data_verified: bool = False) -> Decision:
    """Use held position, prior peak and today's completed close to produce next-opening candidate.

    ``position.reconciled_through`` is the latest COMPLETED signal session,
    including any manually confirmed orders executed at that session's opening.
    The program NEVER guesses holdings, entry fills, peak price, or corporate actions.
    ``data_verified`` is reserved for a future attested verification workflow.
    During P01, all candidate signals are provisional: a caller cannot promote
    one to VERIFIED by supplying a Boolean or another truthy value.
    """
    if data_verified is not False:
        raise MonitorDataError(
            "VERIFIED status is disabled until a reviewed independent verification gate exists"
        )
    validate_bars(bars)
    if not isinstance(strategy, Strategy):
        strategy = Strategy(strategy)
    if len(bars) != len(indicators):
        raise MonitorDataError("Bars/indicators are not aligned")
    today = bars[-1]
    last = indicators[-1]
    if date.fromisoformat(last.date) != today.session:
        raise MonitorDataError("Last indicator/session date mismatch")
    if position.reconciled_through != today.session:
        raise MonitorDataError("Position state must be manually reconciled THROUGH latest session (fills or no fills)")
    if position.symbol.upper() not in {"SCHD", "VYM", "VIG", "DGRO", "HDV", "SPYD", "DGRW"}:
        raise MonitorDataError("Unsupported ticker")
    if last.sma60 is None or last.atr14 is None or last.macd is None or last.macd_signal is None:
        raise MonitorDataError("Indicators not warmed up")
    if strategy == Strategy.RS20 and rs20_underperformance is None and position.shares > 0:
        raise MonitorDataError("RS20 comparison missing verified dividend-inclusive SPY return")
    reasons = []
    held = position.shares > 1e-8
    peak = max(position.peak_close, today.close) if held else None
    stop = peak-2.7*last.atr14 if held else None
    stop_fired = bool(held and today.close < stop)
    decline = bool(held and today.close <= peak*.995)
    bearish = bool(held and last.bearish_cross and decline)
    atr_gate = bool(held and (peak-today.close) / last.atr14 >= 1.75)
    rapid = bool(held and (bars[-6].close - today.close)/last.atr14 >= 1)
    gate = (strategy == Strategy.CHAMPION or
            strategy == Strategy.RAPID_100 and (atr_gate or rapid) or
            strategy == Strategy.RS20 and bool(rs20_underperformance))
    if held and stop_fired:
        action = 'SELL'
        reasons.append('ATR_STOP_INDEPENDENT')
    elif bearish and gate:
        action = 'SELL'
        reasons.append('FRESH_BEARISH_MACD_AND_PEAK_DECLINE')
        if strategy == Strategy.RAPID_100:
            reasons.append('ATR_GATE_175' if atr_gate else 'RAPID_FIVE_SESSION_100')
        if strategy == Strategy.RS20:
            reasons.append('RS20_UNDERPERFORMS_SPY')
    elif not held and today.close <= last.sma60 and position.available_cash > 1.000001:
        action = 'BUY'
        reasons.append('CLOSE_AT_OR_BELOW_SMA60')
    else:
        action = 'HOLD' if held else 'WAIT_IN_CASH'
        reasons.append('NO_ACTION_CONDITION')
    return Decision(strategy.value, position.symbol.upper(), today.session.isoformat(),
                    'NEXT_REGULAR_US_TRADING_OPEN',action,
                    'UNVERIFIED_DO_NOT_TRADE',tuple(reasons),
                    today.close,last.sma60,last.atr14,peak,stop,last.macd,last.macd_signal,
                    bool(last.bearish_cross),decline,rapid if held else None,
                    atr_gate if held else None,rs20_underperformance)
