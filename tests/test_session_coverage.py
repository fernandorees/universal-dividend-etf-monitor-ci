"""Synthetic-only tests: expected-session manifest is not an attested exchange calendar."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from schd_monitor.cli import main
from schd_monitor.data import expected_sessions_from_csv
from schd_monitor.models import Bar, MonitorDataError, validate_expected_sessions


def synthetic_bars(count=90):
    rows = []
    day = date(2024, 1, 2)
    while len(rows) < count:
        if day.weekday() < 5:
            close = 100 + len(rows) * .1
            rows.append(Bar(day, close, close + .5, close - .5, close))
        day += timedelta(days=1)
    return rows


def write_bars(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close"])
        for row in rows:
            w.writerow([row.session, row.open, row.high, row.low, row.close])


def write_sessions(path, sessions):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date"])
        for session in sessions:
            w.writerow([session])


class TestExpectedSessionCoverage(unittest.TestCase):
    def setUp(self):
        self.bars = synthetic_bars()
        self.sessions = [bar.session for bar in self.bars]

    def test_complete_manifest_succeeds(self):
        validate_expected_sessions(self.bars, self.sessions)

    def test_explicit_holiday_in_manifest_is_allowed(self):
        # Excluding a weekday is valid when the supplied manifest also excludes it.
        # This is a synthetic demonstration, not a claim about a real exchange holiday.
        with_holiday = self.bars[:25] + self.bars[26:]
        validate_expected_sessions(with_holiday, [bar.session for bar in with_holiday])

    def test_missing_internal_trading_session_is_rejected(self):
        missing = self.sessions[30]
        partial = self.bars[:30] + self.bars[31:]
        with self.assertRaisesRegex(MonitorDataError, "missing expected sessions=1"):
            validate_expected_sessions(partial, self.sessions)
        with self.assertRaisesRegex(MonitorDataError, missing.isoformat()):
            validate_expected_sessions(partial, self.sessions)

    def test_missing_latest_trading_session_is_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "missing expected sessions=1"):
            validate_expected_sessions(self.bars[:-1], self.sessions)

    def test_unexpected_holiday_bar_is_rejected(self):
        calendar = self.sessions[:25] + self.sessions[26:]
        with self.assertRaisesRegex(MonitorDataError, "unexpected bar sessions=1"):
            validate_expected_sessions(self.bars, calendar)

    def test_malformed_manifest_is_rejected(self):
        for bad in (
            [],
            self.sessions[:30] + [self.sessions[29]] + self.sessions[30:],
            [self.sessions[1], self.sessions[0]] + self.sessions[2:],
            [date(2024, 1, 6)] + self.sessions,
            ["2024-01-02"] + self.sessions[1:],
        ):
            with self.subTest(bad=repr(bad[:2])):
                with self.assertRaises(MonitorDataError):
                    validate_expected_sessions(self.bars, bad)

    def test_csv_manifest_parsing_and_invalid_dates(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "expected.csv"
            write_sessions(p, self.sessions)
            self.assertEqual(expected_sessions_from_csv(p), self.sessions)
            p.write_text("date\n2024-13-99\n", encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "Invalid expected-session date"):
                expected_sessions_from_csv(p)
            p.write_text("session\n2024-01-02\n", encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "exactly one column"):
                expected_sessions_from_csv(p)
            p.write_text("date\n2024-01-02,extra-column\n", encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "Malformed expected-session"):
                expected_sessions_from_csv(p)
            p.write_text("date\n20240102\n", encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "YYYY-MM-DD"):
                expected_sessions_from_csv(p)

    def test_cli_rejects_missing_session_without_export(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            csv_path, calendar_path = folder / "bars.csv", folder / "sessions.csv"
            pos_path, output_path = folder / "position.json", folder / "signal.json"
            write_bars(csv_path, self.bars[:-1])
            write_sessions(calendar_path, self.sessions)
            pos_path.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": self.bars[-2].session.isoformat()
            }), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                exit_code = main(["--csv", str(csv_path), "--position", str(pos_path),
                                  "--expected-sessions", str(calendar_path),
                                  "--output", str(output_path)])
            self.assertEqual(exit_code, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", err.getvalue())
            self.assertIn("Session coverage mismatch", err.getvalue())
            self.assertFalse(output_path.exists())

    def test_cli_with_complete_manifest_remains_unverified(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            csv_path, calendar_path = folder / "bars.csv", folder / "sessions.csv"
            pos_path = folder / "position.json"
            write_bars(csv_path, self.bars)
            write_sessions(calendar_path, self.sessions)
            pos_path.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": self.bars[-1].session.isoformat()
            }), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--csv", str(csv_path), "--position", str(pos_path),
                                  "--expected-sessions", str(calendar_path)])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["safety"], "RESEARCH_ONLY_UNVERIFIED")
            self.assertEqual(payload["result"]["verification"], "UNVERIFIED_DO_NOT_TRADE")


if __name__ == "__main__":
    unittest.main()
