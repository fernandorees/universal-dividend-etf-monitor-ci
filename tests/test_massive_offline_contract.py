"""Fictional Massive daily REST response verification, with no network/secrets."""
from __future__ import annotations

import copy
import io
import json
import os
import unittest
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from schd_monitor.massive_offline_contract import (
    MAX_BASE_AGGREGATES, MassiveDailyRequest, check_massive_offline_response,
)
from schd_monitor.models import MonitorDataError
from schd_monitor.data import (
    MAX_MASSIVE_REST_BYTES, _NoMassiveRedirect, fetch_massive_readonly_daily,
    fetch_alpaca_raw_daily,
)


def req():
    return MassiveDailyRequest("MOCK", date(2038, 6, 10), date(2038, 6, 14))


def epoch_ms(day, *, hour=0, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


def good():
    return {
        "ticker": "MOCK", "adjusted": True, "queryCount": 2,
        "resultsCount": 2, "request_id": "mock-123", "status": "OK",
        "results": [
            {"t": epoch_ms(date(2038, 6, 10)), "o": 10.0, "h": 11.0,
             "l": 9.0, "c": 10.5, "v": 1000, "n": 30, "vw": 10.4},
            {"t": epoch_ms(date(2038, 6, 14)), "o": 10.2, "h": 12.0,
             "l": 10.0, "c": 11.5, "v": 1600, "n": 50, "vw": 11.3},
        ],
    }


class MassiveOfflineContractTests(unittest.TestCase):
    def check(self, payload=None, status=200):
        return check_massive_offline_response(
            request=req(), http_status=status, payload=good() if payload is None else payload)

    def test_explicit_split_adjusted_daily_request_without_credentials(self):
        r = req()
        self.assertEqual(r.path_segments(),
                         ("v2", "aggs", "ticker", "MOCK", "range",
                          "1", "day", "2038-06-10", "2038-06-14"))
        self.assertEqual(r.params(), {"adjusted": "true", "sort": "asc", "limit": "50000"})
        self.assertEqual(len(r.request_sha256()), 64)
        self.assertNotIn("key", str(r.params()).lower())

    def test_invalid_requests_fail_closed(self):
        for patch in ({"symbol": "mock"}, {"symbol": "MOCK?"},
                      {"start": "2038-06-10"}, {"end": date(2038, 6, 9)},
                      {"adjusted": False}, {"adjusted": 1},
                      {"schema_version": True}, {"schema_version": 12}):
            with self.subTest(patch=patch):
                with self.assertRaises(MonitorDataError):
                    replace(req(), **patch)

    def test_genuine_shape_does_not_authenticate_vendor_or_produce_signal(self):
        result = self.check()
        self.assertEqual(result.status, "MASSIVE_REST_SHAPE_CONSISTENT_UNATTESTED")
        self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertEqual(len(result.bars), 2)
        self.assertEqual(result.bars[-1].session.isoformat(), "2038-06-14")
        self.assertEqual(result.claimed_server_request_id, "mock-123")
        self.assertEqual(result.as_json()["bars_count"], 2)
        self.assertNotIn("bars", result.as_json())
        for flag in ("provider_authenticated", "split_adjustment_verified",
                     "market_data_entitlement_verified", "session_coverage_verified",
                     "source_independence_verified", "vendor_bar_finality_verified",
                     "production_signals_verified"):
            self.assertFalse(getattr(result, flag), flag)

    def test_http_errors_never_become_fallback(self):
        for code in (401, 403, 404, 429, 500, 503, False):
            with self.subTest(code=code):
                with self.assertRaisesRegex(MonitorDataError, "HTTP status"):
                    self.check(status=code)

    def test_wrong_status_ticker_and_adjustment_fail(self):
        for field, bad in (("status", "DELAYED"), ("ticker", "OTHER"),
                           ("adjusted", False), ("adjusted", 1), ("adjusted", "true")):
            with self.subTest(field=field):
                data = good()
                data[field] = bad
                with self.assertRaises(MonitorDataError):
                    self.check(data)

    def test_missing_unexpected_and_invalid_envelope_fail(self):
        data = good()
        del data["request_id"]
        with self.assertRaises(MonitorDataError):
            self.check(data)
        data = good()
        data["secret"] = "fake"
        with self.assertRaises(MonitorDataError):
            self.check(data)
        for wrong in ([], "bad", 123):
            with self.assertRaises(MonitorDataError):
                self.check(wrong)
        with self.assertRaises(MonitorDataError):
            check_massive_offline_response(request={}, http_status=200, payload=good())

    def test_request_id_is_required_and_structurally_checked(self):
        for bad in (None, "", "key=value", 123, " "):
            with self.subTest(id=bad):
                data = good()
                data["request_id"] = bad
                with self.assertRaises(MonitorDataError):
                    self.check(data)

    def test_truncation_and_contradictory_counts_fail(self):
        for field, bad in (("queryCount", MAX_BASE_AGGREGATES),
                           ("queryCount", -1), ("queryCount", 1), ("queryCount", True),
                           ("resultsCount", 0), ("resultsCount", 3),
                           ("resultsCount", 2.0)):
            with self.subTest(field=field, bad=bad):
                data = good()
                data[field] = bad
                with self.assertRaises(MonitorDataError):
                    self.check(data)

    def test_documented_optional_count_is_checked_not_always_rejected(self):
        # Massive's published discrepancy example includes count in addition
        # to resultsCount; the response field is optional, not a new truth source.
        data = good()
        self.assertEqual(len(self.check(data).bars), 2)  # omitted remains valid
        data["count"] = 2
        accepted = self.check(data)
        self.assertEqual(len(accepted.bars), 2)
        self.assertEqual(accepted.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(accepted.provider_authenticated)
        for bad in (1, 3, 0, True, 2.0, "2", None):
            with self.subTest(bad=bad):
                broken = copy.deepcopy(data)
                broken["count"] = bad
                with self.assertRaisesRegex(MonitorDataError, "optional count"):
                    self.check(broken)

    def test_nonempty_paginated_responses_fail_closed(self):
        for link in ("https://example.invalid/next", "", False):
            with self.subTest(link=link):
                data = good()
                data["next_url"] = link
                with self.assertRaises(MonitorDataError):
                    self.check(data)
        data = good()
        data["next_url"] = None
        self.assertEqual(len(self.check(data).bars), 2)

    def test_empty_or_invalid_results_fail(self):
        for bad in (None, [], {}, "bad"):
            with self.subTest(bad=bad):
                data = good()
                data["results"] = bad
                with self.assertRaises(MonitorDataError):
                    self.check(data)

    def test_new_york_midnight_timestamp_and_dst_contract(self):
        for bad in ("1780000000000", True, -1, epoch_ms(date(2038, 6, 10), hour=1),
                    epoch_ms(date(2038, 6, 9))):
            with self.subTest(t=bad):
                data = copy.deepcopy(good())
                data["results"][0]["t"] = bad
                with self.assertRaises(MonitorDataError):
                    self.check(data)
        winter = MassiveDailyRequest("MOCK", date(2038, 1, 5), date(2038, 1, 5))
        data = good()
        data["results"] = [{"t": epoch_ms(date(2038, 1, 5)),
                            "o": 9.0, "h": 10.0, "l": 8.0, "c": 9.5}]
        data["resultsCount"] = 1
        data["queryCount"] = 1
        checked = check_massive_offline_response(request=winter, http_status=200, payload=data)
        self.assertEqual(checked.bars[0].session, date(2038, 1, 5))

    def test_malformed_prices_auxiliary_fields_and_unknown_bars_fail(self):
        for key, bad in (("o", True), ("h", "11.0"), ("l", -2),
                         ("c", float("nan")), ("v", -1), ("v", 10**1000),
                         ("n", 1.5), ("vw", "bad"), ("otc", "false"),
                         ("t", None)):
            with self.subTest(key=key):
                data = copy.deepcopy(good())
                data["results"][0][key] = bad
                with self.assertRaises(MonitorDataError):
                    self.check(data)
        data = good()
        data["results"][0]["unexpected"] = 123
        with self.assertRaises(MonitorDataError):
            self.check(data)

    def test_unsorted_and_duplicate_daily_bars_fail(self):
        data = good()
        data["results"].reverse()
        with self.assertRaises(MonitorDataError):
            self.check(data)
        data = good()
        data["results"][1]["t"] = data["results"][0]["t"]
        with self.assertRaises(MonitorDataError):
            self.check(data)

    def test_fingerprints_change_with_response_and_do_not_modify_input(self):
        raw = good()
        before = copy.deepcopy(raw)
        first = self.check(raw)
        changed = copy.deepcopy(raw)
        changed["results"][0]["o"] += .01
        second = self.check(changed)
        self.assertNotEqual(first.response_sha256, second.response_sha256)
        self.assertEqual(first.request_sha256, second.request_sha256)
        self.assertEqual(raw, before)


class _FakeMassiveReply(io.BytesIO):
    def __init__(self, payload: bytes, status: int = 200):
        super().__init__(payload)
        self._status = status
        self.max_requested_bytes = 0

    def getcode(self):
        return self._status

    def read(self, size=-1):
        self.max_requested_bytes = max(self.max_requested_bytes, size)
        return super().read(size)


class MassiveReadOnlyTransportTests(unittest.TestCase):
    """All transport tests use mocked HTTPS; never call a vendor or log keys."""

    def test_no_local_data_key_rejects_without_network(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "schd_monitor.data.build_opener"
        ) as make_opener:
            with self.assertRaisesRegex(MonitorDataError, "MASSIVE_API_KEY"):
                fetch_massive_readonly_daily(req())
            make_opener.assert_not_called()
        with self.assertRaisesRegex(MonitorDataError, "Explicit MassiveDailyRequest"):
            fetch_massive_readonly_daily("MOCK")

    def test_explicit_https_bearer_get_is_bounded_and_unattested(self):
        response = _FakeMassiveReply(json.dumps(good()).encode("utf-8"))
        opener = Mock()
        opener.open.return_value = response
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "fictional-local-data-key"}), patch(
            "schd_monitor.data.build_opener", return_value=opener
        ) as build:
            result = fetch_massive_readonly_daily(req())
        self.assertEqual(len(result.bars), 2)
        self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(result.provider_authenticated)
        self.assertFalse(result.production_signals_verified)
        self.assertEqual(response.max_requested_bytes, MAX_MASSIVE_REST_BYTES + 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, (
            "https://api.massive.com/v2/aggs/ticker/MOCK/range/1/day/2038-06-10/2038-06-14"
            "?adjusted=true&sort=asc&limit=50000"
        ))
        self.assertEqual(request.get_header("Authorization"),
                         "Bearer fictional-local-data-key")
        self.assertNotIn("fictional-local-data-key", request.full_url)
        self.assertIsInstance(build.call_args.args[0], _NoMassiveRedirect)
        self.assertIsNone(build.call_args.args[0].redirect_request(
            None, None, 302, "", {}, "https://other.example.invalid"
        ))

    def test_no_token_or_payload_leaks_on_denied_redirect_and_invalid_data(self):
        for response in (
            _FakeMassiveReply(json.dumps(good()).encode(), status=403),
            _FakeMassiveReply(b"\\xff"),
            _FakeMassiveReply(b'{"status": "OK", "status": "UNEXPECTED"}'),
            _FakeMassiveReply(b"{"),
            _FakeMassiveReply(b" " * (MAX_MASSIVE_REST_BYTES + 1)),
        ):
            with self.subTest(payload_size=len(response.getvalue())):
                opener = Mock()
                opener.open.return_value = response
                with patch.dict(os.environ, {"MASSIVE_API_KEY": "fictional-local-data-key"}), patch(
                    "schd_monitor.data.build_opener", return_value=opener
                ):
                    with self.assertRaises(MonitorDataError) as error:
                        fetch_massive_readonly_daily(req())
                self.assertNotIn("fictional-local-data-key", str(error.exception))
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.massive.com", 302,
                                            "redirect", {}, None)
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "fictional-local-data-key"}), patch(
            "schd_monitor.data.build_opener", return_value=opener
        ):
            with self.assertRaisesRegex(MonitorDataError, "HTTPS request failed") as error:
                fetch_massive_readonly_daily(req())
        self.assertNotIn("fictional-local-data-key", str(error.exception))


