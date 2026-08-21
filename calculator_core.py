"""Safe calculation engine used by 曜衡."""

from __future__ import annotations

import ast
import io
import math
import operator
import re
import tokenize
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, localcontext
from typing import Callable


class CalculationError(ValueError):
    """A user-facing calculation error."""


Number = int | float
_MAX_INTEGER_DIGITS = 4000
_MAX_ABS_INTEGER = 10**_MAX_INTEGER_DIGITS
_FUNCTION_NAMES = {
    "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
    "sqrt", "cbrt", "ln", "log", "exp", "abs", "floor", "ceil", "fact",
}
_DECIMAL_NUMBER_PATTERN = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?\Z")
_GROUPED_NUMBER_PATTERN = re.compile(
    r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d*)?(?:[eE][+\-]?\d+)?(?![\d.,])"
)
_ROOT_NUMBER_PATTERN = re.compile(
    r"[+\-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?"
)
_FULL_WIDTH_TRANSLATION = str.maketrans({
    **{chr(ord("０") + index): str(index) for index in range(10)},
    "＋": "+", "－": "-", "＊": "*", "／": "/", "（": "(", "）": ")",
    "＾": "^", "％": "%", "，": ",", "．": ".", "＝": "=",
})


def _format_large_integer(value: int, precision: int = 15) -> str:
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    exponent = len(digits) - 1
    leading = int(digits[:precision])
    if len(digits) > precision and digits[precision] >= "5":
        leading += 1
    significant = str(leading)
    if len(significant) > precision:
        exponent += 1
        significant = significant[:precision]
    fraction = significant[1:].rstrip("0")
    mantissa = significant[0] + (f".{fraction}" if fraction else "")
    return f"{sign}{mantissa}e{exponent}"


def format_number(value: Number) -> str:
    """Format a result without the usual floating-point visual noise."""
    if isinstance(value, bool):
        raise CalculationError("无法计算该表达式")
    if isinstance(value, int):
        if abs(value) >= _MAX_ABS_INTEGER:
            raise CalculationError("结果超出可表示范围")
        digits = len(str(abs(value))) if value else 1
        return str(value) if digits <= 16 else _format_large_integer(value)
    value = float(value)
    if not math.isfinite(value):
        raise CalculationError("结果超出可表示范围")
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    rendered = f"{value:.15g}".replace("e+", "e")
    return re.sub(r"e(-?)0+(\d+)$", r"e\1\2", rendered)


def _expand_square_roots(expression: str) -> str:
    """Turn calculator-style root prefixes into ordinary function calls."""
    while "√" in expression:
        root_index = expression.rfind("√")
        atom_start = root_index + 1
        while atom_start < len(expression) and expression[atom_start].isspace():
            atom_start += 1
        if atom_start >= len(expression):
            return expression[:root_index] + "sqrt" + expression[root_index + 1:]
        if expression[atom_start] == "(":
            expression = expression[:root_index] + "sqrt" + expression[root_index + 1:]
            continue

        atom_end: int | None = None
        number_match = _ROOT_NUMBER_PATTERN.match(expression, atom_start)
        if number_match:
            atom_end = number_match.end()
        else:
            name_match = re.match(r"[A-Za-z_]\w*", expression[atom_start:])
            if name_match:
                atom_end = atom_start + name_match.end()
                call_start = atom_end
                while call_start < len(expression) and expression[call_start].isspace():
                    call_start += 1
                if call_start < len(expression) and expression[call_start] == "(":
                    depth = 0
                    for index in range(call_start, len(expression)):
                        if expression[index] == "(":
                            depth += 1
                        elif expression[index] == ")":
                            depth -= 1
                            if depth == 0:
                                atom_end = index + 1
                                break
        if atom_end is None:
            expression = expression[:root_index] + "sqrt" + expression[root_index + 1:]
            continue
        atom = expression[atom_start:atom_end]
        expression = expression[:root_index] + f"sqrt({atom})" + expression[atom_end:]
    return expression


def _normalize_grouped_numbers(expression: str) -> str:
    return _GROUPED_NUMBER_PATTERN.sub(lambda match: match.group(0).replace(",", ""), expression)


