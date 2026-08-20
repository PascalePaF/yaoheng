from __future__ import annotations

import json
import unittest

from c2c.models import DataState, Direction, QuoteResult, QuoteStatus
from conversion_core import DecimalConversionEngine
from exchange_page import (
    C2CQuoteJob,
    DEFAULT_EXCHANGE_CURRENCIES,
    ExchangeCoordinator,
    ExchangeEdgeResult,
    ExchangePageState,
)


KINDS = {
    "CNY": "fiat",
    "USD": "fiat",
    "EUR": "fiat",
    "JPY": "fiat",
    "HKD": "fiat",
    "BTC": "crypto",
    "USDT": "crypto",
}
RATES = {
    "CNY": "2",
    "USD": "1",
    "EUR": "0.9",
    "JPY": "100",
    "HKD": "7",
    "BTC": "0.00001",
    "USDT": "1",
}


def make_coordinator(c2c_service: object | None = None) -> ExchangeCoordinator:
    return ExchangeCoordinator(DecimalConversionEngine(RATES, KINDS), c2c_service)


def make_edge_result(
    state: ExchangePageState,
    slot: int,
    *,
    exact: str | None = "2",
    display: str = "2.00",
    generation: int | None = None,
    source: str | None = None,
    target: str | None = None,
) -> ExchangeEdgeResult:
    return ExchangeEdgeResult(
        slot=slot,
        generation=state.generation if generation is None else generation,
        source=state.primary_code if source is None else source,
        target=state.currencies[slot] if target is None else target,
        route="market",
        exact_value=exact,
        display_value=display if exact is not None else "—",
        status="fixture",
        state="live" if exact is not None else "error",
    )


def make_c2c_job(
    coordinator: ExchangeCoordinator,
    *,
    provider: str = "binance",
    generation: int = 7,
    amount: str = "1000",
) -> C2CQuoteJob:
    prepared = coordinator.prepare_edge(
        slot=6,
        generation=generation,
        amount=amount,
        source="CNY",
        target="USDT",
        kinds=KINDS,
        mode="c2c",
        provider=provider,
    )
    if not isinstance(prepared, C2CQuoteJob):
        raise AssertionError("fixture edge did not produce a C2C job")
    return prepared


class ExchangePageStateTests(unittest.TestCase):
    def test_state_requires_exactly_seven_unique_currencies(self):
        state = ExchangePageState(currencies=[code.lower() for code in DEFAULT_EXCHANGE_CURRENCIES])

        self.assertEqual(tuple(state.currencies), DEFAULT_EXCHANGE_CURRENCIES)
        self.assertEqual(len(state.currencies), 7)
        self.assertEqual(len(set(state.currencies)), 7)

        invalid_sets = (
            ["CNY", "USD", "EUR", "JPY", "HKD", "BTC"],
            ["CNY", "USD", "EUR", "JPY", "HKD", "BTC", "BTC"],
        )
        for currencies in invalid_sets:
            with self.subTest(currencies=currencies), self.assertRaises(ValueError):
                ExchangePageState(currencies=currencies)

    def test_selecting_an_existing_currency_swaps_slots_and_preserves_uniqueness(self):
        state = ExchangePageState()
        self.assertTrue(state.accept_result(make_edge_result(state, 2)))

        changed = state.select_currency(1, "btc")

        self.assertTrue(changed)
        self.assertEqual(state.currencies[1], "BTC")
        self.assertEqual(state.currencies[5], "USD")
        self.assertEqual(len(set(state.currencies)), 7)
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.results, {})
        self.assertFalse(state.select_currency(1, "BTC"))
        self.assertEqual(state.generation, 1)

    def test_set_primary_keeps_input_when_selected_card_has_a_current_result(self):
        state = ExchangePageState(amount="123.45")
        self.assertTrue(state.accept_result(make_edge_result(state, 2, exact="111.222")))

        change = state.set_primary(2)

        self.assertEqual(change.previous_slot, 0)
        self.assertEqual(change.primary_slot, 2)
        self.assertFalse(change.amount_cleared)
        self.assertEqual(state.primary_code, "EUR")
        self.assertEqual(state.amount, "123.45")
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.results, {})

    def test_set_primary_without_a_usable_result_clears_the_input(self):
        state = ExchangePageState(amount="123.45")
        self.assertTrue(state.accept_result(make_edge_result(state, 3, exact=None)))

        change = state.set_primary(3)

        self.assertTrue(change.amount_cleared)
        self.assertEqual(state.primary_slot, 3)
        self.assertEqual(state.primary_code, "JPY")
        self.assertEqual(state.amount, "")
        self.assertEqual(state.results, {})

    def test_swap_uses_the_exact_unrounded_result_as_the_new_input(self):
        state = ExchangePageState(amount="1")
        result = make_edge_result(
            state,
            5,
            exact="0.123456789123456789000",
            display="0.12345679",
        )
        self.assertTrue(state.accept_result(result))

        swapped = state.swap_with_primary(5)

        self.assertTrue(swapped)
        self.assertEqual(state.primary_slot, 5)
        self.assertEqual(state.primary_code, "BTC")
        self.assertEqual(state.amount, "0.123456789123456789")
        self.assertNotEqual(state.amount, result.display_value)
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.results, {})

    def test_generation_change_clears_results_and_rejects_late_results(self):
        state = ExchangePageState(amount="1")
        old = make_edge_result(state, 1)
        self.assertTrue(state.accept_result(old))

        self.assertTrue(state.set_amount("2"))

        self.assertEqual(state.generation, 1)
        self.assertIsNone(state.current_result(1))
        self.assertFalse(state.accept_result(old))
        self.assertFalse(state.accept_result(make_edge_result(state, 1, source="EUR")))
        self.assertFalse(state.accept_result(make_edge_result(state, 1, target="JPY")))
        self.assertTrue(state.accept_result(make_edge_result(state, 1, exact="4")))

    def test_serialization_persists_only_user_state_not_derived_results(self):
        state = ExchangePageState(
            primary_slot=1,
            amount="250.00",
            mode="c2c",
            provider="okx",
            payment_method="provider.pay:42",
        )
        self.assertTrue(state.accept_result(make_edge_result(state, 5, exact="0.00123456789")))

        payload = state.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)

        self.assertEqual(
            set(payload),
            {"currencies", "primary_slot", "amount", "mode", "provider", "payment_method"},
        )
        self.assertNotIn("results", payload)
        self.assertNotIn("generation", payload)
        self.assertNotIn("0.00123456789", encoded)

        restored = ExchangePageState.from_mapping(payload)
        self.assertEqual(restored.to_dict(), payload)
        self.assertEqual(restored.generation, 0)
        self.assertEqual(restored.results, {})


