"""Synthetic-only P01 split normalization tests; NO real corporate-action prices."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from schd_monitor.cli import main
from schd_monitor.models import Bar, MonitorDataError
from schd_monitor.split_audit import (
    SplitEvent, SplitLedger, audit_split_only_bars, split_ledger_from_json,
)


def dates(n=90):
    result = []
    d = date(2024, 1, 2)
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d)
        d += timedelta(days=1)
    return result


DAYS = dates()
SPLIT_DAY = DAYS[30]
REVERSE_DAY = DAYS[65]


def bars_with_prices(prices):
    return [Bar(day, price, price + .6, price - .6, price)
            for day, price in zip(DAYS, prices)]


def raw_three_for_one():
    return bars_with_prices([90.0 if i < 30 else 30.0 for i in range(len(DAYS))])


def split_adjusted(raw, events):
    # Explicit synthetic fixture: divide every pre-effective-session OHLC
    # by the product of later share-multiplication ratios.
    out = []
    for bar in raw:
        factor = 1.0
        for event in events:
            if bar.session < event.effective_session:
                factor *= event.new_shares / event.old_shares
        out.append(Bar(bar.session, bar.open / factor, bar.high / factor,
                       bar.low / factor, bar.close / factor))
    return out


def ledger(*events, symbol="SCHD"):
    return SplitLedger(symbol, tuple(events))


def write_bars(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close"])
        for row in rows:
            writer.writerow([row.session, row.open, row.high, row.low, row.close])


def write_ledger(path, events):
    path.write_text(json.dumps({
        "schema_version": 1,
        "symbol": "SCHD",
        "events": [
            {"effective_session": event.effective_session.isoformat(),
             "new_shares": event.new_shares, "old_shares": event.old_shares}
            for event in events
        ],
    }), encoding="utf-8")


class TestOfflineSplitAudit(unittest.TestCase):
    def setUp(self):
        self.split = SplitEvent(SPLIT_DAY, 3, 1)
        self.raw = raw_three_for_one()
        self.adjusted = split_adjusted(self.raw, (self.split,))
        self.ledger = ledger(self.split)

    def test_three_for_one_correctly_adjusts_only_prior_sessions(self):
        report = audit_split_only_bars(self.raw, self.adjusted, self.ledger, expected_symbol="SCHD")
        self.assertEqual(report.status, "CONSISTENT_UNATTESTED")
        self.assertEqual(report.sessions_compared, 90)
        self.assertEqual(report.split_events_applied, 1)
        self.assertEqual(report.largest_relative_difference, 0)
        self.assertEqual(len(report.split_ledger_sha256), 64)
        self.assertEqual(report.as_json()["schema_version"], 1)
        self.assertEqual(self.adjusted[29].close, 30.0)
        self.assertEqual(self.adjusted[30].close, 30.0)

    def test_reverse_split_one_for_five(self):
        event = SplitEvent(SPLIT_DAY, 1, 5)
        raw = bars_with_prices([30.0 if i < 30 else 150.0 for i in range(90)])
        adjusted = split_adjusted(raw, (event,))
        report = audit_split_only_bars(raw, adjusted, ledger(event), expected_symbol="SCHD")
        self.assertEqual(report.split_events_applied, 1)
        self.assertEqual(adjusted[29].close, 150.0)
        self.assertEqual(adjusted[30].close, 150.0)

    def test_multiple_compounded_forward_and_reverse_splits(self):
        reverse = SplitEvent(REVERSE_DAY, 1, 5)
        raw = bars_with_prices([90.0 if i < 30 else 30.0 if i < 65 else 150.0
                               for i in range(90)])
        adjusted = split_adjusted(raw, (self.split, reverse))
        report = audit_split_only_bars(raw, adjusted, ledger(self.split, reverse),
                                       expected_symbol="SCHD")
        self.assertEqual(report.split_events_applied, 2)
        self.assertEqual([round(adjusted[i].close, 5) for i in (29, 30, 64, 65)],
                         [150.0] * 4)

    def test_explicit_no_split_history_is_supported_but_unverified(self):
        raw = bars_with_prices([100.0 + i * .03 for i in range(90)])
        report = audit_split_only_bars(raw, raw, ledger(), expected_symbol="SCHD")
        self.assertEqual(report.split_events_applied, 0)
        self.assertEqual(report.status, "CONSISTENT_UNATTESTED")

    def test_reject_missing_split_in_declared_ledger(self):
        with self.assertRaisesRegex(MonitorDataError, "Split-only OHLC mismatch"):
            audit_split_only_bars(self.raw, self.adjusted, ledger(), expected_symbol="SCHD")

    def test_reject_wrong_split_factor(self):
        with self.assertRaisesRegex(MonitorDataError, "Split-only OHLC mismatch"):
            audit_split_only_bars(self.raw, self.adjusted,
                                  ledger(SplitEvent(SPLIT_DAY, 2, 1)), expected_symbol="SCHD")

    def test_reject_unadjusted_ohlc_as_split_adjusted(self):
        with self.assertRaisesRegex(MonitorDataError, "Split-only OHLC mismatch"):
            audit_split_only_bars(self.raw, self.raw, self.ledger, expected_symbol="SCHD")

    def test_reject_dividend_adjusted_close(self):
        adjusted = list(self.adjusted)
        bar = adjusted[20]
        adjusted[20] = replace(bar, close=bar.close - .10)  # valid OHLC; wrong adjustment basis
        with self.assertRaisesRegex(MonitorDataError, "field=close"):
            audit_split_only_bars(self.raw, adjusted, self.ledger, expected_symbol="SCHD")

    def test_reject_missing_or_extra_raw_sessions(self):
        with self.assertRaisesRegex(MonitorDataError, "session histories differ"):
            audit_split_only_bars(self.raw[:-1], self.adjusted,
                                  self.ledger, expected_symbol="SCHD")
        with self.assertRaisesRegex(MonitorDataError, "session histories differ"):
            audit_split_only_bars(self.raw[:35] + self.raw[36:], self.adjusted,
                                  self.ledger, expected_symbol="SCHD")

    def test_reject_event_date_outside_or_not_present_in_history(self):
        missing_date = DAYS[30] + timedelta(days=4)  # Saturday in synthetic weekday series
        self.assertNotIn(missing_date, DAYS)
        for event in (SplitEvent(DAYS[0], 3, 1),
                      SplitEvent(DAYS[-1] + timedelta(days=10), 3, 1),
                      SplitEvent(missing_date, 3, 1)):
            with self.subTest(event=event):
                with self.assertRaisesRegex(MonitorDataError, "effective session"):
                    audit_split_only_bars(self.raw, self.adjusted, ledger(event),
                                          expected_symbol="SCHD")

    def test_reject_invalid_ratio_and_duplicate_or_reordered_events(self):
        for new, old in ((0, 1), (-1, 1), (3, 0), (1, 1), (True, 1),
                         (1, False), (3.0, 1), (float("inf"), 1)):
            with self.subTest(new=new, old=old):
                with self.assertRaises(MonitorDataError):
                    SplitEvent(SPLIT_DAY, new, old)
        for events in ((self.split, self.split),
                       (SplitEvent(REVERSE_DAY, 1, 5), self.split)):
            with self.assertRaisesRegex(MonitorDataError, "strictly ordered"):
                ledger(*events)

    def test_reject_mismatched_symbol(self):
        with self.assertRaisesRegex(MonitorDataError, "does not match"):
            audit_split_only_bars(self.raw, self.adjusted,
                                  ledger(self.split, symbol="VYM"), expected_symbol="SCHD")

    def test_parse_versioned_json_and_reject_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "split.json"
            write_ledger(path, (self.split,))
            self.assertEqual(split_ledger_from_json(path), self.ledger)
            base = json.loads(path.read_text(encoding="utf-8"))
            for change in (
                {"schema_version": 2},
                {"schema_version": True},
                {"events": {"one": True}},
                {"events": [{"effective_session": "20240412", "new_shares": 3, "old_shares": 1}]},
                {"events": [{"effective_session": "2024-13-99", "new_shares": 3, "old_shares": 1}]},
                {"events": [{"effective_session": self.split.effective_session.isoformat(),
                             "new_shares": 3.0, "old_shares": 1}]},
                {"symbol": "schd"},
            ):
                with self.subTest(change=change):
                    path.write_text(json.dumps({**base, **change}), encoding="utf-8")
                    with self.assertRaises(MonitorDataError):
                        split_ledger_from_json(path)
            path.write_text(json.dumps({**base, "unknown": 1}), encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "exactly"):
                split_ledger_from_json(path)
            path.write_text("{bad JSON", encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "Invalid split-ledger JSON"):
                split_ledger_from_json(path)

    def test_cli_refuses_partial_flags(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            adjusted_path, pos = root / "adjusted.csv", root / "position.json"
            write_bars(adjusted_path, self.adjusted)
            pos.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": DAYS[-1].isoformat()
            }), encoding="utf-8")
            for flag in (["--raw-csv", "raw.csv"], ["--split-ledger", "split.json"]):
                with self.subTest(flag=flag):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        exit_code = main(["--csv", str(adjusted_path), "--position", str(pos)] + flag)
                    self.assertEqual(exit_code, 2)
                    self.assertIn("NO_VERIFIED_SIGNAL", stderr.getvalue())
                    self.assertIn("requires both", stderr.getvalue())

    def test_cli_rejects_split_mismatch_without_signal_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw, adjusted = root / "raw.csv", root / "adjusted.csv"
            pos, events, output = root / "position.json", root / "events.json", root / "signal.json"
            write_bars(raw, self.raw)
            write_bars(adjusted, self.raw)  # wrong: unadjusted supplied as adjusted
            write_ledger(events, (self.split,))
            pos.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": DAYS[-1].isoformat()
            }), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(["--csv", str(adjusted), "--position", str(pos),
                             "--raw-csv", str(raw), "--split-ledger", str(events),
                             "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", stderr.getvalue())
            self.assertIn("Split-only OHLC mismatch", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_correct_split_report_remains_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw, adjusted = root / "raw.csv", root / "adjusted.csv"
            pos, events = root / "position.json", root / "events.json"
            write_bars(raw, self.raw)
            write_bars(adjusted, self.adjusted)
            write_ledger(events, (self.split,))
            pos.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": DAYS[-1].isoformat()
            }), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--csv", str(adjusted), "--position", str(pos),
                             "--raw-csv", str(raw), "--split-ledger", str(events)])
            self.assertEqual(code, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["result"]["verification"], "UNVERIFIED_DO_NOT_TRADE")
            self.assertEqual(result["split_audit"]["status"], "CONSISTENT_UNATTESTED")
            self.assertEqual(result["split_audit"]["split_events_applied"], 1)


if __name__ == "__main__":
    unittest.main()
