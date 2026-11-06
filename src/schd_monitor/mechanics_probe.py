"""P01 offline, strict original-versus-candidate transaction mechanics probe.

Two user-prepared complete local CSVs are compared, not generated. No source
archive, broker, signal engine or holdings are accessed automatically. All
successful matches remain UNATTESTED, never historical simulator parity.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal, Inexact, InvalidOperation, Overflow, Rounded, localcontext
from pathlib import Path

from .data import expected_sessions_from_csv
from .models import MonitorDataError
from .reference_parity import (
    _date, _decimal, _hash_file, manifest_from_json,
)

MECHANICS_PROBE_SCHEMA = 1
MECHANICS_COLUMNS = (
    "session", "open_price", "close_price", "fill_side",
    "shares_before_open", "cash_before_credits",
    "cash_credits_before_fill", "fill_quantity", "discretionary_fee",
    "effective_fill_price", "shares_after_open", "cash_after_open",
    "held_peak_after_close",
)
NUMERIC_COLUMNS = tuple(
    name for name in MECHANICS_COLUMNS
    if name not in {"session", "fill_side"}
)
OPTIONAL_COLUMNS = frozenset({"effective_fill_price", "held_peak_after_close"})
FRICTION = Decimal("0.0005")


def _quantity(text: object, field: str, *, positive: bool = False) -> Decimal:
    number = _decimal(text, field, zero_allowed=not positive)
    if number < 0 or len(number.as_tuple().digits) > 50 or abs(number.as_tuple().exponent) > 50:
        raise MonitorDataError(f"Mechanics field {field} outside exact nonnegative bounds")
    return number


def _read_mechanics(path: Path, sessions: list) -> list[dict]:
    rows = []
    previous = None
    try:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(MECHANICS_COLUMNS):
                raise MonitorDataError("Mechanics CSV schema/order must match version 1")
            with localcontext() as ctx:
                ctx.prec = 60
                for trap in (InvalidOperation, Inexact, Rounded, Overflow):
                    ctx.traps[trap] = True
                for i, raw in enumerate(reader):
                    if i >= len(sessions) or set(raw) != set(MECHANICS_COLUMNS):
                        raise MonitorDataError("Mechanics trace contains extra or malformed session")
                    session = _date(raw["session"], "mechanics session")
                    if session != sessions[i]:
                        raise MonitorDataError("Mechanics missing, duplicate or out-of-order session")
                    side = raw["fill_side"]
                    if side not in {"BUY", "SELL", "NONE"}:
                        raise MonitorDataError("Mechanics fill_side must be BUY, SELL or NONE")
                    row = {"session": session, "fill_side": side}
                    for key in NUMERIC_COLUMNS:
                        if raw[key] == "" and key in OPTIONAL_COLUMNS:
                            row[key] = None
                        else:
                            row[key] = _quantity(raw[key], key, positive=key in {
                                "open_price", "close_price", "effective_fill_price",
                            })
                    if previous is not None:
                        if (row["shares_before_open"] != previous["shares_after_open"]
                                or row["cash_before_credits"] != previous["cash_after_open"]):
                            raise MonitorDataError("Mechanics holdings or cash continuity mismatch")
                    before = row["shares_before_open"]
                    funded = row["cash_before_credits"] + row["cash_credits_before_fill"]
                    qty = row["fill_quantity"]
                    fee = row["discretionary_fee"]
                    effective = row["effective_fill_price"]
                    if side == "NONE":
                        if qty != 0 or fee != 0 or effective is not None:
                            raise MonitorDataError("Mechanics NONE row cannot contain a fill or fee")
                        expected_shares = before
                        expected_cash = funded
                    else:
                        if qty <= 0 or effective is None:
                            raise MonitorDataError("Mechanics fill requires quantity and effective price")
                        expected_price = row["open_price"] * (
                            (Decimal(1) + FRICTION) if side == "BUY"
                            else (Decimal(1) - FRICTION)
                        )
                        if effective != expected_price:
                            raise MonitorDataError("Mechanics effective price inconsistent with 0.05% friction")
                        if side == "BUY":
                            expected_shares = before + qty
                            expected_cash = funded - effective * qty - fee
                        else:
                            if qty != before:
                                raise MonitorDataError("Mechanics champion SELL must close the full holding")
                            expected_shares = before - qty
                            expected_cash = funded + effective * qty - fee
                    if expected_cash < 0:
                        raise MonitorDataError("Mechanics transaction exceeds cash or has negative proceeds")
                    if (row["shares_after_open"] != expected_shares
                            or row["cash_after_open"] != expected_cash):
                        raise MonitorDataError("Mechanics share/cash conservation mismatch")
                    peak = row["held_peak_after_close"]
                    if (expected_shares == 0) != (peak is None):
                        raise MonitorDataError("Mechanics held peak must be empty iff flat")
                    # DO NOT invent an entry-peak seed. We only establish that
                    # the supplied peak is positive, not the original source
                    # initialization or end-of-session recurrence.
                    if peak is not None and peak <= 0:
                        raise MonitorDataError("Mechanics held peak must be positive")
                    rows.append(row)
                    previous = row
    except ArithmeticError as exc:
        raise MonitorDataError("Mechanics numeric exactness limit exceeded") from exc
    if len(rows) != len(sessions):
        raise MonitorDataError("Mechanics trace does not cover all expected sessions")
    return rows


@dataclass(frozen=True)
class MechanicsProbeReport:
    status: str
    schema_version: int
    comparison_scope: str
    symbol: str
    sessions_checked: int
    first_session: str
    last_session: str
    field_comparisons: int
    field_differences: int
    first_differences: list[dict[str, str]]
    reference_file_sha256: str
    candidate_file_sha256: str
    manifest_file_sha256: str
    sessions_file_sha256: str
    original_archive_opened_by_tool: bool
    source_origin_independently_attested: bool
    original_peak_initialization_verified: bool
    historical_sizing_fee_parity_verified: bool
    real_money_signal_verified: bool

    def as_json(self) -> dict:
        return asdict(self)


def compare_mechanics(
    reference_csv: str | Path, candidate_csv: str | Path,
    manifest_json: str | Path, expected_sessions_csv: str | Path,
) -> MechanicsProbeReport:
    """Strict exact comparison of supplied local records; never promote parity."""
    reference, candidate = Path(reference_csv), Path(candidate_csv)
    manifest_path, dates_path = Path(manifest_json), Path(expected_sessions_csv)
    if reference.resolve() == candidate.resolve():
        raise MonitorDataError("Mechanics reference and candidate must be distinct files")
    manifest = manifest_from_json(manifest_path)
    sessions = expected_sessions_from_csv(dates_path)
    if (len(sessions) != manifest.expected_session_count or not sessions
            or sessions[0] != manifest.first_session
            or sessions[-1] != manifest.last_session):
        raise MonitorDataError("Mechanics calendar does not match independently supplied manifest")
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise MonitorDataError("Mechanics calendar must be sorted and unique")
    if any(day.weekday() >= 5 for day in sessions):
        raise MonitorDataError("Mechanics calendar contains weekend session")
    ref_rows = _read_mechanics(reference, sessions)
    cand_rows = _read_mechanics(candidate, sessions)
    differences = []
    total = 0
    for ref, cand in zip(ref_rows, cand_rows):
        for key in MECHANICS_COLUMNS[1:]:
            if ref[key] != cand[key]:
                total += 1
                if len(differences) < 10:
                    # Do NOT print raw private transaction values in reports.
                    differences.append({"session": ref["session"].isoformat(), "field": key})
    return MechanicsProbeReport(
        status=("MECHANICS_FIELDS_MATCH_UNATTESTED" if total == 0
                else "MECHANICS_FIELDS_DIFFER_NO_PARITY"),
        schema_version=MECHANICS_PROBE_SCHEMA,
        comparison_scope="SUPPLIED_LOCAL_FULL_SESSION_TRANSACTION_MECHANICS_ONLY",
        symbol=manifest.symbol, sessions_checked=len(sessions),
        first_session=sessions[0].isoformat(), last_session=sessions[-1].isoformat(),
        field_comparisons=len(sessions) * (len(MECHANICS_COLUMNS) - 1),
        field_differences=total, first_differences=differences,
        reference_file_sha256=_hash_file(reference),
        candidate_file_sha256=_hash_file(candidate),
        manifest_file_sha256=_hash_file(manifest_path),
        sessions_file_sha256=_hash_file(dates_path),
        original_archive_opened_by_tool=False,
        source_origin_independently_attested=False,
        original_peak_initialization_verified=False,
        historical_sizing_fee_parity_verified=False,
        real_money_signal_verified=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local transaction mechanics probe; not archived simulator parity"
    )
    parser.add_argument("--reference", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--sessions", required=True)
    parser.add_argument("--report", help="Optional new local JSON report, no overwrites")
    args = parser.parse_args(argv)
    try:
        result = compare_mechanics(
            args.reference, args.candidate, args.manifest, args.sessions,
        )
        output = json.dumps(result.as_json(), indent=2, sort_keys=True) + "\n"
        if args.report:
            dest = Path(args.report)
            if dest.resolve() in {
                Path(p).resolve() for p in (
                    args.reference, args.candidate, args.manifest, args.sessions
                )
            }:
                raise MonitorDataError("Mechanics summary path cannot overwrite an input")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("x", encoding="utf-8") as stream:
                stream.write(output)
        print(output, end="")
        return 0 if result.field_differences == 0 else 2
    except (MonitorDataError, OSError, ValueError, TypeError) as exc:
        # Avoid echoing paths/data from untrusted private local input in errors.
        print(json.dumps({
            "status": "NO_HISTORICAL_MECHANICS_PARITY_ESTABLISHED",
            "error_type": type(exc).__name__,
        }), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
