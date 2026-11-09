"""Fictional Sharadar FUNDS field-contract tests; no vendor data or HTTP."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import date

from schd_monitor.models import MonitorDataError
from schd_monitor.sharadar_funds_offline_contract import (
    MAX_SHARADAR_FUNDS_ROWS,
    SharadarFundsRequest,
    review_sharadar_funds_offline_rows,
)


def request():
    return SharadarFundsRequest(
        symbol="MOCK", start=date(2038, 6, 10), end=date(2038, 6, 14),
    )


def rows():
    return [
        {"ticker": "MOCK", "date": "2038-06-10", "open": 10.0, "high": 11.0,
         "low": 9.0, "close": 10.5, "volume": 1250, "lastupdated": "2038-06-12"},
        {"ticker": "MOCK", "date": "2038-06-14", "open": 10.2, "high": 12.0,
         "low": 10.0, "close": 11.5, "volume": 1300, "lastupdated": "2038-06-15"},
    ]


_OMITTED = object()


class SharadarFundsOfflineContractTests(unittest.TestCase):
    def check(self, data=_OMITTED):
        # Explicit None is invalid input; only an omitted argument uses fixtures.
        return review_sharadar_funds_offline_rows(
            request=request(), rows=rows() if data is _OMITTED else data,
        )

    def test_inert_documented_funds_query_has_no_key_or_url(self):
        spec = request()
        self.assertEqual(spec.inert_fields(), {
            "ticker": "MOCK", "from": "2038-06-10", "to": "2038-06-14",
            "fields": "ticker,date,open,high,low,close,volume,lastupdated",
            "sort": "date.asc", "format": "json", "limit": "10000", "skip": "0",
        })
        self.assertNotIn("api_key", str(spec.inert_fields()))
        self.assertNotIn("http", str(spec.inert_fields()))
        self.assertEqual(len(spec.request_sha256()), 64)

    def test_fictional_rows_yield_only_unattested_schema_result(self):
        result = self.check()
        self.assertEqual([b.session.isoformat() for b in result.bars],
                         ["2038-06-10", "2038-06-14"])
        self.assertEqual(len(result.fictional_rows_sha256), 64)
        self.assertEqual(result.as_json()["bars_count"], 2)
        self.assertNotIn("bars", result.as_json())
        self.assertEqual(result.trading_status, "UNVERIFIED_DO_NOT_TRADE")
        for flag in ("provider_authenticated", "market_data_entitlement_verified",
                     "split_adjustment_verified", "session_coverage_verified",
                     "source_independence_verified", "vendor_bar_finality_verified",
                     "production_signals_verified"):
            self.assertFalse(getattr(result, flag))

    def test_reject_invalid_requests(self):
        base = request()
        for change in ({"symbol": "mock"}, {"symbol": "MOCK?api_key"},
                       {"symbol": "A" * 16}, {"start": "2038-06-10"},
                       {"end": date(2038, 6, 9)}, {"schema_version": True},
                       {"schema_version": 2}):
            with self.subTest(change=change), self.assertRaises(MonitorDataError):
                replace(base, **change)

    def test_reject_missing_extra_and_dividend_adjusted_fields(self):
        for name in ("ticker", "date", "open", "high", "low", "close",
                     "volume", "lastupdated"):
            with self.subTest(missing=name):
                data = rows()
                del data[0][name]
                with self.assertRaisesRegex(MonitorDataError, "fields"):
                    self.check(data)
        for extra in ("closeadj", "closeunadj", "surprise"):
            with self.subTest(extra=extra):
                data = rows()
                data[0][extra] = 123.0
                with self.assertRaisesRegex(MonitorDataError, "fields"):
                    self.check(data)

    def test_reject_wrong_ticker_invalid_dates_and_unsorted_rows(self):
        for key, value, message in (
            ("ticker", "OTHER", "ticker"),
            ("date", "2038-06-09", "outside"),
            ("date", "2038-06-10T00:00:00", "date"),
            ("date", "2038-06-13", "Weekend"),
            ("lastupdated", "20380612", "lastupdated"),
            ("lastupdated", None, "lastupdated"),
        ):
            with self.subTest(key=key, value=value):
                data = rows()
                data[0][key] = value
                with self.assertRaisesRegex(MonitorDataError, message):
                    self.check(data)
        for data in ([rows()[1], rows()[0]], [rows()[0], rows()[0]]):
            with self.assertRaisesRegex(MonitorDataError, "sort order or duplicate"):
                self.check(data)

    def test_reject_nonfinite_boolean_text_or_inconsistent_prices(self):
        for field, value in (
            ("open", True), ("close", "10.5"), ("high", float("inf")),
            ("low", float("nan")), ("volume", -1), ("volume", "100"),
            ("volume", True), ("close", 11.5),
        ):
            with self.subTest(field=field, value=value):
                data = rows()
                data[0][field] = value
                with self.assertRaises(MonitorDataError):
                    self.check(data)

    def test_reject_missing_or_ambiguous_full_page(self):
        for bad in (None, [], {}, "not rows", [rows()[0]] * MAX_SHARADAR_FUNDS_ROWS):
            with self.subTest(sample_type=type(bad).__name__):
                with self.assertRaises(MonitorDataError):
                    self.check(bad)

    def test_reject_nontyped_request_or_nonmapping_row(self):
        with self.assertRaisesRegex(MonitorDataError, "Typed Sharadar"):
            review_sharadar_funds_offline_rows(request=None, rows=rows())
        with self.assertRaisesRegex(MonitorDataError, "row fields"):
            self.check([["not", "a", "mapping"]])
        self.assertEqual(self.check().status, "SHARADAR_FUNDS_ROWS_SHAPE_UNATTESTED")


if __name__ == "__main__":
    unittest.main()
