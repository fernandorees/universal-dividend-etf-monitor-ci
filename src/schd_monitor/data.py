"""Transport adapters. Never connects to brokers or places orders."""
from __future__ import annotations
import csv
import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .models import Bar, MonitorDataError, Position, validate_bars, validate_expected_sessions


def bars_from_csv(path: str | Path, *, expected_sessions: list[date] | None = None) -> list[Bar]:
    bars = []
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        need = {'date','open','high','low','close'}
        if not need.issubset(reader.fieldnames or ()):
            raise MonitorDataError('CSV must have columns: date,open,high,low,close')
        for row in reader:
            bars.append(Bar(date.fromisoformat(row['date']),
                            *(float(row[k]) for k in ('open','high','low','close'))))
    validate_bars(bars)
    if expected_sessions is not None:
        validate_expected_sessions(bars, expected_sessions)
    return bars


def expected_sessions_from_csv(path: str | Path) -> list[date]:
    """Read an EXTERNALLY PREPARED date-only session manifest (not attested)."""
    sessions: list[date] = []
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ['date']:
            raise MonitorDataError("Expected-session CSV must have exactly one column: date")
        for number, row in enumerate(reader, start=2):
            if set(row) != {'date'} or not isinstance(row['date'], str):
                raise MonitorDataError(f"Malformed expected-session CSV line {number}")
            try:
                session = date.fromisoformat(row['date'])
            except (TypeError, ValueError) as exc:
                raise MonitorDataError(f"Invalid expected-session date at CSV line {number}") from exc
            if session.isoformat() != row['date']:
                raise MonitorDataError(f"Expected-session date must be YYYY-MM-DD at CSV line {number}")
            sessions.append(session)
    return sessions


def position_from_json(path: str | Path) -> Position:
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    return Position(raw['symbol'],float(raw['shares']),float(raw['available_cash']),
                    None if raw['peak_close'] is None else float(raw['peak_close']),
                    date.fromisoformat(raw['reconciled_through']))


def fetch_alpaca_raw_daily(symbol: str, start: str, end: str,
                           *, feed: str = 'sip', max_pages: int = 50) -> list[Bar]:
    """Authenticated GET, read-only. Raw (unadjusted) bars: NOT suitable for production
    indicator calculation across splits until corporate-action normalization is validated.
    ``end`` is exclusive in the provider's daily-bars API. Avoid partial current session.
    """
    key, secret = os.getenv('ALPACA_API_KEY_ID'), os.getenv('ALPACA_API_SECRET_KEY')
    if not key or not secret:
        raise MonitorDataError('ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY missing (do not commit credentials)')
    if feed != 'sip':
        raise MonitorDataError('Use SIP consolidated feed; IEX subset not accepted')
    if symbol.upper() not in {'SCHD','VYM','VIG','DGRO','HDV','SPYD','DGRW','SPY'}:
        raise MonitorDataError('Unsupported ticker')
    bars = []
    token = None
    for _ in range(max_pages):
        params = {'timeframe':'1Day','start':start,'end':end,'adjustment':'raw',
                  'feed':feed,'limit':'10000','sort':'asc'}
        if token: params['page_token'] = token
        url = ('https://data.alpaca.markets/v2/stocks/' + symbol.upper() + '/bars?' + urlencode(params))
        req = Request(url, headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret,
                                     'User-Agent':'universal-etf-monitor/0.1'})
        try:
            with urlopen(req, timeout=30) as reply:
                payload = json.load(reply)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise MonitorDataError(f'Alpaca data request failed: {type(exc).__name__}') from exc
        for row in payload.get('bars',[]):
            bars.append(Bar(date.fromisoformat(row['t'][:10]),
                            float(row['o']),float(row['h']),float(row['l']),float(row['c'])))
        token = payload.get('next_page_token')
        if not token:break
    else:raise MonitorDataError('Exceeded Alpaca pagination limit')
    validate_bars(bars)
    return bars
