import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from command_service import (
    C2C_NON_GUARANTEE_WARNING,
    MARKET_FALLBACK_WARNING,
    MAX_COMMAND_CHARS,
    CommandError,
    CommandService,
    parse_command,
)
from conversion_core import canonical_amount_string


class ExactStub:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def convert_exact(self, amount, source, target):
        with self.lock:
            self.calls.append((amount, source, target))
        return canonical_amount_string(Decimal(str(amount)) * Decimal("1.25"))


class QuoteStub:
    def __init__(self, status="ok"):
        self.status = status
        self.requests = []
        self.lock = threading.Lock()

    def quote(self, request, *, cancel=None):
        with self.lock:
            self.requests.append((request, cancel))
        return {
            "provider": None if request.provider == "auto" else request.provider,
            "status": self.status,
            "data_state": "live" if self.status == "ok" else "market_fallback",
            "fiat": request.fiat,
            "asset": request.asset,
            "direction": request.direction.value,
            "input_amount": request.amount,
            "market_best_price": "7.1234" if self.status == "ok" else None,
            "indicative_price": "7.2" if self.status == "market_fallback" else None,
            "warnings": ["资格条件需由用户确认。"],
        }

    def capabilities(self):
        return (
            {
                "provider": "binance",
                "enabled": True,
                "configured": True,
                "read_only": True,
            },
        )


class CommandParserTests(unittest.TestCase):
    def test_calc_syntax(self):
        parsed = parse_command("/calc (12.5+7)*3")
        self.assertEqual(parsed.kind, "calculate")
        self.assertEqual(parsed.expression, "(12.5+7)*3")

    def test_fx_amount_is_canonical_decimal_string(self):
        parsed = parse_command("/fx 001.2300e2 cny usd")
        self.assertEqual(parsed.amount, "123")
        self.assertEqual((parsed.source, parsed.target), ("CNY", "USD"))

    def test_c2c_options_are_normalized(self):
        parsed = parse_command(
            "/c2c 1000 CNY USDT --provider BINANCE --pay ALIPAY"
        )
        self.assertEqual(parsed.kind, "c2c")
        self.assertEqual(parsed.provider, "binance")
        self.assertEqual(parsed.payment_method, "ALIPAY")

    def test_chinese_conversion_supports_explicit_c2c_mode(self):
        parsed = parse_command("兑换 0.02 BTC CNY --mode c2c --provider auto")
        self.assertEqual(parsed.kind, "c2c")
        self.assertEqual(parsed.amount, "0.02")
        self.assertEqual(parsed.provider, "auto")

    def test_chinese_conversion_defaults_to_normal_market_mode(self):
        parsed = parse_command("兑换 10 CNY USD")
        self.assertEqual(parsed.kind, "convert")
        self.assertEqual(parsed.mode, "market")

    def test_unknown_duplicate_and_missing_parameters_have_stable_codes(self):
        cases = {
            "/c2c 1 CNY USDT --unknown value": "unknown_parameter",
            "/c2c 1 CNY USDT --provider auto --provider binance": "duplicate_parameter",
            "/c2c 1 CNY USDT --pay": "missing_parameter_value",
            "兑换 1 CNY USD --provider auto": "invalid_parameter_combination",
        }
        for text, code in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(CommandError) as caught:
                    parse_command(text)
                self.assertEqual(caught.exception.code, code)

    def test_control_injection_and_overlong_text_are_rejected(self):
        cases = {
            "/calc 1\n+2": "control_character",
            "/calc __import__(1)": "unsafe_text",
            "/calc 1;2": "unsafe_text",
            "/calc $(1+2)": "unsafe_text",
            "/calc " + "1" * MAX_COMMAND_CHARS: "command_too_long",
        }
        for text, code in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(CommandError) as caught:
                    parse_command(text)
                self.assertEqual(caught.exception.code, code)

    def test_invalid_currency_amount_pair_and_unknown_command_are_rejected(self):
        cases = (
            ("/fx amount CNY USD", "invalid_amount"),
            ("/fx 1 CNY! USD", "invalid_currency"),
            ("/c2c 0 CNY USDT", "invalid_amount"),
            ("/unknown 1", "unknown_command"),
        )
        for text, code in cases:
            with self.subTest(text=text):
                with self.assertRaises(CommandError) as caught:
                    parse_command(text)
                self.assertEqual(caught.exception.code, code)


