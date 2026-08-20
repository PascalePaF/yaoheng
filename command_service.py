"""Thread-safe command parsing and service orchestration for local clients.

The parser is deliberately independent of any chat platform SDK.  Monetary
values cross this boundary as canonical decimal strings and all collaborators
are injected, which keeps the default service offline.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Mapping, Protocol

from calculator_core import CalculationError, SafeEvaluator, format_number
from c2c.models import Direction, QuoteRequest, QuoteStatus
from conversion_core import (
    ConversionError,
    canonical_amount_string,
    canonical_rate_string,
    currency_metadata,
    normalize_currency_code,
    parse_amount,
)


MAX_COMMAND_CHARS = 1024
MAX_EXPRESSION_CHARS = 512
C2C_NON_GUARANTEE_WARNING = "C2C 报价仅供筛选，不保证广告仍可用、账户符合资格或最终成交。"
MARKET_FALLBACK_WARNING = "非 C2C 可成交价：普通行情仅供参考，不代表任何单广告可成交。"

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_PAYMENT_RE = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_UNSAFE_TEXT_RE = re.compile(r"(?:[;&|`$<>\\'\"{}\[\]]|__|/\*|\*/|<!--|-->)")


class ExactConversionService(Protocol):
    def convert_exact(self, amount: object, source: str, target: str) -> str: ...


class QuoteService(Protocol):
    def quote(self, request: QuoteRequest, *, cancel: object | None = None) -> object: ...

    def capabilities(self) -> object: ...


class CalculationService(Protocol):
    def calculate(self, expression: str) -> object: ...


class CommandError(ValueError):
    """A stable, input-redacted command failure."""

    code = "invalid_command"

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code or type(self).code
        self.retryable = bool(retryable)


@dataclass(frozen=True, slots=True)
class CommandErrorData:
    code: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    kind: str
    expression: str = ""
    amount: str = ""
    source: str = ""
    target: str = ""
    mode: str = "market"
    provider: str = "auto"
    payment_method: str = ""


@dataclass(frozen=True, slots=True)
class CommandResult:
    status: str
    kind: str
    source: str
    data: Mapping[str, object]
    warnings: tuple[str, ...] = ()
    error: CommandErrorData | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.error is None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "kind": self.kind,
            "source": self.source,
            "data": dict(self.data),
            "warnings": list(self.warnings),
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload


def _failure(error: CommandError, *, kind: str = "command") -> CommandResult:
    return CommandResult(
        status="error",
        kind=kind,
        source="command_service",
        data={},
        error=CommandErrorData(error.code, str(error), error.retryable),
    )


def _validate_text(text: object, *, limit: int, empty_code: str) -> str:
    if not isinstance(text, str):
        raise CommandError("命令必须是文本", code="invalid_type")
    if not text.strip():
        raise CommandError("命令不能为空", code=empty_code)
    if len(text) > limit:
        raise CommandError("命令过长", code="command_too_long")
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            raise CommandError("命令包含控制或不可见字符", code="control_character")
    if _UNSAFE_TEXT_RE.search(text):
        raise CommandError("命令包含不安全文本", code="unsafe_text")
    return text.strip()


def _amount(value: object, *, positive: bool) -> str:
    if isinstance(value, (bool, float)):
        raise CommandError("金额必须使用十进制文本", code="invalid_amount")
    try:
        canonical = canonical_amount_string(value)
        parsed = parse_amount(canonical)
    except (ConversionError, ValueError, TypeError) as exc:
        raise CommandError("金额格式无效", code="invalid_amount") from exc
    if parsed < 0 or (positive and parsed <= 0):
        message = "金额必须大于零" if positive else "金额不能为负数"
        raise CommandError(message, code="invalid_amount")
    return canonical


def _currency(value: object) -> str:
    try:
        return normalize_currency_code(value)
    except ConversionError as exc:
        raise CommandError("币种代码无效", code="invalid_currency") from exc


def _parse_options(tokens: list[str], allowed: set[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        option = tokens[index]
        if not option.startswith("--") or "=" in option or len(option) <= 2:
            raise CommandError("命令参数语法无效", code="invalid_syntax")
        name = option[2:].lower()
        if name not in allowed:
            raise CommandError("存在未知参数", code="unknown_parameter")
        if name in options:
            raise CommandError("存在重复参数", code="duplicate_parameter")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            raise CommandError("命令参数缺少值", code="missing_parameter_value")
        options[name] = tokens[index + 1]
        index += 2
    return options


def _normalize_provider(value: str) -> str:
    provider = value.strip().lower()
    if provider != "auto" and _PROVIDER_RE.fullmatch(provider) is None:
        raise CommandError("C2C 平台标识无效", code="invalid_provider")
    return provider


def _normalize_payment(value: str) -> str:
    payment = value.strip()
    if _PAYMENT_RE.fullmatch(payment) is None:
        raise CommandError("支付方式标识无效", code="invalid_payment_method")
    return payment


def _parse_conversion(
    base_tokens: list[str],
    option_tokens: list[str],
    *,
    chinese: bool,
) -> ParsedCommand:
    if len(base_tokens) != 3:
        raise CommandError("换算命令语法无效", code="invalid_syntax")
    amount = _amount(base_tokens[0], positive=chinese is False)
    source = _currency(base_tokens[1])
    target = _currency(base_tokens[2])
    if source == target:
        raise CommandError("源币种与目标币种不能相同", code="invalid_pair")
    allowed = {"mode", "provider", "pay"} if chinese else {"provider", "pay"}
    options = _parse_options(option_tokens, allowed)
    mode = options.get("mode", "market" if chinese else "c2c").strip().lower()
    if mode in {"fx", "normal"}:
        mode = "market"
    if mode not in {"market", "c2c"}:
        raise CommandError("换算模式无效", code="invalid_mode")
    if mode == "market" and ("provider" in options or "pay" in options):
        raise CommandError("普通换算不接受 C2C 参数", code="invalid_parameter_combination")
    if mode == "c2c" and parse_amount(amount) <= 0:
        raise CommandError("C2C 金额必须大于零", code="invalid_amount")
    provider = _normalize_provider(options.get("provider", "auto"))
    payment = _normalize_payment(options["pay"]) if "pay" in options else ""
    return ParsedCommand(
        kind="convert" if mode == "market" else "c2c",
        amount=amount,
        source=source,
        target=target,
        mode=mode,
        provider=provider,
        payment_method=payment,
    )


def parse_command(command: object) -> ParsedCommand:
    """Parse a supported command without accessing I/O or shared state."""

    text = _validate_text(command, limit=MAX_COMMAND_CHARS, empty_code="empty_command")
    tokens = text.split()
    head = tokens[0].lower()
    if head == "/calc":
        expression = text[len(tokens[0]):].strip()
        if not expression:
            raise CommandError("计算表达式不能为空", code="invalid_syntax")
        if len(expression) > MAX_EXPRESSION_CHARS:
            raise CommandError("计算表达式过长", code="command_too_long")
        return ParsedCommand(kind="calculate", expression=expression)
    if head == "/fx":
        if len(tokens) != 4:
            raise CommandError("换算命令语法无效", code="invalid_syntax")
        return ParsedCommand(
            kind="convert",
            amount=_amount(tokens[1], positive=False),
            source=_currency(tokens[2]),
            target=_currency(tokens[3]),
        )
    if head == "/c2c":
        if len(tokens) < 4:
            raise CommandError("C2C 命令语法无效", code="invalid_syntax")
        return _parse_conversion(tokens[1:4], tokens[4:], chinese=False)
    if tokens[0] == "兑换":
        if len(tokens) < 4:
            raise CommandError("换算命令语法无效", code="invalid_syntax")
        return _parse_conversion(tokens[1:4], tokens[4:], chinese=True)
    raise CommandError("未知命令", code="unknown_command")


def _dedupe_warnings(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raw = (values,)
    else:
        try:
            raw = tuple(values)  # type: ignore[arg-type]
        except TypeError:
            raw = ()
    clean: list[str] = []
    for value in raw:
        text = str(value)
        if text and len(text) <= 500 and not any(unicodedata.category(ch) == "Cc" for ch in text):
            if text not in clean:
                clean.append(text)
    return tuple(clean)


def _quote_to_dict(value: object) -> dict[str, object]:
    if hasattr(value, "to_dict"):
        payload = value.to_dict()  # type: ignore[union-attr]
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise CommandError("C2C 服务返回无效结果", code="service_error", retryable=True)
    if not isinstance(payload, dict):
        raise CommandError("C2C 服务返回无效结果", code="service_error", retryable=True)
    allowed = {
        "provider", "status", "data_state", "fiat", "asset", "direction",
        "input_amount", "market_best_price", "market_best_provider",
        "indicative_price", "indicative_output_amount", "output_unit", "match",
        "range_error", "ads_considered", "compared_providers", "warnings",
        "retry_after_seconds", "request_id", "generation",
    }
    filtered = {key: payload[key] for key in allowed if key in payload}

    def filter_mapping(raw: object, fields: set[str]) -> dict[str, object] | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise CommandError("C2C 服务返回无效结果", code="service_error", retryable=True)
        return {key: raw[key] for key in fields if key in raw}

    match_fields = {
        "provider", "ad", "input_amount", "input_unit", "output_amount", "output_unit",
        "price", "actual_fiat", "actual_crypto", "remainder", "warnings",
    }
    ad_fields = {
        "provider", "ad_id", "fiat", "asset", "direction", "price", "min_fiat",
        "available_asset", "max_fiat", "payment_methods", "completion_rate",
        "completed_orders", "asset_step", "fiat_step", "unknown_qualifications",
    }
    range_error_fields = {"requested_amount", "requested_unit", "ranges", "code"}
    range_fields = {"provider", "ad_id", "unit", "lower", "upper", "payment_methods"}

    match = filter_mapping(filtered.get("match"), match_fields)
    if match is not None:
        match["ad"] = filter_mapping(match.get("ad"), ad_fields)
        match["warnings"] = list(_dedupe_warnings(match.get("warnings")))
        filtered["match"] = match
    elif "match" in filtered:
        filtered["match"] = None
    range_error = filter_mapping(filtered.get("range_error"), range_error_fields)
    if range_error is not None:
        raw_ranges = range_error.get("ranges", ())
        if not isinstance(raw_ranges, (list, tuple)):
            raise CommandError("C2C 服务返回无效结果", code="service_error", retryable=True)
        range_error["ranges"] = [
            filter_mapping(item, range_fields) for item in raw_ranges
        ]
        filtered["range_error"] = range_error
    elif "range_error" in filtered:
        filtered["range_error"] = None

    def canonicalize(mapping: dict[str, object], keys: set[str], *, rates: bool = False) -> None:
        formatter = canonical_rate_string if rates else canonical_amount_string
        for key in keys:
            if key in mapping and mapping[key] is not None:
                if isinstance(mapping[key], (bool, float)):
                    raise CommandError(
                        "C2C 服务返回了非字符串金额",
                        code="service_error",
                        retryable=True,
                    )
                mapping[key] = formatter(mapping[key])

    canonicalize(filtered, {"input_amount", "indicative_output_amount"})
    canonicalize(filtered, {"market_best_price", "indicative_price"}, rates=True)
    if match is not None:
        canonicalize(
            match,
            {"input_amount", "output_amount", "actual_fiat", "actual_crypto", "remainder"},
        )
        canonicalize(match, {"price"}, rates=True)
        ad = match.get("ad")
        if isinstance(ad, dict):
            canonicalize(
                ad,
                {
                    "min_fiat", "available_asset", "max_fiat", "completion_rate",
                    "completed_orders", "asset_step", "fiat_step",
                },
            )
            canonicalize(ad, {"price"}, rates=True)
    if range_error is not None:
        canonicalize(range_error, {"requested_amount"})
        for item in range_error.get("ranges", []):
            if isinstance(item, dict):
                canonicalize(item, {"lower", "upper"})
    filtered["warnings"] = list(_dedupe_warnings(filtered.get("warnings")))
    return filtered


class CommandService:
    """Execute parsed commands against explicitly injected services."""

    __slots__ = ("_calculator", "_conversion", "_c2c")

    def __init__(
        self,
        conversion_service: ExactConversionService | None = None,
        c2c_service: QuoteService | None = None,
        *,
        calculator_service: CalculationService | Callable[[str], object] | None = None,
        calculator: object | None = None,
    ) -> None:
        if calculator_service is not None and calculator is not None:
            raise ValueError("计算服务只能注入一次")
        self._calculator = calculator_service if calculator_service is not None else calculator
        self._conversion = conversion_service
        self._c2c = c2c_service

    def _calculate(self, expression: str) -> str:
        if self._calculator is None:
            return format_number(SafeEvaluator().evaluate(expression))
        if callable(self._calculator):
            value = self._calculator(expression)
        elif callable(getattr(self._calculator, "calculate", None)):
            value = self._calculator.calculate(expression)
        elif callable(getattr(self._calculator, "evaluate", None)):
            value = self._calculator.evaluate(expression)
        else:
            raise CalculationError("计算服务接口无效")
        if isinstance(value, bool):
            raise CalculationError("计算服务返回值无效")
        if isinstance(value, (int, float)):
            return format_number(value)
        if isinstance(value, str):
            rendered = value.strip()
            if (
                not rendered
                or len(rendered) > 4096
                or any(unicodedata.category(character) in {"Cc", "Cs"} for character in rendered)
            ):
                raise CalculationError("计算服务返回值无效")
            return rendered
        raise CalculationError("计算服务返回值无效")

    def calculate(self, expression: object) -> CommandResult:
        try:
            text = _validate_text(
                expression,
                limit=MAX_EXPRESSION_CHARS,
                empty_code="empty_expression",
            )
            rendered = self._calculate(text)
            return CommandResult(
                status="ok",
                kind="calculate",
                source="calculator_core",
                data={"result": rendered},
            )
        except CommandError as exc:
            return _failure(exc, kind="calculate")
        except CalculationError:
            return _failure(
                CommandError("计算表达式无效", code="invalid_expression"),
                kind="calculate",
            )
        except Exception:
            return _failure(
                CommandError("计算服务暂不可用", code="service_error", retryable=True),
                kind="calculate",
            )

    def convert(self, amount: object, source: object, target: object) -> CommandResult:
        try:
            amount_text = _amount(amount, positive=False)
            source_code = _currency(source)
            target_code = _currency(target)
            if source_code == target_code:
                raise CommandError("源币种与目标币种不能相同", code="invalid_pair")
            if self._conversion is None:
                raise CommandError("普通换算服务未配置", code="service_unavailable", retryable=True)
            result = self._conversion.convert_exact(amount_text, source_code, target_code)
            value = canonical_amount_string(result)
            return CommandResult(
                status="ok",
                kind="convert",
                source="conversion_service",
                data={
                    "amount": amount_text,
                    "source": source_code,
                    "target": target_code,
                    "value": value,
                },
            )
        except CommandError as exc:
            return _failure(exc, kind="convert")
        except ConversionError as exc:
            code = getattr(exc, "code", "conversion_error")
            return _failure(CommandError("无法完成精确换算", code=code), kind="convert")
        except Exception:
            return _failure(
                CommandError("普通换算服务暂不可用", code="service_error", retryable=True),
                kind="convert",
            )

    @staticmethod
    def _c2c_request(parsed: ParsedCommand, request_id: str) -> QuoteRequest:
        source_kind = currency_metadata(parsed.source).kind
        target_kind = currency_metadata(parsed.target).kind
        if source_kind == "fiat" and target_kind == "crypto":
            fiat, asset, direction = parsed.source, parsed.target, Direction.BUY
        elif source_kind == "crypto" and target_kind == "fiat":
            fiat, asset, direction = parsed.target, parsed.source, Direction.SELL
        else:
            raise CommandError(
                "C2C 必须在法币与虚拟币之间换算",
                code="invalid_c2c_pair",
            )
        payments = (parsed.payment_method,) if parsed.payment_method else ()
        return QuoteRequest(
            fiat=fiat,
            asset=asset,
            direction=direction,
            amount=parsed.amount,
            provider=parsed.provider,
            payment_methods=payments,
            allow_market_fallback=False,
            request_id=request_id,
        )

    def quote_c2c(
        self,
        parsed: ParsedCommand,
        *,
        request_id: str = "",
        cancel: object | None = None,
    ) -> CommandResult:
        try:
            if parsed.kind != "c2c":
                raise CommandError("命令不是 C2C 请求", code="invalid_command")
            if self._c2c is None:
                raise CommandError("C2C 报价服务未配置", code="service_unavailable", retryable=True)
            request = self._c2c_request(parsed, request_id)
            result = self._c2c.quote(request, cancel=cancel)
            quote = _quote_to_dict(result)
            warnings = list(_dedupe_warnings(quote.get("warnings")))
            warnings.append(C2C_NON_GUARANTEE_WARNING)
            status = str(quote.get("status", ""))
            if status == QuoteStatus.MARKET_FALLBACK.value:
                warnings.append(MARKET_FALLBACK_WARNING)
            warnings = list(dict.fromkeys(warnings))
            quote["warnings"] = warnings
            provider = quote.get("provider") or parsed.provider
            return CommandResult(
                status="ok",
                kind="c2c",
                source=f"c2c:{provider}",
                data={"quote": quote},
                warnings=tuple(warnings),
            )
        except CommandError as exc:
            return _failure(exc, kind="c2c")
        except (ConversionError, ValueError):
            return _failure(
                CommandError("C2C 请求参数无效", code="invalid_c2c_request"),
                kind="c2c",
            )
        except Exception:
            return _failure(
                CommandError("C2C 报价服务暂不可用", code="service_error", retryable=True),
                kind="c2c",
            )

    def execute(
        self,
        command: object,
        *,
        request_id: str = "",
        cancel: object | None = None,
    ) -> CommandResult:
        try:
            parsed = parse_command(command)
        except CommandError as exc:
            return _failure(exc)
        if parsed.kind == "calculate":
            return self.calculate(parsed.expression)
        if parsed.kind == "convert":
            return self.convert(parsed.amount, parsed.source, parsed.target)
        return self.quote_c2c(parsed, request_id=request_id, cancel=cancel)

    def capabilities(self) -> dict[str, object]:
        providers: dict[str, dict[str, object]] = {
            "binance": {"enabled": False, "configured": False, "read_only": True},
            "okx": {"enabled": False, "configured": False, "read_only": True},
        }
        if self._c2c is not None:
            try:
                raw_capabilities = self._c2c.capabilities()
                for capability in tuple(raw_capabilities):  # type: ignore[arg-type]
                    if hasattr(capability, "to_dict"):
                        raw = capability.to_dict()
                    elif isinstance(capability, Mapping):
                        raw = dict(capability)
                    else:
                        continue
                    name = str(raw.get("provider", "")).strip().lower()
                    if _PROVIDER_RE.fullmatch(name) is None:
                        continue
                    providers[name] = {
                        "enabled": bool(raw.get("enabled", False)),
                        "configured": bool(raw.get("configured", False)),
                        "read_only": bool(raw.get("read_only", True)),
                    }
            except Exception:
                pass
        return {
            "calculator": {"available": True},
            "conversion": {"available": self._conversion is not None},
            "c2c": {
                "available": self._c2c is not None,
                "providers": providers,
            },
        }


__all__ = [
    "C2C_NON_GUARANTEE_WARNING",
    "CommandError",
    "CommandErrorData",
    "CommandResult",
    "CommandService",
    "MARKET_FALLBACK_WARNING",
    "MAX_COMMAND_CHARS",
    "ParsedCommand",
    "parse_command",
]