def _float_literal_underflows(literal: str) -> bool:
    mantissa = re.split(r"[eE]", literal, maxsplit=1)[0]
    return float(literal) == 0.0 and any(digit in mantissa for digit in "123456789")


def _normalize_structural_characters(expression: str) -> str:
    return (
        expression.translate(_FULL_WIDTH_TRANSLATION)
        .replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
    )


def _normalize_expression(expression: str) -> str:
    normalized = (
        expression.strip().translate(_FULL_WIDTH_TRANSLATION)
        .replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("^", "**")
        .replace("π", " pi ")
    )
    normalized = _expand_square_roots(normalized)
    if normalized.endswith("="):
        normalized = normalized[:-1].rstrip()
    if "#" in normalized or "\\" in normalized:
        raise CalculationError("表达式包含不安全或不支持的内容")
    # Remove only complete, conventionally grouped decimal numbers. Any
    # malformed comma remains and is rejected below instead of changing value.
    normalized = _normalize_grouped_numbers(normalized)
    # Constants immediately followed by a number are calculator-style
    # multiplication (pi2 => pi*2). Scientific notation such as 1e-3 is one
    # NUMBER token and is deliberately left untouched.
    normalized = re.sub(r"\b(pi|e)(?=\d)", r"\1*", normalized)
    # Python rejects decimal integers such as 0002, while a calculator treats
    # them as ordinary base-10 entry. This does not affect zeros in fractions.
    normalized = re.sub(r"(?<![\w.])0+(?=\d)", "", normalized)

    tokens = list(tokenize.generate_tokens(io.StringIO(normalized).readline))
    rebuilt: list[tuple[int, str]] = []
    previous: tokenize.TokenInfo | None = None
    ignored = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}

    def ends_value(token: tokenize.TokenInfo) -> bool:
        return (
            token.type == tokenize.NUMBER
            or (token.type == tokenize.NAME and token.string in {"pi", "e"})
            or (token.type == tokenize.OP and token.string == ")")
        )

    def starts_value(token: tokenize.TokenInfo) -> bool:
        return token.type in {tokenize.NUMBER, tokenize.NAME} or (token.type == tokenize.OP and token.string == "(")

    for token in tokens:
        if token.type in ignored:
            continue
        if token.type == tokenize.NUMBER and not _DECIMAL_NUMBER_PATTERN.fullmatch(token.string):
            raise CalculationError("仅支持十进制数字")
        if token.type == tokenize.NUMBER and _float_literal_underflows(token.string):
            raise CalculationError("结果超出可表示范围")
        if token.type not in {tokenize.NUMBER, tokenize.NAME, tokenize.OP}:
            raise CalculationError("表达式包含不安全或不支持的内容")
        if token.type == tokenize.OP and token.string not in {"+", "-", "*", "/", "**", "%", "(", ")"}:
            raise CalculationError("表达式包含不安全或不支持的内容")
        if previous is not None and ends_value(previous) and starts_value(token):
            function_call = (
                previous.type == tokenize.NAME
                and previous.string in _FUNCTION_NAMES
                and token.type == tokenize.OP
                and token.string == "("
            )
            separated_numbers = previous.type == tokenize.NUMBER and token.type == tokenize.NUMBER
            if not function_call and not separated_numbers:
                rebuilt.append((tokenize.OP, "*"))
        rebuilt.append((token.type, token.string))
        previous = token
    return tokenize.untokenize(rebuilt).strip()


