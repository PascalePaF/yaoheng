"""Thread-safe, side-effect-free Decimal currency conversion primitives.

All public boundary helpers accept JSON-compatible scalar values but return
canonical decimal strings.  No function in this module mutates caller-owned
state, performs I/O, or relies on the process-wide Decimal context.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import (
    Decimal,
    DecimalException,
    InvalidOperation,
    ROUND_HALF_EVEN,
    localcontext,
)
from types import MappingProxyType
from typing import Final


MIN_DECIMAL_PRECISION: Final = 80
MAX_DECIMAL_INPUT_CHARS: Final = 4096
MAX_SIGNIFICANT_DIGITS: Final = 200
MAX_CANONICAL_CHARS: Final = 4096
DEFAULT_CRYPTO_DISPLAY_PLACES: Final = 8
MAX_CRYPTO_DISPLAY_PLACES: Final = 24
CRYPTO_TINY_SIGNIFICANT_DIGITS: Final = 8

_NUMBER_RE = re.compile(
    r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?\Z"
)
_CURRENCY_CODE_RE = re.compile(r"[A-Z0-9_]{1,24}\Z")

# ISO 4217 minor units that differ from the usual two decimal places.
_ZERO_MINOR_UNIT_FIATS: Final = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW",
        "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)
_THREE_MINOR_UNIT_FIATS: Final = frozenset(
    {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
)
_FOUR_MINOR_UNIT_FIATS: Final = frozenset({"CLF", "UYW"})
_KNOWN_CRYPTO_CODES: Final = frozenset(
    {
        "AAVE", "ADA", "ALGO", "ARB", "ATOM", "AVAX", "BCH", "BNB",
        "BONK", "BTC", "DAI", "DOGE", "DOT", "ENA", "ETC", "ETH",
        "FDUSD", "FIL", "HBAR", "ICP", "JUP", "KAS", "LINK", "LTC",
        "MATIC", "MKR", "NEAR", "OKB", "OP", "PAXG", "PEPE", "POL",
        "PYUSD", "RENDER", "SHIB", "SOL", "SUI", "TON", "TRX", "TUSD",
        "UNI", "USDC", "USDE", "USDP", "USDS", "USDT", "VET", "WBTC",
        "WETH", "XAUT", "XLM", "XMR", "XRP", "ZEC",
    }
)


class ConversionError(ValueError):
    """Base class with a stable machine-readable error code."""

    code = "conversion_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or type(self).code


class DecimalBoundaryError(ConversionError):
    code = "invalid_decimal"


class AmountInputError(DecimalBoundaryError):
    """Stable error type for invalid user-entered amounts."""

    code = "invalid_amount"


class RateValueError(DecimalBoundaryError):
    code = "invalid_rate"


class CurrencyCodeError(ConversionError):
    code = "invalid_currency"


class MissingRateError(ConversionError):
    code = "missing_rate"


class ConversionRangeError(ConversionError):
    code = "conversion_out_of_range"


# Clear aliases for callers that prefer the adjective-first naming style.
InvalidAmountError = AmountInputError
InvalidRateError = RateValueError


def _raise(error_type: type[DecimalBoundaryError], message: str, exc: BaseException | None = None) -> None:
    error = error_type(message)
    if exc is None:
        raise error
    raise error from exc


def _text_from_scalar(value: object, error_type: type[DecimalBoundaryError]) -> str:
    if isinstance(value, bool) or value is None:
        _raise(error_type, "数值格式无效")
    try:
        if isinstance(value, float):
            if not math.isfinite(value):
                _raise(error_type, "数值必须是有限十进制数")
            text = str(value)
        elif isinstance(value, (str, int, Decimal)):
            text = str(value).strip()
        else:
            _raise(error_type, "数值格式无效")
    except (OverflowError, ValueError) as exc:
        _raise(error_type, "数值格式无效或过长", exc)
    if not text or len(text) > MAX_DECIMAL_INPUT_CHARS or _NUMBER_RE.fullmatch(text) is None:
        _raise(error_type, "数值格式无效或过长")
    return text


def _parse_decimal(
    value: object,
    error_type: type[DecimalBoundaryError],
    *,
    strictly_positive: bool = False,
) -> Decimal:
    text = _text_from_scalar(value, error_type)
    try:
        parsed = Decimal(text)
    except (DecimalException, TypeError, ValueError) as exc:
        _raise(error_type, "数值格式无效", exc)
    if not parsed.is_finite():
        _raise(error_type, "数值必须是有限十进制数")
    significant = "".join(str(digit) for digit in parsed.as_tuple().digits).strip("0")
    if len(significant) > MAX_SIGNIFICANT_DIGITS:
        _raise(error_type, "数值有效数字过长")
    if strictly_positive and parsed <= 0:
        _raise(error_type, "汇率必须大于零")
    if parsed.is_zero():
        return Decimal(0)
    if abs(parsed.adjusted()) >= MAX_CANONICAL_CHARS:
        _raise(error_type, "数值范围或长度超出限制")
    # Plain canonical output is deliberately bounded so a compact exponent
    # cannot expand into an unbounded JSON/cache value.
    if len(format(parsed, "f")) > MAX_CANONICAL_CHARS:
        _raise(error_type, "数值范围或长度超出限制")
    return parsed


def parse_amount(value: object) -> Decimal:
    """Parse an amount or raise :class:`AmountInputError`."""

    return _parse_decimal(value, AmountInputError)


def parse_rate(value: object) -> Decimal:
    """Parse a strictly positive rate or raise :class:`RateValueError`."""

    return _parse_decimal(value, RateValueError, strictly_positive=True)


def decimal_to_canonical(value: Decimal) -> str:
    """Return a finite Decimal as a unique, non-exponent decimal string."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise DecimalBoundaryError("数值必须是有限 Decimal")
    if value.is_zero():
        return "0"
    if abs(value.adjusted()) >= MAX_CANONICAL_CHARS:
        raise DecimalBoundaryError("规范十进制字符串过长")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text.startswith("+"):
        text = text[1:]
    if len(text) > MAX_CANONICAL_CHARS:
        raise DecimalBoundaryError("规范十进制字符串过长")
    return text


