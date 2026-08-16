"""Safe calculation engine used by 曜衡."""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass, field
from typing import Callable


class CalculationError(ValueError):
    """A user-facing calculation error."""


def format_number(value: float | int) -> str:
    """Format a result without the usual floating-point visual noise."""
    value = float(value)
    if not math.isfinite(value):
        raise CalculationError("结果超出可表示范围")
    if abs(value) < 1e-14:
        value = 0.0
    if value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    magnitude = abs(value)
    if magnitude and (magnitude >= 1e12 or magnitude < 1e-9):
        return f"{value:.12e}".replace("e+", "e")
    return f"{value:.12f}".rstrip("0").rstrip(".")


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
    def _factorial(value: float) -> int:
        if not float(value).is_integer() or value < 0:
            raise CalculationError("阶乘仅支持非负整数")
        if value > 1000:
            raise CalculationError("阶乘数值过大")
        return math.factorial(int(value))

    @staticmethod
    def _power(left: float, right: float) -> float:
        if abs(right) > 10000:
            raise CalculationError("指数过大")
        result = left**right
        if isinstance(result, complex):
            raise CalculationError("当前不支持复数结果")
        return result

    def functions(self) -> dict[str, Callable[..., float]]:
        return {
            "sin": lambda x: math.sin(self._to_radians(x)),
            "cos": lambda x: math.cos(self._to_radians(x)),
            "tan": lambda x: math.tan(self._to_radians(x)),
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

    def evaluate(self, expression: str) -> float:
        if not expression.strip():
            return 0.0
        if len(expression) > 512:
            raise CalculationError("表达式过长")
        normalized = (
            expression.replace("×", "*")
            .replace("÷", "/")
            .replace("−", "-")
            .replace("^", "**")
            .replace("π", "pi")
            .replace("√", "sqrt")
        )
        # Add multiplication in common calculator-style input such as 2π and 2(3+4).
        normalized = re.sub(r"(?<=\d)(?=(?:pi|e|[A-Za-z_(]))", "*", normalized)
        normalized = re.sub(r"(?<=\))(?=(?:\d|pi|e|[A-Za-z_(]))", "*", normalized)
        try:
            tree = ast.parse(normalized, mode="eval")
            value = self._visit(tree.body)
            value = float(value)
            if not math.isfinite(value):
                raise CalculationError("结果超出可表示范围")
            return value
        except CalculationError:
            raise
        except ZeroDivisionError as exc:
            raise CalculationError("不能除以零") from exc
        except (SyntaxError, ValueError, OverflowError, TypeError) as exc:
            raise CalculationError("无法计算该表达式") from exc

    def _visit(self, node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            constants = {"pi": math.pi, "e": math.e}
            if node.id in constants:
                return constants[node.id]
            raise CalculationError("包含未知常量")
        if isinstance(node, ast.BinOp):
            left = self._visit(node.left)
            right = self._visit(node.right)
            if isinstance(node.op, ast.Pow):
                return self._power(left, right)
            func = self._binary.get(type(node.op))
            if not func:
                raise CalculationError("不支持该运算")
            return func(left, right)
        if isinstance(node, ast.UnaryOp):
            func = self._unary.get(type(node.op))
            if not func:
                raise CalculationError("不支持该运算")
            return func(self._visit(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func = self.functions().get(node.func.id)
            if not func or node.keywords or len(node.args) != 1:
                raise CalculationError("不支持该函数")
            return func(self._visit(node.args[0]))
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
        if self.expression[-1:] in "+−×÷^.(":
            return "0"
        try:
            return format_number(self.evaluator().evaluate(self.expression))
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
        operators = "+−×÷^"
        if self.just_evaluated and token not in operators:
            self.expression = ""
        self.just_evaluated = False

        if token in operators:
            if not self.expression:
                if token == "−":
                    self.expression = token
                return
            if self.expression[-1] in operators:
                self.expression = self.expression[:-1] + token
            else:
                self.expression += token
            return
        if token == ".":
            tail = re.split(r"[+−×÷^()]", self.expression)[-1]
            if "." in tail:
                return
            if not tail:
                token = "0."
        self.expression += token

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
        self.expression = f"−({self.expression})" if not self.expression.startswith("−(") else self.expression[2:-1]
        self.just_evaluated = False

    def equals(self) -> str:
        if not self.expression:
            return "0"
        original = self.expression
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
