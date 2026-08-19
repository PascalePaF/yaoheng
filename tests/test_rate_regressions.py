import json
import math
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from rate_service import (
    RateService,
    RateSnapshot,
    fiat_daily_changes,
    relative_rate_change,
)


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


class RateApiValidationRegressionTests(unittest.TestCase):
    def test_fiat_refresh_rejects_a_non_usd_based_payload(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            old = make_snapshot()
            service.snapshot = old
            service._fetch_fiat_payload = lambda: {
                "result": "success",
                "rates": {"USD": 2.0, "CNY": 14.4},
                "time_last_update_unix": 1_700_000_000,
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
                "time_last_update_unix": 1_700_000_000,
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
                "time_last_update_unix": 1_700_000_000,
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
                "time_last_update_unix": 1_700_000_000,
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
                "time_last_update_unix": 1_700_000_000,
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
                "time_last_update_unix": 1_700_000_000,
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


class RateCacheRegressionTests(unittest.TestCase):
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


class RateChartRegressionTests(unittest.TestCase):
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
                    "time_last_update_unix": 1_700_000_000,
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
