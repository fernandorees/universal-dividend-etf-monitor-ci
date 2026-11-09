"""Offline-only synthetic regression tests for UNATTESTED two-feed consistency."""
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
from schd_monitor.feed_integrity import (
    FeedDeclaration, compare_offline_feeds, feed_declaration_from_json,
)
from schd_monitor.models import Bar, MonitorDataError


def history(n=90):
    rows = []
    day = date(2024, 1, 2)
    while len(rows) < n:
        if day.weekday() < 5:
            close = 100.0 + len(rows) * 0.03
            rows.append(Bar(day, close, close + .5, close - .5, close))
        day += timedelta(days=1)
    return rows


def declaration(bars, provider="FICTIONAL_A", symbol="SCHD", adjustment="SPLIT_ONLY",
                through=None):
    return FeedDeclaration(
        provider=provider, symbol=symbol, adjustment=adjustment,
        last_complete_session=through if through is not None else bars[-1].session
    )


def compare(a, b, **kwargs):
    return compare_offline_feeds(
        a, b,
        kwargs.get("first", declaration(a)),
        kwargs.get("second", declaration(b, provider="FICTIONAL_B")),
        expected_symbol=kwargs.get("symbol", "SCHD")
    )


def write_bars(path, bars):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close"])
        for item in bars:
            writer.writerow([item.session.isoformat(), item.open, item.high, item.low, item.close])


def write_manifest(path, rows, provider):
    path.write_text(json.dumps({
        "schema_version": 1,
        "provider": provider,
        "symbol": "SCHD",
        "adjustment": "SPLIT_ONLY",
        "last_complete_session": rows[-1].session.isoformat(),
    }), encoding="utf-8")


