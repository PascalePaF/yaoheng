"""Seven-currency exchange state, quote coordination, and Tk page.

The state and coordinator intentionally have no Tk dependency.  They are used
by both the seven-currency page and the legacy three-way crypto converter so
amount-aware C2C routing and status wording stay in one place.
"""

from __future__ import annotations

import re
import threading
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from queue import Empty, SimpleQueue
from tkinter import ttk
from typing import Any, Callable, Mapping, Sequence

from c2c.models import DataState, Direction, QuoteRequest, QuoteResult, QuoteStatus
from calculator_core import CalculationError, evaluate_basic_amount_decimal
from conversion_core import (
    AmountInputError,
    canonical_amount_string,
    format_for_display,
    normalize_currency_code,
    parse_amount,
)


DEFAULT_EXCHANGE_CURRENCIES = ("CNY", "USD", "EUR", "JPY", "HKD", "BTC", "USDT")
SUPPORTED_MODES = frozenset({"market", "c2c"})
SUPPORTED_PROVIDERS = ("auto", "binance", "okx")
PROVIDER_LABELS = {
    "auto": "自动",
    "binance": "Binance",
    "okx": "OKX",
}
PROVIDER_BY_LABEL = {label: provider for provider, label in PROVIDER_LABELS.items()}
PAYMENT_ALL_LABEL = "不限支付方式"
PAYMENT_UNKNOWN_LABEL = "不限支付方式（平台未提供列表）"
_PAYMENT_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


def _mapping_value(value: object, name: str, fallback: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, fallback)
    return getattr(value, name, fallback)


def _enum_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _normalize_payment_method(value: object) -> str:
    text = str(value or "").strip()
    return text if _PAYMENT_RE.fullmatch(text) is not None else ""


@dataclass(frozen=True, slots=True)
class ExchangeEdgeResult:
    """One target card result, including the exact value used for swapping."""

    slot: int
    generation: int
    source: str
    target: str
    route: str
    exact_value: str | None
    display_value: str
    status: str
    state: str
    details: tuple[str, ...] = ()
    matched_price: str | None = None
    market_best_price: str | None = None
    provider: str | None = None

    @property
    def valid(self) -> bool:
        return self.exact_value is not None


@dataclass(frozen=True, slots=True)
class C2CQuoteJob:
    slot: int
    generation: int
    source: str
    target: str
    request: QuoteRequest


@dataclass(frozen=True, slots=True)
class C2CBridgeJob:
    """Two-leg crypto-to-crypto C2C quote through an explicit fiat currency."""

    slot: int
    generation: int
    source: str
    target: str
    settlement_fiat: str
    sell_request: QuoteRequest
    provider: str
    payment_method: str = ""


@dataclass(frozen=True, slots=True)
class C2CBridgeExecution:
    """Provider responses for the sell-to-fiat and fiat-to-buy legs."""

    first_quote: object
    second_request: QuoteRequest | None = None
    second_quote: object | None = None


@dataclass(frozen=True, slots=True)
class PrimaryChange:
    previous_slot: int
    primary_slot: int
    amount_cleared: bool


@dataclass(slots=True)
class ExchangePageState:
    """Pure seven-slot state with uniqueness and stale-result protection."""

    currencies: list[str] = field(default_factory=lambda: list(DEFAULT_EXCHANGE_CURRENCIES))
    primary_slot: int = 0
    active_slot: int | None = None
    amount: str = "1"
    mode: str = "market"
    provider: str = "auto"
    payment_method: str = ""
    settlement_fiat: str = "CNY"
    generation: int = 0
    results: dict[int, ExchangeEdgeResult] = field(default_factory=dict, repr=False)
    persistable_amount: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        normalized = [normalize_currency_code(code) for code in self.currencies]
        if len(normalized) != 7 or len(set(normalized)) != 7:
            raise ValueError("兑换页必须包含七个唯一币种")
        if isinstance(self.primary_slot, bool) or not 0 <= int(self.primary_slot) < 7:
            raise ValueError("主币槽位无效")
        active_slot = self.primary_slot if self.active_slot is None else self.active_slot
        if isinstance(active_slot, bool) or not 0 <= int(active_slot) < 7:
            raise ValueError("当前输入槽位无效")
        mode = str(self.mode).strip().lower()
        if mode not in SUPPORTED_MODES:
            raise ValueError("兑换模式无效")
        provider = str(self.provider).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            provider = "auto"
        amount = str(self.amount or "").strip()
        if len(amount) > 4096:
            amount = ""
        payment = _normalize_payment_method(self.payment_method)
        settlement_fiat = normalize_currency_code(self.settlement_fiat or "CNY")
        self.currencies = normalized
        self.primary_slot = int(self.primary_slot)
        self.active_slot = int(active_slot)
        self.amount = amount
        if not amount:
            self.persistable_amount = ""
        else:
            try:
                self.persistable_amount = canonical_amount_string(amount)
            except AmountInputError:
                self.persistable_amount = ""
        self.mode = mode
        self.provider = provider
        self.payment_method = payment
        self.settlement_fiat = settlement_fiat
        self.generation = max(0, int(self.generation))
        self.results = {}

    @classmethod
    def from_mapping(cls, value: object) -> "ExchangePageState":
        defaults = cls()
        if not isinstance(value, Mapping):
            return defaults
        try:
            return cls(
                currencies=list(value.get("currencies", defaults.currencies)),
                primary_slot=value.get("primary_slot", defaults.primary_slot),
                active_slot=value.get("active_slot", value.get("primary_slot", defaults.primary_slot)),
                amount=str(value.get("amount", defaults.amount) or ""),
                mode=str(value.get("mode", defaults.mode)),
                provider=str(value.get("provider", defaults.provider)),
                payment_method=str(value.get("payment_method", "") or ""),
                settlement_fiat=str(value.get("settlement_fiat", defaults.settlement_fiat) or "CNY"),
            )
        except (TypeError, ValueError):
            return defaults

    @property
    def primary_code(self) -> str:
        return self.currencies[self.primary_slot]

    @property
    def active_code(self) -> str:
        return self.currencies[int(self.active_slot)]

    @property
    def target_slots(self) -> tuple[int, ...]:
        return tuple(slot for slot in range(7) if slot != self.primary_slot)

    @property
    def quote_slots(self) -> tuple[int, ...]:
        return tuple(slot for slot in range(7) if slot != self.active_slot)

    def to_dict(self) -> dict[str, object]:
        """Serialize selections and input only; derived quotes never persist."""

        return {
            "currencies": list(self.currencies),
            "primary_slot": self.primary_slot,
            "active_slot": self.active_slot,
            "amount": self.persistable_amount,
            "mode": self.mode,
            "provider": self.provider,
            "payment_method": self.payment_method,
            "settlement_fiat": self.settlement_fiat,
        }

    serialize = to_dict

    def _invalidate(self) -> int:
        self.generation += 1
        self.results.clear()
        return self.generation

    def invalidate(self) -> int:
        return self._invalidate()

    def set_amount(self, amount: object) -> bool:
        text = str(amount or "").strip()[:4096]
        if text == self.amount:
            return False
        self.amount = text
        if not text:
            self.persistable_amount = ""
        else:
            try:
                self.persistable_amount = canonical_amount_string(text)
            except AmountInputError:
                pass
        self._invalidate()
        return True

    def set_input(self, slot: int, amount: object) -> bool:
        if isinstance(slot, bool) or not 0 <= int(slot) < 7:
            raise IndexError("当前输入槽位无效")
        slot = int(slot)
        text = str(amount or "").strip()[:4096]
        slot_changed = slot != self.active_slot
        changed = slot_changed or text != self.amount
        if not changed:
            return False
        self.active_slot = slot
        self.amount = text
        if slot_changed:
            self.persistable_amount = ""
        if not text:
            self.persistable_amount = ""
        else:
            try:
                self.persistable_amount = canonical_amount_string(text)
            except AmountInputError:
                pass
        self._invalidate()
        return True

    def select_currency(self, slot: int, code: object) -> bool:
        if isinstance(slot, bool) or not 0 <= int(slot) < 7:
            raise IndexError("兑换币种槽位无效")
        slot = int(slot)
        normalized = normalize_currency_code(code)
        if self.currencies[slot] == normalized:
            return False
        if normalized in self.currencies:
            other = self.currencies.index(normalized)
            self.currencies[slot], self.currencies[other] = (
                self.currencies[other],
                self.currencies[slot],
            )
        else:
            self.currencies[slot] = normalized
        self._invalidate()
        return True

    choose_currency = select_currency

    def set_mode(self, mode: object) -> bool:
        normalized = str(mode).strip().lower()
        if normalized not in SUPPORTED_MODES:
            raise ValueError("兑换模式无效")
        if normalized == self.mode:
            return False
        self.mode = normalized
        self._invalidate()
        return True

    def set_provider(self, provider: object) -> bool:
        normalized = str(provider).strip().lower()
        if normalized not in SUPPORTED_PROVIDERS:
            raise ValueError("C2C 来源无效")
        if normalized == self.provider:
            return False
        self.provider = normalized
        self._invalidate()
        return True

    def set_payment_method(self, payment_method: object) -> bool:
        normalized = _normalize_payment_method(payment_method)
        if normalized == self.payment_method:
            return False
        self.payment_method = normalized
        self._invalidate()
        return True

    def set_settlement_fiat(self, code: object) -> bool:
        normalized = normalize_currency_code(code)
        if normalized == self.settlement_fiat:
            return False
        self.settlement_fiat = normalized
        if self.payment_method:
            self.payment_method = ""
        self._invalidate()
        return True

    def accept_result(self, result: ExchangeEdgeResult) -> bool:
        if (
            not isinstance(result, ExchangeEdgeResult)
            or result.generation != self.generation
            or result.slot == self.active_slot
            or not 0 <= result.slot < 7
            or result.source != self.active_code
            or result.target != self.currencies[result.slot]
        ):
            return False
        self.results[result.slot] = result
        return True

    def current_result(self, slot: int) -> ExchangeEdgeResult | None:
        result = self.results.get(slot)
        if result is None or result.generation != self.generation:
            return None
        return result

    def can_swap(self, slot: int) -> bool:
        result = self.current_result(slot)
        return bool(result and result.valid)

    def set_primary(self, slot: int) -> PrimaryChange:
        if isinstance(slot, bool) or not 0 <= int(slot) < 7:
            raise IndexError("主币槽位无效")
        slot = int(slot)
        previous = self.primary_slot
        if slot == previous:
            return PrimaryChange(previous, slot, False)
        self.primary_slot = slot
        self._invalidate()
        return PrimaryChange(previous, slot, False)

    def swap_with_primary(self, slot: int) -> bool:
        if isinstance(slot, bool) or not 0 <= int(slot) < 7 or int(slot) == self.primary_slot:
            return False
        slot = int(slot)
        result = self.current_result(slot)
        if result is None or result.exact_value is None:
            return False
        try:
            exact = canonical_amount_string(result.exact_value)
        except AmountInputError:
            return False
        self.primary_slot = slot
        self.active_slot = slot
        self.amount = exact
        self.persistable_amount = exact
        self._invalidate()
        return True


