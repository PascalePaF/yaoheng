from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from c2c.base import BaseP2PAdapter, ProviderUnavailableError
from c2c.models import (
    DataState,
    NormalizedAd,
    ProviderCapability,
    QuoteRequest,
    QuoteStatus,
)
from c2c.service import C2CQuoteService, MARKET_FALLBACK_WARNING


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def ad(
    provider: str,
    ad_id: str,
    price: str,
    *,
    direction: str = "BUY",
    lower: str = "100",
    upper: str = "5000",
    available: str = "1000",
) -> NormalizedAd:
    return NormalizedAd(
        provider=provider,
        ad_id=ad_id,
        fiat="CNY",
        asset="USDT",
        direction=direction,
        price=price,
        min_fiat=lower,
        max_fiat=upper,
        available_asset=available,
        payment_methods=("FIXTURE_BANK",),
        completion_rate="99",
        completed_orders="100",
    )


class SequenceAdapter(BaseP2PAdapter):
    def __init__(
        self,
        provider: str,
        outcomes,
        *,
        enabled: bool = True,
        configured: bool = True,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.provider = provider
        self.outcomes = list(outcomes)
        self.enabled = enabled
        self.configured = configured
        self.entered = entered
        self.release = release
        self.calls = 0
        self._lock = threading.Lock()

    @property
    def capability(self):
        return ProviderCapability(
            provider=self.provider,
            enabled=self.enabled,
            configured=self.configured,
            requires_whitelist=self.provider == "okx",
            quote_price=False,
            ad_list=True,
            trade_methods=False,
            note="offline fixture adapter",
        )

    def fetch_ads(self, request, *, cancel=None):
        with self._lock:
            index = self.calls
            self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(2)
        outcome = self.outcomes[min(index, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return tuple(outcome)


class MemoryCacheTests(unittest.TestCase):
    def test_ten_second_fresh_cache_uses_no_second_provider_call(self):
        clock = FakeClock()
        adapter = SequenceAdapter("binance", [(ad("binance", "a", "7.2"),)])
        service = C2CQuoteService({"binance": adapter}, clock=clock)
        request = QuoteRequest("CNY", "USDT", "BUY", "1000", provider="binance")

        live = service.quote(request)
        clock.advance(10)
        cached = service.quote(request)

        self.assertEqual(live.data_state, DataState.LIVE)
        self.assertEqual(cached.data_state, DataState.FRESH_CACHE)
        self.assertEqual(adapter.calls, 1)

    def test_live_failure_uses_only_sixty_second_stale_grace(self):
        clock = FakeClock()
        adapter = SequenceAdapter(
            "binance",
            [(ad("binance", "a", "7.2"),), ProviderUnavailableError("offline")],
        )
        service = C2CQuoteService({"binance": adapter}, clock=clock)
        request = QuoteRequest("CNY", "USDT", "BUY", "1000", provider="binance")
        service.quote(request)
        clock.advance(11)

        stale = service.quote(request)

        self.assertEqual(stale.status, QuoteStatus.OK)
        self.assertEqual(stale.data_state, DataState.STALE_CACHE)
        self.assertTrue(any("缓存报价" in warning for warning in stale.warnings))
        self.assertEqual(adapter.calls, 2)

        clock.advance(50)
        expired = service.quote(request)
        self.assertEqual(expired.status, QuoteStatus.UNAVAILABLE)
        self.assertNotEqual(expired.data_state, DataState.STALE_CACHE)

    def test_failure_negative_cache_lasts_four_seconds(self):
        clock = FakeClock()
        adapter = SequenceAdapter(
            "binance", [ProviderUnavailableError("offline"), (ad("binance", "a", "7.2"),)]
        )
        service = C2CQuoteService({"binance": adapter}, clock=clock)
        request = QuoteRequest("CNY", "USDT", "BUY", "1000", provider="binance")

        first = service.quote(request)
        clock.advance(3.9)
        negative = service.quote(request)
        clock.advance(0.2)
        recovered = service.quote(request)

        self.assertEqual(first.status, QuoteStatus.UNAVAILABLE)
        self.assertEqual(negative.data_state, DataState.NEGATIVE_CACHE)
        self.assertEqual(recovered.status, QuoteStatus.OK)
        self.assertEqual(adapter.calls, 2)

    def test_cache_never_reuses_request_id_or_generation(self):
        adapter = SequenceAdapter("binance", [(ad("binance", "a", "7.2"),)])
        service = C2CQuoteService({"binance": adapter})
        first = QuoteRequest(
            "CNY", "USDT", "BUY", "1000", provider="binance", request_id="one", generation=1
        )
        second = QuoteRequest(
            "CNY", "USDT", "BUY", "1000", provider="binance", request_id="two", generation=2
        )

        one = service.quote(first)
        two = service.quote(second)

        self.assertEqual((one.request_id, one.generation), ("one", 1))
        self.assertEqual((two.request_id, two.generation), ("two", 2))
        self.assertEqual(adapter.calls, 1)


class ConcurrencyAndCancellationTests(unittest.TestCase):
    def test_identical_requests_merge_one_inflight_provider_call(self):
        entered = threading.Event()
        release = threading.Event()
        adapter = SequenceAdapter(
            "binance",
            [(ad("binance", "a", "7.2"),)],
            entered=entered,
            release=release,
        )
        service = C2CQuoteService({"binance": adapter})
        request = QuoteRequest("CNY", "USDT", "BUY", "1000", provider="binance")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.quote, request)
            self.assertTrue(entered.wait(1))
            second = pool.submit(service.quote, request)
            release.set()
            results = (first.result(2), second.result(2))

        self.assertEqual(adapter.calls, 1)
        self.assertTrue(all(item.status is QuoteStatus.OK for item in results))

    def test_waiting_follower_can_cancel_without_cancelling_shared_leader(self):
        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        adapter = SequenceAdapter(
            "binance",
            [(ad("binance", "a", "7.2"),)],
            entered=entered,
            release=release,
        )
        service = C2CQuoteService({"binance": adapter})
        request = QuoteRequest(
            "CNY", "USDT", "BUY", "1000", provider="binance", generation="old"
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(service.quote, request)
            self.assertTrue(entered.wait(1))
            follower = pool.submit(service.quote, request, cancel=cancelled)
            cancelled.set()
            follower_result = follower.result(2)
            release.set()
            leader_result = leader.result(2)

        self.assertEqual(follower_result.status, QuoteStatus.CANCELLED)
        self.assertEqual(follower_result.generation, "old")
        self.assertEqual(leader_result.status, QuoteStatus.OK)
        self.assertEqual(adapter.calls, 1)


class AutoProviderTests(unittest.TestCase):
    def test_auto_compares_only_enabled_configured_platforms_and_picks_buy_lowest(self):
        binance = SequenceAdapter("binance", [(ad("binance", "b", "7.2"),)])
        okx = SequenceAdapter("okx", [(ad("okx", "o", "7.15"),)])
        disabled = SequenceAdapter("disabled", [(ad("disabled", "d", "7"),)], enabled=False)
        service = C2CQuoteService({
            "binance": binance,
            "okx": okx,
            "disabled": disabled,
        })

        result = service.quote(QuoteRequest("CNY", "USDT", "BUY", "1000"))

        self.assertEqual(result.status, QuoteStatus.OK)
        self.assertEqual(result.provider, "okx")
        self.assertEqual(result.match.provider, "okx")  # type: ignore[union-attr]
        self.assertEqual(result.compared_providers, ("binance", "okx"))
        self.assertEqual(disabled.calls, 0)

    def test_auto_reports_market_source_separately_from_amount_match_source(self):
        binance = SequenceAdapter(
            "binance", [(ad("binance", "display", "7.1", lower="5000"),)]
        )
        okx = SequenceAdapter("okx", [(ad("okx", "match", "7.2", upper="2000"),)])
        service = C2CQuoteService({"binance": binance, "okx": okx})

        result = service.quote(QuoteRequest("CNY", "USDT", "BUY", "1000"))

        self.assertEqual(result.provider, "okx")
        self.assertEqual(result.market_best_price, "7.1")
        self.assertEqual(result.market_best_provider, "binance")
        self.assertEqual(result.match.price, "7.2")  # type: ignore[union-attr]

    def test_auto_sell_chooses_highest_price(self):
        binance = SequenceAdapter(
            "binance", [(ad("binance", "b", "7.2", direction="SELL"),)]
        )
        okx = SequenceAdapter("okx", [(ad("okx", "o", "7.3", direction="SELL"),)])
        result = C2CQuoteService({"binance": binance, "okx": okx}).quote(
            QuoteRequest("CNY", "USDT", "SELL", "20")
        )
        self.assertEqual(result.provider, "okx")
        self.assertEqual(result.market_best_price, "7.3")

    def test_auto_skips_unconfigured_okx_without_pretending_it_is_a_candidate(self):
        binance = SequenceAdapter("binance", [(ad("binance", "b", "7.2"),)])
        okx = SequenceAdapter("okx", [(ad("okx", "o", "7"),)], configured=False)
        service = C2CQuoteService({"binance": binance, "okx": okx})

        result = service.quote(QuoteRequest("CNY", "USDT", "BUY", "1000"))

        self.assertEqual(result.provider, "binance")
        self.assertEqual(result.compared_providers, ("binance",))
        self.assertEqual(okx.calls, 0)
        self.assertTrue(any("okx 未配置" in warning for warning in result.warnings))

    def test_auto_no_match_combines_truthful_ranges(self):
        binance = SequenceAdapter(
            "binance", [(ad("binance", "b", "7.2", lower="100", upper="500"),)]
        )
        okx = SequenceAdapter(
            "okx", [(ad("okx", "o", "7.1", lower="1000", upper="2000"),)]
        )
        result = C2CQuoteService({"binance": binance, "okx": okx}).quote(
            QuoteRequest("CNY", "USDT", "BUY", "750", allow_market_fallback=True)
        )

        self.assertEqual(result.status, QuoteStatus.NO_MATCH)
        self.assertIsNone(result.match)
        self.assertEqual(
            {(item.provider, item.lower, item.upper) for item in result.range_error.ranges},  # type: ignore[union-attr]
            {("binance", "100", "500"), ("okx", "1000", "2000")},
        )


class MarketFallbackTests(unittest.TestCase):
    def test_unavailable_platform_can_use_injected_non_c2c_market_fallback(self):
        calls = []

        def fallback(request):
            calls.append(request.cache_key)
            return {"price": "7.25", "source": "ordinary_market"}

        adapter = SequenceAdapter("binance", [ProviderUnavailableError("offline")])
        service = C2CQuoteService({"binance": adapter}, market_fallback=fallback)
        request = QuoteRequest(
            "CNY", "USDT", "BUY", "1000", provider="binance",
            asset_step="0.01", allow_market_fallback=True
        )

        result = service.quote(request)

        self.assertEqual(result.status, QuoteStatus.MARKET_FALLBACK)
        self.assertEqual(result.data_state, DataState.MARKET_FALLBACK)
        self.assertFalse(result.is_c2c_executable)
        self.assertEqual(result.indicative_price, "7.25")
        self.assertEqual(result.indicative_output_amount, "137.93")
        self.assertIsNone(result.market_best_price)
        self.assertIn(MARKET_FALLBACK_WARNING, result.warnings)
        self.assertEqual(len(calls), 1)

    def test_advertisements_with_no_amount_match_never_use_market_fallback(self):
        calls = []
        adapter = SequenceAdapter(
            "binance", [(ad("binance", "a", "7.2", lower="5000"),)]
        )
        service = C2CQuoteService(
            {"binance": adapter}, market_fallback=lambda request: calls.append(request) or "7"
        )
        result = service.quote(QuoteRequest(
            "CNY", "USDT", "BUY", "1000", provider="binance", allow_market_fallback=True
        ))

        self.assertEqual(result.status, QuoteStatus.NO_MATCH)
        self.assertEqual(calls, [])
        self.assertEqual(result.range_error.ranges[0].lower, "5000")  # type: ignore[union-attr]

    def test_fallback_rejects_float_and_keeps_failure_prominent(self):
        adapter = SequenceAdapter("binance", [ProviderUnavailableError("offline")])
        service = C2CQuoteService({"binance": adapter}, market_fallback=lambda request: 7.2)
        result = service.quote(QuoteRequest(
            "CNY", "USDT", "BUY", "1000", provider="binance", allow_market_fallback=True
        ))

        self.assertEqual(result.status, QuoteStatus.UNAVAILABLE)
        self.assertTrue(any("回调失败" in warning for warning in result.warnings))

    def test_short_negative_cache_preserves_market_fallback_label(self):
        adapter = SequenceAdapter("binance", [ProviderUnavailableError("offline")])
        clock = FakeClock()
        service = C2CQuoteService(
            {"binance": adapter}, market_fallback=lambda request: "7.2", clock=clock
        )
        request = QuoteRequest(
            "CNY", "USDT", "BUY", "1000", provider="binance", allow_market_fallback=True
        )

        service.quote(request)
        cached = service.quote(request)

        self.assertEqual(cached.status, QuoteStatus.MARKET_FALLBACK)
        self.assertEqual(cached.data_state, DataState.MARKET_FALLBACK)
        self.assertIn("非 C2C 可成交价", " ".join(cached.warnings))
        self.assertEqual(adapter.calls, 1)


class CapabilityTests(unittest.TestCase):
    def test_capabilities_are_safe_to_serialize(self):
        adapter = SequenceAdapter("binance", [(ad("binance", "a", "7.2"),)])
        capabilities = C2CQuoteService({"binance": adapter}).capabilities()
        payload = json.dumps([item.to_dict() for item in capabilities], ensure_ascii=False)
        self.assertIn('"provider": "binance"', payload)
        self.assertNotIn("credential", payload.lower())


if __name__ == "__main__":
    unittest.main()
