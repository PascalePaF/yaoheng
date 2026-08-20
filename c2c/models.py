"""Public, serialization-safe models for C2C amount quotes.

Monetary values cross this package boundary only as canonical plain decimal
strings.  Internal arithmetic converts those strings back to ``Decimal``
under a local context; no price, amount, inventory, or limit is represented by
``float``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any

from conversion_core import (
    DecimalBoundaryError,
    canonical_amount_string,
    canonical_rate_string,
    currency_metadata,
    decimal_to_canonical,
    normalize_currency_code,
    parse_amount,
)


_PROVIDER_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_PAYMENT_RE = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_AD_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_REQUEST_ID_RE = re.compile(r"[^\x00-\x1f\x7f]{0,128}\Z")


class C2CModelError(ValueError):
    """A stable validation error for public C2C models."""

    code = "invalid_c2c_model"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or type(self).code


class Direction(str, Enum):
    """Trade direction from the user's perspective."""

    BUY = "BUY"  # Fiat source -> crypto target; the user buys crypto.
    SELL = "SELL"  # Crypto source -> fiat target; the user sells crypto.


class QuoteStatus(str, Enum):
    OK = "ok"
    NO_MATCH = "no_match"
    UNCONFIGURED = "unconfigured"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED = "cancelled"
    MARKET_FALLBACK = "market_fallback"


class DataState(str, Enum):
    LIVE = "live"
    FRESH_CACHE = "fresh_cache"
    STALE_CACHE = "stale_cache"
    NEGATIVE_CACHE = "negative_cache"
    MARKET_FALLBACK = "market_fallback"
    NONE = "none"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return decimal_to_canonical(value)
    if isinstance(value, SerializableModel):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


class SerializableModel:
    """Mixin exposing an explicit JSON-safe boundary for every public model."""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            field.name: _json_safe(getattr(self, field.name))
            for field in fields(self)
            if not field.name.startswith("_")
        }

    as_dict = to_dict


def _provider_name(value: object) -> str:
    if not isinstance(value, str):
        raise C2CModelError("C2C 平台标识无效", code="invalid_provider")
    name = value.strip().lower()
    if _PROVIDER_RE.fullmatch(name) is None:
        raise C2CModelError("C2C 平台标识无效", code="invalid_provider")
    return name


def _positive_amount(value: object, label: str) -> str:
    if isinstance(value, (bool, float)):
        raise C2CModelError(f"{label}必须使用十进制字符串或 Decimal", code="invalid_decimal")
    try:
        text = canonical_amount_string(value)
        parsed = parse_amount(text)
    except (DecimalBoundaryError, ValueError) as exc:
        raise C2CModelError(f"{label}无效", code="invalid_decimal") from exc
    if parsed <= 0:
        raise C2CModelError(f"{label}必须大于零", code="invalid_decimal")
    return text


def _nonnegative_amount(value: object, label: str) -> str:
    if isinstance(value, (bool, float)):
        raise C2CModelError(f"{label}必须使用十进制字符串或 Decimal", code="invalid_decimal")
    try:
        text = canonical_amount_string(value)
        parsed = parse_amount(text)
    except (DecimalBoundaryError, ValueError) as exc:
        raise C2CModelError(f"{label}无效", code="invalid_decimal") from exc
    if parsed < 0:
        raise C2CModelError(f"{label}不得为负数", code="invalid_decimal")
    return text