def canonical_amount_string(value: object) -> str:
    return decimal_to_canonical(parse_amount(value))


def canonical_rate_string(value: object) -> str:
    return decimal_to_canonical(parse_rate(value))


# General JSON-boundary spelling; amount-specific callers should use the
# function above so invalid input retains its stable AmountInputError type.
canonical_decimal_string = canonical_amount_string


def _decimal_digits(value: Decimal) -> int:
    return max(1, len(value.as_tuple().digits))


def calculate_conversion(amount: object, source_rate: object, target_rate: object) -> Decimal:
    """Calculate ``Y = X * R(Y) / R(X)`` without intermediate quantization."""

    amount_value = parse_amount(amount)
    source_value = parse_rate(source_rate)
    target_value = parse_rate(target_rate)
    precision = max(
        MIN_DECIMAL_PRECISION,
        _decimal_digits(amount_value)
        + _decimal_digits(source_value)
        + _decimal_digits(target_value)
        + 24,
    )
    try:
        with localcontext() as context:
            context.prec = precision
            result = amount_value * target_value / source_value
    except (DecimalException, OverflowError, ValueError) as exc:
        raise ConversionRangeError("换算结果超出可表示范围") from exc
    if not result.is_finite():
        raise ConversionRangeError("换算结果超出可表示范围")
    try:
        decimal_to_canonical(result)
    except DecimalBoundaryError as exc:
        raise ConversionRangeError("换算结果超出可表示范围") from exc
    return result


def convert_exact(amount: object, source_rate: object, target_rate: object) -> str:
    """Return an unquantized conversion result at the string boundary."""

    return decimal_to_canonical(calculate_conversion(amount, source_rate, target_rate))


@dataclass(frozen=True, slots=True)
class CurrencyMetadata:
    code: str
    kind: str
    display_precision: int
    max_display_precision: int
    rounding: str = ROUND_HALF_EVEN


def normalize_currency_code(value: object) -> str:
    if value is None or isinstance(value, bool):
        raise CurrencyCodeError("币种代码无效")
    code = str(value).strip().upper()
    if _CURRENCY_CODE_RE.fullmatch(code) is None:
        raise CurrencyCodeError("币种代码无效")
    return code


def currency_metadata(code: object, kind: str | None = None) -> CurrencyMetadata:
    normalized = normalize_currency_code(code)
    normalized_kind = str(kind).strip().lower() if kind is not None else ""
    if not normalized_kind:
        normalized_kind = "crypto" if normalized in _KNOWN_CRYPTO_CODES else "fiat"
    if normalized_kind not in {"fiat", "crypto"}:
        raise CurrencyCodeError("币种类型无效")
    if normalized_kind == "crypto":
        return CurrencyMetadata(
            normalized,
            "crypto",
            DEFAULT_CRYPTO_DISPLAY_PLACES,
            MAX_CRYPTO_DISPLAY_PLACES,
        )
    if normalized in _ZERO_MINOR_UNIT_FIATS:
        places = 0
    elif normalized in _THREE_MINOR_UNIT_FIATS:
        places = 3
    elif normalized in _FOUR_MINOR_UNIT_FIATS:
        places = 4
    else:
        places = 2
    return CurrencyMetadata(normalized, "fiat", places, places)


def _display_places(value: Decimal, metadata: CurrencyMetadata) -> int:
    if metadata.kind != "crypto" or value.is_zero():
        return metadata.display_precision
    default_quantum = Decimal(1).scaleb(-metadata.display_precision)
    if value.copy_abs() >= default_quantum:
        return metadata.display_precision
    required = -value.copy_abs().adjusted() + CRYPTO_TINY_SIGNIFICANT_DIGITS - 1
    return min(metadata.max_display_precision, max(metadata.display_precision, required))


