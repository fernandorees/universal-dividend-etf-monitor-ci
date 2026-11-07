"""Offline-only Alpaca historical daily-bar request/response contract.

No HTTP, credentials, brokerage, orders, portfolio state, or production signals.
Passing validates a *self-declared payload shape*, NOT vendor authenticity,
entitlement, split correctness, calendar coverage or bar finality.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from math import isfinite
from zoneinfo import ZoneInfo

from .models import Bar, MonitorDataError, validate_bars

ALPACA_CONTRACT_SCHEMA = 1
# Pinned single-page upper bound sent with every reviewed offline request.
MAX_ALPACA_SINGLE_PAGE_BARS = 10000
# Independent in-memory UTF-8/JSON intake cap before existing shape validation.
MAX_ALPACA_REST_BYTES = 4 * 1024 * 1024
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,14}\Z")
_NEW_YORK = ZoneInfo("America/New_York")
# The reviewed historical 1Day bar contract includes exactly these fields.
# Unknown fields require an explicit future provider-contract review.
_DAILY_BAR_FIELDS = frozenset({"t", "o", "h", "l", "c", "v", "n", "vw"})


@dataclass(frozen=True)
class AlpacaDailyRequest:
    """Explicit, credential-free single-symbol SIP/split-only specification.

    Both date bounds are treated as inclusive per the published API reference;
    real endpoint behavior has NOT been independently confirmed.  The `asof`
    identity date is pinned so a changing provider default cannot be hidden.
    """
    symbol: str
    start: date
    end: date
    symbol_asof: date
    feed: str
    adjustment: str
    schema_version: int = ALPACA_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != ALPACA_CONTRACT_SCHEMA:
            raise MonitorDataError("Unsupported Alpaca request contract schema")
        if type(self.symbol) is not str or not _SYMBOL.fullmatch(self.symbol):
            raise MonitorDataError("Alpaca symbol must be a valid uppercase ticker")
        if any(type(x) is not date for x in (self.start, self.end, self.symbol_asof)):
            raise MonitorDataError("Alpaca request dates must be explicit date objects")
        if self.start > self.end:
            raise MonitorDataError("Alpaca inclusive request date range reversed")
        if self.feed != "sip":
            raise MonitorDataError("Alpaca request must explicitly select SIP")
        if self.adjustment != "split":
            raise MonitorDataError("Alpaca request must explicitly select split-only prices")

    def params(self) -> dict[str, str]:
        """No secret-bearing URL or API key is constructed by this method."""
        return {
            "timeframe": "1Day", "start": self.start.isoformat(),
            "end": self.end.isoformat(), "asof": self.symbol_asof.isoformat(),
            "feed": self.feed, "adjustment": self.adjustment,
            "sort": "asc", "limit": str(MAX_ALPACA_SINGLE_PAGE_BARS),
        }

    def request_sha256(self) -> str:
        return _digest({"symbol": self.symbol, "params": self.params()})


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AlpacaOfflinePage:
    bars: tuple[Bar, ...]
    request_sha256: str
    response_sha256: str
    status: str = "ALPACA_PAGE_SHAPE_CONSISTENT_UNATTESTED"
    schema_version: int = ALPACA_CONTRACT_SCHEMA
    provider_authenticated: bool = False
    feed_entitlement_verified: bool = False
    split_adjustment_verified: bool = False
    session_coverage_verified: bool = False
    vendor_bar_finality_verified: bool = False
    production_signals_verified: bool = False
    trading_status: str = "UNVERIFIED_DO_NOT_TRADE"

    def as_json(self) -> dict:
        data = asdict(self)
        data.pop("bars")  # Never turn research bars into presumed account records.
        data["bars_count"] = len(self.bars)
        return data


def check_alpaca_offline_page(*, request: AlpacaDailyRequest, http_status: int,
                              payload: object) -> AlpacaOfflinePage:
    """Fail closed on auth/errors, malformed bars, wrong symbols or pagination.

    This deliberately refuses a multi-page response instead of silently using
    the first page. A future reviewed pagination collector is required for it.
    """
    if type(request) is not AlpacaDailyRequest:
        raise MonitorDataError("Typed Alpaca request contract required")
    if type(http_status) is not int or http_status != 200:
        raise MonitorDataError("Alpaca HTTP status not successful; no fallback feed")
    if type(payload) is not dict or set(payload) != {"symbol", "bars", "next_page_token"}:
        raise MonitorDataError("Alpaca response envelope incomplete or unexpected")
    if payload["symbol"] != request.symbol:
        raise MonitorDataError("Alpaca response symbol differs from request")
    if payload["next_page_token"] is not None:
        raise MonitorDataError("Alpaca historical response not complete: pagination required")
    rows = payload["bars"]
    if type(rows) is not list or not rows:
        raise MonitorDataError("Alpaca response has no usable daily bars")
    if len(rows) > MAX_ALPACA_SINGLE_PAGE_BARS:
        raise MonitorDataError(
            "Alpaca single-page response exceeds requested 10000-bar limit"
        )
    result: list[Bar] = []
    for row in rows:
        if type(row) is not dict or set(row) != _DAILY_BAR_FIELDS:
            raise MonitorDataError(
                "Alpaca daily bar missing required fields or contains unexpected fields"
            )
        timestamp = row["t"]
        if type(timestamp) is not str or not timestamp.endswith("Z"):
            raise MonitorDataError("Alpaca daily timestamp must be UTC RFC3339")
        try:
            stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MonitorDataError("Malformed Alpaca daily timestamp") from exc
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise MonitorDataError("Alpaca daily timestamp must be UTC")
        local = stamp.astimezone(_NEW_YORK)
        if local.time().hour or local.time().minute or local.time().second or local.time().microsecond:
            raise MonitorDataError("Alpaca daily bar timestamp not New York session midnight")
        session = local.date()
        if not request.start <= session <= request.end:
            raise MonitorDataError("Alpaca bar outside inclusive requested dates")
        prices = []
        for key in ("o", "h", "l", "c"):
            value = row[key]
            if type(value) not in (int, float):
                raise MonitorDataError("Alpaca price must be a JSON number, not bool/text")
            try:
                price = float(value)
            except (OverflowError, ValueError) as exc:
                raise MonitorDataError("Alpaca price cannot be represented") from exc
            if not isfinite(price):
                raise MonitorDataError("Alpaca price must be finite")
            prices.append(price)
        # Published single-symbol historical daily bars include volume and
        # trade count. Missing/invalid activity metadata must not look valid.
        # Split-adjusted volume may be fractional; accept finite numerics
        # without assuming a trade happened or the source is authenticated.
        volume = row["v"]
        if type(volume) not in (int, float):
            raise MonitorDataError("Alpaca daily volume must be a JSON number")
        try:
            volume_value = float(volume)
        except (OverflowError, ValueError) as exc:
            raise MonitorDataError("Alpaca daily volume cannot be represented") from exc
        if not isfinite(volume_value) or volume_value < 0:
            raise MonitorDataError("Alpaca daily volume must be finite/nonnegative")
        if type(row["n"]) is not int or row["n"] < 0:
            raise MonitorDataError("Alpaca daily trade count must be a nonnegative integer")
        # Alpaca's published historical-bars schema requires vw (unlike the
        # separate Massive aggregates schema, where vw is optional).
        weighted = row["vw"]
        if type(weighted) not in (int, float):
            raise MonitorDataError("Alpaca VWAP must be a JSON number")
        try:
            weighted_value = float(weighted)
        except (OverflowError, ValueError) as exc:
            raise MonitorDataError("Alpaca VWAP cannot be represented") from exc
        if not isfinite(weighted_value) or weighted_value <= 0:
            raise MonitorDataError("Alpaca VWAP must be finite/positive")
        result.append(Bar(session, *prices))
    validate_bars(result, minimum=1)
    try:
        response_hash = _digest(payload)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MonitorDataError("Unhashable or invalid Alpaca response JSON") from exc
    return AlpacaOfflinePage(tuple(result), request.request_sha256(), response_hash)


def check_alpaca_bounded_response_bytes(
    *, request: AlpacaDailyRequest, http_status: int, raw_body: bytes,
) -> AlpacaOfflinePage:
    """Review caller-supplied untrusted HTTP body bytes WITHOUT making any HTTP call.

    This pure, credential-free boundary intentionally has no URL, headers,
    socket, environment-variable, file or brokerage handling. A future
    independently approved read-only data transport may call it, but this
    function never attests the provider or upgrades production status.
    """
    if type(request) is not AlpacaDailyRequest:
        raise MonitorDataError("Typed Alpaca request required for byte intake")
    if type(http_status) is not int or http_status != 200:
        raise MonitorDataError("Alpaca HTTP status not successful; no fallback feed")
    if type(raw_body) is not bytes or not raw_body:
        raise MonitorDataError("Alpaca response body must be nonempty bytes")
    if len(raw_body) > MAX_ALPACA_REST_BYTES:
        raise MonitorDataError("Alpaca response body exceeds 4 MiB size limit")

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise MonitorDataError("Duplicate Alpaca response JSON key")
            obj[key] = value
        return obj

    def reject_non_json_constant(token):
        raise MonitorDataError("Alpaca response contains non-JSON numeric constant")

    try:
        payload = json.loads(
            raw_body.decode("utf-8"), object_pairs_hook=unique_object,
            parse_constant=reject_non_json_constant,
        )
    except MonitorDataError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError, OverflowError):
        raise MonitorDataError("Invalid Alpaca response UTF-8/JSON") from None

    return check_alpaca_offline_page(
        request=request, http_status=http_status, payload=payload,
    )
