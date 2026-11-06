"""Fictional, transport-free Massive stock REST daily aggregate contract.

No API keys, HTTP, brokerage access, orders, live data, or signal promotion.
A passing record checks only an untrusted response's internal consistency.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from math import isfinite
from zoneinfo import ZoneInfo

from .models import Bar, MonitorDataError, validate_bars

MASSIVE_CONTRACT_SCHEMA = 1
MAX_BASE_AGGREGATES = 50000
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,14}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NY = ZoneInfo("America/New_York")


def _digest(payload: object) -> str:
    try:
        wire = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise MonitorDataError("Massive response contains invalid JSON-compatible data") from exc
    return hashlib.sha256(wire).hexdigest()


@dataclass(frozen=True)
class MassiveDailyRequest:
    """An explicit split-adjusted daily REST request; no vendor call."""
    symbol: str
    start: date
    end: date
    adjusted: bool = True
    schema_version: int = MASSIVE_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != MASSIVE_CONTRACT_SCHEMA:
            raise MonitorDataError("Unsupported Massive request schema")
        if type(self.symbol) is not str or not _SYMBOL.fullmatch(self.symbol):
            raise MonitorDataError("Massive symbol must be a valid uppercase ticker")
        if type(self.start) is not date or type(self.end) is not date or self.start > self.end:
            raise MonitorDataError("Massive request requires ordered explicit date bounds")
        if type(self.adjusted) is not bool or not self.adjusted:
            raise MonitorDataError("Massive REST daily bars must request split-adjusted prices")

    def path_segments(self) -> tuple[str, ...]:
        """Only inert path components, not a key-bearing or executable URL."""
        return ("v2", "aggs", "ticker", self.symbol, "range",
                "1", "day", self.start.isoformat(), self.end.isoformat())

    def params(self) -> dict[str, str]:
        return {"adjusted": "true", "sort": "asc",
                "limit": str(MAX_BASE_AGGREGATES)}

    def request_sha256(self) -> str:
        return _digest({"path": self.path_segments(), "params": self.params()})


@dataclass(frozen=True)
class MassiveOfflineResponse:
    bars: tuple[Bar, ...]
    request_sha256: str
    response_sha256: str
    claimed_server_request_id: str
    claimed_query_count: int
    status: str = "MASSIVE_REST_SHAPE_CONSISTENT_UNATTESTED"
    schema_version: int = MASSIVE_CONTRACT_SCHEMA
    provider_authenticated: bool = False
    split_adjustment_verified: bool = False
    market_data_entitlement_verified: bool = False
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


def check_massive_offline_response(
    *, request: MassiveDailyRequest, http_status: int, payload: object,
) -> MassiveOfflineResponse:
    """Reject denied, malformed, wrong-adjustment and possibly truncated data.

    HTTP status and metadata are supplied by a caller and NOT independently
    authenticated. Reject a count hitting the base-aggregate limit; Massive
    documents that this limit can affect aggregate completeness.
    """
    if type(request) is not MassiveDailyRequest:
        raise MonitorDataError("Typed Massive REST request required")
    if type(http_status) is not int or http_status != 200:
        raise MonitorDataError("Massive HTTP status not successful")
    required = {"ticker", "adjusted", "queryCount", "request_id",
                "resultsCount", "status", "results"}
    if type(payload) is not dict or not required.issubset(payload) or (
        set(payload) - required - {"next_url", "count"}
    ):
        raise MonitorDataError("Massive response envelope missing or unexpected fields")
    if payload["status"] != "OK" or payload["ticker"] != request.symbol:
        raise MonitorDataError("Massive response status or symbol inconsistent")
    if type(payload["adjusted"]) is not bool or payload["adjusted"] is not True:
        raise MonitorDataError("Massive response not declared split-adjusted")
    request_id = payload["request_id"]
    if type(request_id) is not str or not _REQUEST_ID.fullmatch(request_id):
        raise MonitorDataError("Missing/invalid Massive server request ID")
    if "next_url" in payload and payload["next_url"] is not None:
        raise MonitorDataError("Massive response pagination not supported offline")
    query_count, results_count = payload["queryCount"], payload["resultsCount"]
    if (type(query_count) is not int or type(results_count) is not int
            or query_count < 0 or query_count >= MAX_BASE_AGGREGATES
            or results_count <= 0 or query_count < results_count):
        raise MonitorDataError("Massive aggregate counts invalid or potentially truncated")
    # Massive's documented discrepancy example includes an OPTIONAL `count`
    # alongside `resultsCount`. Both are untrusted: require agreement when
    # present, but do not mistake an omitted optional field for truncation.
    if "count" in payload and (
        type(payload["count"]) is not int or payload["count"] != results_count
    ):
        raise MonitorDataError("Massive optional count contradicts resultsCount")
    rows = payload["results"]
    if type(rows) is not list or len(rows) != results_count:
        raise MonitorDataError("Massive resultsCount mismatch or missing bars")
    bars: list[Bar] = []
    allowed = {"t", "o", "h", "l", "c", "v", "vw", "n", "otc"}
    for row in rows:
        if type(row) is not dict or not {"t", "o", "h", "l", "c"}.issubset(row) or (
            set(row) - allowed
        ):
            raise MonitorDataError("Massive daily aggregate fields missing/unknown")
        timestamp_ms = row["t"]
        if type(timestamp_ms) is not int or timestamp_ms < 0:
            raise MonitorDataError("Massive timestamp must be Unix milliseconds")
        try:
            stamped = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        except (OverflowError, ValueError, OSError) as exc:
            raise MonitorDataError("Massive timestamp outside supported datetime range") from exc
        local = stamped.astimezone(_NY)
        if local.time() != time(0, 0):
            raise MonitorDataError("Massive daily aggregate is not midnight in New York")
        session = local.date()
        if not request.start <= session <= request.end:
            raise MonitorDataError("Massive aggregate outside requested dates")
        prices = []
        for field in ("o", "h", "l", "c"):
            raw = row[field]
            if type(raw) not in (int, float):
                raise MonitorDataError("Massive OHLC must be numeric, not text/bool")
            try:
                px = float(raw)
            except (OverflowError, ValueError) as exc:
                raise MonitorDataError("Massive OHLC not representable") from exc
            if not isfinite(px):
                raise MonitorDataError("Massive OHLC not finite")
            prices.append(px)
        for field in ("v", "n", "vw"):
            if field in row:
                raw = row[field]
                if type(raw) not in (int, float):
                    raise MonitorDataError("Massive aggregate auxiliary value invalid")
                try:
                    numeric = float(raw)
                except (OverflowError, ValueError) as exc:
                    raise MonitorDataError("Massive auxiliary value not representable") from exc
                if not isfinite(numeric) or numeric < 0:
                    raise MonitorDataError("Massive aggregate auxiliary value invalid")
                if field == "n" and type(raw) is not int:
                    raise MonitorDataError("Massive trade count must be an integer")
        if "otc" in row and type(row["otc"]) is not bool:
            raise MonitorDataError("Massive OTC marker must be boolean")
        bars.append(Bar(session, *prices))
    validate_bars(bars, minimum=1)
    return MassiveOfflineResponse(
        bars=tuple(bars),
        request_sha256=request.request_sha256(),
        response_sha256=_digest(payload),
        claimed_server_request_id=request_id,
        claimed_query_count=query_count,
    )
