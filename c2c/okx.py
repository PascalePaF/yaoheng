"""Configurable, whitelist-gated adapter boundary for OKX P2P merchant APIs.

OKX does not publish a general public P2P order-book endpoint.  This module
therefore contains no guessed or reverse-engineered path.  A caller with an
approved merchant contract must explicitly supply its path and parameter names.
Credentials are obtained transiently from a callback and never serialized,
cached, logged, or included in public capability objects.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from app_version import APP_USER_AGENT
from conversion_core import canonical_amount_string, parse_amount

from .base import (
    BaseP2PAdapter,
    CancellationToken,
    HttpTransport,
    PaymentMethodUnavailableError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderUnconfiguredError,
    ResilientJsonClient,
)
from .models import (
    C2CModelError,
    NormalizedAd,
    PaymentMethod,
    ProviderCapability,
    QuoteRequest,
    QuoteResult,
    QuoteStatus,
)


OKX_DEFAULT_BASE_URL = "https://openapi.okx.com"
_OKX_ALLOWED_HOSTS = frozenset({"openapi.okx.com", "us.okx.com", "eea.okx.com"})
_PARAMETER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


def _safe_parameter(value: object, label: str) -> str:
    text = str(value or "").strip()
    if _PARAMETER_RE.fullmatch(text) is None:
        raise C2CModelError(f"OKX {label}无效", code="invalid_okx_contract")
    return text


def _safe_contract_value(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 96 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise C2CModelError(f"OKX {label}无效", code="invalid_okx_contract")
    return text


@dataclass(frozen=True, slots=True)
class OkxApprovedRequest:
    """Names and values copied from a caller's approved OKX P2P contract."""

    path: str
    method: str
    asset_parameter: str
    fiat_parameter: str
    direction_parameter: str
    buy_value: str
    sell_value: str
    payment_parameter: str | None = None
    payment_mode: str = "single"
    limit_parameter: str | None = None
    limit_value: str | None = None

    def __post_init__(self) -> None:
        path = str(self.path or "").strip()
        if (
            not path.startswith("/")
            or path.startswith("//")
            or len(path) > 300
            or "?" in path
            or "#" in path
            or "\\" in path
            or any(segment in {".", ".."} for segment in path.split("/"))
            or re.fullmatch(r"/[A-Za-z0-9_./-]+", path) is None
            or any(char in path for char in "\r\n\x00")
        ):
            raise C2CModelError("OKX 获批接口路径无效", code="invalid_okx_contract")
        method = str(self.method or "").strip().upper()
        if method not in {"GET", "POST"}:
            raise C2CModelError("OKX 只读接口方法必须为 GET 或 POST", code="invalid_okx_contract")
        payment_mode = str(self.payment_mode or "single").strip().lower()
        if payment_mode not in {"single", "comma", "repeat"}:
            raise C2CModelError("OKX 支付参数编码方式无效", code="invalid_okx_contract")
        if self.limit_parameter is None and self.limit_value is not None:
            raise C2CModelError("OKX limit 配置不完整", code="invalid_okx_contract")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "asset_parameter", _safe_parameter(
            self.asset_parameter, "资产参数"
        ))
        object.__setattr__(self, "fiat_parameter", _safe_parameter(
            self.fiat_parameter, "法币参数"
        ))
        object.__setattr__(self, "direction_parameter", _safe_parameter(
            self.direction_parameter, "方向参数"
        ))
        object.__setattr__(self, "buy_value", _safe_contract_value(self.buy_value, "买入值"))
        object.__setattr__(self, "sell_value", _safe_contract_value(self.sell_value, "卖出值"))
        object.__setattr__(
            self,
            "payment_parameter",
            None if self.payment_parameter is None else _safe_parameter(
                self.payment_parameter, "支付参数"
            ),
        )
        object.__setattr__(self, "payment_mode", payment_mode)
        object.__setattr__(
            self,
            "limit_parameter",
            None if self.limit_parameter is None else _safe_parameter(
                self.limit_parameter, "数量参数"
            ),
        )
        object.__setattr__(
            self,
            "limit_value",
            None if self.limit_value is None else _safe_contract_value(
                self.limit_value, "数量值"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "method": self.method,
            "asset_parameter": self.asset_parameter,
            "fiat_parameter": self.fiat_parameter,
            "direction_parameter": self.direction_parameter,
            "buy_value": self.buy_value,
            "sell_value": self.sell_value,
            "payment_parameter": self.payment_parameter,
            "payment_mode": self.payment_mode,
            "limit_parameter": self.limit_parameter,
            "limit_value": self.limit_value,
        }


@dataclass(frozen=True, slots=True)
class OkxResponseSchema:
    """Allowlisted response-field map supplied with the merchant contract."""

    list_path: tuple[str, ...] = ("data",)
    ad_id_fields: tuple[str, ...] = ("adId", "advNo", "id")
    price_fields: tuple[str, ...] = ("price", "unitPrice")
    min_fiat_fields: tuple[str, ...] = ("minAmount", "minFiatAmount", "minLimit")
    max_fiat_fields: tuple[str, ...] = ("maxAmount", "maxFiatAmount", "maxLimit")
    available_asset_fields: tuple[str, ...] = (
        "availableAmount", "availableAsset", "surplusAmount"
    )
    payment_fields: tuple[str, ...] = (
        "paymentMethods", "tradeMethods", "paymentMethodIdentifiers"
    )
    completion_rate_fields: tuple[str, ...] = (
        "completionRate", "monthFinishRate", "completedRate"
    )
    completed_orders_fields: tuple[str, ...] = (
        "completedOrders", "monthOrderCount", "orderCount"
    )
    asset_fields: tuple[str, ...] = ("asset", "cryptoCurrency")
    fiat_fields: tuple[str, ...] = ("fiat", "fiatCurrency", "fiatUnit")
    direction_fields: tuple[str, ...] = ("side", "tradeType", "direction")
    asset_step_fields: tuple[str, ...] = ("assetStep", "quantityStep")
    fiat_step_fields: tuple[str, ...] = ("fiatStep", "priceStep")

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:  # type: ignore[attr-defined]
            raw_values = getattr(self, field_name)
            values = (raw_values,) if isinstance(raw_values, str) else tuple(raw_values)
            if not values or any(_PATH_SEGMENT_RE.fullmatch(str(item)) is None for item in values):
                raise C2CModelError("OKX 响应字段映射无效", code="invalid_okx_schema")
            object.__setattr__(self, field_name, values)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            field_name: list(getattr(self, field_name))
            for field_name in self.__dataclass_fields__  # type: ignore[attr-defined]
        }


