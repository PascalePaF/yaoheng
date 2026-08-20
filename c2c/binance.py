"""Read-only adapter for Binance's official public P2P Skill endpoints."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from threading import RLock
from typing import Any, Callable

from app_version import APP_USER_AGENT
from conversion_core import canonical_amount_string, decimal_to_canonical, normalize_currency_code, parse_amount

from .base import (
    BaseP2PAdapter,
    CancellationToken,
    HttpTransport,
    PaymentMethodUnavailableError,
    ProviderProtocolError,
    ProviderUnconfiguredError,
    ResilientJsonClient,
)
from .models import (
    C2CModelError,
    Direction,
    NormalizedAd,
    PaymentMethod,
    ProviderCapability,
    QuoteRequest,
)


BINANCE_P2P_BASE_URL = "https://www.binance.com"
BINANCE_QUOTE_PATH = "/bapi/c2c/v1/public/c2c/agent/quote-price"
BINANCE_AD_LIST_PATH = "/bapi/c2c/v1/public/c2c/agent/ad-list"
BINANCE_TRADE_METHODS_PATH = "/bapi/c2c/v1/public/c2c/agent/trade-methods"
TRADE_METHOD_CACHE_SECONDS = 24 * 60 * 60


def _field(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _unwrap(payload: object, *, provider: str = "Binance") -> object:
    root = _mapping(payload)
    if root is None:
        raise ProviderProtocolError(f"{provider} 响应根节点必须为对象")
    success = root.get("success")
    if success is False:
        raise ProviderProtocolError(f"{provider} 返回失败状态")
    code = root.get("code")
    if code is not None and str(code).strip() not in {"0", "000000", "SUCCESS", "success"}:
        raise ProviderProtocolError(f"{provider} 返回非成功代码")
    if "data" not in root:
        raise ProviderProtocolError(f"{provider} 响应缺少 data")
    return root["data"]


def _list_data(data: object, keys: Sequence[str]) -> list[object]:
    if isinstance(data, list):
        return data
    mapping = _mapping(data)
    if mapping is None:
        raise ProviderProtocolError("Binance 列表响应格式无效")
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, list):
            return value
    raise ProviderProtocolError("Binance 列表响应缺少数组")


def _scale_step(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        scale = int(str(value))
    except (TypeError, ValueError):
        return None
    if scale < 0 or scale > 24:
        return None
    return decimal_to_canonical(Decimal(1).scaleb(-scale))


def _payment_identifiers(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    rows = value if isinstance(value, list) else (value,)
    found: list[str] = []
    for row in rows:
        if isinstance(row, str):
            identifier = row.strip()
        else:
            item = _mapping(row)
            if item is None:
                continue
            raw_identifier = _field(
                item,
                ("identifier", "tradeMethodIdentifier", "payType", "code"),
            )
            if not isinstance(raw_identifier, str):
                continue
            identifier = raw_identifier.strip()
        if identifier and identifier not in found:
            try:
                found.append(PaymentMethod(identifier).identifier)
            except C2CModelError:
                continue
    return tuple(found)


def _optional_stat(value: object, *, integer: bool = False, maximum: str | None = None) -> str | None:
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        text = canonical_amount_string(value)
        parsed = parse_amount(text)
    except ValueError:
        return None
    if parsed < 0 or (integer and parsed != parsed.to_integral_value()):
        return None
    if maximum is not None and parsed > parse_amount(maximum):
        return None
    return text


def parse_binance_trade_methods(payload: object) -> tuple[PaymentMethod, ...]:
    rows = _list_data(
        _unwrap(payload),
        ("tradeMethods", "methods", "list", "rows"),
    )
    methods: list[PaymentMethod] = []
    for row in rows:
        item = _mapping(row)
        if item is None:
            continue
        identifier = _field(item, ("identifier", "tradeMethodIdentifier", "code"))
        name = _field(item, ("tradeMethodName", "name", "displayName"))
        try:
            if not isinstance(identifier, str):
                raise C2CModelError("支付方式标识无效")
            method = PaymentMethod(identifier, str(name or ""))
        except C2CModelError:
            continue
        if all(existing.identifier != method.identifier for existing in methods):
            methods.append(method)
    if rows and not methods:
        raise ProviderProtocolError("Binance 支付方式响应没有可用标识")
    return tuple(methods)


def parse_binance_quote_price(payload: object) -> str:
    data = _unwrap(payload)
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, (str, int, Decimal)) and not isinstance(data, bool):
        candidate = data
    else:
        item = _mapping(data)
        if item is None:
            raise ProviderProtocolError("Binance 快速报价格式无效")
        candidate = _field(item, ("price", "quotePrice", "unitPrice"))
    try:
        from conversion_core import canonical_rate_string

        return canonical_rate_string(candidate)
    except (TypeError, ValueError) as exc:
        raise ProviderProtocolError("Binance 快速报价价格无效") from exc


def _parse_binance_ad(row: object, request: QuoteRequest) -> NormalizedAd:
    outer = _mapping(row)
    if outer is None:
        raise ProviderProtocolError("Binance 广告行必须为对象")
    ad = _mapping(outer.get("adv")) or _mapping(outer.get("advertisement")) or outer
    advertiser = _mapping(outer.get("advertiser")) or _mapping(outer.get("merchant")) or {}

    fiat = _field(ad, ("fiatUnit", "fiat", "fiatCurrency")) or request.fiat
    asset = _field(ad, ("asset", "cryptoCurrency")) or request.asset
    raw_direction = _field(ad, ("tradeType", "side", "direction"))
    direction = request.direction if raw_direction is None else str(raw_direction).upper()
    payments = _payment_identifiers(
        _field(ad, ("tradeMethods", "tradeMethodIdentifiers", "payTypes", "paymentMethods"))
    )
    completion = _field(
        advertiser,
        ("monthFinishRate", "monthlyFinishRate", "completionRate", "finishRate"),
    )
    if completion is None:
        completion = _field(ad, ("monthFinishRate", "completionRate"))
    orders = _field(
        advertiser,
        ("monthOrderCount", "monthlyOrderCount", "completedOrderNumOfLatest30day", "orderCount"),
    )
    if orders is None:
        orders = _field(ad, ("monthOrderCount", "orderCount"))
    completion = _optional_stat(completion, maximum="100")
    orders = _optional_stat(orders, integer=True)

    try:
        raw_ad_id = _field(ad, ("advNo", "adNo", "adId", "id"))
        if isinstance(raw_ad_id, bool) or raw_ad_id is None:
            raise ProviderProtocolError("Binance 广告标识无效")
        normalized = NormalizedAd(
            provider="binance",
            ad_id=str(raw_ad_id),
            fiat=str(fiat),
            asset=str(asset),
            direction=direction,
            price=_field(ad, ("price", "unitPrice", "quotePrice")),
            min_fiat=_field(
                ad,
                ("minSingleTransAmount", "minAmount", "minLimit", "minFiatAmount"),
            ),
            max_fiat=_field(
                ad,
                ("maxSingleTransAmount", "maxAmount", "maxLimit", "maxFiatAmount"),
            ),
            available_asset=_field(
                ad,
                ("surplusAmount", "availableAmount", "availableAsset", "assetAmount"),
            ),
            payment_methods=payments,
            completion_rate=completion,
            completed_orders=orders,
            asset_step=_field(ad, ("assetStep", "quantityStep")) or _scale_step(
                _field(ad, ("assetScale", "quantityScale"))
            ),
            fiat_step=_field(ad, ("fiatStep", "priceStep")) or _scale_step(
                _field(ad, ("fiatScale", "priceScale"))
            ),
        )
        # Force validation of the derived inventory cap at the parser boundary.
        normalized.effective_max_fiat
        return normalized
    except (C2CModelError, TypeError, ValueError) as exc:
        raise ProviderProtocolError("Binance 广告字段未通过数值或结构校验") from exc


def parse_binance_ads(payload: object, request: QuoteRequest) -> tuple[NormalizedAd, ...]:
    rows = _list_data(_unwrap(payload), ("ads", "list", "rows", "items"))
    parsed: list[NormalizedAd] = []
    rejected = 0
    for row in rows:
        try:
            parsed.append(_parse_binance_ad(row, request))
        except ProviderProtocolError:
            rejected += 1
    if rows and rejected == len(rows):
        raise ProviderProtocolError("Binance 广告响应全部无效")
    return tuple(parsed)


class BinanceP2PAdapter(BaseP2PAdapter):
    """Only the three official public, read-only P2P Skill capabilities."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        client: ResilientJsonClient | None = None,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("Binance enabled 必须为布尔值")
        self._enabled = enabled
        self._client = client or ResilientJsonClient(transport=transport, clock=clock)
        self._clock = clock
        self._methods_cache: dict[str, tuple[float, tuple[PaymentMethod, ...]]] = {}
        self._methods_lock = RLock()
        self._headers = {"Accept": "application/json", "User-Agent": APP_USER_AGENT}

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider="binance",
            enabled=self._enabled,
            configured=self._enabled,
            requires_whitelist=False,
            quote_price=True,
            ad_list=True,
            trade_methods=True,
            note="Binance 官方 P2P Skill 公共只读接口；不含订单、账户或商家管理。",
        )

    def fetch_quote_price(
        self,
        fiat: str,
        asset: str,
        direction: Direction | str,
        *,
        cancel: CancellationToken | None = None,
    ) -> str:
        if not self._enabled:
            raise ProviderUnconfiguredError("Binance P2P 已禁用")
        fiat_code = normalize_currency_code(fiat)
        asset_code = normalize_currency_code(asset)
        try:
            side = direction if isinstance(direction, Direction) else Direction(str(direction).upper())
        except ValueError as exc:
            raise ProviderProtocolError("Binance 报价方向无效") from exc
        payload = self._client.request_json(
            "GET",
            BINANCE_P2P_BASE_URL + BINANCE_QUOTE_PATH,
            params={"fiat": fiat_code, "asset": asset_code, "tradeType": side.value},
            headers=self._headers,
            cancel=cancel,
        )
        return parse_binance_quote_price(payload)

    quote_price = fetch_quote_price

    def list_trade_methods(
        self,
        fiat: str,
        *,
        cancel: CancellationToken | None = None,
    ) -> tuple[PaymentMethod, ...]:
        if not self._enabled:
            raise ProviderUnconfiguredError("Binance P2P 已禁用")
        fiat_code = normalize_currency_code(fiat)
        now = self._clock()
        with self._methods_lock:
            cached = self._methods_cache.get(fiat_code)
            if cached is not None and now - cached[0] <= TRADE_METHOD_CACHE_SECONDS:
                return cached[1]
        payload = self._client.request_json(
            "GET",
            BINANCE_P2P_BASE_URL + BINANCE_TRADE_METHODS_PATH,
            params={"fiat": fiat_code},
            headers=self._headers,
            cancel=cancel,
        )
        methods = parse_binance_trade_methods(payload)
        with self._methods_lock:
            self._methods_cache[fiat_code] = (self._clock(), methods)
        return methods

    trade_methods = list_trade_methods

    def fetch_ads(
        self,
        request: QuoteRequest,
        *,
        cancel: CancellationToken | None = None,
    ) -> tuple[NormalizedAd, ...]:
        if not self._enabled:
            raise ProviderUnconfiguredError("Binance P2P 已禁用")
        if request.payment_methods:
            supported = {method.identifier for method in self.list_trade_methods(
                request.fiat, cancel=cancel
            )}
            if any(identifier not in supported for identifier in request.payment_methods):
                raise PaymentMethodUnavailableError("支付方式标识未由 Binance 当前接口返回")
        params: list[tuple[str, object]] = [
            ("fiat", request.fiat),
            ("asset", request.asset),
            ("tradeType", request.direction.value),
            ("limit", "20"),
        ]
        for identifier in request.payment_methods:
            # The official Skill requires a plain repeated query value, never a
            # JSON array and never a locally guessed payment-method alias.
            params.append(("tradeMethodIdentifiers", identifier))
        payload = self._client.request_json(
            "GET",
            BINANCE_P2P_BASE_URL + BINANCE_AD_LIST_PATH,
            params=params,
            headers=self._headers,
            cancel=cancel,
        )
        return parse_binance_ads(payload, request)

    ad_list = fetch_ads


BinanceAdapter = BinanceP2PAdapter


__all__ = [
    "BINANCE_AD_LIST_PATH",
    "BINANCE_P2P_BASE_URL",
    "BINANCE_QUOTE_PATH",
    "BINANCE_TRADE_METHODS_PATH",
    "BinanceAdapter",
    "BinanceP2PAdapter",
    "TRADE_METHOD_CACHE_SECONDS",
    "parse_binance_ads",
    "parse_binance_quote_price",
    "parse_binance_trade_methods",
]
