"""P01 local-only normalized trace comparator. NOT an independent simulator replay.

Only compares already prepared, complete, separately produced trace CSVs.
It cannot authenticate source origins or prove the original SCHD replay.
Never import private reference material into GitHub, Replit or CI.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .data import expected_sessions_from_csv
from .models import MonitorDataError

PARITY_SCHEMA = 1
# The normalized historical trace is deliberately exhaustive: prices,
# indicators, signal eligibility and outcomes, fills, holdings and all flows.
TRACE_COLUMNS = (
    "session", "open", "high", "low", "close",
    "sma60", "atr14", "macd", "macd_signal", "bearish_cross",
    "highest_held_close", "buy_eligible", "sell_eligible",
    "signal_action", "fill_action", "fill_price", "shares", "cash",
    "monthly_deposit", "dividend_ex_accrual", "dividend_payment",
    "interest_credit", "equity_value", "ending_wealth",
)
DECIMAL_COLUMNS = (
    "open", "high", "low", "close", "sma60", "atr14", "macd",
    "macd_signal", "highest_held_close", "fill_price", "shares", "cash",
    "monthly_deposit", "dividend_ex_accrual", "dividend_payment",
    "interest_credit", "equity_value", "ending_wealth",
)
MAY_BE_EMPTY = frozenset({"sma60", "atr14", "macd", "macd_signal",
                         "highest_held_close", "fill_price"})
NONNEGATIVE = frozenset({
    "open", "high", "low", "close", "atr14", "highest_held_close",
    "fill_price", "shares", "cash", "monthly_deposit",
    "dividend_ex_accrual", "dividend_payment", "interest_credit",
    "equity_value", "ending_wealth",
})
BOOL_COLUMNS = frozenset({"bearish_cross", "buy_eligible", "sell_eligible"})
ACTIONS = frozenset({"BUY", "SELL", "HOLD", "WAIT_IN_CASH", "NO_SIGNAL"})
FILLS = frozenset({"BUY", "SELL", "NONE"})
DECIMAL_LITERAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


def _date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise MonitorDataError(f"{label} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MonitorDataError(f"Invalid {label}") from exc
    if parsed.isoformat() != value:
        raise MonitorDataError(f"{label} must be canonical YYYY-MM-DD")
    return parsed


def _decimal(value: object, label: str, *, zero_allowed: bool = True) -> Decimal:
    if not isinstance(value, str) or len(value) > 100 or not DECIMAL_LITERAL.fullmatch(value):
        raise MonitorDataError(f"Invalid fixed-point decimal in {label}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise MonitorDataError(f"Invalid decimal in {label}") from exc
    if not parsed.is_finite() or (not zero_allowed and parsed <= 0):
        raise MonitorDataError(f"Nonfinite/nonpositive decimal in {label}")
    return parsed


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class ParityManifest:
    symbol: str
    strategy: str
    first_session: date
    last_session: date
    expected_session_count: int
    hypothetical_cash_interest_rate: Decimal
    reference_origin_claim: str
    candidate_origin_claim: str
    schema_version: int = PARITY_SCHEMA

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PARITY_SCHEMA:
            raise MonitorDataError("Unsupported parity manifest version")
        if (not isinstance(self.symbol, str) or not self.symbol
                or self.symbol != self.symbol.strip().upper()):
            raise MonitorDataError("Parity symbol must be uppercase")
        if self.strategy != "CHAMPION":
            raise MonitorDataError("Only frozen CHAMPION trace comparison is supported")
        if type(self.first_session) is not date or type(self.last_session) is not date:
            raise MonitorDataError("Parity manifest dates must be date objects")
        if self.first_session > self.last_session:
            raise MonitorDataError("Parity first_session after last_session")
        if type(self.expected_session_count) is not int or self.expected_session_count < 1:
            raise MonitorDataError("Parity expected_session_count must be a positive integer")
        if (not isinstance(self.hypothetical_cash_interest_rate, Decimal)
                or not self.hypothetical_cash_interest_rate.is_finite()
                or self.hypothetical_cash_interest_rate not in {
                    Decimal("0"), Decimal("0.03"), Decimal("0.05")
                }):
            raise MonitorDataError("Parity cash-interest scenario must be 0, 0.03 or 0.05")
        for key in ("reference_origin_claim", "candidate_origin_claim"):
            value = getattr(self, key)
            if (not isinstance(value, str) or not value.strip() or value != value.strip()
                    or len(value) > 120):
                raise MonitorDataError(f"Parity {key} must be a short nonempty local claim")
        if self.reference_origin_claim.casefold() == self.candidate_origin_claim.casefold():
            raise MonitorDataError("Parity reference and candidate origin claims must differ")


def manifest_from_json(path: str | Path) -> ParityManifest:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"),
                         object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise MonitorDataError("Invalid parity manifest JSON") from exc
    keys = {
        "schema_version", "symbol", "strategy", "first_session",
        "last_session", "expected_session_count",
        "hypothetical_cash_interest_rate", "reference_origin_claim",
        "candidate_origin_claim",
    }
    if not isinstance(raw, dict) or set(raw) != keys:
        raise MonitorDataError("Parity manifest requires exactly versioned fields")
    return ParityManifest(
        symbol=raw["symbol"], strategy=raw["strategy"],
        first_session=_date(raw["first_session"], "first_session"),
        last_session=_date(raw["last_session"], "last_session"),
        expected_session_count=raw["expected_session_count"],
        hypothetical_cash_interest_rate=_decimal(
            raw["hypothetical_cash_interest_rate"], "hypothetical_cash_interest_rate"
        ), reference_origin_claim=raw["reference_origin_claim"],
        candidate_origin_claim=raw["candidate_origin_claim"],
        schema_version=raw["schema_version"],
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MonitorDataError("Duplicate parity manifest JSON key")
        result[key] = value
    return result


def _trace_from_csv(path: Path, sessions: list[date]) -> list[dict]:
    data = []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(TRACE_COLUMNS):
            raise MonitorDataError("Parity trace CSV columns/order must match schema v1 exactly")
        for i, raw in enumerate(reader):
            if set(raw) != set(TRACE_COLUMNS):
                raise MonitorDataError(f"Malformed parity CSV at row {i + 2}")
            if i >= len(sessions):
                raise MonitorDataError("Parity trace has extra or unexpected trading sessions")
            day = _date(raw["session"], "parity session")
            if day != sessions[i]:
                raise MonitorDataError(f"Parity trace missing/unexpected session at row {i + 2}")
            parsed = {"session": day}
            for key in DECIMAL_COLUMNS:
                value = raw[key]
                if value == "" and key in MAY_BE_EMPTY:
                    parsed[key] = None
                else:
                    number = _decimal(value, key, zero_allowed=key not in {
                        "open", "high", "low", "close", "atr14", "fill_price",
                    })
                    if key in NONNEGATIVE and number < 0:
                        raise MonitorDataError(f"Negative amount in parity {key} at {day}")
                    parsed[key] = number
            for key in BOOL_COLUMNS:
                if raw[key] not in {"true", "false"}:
                    raise MonitorDataError(f"Invalid Boolean parity {key} at {day}")
                parsed[key] = raw[key] == "true"
            if raw["signal_action"] not in ACTIONS:
                raise MonitorDataError(f"Invalid parity signal_action at {day}")
            if raw["fill_action"] not in FILLS:
                raise MonitorDataError(f"Invalid parity fill_action at {day}")
            parsed["signal_action"] = raw["signal_action"]
            parsed["fill_action"] = raw["fill_action"]
            if (parsed["fill_action"] == "NONE") != (parsed["fill_price"] is None):
                raise MonitorDataError(f"Parity fill_action/fill_price inconsistent at {day}")
            if (parsed["shares"] == 0) != (parsed["highest_held_close"] is None):
                raise MonitorDataError(f"Parity held peak/shares inconsistent at {day}")
            if parsed["high"] < max(parsed["open"], parsed["low"], parsed["close"]):
                raise MonitorDataError(f"Invalid parity high at {day}")
            if parsed["low"] > min(parsed["open"], parsed["high"], parsed["close"]):
                raise MonitorDataError(f"Invalid parity low at {day}")
            data.append(parsed)
    if len(data) != len(sessions):
        raise MonitorDataError("Parity trace has missing completed sessions")
    return data


@dataclass(frozen=True)
class ParityReport:
    status: str
    schema_version: int
    comparison_scope: str
    symbol: str
    strategy: str
    first_session: str
    last_session: str
    sessions_compared: int
    fields_compared: int
    differences: int
    first_differences: list[dict[str, str]]
    hypothetical_cash_interest_rate: str
    reference_origin_claim: str
    candidate_origin_claim: str
    reference_file_sha256: str
    candidate_file_sha256: str
    manifest_file_sha256: str
    sessions_file_sha256: str
    original_reference_replay_performed: bool
    original_simulator_parity_verified: bool

    def as_json(self) -> dict:
        return asdict(self)


def compare_local_traces(
    reference_csv: str | Path,
    candidate_csv: str | Path,
    manifest_json: str | Path,
    expected_sessions_csv: str | Path,
) -> ParityReport:
    """Compare normalized local exports; NEVER claim historical parity verification."""
    reference, candidate = Path(reference_csv), Path(candidate_csv)
    manifest_path, calendar_path = Path(manifest_json), Path(expected_sessions_csv)
    if reference.resolve() == candidate.resolve():
        raise MonitorDataError("Parity reference and candidate must be separate local files")
    manifest = manifest_from_json(manifest_path)
    sessions = expected_sessions_from_csv(calendar_path)
    if (len(sessions) != manifest.expected_session_count or not sessions
            or sessions[0] != manifest.first_session
            or sessions[-1] != manifest.last_session):
        raise MonitorDataError("Parity independent session manifest does not match run specification")
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise MonitorDataError("Parity expected sessions must be ascending and unique")
    if any(s.weekday() >= 5 for s in sessions):
        raise MonitorDataError("Weekend in parity expected-session manifest")
    ref_rows = _trace_from_csv(reference, sessions)
    cand_rows = _trace_from_csv(candidate, sessions)
    discrepancies: list[dict[str, str]] = []
    difference_count = 0
    for r, c in zip(ref_rows, cand_rows):
        for field in TRACE_COLUMNS[1:]:
            if r[field] != c[field]:
                difference_count += 1
                if len(discrepancies) < 10:
                    # No private prices, position sizes, fills or cash in report.
                    discrepancies.append({"session": r["session"].isoformat(), "field": field})
    # Exact numeric Decimal comparison: no tolerance can silently waive an
    # historical financial difference. A separate reviewed policy is needed.
    return ParityReport(
        status="TRACE_MATCH_UNATTESTED" if difference_count == 0
               else "TRACE_MISMATCH_NO_PARITY_CLAIM",
        schema_version=PARITY_SCHEMA,
        comparison_scope="NORMALIZED_LOCAL_TRACE_TO_TRACE_ONLY",
        symbol=manifest.symbol, strategy=manifest.strategy,
        first_session=sessions[0].isoformat(),
        last_session=sessions[-1].isoformat(),
        sessions_compared=len(sessions),
        fields_compared=len(sessions) * (len(TRACE_COLUMNS) - 1),
        differences=difference_count, first_differences=discrepancies,
        hypothetical_cash_interest_rate=str(manifest.hypothetical_cash_interest_rate),
        reference_origin_claim=manifest.reference_origin_claim,
        candidate_origin_claim=manifest.candidate_origin_claim,
        reference_file_sha256=_hash_file(reference),
        candidate_file_sha256=_hash_file(candidate),
        manifest_file_sha256=_hash_file(manifest_path),
        sessions_file_sha256=_hash_file(calendar_path),
        original_reference_replay_performed=False,
        original_simulator_parity_verified=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline normalized-reference trace comparison; NOT simulator parity verification"
    )
    parser.add_argument("--reference", required=True, help="Locally prepared normalized original trace CSV")
    parser.add_argument("--candidate", required=True, help="Separately generated normalized candidate trace CSV")
    parser.add_argument("--manifest", required=True, help="Versioned local run contract JSON")
    parser.add_argument("--sessions", required=True, help="Independently prepared exact complete session-date CSV")
    parser.add_argument("--report", help="Optional local output JSON summary; never overwrite")
    args = parser.parse_args(argv)
    try:
        result = compare_local_traces(
            args.reference, args.candidate, args.manifest, args.sessions
        )
        output = json.dumps(result.as_json(), indent=2, sort_keys=True) + "\n"
        if args.report:
            output_path = Path(args.report)
            # Never overwrite an existing report or a referenced input file.
            if output_path.resolve() in {
                Path(x).resolve() for x in (
                    args.reference, args.candidate, args.manifest, args.sessions
                )
            }:
                raise MonitorDataError("Parity report path cannot overwrite an input")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("x", encoding="utf-8") as report:
                report.write(output)
        print(output, end="")
        return 0 if result.differences == 0 else 2
    except (MonitorDataError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({
            "status": "NO_HISTORICAL_PARITY_ESTABLISHED",
            "error": str(exc),
        }, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
