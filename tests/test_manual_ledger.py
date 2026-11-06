"""P01 manual ledger tests: fictional events only; no private trades or broker data."""
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
from schd_monitor.manual_ledger import (
    ManualEvent, ManualLedger, audit_manual_ledger, manual_ledger_from_json,
)
from schd_monitor.models import MonitorDataError, Position


def trading_days(count=90):
    days = []
    day = date(2024, 1, 2)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


DAYS = trading_days()


def event(seq, kind, shares, cash, index, eid=None):
    return ManualEvent(seq, eid or ("E" + str(seq)), DAYS[index], kind,
                       Decimal(shares), Decimal(cash), True)


def records():
    return (
        event(1, "CASH_CONTRIBUTION", "0", "50", 70),
        event(2, "BUY_FILL", "2", "-30", 72),
        event(3, "SELL_FILL", "-1", "20", 75),
        event(4, "DIVIDEND_CREDIT", "0", "3.50", 80),
    )


def journal(events=None, **changed):
    values = {
        "symbol": "SCHD", "checkpoint_session": DAYS[65],
        "checkpoint_shares": Decimal("5"),
        "checkpoint_cash": Decimal("100"),
        "reviewed_through": DAYS[-1], "activity_reviewed": True,
        "events": tuple(records() if events is None else events),
        "schema_version": 1,
    }
    values.update(changed)
    return ManualLedger(**values)


def position(shares=6, cash=143.5, through=None, peak=130):
    return Position("SCHD", shares, cash, peak if shares else None,
                    DAYS[-1] if through is None else through)


def audit(j=None, p=None, days=None):
    return audit_manual_ledger(
        journal() if j is None else j,
        position() if p is None else p,
        sessions=DAYS if days is None else days,
    )


def json_payload(j):
    return {
        "schema_version": j.schema_version, "symbol": j.symbol,
        "checkpoint_session": j.checkpoint_session.isoformat(),
        "checkpoint_shares": str(j.checkpoint_shares),
        "checkpoint_cash": str(j.checkpoint_cash),
        "reviewed_through": j.reviewed_through.isoformat(),
        "activity_reviewed": j.activity_reviewed,
        "events": [
            {
                "sequence": e.sequence, "event_id": e.event_id,
                "session": e.session.isoformat(), "kind": e.kind,
                "shares_delta": str(e.shares_delta), "cash_delta": str(e.cash_delta),
                "manually_confirmed": e.manually_confirmed,
            }
            for e in j.events
        ],
    }


def price_csv(path):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close"])
        for i, day in enumerate(DAYS):
            close = 100 + i * .05
            writer.writerow([day, close, close + .5, close - .5, close])


