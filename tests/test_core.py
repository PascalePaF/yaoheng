import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from calculator_core import CalculationError, CalculatorModel, SafeEvaluator, evaluate_basic_amount, format_number
from app_ui import DualConverterPage, MarketPage
from rate_service import BINANCE_CRYPTOS, RateService, RateSnapshot, crypto_display_name, fiat_daily_changes, fiat_display_name, fiat_region, relative_rate_change
from settings_service import AppSettings, SettingsStore, timezone_names


class SafeEvaluatorTests(unittest.TestCase):
    def test_arithmetic_and_constants(self):
        evaluator = SafeEvaluator()
        self.assertEqual(evaluator.evaluate("2+3×4"), 14)
        self.assertEqual(evaluator.evaluate("10%3"), 1)
        self.assertAlmostEqual(evaluator.evaluate("2π"), 2 * math.pi)
        self.assertEqual(evaluator.evaluate("(2+3)^2"), 25)

    def test_scientific_degree_mode(self):
        evaluator = SafeEvaluator("DEG")
        self.assertAlmostEqual(evaluator.evaluate("sin(30)"), 0.5, places=12)
        self.assertAlmostEqual(evaluator.evaluate("asin(0.5)"), 30, places=12)
        self.assertEqual(evaluator.evaluate("fact(6)"), 720)

    def test_scientific_radian_mode(self):
        evaluator = SafeEvaluator("RAD")
        self.assertAlmostEqual(evaluator.evaluate("cos(pi)"), -1, places=12)

    def test_scientific_notation_is_not_confused_with_e_constant(self):
        evaluator = SafeEvaluator()
        self.assertEqual(evaluator.evaluate("1e3"), 1000)
        self.assertAlmostEqual(evaluator.evaluate("1e-3"), 0.001)
        self.assertAlmostEqual(evaluator.evaluate("2e"), 2 * math.e)

    def test_implicit_multiplication_covers_calculator_style_input(self):
        evaluator = SafeEvaluator()
        self.assertEqual(evaluator.evaluate("2(3+4)"), 14)
        self.assertEqual(evaluator.evaluate("(2+1)(4+1)"), 15)
        self.assertAlmostEqual(evaluator.evaluate("2sin(30)"), 1.0, places=12)

    def test_pasted_grouped_and_full_width_expression(self):
        self.assertEqual(SafeEvaluator().evaluate("1,234.5＋5.5="), 1240)

    def test_large_integer_arithmetic_does_not_lose_a_unit(self):
        evaluator = SafeEvaluator()
        self.assertEqual(evaluator.evaluate("9007199254740993+1"), 9007199254740994)

    def test_unsafe_expression_is_rejected(self):
        with self.assertRaises(CalculationError):
            SafeEvaluator().evaluate("__import__('os').system('whoami')")

    def test_errors_are_user_friendly(self):
        with self.assertRaisesRegex(CalculationError, "不能除以零"):
            SafeEvaluator().evaluate("1÷0")
        with self.assertRaisesRegex(CalculationError, "阶乘"):
            SafeEvaluator().evaluate("fact(-2)")


