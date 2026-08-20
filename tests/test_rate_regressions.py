import json
import math
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch

from rate_service import (
    MAX_HTTP_RESPONSE_BYTES,
    RateService,
    RateSnapshot,
    fiat_daily_changes,
    relative_rate_change,
)


VALID_SOURCE_UNIX = int(datetime(2026, 8, 18, 12, tzinfo=timezone.utc).timestamp())


def make_snapshot(**overrides):
    values = {
        "rates": {"USD": 1.0, "CNY": 7.2},
        "names": {"USD": "美元", "CNY": "人民币"},
        "kinds": {"USD": "fiat", "CNY": "fiat"},
        "changes": {"USD": 0.0, "CNY": 0.5},
        "fetched_at": "2026-08-18T00:00:00+00:00",
        "fiat_updated_at": "2026-08-17T00:00:00+00:00",
        "errors": [],
        "coin_ids": {},
    }
    values.update(overrides)
    return RateSnapshot(**values)


def history_rows(cny_rate=7.3):
    return [
        {"date": "2026-08-17", "base": "USD", "quote": "CNY", "rate": 7.2},
        {"date": "2026-08-18", "base": "USD", "quote": "CNY", "rate": cny_rate},
    ]


class OfflineSession:
    def get(self, *_args, **_kwargs):
        raise OSError("offline-test")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class StreamingResponse:
    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        del chunk_size
        yield from self.chunks

    def close(self):
        self.closed = True


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        return value if tz is not None else value.replace(tzinfo=None)