def _normalize_payments(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values = (value,) if isinstance(value, str) else tuple(value)  # type: ignore[arg-type]
    normalized: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise C2CModelError("支付方式标识无效", code="invalid_payment_method")
        identifier = raw.strip()
        if _PAYMENT_RE.fullmatch(identifier) is None:
            raise C2CModelError("支付方式标识无效", code="invalid_payment_method")
        if identifier not in normalized:
            normalized.append(identifier)
    return tuple(normalized)


def _normalize_unknown_qualifications(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values = (value,) if isinstance(value, str) else tuple(value)  # type: ignore[arg-type]
    normalized: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip()
        if not text or len(text) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise C2CModelError("资格条件标识无效", code="invalid_qualification")
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _default_fiat_step(fiat: str) -> str:
    places = currency_metadata(fiat, "fiat").display_precision
    return decimal_to_canonical(Decimal(1).scaleb(-places))


def _normalize_generation(value: object) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise C2CModelError("请求 generation 无效", code="invalid_generation")
    if isinstance(value, int):
        if value.bit_length() > 256:
            raise C2CModelError("请求 generation 过长", code="invalid_generation")
        return value
    if _REQUEST_ID_RE.fullmatch(value) is None:
        raise C2CModelError("请求 generation 无效", code="invalid_generation")
    return value


@dataclass(frozen=True, slots=True)
class QuoteRequest(SerializableModel):
    """An amount quote request.

    ``amount`` is fiat for ``BUY`` and crypto for ``SELL``.  A payment filter
    contains provider-returned identifiers, never guessed display names.
    """

    fiat: str
    asset: str
    direction: Direction | str
    amount: str
    provider: str = "auto"
    payment_methods: tuple[str, ...] = ()
    asset_step: str | None = None
    fiat_step: str | None = None
    allow_market_fallback: bool = False
    request_id: str = ""
    generation: int | str | None = None

    def __post_init__(self) -> None:
        try:
            direction = self.direction if isinstance(self.direction, Direction) else Direction(
                str(self.direction).strip().upper()
            )
        except ValueError as exc:
            raise C2CModelError("C2C 方向必须为 BUY 或 SELL", code="invalid_direction") from exc
        amount = _positive_amount(self.amount, "报价金额")
        if not isinstance(self.provider, str):
            raise C2CModelError("C2C 平台标识无效", code="invalid_provider")
        provider = self.provider.strip().lower()
        if provider != "auto":
            provider = _provider_name(provider)
        request_id = str(self.request_id or "")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise C2CModelError("请求标识无效", code="invalid_request_id")
        if isinstance(self.allow_market_fallback, bool) is False:
            raise C2CModelError("普通行情降级开关无效", code="invalid_fallback_option")
        generation = _normalize_generation(self.generation)
        asset_step = None if self.asset_step is None else _positive_amount(
            self.asset_step, "虚拟币步长"
        )
        fiat_step = None if self.fiat_step is None else _positive_amount(
            self.fiat_step, "法币最小单位"
        )
        fiat = normalize_currency_code(self.fiat)
        asset = normalize_currency_code(self.asset)
        if fiat == asset:
            raise C2CModelError("C2C 法币与虚拟币不得相同", code="invalid_pair")
        object.__setattr__(self, "fiat", fiat)
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "payment_methods", _normalize_payments(self.payment_methods))
        object.__setattr__(self, "asset_step", asset_step)
        object.__setattr__(self, "fiat_step", fiat_step)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "generation", generation)

    @property
    def source(self) -> str:
        return self.fiat if self.direction is Direction.BUY else self.asset

    @property
    def target(self) -> str:
        return self.asset if self.direction is Direction.BUY else self.fiat

    @property
    def amount_unit(self) -> str:
        return self.fiat if self.direction is Direction.BUY else self.asset

    @property
    def resolved_asset_step(self) -> str:
        return self.asset_step or "0.00000001"

    @property
    def resolved_fiat_step(self) -> str:
        return self.fiat_step or _default_fiat_step(self.fiat)

    @property
    def cache_key(self) -> tuple[object, ...]:
        return (
            self.provider,
            self.fiat,
            self.asset,
            self.direction.value,
            self.amount,
            self.payment_methods,
            self.resolved_asset_step,
            self.resolved_fiat_step,
            self.allow_market_fallback,
        )


@dataclass(frozen=True, slots=True)
class PaymentMethod(SerializableModel):
    identifier: str
    display_name: str = ""

    def __post_init__(self) -> None:
        identifier = _normalize_payments((self.identifier,))[0]
        display_name = str(self.display_name or "").strip()
        if len(display_name) > 120 or any(
            ord(char) < 32 or ord(char) == 127 for char in display_name
        ):
            raise C2CModelError("支付方式名称无效", code="invalid_payment_method")
        object.__setattr__(self, "identifier", identifier)
        object.__setattr__(self, "display_name", display_name)