class CalculatorModelTests(unittest.TestCase):
    def test_phone_style_percent(self):
        model = CalculatorModel(expression="200+10")
        model.apply_percent()
        self.assertEqual(model.equals(), "220")

    def test_sign_memory_and_history(self):
        model = CalculatorModel(expression="12.5")
        model.memory_action("M+")
        self.assertEqual(model.memory, 12.5)
        model.clear()
        model.memory_action("MR")
        model.toggle_sign()
        self.assertEqual(model.equals(), "-12.5")
        self.assertEqual(len(model.history), 1)

    def test_result_format(self):
        self.assertEqual(format_number(0.1 + 0.2), "0.3")
        self.assertEqual(format_number(12.0), "12")
        self.assertEqual(format_number(0.9999999999999), "0.9999999999999")
        self.assertEqual(format_number(1.0000000000001), "1.0000000000001")

    def test_negative_operand_can_follow_multiply_and_power(self):
        model = CalculatorModel()
        for token in ("5", "×", "−", "2"):
            model.input(token)
        self.assertEqual(model.equals(), "-10")

        model.clear()
        for token in ("2", "^", "−", "3"):
            model.input(token)
        self.assertEqual(model.equals(), "0.125")

    def test_modulo_button_continues_as_a_binary_operator(self):
        model = CalculatorModel()
        for token in ("7", "%", "4"):
            model.input(token)
        self.assertEqual(model.equals(), "3")

    def test_equals_closes_unmatched_parentheses(self):
        model = CalculatorModel(expression="2×(3+4")
        self.assertEqual(model.equals(), "14")
        self.assertEqual(model.history[0], ("2×(3+4)", "14"))

    def test_toggle_sign_changes_the_current_operand(self):
        model = CalculatorModel(expression="10+2")
        model.toggle_sign()
        self.assertEqual(model.equals(), "8")

    def test_incomplete_expression_previews_zero(self):
        model = CalculatorModel(expression="50+")
        self.assertEqual(model.preview(), "0")

    def test_direct_formula_entry(self):
        model = CalculatorModel()
        model.set_expression("50+2*3")
        self.assertEqual(model.preview(), "56")
        self.assertEqual(model.equals(), "56")
        self.assertEqual(model.history[0], ("50+2*3", "56"))

    def test_history_limit_is_configurable(self):
        model = CalculatorModel(history_limit=2)
        for expression in ("1+1", "2+2", "3+3"):
            model.set_expression(expression)
            model.equals()
        self.assertEqual(len(model.history), 2)

    def test_reference_amount_accepts_only_basic_arithmetic(self):
        self.assertEqual(evaluate_basic_amount("100+25*2"), 150)
        self.assertAlmostEqual(evaluate_basic_amount("10÷4"), 2.5)
        self.assertEqual(evaluate_basic_amount("(1,200÷3)+(17%5)="), 402)
        self.assertEqual(evaluate_basic_amount("20+(18%5)*4"), 32)
        with self.assertRaises(CalculationError):
            evaluate_basic_amount("sqrt(4)")
        with self.assertRaises(CalculationError):
            evaluate_basic_amount("2**8")


