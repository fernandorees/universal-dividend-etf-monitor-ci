"""P01 offline, *unattested* OHLC comparison with declared provider metadata.

This is a conservative discrepancy detector, NOT evidence of independent feeds,
finalized market data, correct corporate actions or production readiness.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .models import Bar, MonitorDataError, validate_bars

# Strict P01 research-only threshold: 0.01% of the larger comparison price.
# Review against genuine provider conventions BEFORE adopting any production policy.
MAX_RELATIVE_OHLC_DIFFERENCE = 0.0001
FEED_DECLARATION_SCHEMA = 1
# Identifier labels are untrusted metadata: forbid whitespace, control chars,
# Unicode lookalikes, paths and unbounded strings in audit output.
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


@dataclass(frozen=True)
class FeedDeclaration:
    provider: str
    symbol: str
    adjustment: str
    last_complete_session: date
    schema_version: int = FEED_DECLARATION_SCHEMA

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != FEED_DECLARATION_SCHEMA:
            raise MonitorDataError("Unsupported feed declaration schema version")
        if type(self.provider) is not str or not _PROVIDER_ID.fullmatch(self.provider):
            raise MonitorDataError("Feed provider must be a bounded ASCII identifier")
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip().upper():
            raise MonitorDataError("Feed symbol must be a nonempty uppercase ticker")
        if self.adjustment != "SPLIT_ONLY":
            raise MonitorDataError("Feed adjustment must declare SPLIT_ONLY (not raw or dividend adjusted)")
        if type(self.last_complete_session) is not date:
            raise MonitorDataError("Feed last_complete_session must be a date")


def _unique_feed_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous JSON, including duplicate keys with identical values."""
    fields: dict[str, object] = {}
    for name, value in pairs:
        if name in fields:
            raise MonitorDataError("Duplicate feed declaration JSON field")
        fields[name] = value
    return fields


def feed_declaration_from_json(path: str | Path) -> FeedDeclaration:
    """Read self-declared metadata; this does not authenticate its claims."""
    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_feed_json_keys,
        )
    except json.JSONDecodeError as exc:
        raise MonitorDataError("Invalid feed declaration JSON") from exc
    required = {"schema_version", "provider", "symbol", "adjustment", "last_complete_session"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise MonitorDataError("Feed declaration requires exactly the versioned metadata fields")
    value = payload["last_complete_session"]
    if not isinstance(value, str):
        raise MonitorDataError("Feed last_complete_session must be YYYY-MM-DD")
    try:
        session = date.fromisoformat(value)
    except ValueError as exc:
        raise MonitorDataError("Invalid feed last_complete_session date") from exc
    if session.isoformat() != value:
        raise MonitorDataError("Feed last_complete_session must be YYYY-MM-DD")
    return FeedDeclaration(
        provider=payload["provider"], symbol=payload["symbol"],
        adjustment=payload["adjustment"], last_complete_session=session,
        schema_version=payload["schema_version"],
    )


@dataclass(frozen=True)
class FeedComparison:
    status: str
    schema_version: int
    symbol: str
    primary_provider_claim: str
    secondary_provider_claim: str
    last_session: str
    sessions_compared: int
    max_relative_ohlc_difference: float
    tolerance: float
    primary_bars_sha256: str
    secondary_bars_sha256: str

    def as_json(self) -> dict:
        return asdict(self)


def _bar_digest(bars: list[Bar]) -> str:
    canonical = [[row.session.isoformat(), row.open, row.high, row.low, row.close] for row in bars]
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def compare_offline_feeds(
    primary: list[Bar], secondary: list[Bar],
    primary_declaration: FeedDeclaration, secondary_declaration: FeedDeclaration,
    *, expected_symbol: str,
) -> FeedComparison:
    """Reject unaligned or discrepant histories; passing does NOT verify sources."""
    validate_bars(primary)
    validate_bars(secondary)
    if not isinstance(primary_declaration, FeedDeclaration) or not isinstance(secondary_declaration, FeedDeclaration):
        raise MonitorDataError("Both feed declarations are required")
    if not isinstance(expected_symbol, str) or not expected_symbol or expected_symbol != expected_symbol.strip().upper():
        raise MonitorDataError("Expected ticker must be uppercase")
    if primary_declaration.symbol != expected_symbol or secondary_declaration.symbol != expected_symbol:
        raise MonitorDataError("Feed symbol does not match the manually declared position symbol")
    if primary_declaration.provider.casefold() == secondary_declaration.provider.casefold():
        raise MonitorDataError("Distinct claimed providers are required; independence remains unverified")
    if primary[-1].session != primary_declaration.last_complete_session:
        raise MonitorDataError("Primary claimed completed-through date does not match bars")
    if secondary[-1].session != secondary_declaration.last_complete_session:
        raise MonitorDataError("Secondary claimed completed-through date does not match bars")

    if len(primary) != len(secondary) or any(a.session != b.session for a, b in zip(primary, secondary)):
        raise MonitorDataError("Independent feed session history mismatch (missing or unexpected session)")

    maximum = 0.0
    for a, b in zip(primary, secondary):
        for field in ("open", "high", "low", "close"):
            left, right = getattr(a, field), getattr(b, field)
            difference = abs(left - right) / max(left, right)
            if difference > MAX_RELATIVE_OHLC_DIFFERENCE:
                raise MonitorDataError(
                    f"Feed OHLC mismatch {a.session.isoformat()} field={field}: "
                    "exceeds P01 relative tolerance"
                )
            maximum = max(maximum, difference)

    return FeedComparison(
        status="CONSISTENT_UNATTESTED",
        schema_version=1,
        symbol=expected_symbol,
        primary_provider_claim=primary_declaration.provider,
        secondary_provider_claim=secondary_declaration.provider,
        last_session=primary[-1].session.isoformat(),
        sessions_compared=len(primary),
        max_relative_ohlc_difference=maximum,
        tolerance=MAX_RELATIVE_OHLC_DIFFERENCE,
        primary_bars_sha256=_bar_digest(primary),
        secondary_bars_sha256=_bar_digest(secondary),
    )
