import random
import unittest
from fractions import Fraction

from calculator_core import CalculationError, CalculatorModel, SafeEvaluator


class CalculatorPropertyTests(unittest.TestCase):
    REFERENCE_SEED = 317_2026_0820
    STATE_SEED = 317_0317

    @classmethod
    def _integer_expression(cls, rng: random.Random, depth: int) -> tuple[str, Fraction]:
        if depth <= 0 or rng.random() < 0.25:
            value = rng.randint(-100, 100)
            return str(value), Fraction(value)

        left_expression, left = cls._integer_expression(rng, depth - 1)
        right_expression, right = cls._integer_expression(rng, depth - 1)
        operation = rng.choice(("+", "-", "*", "%", "exact_divide"))
        if operation == "+":
            return f"({left_expression}+{right_expression})", left + right
        if operation == "-":
            return f"({left_expression}-{right_expression})", left - right
        if operation == "*":
            return f"({left_expression}*{right_expression})", left * right
        if operation == "%":
            if right == 0:
                right_expression, right = "1", Fraction(1)
            return f"({left_expression}%{right_expression})", Fraction(
                left.numerator % right.numerator
            )

        divisor = rng.randint(1, 100)
        return f"(({left_expression})*{divisor}/{divisor})", left

    def test_seeded_integer_expressions_match_fraction_reference_and_full_width_variant(self):
        rng = random.Random(self.REFERENCE_SEED)
        evaluator = SafeEvaluator()
        translation = str.maketrans(
            "0123456789+-*/%()",
            "０１２３４５６７８９＋－＊／％（）",
        )

        for case in range(3_000):
            expression, reference = self._integer_expression(rng, rng.randint(1, 5))
            expected = reference.numerator
            actual = evaluator.evaluate(expression)
            full_width_actual = evaluator.evaluate(expression.translate(translation) + "＝")
            self.assertEqual(
                (type(actual), actual, full_width_actual),
                (int, expected, expected),
                f"case={case}, seed={self.REFERENCE_SEED}, expression={expression}",
            )

    def test_seeded_model_state_machine_preserves_safety_invariants(self):
        rng = random.Random(self.STATE_SEED)
        inputs = list("0123456789") + [
            ".", "+", "−", "×", "÷", "^", "%", "(", ")", "π",
            "．", "＋", "－", "＊", "／", "％", "（", "）",
        ]
        actions = (
            ["input"] * 12
            + ["backspace", "toggle", "sqrt", "sin", "equals", "M+", "M−", "MR", "MC", "clear"]
        )
        structural_translation = str.maketrans({
            "（": "(", "）": ")", "＋": "+", "－": "-", "＊": "*", "／": "/",
            "％": "%", "．": ".", "−": "-", "×": "*", "÷": "/",
        })

        for case in range(100):
            model = CalculatorModel(
                angle_mode=rng.choice(("DEG", "RAD")),
                history_limit=rng.randint(1, 5),
            )
            for step in range(100):
                action = rng.choice(actions)
                previous_memory = model.memory
                try:
                    if action == "input":
                        model.input(rng.choice(inputs))
                    elif action == "backspace":
                        model.backspace()
                    elif action == "toggle":
                        model.toggle_sign()
                    elif action in {"sqrt", "sin"}:
                        model.wrap(f"{action}(")
                    elif action == "equals":
                        model.equals()
                    elif action in {"M+", "M−", "MR", "MC"}:
                        model.memory_action(action)
                    else:
                        model.clear()
                    model.preview()
                except CalculationError:
                    if action in {"M+", "M−"}:
                        self.assertEqual(model.memory, previous_memory)

                self.assertLessEqual(
                    len(model.expression), 512,
                    f"case={case}, step={step}, seed={self.STATE_SEED}",
                )
                balance = 0
                for character in model.expression.translate(structural_translation):
                    if character == "(":
                        balance += 1
                    elif character == ")":
                        balance -= 1
                        self.assertGreaterEqual(
                            balance,
                            0,
                            f"case={case}, step={step}, expression={model.expression!r}",
                        )
                self.assertLessEqual(len(model.history), model.history_limit)

    def test_unsupported_python_syntax_remains_outside_the_expression_surface(self):
        evaluator = SafeEvaluator()
        payloads = (
            "True",
            "None",
            "[1]",
            "(1, 2)",
            "{'value': 1}",
            "{1}",
            "lambda: 1",
            "(value := 1)",
            "1 << 2",
            "1 // 2",
            "1 @ 2",
            "~1",
            "not 1",
            "abs(value=1)",
            "sin(1, 2)",
            "pi.real",
            "__import__('os')",
            "1 # ignored tail",
            "1\\\n+ 2",
            "f'{1}'",
        )
        for expression in payloads:
            with self.subTest(expression=expression):
                with self.assertRaises(CalculationError):
                    evaluator.evaluate(expression)
        with self.assertRaisesRegex(CalculationError, "表达式过长"):
            evaluator.evaluate("1+" * 256 + "1")


if __name__ == "__main__":
    unittest.main()
