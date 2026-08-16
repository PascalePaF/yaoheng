import math
import tempfile
import unittest
from pathlib import Path

from calculator_core import CalculationError, CalculatorModel, SafeEvaluator, evaluate_basic_amount, format_number
from app_ui import DualConverterPage
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
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return [
                    {"date": "2026-07-16", "base": "USD", "quote": "CNY", "rate": 7.1},
                    {"date": "2026-07-17", "base": "USD", "quote": "CNY", "rate": 7.2},
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


class UiRegressionTests(unittest.TestCase):
    def test_converter_does_not_shadow_tk_callback_registration(self):
        self.assertNotIn("_register", DualConverterPage.__dict__)


if __name__ == "__main__":
    unittest.main()
