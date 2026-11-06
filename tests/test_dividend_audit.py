"""P01 dividend audit tests use only fictional dates, amounts and shares."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from schd_monitor.cli import main
from schd_monitor.dividend_audit import (
    DividendEntry, DividendLedger, audit_dividend_ledger, dividend_ledger_from_json,
)
from schd_monitor.models import MonitorDataError


def sessions(n=90):
    result = []
    day = date(2024, 1, 2)
    while len(result) < n:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return result


DAYS = sessions()


def entry(event_id="A", ex_index=30, payment_index=40, amount="0.15",
          shares="50", cash="7.50"):
    return DividendEntry(
        event_id, DAYS[ex_index], DAYS[payment_index],
        Decimal(amount), Decimal(shares), None if cash is None else Decimal(cash)
    )


def ledger(*items, symbol="SCHD"):
    return DividendLedger(symbol, tuple(items))


def write_ledger(path, rows):
    path.write_text(json.dumps({
        "schema_version": 1,
        "symbol": "SCHD",
        "events": rows,
    }), encoding="utf-8")


def sample_event(**changes):
    return {
        "event_id": "A",
        "ex_session": DAYS[30].isoformat(),
        "payment_session": DAYS[40].isoformat(),
        "cash_per_share": "0.15",
        "eligible_shares_before_ex_open": "50",
        "gross_cash_recorded_at_payment_open": "7.50",
        **changes,
    }


def write_bars(path, falling=True):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close"])
        for i, day in enumerate(DAYS):
            close = 100-i*.1 if falling else 100+i*.1
            writer.writerow([day.isoformat(), close, close+.5, close-.5, close])


class TestDividendAudit(unittest.TestCase):
    def test_paid_gross_dividend_at_payment_open(self):
        result = audit_dividend_ledger(
            ledger(entry()), expected_symbol="SCHD", sessions=DAYS)
        self.assertEqual(result.status, "CONSISTENT_UNATTESTED")
        self.assertEqual(result.events_checked, 1)
        self.assertEqual(result.paid_events, 1)
        self.assertEqual(result.pending_events, 0)
        self.assertEqual(Decimal(result.gross_entitlement), Decimal("7.50"))
        self.assertEqual(Decimal(result.gross_payment_recorded), Decimal("7.50"))
        self.assertEqual(len(result.ledger_sha256), 64)

    def test_entitlement_accrues_at_ex_before_payment(self):
        e = entry()
        limited = DAYS[:35]
        result = audit_dividend_ledger(ledger(replace(e, gross_cash_recorded_at_payment_open=None)),
                                       expected_symbol="SCHD", sessions=limited)
        self.assertEqual(result.paid_events, 0)
        self.assertEqual(result.pending_events, 1)
        self.assertEqual(Decimal(result.gross_pending), Decimal("7.50"))

    def test_payment_open_boundary_requires_record(self):
        e = entry()
        before = audit_dividend_ledger(
            ledger(replace(e, gross_cash_recorded_at_payment_open=None)),
            expected_symbol="SCHD", sessions=DAYS[:40])
        self.assertEqual(before.pending_events, 1)
        with self.assertRaisesRegex(MonitorDataError, "no gross cash record"):
            audit_dividend_ledger(
                ledger(replace(e, gross_cash_recorded_at_payment_open=None)),
                expected_symbol="SCHD", sessions=DAYS[:41])

    def test_explicit_zero_entitlement_means_no_inferred_fill(self):
        # The optional auditor never reads a later Position.shares or a buy candidate.
        zero = entry(shares="0", cash="0")
        result = audit_dividend_ledger(ledger(zero), expected_symbol="SCHD", sessions=DAYS)
        self.assertEqual(Decimal(result.gross_entitlement), Decimal("0"))
        self.assertEqual(result.paid_events, 1)

    def test_multiple_distributions_paid_and_pending(self):
        pending = replace(entry("B", ex_index=70, payment_index=80,
                                amount=".2", shares="10", cash=None),
                          payment_session=DAYS[-1] + timedelta(days=7))
        result = audit_dividend_ledger(ledger(entry(), pending),
                                       expected_symbol="SCHD", sessions=DAYS)
        self.assertEqual((result.paid_events, result.pending_events), (1, 1))
        self.assertEqual(Decimal(result.gross_entitlement), Decimal("9.50"))
        self.assertEqual(Decimal(result.gross_payment_recorded), Decimal("7.50"))
        self.assertEqual(Decimal(result.gross_pending), Decimal("2.0"))

    def test_wrong_cash_amount_is_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "gross payment mismatch"):
            audit_dividend_ledger(ledger(entry(cash="7.49")),
                                  expected_symbol="SCHD", sessions=DAYS)

    def test_future_payment_credited_early_is_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "cannot be recorded early"):
            audit_dividend_ledger(ledger(entry()), expected_symbol="SCHD",
                                  sessions=DAYS[:35])

    def test_ex_date_missing_or_future_ex_date_fails_closed(self):
        with self.assertRaisesRegex(MonitorDataError, "ex session absent"):
            audit_dividend_ledger(ledger(entry()), expected_symbol="SCHD",
                                  sessions=DAYS[:30])
        absent = replace(entry(), ex_session=DAYS[30] + timedelta(days=4))
        with self.assertRaisesRegex(MonitorDataError, "ex session absent"):
            audit_dividend_ledger(ledger(absent), expected_symbol="SCHD", sessions=DAYS)

    def test_due_payment_date_missing_from_history_fails(self):
        e = replace(entry(), payment_session=DAYS[40] + timedelta(days=4))
        with self.assertRaisesRegex(MonitorDataError, "payment session absent"):
            audit_dividend_ledger(ledger(e), expected_symbol="SCHD", sessions=DAYS)

    def test_reject_reversed_dates_duplicate_ids_or_unordered_events(self):
        with self.assertRaisesRegex(MonitorDataError, "cannot precede"):
            replace(entry(), payment_session=DAYS[29])
        with self.assertRaisesRegex(MonitorDataError, "Duplicate dividend"):
            ledger(entry(), entry())
        with self.assertRaisesRegex(MonitorDataError, "ordered by ex session"):
            ledger(entry("B", 50, 60), entry("A", 30, 40))

    def test_reject_nonfinite_negative_and_wrong_decimal_types(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"),
                      "0.15", .15, None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(MonitorDataError):
                    replace(entry(), cash_per_share=value)
        with self.assertRaises(MonitorDataError):
            replace(entry(), cash_per_share=Decimal("0"))
        with self.assertRaises(MonitorDataError):
            replace(entry(), eligible_shares_before_ex_open=Decimal("-1"))
        with self.assertRaises(MonitorDataError):
            replace(entry(), gross_cash_recorded_at_payment_open=Decimal("-0.01"))
        # Decimal can be finite but still overflow when two huge inputs multiply.
        huge = replace(entry(), cash_per_share=Decimal("1E+999999"),
                       eligible_shares_before_ex_open=Decimal("1E+999999"))
        with self.assertRaisesRegex(MonitorDataError, "decimal range"):
            audit_dividend_ledger(ledger(huge), expected_symbol="SCHD", sessions=DAYS)

    def test_reject_invalid_symbol_schema_and_session_order(self):
        with self.assertRaisesRegex(MonitorDataError, "symbol"):
            audit_dividend_ledger(ledger(entry(), symbol="VYM"),
                                  expected_symbol="SCHD", sessions=DAYS)
        with self.assertRaisesRegex(MonitorDataError, "schema"):
            DividendLedger("SCHD", tuple(), schema_version=True)
        with self.assertRaisesRegex(MonitorDataError, "strictly ascending"):
            audit_dividend_ledger(ledger(entry()), expected_symbol="SCHD",
                                  sessions=DAYS[:50] + DAYS[49:])

    def test_empty_ledger_does_not_claim_complete_dividend_history(self):
        result = audit_dividend_ledger(ledger(), expected_symbol="SCHD", sessions=DAYS)
        self.assertEqual(result.status, "CONSISTENT_UNATTESTED")
        self.assertEqual(result.events_checked, 0)
        self.assertEqual(result.gross_payment_recorded, "0")

    def test_versioned_json_validation_and_exact_decimal_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "dividends.json"
            write_ledger(p, [sample_event()])
            self.assertEqual(dividend_ledger_from_json(p), ledger(entry()))
            cases = [
                sample_event(cash_per_share=0.15),
                sample_event(cash_per_share="NaN"),
                sample_event(cash_per_share="Infinity"),
                sample_event(cash_per_share="-0.1"),
                sample_event(cash_per_share="0"),
                sample_event(eligible_shares_before_ex_open="-3"),
                sample_event(ex_session="20240315"),
                sample_event(payment_session="2024-99-99"),
                sample_event(gross_cash_recorded_at_payment_open=7.50),
                {**sample_event(), "unknown": 10},
            ]
            for record in cases:
                with self.subTest(record=record):
                    write_ledger(p, [record])
                    with self.assertRaises(MonitorDataError):
                        dividend_ledger_from_json(p)
            p.write_text(json.dumps({"schema_version": 2, "symbol": "SCHD", "events": []}),
                         encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "schema"):
                dividend_ledger_from_json(p)
            p.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "Invalid dividend-ledger JSON"):
                dividend_ledger_from_json(p)

    def test_cli_mismatched_dividend_blocks_new_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bars, position = root / "prices.csv", root / "position.json"
            div, dest = root / "dividends.json", root / "output.json"
            write_bars(bars)
            position.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 0,
                "peak_close": None, "reconciled_through": DAYS[-1].isoformat(),
            }), encoding="utf-8")
            write_ledger(div, [sample_event(gross_cash_recorded_at_payment_open="7.49")])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main(["--csv", str(bars), "--position", str(position),
                             "--dividend-ledger", str(div), "--output", str(dest)])
            self.assertEqual(code, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", err.getvalue())
            self.assertIn("gross payment mismatch", err.getvalue())
            self.assertFalse(dest.exists())

    def test_cli_audit_cannot_credit_cash_or_verify_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bars, position, div = root / "prices.csv", root / "position.json", root / "dividends.json"
            write_bars(bars, falling=True)
            position.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 0,
                "peak_close": None, "reconciled_through": DAYS[-1].isoformat(),
            }), encoding="utf-8")
            write_ledger(div, [sample_event()])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--csv", str(bars), "--position", str(position),
                             "--dividend-ledger", str(div)])
            self.assertEqual(code, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["dividend_audit"]["status"], "CONSISTENT_UNATTESTED")
            self.assertEqual(result["result"]["verification"], "UNVERIFIED_DO_NOT_TRADE")
            self.assertEqual(result["result"]["candidate_action"], "WAIT_IN_CASH")


if __name__ == "__main__":
    unittest.main()