def _canonical_completion_rate(value: object | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = _nonnegative_amount(value, "完成率")
    parsed = parse_amount(text)
    if parsed <= 1:
        with localcontext() as context:
            context.prec = 220
            parsed *= Decimal(100)
    if parsed > 100:
        raise C2CModelError("完成率超出范围", code="invalid_completion_rate")
    return decimal_to_canonical(parsed)


@dataclass(frozen=True, slots=True)
class NormalizedAd(SerializableModel):
    """A privacy-minimized advertisement used by the matching engine."""

    provider: str
    ad_id: str
    fiat: str
    asset: str
    direction: Direction | str
    price: str
    min_fiat: str
    available_asset: str
    max_fiat: str | None = None
    payment_methods: tuple[str, ...] = ()
    completion_rate: str | None = None
    completed_orders: str | None = None
    asset_step: str | None = None
    fiat_step: str | None = None
    unknown_qualifications: tuple[str, ...] = ("KYC", "地区", "账户年龄等资格")

    def __post_init__(self) -> None:
        try:
            direction = self.direction if isinstance(self.direction, Direction) else Direction(
                str(self.direction).strip().upper()
            )
        except ValueError as exc:
            raise C2CModelError("广告方向无效", code="invalid_direction") from exc
        if not isinstance(self.ad_id, str):
            raise C2CModelError("广告标识无效", code="invalid_ad_id")
        ad_id = self.ad_id.strip()
        if _AD_ID_RE.fullmatch(ad_id) is None:
            raise C2CModelError("广告标识无效", code="invalid_ad_id")
        completed_orders = None
        if self.completed_orders is not None and str(self.completed_orders).strip() != "":
            completed_orders = _nonnegative_amount(self.completed_orders, "成交数")
            if parse_amount(completed_orders) != parse_amount(completed_orders).to_integral_value():
                raise C2CModelError("成交数必须为整数", code="invalid_completed_orders")
        object.__setattr__(self, "provider", _provider_name(self.provider))
        object.__setattr__(self, "ad_id", ad_id)
        object.__setattr__(self, "fiat", normalize_currency_code(self.fiat))
        object.__setattr__(self, "asset", normalize_currency_code(self.asset))
        object.__setattr__(self, "direction", direction)
        if isinstance(self.price, (bool, float)):
            raise C2CModelError("广告价格必须使用十进制字符串或 Decimal", code="invalid_decimal")
        object.__setattr__(self, "price", canonical_rate_string(self.price))
        object.__setattr__(self, "min_fiat", _nonnegative_amount(self.min_fiat, "广告下限"))
        object.__setattr__(
            self, "available_asset", _nonnegative_amount(self.available_asset, "广告库存")
        )
        object.__setattr__(
            self,
            "max_fiat",
            None if self.max_fiat is None or str(self.max_fiat).strip() == "" else
            _nonnegative_amount(self.max_fiat, "广告上限"),
        )
        object.__setattr__(self, "payment_methods", _normalize_payments(self.payment_methods))
        object.__setattr__(self, "completion_rate", _canonical_completion_rate(self.completion_rate))
        object.__setattr__(self, "completed_orders", completed_orders)
        object.__setattr__(
            self,
            "asset_step",
            None if self.asset_step is None or str(self.asset_step).strip() == "" else
            _positive_amount(self.asset_step, "广告虚拟币步长"),
        )
        object.__setattr__(
            self,
            "fiat_step",
            None if self.fiat_step is None or str(self.fiat_step).strip() == "" else
            _positive_amount(self.fiat_step, "广告法币单位"),
        )
        object.__setattr__(
            self,
            "unknown_qualifications",
            _normalize_unknown_qualifications(self.unknown_qualifications),
        )

    @property
    def effective_max_fiat(self) -> str:
        available = parse_amount(self.available_asset)
        price = parse_amount(self.price)
        with localcontext() as context:
            context.prec = max(440, len(available.as_tuple().digits) + len(price.as_tuple().digits) + 16)
            inventory_value = available * price
        if self.max_fiat is not None:
            inventory_value = min(inventory_value, parse_amount(self.max_fiat))
        try:
            return canonical_amount_string(decimal_to_canonical(inventory_value))
        except DecimalBoundaryError as exc:
            raise C2CModelError(
                "广告有效上限超出安全十进制边界", code="invalid_effective_limit"
            ) from exc

    @property
    def valid_range(self) -> bool:
        effective = parse_amount(self.effective_max_fiat)
        return (
            parse_amount(self.available_asset) > 0
            and effective > 0
            and parse_amount(self.min_fiat) <= effective
        )


@dataclass(frozen=True, slots=True)
class AmountRange(SerializableModel):
    provider: str
    ad_id: str
    unit: str
    lower: str
    upper: str
    payment_methods: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        lower = _nonnegative_amount(self.lower, "范围下限")
        upper = _nonnegative_amount(self.upper, "范围上限")
        if parse_amount(lower) > parse_amount(upper):
            raise C2CModelError("范围下限大于上限", code="invalid_range")
        object.__setattr__(self, "provider", _provider_name(self.provider))
        object.__setattr__(self, "unit", normalize_currency_code(self.unit))
        object.__setattr__(self, "lower", lower)
        object.__setattr__(self, "upper", upper)
        object.__setattr__(self, "payment_methods", _normalize_payments(self.payment_methods))


@dataclass(frozen=True, slots=True)
class RangeError(SerializableModel):
    requested_amount: str
    requested_unit: str
    ranges: tuple[AmountRange, ...]
    code: str = "amount_out_of_range"

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_amount", _positive_amount(
            self.requested_amount, "请求金额"
        ))
        object.__setattr__(self, "requested_unit", normalize_currency_code(self.requested_unit))
        object.__setattr__(self, "ranges", tuple(self.ranges))


