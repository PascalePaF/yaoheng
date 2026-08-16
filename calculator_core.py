"""Safe calculation engine used by 曜衡."""

from __future__ import annotations

import ast
import io
import math
import operator
import re
import tokenize
from dataclasses import dataclass, field
from typing import Callable


class CalculationError(ValueError):
    """A user-facing calculation error."""


Number = int | float
_MAX_INTEGER_DIGITS = 4000
_FUNCTION_NAMES = {
    "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
    "sqrt", "cbrt", "ln", "log", "exp", "abs", "floor", "ceil", "fact",
}


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
        digits = len(str(abs(value))) if value else 1
        if digits > _MAX_INTEGER_DIGITS:
            raise CalculationError("结果超出可表示范围")
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


def _normalize_expression(expression: str) -> str:
    normalized = (
        expression.strip()
        .replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("＋", "+")
        .replace("－", "-")
        .replace("＊", "*")
        .replace("／", "/")
        .replace("（", "(")
        .replace("）", ")")
        .replace("^", "**")
        .replace("π", "pi")
        .replace("√", "sqrt")
    )
    if normalized.endswith("="):
        normalized = normalized[:-1].rstrip()
    # Remove only conventional thousands separators. Malformed commas remain
    # in the expression and are rejected by the AST evaluator.
    normalized = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", normalized)
    # Constants immediately followed by a number are calculator-style
    # multiplication (pi2 => pi*2). Scientific notation such as 1e-3 is one
    # NUMBER token and is deliberately left untouched.
    normalized = re.sub(r"\b(pi|e)(?=\d)", r"\1*", normalized)

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
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
    }
    _unary = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    def __init__(self, angle_mode: str = "DEG") -> None:
        self.angle_mode = angle_mode

    def _to_radians(self, value: float) -> float:
        return math.radians(value) if self.angle_mode == "DEG" else value

    def _from_radians(self, value: float) -> float:
        return math.degrees(value) if self.angle_mode == "DEG" else value

    @staticmethod
    def _factorial(value: Number) -> int:
        if not float(value).is_integer() or value < 0:
            raise CalculationError("阶乘仅支持非负整数")
        if value > 1000:
            raise CalculationError("阶乘数值过大")
        return math.factorial(int(value))

    @staticmethod
    def _power(left: Number, right: Number) -> Number:
        if abs(right) > 10000:
            raise CalculationError("指数过大")
        if isinstance(left, int) and isinstance(right, int) and right >= 0 and abs(left) > 1:
            estimated_digits = int(right * math.log10(abs(left))) + 1
            if estimated_digits > _MAX_INTEGER_DIGITS:
                raise CalculationError("结果超出可表示范围")
        result = left**right
        if isinstance(result, complex):
            raise CalculationError("当前不支持复数结果")
        return result

    def _tan(self, value: Number) -> float:
        radians = self._to_radians(float(value))
        if abs(math.cos(radians)) < 1e-15:
            raise CalculationError("正切在该角度无定义")
        return math.tan(radians)

    def functions(self) -> dict[str, Callable[..., Number]]:
        return {
            "sin": lambda x: math.sin(self._to_radians(x)),
            "cos": lambda x: math.cos(self._to_radians(x)),
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
            "exp": math.exp,
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
        if isinstance(value, int) and value:
            estimated_digits = int(value.bit_length() * math.log10(2)) + 1
            if estimated_digits > _MAX_INTEGER_DIGITS:
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
        except (SyntaxError, tokenize.TokenError, ValueError, OverflowError, TypeError, RecursionError) as exc:
            raise CalculationError("无法计算该表达式") from exc

    def _visit(self, node: ast.AST) -> Number:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return self._guard_result(node.value)
        if isinstance(node, ast.Name):
            constants = {"pi": math.pi, "e": math.e}
            if node.id in constants:
                return constants[node.id]
            raise CalculationError("包含未知常量")
        if isinstance(node, ast.BinOp):
            left = self._visit(node.left)
            right = self._visit(node.right)
            if isinstance(node.op, ast.Pow):
                return self._guard_result(self._power(left, right))
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


def evaluate_basic_amount(expression: str) -> float:
    """Evaluate a non-negative amount made only from basic arithmetic and modulo."""
    text = expression.strip()
    if text.endswith("="):
        text = text[:-1].rstrip()
    if not text or len(text) > 80 or not _BASIC_AMOUNT_PATTERN.fullmatch(text):
        raise CalculationError("仅支持数字、加减乘除、除余和括号")
    normalized = text.replace(",", "").replace("×", "*").replace("÷", "/").replace("−", "-")
    if "**" in normalized or "//" in normalized:
        raise CalculationError("仅支持数字、加减乘除、除余和括号")
    value = SafeEvaluator().evaluate(normalized)
    if value < 0:
        raise CalculationError("金额不能小于零")
    return value


@dataclass
class CalculatorModel:
    expression: str = ""
    memory: float = 0.0
    angle_mode: str = "DEG"
    just_evaluated: bool = False
    history: list[tuple[str, str]] = field(default_factory=list)
    history_limit: int = 30

    def evaluator(self) -> SafeEvaluator:
        return SafeEvaluator(self.angle_mode)

    def display_expression(self) -> str:
        return self.expression or "0"

    def preview(self) -> str:
        if not self.expression:
            return "0"
        if self.expression[-1:] in "+−-×*÷/^%.(":
            return "0"
        try:
            return format_number(self.evaluator().evaluate(self._close_parentheses(self.expression)))
        except CalculationError:
            return ""

    def clear(self) -> None:
        self.expression = ""
        self.just_evaluated = False

    def set_expression(self, expression: str) -> None:
        """Replace the expression from direct keyboard editing."""
        self.expression = expression[:512]
        self.just_evaluated = False

    def backspace(self) -> None:
        if self.just_evaluated:
            self.clear()
        else:
            self.expression = self.expression[:-1]

    def input(self, token: str) -> None:
        token = {"-": "−", "*": "×", "/": "÷"}.get(token, token)
        operators = "+−×÷^%"
        if self.just_evaluated and token not in operators:
            self.expression = ""
        self.just_evaluated = False

        if token in operators:
            if not self.expression:
                if token == "−":
                    self.expression = token
                return
            if self.expression[-1] == "(":
                if token == "−":
                    self.expression += token
                return
            if self.expression[-1] in operators:
                last_is_unary = len(self.expression) == 1 or self.expression[-2] in operators + "("
                if token == "−":
                    if not last_is_unary:
                        self.expression += token
                elif last_is_unary and len(self.expression) >= 2 and self.expression[-2] in operators:
                    self.expression = self.expression[:-2] + token
                elif last_is_unary:
                    self.expression = self.expression[:-1]
                else:
                    self.expression = self.expression[:-1] + token
            else:
                self.expression += token
            return
        if token == ")" and self.expression.count("(") <= self.expression.count(")"):
            return
        if token == ".":
            tail = re.split(r"[+−×÷^%()]", self.expression)[-1]
            if "." in tail:
                return
            if not tail:
                token = "0."
        remaining = 512 - len(self.expression)
        if remaining > 0:
            self.expression += token[:remaining]

    def wrap(self, prefix: str, suffix: str = ")") -> None:
        target = self.expression or "0"
        self.expression = f"{prefix}{target}{suffix}"
        self.just_evaluated = False

    def apply_percent(self) -> None:
        if not self.expression:
            return
        # Phone-calculator behavior: 200 + 10% becomes 200 + (200 * 10 / 100).
        match = re.match(r"^(.*?)([+−×÷])(-?\d+(?:\.\d+)?)$", self.expression)
        if match:
            left, op, number = match.groups()
            if op in "+−":
                self.expression = f"{left}{op}(({left})×{number}÷100)"
            else:
                self.expression = f"{left}{op}({number}÷100)"
        else:
            self.expression = f"({self.expression})÷100"
        self.just_evaluated = False

    def toggle_sign(self) -> None:
        if not self.expression:
            self.expression = "−"
            return
        start = self._last_operand_start(self.expression)
        target = self.expression[start:]
        if not target:
            return
        if target.startswith("−(") and target.endswith(")"):
            target = target[2:-1]
        elif target.startswith("−"):
            target = target[1:]
        else:
            target = f"−({target})"
        self.expression = self.expression[:start] + target
        self.just_evaluated = False

    @staticmethod
    def _last_operand_start(expression: str) -> int:
        operators = "+−-×*÷/^%"
        depth = 0
        for index in range(len(expression) - 1, -1, -1):
            character = expression[index]
            if character == ")":
                depth += 1
            elif character == "(":
                depth = max(0, depth - 1)
            elif depth == 0 and character in operators:
                unary = index == 0 or expression[index - 1] in operators + "("
                if not unary:
                    return index + 1
        return 0

    @staticmethod
    def _close_parentheses(expression: str) -> str:
        balance = 0
        for character in expression:
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
        value = format_number(self.evaluator().evaluate(original))
        self.history.insert(0, (original, value))
        del self.history[max(1, self.history_limit):]
        self.expression = value.replace("-", "−")
        self.just_evaluated = True
        return value

    def memory_action(self, action: str) -> None:
        if action == "MC":
            self.memory = 0.0
        elif action == "MR":
            self.expression = format_number(self.memory).replace("-", "−")
            self.just_evaluated = False
        elif action in {"M+", "M−"}:
            value = self.evaluator().evaluate(self.expression or "0")
            self.memory += value if action == "M+" else -value