@dataclass(frozen=True, slots=True)
class OkxP2PConfig:
    enabled: bool = False
    whitelisted: bool = False
    base_url: str = OKX_DEFAULT_BASE_URL
    approved_request: OkxApprovedRequest | None = None
    response_schema: OkxResponseSchema = OkxResponseSchema()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.whitelisted, bool):
            raise C2CModelError("OKX 启用/白名单标志必须为布尔值", code="invalid_okx_config")
        if self.approved_request is not None and not isinstance(
            self.approved_request, OkxApprovedRequest
        ):
            raise C2CModelError("OKX 获批请求配置类型无效", code="invalid_okx_config")
        if not isinstance(self.response_schema, OkxResponseSchema):
            raise C2CModelError("OKX 响应配置类型无效", code="invalid_okx_config")
        parsed = urlparse(str(self.base_url or "").strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _OKX_ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.path not in {"", "/"})
        ):
            raise C2CModelError("OKX API 域名必须为官方区域域名", code="invalid_okx_base_url")
        object.__setattr__(self, "base_url", f"https://{parsed.hostname}")

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "whitelisted": self.whitelisted,
            "base_url": self.base_url,
            "approved_request": None if self.approved_request is None else
            self.approved_request.to_dict(),
            "response_schema": self.response_schema.to_dict(),
        }


CredentialsProvider = Callable[[], Mapping[str, str] | None]


