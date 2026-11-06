"""Offline XNYS trading-session tests with fictional OHLC data only."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from schd_monitor.cli import main
from schd_monitor.exchange_calendar import completed_xnys_sessions, xnys_sessions_within_inclusive_dates
from schd_monitor.models import Bar, MonitorDataError, validate_expected_sessions


UTC = timezone.utc


def utc_time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def fictional_bars(sessions: list[date]) -> list[Bar]:
    return [
        Bar(day, 100.0 + index * .02, 100.5 + index * .02,
            99.5 + index * .02, 100.0 + index * .02)
        for index, day in enumerate(sessions)
    ]


def write_bars(path: Path, bars: list[Bar]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close"])
        for bar in bars:
            writer.writerow([bar.session, bar.open, bar.high, bar.low, bar.close])


def write_position(path: Path, through: date) -> None:
    path.write_text(json.dumps({
        "symbol": "SCHD",
        "shares": 0,
        "available_cash": 2000,
        "peak_close": None,
        "reconciled_through": through.isoformat(),
    }), encoding="utf-8")


class TestXnysCompletedSessions(unittest.TestCase):
    def test_inclusive_request_calendar_maps_holiday_and_weekend_bounds(self):
        expected = completed_xnys_sessions(
            date(2024, 1, 2), utc_time("2024-05-11T12:00:00+00:00")
        )
        self.assertEqual(
            xnys_sessions_within_inclusive_dates(
                date(2024, 1, 1), date(2024, 5, 11)
            ), expected
        )
        self.assertEqual(
            xnys_sessions_within_inclusive_dates(
                date(2024, 5, 10), date(2024, 5, 12)
            ), [date(2024, 5, 10)]
        )

    def test_inclusive_request_calendar_refuses_invalid_or_empty_range(self):
        for start, end in (
            (date(2024, 1, 1), date(2024, 1, 1)),
            (date(2024, 5, 11), date(2024, 5, 12)),
            (date(2024, 5, 13), date(2024, 5, 12)),
            ("2024-05-10", date(2024, 5, 11)),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(MonitorDataError):
                    xnys_sessions_within_inclusive_dates(start, end)

    def test_thanksgiving_holiday_and_early_close_boundary(self):
        first = date(2024, 11, 25)
        before = completed_xnys_sessions(first, utc_time("2024-11-29T18:29:59+00:00"))
        at_buffer = completed_xnys_sessions(first, utc_time("2024-11-29T18:30:00+00:00"))
        self.assertEqual(before, [date(2024, 11, 25), date(2024, 11, 26),
                                  date(2024, 11, 27)])
        self.assertEqual(at_buffer, before + [date(2024, 11, 29)])
        self.assertNotIn(date(2024, 11, 28), at_buffer)

    def test_independence_day_early_close_and_holiday(self):
        first = date(2024, 7, 2)
        before = completed_xnys_sessions(first, utc_time("2024-07-03T17:29:00+00:00"))
        after = completed_xnys_sessions(first, utc_time("2024-07-05T14:00:00+00:00"))
        self.assertEqual(before, [date(2024, 7, 2)])
        self.assertEqual(after, [date(2024, 7, 2), date(2024, 7, 3)])
        self.assertNotIn(date(2024, 7, 4), after)

    def test_new_york_daylight_saving_change(self):
        first = date(2024, 3, 8)
        friday = completed_xnys_sessions(first, utc_time("2024-03-08T21:30:00+00:00"))
        before_monday = completed_xnys_sessions(first, utc_time("2024-03-11T20:29:59+00:00"))
        monday = completed_xnys_sessions(first, utc_time("2024-03-11T20:30:00+00:00"))
        self.assertEqual(friday, [date(2024, 3, 8)])
        self.assertEqual(before_monday, friday)
        self.assertEqual(monday, friday + [date(2024, 3, 11)])

    def test_2026_spring_dst_exact_close_buffer_boundary(self):
        # US Eastern changes UTC offset on March 8, 2026.
        # March 6 Friday regular close+30m = 21:30 UTC;
        # March 9 Monday regular close+30m = 20:30 UTC.
        first = date(2026, 3, 6)
        friday = completed_xnys_sessions(
            first, utc_time("2026-03-06T21:30:00+00:00"))
        before = completed_xnys_sessions(
            first, utc_time("2026-03-09T20:29:59+00:00"))
        at_boundary = completed_xnys_sessions(
            first, utc_time("2026-03-09T20:30:00+00:00"))
        self.assertEqual(friday, [first])
        self.assertEqual(before, friday)
        self.assertEqual(at_boundary, [first, date(2026, 3, 9)])

    def test_2026_fall_dst_exact_close_buffer_boundary(self):
        # US Eastern returns to standard time November 1, 2026.
        # Oct 30 Friday regular close+30m = 20:30 UTC;
        # Nov 2 Monday regular close+30m = 21:30 UTC.
        first = date(2026, 10, 30)
        friday = completed_xnys_sessions(
            first, utc_time("2026-10-30T20:30:00+00:00"))
        before = completed_xnys_sessions(
            first, utc_time("2026-11-02T21:29:59+00:00"))
        at_boundary = completed_xnys_sessions(
            first, utc_time("2026-11-02T21:30:00+00:00"))
        self.assertEqual(friday, [first])
        self.assertEqual(before, friday)
        self.assertEqual(at_boundary, [first, date(2026, 11, 2)])

    def test_2026_dubai_and_utc_boundary_invariance_both_dst_changes(self):
        # Same instant: Dubai is always UTC+04, NY changes -05/-04.
        cases = (
            (date(2026, 3, 6), "2026-03-09T20:29:59+00:00",
             "2026-03-10T00:29:59+04:00", [date(2026, 3, 6)]),
            (date(2026, 3, 6), "2026-03-09T20:30:00+00:00",
             "2026-03-10T00:30:00+04:00",
             [date(2026, 3, 6), date(2026, 3, 9)]),
            (date(2026, 10, 30), "2026-11-02T21:29:59+00:00",
             "2026-11-03T01:29:59+04:00", [date(2026, 10, 30)]),
            (date(2026, 10, 30), "2026-11-02T21:30:00+00:00",
             "2026-11-03T01:30:00+04:00",
             [date(2026, 10, 30), date(2026, 11, 2)]),
        )
        for start, utc, dubai, expected in cases:
            with self.subTest(utc=utc, dubai=dubai):
                utc_result = completed_xnys_sessions(start, utc_time(utc))
                dubai_result = completed_xnys_sessions(start, utc_time(dubai))
                self.assertEqual(utc_result, expected)
                self.assertEqual(dubai_result, expected)

    def test_dubai_offset_does_not_change_exchange_session_date(self):
        first = date(2024, 3, 8)
        ny_session_from_dubai = completed_xnys_sessions(
            first, utc_time("2024-03-12T00:30:00+04:00")
        )
        self.assertEqual(ny_session_from_dubai, [first, date(2024, 3, 11)])

    def test_single_session_and_preclose_fail_closed(self):
        day = date(2024, 3, 8)
        with self.assertRaisesRegex(MonitorDataError, "no completed sessions"):
            completed_xnys_sessions(day, utc_time("2024-03-08T21:29:59+00:00"))
        self.assertEqual(
            completed_xnys_sessions(day, utc_time("2024-03-08T21:30:00+00:00")),
            [day]
        )

    def test_invalid_first_day_and_timestamp_fail_closed(self):
        day = date(2024, 11, 29)
        for wrong in (None, "2024-11-29", datetime(2024, 11, 29, 18, 40)):
            with self.subTest(wrong=repr(wrong)):
                with self.assertRaisesRegex(MonitorDataError, "timezone-aware"):
                    completed_xnys_sessions(day, wrong)
        with self.assertRaises(MonitorDataError):
            completed_xnys_sessions(date(2024, 11, 28),
                                    utc_time("2024-11-29T18:40:00+00:00"))
        with self.assertRaisesRegex(MonitorDataError, "later than"):
            completed_xnys_sessions(date(2024, 11, 29),
                                    utc_time("2024-11-27T21:00:00+00:00"))
        with self.assertRaisesRegex(MonitorDataError, "must be a date"):
            completed_xnys_sessions("2024-11-29",
                                    utc_time("2024-11-29T18:40:00+00:00"))

    def test_calendar_rejects_synthetic_missing_day(self):
        sessions = completed_xnys_sessions(
            date(2024, 1, 2), utc_time("2024-06-28T20:40:00+00:00")
        )
        self.assertGreater(len(sessions), 65)
        bars = fictional_bars(sessions)
        validate_expected_sessions(bars, sessions)
        lost_session = sessions[40]
        with self.assertRaisesRegex(MonitorDataError, lost_session.isoformat()):
            validate_expected_sessions(bars[:40] + bars[41:], sessions)

    def test_cli_rejects_incomplete_history_and_no_file_written(self):
        asof = "2024-06-28T20:40:00+00:00"
        all_sessions = completed_xnys_sessions(date(2024, 1, 2), utc_time(asof))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bars, position, output = root / "bars.csv", root / "position.json", root / "signal.json"
            # Synthetic history ends one XNYS session early.
            write_bars(bars, fictional_bars(all_sessions[:-1]))
            write_position(position, all_sessions[-2])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(["--csv", str(bars), "--position", str(position),
                                  "--xnys-asof", asof, "--output", str(output)])
            self.assertEqual(exit_code, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", stderr.getvalue())
            self.assertIn("Session coverage mismatch", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_matching_calendar_is_still_research_only(self):
        asof = "2024-06-28T20:40:00+00:00"
        sessions = completed_xnys_sessions(date(2024, 1, 2), utc_time(asof))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bars, position = root / "bars.csv", root / "position.json"
            write_bars(bars, fictional_bars(sessions))
            write_position(position, sessions[-1])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--csv", str(bars), "--position", str(position),
                                  "--xnys-asof", asof])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["safety"], "RESEARCH_ONLY_UNVERIFIED")
            self.assertEqual(payload["result"]["verification"], "UNVERIFIED_DO_NOT_TRADE")

    def test_cli_rejects_naive_asof_and_conflicting_gates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bars, position = root / "bars.csv", root / "position.json"
            sessions = completed_xnys_sessions(
                date(2024, 1, 2), utc_time("2024-06-28T20:40:00+00:00")
            )
            write_bars(bars, fictional_bars(sessions))
            write_position(position, sessions[-1])
            for extra, error in [
                (["--xnys-asof", "2024-06-28T20:40:00"], "timezone-aware"),
                (["--xnys-asof", "2024-06-28T20:40:00+00:00",
                  "--expected-sessions", "unused.csv"], "Choose one calendar gate"),
            ]:
                with self.subTest(extra=extra):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        code = main(["--csv", str(bars), "--position", str(position)] + extra)
                    self.assertEqual(code, 2)
                    self.assertIn("NO_VERIFIED_SIGNAL", stderr.getvalue())
                    self.assertIn(error, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
