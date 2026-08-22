from __future__ import annotations

import json
import math
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app_version import APP_USER_AGENT, APP_VERSION, RATE_CACHE_SCHEMA_VERSION
from conversion_core import (
    AmountInputError,
    DecimalConversionEngine,
    RateValueError,
    calculate_conversion,
    canonical_amount_string,
    canonical_rate_string,
    convert_exact,
    currency_metadata,
    format_for_display,
)
from rate_service import RateService, RateSnapshot


class DecimalBoundaryTests(unittest.TestCase):
    def test_boundaries_use_canonical_plain_decimal_strings(self):
        self.assertEqual(canonical_amount_string("00012.3400"), "12.34")
        self.assertEqual(canonical_amount_string("-0.000"), "0")
        self.assertEqual(canonical_amount_string("1e-12"), "0.000000000001")
        self.assertEqual(canonical_rate_string(1.0), "1")

    def test_invalid_amount_has_one_stable_error_type_and_code(self):
        invalid_values = ("", "1,000", "NaN", "Infinity", math.nan, math.inf, True, "1" * 5000)
        for value in invalid_values:
            with self.subTest(value=repr(value)), self.assertRaises(AmountInputError) as caught:
                canonical_amount_string(value)
            self.assertEqual(caught.exception.code, "invalid_amount")

    def test_nonpositive_or_nonfinite_rates_are_rejected(self):
        for value in ("0", "-0.1", "NaN", "Infinity", -math.inf):
            with self.subTest(value=value), self.assertRaises(RateValueError) as caught:
                canonical_rate_string(value)
            self.assertEqual(caught.exception.code, "invalid_rate")

    def test_formula_uses_high_precision_without_intermediate_quantization(self):
        repeating = calculate_conversion("1", "3", "1")
        self.assertTrue(str(repeating).startswith("0.33333333333333333333333333333333333333333333333333"))
        self.assertGreaterEqual(len(repeating.as_tuple().digits), 80)
        self.assertEqual(convert_exact("0.1", "1", "3"), "0.3")
        self.assertEqual(convert_exact("1e308", "1e-308", "1e-308"), "1" + "0" * 308)

    def test_fiat_metadata_and_half_even_rounding(self):
        self.assertEqual(currency_metadata("JPY", "fiat").display_precision, 0)
        self.assertEqual(currency_metadata("KWD", "fiat").display_precision, 3)
        self.assertEqual(currency_metadata("BHD", "fiat").display_precision, 3)
        self.assertEqual(currency_metadata("ZZZ", "fiat").display_precision, 2)
        self.assertEqual(format_for_display("2.5", "JPY", "fiat"), "2")
        self.assertEqual(format_for_display("3.5", "JPY", "fiat"), "4")
        self.assertEqual(format_for_display("2.345", "USD", "fiat"), "2.34")
        self.assertEqual(format_for_display("2.355", "USD", "fiat"), "2.36")
        self.assertEqual(format_for_display("1.2345", "KWD", "fiat"), "1.234")

    def test_crypto_uses_eight_places_normally_and_expands_for_tiny_values(self):
        self.assertEqual(format_for_display("0.123456789", "BTC", "crypto"), "0.12345679")
        self.assertEqual(format_for_display("12.34000000", "BTC", "crypto"), "12.34")
        self.assertEqual(format_for_display("0.000000009", "BTC", "crypto"), "0.000000009")
        self.assertEqual(
            format_for_display("0.000000000123456789", "BTC", "crypto"),
            "0.00000000012345679",
        )
        self.assertEqual(format_for_display("1e-30", "BTC", "crypto"), "0")


class ConversionEngineTests(unittest.TestCase):
    def test_engine_copies_inputs_and_is_safe_for_parallel_readers(self):
        rates = {"USD": "1.0", "CNY": "7.2500", "BTC": "0.000002"}
        engine = DecimalConversionEngine(rates, {"USD": "fiat", "CNY": "fiat", "BTC": "crypto"})
        rates["CNY"] = "99"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: engine.convert_exact("10", "USD", "CNY"), range(100)))

        self.assertEqual(set(results), {"72.5"})
        copied = engine.rate_strings
        copied["CNY"] = "1"
        self.assertEqual(engine.rate_strings["CNY"], "7.25")