class RateApiValidationRegressionTests(unittest.TestCase):
    def test_truncated_streamed_json_preserves_the_last_snapshot(self):
        class TruncatedSession:
            def get(self, url, **_kwargs):
                if "frankfurter" in url:
                    return FakeResponse(history_rows())
                return StreamingResponse([b'{"result":"success","rates":'])

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            old = make_snapshot()
            service.snapshot = old
            service.session = TruncatedSession()

            with self.assertRaises(ConnectionError):
                service.refresh("fiat")

            self.assertIs(service.snapshot, old)

    def test_declared_oversized_http_body_is_rejected_before_streaming(self):
        response = StreamingResponse(
            [b"must-not-be-read"],
            {"Content-Length": str(MAX_HTTP_RESPONSE_BYTES + 1)},
        )

        class OversizedSession:
            def get(self, *_args, **_kwargs):
                return response

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.session = OversizedSession()

            with self.assertRaisesRegex(ValueError, "过大"):
                service._fetch_fiat_payload()

            self.assertTrue(response.closed)

    def test_fiat_refresh_rejects_a_non_usd_based_payload(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            old = make_snapshot()
            service.snapshot = old
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 2.0, "CNY": 14.4},
                "time_last_update_unix": VALID_SOURCE_UNIX,
            }
            service._fetch_fiat_history_rows = history_rows

            with self.assertRaises(ConnectionError):
                service.refresh("fiat")

            self.assertIs(service.snapshot, old)
            self.assertEqual(service.snapshot.rates["CNY"], 7.2)

    def test_fiat_refresh_does_not_treat_json_booleans_as_rates(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            old = make_snapshot()
            service.snapshot = old
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": True, "CNY": 7.3},
                "time_last_update_unix": VALID_SOURCE_UNIX,
            }
            service._fetch_fiat_history_rows = history_rows

            with self.assertRaises(ConnectionError):
                service.refresh("fiat")

            self.assertIs(service.snapshot, old)

    def test_fiat_refresh_ignores_invalid_currency_codes(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3, "NOT A CODE": 99},
                "time_last_update_unix": VALID_SOURCE_UNIX,
            }
            service._fetch_fiat_history_rows = history_rows

            snapshot = service.refresh("fiat")

            self.assertEqual(snapshot.rates["CNY"], 7.3)
            self.assertNotIn("NOT A CODE", snapshot.rates)

    def test_invalid_source_timestamp_does_not_discard_valid_rates(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": 10**30,
            }
            service._fetch_fiat_history_rows = history_rows

            snapshot = service.refresh("fiat")

            self.assertEqual(snapshot.rates["CNY"], 7.3)
            self.assertEqual(snapshot.fiat_updated_at, "2026-08-17T00:00:00+00:00")
            self.assertTrue(any("时间戳" in error for error in snapshot.errors))

    def test_older_source_batch_cannot_replace_newer_cached_fiat(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            old = make_snapshot(
                rates={"USD": 1.0, "CNY": 7.4},
                fiat_updated_at="2026-08-18T12:00:00+00:00",
            )
            service.snapshot = old
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.1},
                "time_last_update_unix": int(
                    datetime(2026, 8, 17, 12, tzinfo=timezone.utc).timestamp()
                ),
            }
            service._fetch_fiat_history_rows = history_rows

            with self.assertRaisesRegex(ConnectionError, "早于本地缓存"):
                service.refresh("fiat")

            self.assertIs(service.snapshot, old)
            self.assertEqual(service.snapshot.rates["CNY"], 7.4)

    def test_future_source_timestamp_does_not_claim_future_freshness(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": int(
                    datetime(2026, 8, 23, tzinfo=timezone.utc).timestamp()
                ),
            }
            service._fetch_fiat_history_rows = history_rows

            with patch("rate_service.datetime", FrozenDateTime):
                snapshot = service.refresh("fiat")

            self.assertEqual(snapshot.rates["CNY"], 7.3)
            self.assertEqual(snapshot.fiat_updated_at, "2026-08-17T00:00:00+00:00")
            self.assertTrue(any("时间戳" in error for error in snapshot.errors))

    def test_missing_source_timestamp_is_reported_as_partial_freshness(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3},
            }
            service._fetch_fiat_history_rows = history_rows

            snapshot = service.refresh("fiat")

            self.assertEqual(snapshot.rates["CNY"], 7.3)
            self.assertEqual(snapshot.fiat_updated_at, "2026-08-17T00:00:00+00:00")
            self.assertTrue(any("缺少更新时间" in error for error in snapshot.errors))

    def test_valid_crypto_rows_survive_malformed_siblings(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_crypto_rows = lambda: [
                {"symbol": "BTCUSDT", "lastPrice": "50000", "openPrice": "49000"},
                None,
                {"symbol": "ETHUSDT", "lastPrice": "nan", "openPrice": "3000"},
            ]
            service.session = OfflineSession()

            snapshot = service.refresh("crypto")

            self.assertAlmostEqual(snapshot.rates["BTC"], 1 / 50000)
            self.assertNotIn("ETH", snapshot.rates)

    def test_failed_crypto_source_does_not_inject_synthetic_usdt(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot(
                rates={"USD": 1.0, "CNY": 7.2, "ETH": 1 / 3000},
                names={"USD": "美元", "CNY": "人民币", "ETH": "Ethereum"},
                kinds={"USD": "fiat", "CNY": "fiat", "ETH": "crypto"},
                changes={"USD": 0.0, "CNY": 0.5, "ETH": 1.0},
            )
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": VALID_SOURCE_UNIX,
            }
            service._fetch_fiat_history_rows = history_rows
            service._fetch_crypto_rows = lambda: [{"symbol": "UNKNOWNUSDT", "lastPrice": "1"}]
            service.session = OfflineSession()

            snapshot = service.refresh("all")

            self.assertEqual(snapshot.rates["CNY"], 7.3)
            self.assertIn("ETH", snapshot.rates)
            self.assertNotIn("USDT", snapshot.rates)
            self.assertTrue(any(error.startswith("虚拟币：") for error in snapshot.errors))

    def test_partial_history_payload_preserves_last_known_daily_change(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": VALID_SOURCE_UNIX,
            }
            service._fetch_fiat_history_rows = lambda: history_rows()[:1]

            snapshot = service.refresh("fiat")

            self.assertEqual(snapshot.changes["CNY"], 0.5)
            self.assertTrue(any("法币24h数据" in error for error in snapshot.errors))

    def test_partial_fiat_payload_preserves_missing_cached_currencies(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot(
                rates={"USD": 1.0, "CNY": 7.2, "EUR": 0.9},
                names={"USD": "美元", "CNY": "人民币", "EUR": "欧元"},
                kinds={"USD": "fiat", "CNY": "fiat", "EUR": "fiat"},
                changes={"USD": 0.0, "CNY": 0.5, "EUR": -0.1},
            )
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": VALID_SOURCE_UNIX,
            }
            service._fetch_fiat_history_rows = history_rows

            snapshot = service.refresh("fiat")

            self.assertEqual(snapshot.rates["EUR"], 0.9)
            self.assertEqual(snapshot.changes["EUR"], -0.1)
            self.assertTrue(any("缺少 1 个" in error for error in snapshot.errors))

    def test_binance_refresh_records_reliable_chart_identifiers(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_crypto_rows = lambda: [
                {"symbol": "BCHUSDT", "lastPrice": "600", "openPrice": "590"}
            ]

            snapshot = service.refresh("crypto")

            self.assertEqual(snapshot.coin_ids["BCH"], "bitcoin-cash")

    def test_binance_rates_are_calibrated_against_the_usdc_usdt_pair(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_crypto_rows = lambda: [
                {"symbol": "USDCUSDT", "lastPrice": "0.98", "openPrice": "1.00"},
                {"symbol": "BTCUSDT", "lastPrice": "49000", "openPrice": "50000"},
            ]

            snapshot = service.refresh("crypto")

            self.assertEqual(snapshot.rates["USDC"], 1.0)
            self.assertEqual(snapshot.rates["USDT"], 0.98)
            self.assertEqual(snapshot.rates["BTC"], 0.98 / 49000)
            self.assertAlmostEqual(snapshot.changes["USDC"], 0.0)
            self.assertAlmostEqual(snapshot.changes["BTC"], 0.0)

    def test_extreme_fallback_coin_does_not_discard_valid_siblings(self):
        class FallbackSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse([
                    {
                        "id": "absurd-coin",
                        "symbol": "absurd",
                        "name": "Absurd",
                        "current_price": 5e-324,
                        "price_change_percentage_24h": 1.0,
                    },
                    {
                        "id": "bitcoin",
                        "symbol": "btc",
                        "name": "Bitcoin",
                        "current_price": 360_000.0,
                        "price_change_percentage_24h": 2.0,
                    },
                ])

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_crypto_rows = lambda: (_ for _ in ()).throw(OSError("primary-offline"))
            service.session = FallbackSession()

            snapshot = service.refresh("crypto")

            self.assertNotIn("ABSURD", snapshot.rates)
            self.assertEqual(snapshot.rates["BTC"], 7.2 / 360_000.0)
            self.assertAlmostEqual(snapshot.changes["BTC"], (1.02 / 1.005 - 1) * 100)


class RateMathRegressionTests(unittest.TestCase):
    def test_conversion_avoids_decimal_noise_and_intermediate_overflow(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot(
                rates={"USD": 1.0, "THREE": 3.0, "SMALL": 1e-308, "PEER": 1e-308},
                names={},
                kinds={},
                changes={},
            )

            self.assertEqual(service.convert(0.1, "USD", "THREE"), 0.3)
            self.assertEqual(service.convert(1e308, "SMALL", "PEER"), 1e308)

    def test_change_helpers_ignore_nonfinite_and_invalid_inputs(self):
        changes = fiat_daily_changes([
            {"date": "2026-08-17", "base": "USD", "quote": "CNY", "rate": 7.2},
            {"date": "2026-08-18", "base": "USD", "quote": "CNY", "rate": math.inf},
            {"date": "not-a-date", "base": "USD", "quote": "EUR", "rate": 0.9},
            {"date": "2026-08-18", "base": "USD", "quote": "EUR", "rate": 0.8},
        ])

        self.assertNotIn("CNY", changes)
        self.assertNotIn("EUR", changes)
        self.assertIsNone(relative_rate_change(math.nan, 1.0))
        self.assertIsNone(relative_rate_change(1.0, math.inf))
        self.assertNotIn("XTS", fiat_daily_changes([
            {"date": "2026-08-17", "base": "USD", "quote": "XTS", "rate": 5e-324},
            {"date": "2026-08-18", "base": "USD", "quote": "XTS", "rate": 1e308},
        ]))
        self.assertIsNone(relative_rate_change(-99.99999999999999, 1e308))


class RateCacheRegressionTests(unittest.TestCase):
    def test_cache_without_the_usd_anchor_is_rejected(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            (data_dir / "rates_cache.json").write_text(json.dumps({
                "rates": {"CNY": 7.2, "EUR": 0.9},
                "names": {},
                "kinds": {"CNY": "fiat", "EUR": "fiat"},
                "changes": {},
            }), encoding="utf-8")

            self.assertFalse(RateService(data_dir).snapshot.rates)

    def test_cache_timestamps_are_sanitized_on_load(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            payload = {
                "rates": {"USD": 1.0, "CNY": 7.2},
                "names": {},
                "kinds": {"USD": "fiat", "CNY": "fiat"},
                "changes": {},
                "fetched_at": "not-a-timestamp",
                "fiat_updated_at": "also-invalid",
            }
            (data_dir / "rates_cache.json").write_text(json.dumps(payload), encoding="utf-8")

            snapshot = RateService(data_dir).snapshot

            self.assertEqual(snapshot.fetched_at, "")
            self.assertEqual(snapshot.fiat_updated_at, "")

    def test_future_cache_timestamp_is_not_reported_as_fresh(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            payload = {
                "rates": {"USD": 1.0, "CNY": 7.2},
                "names": {},
                "kinds": {"USD": "fiat", "CNY": "fiat"},
                "changes": {},
                "fetched_at": "2026-08-23T00:00:00+00:00",
                "fiat_updated_at": "2026-08-23T00:00:00+00:00",
            }
            (data_dir / "rates_cache.json").write_text(json.dumps(payload), encoding="utf-8")

            with patch("rate_service.datetime", FrozenDateTime):
                snapshot = RateService(data_dir).snapshot

            self.assertEqual(snapshot.fetched_at, "")
            self.assertEqual(snapshot.fiat_updated_at, "")

    def test_invalid_change_does_not_discard_otherwise_valid_cache(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            payload = {
                "rates": {"USD": 1.0, "CNY": 7.2},
                "names": {},
                "kinds": {"USD": "fiat", "CNY": "fiat"},
                "changes": {"CNY": 10**4_000},
                "fetched_at": "2026-08-18T00:00:00+00:00",
            }
            (data_dir / "rates_cache.json").write_text(json.dumps(payload), encoding="utf-8")

            snapshot = RateService(data_dir).snapshot

            self.assertEqual(snapshot.rates["CNY"], 7.2)
            self.assertIsNone(snapshot.changes["CNY"])

    def test_invalid_optional_kind_does_not_discard_valid_cache_rates(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            payload = {
                "rates": {"USD": 1.0, "CNY": 7.2, "CUSTOM": 2.0},
                "names": {},
                "kinds": {"CUSTOM": []},
                "changes": {},
            }
            (data_dir / "rates_cache.json").write_text(json.dumps(payload), encoding="utf-8")

            snapshot = RateService(data_dir).snapshot

            self.assertEqual(snapshot.rates["CNY"], 7.2)
            self.assertEqual(snapshot.kinds["USD"], "fiat")
            self.assertNotIn("CUSTOM", snapshot.kinds)

    def test_nonfinite_snapshot_never_replaces_a_valid_cache(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            service = RateService(data_dir)
            service.save_cache(make_snapshot())
            original = service.cache_path.read_bytes()

            service.save_cache(make_snapshot(changes={"USD": math.nan, "CNY": 0.5}))

            self.assertEqual(service.cache_path.read_bytes(), original)
            self.assertNotIn(b"NaN", service.cache_path.read_bytes())
            self.assertFalse((data_dir / "rates_cache.tmp").exists())

    def test_oversized_rate_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            raw = json.dumps({"rates": {"USD": 1.0}, "names": {}, "kinds": {}, "changes": {}})
            (data_dir / "rates_cache.json").write_text(
                raw + (" " * (8 * 1024 * 1024)), encoding="utf-8"
            )

            self.assertFalse(RateService(data_dir).snapshot.rates)

    def test_configured_cache_limit_is_a_hard_total_limit(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            payload = {
                "rates": {"USD": 1.0, "CNY": 7.2},
                "names": {},
                "kinds": {"USD": "fiat", "CNY": "fiat"},
                "changes": {},
                "padding": "x" * (2 * 1024 * 1024),
            }
            cache_path = data_dir / "rates_cache.json"
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            service = RateService(data_dir)

            service.set_cache_limit(1)

            self.assertLessEqual(service.cache_size_bytes(), 1024 * 1024)
            self.assertFalse(cache_path.exists())

    def test_offline_refresh_preserves_cache_and_reports_the_cause(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            old = make_snapshot()
            service.snapshot = old
            service.save_cache(old)
            original = service.cache_path.read_bytes()
            service.session = OfflineSession()

            with self.assertRaisesRegex(ConnectionError, "offline-test"):
                service.refresh("all")

            self.assertIs(service.snapshot, old)
            self.assertEqual(service.cache_path.read_bytes(), original)

    def test_first_run_offline_error_says_that_no_cache_exists(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.session = OfflineSession()

            with self.assertRaisesRegex(ConnectionError, "没有可用缓存"):
                service.refresh("all")

            self.assertFalse(service.snapshot.rates)

    def test_cache_write_failure_marks_an_online_update_as_session_only(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": VALID_SOURCE_UNIX,
            }
            service._fetch_fiat_history_rows = history_rows

            def fail_write(*_args, **_kwargs):
                raise OSError("disk-full")

            service._atomic_write_json = fail_write

            snapshot = service.refresh("fiat")

            self.assertEqual(snapshot.rates["CNY"], 7.3)
            self.assertTrue(any("仅在当前会话有效" in error for error in snapshot.errors))


class RateChartRegressionTests(unittest.TestCase):
    def test_known_coin_chart_id_cannot_be_poisoned_by_cache_metadata(self):
        requested_urls: list[str] = []
        binance_calls: list[tuple] = []

        class ChartSession:
            def get(self, url, **_kwargs):
                requested_urls.append(url)
                return FakeResponse({"prices": [[1000, 10.0], [2000, 11.0]]})

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot(coin_ids={"BTC": "ethereum"})
            service._fetch_binance_chart = lambda *args: binance_calls.append(args)
            service.session = ChartSession()

            points = service.fetch_market_chart("BTC", 7)

            self.assertTrue(any("/coins/bitcoin/" in url for url in requested_urls))
            self.assertEqual(points, [(1000, 10.0), (2000, 11.0)])
            self.assertFalse(binance_calls)

    def test_market_chart_skips_bad_live_points_and_sorts_the_rest(self):
        class ChartService(RateService):
            def _fetch_binance_chart(self, _code, _days):
                raise OSError("binance-offline")

        class ChartSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse({
                    "prices": [
                        [2000, 20],
                        None,
                        [1000, 10],
                        [3000, "NaN"],
                        [4000, -1],
                    ]
                })

        with tempfile.TemporaryDirectory() as path:
            service = ChartService(Path(path))
            service.snapshot = make_snapshot(coin_ids={"BTC": "bitcoin"})
            service.session = ChartSession()

            self.assertEqual(service.fetch_market_chart("BTC", 7), [(1000, 10.0), (2000, 20.0)])

    def test_poisoned_market_chart_cache_is_not_returned_offline(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            (data_dir / "chart_bitcoin_7.json").write_text(
                "[[1000, 10.0], [2000, NaN]]", encoding="utf-8"
            )
            service = RateService(data_dir)
            service.snapshot = make_snapshot(coin_ids={"BTC": "bitcoin"})
            service.session = OfflineSession()

            with self.assertRaises(ConnectionError):
                service.fetch_market_chart("BTC", 7)

    def test_binance_chart_skips_malformed_rows(self):
        class ChartSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse([
                    [2000, "", "", "", "12"],
                    ["bad"],
                    [1000, "", "", "", "10"],
                    [3000, "", "", "", "inf"],
                ])

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service.session = ChartSession()

            self.assertEqual(service._fetch_binance_chart("BTC", 7), [(1000, 72.0), (2000, 86.4)])

    def test_binance_chart_uses_the_snapshot_usdt_usd_calibration(self):
        class ChartSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse([
                    [1000, "", "", "", "49000"],
                    [2000, "", "", "", "50000"],
                ])

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot(
                rates={"USD": 1.0, "CNY": 7.0, "USDT": 0.98, "BTC": 0.98 / 49000},
                names={},
                kinds={},
                changes={},
            )
            service.session = ChartSession()

            points = service._fetch_binance_chart("BTC", 7)

            self.assertEqual(points, [(1000, 350000.0), (2000, 50000 / 0.98 * 7.0)])

    def test_offline_fiat_chart_uses_a_finite_flat_snapshot_fallback(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            service.session = OfflineSession()

            points = service.fetch_fiat_chart("USD", 7, "CNY")

            self.assertEqual(len(points), 2)
            self.assertEqual([value for _, value in points], [7.2, 7.2])
            self.assertLess(points[0][0], points[1][0])

    def test_offline_fiat_chart_rejects_unrenderable_extreme_fallback(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot(
                rates={"USD": 1.0, "XTS": 1e308},
                names={"USD": "美元", "XTS": "Extreme"},
                kinds={"USD": "fiat", "XTS": "fiat"},
                changes={},
            )
            service.session = OfflineSession()

            with self.assertRaisesRegex(ValueError, "行情数据不足"):
                service.fetch_fiat_chart("USD", 7, "XTS")

    def test_chart_cache_discards_timestamps_outside_the_renderable_range(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            cache_path = data_dir / "chart_bitcoin_7.json"
            cache_path.write_text(
                json.dumps([
                    [1_700_000_000_000, 100.0],
                    [1_700_000_001_000, 101.0],
                    [10**100, 999.0],
                    [1_700_000_002_000, 1.7e308],
                ]),
                encoding="utf-8",
            )
            service = RateService(data_dir)

            self.assertEqual(
                service._read_chart_cache(cache_path),
                [(1_700_000_000_000, 100.0), (1_700_000_001_000, 101.0)],
            )


class RateConcurrencyRegressionTests(unittest.TestCase):
    def test_repeated_parallel_refreshes_leave_one_valid_atomic_cache(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            service = RateService(data_dir)
            service.snapshot = make_snapshot()
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": VALID_SOURCE_UNIX,
            }
            service._fetch_fiat_history_rows = history_rows

            with ThreadPoolExecutor(max_workers=8) as executor:
                snapshots = list(executor.map(lambda _index: service.refresh("fiat"), range(80)))

            self.assertTrue(all(snapshot.rates["CNY"] == 7.3 for snapshot in snapshots))
            self.assertFalse(service._refresh_flights)
            self.assertFalse(list(data_dir.glob("*.tmp*")))
            cached = RateService(data_dir).snapshot
            self.assertEqual(cached.rates["CNY"], 7.3)

    def test_same_scope_refreshes_share_one_inflight_network_batch(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            started = Event()
            release = Event()
            calls = {"fiat": 0, "history": 0}

            def fetch_fiat():
                calls["fiat"] += 1
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release fiat source")
                return {
                    "result": "success",
                    "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": VALID_SOURCE_UNIX,
                }

            def fetch_history():
                calls["history"] += 1
                return history_rows()

            service._fetch_fiat_payload = fetch_fiat
            service._fetch_fiat_history_rows = fetch_history

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(service.refresh, "fiat")
                self.assertTrue(started.wait(5))
                flight = service._refresh_flights["fiat"]
                waiter_joined = Event()
                original_wait = flight.done.wait

                def observed_wait(*args, **kwargs):
                    waiter_joined.set()
                    return original_wait(*args, **kwargs)

                flight.done.wait = observed_wait
                second = executor.submit(service.refresh, "fiat")
                self.assertTrue(waiter_joined.wait(5))
                release.set()
                first_snapshot = first.result(timeout=5)
                second_snapshot = second.result(timeout=5)

            self.assertIs(first_snapshot, second_snapshot)
            self.assertEqual(calls, {"fiat": 1, "history": 1})

    def test_older_overlapping_all_refresh_cannot_overwrite_newer_fiat(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            old_started = Event()
            release_old = Event()
            call_count = 0

            def fetch_fiat():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    old_started.set()
                    if not release_old.wait(5):
                        raise TimeoutError("test did not release old response")
                    cny = 7.1
                else:
                    cny = 7.4
                return {
                    "result": "success",
                    "rates": {"USD": 1.0, "CNY": cny},
                    "time_last_update_unix": VALID_SOURCE_UNIX,
                }

            service._fetch_fiat_payload = fetch_fiat
            service._fetch_fiat_history_rows = history_rows
            service._fetch_crypto_rows = lambda: [
                {"symbol": "BTCUSDT", "lastPrice": "50000", "openPrice": "49000"}
            ]

            with ThreadPoolExecutor(max_workers=2) as executor:
                older_all = executor.submit(service.refresh, "all")
                self.assertTrue(old_started.wait(5))
                newer_fiat = executor.submit(service.refresh, "fiat")
                self.assertEqual(newer_fiat.result(timeout=5).rates["CNY"], 7.4)
                release_old.set()
                merged = older_all.result(timeout=5)

            self.assertEqual(merged.rates["CNY"], 7.4)
            self.assertAlmostEqual(merged.rates["BTC"], 1 / 50000)
            self.assertEqual(service.snapshot.rates["CNY"], 7.4)

    def test_newer_provider_timestamp_wins_even_if_its_request_started_first(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            first_started = Event()
            release_first = Event()
            call_count = 0

            def fetch_fiat():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    first_started.set()
                    if not release_first.wait(5):
                        raise TimeoutError("test did not release first response")
                    cny, source_time = 7.5, VALID_SOURCE_UNIX + 200
                else:
                    cny, source_time = 7.3, VALID_SOURCE_UNIX + 100
                return {
                    "result": "success",
                    "rates": {"USD": 1.0, "CNY": cny},
                    "time_last_update_unix": source_time,
                }

            service._fetch_fiat_payload = fetch_fiat
            service._fetch_fiat_history_rows = history_rows
            service._fetch_crypto_rows = lambda: [
                {"symbol": "BTCUSDT", "lastPrice": "50000", "openPrice": "49000"}
            ]

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_all = executor.submit(service.refresh, "all")
                self.assertTrue(first_started.wait(5))
                second_fiat = executor.submit(service.refresh, "fiat")
                self.assertEqual(second_fiat.result(timeout=5).rates["CNY"], 7.3)
                release_first.set()
                merged = first_all.result(timeout=5)

            self.assertEqual(merged.rates["CNY"], 7.5)
            self.assertEqual(service.snapshot.rates["CNY"], 7.5)

    def test_concurrent_section_refreshes_do_not_lose_each_other(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = make_snapshot()
            fiat_started = Event()
            crypto_finished = Event()

            def fetch_fiat():
                fiat_started.set()
                if not crypto_finished.wait(5):
                    raise TimeoutError("crypto refresh did not finish")
                return {
                    "result": "success",
                    "rates": {"USD": 1.0, "CNY": 7.3},
                "time_last_update_unix": VALID_SOURCE_UNIX,
                }

            service._fetch_fiat_payload = fetch_fiat
            service._fetch_fiat_history_rows = history_rows
            service._fetch_crypto_rows = lambda: [
                {"symbol": "BTCUSDT", "lastPrice": "50000", "openPrice": "49000"}
            ]

            with ThreadPoolExecutor(max_workers=2) as executor:
                fiat_future = executor.submit(service.refresh, "fiat")
                self.assertTrue(fiat_started.wait(5))
                crypto_future = executor.submit(service.refresh, "crypto")
                try:
                    crypto_future.result(timeout=5)
                finally:
                    crypto_finished.set()
                fiat_future.result(timeout=5)

            self.assertEqual(service.snapshot.rates["CNY"], 7.3)
            self.assertAlmostEqual(service.snapshot.rates["BTC"], 1 / 50000)
            datetime.fromisoformat(service.snapshot.fetched_at).astimezone(timezone.utc)
            cached = RateService(Path(path)).snapshot
            self.assertEqual(cached.rates["CNY"], 7.3)
            self.assertAlmostEqual(cached.rates["BTC"], 1 / 50000)


if __name__ == "__main__":
    unittest.main()
