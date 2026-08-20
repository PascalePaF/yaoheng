from __future__ import annotations

import json
import math
import unittest
from decimal import Decimal

from c2c.base import (
    QUALIFICATION_WARNING,
    ProviderProtocolError,
    build_quote_result,
    eligible_ads,
    match_ad,
)
from c2c.models import (
    C2CModelError,
    DataState,
    Direction,
    NormalizedAd,
    QuoteRequest,
    QuoteStatus,
)


def make_ad(
    ad_id: str,
    *,
    direction: str = "BUY",
    price: str = "7.2",
    lower: str = "100",
    upper: str | None = "5000",
    available: str = "1000",
    payments: tuple[str, ...] = ("FIXTURE_BANK",),
    completion: str | None = "0.98",
    orders: str | None = "100",
) -> NormalizedAd:
    return NormalizedAd(
        provider="binance",
        ad_id=ad_id,
        fiat="CNY",
        asset="USDT",
        direction=direction,
        price=price,
        min_fiat=lower,
        max_fiat=upper,
        available_asset=available,
        payment_methods=payments,
        completion_rate=completion,
        completed_orders=orders,
    )


class C2CModelBoundaryTests(unittest.TestCase):
    def test_request_direction_and_canonical_boundaries(self):
        buy = QuoteRequest("cny", "usdt", "buy", "0100.00", generation=7)
        sell = QuoteRequest("CNY", "USDT", Direction.SELL, Decimal("2.500"))

        self.assertEqual((buy.source, buy.target, buy.amount), ("CNY", "USDT", "100"))
        self.assertEqual((sell.source, sell.target, sell.amount), ("USDT", "CNY", "2.5"))
        self.assertEqual(buy.direction, Direction.BUY)

    def test_c2c_money_rejects_float_nonfinite_negative_and_long_values(self):
        for value in (1.25, math.nan, "NaN", "Infinity", "-1", "1" * 5000):
            with self.subTest(value=repr(value)), self.assertRaises((C2CModelError, ValueError)):
                QuoteRequest("CNY", "USDT", "BUY", value)  # type: ignore[arg-type]
        with self.assertRaises(C2CModelError):
            make_ad("float-price", price=7.2)  # type: ignore[arg-type]

    def test_all_public_models_have_json_safe_dicts_without_raw_advertiser_data(self):
        request = QuoteRequest("CNY", "USDT", "BUY", "1000", asset_step="0.01")
        result = build_quote_result(request, "binance", (make_ad("fixture-safe"),))

        serialized = json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False)

        self.assertIn('"price": "7.2"', serialized)
        self.assertNotIn("advertiser", serialized.lower())
        self.assertNotIn("merchant", serialized.lower())
        self.assertNotIn("Decimal", serialized)