@dataclass(frozen=True, slots=True)
class MatchResult(SerializableModel):
    provider: str
    ad: NormalizedAd
    input_amount: str
    input_unit: str
    output_amount: str
    output_unit: str
    price: str
    actual_fiat: str
    actual_crypto: str
    remainder: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _provider_name(self.provider))
        for name in (
            "input_amount", "output_amount", "actual_fiat", "actual_crypto", "remainder"
        ):
            object.__setattr__(self, name, _nonnegative_amount(getattr(self, name), name))
        object.__setattr__(self, "input_unit", normalize_currency_code(self.input_unit))
        object.__setattr__(self, "output_unit", normalize_currency_code(self.output_unit))
        if isinstance(self.price, (bool, float)):
            raise C2CModelError("匹配价格必须使用十进制字符串或 Decimal", code="invalid_decimal")
        object.__setattr__(self, "price", canonical_rate_string(self.price))
        warnings: list[str] = []
        raw_warnings = (self.warnings,) if isinstance(self.warnings, str) else tuple(self.warnings)
        for item in raw_warnings:
            warning = str(item)
            if len(warning) > 500 or any(ord(char) < 32 or ord(char) == 127 for char in warning):
                raise C2CModelError("报价警告文本无效", code="invalid_warning")
            if warning and warning not in warnings:
                warnings.append(warning)
        object.__setattr__(self, "warnings", tuple(warnings))


@dataclass(frozen=True, slots=True)
class ProviderCapability(SerializableModel):
    provider: str
    enabled: bool
    configured: bool
    requires_whitelist: bool
    quote_price: bool
    ad_list: bool
    trade_methods: bool
    read_only: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        for name in (
            "enabled", "configured", "requires_whitelist", "quote_price",
            "ad_list", "trade_methods", "read_only"
        ):
            if not isinstance(getattr(self, name), bool):
                raise C2CModelError("平台能力标志必须为布尔值", code="invalid_capability")
        object.__setattr__(self, "provider", _provider_name(self.provider))
        note = str(self.note or "")
        if len(note) > 300 or any(ord(char) < 32 or ord(char) == 127 for char in note):
            raise C2CModelError("平台能力说明无效", code="invalid_capability")
        object.__setattr__(self, "note", note)