class ExchangeCoordinator:
    """Shared exact-market/C2C edge router and result mapper."""

    def __init__(self, rate_service: object, c2c_service: object | None = None) -> None:
        self.rate_service = rate_service
        self.c2c_service = c2c_service

    @staticmethod
    def route_edge(source: str, target: str, kinds: Mapping[str, str], mode: str) -> str:
        source_kind = str(kinds.get(source, "")).lower()
        target_kind = str(kinds.get(target, "")).lower()
        if mode == "c2c" and {source_kind, target_kind} == {"fiat", "crypto"}:
            return "c2c"
        if mode == "c2c" and source_kind == target_kind == "crypto":
            return "c2c_bridge"
        return "market"

    def prepare_edge(
        self,
        *,
        slot: int,
        generation: int,
        amount: object,
        source: str,
        target: str,
        kinds: Mapping[str, str],
        mode: str,
        provider: str = "auto",
        payment_method: str = "",
        payment_fiat: str = "",
        settlement_fiat: str = "CNY",
        from_cache: bool = False,
    ) -> ExchangeEdgeResult | C2CQuoteJob | C2CBridgeJob:
        route = self.route_edge(source, target, kinds, mode)
        try:
            amount_text = canonical_amount_string(amount)
        except (TypeError, ValueError):
            return ExchangeEdgeResult(
                slot, generation, source, target, route, None, "—",
                "请输入有效金额", "invalid", ("金额需为有限十进制数。",),
            )
        if route == "market":
            return self._market_result(
                slot, generation, amount_text, source, target, kinds, from_cache=from_cache,
                c2c_context=mode == "c2c",
            )
        try:
            if parse_amount(amount_text) <= 0:
                raise AmountInputError("C2C 金额必须大于零")
            payment = _normalize_payment_method(payment_method)
            payment_fiat_code = str(payment_fiat or "").upper()
            if route == "c2c_bridge":
                fiat = normalize_currency_code(settlement_fiat)
                if str(kinds.get(fiat, "")).lower() != "fiat":
                    raise AmountInputError("跨虚拟币 C2C 结算法币无效")
                sell_request = QuoteRequest(
                    fiat=fiat,
                    asset=source,
                    direction=Direction.SELL,
                    amount=amount_text,
                    provider=provider,
                    payment_methods=(payment,) if payment and fiat == payment_fiat_code else (),
                    allow_market_fallback=False,
                    request_id=f"ui-{generation}-{slot}-sell",
                    generation=generation,
                )
                return C2CBridgeJob(
                    slot,
                    generation,
                    source,
                    target,
                    fiat,
                    sell_request,
                    provider,
                    payment if fiat == payment_fiat_code else "",
                )
            source_kind = str(kinds.get(source, "")).lower()
            if source_kind == "fiat":
                fiat, asset, direction = source, target, Direction.BUY
            else:
                fiat, asset, direction = target, source, Direction.SELL
            request = QuoteRequest(
                fiat=fiat,
                asset=asset,
                direction=direction,
                amount=amount_text,
                provider=provider,
                payment_methods=(payment,) if payment and fiat == payment_fiat_code else (),
                allow_market_fallback=False,
                request_id=f"ui-{generation}-{slot}",
                generation=generation,
            )
            return C2CQuoteJob(slot, generation, source, target, request)
        except (TypeError, ValueError) as exc:
            return ExchangeEdgeResult(
                slot, generation, source, target, "c2c", None, "—",
                "C2C 金额无效", "invalid", (str(exc),), provider=provider,
            )

    def _market_result(
        self,
        slot: int,
        generation: int,
        amount: str,
        source: str,
        target: str,
        kinds: Mapping[str, str],
        *,
        from_cache: bool,
        degraded: bool = False,
        details: Sequence[str] = (),
        c2c_context: bool = False,
    ) -> ExchangeEdgeResult:
        try:
            exact = canonical_amount_string(
                self.rate_service.convert_exact(amount, source, target)
            )
            display = format_for_display(exact, target, kinds.get(target))
            if degraded:
                status = "⚠ 非 C2C 可成交价 · 普通行情降级"
                state = "degraded"
            elif c2c_context:
                status = "法币参考汇率 · 缓存" if from_cache else "法币参考汇率"
                state = "cache" if from_cache else "live"
            elif from_cache:
                status = "普通汇率 · 缓存"
                state = "cache"
            else:
                status = "普通汇率 · 实时"
                state = "live"
            return ExchangeEdgeResult(
                slot, generation, source, target, "market", exact, display,
                status, state, tuple(details), provider="ordinary_market",
            )
        except Exception:
            return ExchangeEdgeResult(
                slot, generation, source, target, "market", None, "—",
                "普通汇率暂不可用", "error", ("尚无所选币种的精确汇率。",),
            )

    def execute_job(
        self,
        job: C2CQuoteJob | C2CBridgeJob,
        *,
        cancel: object | None = None,
    ) -> object:
        if self.c2c_service is None:
            raise RuntimeError("C2C 报价服务未配置")
        if isinstance(job, C2CQuoteJob):
            return self.c2c_service.quote(job.request, cancel=cancel)
        first_quote = self.c2c_service.quote(job.sell_request, cancel=cancel)
        if _enum_text(_mapping_value(first_quote, "status")) != QuoteStatus.OK.value:
            return C2CBridgeExecution(first_quote)
        match = _mapping_value(first_quote, "match")
        fiat_amount = _mapping_value(match, "output_amount") if match is not None else None
        try:
            fiat_amount_text = canonical_amount_string(fiat_amount)
            if parse_amount(fiat_amount_text) <= 0:
                raise AmountInputError("C2C 第一段未返回有效法币金额")
            second_request = QuoteRequest(
                fiat=job.settlement_fiat,
                asset=job.target,
                direction=Direction.BUY,
                amount=fiat_amount_text,
                provider=job.provider,
                payment_methods=(job.payment_method,) if job.payment_method else (),
                allow_market_fallback=False,
                request_id=f"ui-{job.generation}-{job.slot}-buy",
                generation=job.generation,
            )
        except (TypeError, ValueError):
            return C2CBridgeExecution(first_quote)
        second_quote = self.c2c_service.quote(second_request, cancel=cancel)
        return C2CBridgeExecution(first_quote, second_request, second_quote)

    def finish_job(
        self,
        job: C2CQuoteJob | C2CBridgeJob,
        quote: object | None,
        *,
        kinds: Mapping[str, str],
        error: BaseException | None = None,
    ) -> ExchangeEdgeResult:
        if isinstance(job, C2CBridgeJob):
            return self._finish_bridge_job(job, quote, kinds=kinds, error=error)
        return self._finish_direct_job(job, quote, kinds=kinds, error=error)

    def _finish_direct_job(
        self,
        job: C2CQuoteJob,
        quote: object | None,
        *,
        kinds: Mapping[str, str],
        error: BaseException | None = None,
    ) -> ExchangeEdgeResult:
        if error is not None or quote is None:
            return ExchangeEdgeResult(
                job.slot, job.generation, job.source, job.target, "c2c", None, "—",
                "C2C 报价暂不可用", "error", ("平台未返回可用报价。",),
                provider=job.request.provider,
            )
        status = _enum_text(_mapping_value(quote, "status"))
        data_state = _enum_text(_mapping_value(quote, "data_state"))
        provider_raw = _mapping_value(quote, "provider")
        provider = str(provider_raw).lower() if provider_raw else job.request.provider
        warnings_raw = _mapping_value(quote, "warnings", ())
        warnings = tuple(
            str(item)[:500] for item in (
                (warnings_raw,) if isinstance(warnings_raw, str) else tuple(warnings_raw or ())
            ) if str(item)
        )
        market_best_raw = _mapping_value(quote, "market_best_price")
        market_best = str(market_best_raw) if market_best_raw is not None else None
        match = _mapping_value(quote, "match")
        matched_price_raw = _mapping_value(match, "price") if match is not None else None
        matched_price = str(matched_price_raw) if matched_price_raw is not None else None

        if status == QuoteStatus.OK.value and match is not None:
            raw_output = _mapping_value(match, "output_amount")
            try:
                exact = canonical_amount_string(raw_output)
                display = format_for_display(exact, job.target, kinds.get(job.target))
            except (TypeError, ValueError):
                exact, display = None, "—"
            state_label, state_kind = self._data_state_label(data_state)
            details = []
            if matched_price:
                details.append(f"本金额匹配价：{matched_price} {job.request.fiat}/{job.request.asset}")
            if market_best:
                details.append(f"最低展示价：{market_best} {job.request.fiat}/{job.request.asset}")
            details.extend(warnings[:2])
            return ExchangeEdgeResult(
                job.slot, job.generation, job.source, job.target, "c2c", exact, display,
                f"{self._provider_label(provider)} C2C · {state_label}", state_kind,
                tuple(details), matched_price, market_best, provider,
            )

        if status == QuoteStatus.MARKET_FALLBACK.value:
            return self._market_result(
                job.slot,
                job.generation,
                job.request.amount,
                job.source,
                job.target,
                kinds,
                from_cache=data_state in {DataState.FRESH_CACHE.value, DataState.STALE_CACHE.value},
                degraded=True,
                details=("普通行情仅供参考，不代表任何单广告可成交。", *warnings[:2]),
            )

        if status == QuoteStatus.NO_MATCH.value:
            details = ["已有广告，但本金额未命中单广告范围；未拿最低价冒充可成交价。"]
            if market_best:
                details.append(f"最低展示价：{market_best} {job.request.fiat}/{job.request.asset}")
            details.extend(warnings[:1])
            return ExchangeEdgeResult(
                job.slot, job.generation, job.source, job.target, "c2c", None, "—",
                "金额越界 · 无本金额匹配价", "range", tuple(details),
                market_best_price=market_best, provider=provider,
            )

        if provider == "okx" and status in {
            QuoteStatus.UNCONFIGURED.value,
            QuoteStatus.PERMISSION_DENIED.value,
        }:
            label = "OKX 官方 P2P API 未配置或无权限"
        elif status == QuoteStatus.UNCONFIGURED.value:
            label = f"{self._provider_label(provider)} C2C 未配置"
        elif status == QuoteStatus.PERMISSION_DENIED.value:
            label = f"{self._provider_label(provider)} C2C 无权限"
        elif status == QuoteStatus.RATE_LIMITED.value:
            label = "C2C 请求受限，请稍后重试"
        elif status == QuoteStatus.CIRCUIT_OPEN.value:
            label = "C2C 暂停请求，等待服务恢复"
        elif status == QuoteStatus.CANCELLED.value:
            label = "C2C 请求已取消"
        else:
            label = "C2C 报价暂不可用"
        return ExchangeEdgeResult(
            job.slot, job.generation, job.source, job.target, "c2c", None, "—",
            label, "unconfigured" if status in {
                QuoteStatus.UNCONFIGURED.value, QuoteStatus.PERMISSION_DENIED.value
            } else "error", warnings[:3], market_best_price=market_best, provider=provider,
        )

    def _finish_bridge_job(
        self,
        job: C2CBridgeJob,
        execution: object | None,
        *,
        kinds: Mapping[str, str],
        error: BaseException | None = None,
    ) -> ExchangeEdgeResult:
        if error is not None or not isinstance(execution, C2CBridgeExecution):
            return ExchangeEdgeResult(
                job.slot, job.generation, job.source, job.target, "c2c", None, "—",
                "C2C 双段报价暂不可用", "error",
                (f"路径：{job.source} → {job.settlement_fiat} → {job.target}",),
                provider=job.provider,
            )
        first_job = C2CQuoteJob(
            job.slot, job.generation, job.source, job.settlement_fiat, job.sell_request
        )
        first = self._finish_direct_job(first_job, execution.first_quote, kinds=kinds)
        if not first.valid or execution.second_request is None or execution.second_quote is None:
            return ExchangeEdgeResult(
                job.slot, job.generation, job.source, job.target, "c2c", None, "—",
                f"C2C 第一段失败 · {first.status}", first.state,
                (f"路径：{job.source} → {job.settlement_fiat} → {job.target}", *first.details[:2]),
                first.matched_price, first.market_best_price, first.provider,
            )
        second_job = C2CQuoteJob(
            job.slot, job.generation, job.settlement_fiat, job.target, execution.second_request
        )
        second = self._finish_direct_job(second_job, execution.second_quote, kinds=kinds)
        if not second.valid:
            return ExchangeEdgeResult(
                job.slot, job.generation, job.source, job.target, "c2c", None, "—",
                f"C2C 第二段失败 · {second.status}", second.state,
                (f"路径：{job.source} → {job.settlement_fiat} → {job.target}", *second.details[:2]),
                second.matched_price, second.market_best_price, second.provider,
            )
        provider_codes = tuple(dict.fromkeys(filter(None, (first.provider, second.provider))))
        providers = "→".join(self._provider_label(code) for code in provider_codes)
        provider_chain = "->".join(provider_codes)
        state = "live" if first.state == second.state == "live" else "cache"
        state_label = "实时" if state == "live" else "含缓存"
        details = (
            f"路径：{job.source} → {job.settlement_fiat} → {job.target}",
            f"卖出段：{first.matched_price or '价格未提供'} {job.settlement_fiat}/{job.source}",
            f"买入段：{second.matched_price or '价格未提供'} {job.settlement_fiat}/{job.target}",
        )
        return ExchangeEdgeResult(
            job.slot,
            job.generation,
            job.source,
            job.target,
            "c2c",
            second.exact_value,
            second.display_value,
            f"{providers or self._provider_label(job.provider)} C2C 双段 · {state_label}",
            state,
            details,
            second.matched_price,
            second.market_best_price,
            provider_chain or job.provider,
        )

    @staticmethod
    def _data_state_label(data_state: str) -> tuple[str, str]:
        mapping = {
            DataState.LIVE.value: ("实时", "live"),
            DataState.FRESH_CACHE.value: ("缓存（新鲜）", "cache"),
            DataState.STALE_CACHE.value: ("缓存（宽限）", "cache"),
            DataState.NEGATIVE_CACHE.value: ("失败缓存", "error"),
            DataState.MARKET_FALLBACK.value: ("普通行情降级", "degraded"),
        }
        return mapping.get(data_state, ("状态未知", "muted"))

    @staticmethod
    def _provider_label(provider: str | None) -> str:
        return PROVIDER_LABELS.get(str(provider or "").lower(), str(provider or "自动").upper())

    def quote_edges(
        self,
        state: ExchangePageState,
        kinds: Mapping[str, str],
        *,
        from_cache: bool = False,
    ) -> tuple[
        tuple[ExchangeEdgeResult, ...],
        tuple[C2CQuoteJob | C2CBridgeJob, ...],
    ]:
        immediate: list[ExchangeEdgeResult] = []
        jobs: list[C2CQuoteJob | C2CBridgeJob] = []
        for slot in state.quote_slots:
            prepared = self.prepare_edge(
                slot=slot,
                generation=state.generation,
                amount=state.amount,
                source=state.active_code,
                target=state.currencies[slot],
                kinds=kinds,
                mode=state.mode,
                provider=state.provider,
                payment_method=state.payment_method,
                payment_fiat=state.settlement_fiat,
                settlement_fiat=state.settlement_fiat,
                from_cache=from_cache,
            )
            if isinstance(prepared, (C2CQuoteJob, C2CBridgeJob)):
                jobs.append(prepared)
            else:
                immediate.append(prepared)
        return tuple(immediate), tuple(jobs)

    def payment_method_options(self, provider: str, fiat: str | None = None) -> tuple[tuple[str, str], ...]:
        """Return provider-supplied identifiers; never guess payment aliases."""

        if self.c2c_service is None:
            return ()
        for method_name in ("payment_methods", "list_trade_methods"):
            method = getattr(self.c2c_service, method_name, None)
            if not callable(method):
                continue
            try:
                raw_values = method(provider, fiat) if fiat is not None else method(provider)
            except (TypeError, ValueError, RuntimeError):
                continue
            options: list[tuple[str, str]] = []
            for item in tuple(raw_values or ()):
                identifier = _normalize_payment_method(_mapping_value(item, "identifier", ""))
                name = str(
                    _mapping_value(item, "name", _mapping_value(item, "display_name", identifier))
                    or identifier
                ).strip()[:80]
                if identifier and all(existing[0] != identifier for existing in options):
                    options.append((identifier, name))
            if options:
                return tuple(options)
        return ()