class TestOfflineFeedIntegrity(unittest.TestCase):
    def setUp(self):
        self.bars = history()

    def test_identical_series_produce_unattested_audit_hashes(self):
        report = compare(self.bars, list(self.bars))
        self.assertEqual(report.status, "CONSISTENT_UNATTESTED")
        self.assertEqual(report.sessions_compared, 90)
        self.assertEqual(report.last_session, self.bars[-1].session.isoformat())
        self.assertEqual(report.primary_bars_sha256, report.secondary_bars_sha256)
        self.assertEqual(len(report.primary_bars_sha256), 64)
        self.assertEqual(report.max_relative_ohlc_difference, 0)
        self.assertEqual(report.as_json()["schema_version"], 1)

    def test_small_difference_allowed_but_still_unattested(self):
        secondary = list(self.bars)
        secondary[35] = replace(secondary[35], close=secondary[35].close + .002)
        report = compare(self.bars, secondary)
        self.assertEqual(report.status, "CONSISTENT_UNATTESTED")
        self.assertNotEqual(report.primary_bars_sha256, report.secondary_bars_sha256)
        self.assertGreater(report.max_relative_ohlc_difference, 0)

    def test_reject_large_discrepancy_in_every_ohlc_field(self):
        for name in ("open", "high", "low", "close"):
            secondary = list(self.bars)
            row = secondary[45]
            if name == "high":
                changed = row.high + .05
            elif name == "low":
                changed = row.low - .05
            else:
                changed = getattr(row, name) + .05
            secondary[45] = replace(row, **{name: changed})
            with self.subTest(field=name):
                with self.assertRaisesRegex(MonitorDataError, "field=" + name):
                    compare(self.bars, secondary)

    def test_reject_missing_internal_session_even_when_latest_matches(self):
        secondary = self.bars[:30] + self.bars[31:]
        with self.assertRaisesRegex(MonitorDataError, "session history mismatch"):
            compare(self.bars, secondary)

    def test_reject_stale_secondary_latest_session(self):
        secondary = self.bars[:-1]
        with self.assertRaisesRegex(MonitorDataError, "session history mismatch"):
            compare(self.bars, secondary)

    def test_reject_incorrect_declared_completion_dates(self):
        with self.assertRaisesRegex(MonitorDataError, "Primary claimed completed-through"):
            compare(self.bars, self.bars,
                    first=declaration(self.bars, through=self.bars[-2].session))
        with self.assertRaisesRegex(MonitorDataError, "Secondary claimed completed-through"):
            compare(self.bars, self.bars,
                    second=declaration(self.bars, provider="FICTIONAL_B",
                                       through=self.bars[-2].session))

    def test_reject_same_provider_even_with_case_difference(self):
        with self.assertRaisesRegex(MonitorDataError, "Distinct claimed providers"):
            compare(self.bars, self.bars,
                    second=declaration(self.bars, provider="fictional_a"))

    def test_reject_mismatched_declared_symbol(self):
        with self.assertRaisesRegex(MonitorDataError, "symbol does not match"):
            compare(self.bars, self.bars,
                    second=declaration(self.bars, provider="FICTIONAL_B", symbol="VYM"))
        with self.assertRaisesRegex(MonitorDataError, "symbol does not match"):
            compare(self.bars, self.bars, symbol="VYM")

    def test_reject_raw_dividend_adjusted_or_unknown_basis(self):
        for basis in ("RAW", "DIVIDEND_ADJUSTED", "", "SPLIT_AND_DIVIDEND"):
            with self.subTest(adjustment=basis):
                with self.assertRaisesRegex(MonitorDataError, "SPLIT_ONLY"):
                    declaration(self.bars, adjustment=basis)

    def test_reject_bad_version_and_bad_provider(self):
        for kwargs in ({"schema_version": 2}, {"schema_version": True},
                       {"provider": ""}, {"provider": " A "}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(MonitorDataError):
                    FeedDeclaration(
                        provider="A", symbol="SCHD", adjustment="SPLIT_ONLY",
                        last_complete_session=self.bars[-1].session, **kwargs
                    ) if "provider" not in kwargs else FeedDeclaration(
                        provider=kwargs["provider"], symbol="SCHD", adjustment="SPLIT_ONLY",
                        last_complete_session=self.bars[-1].session
                    )

    def test_provider_identifiers_are_bounded_and_log_safe(self):
        # Untrusted claimed source labels must not inject logs/paths or huge
        # strings into local audit metadata. This does not authenticate feeds.
        for bad in (
            None, True, 123, "", " A ", "A B", "A\nB", "A\rB",
            "A\tB", "A\x00B", "A/SECRET", "A@EXAMPLE", "Á", "A" * 65,
        ):
            with self.subTest(provider=repr(bad)):
                with self.assertRaisesRegex(MonitorDataError, "Feed provider"):
                    declaration(self.bars, provider=bad)
        for valid in ("A", "FICTIONAL_A", "massive-v2.1", "SOURCE_09"):
            with self.subTest(valid=valid):
                self.assertEqual(declaration(self.bars, provider=valid).provider, valid)

    def test_manifest_rejects_unsafe_provider_before_feed_comparison(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "feed.json"
            for provider in ("A\nFAKE_PROVIDER", "A/../../SECRET", "A" * 65):
                with self.subTest(provider=repr(provider)):
                    write_manifest(manifest, self.bars, provider)
                    with self.assertRaisesRegex(MonitorDataError, "Feed provider"):
                        feed_declaration_from_json(manifest)

    def test_manifest_rejects_malformed_unknown_and_noncanonical_dates(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "manifest.json"
            write_manifest(manifest, self.bars, "FICTIONAL_A")
            self.assertEqual(feed_declaration_from_json(manifest).symbol, "SCHD")
            p = json.loads(manifest.read_text(encoding="utf-8"))
            for change in (
                {"last_complete_session": "20240401"},
                {"last_complete_session": "2024-15-10"},
                {"last_complete_session": None},
                {"schema_version": 2},
                {"adjustment": "RAW"},
            ):
                with self.subTest(change=change):
                    manifest.write_text(json.dumps({**p, **change}), encoding="utf-8")
                    with self.assertRaises(MonitorDataError):
                        feed_declaration_from_json(manifest)
            manifest.write_text(json.dumps({**p, "unexpected": True}), encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "exactly the versioned"):
                feed_declaration_from_json(manifest)
            manifest.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "Invalid feed declaration JSON"):
                feed_declaration_from_json(manifest)

    def test_manifest_rejects_duplicate_json_keys_even_when_last_value_is_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "feed.json"
            write_manifest(manifest, self.bars, "FICTIONAL_A")
            original = manifest.read_text(encoding="utf-8")
            for target, replacement in (
                ('"provider": "FICTIONAL_A"',
                 '"provider": "FORGED", "provider": "FICTIONAL_A"'),
                ('"adjustment": "SPLIT_ONLY"',
                 '"adjustment": "RAW", "adjustment": "SPLIT_ONLY"'),
                ('"schema_version": 1',
                 '"schema_version": 1, "schema_version": 1'),
                ('"last_complete_session": "' + self.bars[-1].session.isoformat() + '"',
                 '"last_complete_session": "2000-01-01", "last_complete_session": "'
                 + self.bars[-1].session.isoformat() + '"'),
            ):
                with self.subTest(field=target):
                    self.assertIn(target, original)
                    manifest.write_text(original.replace(target, replacement), encoding="utf-8")
                    with self.assertRaisesRegex(MonitorDataError, "Duplicate feed declaration JSON"):
                        feed_declaration_from_json(manifest)

    def test_cli_duplicate_provider_field_fails_without_signal_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "a.csv", root / "b.csv"
            primary_meta, secondary_meta = root / "a.json", root / "b.json"
            position, output = root / "position.json", root / "signal.json"
            write_bars(first, self.bars)
            write_bars(second, self.bars)
            write_manifest(primary_meta, self.bars, "FICTIONAL_A")
            write_manifest(secondary_meta, self.bars, "FICTIONAL_B")
            raw = primary_meta.read_text(encoding="utf-8")
            self.assertIn('"provider": "FICTIONAL_A"', raw)
            primary_meta.write_text(
                raw.replace('"provider": "FICTIONAL_A"',
                            '"provider": "FORGED", "provider": "FICTIONAL_A"'),
                encoding="utf-8",
            )
            position.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": self.bars[-1].session.isoformat(),
            }), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["--csv", str(first), "--position", str(position),
                               "--secondary-csv", str(second),
                               "--primary-feed-declaration", str(primary_meta),
                               "--secondary-feed-declaration", str(secondary_meta),
                               "--output", str(output)])
            self.assertEqual(result, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", stderr.getvalue())
            self.assertIn("Duplicate feed declaration JSON", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_rejects_partial_dual_source_flags(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bars = root / "bars.csv"
            position = root / "position.json"
            write_bars(bars, self.bars)
            position.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": self.bars[-1].session.isoformat(),
            }), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["--csv", str(bars), "--position", str(position),
                               "--secondary-csv", str(bars)])
            self.assertEqual(result, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", stderr.getvalue())
            self.assertIn("both feed declarations", stderr.getvalue())

    def test_cli_discrepant_second_feed_fails_without_writing_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "a.csv", root / "b.csv"
            first_m, second_m = root / "a.json", root / "b.json"
            position, output = root / "position.json", root / "result.json"
            write_bars(first, self.bars)
            secondary = list(self.bars)
            secondary[-1] = replace(secondary[-1], close=secondary[-1].close + .05)
            write_bars(second, secondary)
            write_manifest(first_m, self.bars, "FICTIONAL_A")
            write_manifest(second_m, secondary, "FICTIONAL_B")
            position.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": self.bars[-1].session.isoformat(),
            }), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["--csv", str(first), "--position", str(position),
                               "--secondary-csv", str(second),
                               "--primary-feed-declaration", str(first_m),
                               "--secondary-feed-declaration", str(second_m),
                               "--output", str(output)])
            self.assertEqual(result, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", stderr.getvalue())
            self.assertIn("Feed OHLC mismatch", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_consistent_feeds_include_audit_but_stay_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "a.csv", root / "b.csv"
            first_m, second_m = root / "a.json", root / "b.json"
            position = root / "position.json"
            write_bars(first, self.bars)
            write_bars(second, self.bars)
            write_manifest(first_m, self.bars, "FICTIONAL_A")
            write_manifest(second_m, self.bars, "FICTIONAL_B")
            position.write_text(json.dumps({
                "symbol": "SCHD", "shares": 0, "available_cash": 2000,
                "peak_close": None, "reconciled_through": self.bars[-1].session.isoformat(),
            }), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(["--csv", str(first), "--position", str(position),
                               "--secondary-csv", str(second),
                               "--primary-feed-declaration", str(first_m),
                               "--secondary-feed-declaration", str(second_m)])
            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["result"]["verification"], "UNVERIFIED_DO_NOT_TRADE")
            self.assertEqual(payload["safety"], "RESEARCH_ONLY_UNVERIFIED")
            self.assertEqual(payload["feed_comparison"]["status"], "CONSISTENT_UNATTESTED")


if __name__ == "__main__":
    unittest.main()