@dataclass(frozen=True, slots=True)
class QuoteResult(SerializableModel):
    provider: str | None
    status: QuoteStatus | str
    data_state: DataState | str
    fiat: str
    asset: str
    direction: Direction | str
    input_amount: str
    market_best_price: str | None = None
    market_best_provider: str | None = None
    indicative_price: str | None = None
    indicative_output_amount: str | None = None
    output_unit: str | None = None
    match: MatchResult | None = None
    range_error: RangeError | None = None
    ads_considered: int = 0
    compared_providers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    retry_after_seconds: int | None = None
    request_id: str = ""
    generation: int | str | None = None

    def __post_init__(self) -> None:
        try:
            status = self.status if isinstance(self.status, QuoteStatus) else QuoteStatus(self.status)
            state = self.data_state if isinstance(self.data_state, DataState) else DataState(
                self.data_state
            )
            direction = self.direction if isinstance(self.direction, Direction) else Direction(
                str(self.direction).upper()
            )
        except ValueError as exc:
            raise C2CModelError("报价结果枚举无效", code="invalid_quote_result") from exc
        provider = None if self.provider is None else _provider_name(self.provider)
        if isinstance(self.market_best_price, (bool, float)):
            raise C2CModelError("市场展示价必须使用十进制字符串或 Decimal")
        best = None if self.market_best_price is None else canonical_rate_string(self.market_best_price)
        best_provider = None if self.market_best_provider is None else _provider_name(
            self.market_best_provider
        )
        if isinstance(self.indicative_price, (bool, float)):
            raise C2CModelError("普通行情价格必须使用十进制字符串或 Decimal")
        indicative_price = None if self.indicative_price is None else canonical_rate_string(
            self.indicative_price
        )
        indicative = None if self.indicative_output_amount is None else _nonnegative_amount(
            self.indicative_output_amount, "参考输出金额"
        )
        output_unit = None if self.output_unit is None else normalize_currency_code(self.output_unit)
        raw_compared = (
            (self.compared_providers,)
            if isinstance(self.compared_providers, str)
            else tuple(self.compared_providers)
        )
        compared = tuple(_provider_name(item) for item in raw_compared)
        retry = self.retry_after_seconds
        if retry is not None and (isinstance(retry, bool) or not isinstance(retry, int) or retry < 0):
            raise C2CModelError("重试等待时间无效", code="invalid_retry_after")
        if (
            isinstance(self.ads_considered, bool)
            or not isinstance(self.ads_considered, int)
            or self.ads_considered < 0
        ):
            raise C2CModelError("广告计数无效", code="invalid_ad_count")
        request_id = str(self.request_id or "")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise C2CModelError("请求标识无效", code="invalid_request_id")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "data_state", state)
        object.__setattr__(self, "fiat", normalize_currency_code(self.fiat))
        object.__setattr__(self, "asset", normalize_currency_code(self.asset))
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "input_amount", _positive_amount(self.input_amount, "输入金额"))
        object.__setattr__(self, "market_best_price", best)
        object.__setattr__(self, "market_best_provider", best_provider)
        object.__setattr__(self, "indicative_price", indicative_price)
        object.__setattr__(self, "indicative_output_amount", indicative)
        object.__setattr__(self, "output_unit", output_unit)
        object.__setattr__(self, "compared_providers", tuple(dict.fromkeys(compared)))
        warnings: list[str] = []
        generation = _normalize_generation(self.generation)
        raw_warnings = (self.warnings,) if isinstance(self.warnings, str) else tuple(self.warnings)
        for item in raw_warnings:
            warning = str(item)
            if len(warning) > 500 or any(ord(char) < 32 or ord(char) == 127 for char in warning):
                raise C2CModelError("报价警告文本无效", code="invalid_warning")
            if warning and warning not in warnings:
                warnings.append(warning)
        object.__setattr__(self, "warnings", tuple(warnings))
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "generation", generation)

    @property
    def is_c2c_executable(self) -> bool:
        return self.status is QuoteStatus.OK and self.match is not None

    def for_request(self, request: QuoteRequest) -> "QuoteResult":
        return replace(self, request_id=request.request_id, generation=request.generation)

    def with_data_state(self, state: DataState, warning: str | None = None) -> "QuoteResult":
        warnings = self.warnings if not warning else self.warnings + (warning,)
        return replace(self, data_state=state, warnings=warnings)


__all__ = [
    "AmountRange",
    "C2CModelError",
    "DataState",
    "Direction",
    "MatchResult",
    "NormalizedAd",
    "PaymentMethod",
    "ProviderCapability",
    "QuoteRequest",
    "QuoteResult",
    "QuoteStatus",
    "RangeError",
    "SerializableModel",
]
