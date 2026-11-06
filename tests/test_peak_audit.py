"""P01 peak reconstruction uses fictional prices and self-declared fills only."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from schd_monitor.cli import main
from schd_monitor.manual_ledger import ManualEvent, ManualLedger
from schd_monitor.models import Bar, MonitorDataError, Position
from schd_monitor.peak_audit import audit_held_close_peak
from schd_monitor.split_audit import SplitEvent, SplitLedger


def trading_days(count=90):
    dates = []
    today = date(2024, 1, 2)
    while len(dates) < count:
        if today.weekday() < 5:
            dates.append(today)
        today += timedelta(days=1)
    return dates


DAYS = trading_days()


def prices():
    return [
        Bar(d, 100 + idx * .1, 100.5 + idx * .1,
            99.5 + idx * .1, 100 + idx * .1)
        for idx, d in enumerate(DAYS)
    ]


def event(seq, kind, shares, cash, ix):
    return ManualEvent(
        seq, f"FICTIONAL_{seq}", DAYS[ix], kind,
        Decimal(shares), Decimal(cash), True,
    )


def records():
    return (
        event(1, "BUY_FILL", "4", "-500", 60),
        event(2, "SELL_FILL", "-1", "150", 70),
        event(3, "CASH_CONTRIBUTION", "0", "25", 80),
    )


def ledger(events=None, *, checkpoint_shares="0", checkpoint_cash="1000", checkpoint=40):
    return ManualLedger(
        "SCHD", DAYS[checkpoint], Decimal(checkpoint_shares), Decimal(checkpoint_cash),
        DAYS[-1], True, records() if events is None else tuple(events),
    )


def position(shares=3, cash=675, peak=None):
    if peak is None and shares:
        peak = prices()[-1].close
    return Position("SCHD", shares, cash, peak if shares else None, DAYS[-1])


def splits(*events):
    return SplitLedger("SCHD", tuple(events))


def verify(bars=None, book=None, pos=None, split_ledger=None):
    return audit_held_close_peak(
        prices() if bars is None else bars,
        ledger() if book is None else book,
        position() if pos is None else pos,
        split_ledger=splits() if split_ledger is None else split_ledger,
    )


def write_bars(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close"])
        for row in rows:
            writer.writerow([row.session, row.open, row.high, row.low, row.close])


def write_manual(path, book):
    path.write_text(json.dumps({
        "schema_version": 1,
        "symbol": book.symbol,
        "checkpoint_session": book.checkpoint_session.isoformat(),
        "checkpoint_shares": str(book.checkpoint_shares),
        "checkpoint_cash": str(book.checkpoint_cash),
        "reviewed_through": book.reviewed_through.isoformat(),
        "activity_reviewed": book.activity_reviewed,
        "events": [
            {"sequence": e.sequence, "event_id": e.event_id,
             "session": e.session.isoformat(), "kind": e.kind,
             "shares_delta": str(e.shares_delta), "cash_delta": str(e.cash_delta),
             "manually_confirmed": e.manually_confirmed}
            for e in book.events
        ],
    }), encoding="utf-8")


def fixture_paths(root, *, selected_peak=None):
    adjusted, raw = root / "adjusted.csv", root / "raw.csv"
    jpath, spath, ppath = root / "manual.json", root / "splits.json", root / "position.json"
    write_bars(adjusted, prices())
    write_bars(raw, prices())
    write_manual(jpath, ledger())
    spath.write_text(json.dumps({
        "schema_version": 1, "symbol": "SCHD", "events": []
    }), encoding="utf-8")
    ppath.write_text(json.dumps({
        "symbol": "SCHD", "shares": 3, "available_cash": 675,
        "peak_close": position().peak_close if selected_peak is None else selected_peak,
        "reconciled_through": DAYS[-1].isoformat(),
    }), encoding="utf-8")
    return adjusted, raw, jpath, spath, ppath


def args(paths):
    adjusted, raw, jpath, spath, ppath = paths
    return [
        "--csv", str(adjusted), "--position", str(ppath),
        "--manual-ledger", str(jpath),
        "--raw-csv", str(raw), "--split-ledger", str(spath),
        "--peak-audit",
    ]


class TestHeldPeakAudit(unittest.TestCase):
    def test_peak_reconstructed_from_confirmed_opening_fills(self):
        r = verify()
        self.assertEqual(r.status, "CLOSE_PEAK_MATCHES_SELF_DECLARED_OPENING_FILLS_UNATTESTED")
        self.assertEqual(r.holding_periods_started, 1)
        self.assertEqual(r.held_closing_sessions, 30)
        self.assertEqual(r.latest_holding_entry, DAYS[60].isoformat())
        self.assertEqual(r.reconstructed_highest_held_close, prices()[-1].close)
        self.assertFalse(r.source_prices_independently_attested)
        self.assertFalse(r.fill_timing_independently_attested)
        self.assertFalse(r.original_peak_initialization_parity_verified)
        self.assertTrue(r.assumed_fills_at_regular_open)
        self.assertEqual(len(r.manual_ledger_sha256), 64)
        self.assertEqual(len(r.adjusted_bars_sha256), 64)

    def test_first_confirmed_buy_includes_entry_session_closing_price(self):
        b = prices()
        last = b[-1]
        for idx in range(61, len(b)):
            b[idx] = Bar(DAYS[idx], 90, 90.5, 89.5, 90)
        p = position(peak=b[60].close)
        report = verify(bars=b, pos=p)
        self.assertEqual(report.reconstructed_highest_held_close, b[60].close)

    def test_flat_sessions_prior_to_first_buy_do_not_influence_peak(self):
        b = prices()
        b[55] = Bar(DAYS[55], 200, 200.5, 199.5, 200)
        r = verify(bars=b)
        self.assertLess(r.reconstructed_highest_held_close, 200)

    def test_partial_sell_does_not_reset_running_held_peak(self):
        b = prices()
        b[65] = Bar(DAYS[65], 140, 140.5, 139.5, 140)
        r = verify(bars=b, pos=position(peak=140))
        self.assertEqual(r.reconstructed_highest_held_close, 140)
        self.assertEqual(r.holding_periods_started, 1)

    def test_full_exit_and_later_reentry_resets_peak(self):
        ev = (
            event(1, "BUY_FILL", "3", "-500", 50),
            event(2, "SELL_FILL", "-3", "600", 60),
            event(3, "BUY_FILL", "2", "-200", 75),
        )
        b = prices()
        b[55] = Bar(DAYS[55], 170, 170.5, 169.5, 170)
        r = verify(bars=b, book=ledger(ev), pos=position(shares=2, cash=900))
        self.assertEqual(r.holding_periods_started, 2)
        self.assertEqual(r.latest_holding_entry, DAYS[75].isoformat())
        self.assertEqual(r.held_closing_sessions, 25)
        self.assertLess(r.reconstructed_highest_held_close, 170)

    def test_flat_end_position_requires_null_peak(self):
        ev = (event(1, "BUY_FILL", "3", "-500", 50),
              event(2, "SELL_FILL", "-3", "600", 60))
        p = position(shares=0, cash=1100)
        r = verify(book=ledger(ev), pos=p)
        self.assertIsNone(r.reconstructed_highest_held_close)
        self.assertEqual(r.held_closing_sessions, 10)

    def test_no_fills_after_flat_checkpoint_yields_no_peak(self):
        r = verify(book=ledger(events=()), pos=position(shares=0, cash=1000))
        self.assertEqual(r.holding_periods_started, 0)
        self.assertEqual(r.held_closing_sessions, 0)
        self.assertIsNone(r.position_claimed_peak_close)

    def test_wrong_declared_peak_fails_closed(self):
        with self.assertRaisesRegex(MonitorDataError, "Highest held closing price mismatch"):
            verify(pos=position(peak=100))

    def test_nonflat_checkpoint_fails_without_assuming_prior_peak(self):
        j = ledger(checkpoint_shares="1")
        p = position(shares=4, cash=675)
        with self.assertRaisesRegex(MonitorDataError, "flat zero-share checkpoint"):
            verify(book=j, pos=p)

    def test_refuse_any_split_after_checkpoint_even_if_currently_flat(self):
        for date_index in (42, 70):
            with self.subTest(index=date_index):
                event_ = SplitEvent(DAYS[date_index], 3, 1)
                with self.assertRaisesRegex(MonitorDataError, "unmodeled stock split"):
                    verify(split_ledger=splits(event_))

    def test_split_prior_to_checkpoint_not_in_replay_window(self):
        r = verify(split_ledger=splits(SplitEvent(DAYS[30], 3, 1)))
        self.assertEqual(r.split_events_in_replay, 0)

    def test_opposite_same_day_fill_directions_fail_closed(self):
        ev = (
            event(1, "BUY_FILL", "4", "-500", 60),
            event(2, "SELL_FILL", "-1", "150", 60),
            event(3, "CASH_CONTRIBUTION", "0", "25", 80),
        )
        with self.assertRaisesRegex(MonitorDataError, "same-session buy-and-sell"):
            verify(book=ledger(ev))

    def test_multiple_buys_same_day_can_count_one_holding_period(self):
        ev = (
            event(1, "BUY_FILL", "2", "-200", 60),
            event(2, "BUY_FILL", "2", "-300", 60),
            event(3, "SELL_FILL", "-1", "150", 70),
            event(4, "CASH_CONTRIBUTION", "0", "25", 80),
        )
        r = verify(book=ledger(ev))
        self.assertEqual(r.holding_periods_started, 1)

    def test_missing_event_session_fails_before_peak_check(self):
        bad = event(1, "BUY_FILL", "4", "-500", 60)
        bad = ManualEvent(bad.sequence, bad.event_id,
                          DAYS[60] + timedelta(days=4), bad.kind,
                          bad.shares_delta, bad.cash_delta, bad.manually_confirmed)
        j = ledger((bad,) + records()[1:])
        with self.assertRaisesRegex(MonitorDataError, "absent market session"):
            verify(book=j)

    def test_cash_mismatch_fails_before_peak_is_considered(self):
        with self.assertRaisesRegex(MonitorDataError, "declared position shares/cash"):
            verify(pos=position(cash=674))

    def test_missing_or_wrong_split_ledger_fails(self):
        with self.assertRaisesRegex(MonitorDataError, "explicit versioned split ledger"):
            verify(split_ledger="no-ledger")
        with self.assertRaisesRegex(MonitorDataError, "symbol"):
            verify(split_ledger=SplitLedger("VYM", ()))

    def test_cli_peak_audit_requires_every_ledger_input(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = fixture_paths(Path(folder))
            base = args(paths)
            for flag in ("--manual-ledger", "--raw-csv", "--split-ledger"):
                ix = base.index(flag)
                reduced = base[:ix] + base[ix + 2:]
                with self.subTest(missing=flag):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        result = main(reduced)
                    self.assertEqual(result, 2)
                    self.assertIn("NO_VERIFIED_SIGNAL", err.getvalue())
                    self.assertIn("Peak audit requires", err.getvalue())

    def test_cli_wrong_peak_blocks_signal_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = fixture_paths(root, selected_peak=100)
            dest = root / "signal.json"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result = main(args(paths) + ["--output", str(dest)])
            self.assertEqual(result, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", err.getvalue())
            self.assertIn("Highest held closing price mismatch", err.getvalue())
            self.assertFalse(dest.exists())

    def test_cli_matching_peak_remains_unverified_with_explicit_caveats(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = fixture_paths(Path(folder))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(args(paths))
            self.assertEqual(result, 0)
            data = json.loads(stdout.getvalue())
            self.assertEqual(data["result"]["verification"], "UNVERIFIED_DO_NOT_TRADE")
            self.assertEqual(data["safety"], "RESEARCH_ONLY_UNVERIFIED")
            r = data["held_peak_audit"]
            self.assertFalse(r["fill_timing_independently_attested"])
            self.assertFalse(r["original_peak_initialization_parity_verified"])
            self.assertEqual(r["holding_periods_started"], 1)
            self.assertTrue(r["assumed_fills_at_regular_open"])


if __name__ == "__main__":
    unittest.main()