class RateServiceTests(unittest.TestCase):
    def test_cross_conversion(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = RateSnapshot(
                rates={"USD": 1, "CNY": 7.2, "EUR": 0.9, "BTC": 0.00002},
                names={}, kinds={}, changes={}, fetched_at="",
            )
            self.assertAlmostEqual(service.convert(100, "USD", "CNY"), 720)
            self.assertAlmostEqual(service.convert(720, "CNY", "USD"), 100)
            self.assertAlmostEqual(service.convert(1, "BTC", "CNY"), 360000)

    def test_binance_chart_is_converted_to_cny(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return [
                    [1000, "", "", "", "10"],
                    [2000, "", "", "", "12"],
                ]

        class FakeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = RateSnapshot(
                rates={"USD": 1, "CNY": 7.2, "BTC": 0.1},
                names={}, kinds={}, changes={}, fetched_at="",
            )
            service.session = FakeSession()
            points = service._fetch_binance_chart("BTC", 7)
            self.assertEqual(points, [(1000, 72.0), (2000, 86.4)])

    def test_fiat_chart_parses_official_time_series(self):
        today = datetime.now(timezone.utc).date()
        first_day = (today - timedelta(days=2)).isoformat()
        second_day = (today - timedelta(days=1)).isoformat()

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return [
                    {"date": first_day, "base": "USD", "quote": "CNY", "rate": 7.1},
                    {"date": second_day, "base": "USD", "quote": "CNY", "rate": 7.2},
                ]

        class FakeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = RateSnapshot(
                rates={"USD": 1, "CNY": 7.2},
                names={}, kinds={"USD": "fiat", "CNY": "fiat"}, changes={}, fetched_at="",
            )
            service.session = FakeSession()
            points = service.fetch_fiat_chart("USD", 7, "CNY")
            self.assertEqual([value for _, value in points], [7.1, 7.2])

    def test_cache_cleanup_never_removes_unrelated_files(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            (data_dir / "rates_cache.json").write_text("{}", encoding="utf-8")
            (data_dir / "chart_bitcoin_7.json").write_text("[]", encoding="utf-8")
            (data_dir / "notes.txt").write_text("keep me", encoding="utf-8")
            unrelated = data_dir / "other-app"
            unrelated.mkdir()
            (unrelated / "data.json").write_text("keep me too", encoding="utf-8")

            service = RateService(data_dir)
            self.assertEqual(service.cache_size_bytes(), 4)
            service.clear_cache()

            self.assertFalse((data_dir / "rates_cache.json").exists())
            self.assertFalse((data_dir / "chart_bitcoin_7.json").exists())
            self.assertEqual((data_dir / "notes.txt").read_text(encoding="utf-8"), "keep me")
            self.assertTrue((unrelated / "data.json").exists())

    def test_cache_limit_only_evicts_managed_chart_files(self):
        with tempfile.TemporaryDirectory() as path:
            data_dir = Path(path)
            chart = data_dir / "chart_bitcoin_7.json"
            chart.write_bytes(b"x" * (2 * 1024 * 1024))
            notes = data_dir / "notes.bin"
            notes.write_bytes(b"y" * (2 * 1024 * 1024))
            service = RateService(data_dir)
            service.set_cache_limit(1)
            self.assertFalse(chart.exists())
            self.assertTrue(notes.exists())

    def test_convert_rejects_invalid_amounts_and_rates(self):
        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = RateSnapshot(
                rates={"USD": 1, "CNY": 7.2, "BAD": 0}, names={}, kinds={}, changes={}, fetched_at="",
            )
            with self.assertRaises(ValueError):
                service.convert(float("nan"), "USD", "CNY")
            with self.assertRaises(ValueError):
                service.convert(1, "BAD", "CNY")

    def test_failed_refresh_does_not_claim_cached_rates_are_fresh(self):
        class OfflineSession:
            def get(self, *_args, **_kwargs):
                raise OSError("offline")

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = RateSnapshot(
                rates={"USD": 1, "CNY": 7.2}, names={},
                kinds={"USD": "fiat", "CNY": "fiat"}, changes={}, fetched_at="old",
            )
            service.session = OfflineSession()
            with self.assertRaises(ConnectionError):
                service.refresh("fiat")
            self.assertEqual(service.snapshot.fetched_at, "old")

    def test_fiat_daily_changes_and_cross_rate(self):
        changes = fiat_daily_changes([
            {"date": "2026-07-16", "base": "USD", "quote": "CNY", "rate": 7.0},
            {"date": "2026-07-17", "base": "USD", "quote": "CNY", "rate": 7.14},
            {"date": "2026-07-16", "base": "USD", "quote": "EUR", "rate": 0.9},
            {"date": "2026-07-17", "base": "USD", "quote": "EUR", "rate": 0.891},
        ])
        self.assertAlmostEqual(changes["CNY"], 2.0)
        self.assertAlmostEqual(changes["EUR"], -1.0)
        self.assertAlmostEqual(relative_rate_change(changes["CNY"], changes["EUR"]), (0.99 / 1.02 - 1) * 100)

    def test_partial_fiat_refresh_preserves_crypto(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, **_kwargs):
                self.calls.append(url)
                if "frankfurter" in url:
                    return FakeResponse([
                        {"date": "2026-07-16", "base": "USD", "quote": "CNY", "rate": 7.2},
                        {"date": "2026-07-17", "base": "USD", "quote": "CNY", "rate": 7.3},
                    ])
                return FakeResponse({"result": "success", "rates": {"USD": 1, "CNY": 7.3}, "time_last_update_unix": 1_700_000_000})

        with tempfile.TemporaryDirectory() as path:
            service = RateService(Path(path))
            service.snapshot = RateSnapshot(
                rates={"USD": 1, "CNY": 7.2, "BTC": 0.00002}, names={"BTC": "Bitcoin"},
                kinds={"USD": "fiat", "CNY": "fiat", "BTC": "crypto"}, changes={"BTC": 2.0}, fetched_at="",
            )
            service.session = FakeSession()
            snapshot = service.refresh("fiat")
            self.assertEqual(snapshot.rates["CNY"], 7.3)
            self.assertIn("BTC", snapshot.rates)
            self.assertEqual(len(service.session.calls), 2)
            self.assertGreater(snapshot.changes["CNY"], 0)

    def test_fiat_name_and_region_are_user_friendly(self):
        self.assertIn("津巴布韦", fiat_display_name("ZWG", "ZWG"))
        self.assertEqual(fiat_region("JPY"), "亚洲")
        self.assertEqual(fiat_region("EUR"), "欧洲")

    def test_mainstream_crypto_names_are_chinese(self):
        for code, english_name in BINANCE_CRYPTOS.items():
            display = crypto_display_name(code, english_name)
            self.assertRegex(display, r"[\u4e00-\u9fff]", code)
        self.assertEqual(crypto_display_name("BTC", "Bitcoin"), "比特币")


class SettingsTests(unittest.TestCase):
    def test_global_timezones_are_available(self):
        zones = timezone_names()
        self.assertGreater(len(zones), 500)
        self.assertIn("Asia/Tokyo", zones)
        self.assertIn("America/New_York", zones)

    def test_explicit_data_directory(self):
        settings = AppSettings(data_dir="D:/portable-data", keep_data_with_app=False)
        self.assertTrue(str(settings.resolved_data_dir()).lower().endswith("portable-data"))

    def test_refresh_and_calculator_settings_are_validated(self):
        settings = AppSettings(
            fiat_refresh_minutes=0, crypto_refresh_minutes=5000, history_limit=999,
            startup_page="missing", close_action="unknown", calculator_angle_mode="GRAD",
        )
        validated = SettingsStore.validate(settings)
        self.assertEqual(validated.fiat_refresh_minutes, 1)
        self.assertEqual(validated.crypto_refresh_minutes, 1440)
        self.assertEqual(validated.history_limit, 200)
        self.assertEqual(validated.startup_page, "calculator")
        self.assertEqual(validated.close_action, "exit")
        self.assertEqual(validated.calculator_angle_mode, "DEG")

    def test_malformed_individual_settings_fall_back_without_resetting_everything(self):
        settings = AppSettings(
            theme="light", fiat_refresh_minutes="bad", crypto_refresh_minutes=None,
            history_limit="oops", cache_limit_mb=[], keep_data_with_app="false",
        )
        validated = SettingsStore.validate(settings)
        self.assertEqual(validated.theme, "light")
        self.assertEqual(validated.fiat_refresh_minutes, 60)
        self.assertEqual(validated.crypto_refresh_minutes, 10)
        self.assertEqual(validated.history_limit, 30)
        self.assertEqual(validated.cache_limit_mb, 500)
        self.assertFalse(validated.keep_data_with_app)


class UiRegressionTests(unittest.TestCase):
    def test_converter_does_not_shadow_tk_callback_registration(self):
        self.assertNotIn("_register", DualConverterPage.__dict__)

    def test_financial_display_keeps_small_values_and_grouping(self):
        self.assertEqual(DualConverterPage._format(1234567.890123), "1,234,567.890123")
        self.assertEqual(DualConverterPage._format(0.0000000000001), "1e-13")
        self.assertEqual(MarketPage.compact(0), "0")


if __name__ == "__main__":
    unittest.main()