class SingleAdContractTests(unittest.TestCase):
    def test_effective_upper_is_inventory_times_price_when_declared_max_is_missing(self):
        ad = make_ad("missing-high", price="7.25", upper=None, available="10")
        self.assertEqual(ad.effective_max_fiat, "72.5")

    def test_effective_upper_uses_lower_of_declared_limit_and_inventory_value(self):
        declared = make_ad("declared", price="10", upper="80", available="20")
        inventory = make_ad("inventory", price="10", upper="500", available="20")
        self.assertEqual(declared.effective_max_fiat, "80")
        self.assertEqual(inventory.effective_max_fiat, "200")

    def test_lower_greater_than_effective_upper_is_invalid(self):
        request = QuoteRequest("CNY", "USDT", "BUY", "100")
        result = build_quote_result(
            request,
            "binance",
            (make_ad("invalid-range", price="5", lower="100", upper="1000", available="10"),),
        )
        self.assertEqual(result.status, QuoteStatus.NO_MATCH)
        self.assertEqual(result.ads_considered, 0)
        self.assertEqual(result.range_error.ranges, ())  # type: ignore[union-attr]

    def test_buy_uses_inclusive_fiat_bounds_and_floors_to_asset_step(self):
        ad = make_ad("buy", price="3", lower="100", upper="150", available="50")
        request = QuoteRequest("CNY", "USDT", "BUY", "100", asset_step="0.1")

        match = match_ad(request, ad)

        self.assertIsNotNone(match)
        self.assertEqual(match.output_amount, "33.3")  # type: ignore[union-attr]
        self.assertEqual(match.actual_fiat, "99.9")  # type: ignore[union-attr]
        self.assertEqual(match.remainder, "0.1")  # type: ignore[union-attr]
        self.assertIsNotNone(match_ad(
            QuoteRequest("CNY", "USDT", "BUY", "150", asset_step="0.1"), ad
        ))
        self.assertIsNone(match_ad(
            QuoteRequest("CNY", "USDT", "BUY", "150.01", asset_step="0.1"), ad
        ))

    def test_buy_floor_remains_exact_at_large_boundary_and_rejects_oversized_result(self):
        amount = "1" + "0" * 150
        request = QuoteRequest("CNY", "USDT", "BUY", amount, asset_step="1")
        huge = make_ad(
            "huge", price="3", lower="0", upper=None, available=amount
        )

        match = match_ad(request, huge)

        self.assertEqual(match.output_amount, "3" * 150)  # type: ignore[union-attr]
        self.assertEqual(match.remainder, "1")  # type: ignore[union-attr]

        oversized = "1" + "0" * 1000
        with self.assertRaises((C2CModelError, ProviderProtocolError)):
            match_ad(
                QuoteRequest("CNY", "USDT", "BUY", oversized, asset_step="1"),
                make_ad("oversized", price="3", lower="0", upper=None, available=oversized),
            )

    def test_sell_floors_fiat_result_and_checks_range_and_inventory(self):
        ad = make_ad(
            "sell", direction="SELL", price="7.25", lower="100", upper="500", available="20"
        )
        request = QuoteRequest("CNY", "USDT", "SELL", "13.8", fiat_step="0.01")

        match = match_ad(request, ad)

        self.assertEqual(match.output_amount, "100.05")  # type: ignore[union-attr]
        self.assertEqual(match.actual_crypto, "13.8")  # type: ignore[union-attr]
        self.assertIsNone(match_ad(
            QuoteRequest("CNY", "USDT", "SELL", "13.79", fiat_step="0.01"), ad
        ))
        self.assertIsNone(match_ad(
            QuoteRequest("CNY", "USDT", "SELL", "20.01", fiat_step="0.01"), ad
        ))

    def test_market_best_and_amount_match_best_are_separate(self):
        request = QuoteRequest("CNY", "USDT", "BUY", "1000", asset_step="0.01")
        display_best = make_ad("display", price="7.1", lower="5000", upper="10000")
        amount_best = make_ad("matched", price="7.2", lower="100", upper="3000")

        result = build_quote_result(request, "binance", (amount_best, display_best))

        self.assertEqual(result.market_best_price, "7.1")
        self.assertEqual(result.market_best_provider, "binance")
        self.assertEqual(result.match.price, "7.2")  # type: ignore[union-attr]
        self.assertEqual(result.match.ad.ad_id, "matched")  # type: ignore[union-attr]

    def test_sell_market_and_match_sort_by_highest_price(self):
        request = QuoteRequest("CNY", "USDT", "SELL", "20", fiat_step="0.01")
        low = make_ad("low", direction="SELL", price="7.1")
        high = make_ad("high", direction="SELL", price="7.3")

        result = build_quote_result(request, "binance", (low, high))

        self.assertEqual(result.market_best_price, "7.3")
        self.assertEqual(result.match.ad.ad_id, "high")  # type: ignore[union-attr]

    def test_ties_use_completion_orders_inventory_then_stable_id(self):
        request = QuoteRequest("CNY", "USDT", "BUY", "1000")
        ads = (
            make_ad("z", completion="98", orders="500", available="1000"),
            make_ad("c", completion="99", orders="200", available="2000"),
            make_ad("b", completion="99", orders="200", available="3000"),
            make_ad("a", completion="99", orders="200", available="3000"),
        )
        ranked = eligible_ads(request, ads)
        self.assertEqual([ad.ad_id for ad in ranked], ["a", "b", "c", "z"])

    def test_payment_filter_is_exact_and_qualification_warning_is_explicit(self):
        request = QuoteRequest(
            "CNY", "USDT", "BUY", "500", payment_methods=("FIXTURE_WALLET",)
        )
        bank = make_ad("bank", payments=("FIXTURE_BANK",))
        wallet = make_ad("wallet", payments=("FIXTURE_WALLET",))

        result = build_quote_result(request, "binance", (bank, wallet))

        self.assertEqual(result.match.ad.ad_id, "wallet")  # type: ignore[union-attr]
        self.assertIn(QUALIFICATION_WARNING, result.warnings)

    def test_no_amount_match_returns_true_effective_ranges_not_a_fake_quote(self):
        request = QuoteRequest("CNY", "USDT", "BUY", "50")
        ad = make_ad("range", price="10", lower="100", upper="5000", available="200")

        result = build_quote_result(request, "binance", (ad,))

        self.assertEqual(result.status, QuoteStatus.NO_MATCH)
        self.assertEqual(result.data_state, DataState.LIVE)
        self.assertIsNone(result.match)
        self.assertEqual(result.market_best_price, "10")
        self.assertEqual(result.range_error.ranges[0].lower, "100")  # type: ignore[union-attr]
        self.assertEqual(result.range_error.ranges[0].upper, "2000")  # type: ignore[union-attr]
        self.assertIn("未被冒充", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
