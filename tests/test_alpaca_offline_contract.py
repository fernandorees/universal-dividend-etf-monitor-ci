"""Fictional Alpaca payload contracts, wholly offline; no credentials/HTTP."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import date

from schd_monitor.alpaca_offline_contract import AlpacaDailyRequest, check_alpaca_offline_page
from schd_monitor.models import MonitorDataError


def request():
    return AlpacaDailyRequest(symbol="MOCK", start=date(2038, 6, 10),
                              end=date(2038, 6, 14), symbol_asof=date(2038, 6, 14),
                              feed="sip", adjustment="split")


def response():
    return {"symbol": "MOCK", "bars": [
        {"t": "2038-06-10T04:00:00Z", "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5,
         "v": 1250, "n": 42, "vw": 10.3},
        {"t": "2038-06-14T04:00:00Z", "o": 11.0, "h": 12.0, "l": 10.0, "c": 11.5,
         "v": 1380, "n": 39, "vw": 11.3},
    ], "next_page_token": None}


class AlpacaOfflineContractTests(unittest.TestCase):
    def test_explicit_request_contract_and_no_credentials(self):
        r = request()
        self.assertEqual(r.params(), {"timeframe": "1Day", "start": "2038-06-10",
                                      "end": "2038-06-14", "asof": "2038-06-14",
                                      "feed": "sip", "adjustment": "split",
                                      "sort": "asc", "limit": "10000"})
        self.assertEqual(len(r.request_sha256()), 64)
        self.assertNotIn("api", str(r.params()).lower())

    def test_matching_inclusive_start_end_page_stays_unattested(self):
        r = request()
        page = check_alpaca_offline_page(request=r, http_status=200, payload=response())
        self.assertEqual([b.session.isoformat() for b in page.bars], ["2038-06-10", "2038-06-14"])
        self.assertEqual(page.as_json()["bars_count"], 2)
        self.assertNotIn("bars", page.as_json())
        self.assertEqual(page.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(page.provider_authenticated)
        self.assertFalse(page.feed_entitlement_verified)
        self.assertFalse(page.split_adjustment_verified)
        self.assertFalse(page.session_coverage_verified)
        self.assertFalse(page.vendor_bar_finality_verified)
        self.assertFalse(page.production_signals_verified)

    def test_reject_wrong_feed_or_adjustment(self):
        for attr, bad in (("feed", "iex"), ("feed", "SIP"),
                          ("adjustment", "raw"), ("adjustment", "all")):
            with self.subTest(attr=attr, bad=bad):
                with self.assertRaises(MonitorDataError):
                    replace(request(), **{attr: bad})

    def test_reject_invalid_symbol_dates_and_schema(self):
        for patch in ({"symbol": "mock"}, {"symbol": "MOCK?bad"},
                      {"start": "2038-06-10"}, {"end": date(2038, 6, 9)},
                      {"symbol_asof": "today"}, {"schema_version": True},
                      {"schema_version": 2}):
            with self.subTest(patch=patch):
                with self.assertRaises(MonitorDataError):
                    replace(request(), **patch)

    def test_non_200_is_never_fallback_especially_denied_rate_limited(self):
        for status in (400, 401, 403, 404, 429, 500, 503, True):
            with self.subTest(status=status):
                with self.assertRaisesRegex(MonitorDataError, "HTTP status"):
                    check_alpaca_offline_page(request=request(), http_status=status, payload=response())

    def test_wrong_incomplete_or_unexpected_envelope_fails(self):
        for patch in ({"symbol": "OTHER"}, {"bars": []}, {"bars": None},
                      {"next_page_token": "more"}, {"next_page_token": ""},
                      {"next_page_token": 0}, {"unexpected": "field"}):
            with self.subTest(patch=patch):
                data = response()
                data.update(patch)
                with self.assertRaises(MonitorDataError):
                    check_alpaca_offline_page(request=request(), http_status=200, payload=data)
        data = response()
        del data["next_page_token"]
        with self.assertRaises(MonitorDataError):
            check_alpaca_offline_page(request=request(), http_status=200, payload=data)

    def test_invalid_request_or_non_json_envelope_fails(self):
        for bad in (None, {}, "SIP"):
            with self.assertRaises(MonitorDataError):
                check_alpaca_offline_page(request=bad, http_status=200, payload=response())
        for bad in (None, [], "oops"):
            with self.assertRaises(MonitorDataError):
                check_alpaca_offline_page(request=request(), http_status=200, payload=bad)

    def test_bar_missing_fields_and_bad_prices_rejected(self):
        for key, bad in (("o", True), ("h", "11"), ("l", float("nan")),
                         ("c", -1), ("t", 123)):
            with self.subTest(key=key, bad=bad):
                data = copy.deepcopy(response())
                data["bars"][0][key] = bad
                with self.assertRaises(MonitorDataError):
                    check_alpaca_offline_page(request=request(), http_status=200, payload=data)
        data = response()
        del data["bars"][0]["o"]
        with self.assertRaises(MonitorDataError):
            check_alpaca_offline_page(request=request(), http_status=200, payload=data)

    def test_documented_volume_and_trade_count_required_with_valid_values(self):
        for field in ("v", "n"):
            with self.subTest(missing=field):
                bad = copy.deepcopy(response())
                del bad["bars"][0][field]
                with self.assertRaisesRegex(MonitorDataError, "missing required fields"):
                    check_alpaca_offline_page(request=request(), http_status=200, payload=bad)
        valid = response()
        valid["bars"][0]["v"] = 0.5  # decimal split-adjusted volume shape
        checked = check_alpaca_offline_page(request=request(), http_status=200, payload=valid)
        self.assertEqual(checked.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(checked.split_adjustment_verified)

    def test_published_historical_bar_requires_vwap_on_every_row(self):
        # Alpaca marks vw required, unlike Massive's optional vw field.
        for index in (0, 1):
            with self.subTest(bar_index=index):
                incomplete = copy.deepcopy(response())
                del incomplete["bars"][index]["vw"]
                with self.assertRaisesRegex(MonitorDataError, "missing required fields"):
                    check_alpaca_offline_page(
                        request=request(), http_status=200, payload=incomplete
                    )
        result = check_alpaca_offline_page(
            request=request(), http_status=200, payload=response()
        )
        self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        self.assertFalse(result.provider_authenticated)

    def test_invalid_activity_values_and_vwap_fail_closed(self):
        for key, bad_value in (
            ("v", True), ("v", "120"), ("v", -1), ("v", float("inf")),
            ("v", 10**1000), ("n", True), ("n", "2"), ("n", 1.5),
            ("n", -1), ("vw", None), ("vw", "10.2"), ("vw", False),
            ("vw", 0), ("vw", -1), ("vw", float("nan")),
            ("vw", 10**1000),
        ):
            with self.subTest(field=key, value=str(bad_value)[:15]):
                bad = copy.deepcopy(response())
                bad["bars"][0][key] = bad_value
                with self.assertRaises(MonitorDataError):
                    check_alpaca_offline_page(request=request(), http_status=200, payload=bad)

    def test_unreviewed_daily_bar_fields_fail_closed_before_sha_or_signal(self):
        for unexpected in ("future_vendor_field", "unreviewed_timestamp", "api_key"):
            for index in (0, 1):
                with self.subTest(unexpected=unexpected, bar_index=index):
                    data = copy.deepcopy(response())
                    original = copy.deepcopy(data)
                    data["bars"][index][unexpected] = "fictional-untrusted-value"
                    with self.assertRaisesRegex(MonitorDataError, "unexpected fields"):
                        check_alpaca_offline_page(
                            request=request(), http_status=200, payload=data
                        )
                    # The validator must never edit the caller's supplied data.
                    self.assertEqual(data["bars"][index][unexpected],
                                     "fictional-untrusted-value")
                    self.assertEqual(original, response())

    def test_utc_timestamp_must_represent_new_york_midnight(self):
        for bad in ("2038-06-10T00:00:00Z", "2038-06-10T04:01:00Z",
                    "2038-06-10", "2038-06-10T00:00:00-04:00", "not a timestamp"):
            with self.subTest(bad=bad):
                data = copy.deepcopy(response())
                data["bars"][0]["t"] = bad
                with self.assertRaises(MonitorDataError):
                    check_alpaca_offline_page(request=request(), http_status=200, payload=data)

    def test_session_outside_inclusive_dates_fails(self):
        data = copy.deepcopy(response())
        data["bars"][0]["t"] = "2038-06-09T04:00:00Z"
        with self.assertRaises(MonitorDataError):
            check_alpaca_offline_page(request=request(), http_status=200, payload=data)

    def test_reordered_or_duplicate_bars_fail(self):
        data = copy.deepcopy(response())
        data["bars"] = list(reversed(data["bars"]))
        with self.assertRaises(MonitorDataError):
            check_alpaca_offline_page(request=request(), http_status=200, payload=data)
        data = copy.deepcopy(response())
        data["bars"].append(copy.deepcopy(data["bars"][-1]))
        with self.assertRaises(MonitorDataError):
            check_alpaca_offline_page(request=request(), http_status=200, payload=data)

    def test_response_hash_changes_with_prices_and_no_input_mutation(self):
        data = response()
        before = copy.deepcopy(data)
        page = check_alpaca_offline_page(request=request(), http_status=200, payload=data)
        newer = copy.deepcopy(data)
        newer["bars"][0]["o"] += 0.01
        page2 = check_alpaca_offline_page(request=request(), http_status=200, payload=newer)
        self.assertEqual(data, before)
        self.assertNotEqual(page.response_sha256, page2.response_sha256)
        self.assertEqual(page.request_sha256, page2.request_sha256)


if __name__ == "__main__":
    unittest.main()
