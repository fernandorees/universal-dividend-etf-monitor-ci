"""All mechanics-probe CI examples are fictional, never private archive data."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

from schd_monitor.mechanics_probe import (
    MECHANICS_COLUMNS, compare_mechanics, main,
)
from schd_monitor.models import MonitorDataError


def dates(n=75):
    out = []
    day = date(2024, 1, 2)
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = dates()


def rows(*, first_buy="1", first_fee="1", first_peak="100"):
    cash = D("1000")
    shares = D(0)
    peak = None
    out = []
    for i, day in enumerate(DAYS):
        before_cash, before_shares = cash, shares
        credits = D("2000") if i == 20 else D("1.25") if i == 50 else D(0)
        cash += credits
        side, qty, fee, effective = "NONE", D(0), D(0), ""
        if i == 0:
            side, qty, fee = "BUY", D(first_buy), D(first_fee)
        elif i == 30:
            side, qty = "BUY", D("2")
        elif i == 45:
            side, qty, fee = "SELL", shares, D("1")
        if side == "BUY":
            effective = D("100.0500")
            cash -= effective * qty + fee
            shares += qty
            if peak is None:
                peak = D(first_peak) if i == 0 else D(100)
        elif side == "SELL":
            effective = D("99.9500")
            cash += effective * qty - fee
            shares -= qty
            if shares == 0:
                peak = None
        out.append({
            "session": day.isoformat(),
            "open_price": "100",
            "close_price": "100",
            "fill_side": side,
            "shares_before_open": str(before_shares),
            "cash_before_credits": str(before_cash),
            "cash_credits_before_fill": str(credits),
            "fill_quantity": str(qty),
            "discretionary_fee": str(fee),
            "effective_fill_price": str(effective) if side != "NONE" else "",
            "shares_after_open": str(shares),
            "cash_after_open": str(cash),
            "held_peak_after_close": str(peak) if peak is not None else "",
        })
    return out


def write_csv(path, data, cols=MECHANICS_COLUMNS):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)


class TestLocalMechanicsProbe(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.reference = root / "original_paper.csv"
        self.candidate = root / "candidate_paper.csv"
        self.manifest = root / "contract.json"
        self.sessions = root / "sessions.csv"
        self.report = root / "report.json"
        self.base = rows()
        write_csv(self.reference, self.base)
        write_csv(self.candidate, self.base)
        self.spec = {
            "schema_version": 1, "symbol": "SCHD", "strategy": "CHAMPION",
            "first_session": DAYS[0].isoformat(),
            "last_session": DAYS[-1].isoformat(),
            "expected_session_count": len(DAYS),
            "hypothetical_cash_interest_rate": "0",
            "reference_origin_claim": "FICTIONAL_ORIGINAL_FOR_TESTING",
            "candidate_origin_claim": "FICTIONAL_CANDIDATE_FOR_TESTING",
        }
        self.manifest.write_text(json.dumps(self.spec), encoding="utf-8")
        self._calendar(DAYS)

    def _calendar(self, sessions):
        with self.sessions.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["date"])
            writer.writerows(([day.isoformat()] for day in sessions))

    def compare(self):
        return compare_mechanics(
            self.reference, self.candidate, self.manifest, self.sessions,
        )

    def change(self, i, key, value):
        data = [dict(row) for row in self.base]
        data[i][key] = value
        write_csv(self.candidate, data)

    def test_fictional_full_session_mechanics_match_is_not_historical_parity(self):
        r = self.compare()
        self.assertEqual(r.status, "MECHANICS_FIELDS_MATCH_UNATTESTED")
        self.assertEqual(r.sessions_checked, 75)
        self.assertEqual(r.field_comparisons, 75 * 12)
        self.assertEqual(r.field_differences, 0)
        self.assertFalse(r.original_archive_opened_by_tool)
        self.assertFalse(r.historical_sizing_fee_parity_verified)
        self.assertFalse(r.source_origin_independently_attested)
        self.assertFalse(r.original_peak_initialization_verified)
        self.assertFalse(r.real_money_signal_verified)
        self.assertEqual(len(r.reference_file_sha256), 64)

    def test_different_textual_decimal_scales_compare_equal(self):
        self.change(0, "effective_fill_price", "100.05000")
        r = self.compare()
        self.assertEqual(r.field_differences, 0)
        self.assertNotEqual(r.reference_file_sha256, r.candidate_file_sha256)

    def test_fully_coherent_different_buy_quantity_detected(self):
        write_csv(self.candidate, rows(first_buy="2"))
        r = self.compare()
        self.assertEqual(r.status, "MECHANICS_FIELDS_DIFFER_NO_PARITY")
        self.assertGreater(r.field_differences, 1)
        self.assertIn("fill_quantity", [x["field"] for x in r.first_differences])

    def test_fully_coherent_different_fee_detected(self):
        write_csv(self.candidate, rows(first_fee="2"))
        r = self.compare()
        self.assertEqual(r.status, "MECHANICS_FIELDS_DIFFER_NO_PARITY")
        self.assertIn("discretionary_fee", [x["field"] for x in r.first_differences])

    def test_same_cash_and_shares_but_different_initial_peak_is_visible(self):
        write_csv(self.candidate, rows(first_peak="105"))
        r = self.compare()
        self.assertGreater(r.field_differences, 0)
        self.assertEqual(r.first_differences[0],
                         {"session": DAYS[0].isoformat(),
                          "field": "held_peak_after_close"})

    def test_unexplained_cash_and_share_changes_fail_closed(self):
        for i, key, val in (
            (3, "cash_after_open", "100"),
            (3, "shares_after_open", "22"),
            (30, "shares_after_open", "7"),
            (45, "cash_after_open", "10"),
        ):
            with self.subTest(key=key, i=i):
                self.change(i, key, val)
                with self.assertRaisesRegex(MonitorDataError, "conservation"):
                    self.compare()

    def test_cash_or_share_continuity_break_rejected(self):
        for field, val in (("cash_before_credits", "900"),
                           ("shares_before_open", "2")):
            with self.subTest(field=field):
                self.change(8, field, val)
                with self.assertRaisesRegex(MonitorDataError, "continuity"):
                    self.compare()

    def test_wrong_friction_and_sign_fail_closed(self):
        for i, value in ((0, "100"), (45, "100.0500"), (45, "100")):
            with self.subTest(i=i, value=value):
                self.change(i, "effective_fill_price", value)
                with self.assertRaisesRegex(MonitorDataError, "0.05% friction"):
                    self.compare()

    def test_full_sell_required_no_fractional_exit(self):
        self.change(45, "fill_quantity", "1")
        with self.assertRaisesRegex(MonitorDataError, "full holding"):
            self.compare()

    def test_zero_or_missing_fill_quantity_rejected(self):
        for value in ("0", ""):
            with self.subTest(value=value):
                self.change(30, "fill_quantity", value)
                with self.assertRaises(MonitorDataError):
                    self.compare()

    def test_no_fill_cannot_contain_fee_or_effective_price(self):
        for key, value in (("discretionary_fee", "1"),
                           ("effective_fill_price", "100.0500"),
                           ("fill_quantity", "1")):
            with self.subTest(key=key):
                self.change(2, key, value)
                with self.assertRaisesRegex(MonitorDataError, "NONE row"):
                    self.compare()

    def test_nonfinite_negative_exponent_and_invalid_decimals_rejected(self):
        for value in ("NaN", "Infinity", "-2", "1e2", "1,000", "+2"):
            with self.subTest(value=value):
                self.change(4, "cash_after_open", value)
                with self.assertRaises(MonitorDataError):
                    self.compare()

    def test_insufficient_buying_cash_rejected(self):
        candidate = rows()
        candidate[0]["cash_before_credits"] = "0"
        with self.assertRaisesRegex(MonitorDataError, "exceeds cash"):
            write_csv(self.candidate, candidate)
            self.compare()

    def test_empty_peak_when_held_or_peak_when_flat_is_rejected(self):
        for i, value in ((4, ""), (47, "100")):
            with self.subTest(i=i):
                self.change(i, "held_peak_after_close", value)
                with self.assertRaisesRegex(MonitorDataError, "peak must be empty iff flat"):
                    self.compare()

    def test_missing_extra_duplicate_and_reordered_sessions_rejected(self):
        for data in (
            self.base[:-1],
            self.base + [self.base[-1]],
            self.base[:10] + [self.base[9]] + self.base[11:],
            self.base[:10] + [self.base[11], self.base[10]] + self.base[12:],
        ):
            with self.subTest(length=len(data)):
                write_csv(self.candidate, data)
                with self.assertRaises(MonitorDataError):
                    self.compare()

    def test_independent_calendar_catches_both_traces_missing_same_date(self):
        data = self.base[:10] + self.base[11:]
        write_csv(self.reference, data)
        write_csv(self.candidate, data)
        with self.assertRaises(MonitorDataError):
            self.compare()

    def test_manifest_interest_and_session_count_are_strict(self):
        for change in (
            {"hypothetical_cash_interest_rate": "0.02"},
            {"strategy": "RS20"},
            {"expected_session_count": 0},
            {"schema_version": 2},
        ):
            with self.subTest(change=change):
                self.manifest.write_text(json.dumps({**self.spec, **change}), encoding="utf-8")
                with self.assertRaises(MonitorDataError):
                    self.compare()
        self.manifest.write_text(json.dumps(self.spec), encoding="utf-8")
        self._calendar(DAYS[:-1])
        with self.assertRaisesRegex(MonitorDataError, "calendar"):
            self.compare()

    def test_wrong_column_order_and_unknown_side_rejected(self):
        write_csv(self.candidate, self.base, MECHANICS_COLUMNS[::-1])
        with self.assertRaisesRegex(MonitorDataError, "schema/order"):
            self.compare()
        self.change(4, "fill_side", "PENDING")
        with self.assertRaisesRegex(MonitorDataError, "fill_side"):
            self.compare()

    def test_bounded_date_field_differences_do_not_disclose_amounts(self):
        write_csv(self.candidate, rows(first_peak="109"))
        r = self.compare()
        self.assertTrue(all(set(x) == {"session", "field"} for x in r.first_differences))
        self.assertNotIn("reference_value", str(r.as_json()))
        self.assertNotIn("candidate_value", str(r.as_json()))

    def test_cli_success_creates_research_only_summary_without_overwriting(self):
        cmd = ["--reference", str(self.reference), "--candidate", str(self.candidate),
               "--manifest", str(self.manifest), "--sessions", str(self.sessions),
               "--report", str(self.report)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(cmd), 0)
        content = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(content["status"], "MECHANICS_FIELDS_MATCH_UNATTESTED")
        self.assertFalse(content["historical_sizing_fee_parity_verified"])
        backup = self.report.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(cmd), 2)
        self.assertEqual(self.report.read_bytes(), backup)

    def test_cli_mismatch_exits_two_without_money_values(self):
        write_csv(self.candidate, rows(first_peak="171"))
        cmd = ["--reference", str(self.reference), "--candidate", str(self.candidate),
               "--manifest", str(self.manifest), "--sessions", str(self.sessions)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(cmd)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"],
                         "MECHANICS_FIELDS_DIFFER_NO_PARITY")
        self.assertNotIn("171", output.getvalue())

    def test_cli_bad_input_fails_closed_without_private_path_or_values(self):
        cmd = ["--reference", str(self.reference), "--candidate", "not_found.csv",
               "--manifest", str(self.manifest), "--sessions", str(self.sessions)]
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = main(cmd)
        self.assertEqual(code, 2)
        self.assertIn("NO_HISTORICAL_MECHANICS_PARITY_ESTABLISHED", errors.getvalue())
        self.assertNotIn("not_found.csv", errors.getvalue())

    def test_reject_same_input_path_and_report_overwriting_inputs(self):
        with self.assertRaisesRegex(MonitorDataError, "distinct"):
            compare_mechanics(self.reference, self.reference,
                              self.manifest, self.sessions)
        cmd = ["--reference", str(self.reference), "--candidate", str(self.candidate),
               "--manifest", str(self.manifest), "--sessions", str(self.sessions),
               "--report", str(self.candidate)]
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(cmd), 2)

    def test_cash_contribution_and_full_exit_are_mathematically_checked(self):
        r = self.compare()
        self.assertEqual(r.field_differences, 0)
        self.assertEqual(D(self.base[20]["cash_credits_before_fill"]), D(2000))
        self.assertEqual(D(self.base[45]["shares_after_open"]), D(0))
        self.assertEqual(self.base[45]["held_peak_after_close"], "")


if __name__ == "__main__":
    unittest.main()