class ExchangeCoordinatorRoutingTests(unittest.TestCase):
    def test_c2c_mode_mixes_market_and_c2c_edges_in_both_directions(self):
        coordinator = make_coordinator()
        fiat_source = ExchangePageState(
            amount="100",
            mode="c2c",
            provider="binance",
            payment_method="provider.pay:42",
        )

        immediate, jobs = coordinator.quote_edges(fiat_source, KINDS)

        self.assertEqual({result.slot for result in immediate}, {1, 2, 3, 4})
        self.assertTrue(all(result.route == "market" for result in immediate))
        self.assertEqual({job.slot for job in jobs}, {5, 6})
        self.assertTrue(all(job.request.direction is Direction.BUY for job in jobs))
        self.assertTrue(all(job.request.fiat == "CNY" for job in jobs))
        self.assertEqual({job.request.asset for job in jobs}, {"BTC", "USDT"})
        self.assertTrue(all(job.request.payment_methods == ("provider.pay:42",) for job in jobs))

        crypto_source = ExchangePageState(
            primary_slot=5,
            amount="2",
            mode="c2c",
            provider="okx",
        )

        reverse_immediate, reverse_jobs = coordinator.quote_edges(crypto_source, KINDS)

        self.assertEqual({result.slot for result in reverse_immediate}, {6})
        self.assertEqual(reverse_immediate[0].target, "USDT")
        self.assertEqual({job.slot for job in reverse_jobs}, {0, 1, 2, 3, 4})
        self.assertTrue(all(job.request.direction is Direction.SELL for job in reverse_jobs))
        self.assertTrue(all(job.request.asset == "BTC" for job in reverse_jobs))
        self.assertEqual({job.request.fiat for job in reverse_jobs}, {"CNY", "USD", "EUR", "JPY", "HKD"})

    def test_market_mode_maps_all_six_exact_edges_to_live_or_cache_state(self):
        coordinator = make_coordinator()
        state = ExchangePageState(amount="0.1", mode="market")

        for from_cache, expected_state, expected_status in (
            (False, "live", "普通汇率 · 实时"),
            (True, "cache", "普通汇率 · 缓存"),
        ):
            with self.subTest(from_cache=from_cache):
                immediate, jobs = coordinator.quote_edges(
                    state,
                    KINDS,
                    from_cache=from_cache,
                )

                self.assertEqual(len(immediate), 6)
                self.assertEqual(jobs, ())
                self.assertTrue(all(result.route == "market" for result in immediate))
                self.assertTrue(all(result.state == expected_state for result in immediate))
                self.assertTrue(all(result.status == expected_status for result in immediate))
                self.assertTrue(all(result.exact_value is not None for result in immediate))


class ExchangeCoordinatorResultMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = make_coordinator()
        self.job = make_c2c_job(self.coordinator)

    def test_okx_unconfigured_and_permission_denied_are_explicit(self):
        job = make_c2c_job(self.coordinator, provider="okx")
        for status in (QuoteStatus.UNCONFIGURED, QuoteStatus.PERMISSION_DENIED):
            with self.subTest(status=status):
                quote = QuoteResult(
                    provider="okx",
                    status=status,
                    data_state=DataState.NONE,
                    fiat="CNY",
                    asset="USDT",
                    direction=Direction.BUY,
                    input_amount="1000",
                    warnings=("offline fixture",),
                )

                result = self.coordinator.finish_job(job, quote, kinds=KINDS)

                self.assertEqual(result.status, "OKX 官方 P2P API 未配置或无权限")
                self.assertEqual(result.state, "unconfigured")
                self.assertEqual(result.provider, "okx")
                self.assertIsNone(result.exact_value)

    def test_market_fallback_is_clearly_degraded_and_not_a_c2c_match(self):
        quote = QuoteResult(
            provider="ordinary_market",
            status=QuoteStatus.MARKET_FALLBACK,
            data_state=DataState.MARKET_FALLBACK,
            fiat="CNY",
            asset="USDT",
            direction=Direction.BUY,
            input_amount="1000",
            indicative_price="2",
            indicative_output_amount="500",
            output_unit="USDT",
            warnings=("上游 C2C 不可用",),
        )

        result = self.coordinator.finish_job(self.job, quote, kinds=KINDS)

        self.assertEqual(result.route, "market")
        self.assertEqual(result.exact_value, "500")
        self.assertEqual(result.display_value, "500")
        self.assertEqual(result.state, "degraded")
        self.assertIn("非 C2C 可成交价", result.status)
        self.assertIn("不代表任何单广告可成交", result.details[0])
        self.assertIn("上游 C2C 不可用", result.details)
        self.assertEqual(result.provider, "ordinary_market")

    def test_amount_out_of_range_never_promotes_market_best_to_a_result(self):
        quote = QuoteResult(
            provider="binance",
            status=QuoteStatus.NO_MATCH,
            data_state=DataState.LIVE,
            fiat="CNY",
            asset="USDT",
            direction=Direction.BUY,
            input_amount="1000",
            market_best_price="1.99",
            warnings=("fixture range warning",),
        )

        result = self.coordinator.finish_job(self.job, quote, kinds=KINDS)

        self.assertEqual(result.status, "金额越界 · 无本金额匹配价")
        self.assertEqual(result.state, "range")
        self.assertIsNone(result.exact_value)
        self.assertEqual(result.display_value, "—")
        self.assertEqual(result.market_best_price, "1.99")
        self.assertTrue(any("未拿最低价冒充" in detail for detail in result.details))
        self.assertTrue(any("最低展示价：1.99" in detail for detail in result.details))

    def test_success_maps_live_and_cache_states_without_losing_exact_output(self):
        cases = (
            (DataState.LIVE, "live", "Binance C2C · 实时"),
            (DataState.FRESH_CACHE, "cache", "Binance C2C · 缓存（新鲜）"),
            (DataState.STALE_CACHE, "cache", "Binance C2C · 缓存（宽限）"),
        )
        for data_state, expected_state, expected_status in cases:
            with self.subTest(data_state=data_state):
                quote = {
                    "provider": "binance",
                    "status": QuoteStatus.OK,
                    "data_state": data_state,
                    "market_best_price": "1.99",
                    "match": {
                        "price": "2.0000",
                        "output_amount": "0500.123456789000",
                    },
                    "warnings": ("offline fixture",),
                }

                result = self.coordinator.finish_job(self.job, quote, kinds=KINDS)

                self.assertEqual(result.state, expected_state)
                self.assertEqual(result.status, expected_status)
                self.assertEqual(result.exact_value, "500.123456789")
                self.assertEqual(result.display_value, "500.12345679")
                self.assertEqual(result.matched_price, "2.0000")
                self.assertEqual(result.market_best_price, "1.99")


class DynamicPaymentMethodTests(unittest.TestCase):
    def test_payment_options_come_only_from_provider_supplied_identifiers(self):
        calls: list[tuple[str, str | None]] = []

        class ProviderMethods:
            def payment_methods(self, provider: str, fiat: str | None = None):
                calls.append((provider, fiat))
                return (
                    {"identifier": "WIRE_8472", "name": "专用转账"},
                    {"identifier": "mobile:pay", "name": "移动支付"},
                    {"identifier": "WIRE_8472", "name": "重复项"},
                    {"identifier": "invalid method", "name": "无效标识"},
                )

        coordinator = make_coordinator(ProviderMethods())

        options = coordinator.payment_method_options("okx", "JPY")

        self.assertEqual(calls, [("okx", "JPY")])
        self.assertEqual(options, (("WIRE_8472", "专用转账"), ("mobile:pay", "移动支付")))
        self.assertEqual(make_coordinator().payment_method_options("binance", "CNY"), ())


if __name__ == "__main__":
    unittest.main()
