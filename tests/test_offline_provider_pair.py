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
from schd_monitor.offline_provider_pair import (
    audit_offline_alpaca_massive_pair, preflight_offline_alpaca_massive_requests,
    audit_offline_sharadar_massive_pair, preflight_offline_sharadar_massive_requests,
)
from schd_monitor.sharadar_funds_offline_contract import (
    MAX_SHARADAR_FUNDS_ROWS, SharadarFundsRequest,
)

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

    def test_preflight_closed_session_bounds_before_any_provider_response(self):
        baseline = self.sample()
        got = preflight_offline_alpaca_massive_requests(
            alpaca_request=baseline["alpaca_request"],
            massive_request=baseline["massive_request"],
            as_of=baseline["as_of"],
        )
        self.assertIsInstance(got, tuple)
        self.assertGreater(len(got), 65)
        self.assertEqual((got[0], got[-1]), (date(2024, 1, 2), date(2024, 5, 10)))
        # Nontrading boundaries still resolve to the same complete sessions.
        a = replace(baseline["alpaca_request"],
                    start=date(2024, 1, 1), end=date(2024, 5, 11))
        m = replace(baseline["massive_request"],
                    start=date(2024, 1, 1), end=date(2024, 5, 11))
        self.assertEqual(got, preflight_offline_alpaca_massive_requests(
            alpaca_request=a, massive_request=m, as_of=baseline["as_of"],
        ))

    def test_preflight_refuses_unmatched_requests_and_naive_asof(self):
        basis = self.sample()
        call = {
            "alpaca_request": basis["alpaca_request"],
            "massive_request": basis["massive_request"],
            "as_of": basis["as_of"],
        }
        with self.assertRaisesRegex(MonitorDataError, "Typed Alpaca"):
            preflight_offline_alpaca_massive_requests(
                **{**call, "alpaca_request": None})
        with self.assertRaisesRegex(MonitorDataError, "symbol/date bounds"):
            preflight_offline_alpaca_massive_requests(
                **{**call, "massive_request": replace(
                    call["massive_request"], symbol="OTHER")})
        with self.assertRaisesRegex(MonitorDataError, "timezone-aware"):
            preflight_offline_alpaca_massive_requests(
                **{**call, "as_of": datetime(2024, 5, 11, 12)})

    def test_preflight_fails_closed_for_stale_future_unfinished_and_no_session(self):
        basis = self.sample()
        call = {
            "alpaca_request": basis["alpaca_request"],
            "massive_request": basis["massive_request"],
            "as_of": basis["as_of"],
        }
        for bad_end in (date(2024, 5, 9), date(2024, 5, 13)):
            with self.subTest(requested_end=bad_end):
                with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
                    preflight_offline_alpaca_massive_requests(
                        **{**call,
                           "alpaca_request": replace(call["alpaca_request"], end=bad_end),
                           "massive_request": replace(call["massive_request"], end=bad_end)})
        with self.assertRaisesRegex(MonitorDataError, "Session coverage mismatch"):
            preflight_offline_alpaca_massive_requests(
                **{**call, "as_of": datetime(2024, 5, 10, 18, 0, tzinfo=timezone.utc)})
        with self.assertRaisesRegex(MonitorDataError, "no exchange sessions"):
            preflight_offline_alpaca_massive_requests(
                **{**call,
                   "alpaca_request": replace(call["alpaca_request"],
                       start=date(2024, 1, 1), end=date(2024, 1, 1)),
                   "massive_request": replace(call["massive_request"],
                       start=date(2024, 1, 1), end=date(2024, 1, 1))})

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





def _fictional_sharadar_massive():
    """Both invented tables share prices but NOT authenticated upstream data."""
    prior = _mock_payloads()
    sessions = completed_xnys_sessions(date(2024, 1, 2), prior["as_of"])
    invented = []
    for session, a in zip(sessions, prior["alpaca_payload"]["bars"]):
        invented.append({
            "ticker": "MOCK", "date": session.isoformat(),
            "open": a["o"], "high": a["h"], "low": a["l"],
            "close": a["c"], "volume": a["v"],
            "lastupdated": session.isoformat(),  # Untrusted date, not finality.
        })
    return {
        "sharadar_request": SharadarFundsRequest(
            symbol="MOCK", start=date(2024, 1, 2), end=sessions[-1],
        ),
        "sharadar_rows": invented,
        "massive_request": prior["massive_request"],
        "massive_http_status": prior["massive_http_status"],
        "massive_payload": prior["massive_payload"],
        "as_of": prior["as_of"],
    }


class SharadarMassiveOfflinePairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.basis = _fictional_sharadar_massive()

    def sample(self):
        return copy.deepcopy(self.basis)

    def check(self, **changes):
        data = self.sample()
        data.update(changes)
        return audit_offline_sharadar_massive_pair(**data)

    def test_all_calendar_sessions_complete_but_every_trust_flag_false(self):
        report = self.check()
        self.assertEqual(
            report.status, "SHARADAR_MASSIVE_OFFLINE_COMPOSED_UNATTESTED",
        )
        self.assertEqual(report.schema_version, 1)
        self.assertEqual(report.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertEqual(report.first_session, "2024-01-02")
        self.assertEqual(report.last_completed_session, "2024-05-10")
        self.assertEqual(report.sessions_compared, len(self.basis["sharadar_rows"]))
        self.assertGreater(report.sessions_compared, 65)
        self.assertEqual(report.max_relative_ohlc_difference, 0)
        self.assertEqual(
            report.calendar_and_two_feed_gate_status,
            "CALENDAR_COMPLETE_DUAL_FEED_CONSISTENT_UNATTESTED",
        )
        self.assertEqual(
            report.massive_claimed_server_request_id, "fictional-request-123",
        )
        for field in (
            "sharadar_request_sha256", "sharadar_fictional_rows_sha256",
            "massive_request_sha256", "massive_response_sha256",
        ):
            self.assertEqual(len(getattr(report, field)), 64)
        for name in (
            "provider_identity_verified", "feed_independence_verified",
            "vendor_bar_finality_verified", "split_and_dividend_basis_verified",
            "manual_position_reconciled", "production_signals_verified",
        ):
            self.assertIs(getattr(report, name), False)
            self.assertIs(report.as_json()[name], False)
        self.assertFalse(any(
            k == "bars" or k.endswith("_rows") or "payload" in k
            for k in report.as_json()
        ))

    def test_matching_typed_inert_requests_and_completion_preflight(self):
        data = self.sample()
        expected = preflight_offline_sharadar_massive_requests(
            sharadar_request=data["sharadar_request"],
            massive_request=data["massive_request"], as_of=data["as_of"],
        )
        self.assertEqual(expected[0], date(2024, 1, 2))
        self.assertEqual(expected[-1], date(2024, 5, 10))
        self.assertEqual(len(expected), len(data["sharadar_rows"]))
        for patch in (
            {"sharadar_request": None},
            {"massive_request": None},
        ):
            with self.subTest(patch=patch), self.assertRaisesRegex(
                MonitorDataError, "Typed Sharadar FUNDS and Massive",
            ):
                self.check(**patch)

    def test_reject_mismatched_symbol_start_or_end_before_row_review(self):
        for patch in (
            {"sharadar_request": replace(
                self.basis["sharadar_request"], symbol="OTHER")},
            {"massive_request": replace(
                self.basis["massive_request"], start=date(2024, 1, 3))},
            {"massive_request": replace(
                self.basis["massive_request"], end=date(2024, 5, 9))},
        ):
            with self.subTest(patch=patch), self.assertRaisesRegex(
                MonitorDataError, "symbol/date bounds differ",
            ):
                self.check(**patch)

    def test_reject_stale_future_or_unfinished_calendar_and_naive_asof(self):
        a = self.basis["sharadar_request"]
        m = self.basis["massive_request"]
        for end in (date(2024, 5, 9), date(2024, 5, 13)):
            with self.subTest(end=end), self.assertRaisesRegex(
                MonitorDataError, "not exactly closed sessions",
            ):
                self.check(sharadar_request=replace(a, end=end),
                           massive_request=replace(m, end=end))
        with self.assertRaisesRegex(MonitorDataError, "not exactly closed sessions"):
            self.check(as_of=datetime(2024, 5, 10, 18, tzinfo=timezone.utc))
        with self.assertRaisesRegex(MonitorDataError, "timezone-aware"):
            self.check(as_of=datetime(2024, 5, 11, 12))

    def test_weekend_and_holiday_selection_bounds_remain_complete(self):
        a = replace(self.basis["sharadar_request"], start=date(2024, 1, 1),
                    end=date(2024, 5, 11))
        m = replace(self.basis["massive_request"], start=date(2024, 1, 1),
                    end=date(2024, 5, 11))
        report = self.check(sharadar_request=a, massive_request=m)
        self.assertEqual(report.sessions_compared, self.check().sessions_compared)
        self.assertNotEqual(
            report.sharadar_request_sha256, self.check().sharadar_request_sha256,
        )
        self.assertFalse(report.production_signals_verified)

    def test_reject_shared_missing_first_last_or_middle_session(self):
        for point in (0, 22, -1):
            with self.subTest(point=point):
                data = self.sample()
                data["sharadar_rows"].pop(point)
                data["massive_payload"]["results"].pop(point)
                for key in ("queryCount", "resultsCount", "count"):
                    data["massive_payload"][key] -= 1
                with self.assertRaisesRegex(
                    MonitorDataError, "request calendar coverage mismatch",
                ):
                    audit_offline_sharadar_massive_pair(**data)

    def test_reject_ohlc_conflict_and_retained_small_difference(self):
        data = self.sample()
        data["massive_payload"]["results"][23]["c"] += .03
        with self.assertRaisesRegex(MonitorDataError, "Feed OHLC mismatch"):
            audit_offline_sharadar_massive_pair(**data)
        data = self.sample()
        data["massive_payload"]["results"][23]["c"] += .00001
        report = audit_offline_sharadar_massive_pair(**data)
        self.assertGreater(report.max_relative_ohlc_difference, 0)
        self.assertFalse(report.feed_independence_verified)
        self.assertFalse(report.production_signals_verified)

    def test_reject_sharadar_extra_adjustment_bad_date_and_incomplete_page(self):
        for field, value, expected in (
            ("closeadj", 999, "fields"),
            ("closeunadj", 999, "fields"),
            ("lastupdated", "bad", "lastupdated"),
            ("volume", -1, "volume"),
            ("date", "2024-01-06", "Weekend"),
        ):
            with self.subTest(field=field):
                data = self.sample()
                # Place the weekend between Friday and Monday so the
                # ordering guard does not mask the weekend-date failure.
                target = (next(row for row in data["sharadar_rows"]
                               if row["date"] == "2024-05-03")
                          if field == "date" else data["sharadar_rows"][0])
                target[field] = ("2024-05-04" if field == "date" else value)
                with self.assertRaisesRegex(MonitorDataError, expected):
                    audit_offline_sharadar_massive_pair(**data)
        with self.assertRaisesRegex(MonitorDataError, "nonempty list"):
            self.check(sharadar_rows=None)
        data = self.sample()
        data["sharadar_rows"] = [data["sharadar_rows"][0]] * MAX_SHARADAR_FUNDS_ROWS
        with self.assertRaisesRegex(MonitorDataError, "pagination"):
            audit_offline_sharadar_massive_pair(**data)

    def test_reject_massive_denial_unadjusted_or_claimed_next_page(self):
        with self.assertRaisesRegex(MonitorDataError, "HTTP status"):
            self.check(massive_http_status=429)
        data = self.sample()
        data["massive_payload"]["adjusted"] = False
        with self.assertRaisesRegex(MonitorDataError, "split-adjusted"):
            audit_offline_sharadar_massive_pair(**data)
        data = self.sample()
        data["massive_payload"]["next_url"] = "https://invalid.example"
        with self.assertRaisesRegex(MonitorDataError, "pagination"):
            audit_offline_sharadar_massive_pair(**data)


if __name__ == "__main__":
    unittest.main()