def quantize_for_display(value: object, metadata: CurrencyMetadata) -> Decimal:
    parsed = _parse_decimal(value, DecimalBoundaryError)
    places = _display_places(parsed, metadata)
    quantum = Decimal(1).scaleb(-places)
    try:
        with localcontext() as context:
            integer_digits = max(1, parsed.adjusted() + 1) if not parsed.is_zero() else 1
            context.prec = max(MIN_DECIMAL_PRECISION, integer_digits + places + 8)
            rounded = parsed.quantize(quantum, rounding=metadata.rounding)
    except InvalidOperation as exc:
        raise ConversionRangeError("显示数值超出可表示范围") from exc
    return abs(rounded) if rounded.is_zero() else rounded


def format_for_display(
    value: object,
    code: object | None = None,
    kind: str | None = None,
    *,
    metadata: CurrencyMetadata | None = None,
) -> str:
    """Format fiat with minor units and crypto with tiny-value expansion."""

    meta = metadata or currency_metadata(code, kind)
    rounded = quantize_for_display(value, meta)
    places = max(0, -rounded.as_tuple().exponent)
    text = format(rounded, f".{places}f")
    if meta.kind == "crypto" and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


@dataclass(frozen=True, slots=True)
class ConversionResult:
    amount: str
    source: str
    target: str
    source_rate: str
    target_rate: str
    value: str
    display_value: str
    metadata: CurrencyMetadata


def convert_currencies(
    amount: object,
    source: object,
    target: object,
    rates: Mapping[str, object],
    kinds: Mapping[str, str] | None = None,
) -> ConversionResult:
    """Convert against a caller-owned rate mapping without mutating it."""

    source_code = normalize_currency_code(source)
    target_code = normalize_currency_code(target)
    normalized_rates = {str(key).strip().upper(): value for key, value in rates.items()}
    if source_code not in normalized_rates or target_code not in normalized_rates:
        raise MissingRateError("尚无所选币种的汇率")
    source_rate = canonical_rate_string(normalized_rates[source_code])
    target_rate = canonical_rate_string(normalized_rates[target_code])
    amount_text = canonical_amount_string(amount)
    value = convert_exact(amount_text, source_rate, target_rate)
    target_kind = None
    if kinds is not None:
        normalized_kinds = {str(key).strip().upper(): value for key, value in kinds.items()}
        target_kind = normalized_kinds.get(target_code)
    metadata = currency_metadata(target_code, target_kind)
    return ConversionResult(
        amount=amount_text,
        source=source_code,
        target=target_code,
        source_rate=source_rate,
        target_rate=target_rate,
        value=value,
        display_value=format_for_display(value, metadata=metadata),
        metadata=metadata,
    )


class DecimalConversionEngine:
    """Immutable reusable converter; safe to share across worker threads."""

    __slots__ = ("_kinds", "_rates")

    def __init__(
        self,
        rates: Mapping[str, object],
        kinds: Mapping[str, str] | None = None,
    ) -> None:
        normalized_rates: dict[str, str] = {}
        for raw_code, raw_rate in rates.items():
            code = normalize_currency_code(raw_code)
            normalized_rates[code] = canonical_rate_string(raw_rate)
        normalized_kinds: dict[str, str] = {}
        for raw_code, raw_kind in (kinds or {}).items():
            code = normalize_currency_code(raw_code)
            kind = str(raw_kind).strip().lower()
            if kind in {"fiat", "crypto"}:
                normalized_kinds[code] = kind
        self._rates = MappingProxyType(normalized_rates)
        self._kinds = MappingProxyType(normalized_kinds)

    @property
    def rate_strings(self) -> dict[str, str]:
        return dict(self._rates)

    @property
    def kinds(self) -> dict[str, str]:
        return dict(self._kinds)

    def convert(self, amount: object, source: object, target: object) -> ConversionResult:
        return convert_currencies(amount, source, target, self._rates, self._kinds)

    def convert_exact(self, amount: object, source: object, target: object) -> str:
        return self.convert(amount, source, target).value


ConversionEngine = DecimalConversionEngine
format_decimal_for_display = format_for_display


__all__ = [
    "AmountInputError",
    "ConversionEngine",
    "ConversionError",
    "ConversionRangeError",
    "ConversionResult",
    "CurrencyCodeError",
    "CurrencyMetadata",
    "DecimalBoundaryError",
    "DecimalConversionEngine",
    "InvalidAmountError",
    "InvalidRateError",
    "MissingRateError",
    "RateValueError",
    "calculate_conversion",
    "canonical_amount_string",
    "canonical_decimal_string",
    "canonical_rate_string",
    "convert_currencies",
    "convert_exact",
    "currency_metadata",
    "decimal_to_canonical",
    "format_decimal_for_display",
    "format_for_display",
    "normalize_currency_code",
    "parse_amount",
    "parse_rate",
    "quantize_for_display",
]
