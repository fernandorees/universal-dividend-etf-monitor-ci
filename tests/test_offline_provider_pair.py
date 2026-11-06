"""Entirely fictional two-provider page composition; never connects anywhere."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from schd_monitor.alpaca_offline_contract import AlpacaDailyRequest
from schd_monitor.exchange_calendar import completed_xnys_sessions
from schd_monitor.massive_offline_contract import MassiveDailyRequest
from schd_monitor.models import MonitorDataError
from schd_monitor.offline_provider_pair import audit_offline_alpaca_massive_pair

_NY = ZoneInfo("America/New_York")


def _mock_payloads():
    start = date(2024, 1, 2)
    as_of = datetime(2024, 5, 11, 12, tzinfo=timezone.utc)
    sessions = completed_xnys_sessions(start, as_of)
    last = sessions[-1]
    a = AlpacaDailyRequest(
        symbol="MOCK", start=start, end=last, symbol_asof=last,
        feed="sip", adjustment="split",
    )
    m = MassiveDailyRequest(symbol="MOCK", start=start, end=last)
    alpaca_bars, massive_bars = [], []
    for i, day in enumerate(sessions):
        midnight = datetime.combine(day, time(0, 0), tzinfo=_NY)
        stamp = midnight.astimezone(timezone.utc)
        px = round(100 + i * 0.01, 6)
        prices = {"o": px, "h": px + 1, "l": px - 1, "c": px + .2}
        alpaca_bars.append({
            "t": stamp.isoformat().replace("+00:00", "Z"),
            **prices, "v": 5000 + i, "n": 60 + i, "vw": px + .1,
        })
        massive_bars.append({
            "t": int(stamp.timestamp() * 1000),
            **prices, "v": 5000 + i, "n": 60 + i, "vw": px + .1,
        })
    return {
        "alpaca_request": a,
        "alpaca_http_status": 200,
        "alpaca_payload": {
            "symbol": "MOCK", "bars": alpaca_bars, "next_page_token": None,
        },
        "massive_request": m,
        "massive_http_status": 200,
        "massive_payload": {
            "ticker": "MOCK", "adjusted": True, "queryCount": len(sessions),
            "resultsCount": len(sessions), "count": len(sessions),
            "request_id": "fictional-request-123", "status": "OK",
            "results": massive_bars,
        },
        "as_of": as_of,
    }


class OfflineProviderPairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.basis = _mock_payloads()

    def sample(self):
        return copy.deepcopy(self.basis)

    def check(self, **changes):
        data = self.sample()
        data.update(changes)
        return audit_offline_alpaca_massive_pair(**data)

    def test_full_calendar_and_both_shapes_remain_unverified(self):
        result = self.check()
        self.assertEqual(result.status, "ALPACA_MASSIVE_OFFLINE_COMPOSED_UNATTESTED")
        self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertEqual(result.schema_version, 1)
        self.assertGreater(result.sessions_compared, 65)
        self.assertEqual(result.first_session, "2024-01-02")
        self.assertEqual(result.last_completed_session, "2024-05-10")
        self.assertEqual(result.max_relative_ohlc_difference, 0)
        self.assertEqual(result.massive_claimed_server_request_id, "fictional-request-123")
        self.assertEqual(len(result.alpaca_response_sha256), 64)
        self.assertEqual(len(result.massive_response_sha256), 64)
        self.assertEqual(result.calendar_and_two_feed_gate_status,
                         "CALENDAR_COMPLETE_DUAL_FEED_CONSISTENT_UNATTESTED")
        for flag in ("provider_identity_verified", "feed_independence_verified",
                     "vendor_bar_finality_verified", "split_and_dividend_basis_verified",
                     "manual_position_reconciled", "production_signals_verified"):
            self.assertFalse(getattr(result, flag))
            self.assertFalse(result.as_json()[flag])
        self.assertFalse(any("bars" in k.lower() for k in result.as_json()))

    def test_requires_matching_requested_symbol_and_exact_bounds(self):
        for patch in (
            {"alpaca_request": replace(self.basis["alpaca_request"], symbol="OTHER")},
            {"massive_request": replace(self.basis["massive_request"], start=date(2024, 1, 3))},
            {"massive_request": replace(self.basis["massive_request"], end=date(2024, 5, 9))},
        ):
            with self.subTest(patch=str(patch)):
                with self.assertRaisesRegex(MonitorDataError, "requested symbol/date bounds"):
                    self.check(**patch)
        with self.assertRaisesRegex(MonitorDataError, "Typed Alpaca"):
            self.check(alpaca_request=None)

    def test_reject_missing_first_or_last_session_even_if_both_feeds_agree(self):
        for direction in ("first", "last"):
            with self.subTest(direction=direction):
                data = self.sample()
                for field, key in (("alpaca_payload", "bars"),
                                   ("massive_payload", "results")):
                    if direction == "first":
                        data[field][key].pop(0)
                    else:
                        data[field][key].pop()
                data["massive_payload"]["resultsCount"] -= 1
                data["massive_payload"]["count"] -= 1
                data["massive_payload"]["queryCount"] -= 1
                with self.assertRaisesRegex(MonitorDataError, "request calendar coverage"):
                    audit_offline_alpaca_massive_pair(**data)

    def test_reject_same_missing_internal_session_from_both_feeds(self):
        data = self.sample()
        data["alpaca_payload"]["bars"].pop(23)
        data["massive_payload"]["results"].pop(23)
        data["massive_payload"]["resultsCount"] -= 1
        data["massive_payload"]["count"] -= 1
        data["massive_payload"]["queryCount"] -= 1
        with self.assertRaisesRegex(MonitorDataError, "request calendar coverage mismatch"):
            audit_offline_alpaca_massive_pair(**data)

    def test_reject_ohlc_disagreement_beyond_research_tolerance(self):
        data = self.sample()
        data["massive_payload"]["results"][30]["c"] += .03
        with self.assertRaisesRegex(MonitorDataError, "Feed OHLC mismatch"):
            audit_offline_alpaca_massive_pair(**data)

    def test_small_disagreement_changes_evidence_not_production_status(self):
        data = self.sample()
        data["massive_payload"]["results"][30]["c"] += .00001
        result = audit_offline_alpaca_massive_pair(**data)
        self.assertGreater(result.max_relative_ohlc_difference, 0)
        self.assertNotEqual(result.alpaca_response_sha256, result.massive_response_sha256)
        self.assertFalse(result.production_signals_verified)

    def test_reject_incomplete_latest_session_by_asof_clock(self):
        with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
            self.check(as_of=datetime(2024, 5, 10, 18, 0, tzinfo=timezone.utc))
        with self.assertRaisesRegex(MonitorDataError, "timezone-aware"):
            self.check(as_of=datetime(2024, 5, 11, 12, 0))

    def test_vendor_denials_and_incomplete_page_are_blockers(self):
        for change in ({"alpaca_http_status": 403},
                       {"massive_http_status": 429}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(MonitorDataError, "HTTP status"):
                    self.check(**change)
        data = self.sample()
        data["alpaca_payload"]["next_page_token"] = "more"
        with self.assertRaisesRegex(MonitorDataError, "pagination"):
            audit_offline_alpaca_massive_pair(**data)

    def test_composed_gate_refuses_oversized_alpaca_single_page(self):
        data = self.sample()
        original = data["alpaca_payload"]["bars"]
        data["alpaca_payload"]["bars"] = [original[0]] * 10001
        with self.assertRaisesRegex(MonitorDataError, "exceeds requested 10000-bar limit"):
            audit_offline_alpaca_massive_pair(**data)
        self.assertEqual(len(original), len(self.basis["alpaca_payload"]["bars"]))

    def test_composed_gate_rejects_unreviewed_alpaca_daily_bar_field(self):
        data = self.sample()
        data["alpaca_payload"]["bars"][3]["unreviewed_claim"] = "FICTIONAL"
        with self.assertRaisesRegex(MonitorDataError, "unexpected fields"):
            audit_offline_alpaca_massive_pair(**data)

    def test_one_provider_invalid_adjustment_or_missing_vwap_fails(self):
        data = self.sample()
        data["massive_payload"]["adjusted"] = False
        with self.assertRaisesRegex(MonitorDataError, "split-adjusted"):
            audit_offline_alpaca_massive_pair(**data)
        data = self.sample()
        del data["alpaca_payload"]["bars"][20]["vw"]
        with self.assertRaisesRegex(MonitorDataError, "missing required fields"):
            audit_offline_alpaca_massive_pair(**data)

    def test_inclusive_weekend_end_and_holiday_start_are_supported_safely(self):
        # Monday Jan 1 was a holiday; May 11 was Saturday. Jan 2 through
        # May 10 is the exact same complete set of real XNYS sessions.
        new_a = replace(
            self.basis["alpaca_request"],
            start=date(2024, 1, 1), end=date(2024, 5, 11),
        )
        new_m = replace(
            self.basis["massive_request"],
            start=date(2024, 1, 1), end=date(2024, 5, 11),
        )
        result = self.check(alpaca_request=new_a, massive_request=new_m)
        self.assertEqual(result.first_session, "2024-01-02")
        self.assertEqual(result.last_completed_session, "2024-05-10")
        self.assertEqual(result.sessions_compared, self.check().sessions_compared)
        self.assertNotEqual(
            result.alpaca_request_sha256, self.check().alpaca_request_sha256
        )
        self.assertFalse(result.production_signals_verified)
        self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")

    def test_nontrading_bounds_still_reject_shared_missing_edge_sessions(self):
        a_request = replace(
            self.basis["alpaca_request"],
            start=date(2024, 1, 1), end=date(2024, 5, 11),
        )
        m_request = replace(
            self.basis["massive_request"],
            start=date(2024, 1, 1), end=date(2024, 5, 11),
        )
        for direction in ("first", "last"):
            with self.subTest(direction=direction):
                data = self.sample()
                data["alpaca_request"] = a_request
                data["massive_request"] = m_request
                for name, field in (
                    ("alpaca_payload", "bars"), ("massive_payload", "results")
                ):
                    data[name][field].pop(0 if direction == "first" else -1)
                for field in ("queryCount", "resultsCount", "count"):
                    data["massive_payload"][field] -= 1
                with self.assertRaisesRegex(
                    MonitorDataError, "request calendar coverage"
                ):
                    audit_offline_alpaca_massive_pair(**data)


if __name__ == "__main__":
    unittest.main()