class CommandServiceTests(unittest.TestCase):
    def setUp(self):
        self.converter = ExactStub()
        self.quotes = QuoteStub()
        self.service = CommandService(self.converter, self.quotes)

    def test_calculator_uses_safe_core_and_returns_stable_result(self):
        result = self.service.execute("/calc (12.5+7)*3")
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "calculator_core")
        self.assertEqual(result.data["result"], "58.5")

        injection = self.service.execute("/calc open(1)")
        self.assertFalse(injection.ok)
        self.assertEqual(injection.error.code, "invalid_expression")

    def test_conversion_preserves_decimal_input_and_uses_exact_interface(self):
        result = self.service.execute("/fx 0.12345678901234567890 CNY USD")
        self.assertTrue(result.ok)
        self.assertEqual(
            self.converter.calls[-1],
            ("0.1234567890123456789", "CNY", "USD"),
        )
        self.assertEqual(result.data["value"], "0.154320986265432098625")
        self.assertIsInstance(result.data["value"], str)

    def test_missing_services_are_reported_without_network_fallback(self):
        offline = CommandService()
        conversion = offline.execute("/fx 1 CNY USD")
        c2c = offline.execute("/c2c 1 CNY USDT")
        self.assertEqual(conversion.error.code, "service_unavailable")
        self.assertEqual(c2c.error.code, "service_unavailable")
        self.assertTrue(conversion.error.retryable)
        self.assertTrue(c2c.error.retryable)

    def test_c2c_builds_buy_and_sell_requests_without_sdk_coupling(self):
        buy = self.service.execute(
            "/c2c 1000 CNY USDT --provider binance --pay ALIPAY",
            request_id="request-buy",
        )
        sell = self.service.execute(
            "兑换 0.02 BTC CNY --mode c2c --provider auto",
            request_id="request-sell",
        )

        self.assertTrue(buy.ok)
        self.assertTrue(sell.ok)
        buy_request = self.quotes.requests[-2][0]
        sell_request = self.quotes.requests[-1][0]
        self.assertEqual(buy_request.direction.value, "BUY")
        self.assertEqual(buy_request.payment_methods, ("ALIPAY",))
        self.assertEqual(sell_request.direction.value, "SELL")
        self.assertFalse(buy_request.allow_market_fallback)
        self.assertIn(C2C_NON_GUARANTEE_WARNING, buy.warnings)
        self.assertIn("不保证", " ".join(buy.warnings))

    def test_market_fallback_is_never_described_as_c2c_executable(self):
        service = CommandService(self.converter, QuoteStub(status="market_fallback"))
        result = service.execute("/c2c 100 CNY USDT")
        self.assertTrue(result.ok)
        self.assertIn(MARKET_FALLBACK_WARNING, result.warnings)
        self.assertIn("非 C2C 可成交价", " ".join(result.warnings))

    def test_c2c_adapter_drops_raw_platform_fields_and_rejects_float_money(self):
        class RawQuoteStub(QuoteStub):
            def quote(self, request, *, cancel=None):
                payload = super().quote(request, cancel=cancel)
                payload["raw_response"] = {"private": "must-not-cross-boundary"}
                return payload

        filtered = CommandService(self.converter, RawQuoteStub()).execute(
            "/c2c 100 CNY USDT"
        )
        self.assertTrue(filtered.ok)
        self.assertNotIn("raw_response", filtered.data["quote"])

        class FloatQuoteStub(QuoteStub):
            def quote(self, request, *, cancel=None):
                payload = super().quote(request, cancel=cancel)
                payload["market_best_price"] = 7.12
                return payload

        rejected = CommandService(self.converter, FloatQuoteStub()).execute(
            "/c2c 100 CNY USDT"
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error.code, "service_error")

    def test_invalid_c2c_pair_has_stable_error(self):
        result = self.service.execute("/c2c 1 CNY USD")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_c2c_pair")

    def test_capabilities_report_okx_unconfigured_and_injected_binance(self):
        capabilities = self.service.capabilities()
        providers = capabilities["c2c"]["providers"]
        self.assertTrue(capabilities["calculator"]["available"])
        self.assertTrue(capabilities["conversion"]["available"])
        self.assertTrue(providers["binance"]["configured"])
        self.assertFalse(providers["okx"]["configured"])

    def test_direct_and_command_calculation_results_are_identical(self):
        direct = self.service.calculate("0.1+0.2")
        command = self.service.execute("/calc 0.1+0.2")
        self.assertEqual(direct.data, command.data)
        self.assertEqual(direct.source, command.source)

    def test_calculator_facade_can_be_injected(self):
        expressions = []

        def calculate(expression):
            expressions.append(expression)
            return "42.000"

        service = CommandService(
            self.converter,
            self.quotes,
            calculator_service=calculate,
        )
        result = service.execute("/calc 6*7")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["result"], "42.000")
        self.assertEqual(expressions, ["6*7"])

    def test_service_is_safe_for_parallel_command_execution(self):
        commands = [f"/fx {index}.000 CNY USD" for index in range(1, 25)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.service.execute, commands))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(len(self.converter.calls), len(commands))
        self.assertTrue(all(isinstance(result.data["value"], str) for result in results))


if __name__ == "__main__":
    unittest.main()
