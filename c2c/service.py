"""Thread-safe C2C quote orchestration with memory-only resilience layers."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import DecimalException, localcontext
from threading import Event, RLock
from typing import Callable, Mapping

from conversion_core import DecimalBoundaryError, canonical_rate_string, decimal_to_canonical, parse_amount

from .base import (
    BaseP2PAdapter,
    CancellationToken,
    ProviderProtocolError,
    _ad_sort_key,
    _divide_to_step,
    _floor_to_step,
)
from .binance import BinanceP2PAdapter
from .models import (
    DataState,
    Direction,
    ProviderCapability,
    QuoteRequest,
    QuoteResult,
    QuoteStatus,
    RangeError,
)
from .okx import OkxP2PAdapter


FRESH_CACHE_SECONDS = 10.0
STALE_CACHE_SECONDS = 60.0
NEGATIVE_CACHE_SECONDS = 4.0
STALE_CACHE_WARNING = "实时刷新失败，当前为 60 秒宽限内的缓存报价。"
MARKET_FALLBACK_WARNING = "⚠ 非 C2C 可成交价：普通行情仅供参考，不代表任何单广告可成交。"


MarketFallback = Callable[[QuoteRequest], object]


@dataclass(slots=True)
class _CacheEntry:
    result: QuoteResult
    stored_at: float


@dataclass(slots=True)
class _Flight:
    event: Event
    result: QuoteResult | None = None


def _dedupe(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in items if str(item)))


class C2CQuoteService:
    """Amount-aware C2C service; all caches are process memory only."""

    def __init__(
        self,
        providers: Mapping[str, BaseP2PAdapter] | None = None,
        *,
        market_fallback: MarketFallback | None = None,
        clock: Callable[[], float] = time.monotonic,
        fresh_seconds: float = FRESH_CACHE_SECONDS,
        stale_seconds: float = STALE_CACHE_SECONDS,
        negative_seconds: float = NEGATIVE_CACHE_SECONDS,
    ) -> None:
        if providers is None:
            providers = {
                "binance": BinanceP2PAdapter(),
                "okx": OkxP2PAdapter(),
            }
        normalized: dict[str, BaseP2PAdapter] = {}
        for _name, adapter in providers.items():
            capability = adapter.capability
            if not capability.read_only:
                raise ValueError("C2CQuoteService 仅接受只读平台适配器")
            provider = capability.provider
            if provider in normalized:
                raise ValueError(f"重复 C2C 平台：{provider}")
            normalized[provider] = adapter
        if not (0 < fresh_seconds <= stale_seconds) or not (3 <= negative_seconds <= 5):
            raise ValueError("C2C 缓存时长配置无效")
        self._providers = normalized
        self._market_fallback = market_fallback
        self._clock = clock
        self._fresh_seconds = float(fresh_seconds)
        self._stale_seconds = float(stale_seconds)
        self._negative_seconds = float(negative_seconds)
        self._positive_cache: dict[tuple[object, ...], _CacheEntry] = {}
        self._negative_cache: dict[tuple[object, ...], _CacheEntry] = {}
        self._inflight: dict[tuple[object, ...], _Flight] = {}
        self._lock = RLock()

    def capabilities(self) -> tuple[ProviderCapability, ...]:
        return tuple(adapter.capability for adapter in self._providers.values())

    def clear_memory_cache(self) -> None:
        with self._lock:
            self._positive_cache.clear()
            self._negative_cache.clear()

    def quote(
        self,
        request: QuoteRequest,
        *,
        cancel: CancellationToken | None = None,
    ) -> QuoteResult:
        if not isinstance(request, QuoteRequest):
            raise TypeError("request 必须为 QuoteRequest")
        if cancel is not None and cancel.is_set():
            return self._cancelled(request)
        key = request.cache_key
        cached = self._cached_result(key)
        if cached is not None:
            return cached.for_request(request)

        with self._lock:
            flight = self._inflight.get(key)
            if flight is None:
                flight = _Flight(Event())
                self._inflight[key] = flight
                leader = True
            else:
                leader = False
        if not leader:
            while not flight.event.wait(0.05):
                if cancel is not None and cancel.is_set():
                    return self._cancelled(request)
            result = flight.result
            if result is None:
                return self._error_result(request, "合并请求未产生结果。")
            return result.for_request(request)

        try:
            result = self._refresh(key, request, cancel)
        except Exception:
            # A provider implementation bug must not strand other callers or
            # leak exception text that could contain upstream data.
            result = self._error_result(request, "C2C 报价服务内部失败，未生成可成交价。")
        finally:
            with self._lock:
                active = self._inflight.pop(key, flight)
                if "result" in locals():
                    active.result = result
                active.event.set()
        return result.for_request(request)

    def _cached_result(self, key: tuple[object, ...]) -> QuoteResult | None:
        now = self._clock()
        with self._lock:
            positive = self._positive_cache.get(key)
            negative = self._negative_cache.get(key)
            if positive is not None and now - positive.stored_at <= self._fresh_seconds:
                return positive.result.with_data_state(
                    DataState.FRESH_CACHE, "使用 10 秒新鲜期内的内存 C2C 缓存。"
                )
            if negative is not None and now - negative.stored_at <= self._negative_seconds:
                if positive is not None and now - positive.stored_at <= self._stale_seconds:
                    return positive.result.with_data_state(DataState.STALE_CACHE, STALE_CACHE_WARNING)
                if negative.result.status is QuoteStatus.MARKET_FALLBACK:
                    return negative.result.with_data_state(
                        DataState.MARKET_FALLBACK, "普通行情降级结果来自短时失败缓存。"
                    )
                return negative.result.with_data_state(
                    DataState.NEGATIVE_CACHE, "平台失败结果处于 3–5 秒负缓存保护期。"
                )
            if positive is not None and now - positive.stored_at > self._stale_seconds:
                self._positive_cache.pop(key, None)
            if negative is not None and now - negative.stored_at > self._negative_seconds:
                self._negative_cache.pop(key, None)
        return None

    def _refresh(
        self,
        key: tuple[object, ...],
        request: QuoteRequest,
        cancel: CancellationToken | None,
    ) -> QuoteResult:
        result = self._fetch(request, cancel)
        now = self._clock()
        if result.status in {QuoteStatus.OK, QuoteStatus.NO_MATCH}:
            with self._lock:
                self._positive_cache[key] = _CacheEntry(result, now)
                self._negative_cache.pop(key, None)
            return result
        if result.status is QuoteStatus.CANCELLED:
            return result
        with self._lock:
            self._negative_cache[key] = _CacheEntry(result, now)
            stale = self._positive_cache.get(key)
        if stale is not None and now - stale.stored_at <= self._stale_seconds:
            return stale.result.with_data_state(DataState.STALE_CACHE, STALE_CACHE_WARNING)
        if request.allow_market_fallback and result.status not in {QuoteStatus.NO_MATCH}:
            fallback = self._ordinary_market_result(request, result, cancel)
            if fallback is not None:
                with self._lock:
                    self._negative_cache[key] = _CacheEntry(fallback, now)
                return fallback
        return result

    def _fetch(
        self,
        request: QuoteRequest,
        cancel: CancellationToken | None,
    ) -> QuoteResult:
        if request.provider != "auto":
            adapter = self._providers.get(request.provider)
            if adapter is None:
                return QuoteResult(
                    provider=request.provider,
                    status=QuoteStatus.UNCONFIGURED,
                    data_state=DataState.NONE,
                    fiat=request.fiat,
                    asset=request.asset,
                    direction=request.direction,
                    input_amount=request.amount,
                    warnings=("所选 C2C 平台未注册。",),
                )
            return adapter.quote(request, cancel=cancel)

        candidates: list[BaseP2PAdapter] = []
        skipped: list[str] = []
        for adapter in self._providers.values():
            capability = adapter.capability
            if capability.enabled and capability.configured:
                candidates.append(adapter)
            elif capability.enabled and not capability.configured:
                skipped.append(f"{capability.provider} 未配置/无权限，未作为 auto 候选。")
        if not candidates:
            return QuoteResult(
                provider=None,
                status=QuoteStatus.UNCONFIGURED,
                data_state=DataState.NONE,
                fiat=request.fiat,
                asset=request.asset,
                direction=request.direction,
                input_amount=request.amount,
                warnings=_dedupe(skipped + ["auto 没有已启用且已配置的 C2C 平台。"]),
            )

        results: list[QuoteResult] = []
        with ThreadPoolExecutor(max_workers=len(candidates), thread_name_prefix="c2c-auto") as pool:
            futures = [pool.submit(adapter.quote, request, cancel=cancel) for adapter in candidates]
            for adapter, future in zip(candidates, futures):
                try:
                    results.append(future.result())
                except Exception:
                    results.append(QuoteResult(
                        provider=adapter.capability.provider,
                        status=QuoteStatus.UNAVAILABLE,
                        data_state=DataState.NONE,
                        fiat=request.fiat,
                        asset=request.asset,
                        direction=request.direction,
                        input_amount=request.amount,
                        warnings=("平台适配器失败，未返回 C2C 可成交价。",),
                    ))
        compared = tuple(adapter.capability.provider for adapter in candidates)
        matched = [item for item in results if item.status is QuoteStatus.OK and item.match]
        market_price, market_provider = self._market_best(results, request.direction)
        ancillary_warnings = list(skipped)
        for item in results:
            if item.status not in {QuoteStatus.OK, QuoteStatus.NO_MATCH}:
                ancillary_warnings.extend(item.warnings)

        if matched:
            winner = min(
                matched,
                key=lambda item: _ad_sort_key(item.match.ad, request.direction) +
                (item.match.provider,),  # type: ignore[union-attr]
            )
            return replace(
                winner,
                market_best_price=market_price,
                market_best_provider=market_provider,
                ads_considered=sum(item.ads_considered for item in results),
                compared_providers=compared,
                warnings=_dedupe(list(winner.warnings) + ancillary_warnings),
            )

        no_matches = [item for item in results if item.status is QuoteStatus.NO_MATCH]
        if no_matches:
            ranges = []
            seen = set()
            for item in no_matches:
                if item.range_error is None:
                    continue
                for amount_range in item.range_error.ranges:
                    signature = (
                        amount_range.provider,
                        amount_range.ad_id,
                        amount_range.lower,
                        amount_range.upper,
                        amount_range.payment_methods,
                    )
                    if signature not in seen:
                        seen.add(signature)
                        ranges.append(amount_range)
            return QuoteResult(
                provider=market_provider,
                status=QuoteStatus.NO_MATCH,
                data_state=DataState.LIVE,
                fiat=request.fiat,
                asset=request.asset,
                direction=request.direction,
                input_amount=request.amount,
                market_best_price=market_price,
                market_best_provider=market_provider,
                range_error=RangeError(request.amount, request.amount_unit, tuple(ranges)),
                ads_considered=sum(item.ads_considered for item in no_matches),
                compared_providers=compared,
                warnings=_dedupe([
                    "已有 C2C 广告但金额未命中单广告范围；未使用展示最优价代替。",
                    *skipped,
                ]),
            )

        if cancel is not None and cancel.is_set():
            return self._cancelled(request, compared)
        priority = {
            QuoteStatus.RATE_LIMITED: 0,
            QuoteStatus.CIRCUIT_OPEN: 1,
            QuoteStatus.PERMISSION_DENIED: 2,
            QuoteStatus.INVALID_RESPONSE: 3,
            QuoteStatus.UNAVAILABLE: 4,
            QuoteStatus.UNCONFIGURED: 5,
            QuoteStatus.CANCELLED: 6,
        }
        selected = min(results, key=lambda item: priority.get(item.status, 99))
        return replace(
            selected,
            compared_providers=compared,
            warnings=_dedupe(list(selected.warnings) + ancillary_warnings),
        )

    @staticmethod
    def _market_best(
        results: list[QuoteResult], direction: Direction
    ) -> tuple[str | None, str | None]:
        priced = [
            (parse_amount(item.market_best_price), item.market_best_price, item.market_best_provider)
            for item in results
            if item.market_best_price is not None and item.market_best_provider is not None
        ]
        if not priced:
            return None, None
        chosen = min(priced, key=lambda item: item[0]) if direction is Direction.BUY else max(
            priced, key=lambda item: item[0]
        )
        return chosen[1], chosen[2]

    def _ordinary_market_result(
        self,
        request: QuoteRequest,
        failed: QuoteResult,
        cancel: CancellationToken | None,
    ) -> QuoteResult | None:
        if self._market_fallback is None or (cancel is not None and cancel.is_set()):
            return None
        try:
            raw = self._market_fallback(request)
            source = "ordinary_market"
            if isinstance(raw, Mapping):
                source = str(raw.get("source") or raw.get("provider") or source).strip().lower()
                raw_price = raw.get("price") if "price" in raw else raw.get("rate")
            else:
                raw_price = raw
            if isinstance(raw_price, (bool, float)):
                raise ValueError("普通行情必须返回十进制字符串或 Decimal")
            price_text = canonical_rate_string(raw_price)
            price = parse_amount(price_text)
            amount = parse_amount(request.amount)
            if request.direction is Direction.BUY:
                step = parse_amount(request.resolved_asset_step)
                try:
                    output = _divide_to_step(amount, price, step)
                except (DecimalException, DecimalBoundaryError) as exc:
                    raise ProviderProtocolError("普通行情计算超出范围") from exc
                output_unit = request.asset
            else:
                step = parse_amount(request.resolved_fiat_step)
                try:
                    with localcontext() as context:
                        context.prec = 440
                        output = _floor_to_step(amount * price, step)
                except (DecimalException, DecimalBoundaryError) as exc:
                    raise ProviderProtocolError("普通行情计算超出范围") from exc
                output_unit = request.fiat
            return QuoteResult(
                provider=source,
                status=QuoteStatus.MARKET_FALLBACK,
                data_state=DataState.MARKET_FALLBACK,
                fiat=request.fiat,
                asset=request.asset,
                direction=request.direction,
                input_amount=request.amount,
                indicative_price=price_text,
                indicative_output_amount=decimal_to_canonical(output),
                output_unit=output_unit,
                compared_providers=failed.compared_providers,
                warnings=_dedupe([MARKET_FALLBACK_WARNING, *failed.warnings]),
            )
        except Exception:
            return replace(
                failed,
                warnings=_dedupe([*failed.warnings, "普通行情降级回调失败，未返回参考价。"]),
            )

    def _cancelled(
        self,
        request: QuoteRequest,
        compared: tuple[str, ...] = (),
    ) -> QuoteResult:
        return QuoteResult(
            provider=None if request.provider == "auto" else request.provider,
            status=QuoteStatus.CANCELLED,
            data_state=DataState.NONE,
            fiat=request.fiat,
            asset=request.asset,
            direction=request.direction,
            input_amount=request.amount,
            compared_providers=compared,
            warnings=("C2C 请求已取消，可由 UI 丢弃该 generation 的结果。",),
            request_id=request.request_id,
            generation=request.generation,
        )

    @staticmethod
    def _error_result(request: QuoteRequest, warning: str) -> QuoteResult:
        return QuoteResult(
            provider=None if request.provider == "auto" else request.provider,
            status=QuoteStatus.UNAVAILABLE,
            data_state=DataState.NONE,
            fiat=request.fiat,
            asset=request.asset,
            direction=request.direction,
            input_amount=request.amount,
            warnings=(warning,),
        )


C2CService = C2CQuoteService


__all__ = [
    "C2CQuoteService",
    "C2CService",
    "FRESH_CACHE_SECONDS",
    "MARKET_FALLBACK_WARNING",
    "MarketFallback",
    "NEGATIVE_CACHE_SECONDS",
    "STALE_CACHE_SECONDS",
    "STALE_CACHE_WARNING",
]
