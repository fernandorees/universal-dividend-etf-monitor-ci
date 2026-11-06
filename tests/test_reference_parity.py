"""P01 parity comparison CI fixtures are fully synthetic, never archival exports."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from schd_monitor.models import MonitorDataError
from schd_monitor.reference_parity import (
    TRACE_COLUMNS, compare_local_traces, main, manifest_from_json,
)


def dates(n=75):
    result = []
    d = date(2024, 1, 2)
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d)
        d += timedelta(days=1)
    return result


DAYS = dates()


def synthetic_rows():
    rows = []
    cash, shares, peak = 1000, 0, None
    for i, session in enumerate(DAYS):
        close = 100 + (i % 17)
        fill = "NONE"
        price = ""
        if i == 10 or i == 60:
            fill, price = "BUY", "100"
            cash -= 200
            shares += 2
            peak = None
        elif i == 40:
            fill, price = "SELL", "100"
            cash += 200
            shares = 0
            peak = None
        if i == 15:
            cash += 20
        if i == 32:
            cash += 1.5
        if shares:
            peak = close if peak is None else max(peak, close)
        row = {
            "session": session.isoformat(),
            "open": str(close), "high": str(close + 1),
            "low": str(close - 1), "close": str(close),
            "sma60": "" if i < 59 else str(close - 2),
            "atr14": "" if i < 14 else "1.7",
            "macd": "" if i < 34 else "-0.123",
            "macd_signal": "" if i < 34 else "-0.120",
            "bearish_cross": "false",
            "highest_held_close": "" if peak is None else str(peak),
            "buy_eligible": "true" if i in (9, 59) else "false",
            "sell_eligible": "true" if i == 39 else "false",
            "signal_action": "BUY" if i in (9, 59) else "SELL" if i == 39
                             else "HOLD" if shares else "WAIT_IN_CASH",
            "fill_action": fill, "fill_price": price,
            "shares": str(shares), "cash": str(cash),
            "monthly_deposit": "20" if i == 15 else "0",
            "dividend_ex_accrual": "1.5" if i == 25 else "0",
            "dividend_payment": "1.5" if i == 32 else "0",
            "interest_credit": "0",
            "equity_value": str(shares * close),
            "ending_wealth": str(cash + shares * close),
        }
        rows.append(row)
    return rows


def write_trace(path, rows, headers=TRACE_COLUMNS):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_manifest(path, **changes):
    spec = {
        "schema_version": 1, "symbol": "SCHD", "strategy": "CHAMPION",
        "first_session": DAYS[0].isoformat(),
        "last_session": DAYS[-1].isoformat(),
        "expected_session_count": len(DAYS),
        "hypothetical_cash_interest_rate": "0.03",
        "reference_origin_claim": "FICTIONAL_SOURCE_A",
        "candidate_origin_claim": "FICTIONAL_SOURCE_B",
        **changes,
    }
    path.write_text(json.dumps(spec), encoding="utf-8")


def write_calendar(path, days=DAYS):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date"])
        writer.writerows(([day.isoformat()] for day in days))


class ParityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.reference = folder / "reference.csv"
        self.candidate = folder / "candidate.csv"
        self.manifest = folder / "manifest.json"
        self.sessions = folder / "sessions.csv"
        self.report = folder / "report.json"
        self.rows = synthetic_rows()
        write_trace(self.reference, self.rows)
        write_trace(self.candidate, self.rows)
        write_manifest(self.manifest)
        write_calendar(self.sessions)

    def compare(self):
        return compare_local_traces(
            self.reference, self.candidate, self.manifest, self.sessions
        )

    def change_candidate(self, index, field, value):
        rows = deepcopy(self.rows)
        rows[index][field] = value
        write_trace(self.candidate, rows)

    def test_exact_equality_is_unattested_not_historical_parity(self):
        report = self.compare()
        self.assertEqual(report.status, "TRACE_MATCH_UNATTESTED")
        self.assertEqual(report.differences, 0)
        self.assertEqual(report.sessions_compared, 75)
        self.assertEqual(report.fields_compared, 75 * (len(TRACE_COLUMNS) - 1))
        self.assertEqual(len(report.reference_file_sha256), 64)
        self.assertEqual(report.reference_file_sha256, report.candidate_file_sha256)
        self.assertFalse(report.original_reference_replay_performed)
        self.assertFalse(report.original_simulator_parity_verified)

    def test_different_decimal_text_same_exact_value_matches(self):
        self.change_candidate(20, "close", "103.00000")  # original is 103
        report = self.compare()
        self.assertEqual(report.differences, 0)
        self.assertNotEqual(report.reference_file_sha256, report.candidate_file_sha256)

    def test_tiny_indicator_difference_is_not_silently_waived(self):
        self.change_candidate(60, "macd", "-0.123000000000000001")
        report = self.compare()
        self.assertEqual(report.differences, 1)
        self.assertEqual(report.first_differences,
                         [{"session": DAYS[60].isoformat(), "field": "macd"}])

    def test_detects_buy_sell_eligibility_signal_and_execution_changes(self):
        for index, field, replacement in (
            (9, "buy_eligible", "false"), (9, "signal_action", "HOLD"),
            (10, "fill_action", "SELL"),
        ):
            with self.subTest(field=field):
                self.change_candidate(index, field, replacement)
                self.assertEqual(self.compare().status, "TRACE_MISMATCH_NO_PARITY_CLAIM")

    def test_detects_peaks_shares_cash_and_dividend_cash_flows(self):
        for index, field, value in (
            (12, "highest_held_close", "140"),
            (12, "shares", "3"),
            (12, "cash", "801"),
            (15, "monthly_deposit", "21"),
            (25, "dividend_ex_accrual", "1.6"),
            (32, "dividend_payment", "1.6"),
            (32, "interest_credit", "0.01"),
            (40, "fill_price", "101"),
            (60, "equity_value", "1"),
            (60, "ending_wealth", "1"),
        ):
            with self.subTest(field=field):
                self.change_candidate(index, field, value)
                report = self.compare()
                self.assertEqual(report.differences, 1)
                self.assertEqual(report.first_differences[0]["field"], field)

    def test_multiple_discrepancies_are_counted_with_bounded_samples(self):
        rows = deepcopy(self.rows)
        for i in range(30, 50):
            rows[i]["close"] = str(int(rows[i]["close"]) + 1)
        write_trace(self.candidate, rows)
        report = self.compare()
        self.assertEqual(report.differences, 20)
        self.assertEqual(len(report.first_differences), 10)
        # Diagnostic report never contains private prices, positions or cash amounts.
        self.assertNotIn("reference_value", str(report.as_json()))
        self.assertNotIn("candidate_value", str(report.as_json()))

    def test_missing_middle_session_rejected_even_with_matching_latest_date(self):
        rows = self.rows[:35] + self.rows[36:]
        write_trace(self.candidate, rows)
        with self.assertRaisesRegex(MonitorDataError, "missing/unexpected session"):
            self.compare()

    def test_extra_session_and_reordered_session_rejected(self):
        for rows in (self.rows + [self.rows[-1]],
                     self.rows[:15] + [self.rows[16], self.rows[15]] + self.rows[17:]):
            with self.subTest(length=len(rows)):
                write_trace(self.candidate, rows)
                with self.assertRaisesRegex(MonitorDataError, "session"):
                    self.compare()

    def test_independent_expected_manifest_detects_both_traces_missing_same_date(self):
        rows = self.rows[:20] + self.rows[21:]
        write_trace(self.reference, rows)
        write_trace(self.candidate, rows)
        with self.assertRaisesRegex(MonitorDataError, "session"):
            self.compare()

    def test_independent_manifest_must_match_window_count_and_order(self):
        write_calendar(self.sessions, DAYS[:-1])
        with self.assertRaisesRegex(MonitorDataError, "independent session manifest"):
            self.compare()
        # Keep expected row-count/endpoints intact while inserting a duplicate.
        write_calendar(self.sessions, DAYS[:10] + [DAYS[9]] + DAYS[11:])
        with self.assertRaisesRegex(MonitorDataError, "ascending and unique"):
            self.compare()

    def test_reject_unsupported_strategy_and_interest_scenario(self):
        for change in (
            {"strategy": "RS20"},
            {"hypothetical_cash_interest_rate": "0.04"},
            {"hypothetical_cash_interest_rate": 0.03},
            {"schema_version": 2},
            {"schema_version": True},
            {"expected_session_count": False},
            {"candidate_origin_claim": "fictional_source_a"},
        ):
            with self.subTest(change=change):
                write_manifest(self.manifest, **change)
                with self.assertRaises(MonitorDataError):
                    self.compare()

    def test_manifest_rejects_bad_dates_missing_fields_or_duplicate_keys(self):
        write_manifest(self.manifest, first_session="20240102")
        with self.assertRaises(MonitorDataError):
            self.compare()
        self.manifest.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(MonitorDataError, "Duplicate"):
            self.compare()
        self.manifest.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(MonitorDataError, "Invalid parity manifest"):
            self.compare()

    def test_csv_schema_exact_and_no_unnamed_columns(self):
        for columns in (TRACE_COLUMNS[1:], TRACE_COLUMNS[::-1],
                        TRACE_COLUMNS + ("extra",)):
            with self.subTest(columns=columns[:2]):
                write_trace(self.candidate, self.rows, headers=columns)
                with self.assertRaisesRegex(MonitorDataError, "columns/order"):
                    self.compare()

    def test_reject_nan_exponential_and_json_like_float_errors(self):
        for value in ("NaN", "Infinity", "1e4", "nan", "-2"):
            with self.subTest(value=value):
                self.change_candidate(25, "cash", value)
                with self.assertRaises(MonitorDataError):
                    self.compare()

    def test_reject_boolean_and_unknown_execution_category(self):
        for column, value in (
            ("bearish_cross", "True"),
            ("buy_eligible", ""),
            ("signal_action", "SHORT"),
            ("fill_action", "PROPOSED"),
        ):
            with self.subTest(column=column):
                self.change_candidate(25, column, value)
                with self.assertRaises(MonitorDataError):
                    self.compare()

    def test_reject_inconsistent_fill_or_peak_missingness(self):
        for index, field, value in (
            (10, "fill_price", ""),
            (12, "fill_action", "BUY"),
            (12, "highest_held_close", ""),
            (2, "highest_held_close", "100"),
        ):
            with self.subTest(field=field, index=index):
                self.change_candidate(index, field, value)
                with self.assertRaises(MonitorDataError):
                    self.compare()

    def test_reject_same_reference_and_candidate_path(self):
        with self.assertRaisesRegex(MonitorDataError, "separate local files"):
            compare_local_traces(
                self.reference, self.reference, self.manifest, self.sessions
            )

    def test_cli_match_has_research_only_flags_and_safe_report(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main([
                "--reference", str(self.reference),
                "--candidate", str(self.candidate),
                "--manifest", str(self.manifest),
                "--sessions", str(self.sessions),
                "--report", str(self.report),
            ])
        self.assertEqual(code, 0)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "TRACE_MATCH_UNATTESTED")
        self.assertFalse(report["original_simulator_parity_verified"])
        self.assertEqual(report, json.loads(stdout.getvalue()))

    def test_cli_mismatch_exit_two_reports_only_field_and_date(self):
        self.change_candidate(25, "dividend_payment", "7.25")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main([
                "--reference", str(self.reference),
                "--candidate", str(self.candidate),
                "--manifest", str(self.manifest),
                "--sessions", str(self.sessions),
            ])
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "TRACE_MISMATCH_NO_PARITY_CLAIM")
        self.assertEqual(payload["first_differences"][0]["field"], "dividend_payment")
        self.assertNotIn("7.25", stdout.getvalue())

    def test_cli_malformed_or_missing_input_fails_closed(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = main([
                "--reference", str(self.reference),
                "--candidate", "missing.csv",
                "--manifest", str(self.manifest),
                "--sessions", str(self.sessions),
            ])
        self.assertEqual(code, 2)
        self.assertIn("NO_HISTORICAL_PARITY_ESTABLISHED", error.getvalue())

    def test_cli_report_never_overwrites_and_never_replaces_inputs(self):
        args = [
            "--reference", str(self.reference), "--candidate", str(self.candidate),
            "--manifest", str(self.manifest), "--sessions", str(self.sessions),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args + ["--report", str(self.report)]), 0)
        original = self.report.read_bytes()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args + ["--report", str(self.report)]), 2)
            self.assertEqual(main(args + ["--report", str(self.reference)]), 2)
        self.assertEqual(self.report.read_bytes(), original)
        self.assertIn("NO_HISTORICAL_PARITY_ESTABLISHED", err.getvalue())


if __name__ == "__main__":
    unittest.main()
