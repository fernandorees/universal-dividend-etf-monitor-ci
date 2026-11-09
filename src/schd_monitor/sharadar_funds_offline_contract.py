"""Credential-free, offline-only Sharadar FUNDS/SFP schema research contract.

Reviews caller-supplied FICTIONAL rows, never vendor response bytes or an
authenticated service. No URLs, API key, HTTP, brokerage or trading operations.
A passing result cannot prove publication finality or independent feed lineage.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite

from .models import Bar, MonitorDataError, validate_bars

SHARADAR_FUNDS_CONTRACT_SCHEMA = 1
MAX_SHARADAR_FUNDS_ROWS = 10000
_SHARADAR_COLUMNS = (
    "ticker", "date", "open", "high", "low", "close", "volume", "lastupdated",
)
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,14}\Z")


def _digest(payload: object) -> str:
    try:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise MonitorDataError("Invalid fictional Sharadar JSON-compatible input") from exc
    return hashlib.sha256(canonical).hexdigest()


def _date(value: object, field: str) -> date:
    if type(value) is not str:
        raise MonitorDataError(f"Sharadar {field} must be YYYY-MM-DD text")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise MonitorDataError(f"Invalid Sharadar {field}") from exc
    if result.isoformat() != value:
        raise MonitorDataError(f"Noncanonical Sharadar {field}")
    return result


def _number(value: object, field: str, *, allow_zero: bool = False) -> float:
    if type(value) not in (int, float):
        raise MonitorDataError(f"Sharadar {field} must be numeric, not text/bool")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise MonitorDataError(f"Sharadar {field} is not representable") from exc
    if not isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise MonitorDataError(f"Sharadar {field} must be finite and nonnegative/positive")
    return number


@dataclass(frozen=True)
class SharadarFundsRequest:
    """Inert, non-secret selection only; constructing a vendor URL is forbidden."""
    symbol: str
    start: date
    end: date
    schema_version: int = SHARADAR_FUNDS_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SHARADAR_FUNDS_CONTRACT_SCHEMA:
            raise MonitorDataError("Unsupported Sharadar FUNDS request schema")
        if type(self.symbol) is not str or not _SYMBOL.fullmatch(self.symbol):
            raise MonitorDataError("Sharadar FUNDS symbol must be an uppercase ticker")
        if (type(self.start) is not date or type(self.end) is not date
                or self.start > self.end):
            raise MonitorDataError("Sharadar FUNDS requires ordered explicit dates")

    def inert_fields(self) -> dict[str, str]:
        """No URL, transport, authentication option or implicit provider default."""
        return {
            "ticker": self.symbol,
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "fields": ",".join(_SHARADAR_COLUMNS),
            "sort": "date.asc",
            "format": "json",
            "limit": str(MAX_SHARADAR_FUNDS_ROWS),
            "skip": "0",
        }

    def request_sha256(self) -> str:
        return _digest({"table": "funds", "inert_fields": self.inert_fields()})


@dataclass(frozen=True)
class SharadarFundsOfflineRows:
    bars: tuple[Bar, ...]
    request_sha256: str
    fictional_rows_sha256: str
    status: str = "SHARADAR_FUNDS_ROWS_SHAPE_UNATTESTED"
    schema_version: int = SHARADAR_FUNDS_CONTRACT_SCHEMA
    provider_authenticated: bool = False
    market_data_entitlement_verified: bool = False
    split_adjustment_verified: bool = False
    session_coverage_verified: bool = False
    source_independence_verified: bool = False
    vendor_bar_finality_verified: bool = False
    production_signals_verified: bool = False
    trading_status: str = "UNVERIFIED_DO_NOT_TRADE"

    def as_json(self) -> dict:
        result = asdict(self)
        result.pop("bars")
        result["bars_count"] = len(self.bars)
        return result


def review_sharadar_funds_offline_rows(
    *, request: SharadarFundsRequest, rows: object,
) -> SharadarFundsOfflineRows:
    """Review invented Python dictionaries, NOT the live Sharadar wire format.

    Strictly select documented split-adjusted FUNDS columns, not closeadj or
    closeunadj. A full 10,000-row page is ambiguous without pagination and
    must fail closed. This is not proof of missing-session completeness.
    """
    if type(request) is not SharadarFundsRequest:
        raise MonitorDataError("Typed Sharadar FUNDS request required")
    if type(rows) is not list or not rows:
        raise MonitorDataError("Sharadar FUNDS rows must be a nonempty list")
    if len(rows) >= MAX_SHARADAR_FUNDS_ROWS:
        raise MonitorDataError("Sharadar FUNDS page may be truncated: pagination not verified")
    bars: list[Bar] = []
    for row in rows:
        if type(row) is not dict or set(row) != set(_SHARADAR_COLUMNS):
            raise MonitorDataError("Sharadar FUNDS row fields missing or unexpected")
        if row["ticker"] != request.symbol:
            raise MonitorDataError("Sharadar FUNDS ticker differs from request")
        session = _date(row["date"], "date")
        _date(row["lastupdated"], "lastupdated")  # Claim only; not finality.
        if not request.start <= session <= request.end:
            raise MonitorDataError("Sharadar FUNDS row outside requested dates")
        ohlc = [_number(row[field], field) for field in ("open", "high", "low", "close")]
        _number(row["volume"], "volume", allow_zero=True)
        bars.append(Bar(session, *ohlc))
    validate_bars(bars, minimum=1)
    return SharadarFundsOfflineRows(
        tuple(bars), request.request_sha256(), _digest(rows),
    )