class ExactRateServiceTests(unittest.TestCase):
    def test_new_exact_path_returns_strings_while_legacy_path_returns_float(self):
        with tempfile.TemporaryDirectory() as directory:
            service = RateService(Path(directory))
            service.snapshot = RateSnapshot(
                rates={"USD": 1.0, "THREE": 3.0},
                names={},
                kinds={"USD": "fiat", "THREE": "fiat"},
                changes={},
                fetched_at="",
                rate_strings={"USD": "1.000", "THREE": "3.000"},
            )

            self.assertEqual(service.convert_exact("0.1", "USD", "THREE"), "0.3")
            self.assertEqual(service.convert_decimal("0.1", "USD", "THREE"), "0.3")
            self.assertEqual(service.convert(0.1, "USD", "THREE"), 0.3)
            self.assertTrue(all(isinstance(value, str) for value in service.get_rate_strings().values()))
            self.assertEqual(service.session.headers["User-Agent"], APP_USER_AGENT)
            self.assertEqual(APP_VERSION, "3.21.0")

    def test_empty_exact_snapshot_is_safe_and_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = RateService(Path(directory)).get_decimal_snapshot()
            self.assertEqual(dict(snapshot.rates), {})
            with self.assertRaises(TypeError):
                snapshot.rates["USD"] = "1"  # type: ignore[index]

    def test_old_numeric_cache_loads_and_next_write_migrates_rates_to_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            cache_path = data_dir / "rates_cache.json"
            cache_path.write_text(
                json.dumps({
                    "rates": {"USD": 1.0, "CNY": 7.2, "BTC": 0.000002},
                    "names": {},
                    "kinds": {"USD": "fiat", "CNY": "fiat", "BTC": "crypto"},
                    "changes": {},
                }),
                encoding="utf-8",
            )

            service = RateService(data_dir)
            self.assertEqual(service.convert_exact("10", "USD", "CNY"), "72")
            self.assertTrue(service.save_cache(service.snapshot))
            migrated = json.loads(cache_path.read_text(encoding="utf-8"))

            self.assertEqual(migrated["cache_schema_version"], RATE_CACHE_SCHEMA_VERSION)
            self.assertEqual(migrated["rates"], {"USD": "1", "CNY": "7.2", "BTC": "0.000002"})
            self.assertTrue(all(isinstance(value, str) for value in migrated["rates"].values()))

    def test_legacy_cache_tolerates_and_normalizes_its_float_usd_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "rates_cache.json").write_text(json.dumps({
                "rates": {"USD": 1.0000000000001, "CNY": 7.2},
                "names": {},
                "kinds": {"USD": "fiat", "CNY": "fiat"},
                "changes": {},
            }), encoding="utf-8")

            service = RateService(data_dir)

            self.assertEqual(service.snapshot.rates["USD"], 1.0)
            self.assertEqual(service.get_rate_strings()["USD"], "1")

    def test_exact_cache_preserves_a_rate_below_float_range(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "rates_cache.json").write_text(
                json.dumps({
                    "rates": {"USD": "1.000", "TINY": "1e-400"},
                    "names": {"TINY": "Tiny"},
                    "kinds": {"USD": "fiat", "TINY": "crypto"},
                    "changes": {},
                }),
                encoding="utf-8",
            )

            service = RateService(data_dir)

            self.assertNotIn("TINY", service.snapshot.rates)
            self.assertEqual(service.get_rate_strings()["TINY"], "0." + "0" * 399 + "1")
            self.assertEqual(service.convert_exact("1", "USD", "TINY"), "0." + "0" * 399 + "1")

    def test_future_or_malformed_cache_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            cache_path = data_dir / "rates_cache.json"
            for version in (True, "2", RATE_CACHE_SCHEMA_VERSION + 1):
                with self.subTest(version=version):
                    cache_path.write_text(json.dumps({
                        "cache_schema_version": version,
                        "rates": {"USD": "1", "CNY": "7.2"},
                        "names": {},
                        "kinds": {"USD": "fiat", "CNY": "fiat"},
                        "changes": {},
                    }), encoding="utf-8")
                    self.assertFalse(RateService(data_dir).snapshot.rates)

    def test_cache_write_rejects_nonfinite_data_without_replacing_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            service = RateService(Path(directory))
            valid = RateSnapshot(
                rates={"USD": 1.0, "CNY": 7.2}, names={},
                kinds={"USD": "fiat", "CNY": "fiat"}, changes={}, fetched_at="",
            )
            self.assertTrue(service.save_cache(valid))
            original = service.cache_path.read_bytes()
            invalid = RateSnapshot(
                rates={"USD": 1.0, "CNY": math.nan}, names={},
                kinds={"USD": "fiat", "CNY": "fiat"}, changes={}, fetched_at="",
            )

            self.assertFalse(service.save_cache(invalid))
            self.assertEqual(service.cache_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
