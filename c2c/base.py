"""Shared C2C matching, transport, throttling, and circuit-breaker primitives."""

from __future__ import annotations

import json
import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, ROUND_FLOOR, localcontext
from email.utils import parsedate_to_datetime
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, Sequence

import requests
from urllib3.util import Timeout

from conversion_core import DecimalBoundaryError, decimal_to_canonical, parse_amount

from .models import (
    AmountRange,
    C2CModelError,
    DataState,
    Direction,
    MatchResult,
    NormalizedAd,
    PaymentMethod,
    ProviderCapability,
    QuoteRequest,
    QuoteResult,
    QuoteStatus,
    RangeError,
)


MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.5
DEFAULT_READ_TIMEOUT_SECONDS = 5.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 6.0
QUALIFICATION_WARNING = "KYC、地区、账户年龄等资格条件可能未知，报价不承诺必然可成交。"


class ProviderError(RuntimeError):
    code = "provider_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or type(self).code


class ProviderUnconfiguredError(ProviderError):
    code = "unconfigured"


class ProviderPermissionError(ProviderError):
    code = "permission_denied"


class ProviderRateLimitError(ProviderError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(0, retry_after_seconds)


class CircuitOpenError(ProviderError):
    code = "circuit_open"

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(0, retry_after_seconds)


class ProviderUnavailableError(ProviderError):
    code = "unavailable"


class ProviderProtocolError(ProviderError):
    code = "invalid_response"


class ResponseTooLargeError(ProviderProtocolError):
    code = "response_too_large"


class RequestCancelledError(ProviderError):
    code = "cancelled"


class PaymentMethodUnavailableError(ProviderError):
    code = "payment_method_unavailable"


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | Sequence[tuple[str, object]] | None,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        connect_timeout: float,
        read_timeout: float,
        total_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class RequestsTransport:
    """Bounded streaming transport used by production adapters."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session_lock = RLock()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | Sequence[tuple[str, object]] | None,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        connect_timeout: float,
        read_timeout: float,
        total_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        started = time.monotonic()
        timeout = Timeout(
            total=total_timeout,
            connect=min(connect_timeout, total_timeout),
            read=min(read_timeout, total_timeout),
        )
        # ``requests.Session`` mutates cookie/pool state while preparing a
        # request.  Keep that short phase serialized; response streaming can
        # proceed independently on urllib3's thread-safe connection pools.
        with self._session_lock:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                headers=dict(headers or {}),
                data=body,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
        try:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except (TypeError, ValueError) as exc:
                    raise ProviderProtocolError("平台响应长度标头无效") from exc
                if declared_size < 0 or declared_size > max_response_bytes:
                    raise ResponseTooLargeError("平台响应超过 1MiB 上限")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() - started > total_timeout:
                    raise requests.Timeout("C2C request exceeded total deadline")
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_response_bytes:
                    raise ResponseTooLargeError("平台响应超过 1MiB 上限")
                chunks.append(chunk)
            return HttpResponse(
                status_code=int(response.status_code),
                headers={str(key): str(value) for key, value in response.headers.items()},
                body=b"".join(chunks),
            )
        finally:
            response.close()


class TokenBucket:
    """Thread-safe 1 request/second limiter with a burst capacity of two."""

    def __init__(
        self,
        *,
        rate_per_second: float = 1.0,
        capacity: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0 or capacity < 1:
            raise ValueError("令牌桶速率和容量必须为正数")
        self._rate = rate_per_second
        self._capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleeper
        self._updated_at = clock()
        self._lock = RLock()

    def acquire(self, deadline: float, cancel: CancellationToken | None = None) -> None:
        while True:
            if cancel is not None and cancel.is_set():
                raise RequestCancelledError("C2C 请求已取消")
            with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated_at = now
                # Monotonic clocks and token arithmetic are binary floats (not
                # monetary values).  Treat sub-nanosecond residue as a full
                # token so a coarse/fake clock cannot spin forever at 0.999… .
                if self._tokens >= 1.0 - 1e-9:
                    self._tokens = max(0.0, self._tokens - 1.0)
                    return
                wait_for = (1.0 - self._tokens) / self._rate
            remaining = deadline - self._clock()
            if remaining <= 0 or wait_for > remaining:
                raise ProviderUnavailableError("C2C 请求在本地限速等待时超过总截止时间")
            self._sleep(min(wait_for, remaining, 0.05))


class CircuitBreaker:
    """Per-provider circuit breaker with one half-open probe."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        open_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or open_seconds < 1:
            raise ValueError("熔断器配置必须为正数")
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._half_open_inflight = False
        self._lock = RLock()

    @property
    def state(self) -> str:
        with self._lock:
            now = self._clock()
            if self._open_until > now:
                return "open"
            if self._open_until and self._half_open_inflight:
                return "half_open"
            return "closed"

    def before_request(self) -> None:
        with self._lock:
            now = self._clock()
            if self._open_until > now:
                raise CircuitOpenError(
                    "平台熔断保护中",
                    max(1, math.ceil(self._open_until - now)),
                )
            if self._open_until:
                if self._half_open_inflight:
                    raise CircuitOpenError("平台正在半开探测", 1)
                self._half_open_inflight = True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._half_open_inflight = False

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._failures += 1
            if self._half_open_inflight or self._failures >= self._failure_threshold:
                self._open_until = now + self._open_seconds
                self._half_open_inflight = False

    def record_rate_limit(self, retry_after_seconds: int) -> None:
        with self._lock:
            self._failures = self._failure_threshold
            self._open_until = self._clock() + max(1, retry_after_seconds)
            self._half_open_inflight = False


def _retry_after_seconds(value: object, now: datetime | None = None) -> int:
    text = str(value or "").strip()
    if text:
        try:
            seconds = int(text)
            if 0 <= seconds <= 3600:
                return seconds
        except ValueError:
            try:
                stamp = parsedate_to_datetime(text)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                delta = (stamp - (now or datetime.now(timezone.utc))).total_seconds()
                if 0 <= delta <= 3600:
                    return math.ceil(delta)
            except (TypeError, ValueError, OverflowError):
                pass
    return 60


def _strict_json(body: bytes) -> Any:
    if not body:
        raise ProviderProtocolError("平台返回空响应")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        text = body.decode("utf-8", errors="strict")
        return json.loads(
            text,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProviderProtocolError("平台返回的 JSON 无效") from exc


class ResilientJsonClient:
    """Idempotent JSON client with deadline, retry, rate limit, and breaker."""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        limiter: TokenBucket | None = None,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport or RequestsTransport()
        self._clock = clock
        self._sleep = sleeper
        self._jitter = jitter or (lambda: random.uniform(0.150, 0.400))
        self._connect_timeout = min(3.0, max(2.0, connect_timeout))
        self._read_timeout = min(5.0, max(0.1, read_timeout))
        self._total_timeout = min(6.0, max(0.1, total_timeout))
        self.limiter = limiter or TokenBucket(clock=clock, sleeper=sleeper)
        self.breaker = breaker or CircuitBreaker(clock=clock)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | Sequence[tuple[str, object]] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        idempotent: bool | None = None,
        cancel: CancellationToken | None = None,
    ) -> Any:
        method = str(method).strip().upper()
        if method not in {"GET", "HEAD", "OPTIONS", "POST"}:
            raise ProviderProtocolError("不支持的 HTTP 方法")
        if idempotent is None:
            idempotent = method in {"GET", "HEAD", "OPTIONS"}
        elif not isinstance(idempotent, bool):
            raise ProviderProtocolError("幂等请求标志无效")
        deadline = self._clock() + self._total_timeout
        attempts = 2 if idempotent else 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            if cancel is not None and cancel.is_set():
                raise RequestCancelledError("C2C 请求已取消")
            self.limiter.acquire(deadline, cancel)
            # Re-check the breaker after any limiter wait so a concurrent 429
            # cannot slip another network call through an already-open circuit.
            self.breaker.before_request()
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ProviderUnavailableError("C2C 请求超过 6 秒总截止时间")
            try:
                response = self._transport.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    body=body,
                    connect_timeout=min(self._connect_timeout, remaining),
                    read_timeout=min(self._read_timeout, remaining),
                    total_timeout=remaining,
                    max_response_bytes=MAX_HTTP_RESPONSE_BYTES,
                )
            except RequestCancelledError:
                raise
            except (ResponseTooLargeError, ProviderProtocolError):
                self.breaker.record_failure()
                raise
            except (requests.ConnectionError, requests.Timeout, OSError) as exc:
                last_error = exc
                self.breaker.record_failure()
                if attempt + 1 < attempts:
                    self._retry_pause(deadline, cancel)
                    continue
                raise ProviderUnavailableError("C2C 平台连接失败或超时") from exc

            status = response.status_code
            header_map = {str(key).lower(): str(value) for key, value in response.headers.items()}
            if not isinstance(response.body, bytes):
                self.breaker.record_failure()
                raise ProviderProtocolError("平台响应正文类型无效")
            declared = header_map.get("content-length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    self.breaker.record_failure()
                    raise ProviderProtocolError("平台响应长度标头无效") from exc
                if declared_size < 0 or declared_size > MAX_HTTP_RESPONSE_BYTES:
                    self.breaker.record_failure()
                    raise ResponseTooLargeError("平台响应超过 1MiB 上限")
            if len(response.body) > MAX_HTTP_RESPONSE_BYTES:
                self.breaker.record_failure()
                raise ResponseTooLargeError("平台响应超过 1MiB 上限")
            if status == 429:
                retry_after = _retry_after_seconds(header_map.get("retry-after"))
                self.breaker.record_rate_limit(retry_after)
                raise ProviderRateLimitError("C2C 平台请求过于频繁", retry_after)
            if status in {401, 403}:
                self.breaker.record_success()
                raise ProviderPermissionError("C2C 平台拒绝访问或未授予权限")
            if status in {502, 503, 504}:
                last_error = ProviderUnavailableError(f"C2C 平台暂时不可用（HTTP {status}）")
                self.breaker.record_failure()
                if attempt + 1 < attempts:
                    self._retry_pause(deadline, cancel)
                    continue
                raise last_error
            if status < 200 or status >= 300:
                self.breaker.record_failure()
                raise ProviderUnavailableError(f"C2C 平台返回 HTTP {status}")
            try:
                payload = _strict_json(response.body)
            except ProviderProtocolError:
                self.breaker.record_failure()
                raise
            self.breaker.record_success()
            return payload
        raise ProviderUnavailableError("C2C 平台请求失败") from last_error

    def _retry_pause(self, deadline: float, cancel: CancellationToken | None) -> None:
        delay = min(0.400, max(0.150, float(self._jitter())))
        while delay > 0:
            if cancel is not None and cancel.is_set():
                raise RequestCancelledError("C2C 请求已取消")
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ProviderUnavailableError("C2C 请求超过 6 秒总截止时间")
            interval = min(delay, remaining, 0.05)
            self._sleep(interval)
            delay -= interval


def _decimal(value: str) -> Decimal:
    return parse_amount(value)


def _precision(*values: Decimal) -> int:
    return max(440, sum(max(1, len(item.as_tuple().digits)) for item in values) + 32)


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    try:
        with localcontext() as context:
            context.prec = _precision(left, right)
            result = left * right
        decimal_to_canonical(result)
        return result
    except (DecimalException, DecimalBoundaryError, OverflowError) as exc:
        raise ProviderProtocolError("广告数值计算超出安全范围") from exc


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    try:
        with localcontext() as context:
            context.prec = _precision(value, step)
            units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
            result = units * step
        decimal_to_canonical(result)
        return result
    except (DecimalException, DecimalBoundaryError, OverflowError) as exc:
        raise ProviderProtocolError("广告精度计算超出安全范围") from exc


def _divide_to_step(numerator: Decimal, denominator: Decimal, step: Decimal) -> Decimal:
    """Return ``floor(numerator / denominator, step)`` without a rounded interim."""

    try:
        with localcontext() as context:
            context.prec = max(440, _precision(denominator, step))
            scaled_denominator = denominator * step
        if scaled_denominator <= 0:
            raise ProviderProtocolError("广告除数或步长无效")
        integer_digits = max(1, numerator.adjusted() - scaled_denominator.adjusted() + 2)
        with localcontext() as context:
            context.prec = max(
                440,
                integer_digits
                + len(numerator.as_tuple().digits)
                + len(scaled_denominator.as_tuple().digits)
                + 32,
            )
            units = (numerator / scaled_denominator).to_integral_value(rounding=ROUND_FLOOR)
            result = units * step
        decimal_to_canonical(result)
        return result
    except ProviderProtocolError:
        raise
    except (DecimalException, DecimalBoundaryError, OverflowError) as exc:
        raise ProviderProtocolError("广告精度计算超出安全范围") from exc


def _ad_sort_key(ad: NormalizedAd, direction: Direction) -> tuple[Any, ...]:
    price = _decimal(ad.price)
    completion = None if ad.completion_rate is None else _decimal(ad.completion_rate)
    orders = None if ad.completed_orders is None else _decimal(ad.completed_orders)
    inventory = _decimal(ad.available_asset)
    return (
        price if direction is Direction.BUY else -price,
        completion is None,
        Decimal(0) if completion is None else -completion,
        orders is None,
        Decimal(0) if orders is None else -orders,
        -inventory,
        ad.ad_id,
    )


def eligible_ads(request: QuoteRequest, ads: Sequence[NormalizedAd]) -> tuple[NormalizedAd, ...]:
    requested_methods = set(request.payment_methods)
    valid: list[NormalizedAd] = []
    for ad in ads:
        if (
            ad.fiat != request.fiat
            or ad.asset != request.asset
            or ad.direction is not request.direction
            or (requested_methods and not requested_methods.issubset(set(ad.payment_methods)))
        ):
            continue
        try:
            if ad.valid_range:
                valid.append(ad)
        except (DecimalBoundaryError, ProviderProtocolError, ValueError):
            continue
    return tuple(sorted(valid, key=lambda item: _ad_sort_key(item, request.direction)))


def match_ad(request: QuoteRequest, ad: NormalizedAd) -> MatchResult | None:
    """Apply the frozen single-ad amount contract to one normalized ad."""

    lower = _decimal(ad.min_fiat)
    upper = _decimal(ad.effective_max_fiat)
    price = _decimal(ad.price)
    available = _decimal(ad.available_asset)
    amount = _decimal(request.amount)
    asset_step = _decimal(request.asset_step or ad.asset_step or request.resolved_asset_step)
    fiat_step = _decimal(request.fiat_step or ad.fiat_step or request.resolved_fiat_step)
    warnings: tuple[str, ...] = ()
    if ad.unknown_qualifications:
        warnings = (QUALIFICATION_WARNING,)

    if request.direction is Direction.BUY:
        if amount < lower or amount > upper:
            return None
        crypto = _divide_to_step(amount, price, asset_step)
        if crypto <= 0 or crypto > available:
            return None
        actual_fiat = _multiply(crypto, price)
        if actual_fiat > amount:
            return None
        with localcontext() as context:
            context.prec = _precision(amount, actual_fiat)
            remainder = amount - actual_fiat
        try:
            return MatchResult(
                provider=ad.provider,
                ad=ad,
                input_amount=request.amount,
                input_unit=request.fiat,
                output_amount=decimal_to_canonical(crypto),
                output_unit=request.asset,
                price=ad.price,
                actual_fiat=decimal_to_canonical(actual_fiat),
                actual_crypto=decimal_to_canonical(crypto),
                remainder=decimal_to_canonical(remainder),
                warnings=warnings,
            )
        except (C2CModelError, DecimalBoundaryError) as exc:
            raise ProviderProtocolError("C2C 匹配结果超出安全十进制边界") from exc

    if amount > available:
        return None
    gross_fiat = _multiply(amount, price)
    fiat_result = _floor_to_step(gross_fiat, fiat_step)
    if fiat_result < lower or fiat_result > upper:
        return None
    try:
        return MatchResult(
            provider=ad.provider,
            ad=ad,
            input_amount=request.amount,
            input_unit=request.asset,
            output_amount=decimal_to_canonical(fiat_result),
            output_unit=request.fiat,
            price=ad.price,
            actual_fiat=decimal_to_canonical(fiat_result),
            actual_crypto=request.amount,
            remainder="0",
            warnings=warnings,
        )
    except (C2CModelError, DecimalBoundaryError) as exc:
        raise ProviderProtocolError("C2C 匹配结果超出安全十进制边界") from exc


def _ranges(ads: Sequence[NormalizedAd]) -> tuple[AmountRange, ...]:
    ranges: list[AmountRange] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for ad in ads:
        upper = ad.effective_max_fiat
        key = (ad.min_fiat, upper, ad.provider, ad.payment_methods)
        if key in seen:
            continue
        seen.add(key)
        ranges.append(AmountRange(
            provider=ad.provider,
            ad_id=ad.ad_id,
            unit=ad.fiat,
            lower=ad.min_fiat,
            upper=upper,
            payment_methods=ad.payment_methods,
        ))
    return tuple(ranges)


def build_quote_result(
    request: QuoteRequest,
    provider: str,
    ads: Sequence[NormalizedAd],
) -> QuoteResult:
    ranked = eligible_ads(request, ads)
    market_best = ranked[0].price if ranked else None
    for ad in ranked:
        match = match_ad(request, ad)
        if match is not None:
            return QuoteResult(
                provider=provider,
                status=QuoteStatus.OK,
                data_state=DataState.LIVE,
                fiat=request.fiat,
                asset=request.asset,
                direction=request.direction,
                input_amount=request.amount,
                market_best_price=market_best,
                market_best_provider=provider if market_best is not None else None,
                indicative_output_amount=match.output_amount,
                output_unit=match.output_unit,
                match=match,
                ads_considered=len(ranked),
                compared_providers=(provider,),
                warnings=match.warnings,
            )
    ranges = _ranges(ranked)
    warning = (
        "存在广告，但本金额未命中任何单广告范围；展示最优价未被冒充为可成交价。"
        if ranked else "未找到符合币对、方向及支付筛选的有效广告。"
    )
    return QuoteResult(
        provider=provider,
        status=QuoteStatus.NO_MATCH,
        data_state=DataState.LIVE,
        fiat=request.fiat,
        asset=request.asset,
        direction=request.direction,
        input_amount=request.amount,
        market_best_price=market_best,
        market_best_provider=provider if market_best is not None else None,
        range_error=RangeError(
            requested_amount=request.amount,
            requested_unit=request.amount_unit,
            ranges=ranges,
        ),
        ads_considered=len(ranked),
        compared_providers=(provider,),
        warnings=(warning,),
    )


class BaseP2PAdapter(ABC):
    """Read-only provider adapter contract."""

    @property
    @abstractmethod
    def capability(self) -> ProviderCapability: ...

    @abstractmethod
    def fetch_ads(
        self,
        request: QuoteRequest,
        *,
        cancel: CancellationToken | None = None,
    ) -> tuple[NormalizedAd, ...]: ...

    def quote(
        self,
        request: QuoteRequest,
        *,
        cancel: CancellationToken | None = None,
    ) -> QuoteResult:
        capability = self.capability
        if not capability.enabled or not capability.configured:
            return self._unavailable_result(request)
        try:
            return build_quote_result(request, capability.provider, self.fetch_ads(request, cancel=cancel))
        except PaymentMethodUnavailableError:
            return QuoteResult(
                provider=capability.provider,
                status=QuoteStatus.NO_MATCH,
                data_state=DataState.LIVE,
                fiat=request.fiat,
                asset=request.asset,
                direction=request.direction,
                input_amount=request.amount,
                compared_providers=(capability.provider,),
                range_error=RangeError(request.amount, request.amount_unit, ()),
                warnings=("所选支付方式不是该法币由平台当前返回的有效标识。",),
            )
        except RequestCancelledError:
            return self._error_result(request, QuoteStatus.CANCELLED, "C2C 请求已取消。")
        except ProviderUnconfiguredError:
            return self._error_result(
                request, QuoteStatus.UNCONFIGURED, "平台未配置 C2C 官方访问条件。"
            )
        except ProviderRateLimitError as exc:
            return self._error_result(
                request,
                QuoteStatus.RATE_LIMITED,
                "平台限流，已启动本地熔断保护。",
                retry_after_seconds=exc.retry_after_seconds,
            )
        except CircuitOpenError as exc:
            return self._error_result(
                request,
                QuoteStatus.CIRCUIT_OPEN,
                "平台熔断保护中，稍后将进行半开探测。",
                retry_after_seconds=exc.retry_after_seconds,
            )
        except ProviderPermissionError:
            return self._error_result(
                request, QuoteStatus.PERMISSION_DENIED, "平台未授权当前 P2P 只读访问。"
            )
        except ProviderProtocolError:
            return self._error_result(
                request, QuoteStatus.INVALID_RESPONSE, "平台响应未通过 C2C 安全边界校验。"
            )
        except (ProviderUnavailableError, requests.RequestException, OSError):
            return self._error_result(
                request, QuoteStatus.UNAVAILABLE, "平台暂时不可用，未生成 C2C 可成交价。"
            )

    def _unavailable_result(self, request: QuoteRequest) -> QuoteResult:
        return self._error_result(
            request, QuoteStatus.UNCONFIGURED, "平台未配置 C2C 官方访问条件。"
        )

    def _error_result(
        self,
        request: QuoteRequest,
        status: QuoteStatus,
        warning: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> QuoteResult:
        return QuoteResult(
            provider=self.capability.provider,
            status=status,
            data_state=DataState.NONE,
            fiat=request.fiat,
            asset=request.asset,
            direction=request.direction,
            input_amount=request.amount,
            compared_providers=(self.capability.provider,),
            warnings=(warning,),
            retry_after_seconds=retry_after_seconds,
        )

    def list_trade_methods(
        self,
        fiat: str,
        *,
        cancel: CancellationToken | None = None,
    ) -> tuple[PaymentMethod, ...]:
        raise ProviderUnconfiguredError("该平台未提供公开支付方式查询")


__all__ = [
    "BaseP2PAdapter",
    "CancellationToken",
    "CircuitBreaker",
    "CircuitOpenError",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_TOTAL_TIMEOUT_SECONDS",
    "HttpResponse",
    "HttpTransport",
    "MAX_HTTP_RESPONSE_BYTES",
    "PaymentMethodUnavailableError",
    "ProviderError",
    "ProviderPermissionError",
    "ProviderProtocolError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "ProviderUnconfiguredError",
    "QUALIFICATION_WARNING",
    "RequestCancelledError",
    "RequestsTransport",
    "ResilientJsonClient",
    "ResponseTooLargeError",
    "TokenBucket",
    "build_quote_result",
    "eligible_ads",
    "match_ad",
]