class LegacyAlpacaBrokerCredentialsFailClosedTests(unittest.TestCase):
    """Prove the dormant Trading API-key adapter cannot read keys or call HTTP."""

    def test_legacy_alpaca_refuses_even_when_trading_credentials_exist(self):
        with patch.dict(os.environ, {
            "ALPACA_API_KEY_ID": "fictional-never-use-key",
            "ALPACA_API_SECRET_KEY": "fictional-never-use-secret",
        }), patch("schd_monitor.data.os.getenv", side_effect=AssertionError(
            "legacy adapter must never inspect credentials",
        )) as getenv, patch("schd_monitor.data.build_opener", side_effect=AssertionError(
            "legacy adapter must never build an HTTP opener",
        )) as opener, patch("urllib.request.urlopen", side_effect=AssertionError(
            "legacy adapter must never open a URL",
        )) as urlopen:
            for kwargs in (
                {"symbol": "SCHD", "start": "2038-06-10", "end": "2038-06-14"},
                {"symbol": "SCHD", "start": "2038-06-10",
                 "end": "2038-06-14", "feed": "iex"},
            ):
                with self.subTest(feed=kwargs.get("feed", "sip")):
                    with self.assertRaisesRegex(
                        MonitorDataError, "trading-capable credentials forbidden",
                    ) as caught:
                        fetch_alpaca_raw_daily(**kwargs)
                    self.assertNotIn("fictional-never-use-key", str(caught.exception))
                    self.assertNotIn("fictional-never-use-secret", str(caught.exception))
            getenv.assert_not_called()
            opener.assert_not_called()
            urlopen.assert_not_called()

    def test_legacy_alpaca_refuses_before_parsing_inputs_even_without_keys(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "schd_monitor.data.os.getenv", side_effect=AssertionError(
                "legacy adapter must never look up a key",
            ),
        ) as getenv, patch(
            "schd_monitor.data.build_opener", side_effect=AssertionError(
                "legacy adapter must never make HTTP requests",
            ),
        ) as opener:
            for params in (
                ("INVALID", "bad-date", "2038-06-14"),
                (None, None, None),
            ):
                with self.subTest(params=params):
                    with self.assertRaisesRegex(
                        MonitorDataError, "Legacy Alpaca transport disabled",
                    ):
                        fetch_alpaca_raw_daily(*params, max_pages=1)
            getenv.assert_not_called()
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
