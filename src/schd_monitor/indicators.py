"""Faithful indicator seeds/recurrences from archived original position-sizing simulator.

Do not change numerical mechanics without a versioned decision and complete regression replay.
SMA60: closes with 60-session arithmetic rolling mean.
ATR14: true ranges start at history row index 1; seed at row 14; Wilder thereafter.
MACD(18,35,9): SMA seeds at rows 17,34; signal seeded by first available MACD value.
Fresh bearish cross only after index>=43, exactly as original simulator.
"""
from dataclasses import dataclass
from .models import Bar, MonitorDataError, validate_bars


@dataclass(frozen=True)
class Indicator:
    date: str
    close: float
    sma60: float | None
    atr14: float | None
    macd: float | None
    macd_signal: float | None
    bearish_cross: bool


def calculate(bars: list[Bar]) -> list[Indicator]:
    validate_bars(bars)
    out: list[Indicator] = []
    summ = 0.0
    atr = None
    tr_seed: list[float] = []
    e18 = e35 = sign = old_macd = old_sign = None
    for i, row in enumerate(bars):
        c = row.close
        summ += c
        if i >= 60:
            summ -= bars[i-60].close
        sma60 = summ / 60 if i >= 59 else None
        if i:
            prior = bars[i-1].close
            tr = max(row.high-row.low, abs(row.high-prior), abs(row.low-prior))
            if i <= 14:
                tr_seed.append(tr)
                if i == 14:
                    atr = sum(tr_seed) / 14
            else:
                assert atr is not None
                atr = (atr*13+tr)/14
        if i == 17:
            e18 = sum(q.close for q in bars[:18])/18
        elif i > 17:
            e18 = (e18*17+c*2)/19
        if i == 34:
            e35 = sum(q.close for q in bars[:35])/35
        elif i > 34:
            e35 = (e35*34+c*2)/36
        mac = None
        bearish = False
        if e18 is not None and e35 is not None:
            mac = e18-e35
            sign = mac if sign is None else .2*mac+.8*sign
            if i >= 43:
                bearish = bool(old_macd >= old_sign and mac < sign)
            old_macd, old_sign = mac, sign
        out.append(Indicator(row.session.isoformat(), c, sma60, atr, mac, sign, bearish))
    return out