class _TkResultBridge:
    """Queue worker results and deliver them only from Tk's main thread."""

    def __init__(self, owner: tk.Misc, callback: Callable[..., None]) -> None:
        self.owner = owner
        self.callback = callback
        self.results: SimpleQueue[tuple[object, ...]] = SimpleQueue()
        self.pending = 0
        self.job: str | None = None
        self.closed = False

    def expect(self) -> None:
        if self.closed:
            return
        self.pending += 1
        self._ensure_poll()

    def deliver(self, *payload: object) -> None:
        if not self.closed:
            self.results.put(payload)

    def _ensure_poll(self) -> None:
        if self.closed or self.job is not None:
            return
        try:
            self.job = self.owner.after(25, self._poll)
        except tk.TclError:
            self.close()

    def _poll(self) -> None:
        self.job = None
        if self.closed:
            return
        while self.pending:
            try:
                payload = self.results.get_nowait()
            except Empty:
                break
            self.pending -= 1
            self.callback(*payload)
            if self.closed:
                return
        if self.pending:
            self._ensure_poll()

    def close(self) -> None:
        self.closed = True
        self.pending = 0
        if self.job is not None:
            try:
                self.owner.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None


class ExchangePage(tk.Frame):
    """Responsive Tk view for :class:`ExchangePageState`."""

    DEBOUNCE_MS = 650

    def __init__(
        self,
        master: tk.Misc,
        coordinator: ExchangeCoordinator,
        state: ExchangePageState,
        refresh_callback: Callable[[], None],
        save_callback: Callable[[dict[str, object]], None],
        timestamp_formatter: Callable[[str], str],
        *,
        colors: Mapping[str, str],
        font_name: str = "Microsoft YaHei UI",
        currency_selector_factory: Callable[..., tk.Widget] | None = None,
        fixed_mode: str | None = None,
        page_title: str | None = None,
    ) -> None:
        super().__init__(master, bg=colors["bg"])
        if fixed_mode is not None:
            normalized_mode = str(fixed_mode).strip().lower()
            if normalized_mode not in SUPPORTED_MODES:
                raise ValueError("固定兑换模式无效")
            state.mode = normalized_mode
        self.coordinator = coordinator
        self.state = state
        self.fixed_mode = fixed_mode
        self.page_title = page_title or ("C2C 兑换" if state.mode == "c2c" else "市场兑换")
        self.refresh_callback = refresh_callback
        self.save_callback = save_callback
        self.timestamp_formatter = timestamp_formatter
        self.colors = colors
        self.font_name = font_name
        self.currency_selector_factory = currency_selector_factory
        self.snapshot: object | None = None
        self.from_cache = False
        self.visible = False
        self.closed = False
        self.recalculate_job: str | None = None
        self._setting_amount = False
        self.currency_values: list[str] = list(state.currencies)
        self.code_to_display: dict[str, str] = {code: code for code in state.currencies}
        self.display_to_code: dict[str, str] = {code: code for code in state.currencies}
        self.amount_vars = [tk.StringVar(value="") for _slot in range(7)]
        self.amount_vars[int(state.active_slot)].set(state.amount)
        self.amount_var = self.amount_vars[int(state.active_slot)]
        self.amount_entries: dict[int, tk.Entry] = {}
        self.mode_var = tk.StringVar(value="C2C 按金额" if state.mode == "c2c" else "普通汇率")
        self.provider_var = tk.StringVar(value=PROVIDER_LABELS[state.provider])
        self.settlement_var = tk.StringVar(value=state.settlement_fiat)
        self.payment_var = tk.StringVar(value=PAYMENT_ALL_LABEL)
        self.quote_stamp_var = tk.StringVar(value="报价时间：等待汇率")
        self.status_var = tk.StringVar(
            value=(
                "法币与虚拟币按实际输入金额查询 C2C；虚拟币之间经所选法币执行双段参考。"
                if state.mode == "c2c" else
                "使用公开市场与法币参考汇率同步换算；仅供参考，不执行购买或交易。"
            )
        )
        self.card_frame: tk.Frame | None = None
        self.card_widgets: dict[int, tk.Frame] = {}
        self.currency_selectors: dict[int, tk.Widget] = {}
        self.primary_entry: tk.Entry | None = None
        self.settlement_values: tuple[str, ...] = (state.settlement_fiat,)
        self.payment_by_label: dict[str, str] = {PAYMENT_ALL_LABEL: ""}
        self.result_bridge = _TkResultBridge(self, self._finish_quote)
        self.payment_bridge = _TkResultBridge(self, self._finish_payment_options)
        self.payment_cache: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
        self.payment_request: tuple[str, str, int] | None = None
        self.payment_generation = 0
        self.payment_supported_fiat = ""
        self.quote_cancel = threading.Event()
        self._build()
        self.amount_traces = {
            slot: variable.trace_add(
                "write", lambda *_args, selected=slot: self._amount_changed(selected)
            )
            for slot, variable in enumerate(self.amount_vars)
        }
        self.bind("<Destroy>", self._destroyed, add="+")

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        accent: bool = False,
        size: int = 9,
    ) -> tk.Button:
        bg = self.colors["accent"] if accent else self.colors["card_alt"]
        fg = self.colors["on_accent"] if accent else self.colors["text"]
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=self.colors["accent_hover"] if accent else self.colors["key_hover"],
            activeforeground=fg,
            disabledforeground=self.colors["subtle"],
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.colors["accent"] if not accent else bg,
            font=(self.font_name, size, "bold"),
            cursor="hand2",
            takefocus=True,
        )

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = tk.Frame(self, bg=self.colors["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(20, 10))
        header.grid_columnconfigure(0, weight=1)
        tk.Label(
            header, text=self.page_title, bg=self.colors["bg"], fg=self.colors["text"],
            font=(self.font_name, 23, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header, textvariable=self.status_var, bg=self.colors["bg"], fg=self.colors["muted"],
            font=(self.font_name, 9), wraplength=720, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))
        self._button(header, "↻  刷新", self.refresh_callback, accent=True, size=9).grid(
            row=0, column=1, rowspan=2, padx=(10, 0), ipadx=10, ipady=7,
        )

        controls = tk.Frame(header, bg=self.colors["card"])
        controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.mode_combo: ttk.Combobox | None = None
        self.provider_combo: ttk.Combobox | None = None
        self.settlement_combo: ttk.Combobox | None = None
        self.payment_combo: ttk.Combobox | None = None
        if self.state.mode == "c2c":
            for column in range(8):
                controls.grid_columnconfigure(column, weight=1 if column in {1, 3, 5} else 0)
            tk.Label(controls, text="平台", bg=self.colors["card"], fg=self.colors["muted"], font=(self.font_name, 8, "bold")).grid(row=0, column=0, padx=(12, 6), pady=9)
            self.provider_combo = ttk.Combobox(controls, textvariable=self.provider_var, values=tuple(PROVIDER_LABELS.values()), state="readonly", width=11, takefocus=True)
            self.provider_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=7)
            self.provider_combo.bind("<<ComboboxSelected>>", self._provider_changed)
            tk.Label(controls, text="跨币结算法币", bg=self.colors["card"], fg=self.colors["muted"], font=(self.font_name, 8, "bold")).grid(row=0, column=2, padx=(0, 6))
            self.settlement_combo = ttk.Combobox(controls, textvariable=self.settlement_var, values=self.settlement_values, state="readonly", width=10, takefocus=True)
            self.settlement_combo.grid(row=0, column=3, sticky="ew", padx=(0, 10), pady=7)
            self.settlement_combo.bind("<<ComboboxSelected>>", self._settlement_changed)
            tk.Label(controls, text="结算支付", bg=self.colors["card"], fg=self.colors["muted"], font=(self.font_name, 8, "bold")).grid(row=0, column=4, padx=(0, 6))
            self.payment_combo = ttk.Combobox(controls, textvariable=self.payment_var, values=(PAYMENT_UNKNOWN_LABEL,), state="readonly", width=25, takefocus=True)
            self.payment_combo.grid(row=0, column=5, sticky="ew", padx=(0, 10), pady=7)
            self.payment_combo.bind("<<ComboboxSelected>>", self._payment_changed)
            stamp_column = 6
        else:
            controls.grid_columnconfigure(0, weight=1)
            tk.Label(
                controls,
                text="非 C2C · 公开现货/市场行情与法币参考汇率，不代表任何平台可直接成交",
                bg=self.colors["card"], fg=self.colors["muted"],
                font=(self.font_name, 8, "bold"), padx=12, pady=9, anchor="w",
            ).grid(row=0, column=0, sticky="ew")
            stamp_column = 1
        tk.Label(
            controls, textvariable=self.quote_stamp_var, bg=self.colors["accent_dark"], fg=self.colors["accent"],
            font=(self.font_name, 8, "bold"), padx=10, pady=7,
        ).grid(row=0, column=stamp_column, columnspan=2, padx=(0, 10), pady=6)

        holder = tk.Frame(self, bg=self.colors["bg"])
        holder.grid(row=1, column=0, sticky="nsew", padx=(28, 18), pady=(0, 20))
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(holder, bg=self.colors["bg"], bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(7, 0))
        self.card_frame = tk.Frame(self.canvas, bg=self.colors["bg"])
        self.card_window = self.canvas.create_window((0, 0), window=self.card_frame, anchor="nw")
        self.card_frame.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.card_window, width=e.width))
        self.canvas.bind("<MouseWheel>", self._mousewheel, add="+")
        self._render_cards()
        self._update_control_states()

    def _mousewheel(self, event: tk.Event) -> str:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _render_cards(self) -> None:
        if self.card_frame is None or self.closed:
            return
        focus_slot: int | None = None
        cursor = tk.END
        focused = self.focus_get()
        for slot, entry in self.amount_entries.items():
            if focused is entry:
                focus_slot = slot
                try:
                    cursor = entry.index(tk.INSERT)
                except tk.TclError:
                    cursor = tk.END
                break
        for child in self.card_frame.winfo_children():
            child.destroy()
        self.card_widgets.clear()
        self.currency_selectors.clear()
        self.amount_entries.clear()
        self.primary_entry = None
        for column in range(3):
            self.card_frame.grid_columnconfigure(column, weight=1, uniform="exchange_cards")
        primary = self._make_card(self.card_frame, self.state.primary_slot, primary=True)
        self.card_widgets[self.state.primary_slot] = primary
        primary.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=7, pady=(0, 9))
        for index, slot in enumerate(self.state.target_slots):
            card = self._make_card(self.card_frame, slot, primary=False)
            self.card_widgets[slot] = card
            card.grid(row=1 + index // 3, column=index % 3, sticky="nsew", padx=7, pady=7)
        if focus_slot is not None and focus_slot in self.amount_entries:
            try:
                entry = self.amount_entries[focus_slot]
                entry.focus_set()
                entry.icursor(cursor)
            except tk.TclError:
                pass

    def _refresh_target_cards(self, slots: Sequence[int] | None = None) -> None:
        if self.card_frame is None or self.closed:
            return
        selected = tuple(slots) if slots is not None else self.state.quote_slots
        for slot in selected:
            if not 0 <= int(slot) < 7:
                continue
            if slot == self.state.primary_slot:
                self._render_cards()
                return
            old = self.card_widgets.get(slot)
            if old is not None:
                old.destroy()
            index = self.state.target_slots.index(slot)
            card = self._make_card(self.card_frame, slot, primary=False)
            card.grid(row=1 + index // 3, column=index % 3, sticky="nsew", padx=7, pady=7)
            self.card_widgets[slot] = card

    def _make_card(self, parent: tk.Misc, slot: int, *, primary: bool) -> tk.Frame:
        active = slot == self.state.active_slot
        card = tk.Frame(
            parent,
            bg=self.colors["card"],
            highlightthickness=2 if primary or active else 1,
            highlightbackground=self.colors["accent"] if primary or active else self.colors["line"],
        )
        card.grid_columnconfigure(0, weight=1)
        top = tk.Frame(card, bg=self.colors["card"])
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        top.grid_columnconfigure(1, weight=1)
        tk.Label(
            top,
            text=("主货币" if primary else f"货币 {self.state.target_slots.index(slot) + 1}") + (" · 当前输入" if active else ""),
            bg=self.colors["accent"] if primary or active else self.colors["card_alt"],
            fg=self.colors["on_accent"] if primary or active else self.colors["muted"],
            font=(self.font_name, 8, "bold"), padx=8, pady=4,
        ).grid(row=0, column=0, sticky="w")
        currency_var = tk.StringVar(value=self.code_to_display.get(self.state.currencies[slot], self.state.currencies[slot]))
        currency_displays = [self.code_to_display.get(code, code) for code in self.currency_values]
        if self.currency_selector_factory is not None:
            selector = self.currency_selector_factory(
                top,
                currency_var,
                values=currency_displays,
                command=lambda display, selected=slot: self._currency_changed(selected, display),
                width=36 if primary else 23,
                font_size=10 if primary else 9,
                max_rows=9,
            )
            selector.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        else:
            selector = ttk.Combobox(
                top, textvariable=currency_var, values=tuple(currency_displays),
                state="readonly", width=36 if primary else 23, takefocus=True,
            )
            selector.grid(row=0, column=1, sticky="ew", padx=(8, 0))
            selector.bind(
                "<<ComboboxSelected>>",
                lambda _e, selected=slot, variable=currency_var: self._currency_changed(selected, variable.get()),
            )
        self.currency_selectors[slot] = selector
        entry = tk.Entry(
            card, textvariable=self.amount_vars[slot], bg=self.colors["card_alt"], fg=self.colors["text"],
            insertbackground=self.colors["accent"], selectbackground=self.colors["selection"],
            selectforeground=self.colors["selection_text"],
            font=("Segoe UI", 20 if primary else 17, "bold"),
            bd=0, highlightthickness=1,
            highlightbackground=self.colors["accent"] if active else self.colors["line"],
            highlightcolor=self.colors["accent"], takefocus=True, justify="left",
        )
        entry.grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 6), ipady=8 if primary else 6)
        entry.bind("<FocusIn>", lambda _event, selected=slot: self._focus_amount(selected))
        entry.bind("<KeyPress>", lambda event, selected=slot: self._amount_keypress(event, selected))
        entry.bind("<Return>", lambda event, selected=slot: self._commit_amount_expression(event, selected))
        entry.bind("<KP_Enter>", lambda event, selected=slot: self._commit_amount_expression(event, selected))
        entry.bind("<FocusOut>", lambda event, selected=slot: self._commit_amount_expression(event, selected))
        self.amount_entries[slot] = entry
        if primary:
            self.primary_entry = entry

        result = None if active else self.state.current_result(slot)
        status = "当前输入 · 同步换算其他六项" if active else result.status if result is not None else "等待换算"
        details = (
            "支持 +  −  ×  ÷  % 和括号；按 Enter 或点击别处计算。"
            if active else "\n".join(result.details[:3]) if result is not None else ""
        )
        state_kind = "live" if active else result.state if result is not None else "muted"
        status_color = {
            "live": self.colors["up"],
            "cache": self.colors["accent"],
            "degraded": self.colors["down"],
            "range": self.colors["down"],
            "unconfigured": self.colors["down"],
            "error": self.colors["down"],
            "loading": self.colors["accent"],
        }.get(state_kind, self.colors["muted"])
        tk.Label(
            card, text=status, bg=self.colors["card"], fg=status_color,
            font=(self.font_name, 8, "bold"), anchor="w", justify="left", wraplength=275,
        ).grid(row=2, column=0, sticky="ew", padx=14)
        tk.Label(
            card,
            text=details or (
                "最低展示价与本金额匹配价分开展示；不保证成交。"
                if self.state.mode == "c2c" else
                "公开行情仅供换算参考，不代表可直接成交。"
            ),
            bg=self.colors["card"], fg=self.colors["muted"], font=(self.font_name, 7),
            anchor="w", justify="left", wraplength=275,
        ).grid(row=3, column=0, sticky="ew", padx=14, pady=(4, 7))
        if primary:
            return card
        actions = tk.Frame(card, bg=self.colors["card"])
        actions.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 12))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)
        set_button = self._button(actions, "设为主币", lambda selected=slot: self._set_primary(selected), size=8)
        set_button.grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=4)
        swap_button = self._button(actions, "设为主币并换算", lambda selected=slot: self._swap_primary(selected), size=8)
        swap_button.grid(row=0, column=1, sticky="ew", padx=(4, 0), ipady=4)
        if not self.state.can_swap(slot):
            swap_button.configure(state="disabled", cursor="arrow")
        return card

    def apply_snapshot(self, snapshot: object, from_cache: bool = False, animated: bool = False) -> None:
        del animated
        if self.closed:
            return
        self.snapshot = snapshot
        self.from_cache = bool(from_cache)
        rates = _mapping_value(snapshot, "rates", {})
        kinds = _mapping_value(snapshot, "kinds", {})
        names = _mapping_value(snapshot, "names", {})
        if isinstance(rates, Mapping) and isinstance(kinds, Mapping):
            priority = {code: index for index, code in enumerate(DEFAULT_EXCHANGE_CURRENCIES)}
            available = sorted(
                (str(code).upper() for code in rates if str(kinds.get(code, "")) in {"fiat", "crypto"}),
                key=lambda code: (priority.get(code, 999), str(kinds.get(code, "")), code),
            )
            self.currency_values = list(dict.fromkeys([*self.state.currencies, *available]))
            self.code_to_display = {}
            self.display_to_code = {}
            for code in self.currency_values:
                kind = str(kinds.get(code, ""))
                prefix = "₿ " if kind == "crypto" else ""
                display = f"{prefix}{code}  ·  {str(names.get(code, code))[:80]}"
                self.code_to_display[code] = display
                self.display_to_code[display] = code
            fiats = tuple(
                code for code in available if str(kinds.get(code, "")).lower() == "fiat"
            )
            if fiats:
                self.settlement_values = fiats
                if self.state.settlement_fiat not in fiats:
                    preferred = self.state.primary_code if self.state.primary_code in fiats else fiats[0]
                    self.state.set_settlement_fiat(preferred)
                self.settlement_var.set(self.state.settlement_fiat)
                if self.settlement_combo is not None:
                    self.settlement_combo.configure(values=fiats)
        fetched_at = str(_mapping_value(snapshot, "fetched_at", "") or "")
        if fetched_at:
            cache_label = "（缓存）" if from_cache else ""
            self.quote_stamp_var.set(f"报价时间：{self.timestamp_formatter(fetched_at)}{cache_label}")
        self.state.invalidate()
        self._clear_result_amounts()
        self.payment_generation += 1
        self._refresh_payment_options()
        self._render_cards()
        if self.visible:
            self.recalculate_now()

    def begin_refresh(self) -> None:
        self.status_var.set(
            "正在刷新普通参考汇率与 C2C 依赖数据；旧一轮结果不会覆盖新输入。"
            if self.state.mode == "c2c" else
            "正在刷新公开市场与法币参考汇率；旧一轮结果不会覆盖新输入。"
        )

    def finish_refresh_failure(self) -> None:
        self.status_var.set("刷新失败；若有可信缓存仍会标明缓存状态。")

    def on_show(self) -> None:
        if self.closed:
            return
        self.visible = True
        self._refresh_payment_options()
        self.recalculate_now()

    def on_hide(self) -> None:
        if self.closed:
            return
        self.visible = False
        self.quote_cancel.set()
        self.state.invalidate()
        if self.recalculate_job is not None:
            try:
                self.after_cancel(self.recalculate_job)
            except tk.TclError:
                pass
            self.recalculate_job = None
        self.flush_state()

    def _amount_changed(self, slot: int) -> None:
        if self._setting_amount or self.closed:
            return
        self.amount_var = self.amount_vars[slot]
        self.state.set_input(slot, self.amount_vars[slot].get())
        self._clear_result_amounts()
        self._save_state()
        self._schedule_recalculate()

    def _focus_amount(self, slot: int) -> None:
        if self.closed:
            return
        entry = self.amount_entries.get(slot)
        if entry is not None and slot != self.state.active_slot:
            try:
                entry.selection_range(0, tk.END)
            except tk.TclError:
                pass

    def _amount_keypress(self, event: tk.Event, slot: int | None = None) -> str | None:
        selected = int(self.state.active_slot) if slot is None else int(slot)
        self.amount_var = self.amount_vars[selected]
        if event.char in {"=", "＝"}:
            self.after_idle(lambda: self._commit_amount_expression(slot=selected))
            return "break"
        return None

    def _evaluate_amount_expression(self, *, commit: bool, slot: int | None = None) -> str | None:
        selected = int(self.state.active_slot) if slot is None else int(slot)
        variable = self.amount_vars[selected]
        raw = variable.get().strip()
        if not raw:
            self.state.set_input(selected, "")
            self._save_state()
            self.status_var.set("请在任意一个货币金额框输入金额或算式。")
            self._refresh_target_cards()
            return None
        try:
            amount = evaluate_basic_amount_decimal(raw)
        except CalculationError as exc:
            self.state.invalidate()
            self.status_var.set(f"金额算式有误：{exc}")
            self._refresh_target_cards()
            return None
        self.state.set_input(selected, amount)
        self.amount_var = variable
        if commit and raw != amount:
            self._setting_amount = True
            try:
                variable.set(amount)
            finally:
                self._setting_amount = False
        self._save_state()
        return amount

    def _commit_amount_expression(
        self,
        _event: tk.Event | None = None,
        slot: int | None = None,
    ) -> str:
        if self.closed:
            return "break"
        if self._evaluate_amount_expression(commit=True, slot=slot) is not None:
            self.recalculate_now()
        return "break"

    def _schedule_recalculate(self) -> None:
        if self.recalculate_job is not None:
            try:
                self.after_cancel(self.recalculate_job)
            except tk.TclError:
                pass
        try:
            self.recalculate_job = self.after(self.DEBOUNCE_MS, self.recalculate_now)
        except tk.TclError:
            self.recalculate_job = None

    def recalculate_now(self) -> str:
        if self.recalculate_job is not None:
            try:
                self.after_cancel(self.recalculate_job)
            except tk.TclError:
                pass
        self.recalculate_job = None
        if self.closed:
            return "break"
        amount = self._evaluate_amount_expression(commit=False)
        if amount is None:
            return "break"
        if self.snapshot is None:
            self.status_var.set(f"算式结果：{amount}；等待汇率数据。")
            return "break"
        kinds = _mapping_value(self.snapshot, "kinds", {})
        if not isinstance(kinds, Mapping):
            return "break"
        self.state.invalidate()
        self._clear_result_amounts()
        self.quote_cancel.set()
        self.quote_cancel = threading.Event()
        immediate, jobs = self.coordinator.quote_edges(self.state, kinds, from_cache=self.from_cache)
        for result in immediate:
            self.state.accept_result(result)
            self._set_result_amount(result)
        if jobs:
            for job in jobs:
                job_provider = (
                    job.request.provider if isinstance(job, C2CQuoteJob) else job.provider
                )
                loading_text = (
                    "正在获取 C2C 双段本金额匹配价"
                    if isinstance(job, C2CBridgeJob) else
                    "正在获取 C2C 本金额匹配价"
                )
                loading = ExchangeEdgeResult(
                    job.slot, job.generation, job.source, job.target, "c2c", None, "…",
                    loading_text, "loading",
                    ("最低展示价与本金额匹配价不会混用。",), provider=job_provider,
                )
                self.state.accept_result(loading)
                self._set_result_amount(loading)
            if self.visible:
                for job in jobs:
                    self._start_quote(job)
            else:
                for job in jobs:
                    job_provider = (
                        job.request.provider if isinstance(job, C2CQuoteJob) else job.provider
                    )
                    waiting = ExchangeEdgeResult(
                        job.slot, job.generation, job.source, job.target, "c2c", None, "—",
                        "页面打开后获取 C2C 报价", "muted",
                        ("页面不可见时不持续刷新 C2C。",), provider=job_provider,
                    )
                    self.state.accept_result(waiting)
                    self._set_result_amount(waiting)
        self.status_var.set(
            "C2C 页面不使用普通虚拟币行情降级；法币↔虚拟币查单广告，虚拟币↔虚拟币经结算法币双段查询。"
            if self.state.mode == "c2c" else
            f"已按 {self.state.active_code} 输入同步换算其他六项；公开市场行情仅供参考。"
        )
        self._refresh_target_cards()
        return "break"

    def _set_result_amount(self, result: ExchangeEdgeResult) -> None:
        if not 0 <= result.slot < len(self.amount_vars) or result.slot == self.state.active_slot:
            return
        self._setting_amount = True
        try:
            self.amount_vars[result.slot].set(result.display_value)
        finally:
            self._setting_amount = False

    def _clear_result_amounts(self) -> None:
        self._setting_amount = True
        try:
            for slot in self.state.quote_slots:
                self.amount_vars[slot].set("—")
        finally:
            self._setting_amount = False

    def _start_quote(self, job: C2CQuoteJob | C2CBridgeJob) -> None:
        self.result_bridge.expect()
        cancel = self.quote_cancel

        def worker() -> None:
            try:
                quote = self.coordinator.execute_job(job, cancel=cancel)
                self.result_bridge.deliver(job, quote, None)
            except Exception as exc:
                self.result_bridge.deliver(job, None, exc)

        threading.Thread(target=worker, daemon=True, name=f"exchange-c2c-{job.slot}").start()

    def _finish_quote(self, job: object, quote: object, error: object) -> None:
        if self.closed or not isinstance(job, (C2CQuoteJob, C2CBridgeJob)) or self.snapshot is None:
            return
        kinds = _mapping_value(self.snapshot, "kinds", {})
        if not isinstance(kinds, Mapping):
            return
        result = self.coordinator.finish_job(
            job, quote, kinds=kinds,
            error=error if isinstance(error, BaseException) else None,
        )
        if not self.visible or not self.state.accept_result(result):
            return
        self._set_result_amount(result)
        self.quote_stamp_var.set(
            f"报价时间：{self.timestamp_formatter(datetime.now().astimezone().isoformat())}"
        )
        self._refresh_target_cards((result.slot,))

    def _currency_changed(self, slot: int, display: str) -> None:
        code = self.display_to_code.get(display, display.split("·", 1)[0].replace("₿", "").strip())
        try:
            changed = self.state.select_currency(slot, code)
        except (TypeError, ValueError, IndexError):
            changed = False
        if changed:
            # Payment identifiers are provider/fiat specific.  A currency edit
            # can change the applicable fiat before the asynchronous options
            # request completes, so never carry the old filter into a quote.
            if self.state.payment_method:
                self.state.set_payment_method("")
            self.payment_generation += 1
            self._clear_result_amounts()
            self._refresh_payment_options()
            self._save_state()
            # SearchSelect invokes its command before it closes and restores
            # focus.  Rebuilding the cards inside that callback would destroy
            # the active selector mid-event, so finish the UI refresh at idle.
            try:
                self.after_idle(self._finish_currency_change)
            except tk.TclError:
                return

    def _finish_currency_change(self) -> None:
        if self.closed:
            return
        self._render_cards()
        self._schedule_recalculate()

    def _set_primary(self, slot: int) -> None:
        change = self.state.set_primary(slot)
        self._clear_result_amounts()
        self._save_state()
        self._render_cards()
        self._schedule_recalculate()
        if change.amount_cleared and self.primary_entry is not None:
            try:
                self.primary_entry.focus_set()
            except tk.TclError:
                pass

    def _swap_primary(self, slot: int) -> None:
        if not self.state.swap_with_primary(slot):
            return
        self.payment_generation += 1
        self._refresh_payment_options()
        self.amount_var = self.amount_vars[slot]
        self._setting_amount = True
        try:
            self.amount_var.set(self.state.amount)
        finally:
            self._setting_amount = False
        self._clear_result_amounts()
        self._save_state()
        self._render_cards()
        self._schedule_recalculate()
        if self.primary_entry is not None:
            try:
                self.primary_entry.focus_set()
            except tk.TclError:
                pass

    def _mode_changed(self, _event: tk.Event | None = None) -> None:
        if self.fixed_mode is not None:
            return
        mode = "c2c" if self.mode_var.get() == "C2C 按金额" else "market"
        if self.state.set_mode(mode):
            self.payment_generation += 1
            self._refresh_payment_options()
            self._save_state()
            self._update_control_states()
            self._schedule_recalculate()

    def _provider_changed(self, _event: tk.Event | None = None) -> None:
        provider = PROVIDER_BY_LABEL.get(self.provider_var.get(), "auto")
        if provider != self.state.provider and self.state.payment_method:
            self.state.set_payment_method("")
        if self.state.set_provider(provider):
            self.payment_generation += 1
            self._clear_result_amounts()
            self._refresh_payment_options()
            self._save_state()
            self._render_cards()
            self._schedule_recalculate()

    def _settlement_changed(self, _event: tk.Event | None = None) -> None:
        fiat = self.settlement_var.get().strip().upper()
        try:
            changed = self.state.set_settlement_fiat(fiat)
        except (TypeError, ValueError):
            self.settlement_var.set(self.state.settlement_fiat)
            return
        if changed:
            self.payment_generation += 1
            self._clear_result_amounts()
            self._refresh_payment_options()
            self._save_state()
            self._render_cards()
            self._schedule_recalculate()

    def _payment_changed(self, _event: tk.Event | None = None) -> None:
        payment = self.payment_by_label.get(self.payment_var.get(), "")
        if self.state.set_payment_method(payment):
            self._clear_result_amounts()
            self._save_state()
            self._render_cards()
            self._schedule_recalculate()

    def _refresh_payment_options(self) -> None:
        if self.state.mode != "c2c" or self.payment_combo is None:
            self.payment_supported_fiat = ""
            self._update_control_states()
            return
        fiat = None
        if self.snapshot is not None:
            kinds = _mapping_value(self.snapshot, "kinds", {})
            if isinstance(kinds, Mapping):
                if kinds.get(self.state.settlement_fiat) == "fiat":
                    fiat = self.state.settlement_fiat
        self.payment_supported_fiat = str(fiat or "")
        if not fiat and self.state.payment_method:
            self.state.set_payment_method("")
            self._save_state()
        key = (self.state.provider, str(fiat or ""))
        options = self.payment_cache.get(key, ())
        if options:
            self.payment_by_label = {PAYMENT_ALL_LABEL: ""}
            for identifier, name in options:
                self.payment_by_label[f"{name} · {identifier}"] = identifier
            values = tuple(self.payment_by_label)
            selected = next(
                (label for label, identifier in self.payment_by_label.items() if identifier == self.state.payment_method),
                PAYMENT_ALL_LABEL,
            )
        else:
            self.payment_by_label = {PAYMENT_UNKNOWN_LABEL: ""}
            values = (PAYMENT_UNKNOWN_LABEL,)
            selected = PAYMENT_UNKNOWN_LABEL
        self.payment_combo.configure(values=values)
        self.payment_var.set(selected)
        self._update_control_states()
        request = (key[0], key[1], self.payment_generation)
        if (
            self.visible
            and self.state.mode == "c2c"
            and key[1]
            and request != self.payment_request
            and key not in self.payment_cache
        ):
            self.payment_request = request
            self.payment_bridge.expect()

            def worker() -> None:
                loaded = self.coordinator.payment_method_options(key[0], key[1])
                self.payment_bridge.deliver(key[0], key[1], request[2], loaded)

            threading.Thread(target=worker, daemon=True, name="exchange-payment-methods").start()

    def _finish_payment_options(
        self,
        provider: object,
        fiat: object,
        generation: object,
        options: object,
    ) -> None:
        request = (str(provider), str(fiat), int(generation))
        if self.closed:
            return
        if self.payment_request == request:
            self.payment_request = None
        if (
            request[0] != self.state.provider
            or request[2] != self.payment_generation
            or not isinstance(options, (tuple, list))
        ):
            return
        cleaned: list[tuple[str, str]] = []
        for item in options:
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            identifier = _normalize_payment_method(item[0])
            name = str(item[1] or identifier).strip()[:80]
            if identifier and all(existing[0] != identifier for existing in cleaned):
                cleaned.append((identifier, name))
        self.payment_cache[(request[0], request[1])] = tuple(cleaned)
        if self.state.payment_method and self.state.payment_method not in {item[0] for item in cleaned}:
            self.state.set_payment_method("")
            self._save_state()
            self._schedule_recalculate()
        self._refresh_payment_options()

    def _update_control_states(self) -> None:
        enabled = self.state.mode == "c2c"
        if self.provider_combo is not None:
            self.provider_combo.configure(state="readonly" if enabled else "disabled")
        if self.settlement_combo is not None:
            self.settlement_combo.configure(state="readonly" if enabled else "disabled")
        if self.payment_combo is not None:
            self.payment_combo.configure(
                state="readonly" if enabled and self.payment_supported_fiat else "disabled"
            )

    def _save_state(self) -> None:
        self.save_callback(self.state.to_dict())

    def flush_state(self) -> None:
        if not self.closed:
            self._save_state()

    def _destroyed(self, event: tk.Event) -> None:
        if event.widget is not self or self.closed:
            return
        self.closed = True
        self.quote_cancel.set()
        for slot, trace in tuple(self.amount_traces.items()):
            try:
                self.amount_vars[slot].trace_remove("write", trace)
            except (tk.TclError, ValueError):
                pass
        if self.recalculate_job is not None:
            try:
                self.after_cancel(self.recalculate_job)
            except tk.TclError:
                pass
            self.recalculate_job = None
        self.result_bridge.close()
        self.payment_bridge.close()


__all__ = [
    "C2CBridgeExecution",
    "C2CBridgeJob",
    "C2CQuoteJob",
    "DEFAULT_EXCHANGE_CURRENCIES",
    "ExchangeCoordinator",
    "ExchangeEdgeResult",
    "ExchangePage",
    "ExchangePageState",
    "PAYMENT_ALL_LABEL",
    "PAYMENT_UNKNOWN_LABEL",
    "PROVIDER_BY_LABEL",
    "PROVIDER_LABELS",
    "PrimaryChange",
]