def _field(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _at_path(payload: object, path: Sequence[str]) -> object:
    current = payload
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            raise ProviderProtocolError("OKX 响应缺少获批字段路径")
        current = current[segment]
    return current


def _unwrap_okx(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ProviderProtocolError("OKX 响应根节点必须为对象")
    code = payload.get("code")
    if code is not None and str(code).strip() not in {"0", "000000", "SUCCESS", "success"}:
        normalized = str(code).strip()
        if normalized in {"50101", "50111", "50113", "50119"}:
            raise ProviderPermissionError("OKX 凭据或白名单权限无效")
        if normalized in {"50011", "50040"}:
            raise ProviderRateLimitError("OKX 接口限流", 60)
        raise ProviderProtocolError("OKX 返回非成功代码")
    return payload


def _payment_identifiers(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    rows = value if isinstance(value, list) else (value,)
    methods: list[str] = []
    for row in rows:
        if isinstance(row, str):
            raw_identifier = row
        elif isinstance(row, Mapping):
            raw_identifier = _field(
                row,
                ("identifier", "paymentMethodIdentifier", "code", "paymentType"),
            )
        else:
            continue
        try:
            if not isinstance(raw_identifier, str):
                raise C2CModelError("支付方式标识无效")
            identifier = PaymentMethod(raw_identifier).identifier
        except C2CModelError:
            continue
        if identifier not in methods:
            methods.append(identifier)
    return tuple(methods)


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


def _parse_okx_direction(raw: object, request: QuoteRequest, contract: OkxApprovedRequest | None) -> str:
    if raw is None:
        return request.direction.value
    text = str(raw).strip()
    if text.upper() in {"BUY", "SELL"}:
        return text.upper()
    if contract is not None:
        if text == contract.buy_value:
            return "BUY"
        if text == contract.sell_value:
            return "SELL"
    raise ProviderProtocolError("OKX 广告方向无法按获批映射解释")


def parse_okx_ads(
    payload: object,
    request: QuoteRequest,
    *,
    schema: OkxResponseSchema | None = None,
    contract: OkxApprovedRequest | None = None,
) -> tuple[NormalizedAd, ...]:
    """Parse only allowlisted fields; raw OKX rows never escape this function."""

    schema = schema or OkxResponseSchema()
    root = _unwrap_okx(payload)
    target = _at_path(root, schema.list_path)
    if isinstance(target, Mapping):
        for candidate in ("ads", "list", "items", "rows"):
            if isinstance(target.get(candidate), list):
                target = target[candidate]
                break
    if not isinstance(target, list):
        raise ProviderProtocolError("OKX 获批广告列表不是数组")
    parsed: list[NormalizedAd] = []
    rejected = 0
    for row in target:
        if not isinstance(row, Mapping):
            rejected += 1
            continue
        try:
            raw_ad_id = _field(row, schema.ad_id_fields)
            if isinstance(raw_ad_id, bool) or raw_ad_id is None:
                raise ProviderProtocolError("OKX 广告标识无效")
            normalized = NormalizedAd(
                provider="okx",
                ad_id=str(raw_ad_id),
                fiat=str(_field(row, schema.fiat_fields) or request.fiat),
                asset=str(_field(row, schema.asset_fields) or request.asset),
                direction=_parse_okx_direction(
                    _field(row, schema.direction_fields), request, contract
                ),
                price=_field(row, schema.price_fields),
                min_fiat=_field(row, schema.min_fiat_fields),
                max_fiat=_field(row, schema.max_fiat_fields),
                available_asset=_field(row, schema.available_asset_fields),
                payment_methods=_payment_identifiers(_field(row, schema.payment_fields)),
                completion_rate=_optional_stat(
                    _field(row, schema.completion_rate_fields), maximum="100"
                ),
                completed_orders=_optional_stat(
                    _field(row, schema.completed_orders_fields), integer=True
                ),
                asset_step=_field(row, schema.asset_step_fields),
                fiat_step=_field(row, schema.fiat_step_fields),
            )
            normalized.effective_max_fiat
            parsed.append(normalized)
        except (C2CModelError, TypeError, ValueError, ProviderProtocolError):
            rejected += 1
    if target and rejected == len(target):
        raise ProviderProtocolError("OKX 广告响应全部未通过边界校验")
    return tuple(parsed)


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _credential(credentials: Mapping[str, str], name: str) -> str:
    value = credentials.get(name)
    if not isinstance(value, str) or not value or len(value) > 512 or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ProviderUnconfiguredError("OKX 凭据提供器未返回完整凭据")
    return value


class OkxP2PAdapter(BaseP2PAdapter):
    def __init__(
        self,
        config: OkxP2PConfig | None = None,
        *,
        credentials_provider: CredentialsProvider | None = None,
        client: ResilientJsonClient | None = None,
        transport: HttpTransport | None = None,
        timestamp_provider: Callable[[], str] = _timestamp_now,
    ) -> None:
        self.config = config or OkxP2PConfig()
        self._credentials_provider = credentials_provider
        self._client = client or ResilientJsonClient(transport=transport)
        self._timestamp_provider = timestamp_provider

    @property
    def capability(self) -> ProviderCapability:
        configured = bool(
            self.config.enabled
            and self.config.whitelisted
            and self.config.approved_request is not None
            and self._credentials_provider is not None
        )
        return ProviderCapability(
            provider="okx",
            enabled=self.config.enabled,
            configured=configured,
            requires_whitelist=True,
            quote_price=False,
            ad_list=self.config.approved_request is not None,
            trade_methods=False,
            note=(
                "仅支持已获 OKX 白名单批准并显式配置的商家 P2P 只读契约；"
                "未配置时不作为 auto 候选。"
            ),
        )

    def _unavailable_result(self, request: QuoteRequest) -> QuoteResult:
        has_access_configuration = bool(
            self.config.enabled
            and self.config.approved_request is not None
            and self._credentials_provider is not None
        )
        if has_access_configuration and not self.config.whitelisted:
            return self._error_result(
                request,
                QuoteStatus.PERMISSION_DENIED,
                "OKX P2P 需要官方白名单；当前未声明已获授权。",
            )
        return self._error_result(
            request,
            QuoteStatus.UNCONFIGURED,
            "OKX P2P 官方白名单访问条件或获批接口契约未配置。",
        )

    def _request_values(self, request: QuoteRequest) -> list[tuple[str, str]]:
        contract = self.config.approved_request
        if contract is None:
            raise ProviderUnconfiguredError("OKX 获批接口契约未配置")
        values = [
            (contract.asset_parameter, request.asset),
            (contract.fiat_parameter, request.fiat),
            (
                contract.direction_parameter,
                contract.buy_value if request.direction.value == "BUY" else contract.sell_value,
            ),
        ]
        if contract.limit_parameter is not None and contract.limit_value is not None:
            values.append((contract.limit_parameter, contract.limit_value))
        if request.payment_methods:
            if contract.payment_parameter is None:
                raise PaymentMethodUnavailableError("获批 OKX 契约未配置支付筛选参数")
            if contract.payment_mode == "single":
                if len(request.payment_methods) != 1:
                    raise PaymentMethodUnavailableError("获批 OKX 契约只允许单个支付标识")
                values.append((contract.payment_parameter, request.payment_methods[0]))
            elif contract.payment_mode == "comma":
                values.append((contract.payment_parameter, ",".join(request.payment_methods)))
            else:
                values.extend(
                    (contract.payment_parameter, identifier)
                    for identifier in request.payment_methods
                )
        return values

    def _signed_headers(
        self,
        method: str,
        request_path: str,
        body: bytes | None,
    ) -> dict[str, str]:
        if self._credentials_provider is None:
            raise ProviderUnconfiguredError("OKX 凭据提供器未配置")
        try:
            credentials = self._credentials_provider()
        except Exception as exc:
            raise ProviderUnconfiguredError("OKX 凭据提供器不可用") from exc
        if not isinstance(credentials, Mapping):
            raise ProviderUnconfiguredError("OKX 凭据提供器未返回凭据")
        api_key = _credential(credentials, "api_key")
        secret_key = _credential(credentials, "secret_key")
        passphrase = _credential(credentials, "passphrase")
        try:
            timestamp = str(self._timestamp_provider() or "")
        except Exception as exc:
            raise ProviderUnconfiguredError("OKX 时间戳提供器不可用") from exc
        if len(timestamp) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in timestamp):
            raise ProviderUnconfiguredError("OKX 时间戳提供器返回无效值")
        prehash = timestamp + method + request_path + (body.decode("utf-8") if body else "")
        signature = base64.b64encode(
            hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": APP_USER_AGENT,
            "OK-ACCESS-KEY": api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": passphrase,
        }

    def fetch_ads(
        self,
        request: QuoteRequest,
        *,
        cancel: CancellationToken | None = None,
    ) -> tuple[NormalizedAd, ...]:
        if not self.config.enabled:
            raise ProviderUnconfiguredError("OKX P2P 已禁用")
        if not self.config.whitelisted:
            raise ProviderPermissionError("OKX P2P 白名单权限未配置")
        contract = self.config.approved_request
        if contract is None:
            raise ProviderUnconfiguredError("OKX 获批接口契约未配置")
        values = self._request_values(request)
        params = values if contract.method == "GET" else None
        body = None
        request_path = contract.path
        if contract.method == "GET":
            query = urlencode(values)
            if query:
                request_path += "?" + query
        else:
            body_object: dict[str, object] = {}
            for key, value in values:
                if key in body_object:
                    existing = body_object[key]
                    if isinstance(existing, list):
                        existing.append(value)
                    else:
                        body_object[key] = [existing, value]
                else:
                    body_object[key] = value
            body = json.dumps(
                body_object,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        headers = self._signed_headers(contract.method, request_path, body)
        payload = self._client.request_json(
            contract.method,
            self.config.base_url + contract.path,
            params=params,
            headers=headers,
            body=body,
            # This configured operation is an ad-list read even when the
            # approved OKX contract uses POST, so one safe retry is permitted.
            idempotent=True,
            cancel=cancel,
        )
        return parse_okx_ads(
            payload,
            request,
            schema=self.config.response_schema,
            contract=contract,
        )

    ad_list = fetch_ads


OKXAdapter = OkxP2PAdapter
OKXConfig = OkxP2PConfig


__all__ = [
    "CredentialsProvider",
    "OKXAdapter",
    "OKXConfig",
    "OKX_DEFAULT_BASE_URL",
    "OkxApprovedRequest",
    "OkxP2PAdapter",
    "OkxP2PConfig",
    "OkxResponseSchema",
    "parse_okx_ads",
]