class TestManualLedger(unittest.TestCase):
    def test_confirmed_events_replay_into_declared_position_without_attestation(self):
        result = audit()
        self.assertEqual(result.status, "MATCHES_SELF_DECLARED_LEDGER_UNATTESTED")
        self.assertEqual(result.events_replayed, 4)
        self.assertEqual(Decimal(result.shares_from_declared_events), Decimal("6"))
        self.assertEqual(Decimal(result.cash_from_declared_events), Decimal("143.50"))
        self.assertFalse(result.peak_reconciled)
        self.assertFalse(result.independently_attested)
        self.assertEqual(len(result.ledger_sha256), 64)

    def test_unconfirmed_order_or_proposed_fill_is_rejected(self):
        for confirmation in (False, None, 1, "True"):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(MonitorDataError, "manually_confirmed"):
                    replace(records()[1], manually_confirmed=confirmation)

    def test_reject_unapproved_event_kind_or_cash_share_direction(self):
        bad = [
            ("BUY_FILL", "1", "10"), ("BUY_FILL", "0", "-10"),
            ("SELL_FILL", "1", "10"), ("SELL_FILL", "-1", "-10"),
            ("CASH_CONTRIBUTION", "2", "10"),
            ("CASH_WITHDRAWAL", "0", "10"),
            ("DIVIDEND_CREDIT", "0", "-1"),
            ("SPLIT_ADJUSTMENT", "2", "0"),
        ]
        for kind, shares, cash in bad:
            with self.subTest(kind=kind, shares=shares, cash=cash):
                with self.assertRaises(MonitorDataError):
                    event(1, kind, shares, cash, 70)
        with self.assertRaises(MonitorDataError):
            replace(records()[0], kind=[])

    def test_fails_on_wrong_share_and_cash_balances(self):
        for p in (position(shares=7), position(cash=143.49)):
            with self.subTest(position=p):
                with self.assertRaisesRegex(MonitorDataError, "does not match"):
                    audit(p=p)

    def test_intermediate_negative_balance_fails_even_if_end_recovers(self):
        ev = (
            event(1, "BUY_FILL", "1", "-101", 70),
            event(2, "CASH_CONTRIBUTION", "0", "101", 71),
        )
        with self.assertRaisesRegex(MonitorDataError, "negative shares/cash"):
            audit(j=journal(ev))
        ev = (
            event(1, "SELL_FILL", "-6", "100", 70),
            event(2, "BUY_FILL", "6", "-100", 71),
        )
        with self.assertRaisesRegex(MonitorDataError, "negative shares/cash"):
            audit(j=journal(ev))

    def test_manual_deposit_and_dividend_are_not_implicitly_created(self):
        ev = (event(1, "BUY_FILL", "2", "-30", 72),)
        j = journal(ev)
        p = position(shares=7, cash=70)
        self.assertEqual(audit(j=j, p=p).events_replayed, 1)
        with self.assertRaisesRegex(MonitorDataError, "does not match"):
            audit(j=j, p=position(shares=7, cash=70+50+3.5))

    def test_cash_withdrawal_supported_only_as_explicit_event(self):
        ev = (event(1, "CASH_WITHDRAWAL", "0", "-20", 70),)
        j = journal(ev)
        p = position(shares=5, cash=80)
        self.assertEqual(Decimal(audit(j=j, p=p).cash_from_declared_events), Decimal("80"))

    def test_exact_decimal_addition_avoids_binary_float_drift(self):
        j = journal(
            events=(event(1, "CASH_CONTRIBUTION", "0", "0.1", 70),
                    event(2, "CASH_CONTRIBUTION", "0", "0.2", 71)),
            checkpoint_shares=Decimal("0"), checkpoint_cash=Decimal("0"),
        )
        r = audit(j=j, p=position(shares=0, cash=0.3))
        self.assertEqual(Decimal(r.cash_from_declared_events), Decimal("0.3"))

    def test_zero_events_requires_explicit_review_statement(self):
        j = journal(events=(), checkpoint_shares=Decimal(0),
                    checkpoint_cash=Decimal(0))
        self.assertEqual(audit(j=j, p=position(shares=0, cash=0)).events_replayed, 0)
        with self.assertRaisesRegex(MonitorDataError, "activity_reviewed"):
            replace(j, activity_reviewed=False)

    def test_event_sequence_gaps_duplicates_and_chronology_fail(self):
        ev = list(records())
        for mutated in (
            (replace(ev[0], sequence=2),) + tuple(ev[1:]),
            (ev[0], replace(ev[1], event_id=ev[0].event_id)) + tuple(ev[2:]),
            (replace(ev[0], session=DAYS[78]),) + tuple(ev[1:]),
        ):
            with self.subTest(mutated=mutated[:2]):
                with self.assertRaises(MonitorDataError):
                    journal(mutated)

    def test_event_or_checkpoint_outside_supplied_trading_dates_fails(self):
        with self.assertRaisesRegex(MonitorDataError, "checkpoint not within"):
            audit(j=journal(checkpoint_session=DAYS[65] + timedelta(days=4)))
        # Saturday after the third event remains before the fourth event.
        bad = replace(records()[2], session=DAYS[75] + timedelta(days=4))
        with self.assertRaisesRegex(MonitorDataError, "absent market session"):
            audit(j=journal(records()[:2] + (bad,) + records()[3:]))
        with self.assertRaisesRegex(MonitorDataError, "after checkpoint"):
            journal(events=(event(1, "BUY_FILL", "2", "-30", 65),))
        with self.assertRaisesRegex(MonitorDataError, "after checkpoint"):
            journal(events=(event(1, "BUY_FILL", "2", "-30", 70),),
                    reviewed_through=DAYS[69])

    def test_stale_ledger_or_position_reconciliation_is_rejected(self):
        with self.assertRaisesRegex(MonitorDataError, "through latest session"):
            audit(j=journal(reviewed_through=DAYS[-2]))
        with self.assertRaisesRegex(MonitorDataError, "through latest session"):
            audit(p=position(through=DAYS[-2]))
        with self.assertRaisesRegex(MonitorDataError, "symbol"):
            audit(j=journal(symbol="VYM"))

    def test_same_session_multiple_confirmed_transactions_follow_sequence(self):
        ev = (
            event(1, "CASH_CONTRIBUTION", "0", "10", 70),
            event(2, "BUY_FILL", "1", "-10", 70),
        )
        j = journal(events=ev, checkpoint_shares=Decimal(0), checkpoint_cash=Decimal(0))
        self.assertEqual(audit(j=j, p=position(shares=1, cash=0)).events_replayed, 2)
        reversed_ev = (replace(ev[0], sequence=2), replace(ev[1], sequence=1))
        with self.assertRaisesRegex(MonitorDataError, "sequence"):
            journal(events=reversed_ev)

    def test_reject_malformed_json_schema_precision_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            base = json_payload(journal())
            path.write_text(json.dumps(base), encoding="utf-8")
            j = manual_ledger_from_json(path)
            self.assertEqual(j, journal())
            cases = [
                {"schema_version": 2}, {"schema_version": True},
                {"activity_reviewed": False}, {"checkpoint_cash": 100.0},
                {"checkpoint_shares": "-1"}, {"checkpoint_cash": "NaN"},
                {"checkpoint_session": "20240102"}, {"reviewed_through": "2099-99-99"},
                {"symbol": "schd"}, {"events": "not array"},
                {"events": [{**base["events"][0], "manually_confirmed": False}]},
                {"events": [{**base["events"][0], "shares_delta": 1.2}]},
                {"events": [{**base["events"][0], "unknown": True}]},
            ]
            for changes in cases:
                with self.subTest(changes=changes):
                    path.write_text(json.dumps({**base, **changes}), encoding="utf-8")
                    with self.assertRaises(MonitorDataError):
                        manual_ledger_from_json(path)
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "Duplicate manual-ledger JSON"):
                manual_ledger_from_json(path)
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(MonitorDataError, "Invalid manual-ledger JSON"):
                manual_ledger_from_json(path)

    def test_unrepresentably_large_precise_cash_increment_fails_closed(self):
        ev = (event(1, "CASH_CONTRIBUTION", "0", "1", 70),)
        j = journal(ev, checkpoint_shares=Decimal(0),
                    checkpoint_cash=Decimal("1" + "0" * 65))
        with self.assertRaisesRegex(MonitorDataError, "exact arithmetic"):
            audit(j=j, p=position(shares=0, cash=1e65))

    def test_cli_disagreement_returns_no_verified_signal_and_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bars, current, events, output = (root / name for name in
                ("bars.csv", "position.json", "manual.json", "signal.json"))
            price_csv(bars)
            current.write_text(json.dumps({
                "symbol": "SCHD", "shares": 6, "available_cash": 999,
                "peak_close": 130, "reconciled_through": DAYS[-1].isoformat()
            }), encoding="utf-8")
            events.write_text(json.dumps(json_payload(journal())), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["--csv", str(bars), "--position", str(current),
                               "--manual-ledger", str(events), "--output", str(output)])
            self.assertEqual(result, 2)
            self.assertIn("NO_VERIFIED_SIGNAL", stderr.getvalue())
            self.assertIn("does not match", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_matching_ledger_does_not_assume_any_fill_or_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bars, current, events = (root / name for name in
                ("bars.csv", "position.json", "manual.json"))
            price_csv(bars)
            current.write_text(json.dumps({
                "symbol": "SCHD", "shares": 6, "available_cash": 143.5,
                "peak_close": 130, "reconciled_through": DAYS[-1].isoformat()
            }), encoding="utf-8")
            events.write_text(json.dumps(json_payload(journal())), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(["--csv", str(bars), "--position", str(current),
                               "--manual-ledger", str(events)])
            self.assertEqual(result, 0)
            data = json.loads(stdout.getvalue())
            self.assertEqual(data["result"]["verification"], "UNVERIFIED_DO_NOT_TRADE")
            self.assertEqual(data["manual_ledger_audit"]["status"],
                             "MATCHES_SELF_DECLARED_LEDGER_UNATTESTED")
            self.assertFalse(data["manual_ledger_audit"]["peak_reconciled"])
            self.assertEqual(data["manual_ledger_audit"]["events_replayed"], 4)


if __name__ == "__main__":
    unittest.main()