class SafeEvaluator:
    """Small AST evaluator: mathematical expressions only, never Python code."""

    _binary = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mod: operator.mod,
    }
    _unary = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    def __init__(self, angle_mode: str = "DEG", constants: dict[str, Number] | None = None) -> None:
        self.angle_mode = angle_mode
        self.constants = {"pi": math.pi, "e": math.e}
        for name, value in (constants or {}).items():
            if not name.isidentifier() or name in self.constants:
                raise CalculationError("包含未知常量")
            self.constants[name] = self._guard_result(value)

    @staticmethod
    def _reduce_degrees(value: Number, period: int) -> float:
        if isinstance(value, int):
            reduced = value % period
            if reduced > period / 2:
                reduced -= period
            return float(reduced)
        return math.remainder(float(value), float(period))

    def _from_radians(self, value: float) -> float:
        return math.degrees(value) if self.angle_mode == "DEG" else value

    def _sin(self, value: Number) -> float:
        if self.angle_mode == "DEG":
            degrees = self._reduce_degrees(value, 360)
            if degrees in {0.0, -180.0, 180.0}:
                return 0.0
            return math.sin(math.radians(degrees))
        radians = float(value)
        return 0.0 if radians != 0 and math.remainder(radians, math.pi) == 0 else math.sin(radians)

    def _cos(self, value: Number) -> float:
        if self.angle_mode == "DEG":
            degrees = self._reduce_degrees(value, 360)
            if degrees in {-90.0, 90.0}:
                return 0.0
            return math.cos(math.radians(degrees))
        radians = float(value)
        odd_quarter_turn = math.remainder(radians - math.pi / 2, math.pi) == 0
        return 0.0 if odd_quarter_turn else math.cos(radians)

    @staticmethod
    def _factorial(value: Number) -> int:
        if isinstance(value, int):
            integer = value
        elif value.is_integer():
            integer = int(value)
        else:
            raise CalculationError("阶乘仅支持非负整数")
        if integer < 0:
            raise CalculationError("阶乘仅支持非负整数")
        if integer > 1000:
            raise CalculationError("阶乘数值过大")
        return math.factorial(integer)

    @staticmethod
    def _multiply(left: Number, right: Number) -> Number:
        result = operator.mul(left, right)
        if left != 0 and right != 0 and result == 0:
            raise CalculationError("结果超出可表示范围")
        return result

    @staticmethod
    def _divide(left: Number, right: Number) -> Number:
        if right == 0:
            raise ZeroDivisionError
        if isinstance(left, int) and isinstance(right, int):
            quotient, remainder = divmod(left, right)
            if remainder == 0:
                return quotient
        result = operator.truediv(left, right)
        if left != 0 and result == 0:
            raise CalculationError("结果超出可表示范围")
        return result

    @staticmethod
    def _power(left: Number, right: Number) -> Number:
        if abs(right) > 10000:
            raise CalculationError("指数过大")
        if isinstance(left, int) and isinstance(right, int) and right >= 0 and abs(left) > 1:
            estimated_log10 = right * math.log10(abs(left))
            # Reject obviously oversized results before allocating them, but
            # leave a small boundary window for the exact post-check below.
            if estimated_log10 > _MAX_INTEGER_DIGITS + 1:
                raise CalculationError("结果超出可表示范围")
        try:
            result = left**right
        except OverflowError as exc:
            raise CalculationError("结果超出可表示范围") from exc
        if isinstance(result, complex):
            raise CalculationError("当前不支持复数结果")
        if left != 0 and result == 0:
            raise CalculationError("结果超出可表示范围")
        return result

    @staticmethod
    def _exp(value: Number) -> float:
        result = math.exp(value)
        if result == 0:
            raise CalculationError("结果超出可表示范围")
        return result

    def _tan(self, value: Number) -> float:
        if self.angle_mode == "DEG":
            degrees = self._reduce_degrees(value, 180)
            if math.isclose(abs(degrees), 90.0, rel_tol=0.0, abs_tol=1e-12):
                raise CalculationError("正切在该角度无定义")
            radians = math.radians(degrees)
        else:
            radians = float(value)
        if abs(math.cos(radians)) < 1e-15:
            raise CalculationError("正切在该角度无定义")
        return math.tan(radians)

    def functions(self) -> dict[str, Callable[..., Number]]:
        return {
            "sin": self._sin,
            "cos": self._cos,
            "tan": self._tan,
            "asin": lambda x: self._from_radians(math.asin(x)),
            "acos": lambda x: self._from_radians(math.acos(x)),
            "atan": lambda x: self._from_radians(math.atan(x)),
            "sinh": math.sinh,
            "cosh": math.cosh,
            "tanh": math.tanh,
            "sqrt": math.sqrt,
            "cbrt": math.cbrt,
            "ln": math.log,
            "log": math.log10,
            "exp": self._exp,
            "abs": abs,
            "floor": math.floor,
            "ceil": math.ceil,
            "fact": self._factorial,
        }

    @staticmethod
    def _guard_result(value: Number) -> Number:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CalculationError("无法计算该表达式")
        if isinstance(value, float) and not math.isfinite(value):
            raise CalculationError("结果超出可表示范围")
        if isinstance(value, int) and abs(value) >= _MAX_ABS_INTEGER:
            raise CalculationError("结果超出可表示范围")
        return value

    def evaluate(self, expression: str) -> Number:
        if not expression.strip():
            return 0.0
        if len(expression) > 512:
            raise CalculationError("表达式过长")
        try:
            normalized = _normalize_expression(expression)
            if not normalized:
                return 0.0
            tree = ast.parse(normalized, mode="eval")
            return self._guard_result(self._visit(tree.body))
        except CalculationError:
            raise
        except ZeroDivisionError as exc:
            raise CalculationError("不能除以零") from exc
        except OverflowError as exc:
            raise CalculationError("结果超出可表示范围") from exc
        except (SyntaxError, tokenize.TokenError, ValueError, TypeError, RecursionError) as exc:
            raise CalculationError("无法计算该表达式") from exc

    def _visit(self, node: ast.AST) -> Number:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return self._guard_result(node.value)
        if isinstance(node, ast.Name):
            if node.id in self.constants:
                return self.constants[node.id]
            raise CalculationError("包含未知常量")
        if isinstance(node, ast.BinOp):
            left = self._visit(node.left)
            right = self._visit(node.right)
            if isinstance(node.op, ast.Pow):
                return self._guard_result(self._power(left, right))
            if isinstance(node.op, ast.Div):
                return self._guard_result(self._divide(left, right))
            if isinstance(node.op, ast.Mult):
                return self._guard_result(self._multiply(left, right))
            func = self._binary.get(type(node.op))
            if not func:
                raise CalculationError("不支持该运算")
            return self._guard_result(func(left, right))
        if isinstance(node, ast.UnaryOp):
            func = self._unary.get(type(node.op))
            if not func:
                raise CalculationError("不支持该运算")
            return self._guard_result(func(self._visit(node.operand)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func = self.functions().get(node.func.id)
            if not func or node.keywords or len(node.args) != 1:
                raise CalculationError("不支持该函数")
            try:
                return self._guard_result(func(self._visit(node.args[0])))
            except CalculationError:
                raise
            except ValueError as exc:
                raise CalculationError("函数输入超出定义域") from exc
            except OverflowError as exc:
                raise CalculationError("结果超出可表示范围") from exc
        raise CalculationError("表达式包含不安全或不支持的内容")


_BASIC_AMOUNT_PATTERN = re.compile(r"[0-9\s,.()%+\-*/×÷−]+")


def _normalize_basic_amount(expression: str) -> str:
    text = _normalize_structural_characters(expression.strip())
    if text.endswith("="):
        text = text[:-1].rstrip()
    if not text or len(text) > 80 or not _BASIC_AMOUNT_PATTERN.fullmatch(text):
        raise CalculationError("仅支持数字、加减乘除、除余和括号")
    normalized = _normalize_grouped_numbers(text)
    if "," in normalized:
        raise CalculationError("千位分隔符格式不正确")
    if "**" in normalized or "//" in normalized:
        raise CalculationError("仅支持数字、加减乘除、除余和括号")
    # Python rejects decimal integers such as 0002, while a calculator treats
    # them as an ordinary base-10 entry.
    return re.sub(r"(?<![\w.])0+(?=\d)", "", normalized)


def evaluate_basic_amount(expression: str) -> float:
    """Evaluate a non-negative amount made only from basic arithmetic and modulo."""
    normalized = _normalize_basic_amount(expression)
    value = SafeEvaluator().evaluate(normalized)
    if value < 0:
        raise CalculationError("金额不能小于零")
    return value


def evaluate_basic_amount_decimal(expression: str) -> str:
    """Evaluate a basic amount expression and return a canonical Decimal string.

    Currency conversion must not reintroduce binary floating-point noise after
    the exact Decimal conversion boundary.  This evaluator deliberately
    supports only ``+ - * / %`` and parentheses, with a bounded Decimal
    context for recurring divisions.
    """

    normalized = _normalize_basic_amount(expression)
    try:
        tree = ast.parse(normalized, mode="eval")
    except (MemoryError, RecursionError, SyntaxError) as exc:
        raise CalculationError("算式格式不正确") from exc

    def visit(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            literal = ast.get_source_segment(normalized, node) or str(node.value)
            try:
                return Decimal(literal)
            except DecimalException as exc:
                raise CalculationError("算式包含无效数字") from exc
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)):
            left = visit(node.left)
            right = visit(node.right)
            try:
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right
                return left % right
            except (DecimalException, ZeroDivisionError) as exc:
                raise CalculationError("不能除以零") from exc
        raise CalculationError("仅支持数字、加减乘除、除余和括号")

    try:
        with localcontext() as context:
            context.prec = 34
            result = visit(tree)
    except CalculationError:
        raise
    except (DecimalException, MemoryError, OverflowError, RecursionError) as exc:
        raise CalculationError("结果超出可表示范围") from exc
    if result < 0:
        raise CalculationError("金额不能小于零")
    try:
        from conversion_core import canonical_amount_string

        return canonical_amount_string(result)
    except (ValueError, DecimalException) as exc:
        raise CalculationError("结果超出可表示范围") from exc


@dataclass
class CalculatorModel:
    expression: str = ""
    memory: Number = 0
    angle_mode: str = "DEG"
    just_evaluated: bool = False
    history: list[tuple[str, str]] = field(default_factory=list)
    history_limit: int = 30
    _answer_value: Number | None = field(default=None, init=False, repr=False)
    _semantic_expression: str | None = field(default=None, init=False, repr=False)
    _semantic_display: str | None = field(default=None, init=False, repr=False)
    _just_applied_percent: bool = field(default=False, init=False, repr=False)

    def evaluator(self) -> SafeEvaluator:
        return SafeEvaluator(self.angle_mode)

    def _discard_semantic_expression(self) -> None:
        self._answer_value = None
        self._semantic_expression = None
        self._semantic_display = None

    def _active_semantic_expression(self) -> str | None:
        if (
            self._answer_value is not None
            and self._semantic_expression is not None
            and self._semantic_display == self.expression
            and (self._semantic_expression != "ans" or self.just_evaluated)
        ):
            return self._semantic_expression
        self._discard_semantic_expression()
        return None

    def _set_semantic_expression(self, expression: str) -> None:
        self._semantic_expression = expression
        self._semantic_display = self.expression

    @staticmethod
    def _validate_expression_length(*expressions: str) -> None:
        if any(len(expression) > 512 for expression in expressions):
            raise CalculationError("表达式过长")

    def _evaluate_current(self, close_parentheses: bool = False) -> Number:
        semantic = self._active_semantic_expression()
        source = semantic if semantic is not None else (self.expression or "0")
        if close_parentheses:
            source = self._close_parentheses(source)
        if semantic is not None:
            return SafeEvaluator(self.angle_mode, {"ans": self._answer_value}).evaluate(source)
        return self.evaluator().evaluate(source)

    def display_expression(self) -> str:
        return self.expression or "0"

    def preview(self) -> str:
        if not self.expression:
            return "0"
        if _normalize_structural_characters(self.expression)[-1:] in "+-*/^%.(":
            return "0"
        try:
            return format_number(self._evaluate_current(close_parentheses=True))
        except CalculationError:
            return ""

    def clear(self) -> None:
        self.expression = ""
        self.just_evaluated = False
        self._just_applied_percent = False
        self._discard_semantic_expression()

    def set_expression(self, expression: str) -> None:
        """Replace the expression from direct keyboard editing."""
        self.expression = expression[:512]
        self.just_evaluated = False
        self._just_applied_percent = False
        self._discard_semantic_expression()

    def backspace(self) -> None:
        if self.just_evaluated:
            self.clear()
        else:
            # Backspace is an explicit edit of the rendered text, so any
            # hidden exact continuation value must no longer override it.
            self._just_applied_percent = False
            self._discard_semantic_expression()
            self.expression = self.expression[:-1]

    @staticmethod
    def _input_into(expression: str, token: str) -> str:
        normalized = _normalize_structural_characters(expression)
        normalized_token = _normalize_structural_characters(token)
        operators = "+-*/^%"
        if len(normalized_token) == 1 and normalized_token in operators:
            if not expression:
                return token if normalized_token == "-" else expression
            if normalized[-1] == "(":
                if normalized_token == "-":
                    return expression + token
                return expression
            if normalized[-1] in operators:
                last_is_unary = len(expression) == 1 or normalized[-2] in operators + "("
                if normalized_token == "-":
                    if not last_is_unary:
                        return expression + token
                elif last_is_unary and len(expression) >= 2 and normalized[-2] in operators:
                    return expression[:-2] + token
                elif last_is_unary:
                    return expression[:-1]
                else:
                    return expression[:-1] + token
                return expression
            return expression + token
        if normalized_token == ")" and normalized.count("(") <= normalized.count(")"):
            return expression
        if normalized_token == ".":
            tail = re.split(r"[+\-*/^%()]", normalized)[-1]
            scientific_operand = re.search(
                r"(?:\d+(?:\.\d*)?|\.\d+)[eE][+\-]?\d+\Z", normalized
            )
            if "." in tail or scientific_operand:
                return expression
            if not tail:
                token = "0."
        remaining = 512 - len(expression)
        if remaining > 0:
            return expression + token[:remaining]
        return expression

    def input(self, token: str) -> None:
        token = {"-": "−", "*": "×", "/": "÷"}.get(token, token)
        operators = "+−×÷^%"
        semantic = self._active_semantic_expression()
        if self.just_evaluated and token not in operators:
            self.expression = ""
            semantic = None
            self._discard_semantic_expression()
        self.just_evaluated = False
        self._just_applied_percent = False

        old_expression = self.expression
        new_expression = self._input_into(old_expression, token)
        self.expression = new_expression
        if semantic is not None:
            new_semantic = self._input_into(semantic, token)
            # The rendered 512-character limit remains authoritative.
            if new_expression == old_expression and new_semantic != semantic:
                new_semantic = semantic
            self._set_semantic_expression(new_semantic)

    def wrap(self, prefix: str, suffix: str = ")") -> None:
        semantic = self._active_semantic_expression()
        target = self.expression or "0"
        wrapped = f"{prefix}{target}{suffix}"
        wrapped_semantic = f"{prefix}{semantic}{suffix}" if semantic is not None else None
        self._validate_expression_length(wrapped, *(wrapped_semantic,) if wrapped_semantic is not None else ())
        self.expression = wrapped
        if wrapped_semantic is not None:
            self._set_semantic_expression(wrapped_semantic)
        self.just_evaluated = False

    @classmethod
    def _percent_expression(cls, expression: str) -> str:
        expression = cls._close_parentheses(expression)
        start = cls._last_operand_start(expression)
        if start > 0:
            operator_index = start - 1
            operator_token = expression[operator_index]
            normalized_operator = _normalize_structural_characters(operator_token)
            left = expression[:operator_index]
            operand = expression[start:]
            if left and operand and normalized_operator in "+-":
                return f"{left}{operator_token}(({left})×({operand})÷100)"
            if left and operand and normalized_operator in "*/":
                return f"{left}{operator_token}(({operand})÷100)"
        return f"({expression})÷100"

    def apply_percent(self) -> None:
        if not self.expression:
            return
        semantic = self._active_semantic_expression()
        # Phone-calculator behavior: 200 + 10% becomes 200 + (200 * 10 / 100).
        if self._just_applied_percent:
            percentage = f"({self._close_parentheses(self.expression)})÷100"
        else:
            percentage = self._percent_expression(self.expression)
        semantic_percentage: str | None = None
        if semantic is not None:
            if self._just_applied_percent:
                semantic_percentage = f"({self._close_parentheses(semantic)})÷100"
            else:
                semantic_percentage = self._percent_expression(semantic)
        self._validate_expression_length(
            percentage, *(semantic_percentage,) if semantic_percentage is not None else ()
        )
        self.expression = percentage
        if semantic_percentage is not None:
            self._set_semantic_expression(semantic_percentage)
        self._just_applied_percent = True
        self.just_evaluated = False

    @classmethod
    def _toggle_sign_expression(cls, expression: str) -> str:
        start = cls._last_operand_start(expression)
        target = expression[start:]
        if not target:
            return expression
        normalized_target = _normalize_structural_characters(target)
        if normalized_target.startswith("-(") and cls._has_complete_outer_parentheses(target[1:]):
            target = target[2:-1]
        elif normalized_target.startswith("-"):
            target = target[1:]
        else:
            target = f"−({target})"
        return expression[:start] + target

    @staticmethod
    def _has_complete_outer_parentheses(expression: str) -> bool:
        normalized = _normalize_structural_characters(expression)
        if not normalized.startswith("(") or not normalized.endswith(")"):
            return False
        depth = 0
        for index, character in enumerate(normalized):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    return False
                if depth < 0:
                    return False
        return depth == 0

    def toggle_sign(self) -> None:
        if not self.expression:
            self.expression = "−"
            self._discard_semantic_expression()
            return
        semantic = self._active_semantic_expression()
        toggled = self._toggle_sign_expression(self.expression)
        toggled_semantic = self._toggle_sign_expression(semantic) if semantic is not None else None
        self._validate_expression_length(toggled, *(toggled_semantic,) if toggled_semantic is not None else ())
        self.expression = toggled
        if toggled_semantic is not None:
            self._set_semantic_expression(toggled_semantic)
        self.just_evaluated = False

    @staticmethod
    def _last_operand_start(expression: str) -> int:
        normalized = _normalize_structural_characters(expression)
        operators = "+-*/^%"
        depth = 0
        for index in range(len(normalized) - 1, -1, -1):
            character = normalized[index]
            if character == ")":
                depth += 1
            elif character == "(":
                depth = max(0, depth - 1)
            elif depth == 0 and character in operators:
                exponent_sign = (
                    character in "+-"
                    and index >= 2
                    and normalized[index - 1] in "eE"
                    and (normalized[index - 2].isdigit() or normalized[index - 2] == ".")
                )
                if exponent_sign:
                    continue
                unary = index == 0 or normalized[index - 1] in operators + "("
                if not unary:
                    return index + 1
        return 0

    @staticmethod
    def _close_parentheses(expression: str) -> str:
        normalized = _normalize_structural_characters(expression)
        balance = 0
        for character in normalized:
            if character == "(":
                balance += 1
            elif character == ")":
                balance -= 1
                if balance < 0:
                    return expression
        return expression + ")" * balance

    def equals(self) -> str:
        if not self.expression:
            return "0"
        original = self._close_parentheses(self.expression)
        exact_value = self._evaluate_current(close_parentheses=True)
        value = format_number(exact_value)
        self.history.insert(0, (original, value))
        del self.history[max(1, self.history_limit):]
        self.expression = value.replace("-", "−")
        self._answer_value = exact_value
        self._set_semantic_expression("ans")
        self._just_applied_percent = False
        self.just_evaluated = True
        return value

    def memory_action(self, action: str) -> None:
        if action == "MC":
            self.memory = 0
        elif action == "MR":
            self.expression = format_number(self.memory).replace("-", "−")
            self._answer_value = SafeEvaluator._guard_result(self.memory)
            self._set_semantic_expression("ans")
            self._just_applied_percent = False
            self.just_evaluated = True
        elif action in {"M+", "M−"}:
            value = self._evaluate_current(close_parentheses=True)
            current = SafeEvaluator._guard_result(self.memory)
            try:
                updated = current + value if action == "M+" else current - value
            except OverflowError as exc:
                raise CalculationError("结果超出可表示范围") from exc
            self.memory = SafeEvaluator._guard_result(updated)
