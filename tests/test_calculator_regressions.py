import math
import unittest

from calculator_core import (
    CalculationError,
    CalculatorModel,
    SafeEvaluator,
    evaluate_basic_amount,
    format_number,
)


class ExpressionNormalizationRegressionTests(unittest.TestCase):
    def test_pasted_full_width_digits_and_leading_zeroes_are_decimal(self):
        evaluator = SafeEvaluator()
        self.assertEqual(evaluator.evaluate("０００２＋３＝"), 5)
        self.assertEqual(evaluator.evaluate("0002+1"), 3)

    def test_square_root_glyph_and_adjacent_pi_constants_multiply(self):
        evaluator = SafeEvaluator()
        self.assertAlmostEqual(evaluator.evaluate("2√9+ππ"), 6 + math.pi**2)

    def test_malformed_grouping_is_not_silently_reinterpreted(self):
        with self.assertRaises(CalculationError):
            SafeEvaluator().evaluate("1.2,345")
        with self.assertRaises(CalculationError):
            evaluate_basic_amount("1,2+3")

    def test_python_comments_and_non_decimal_literals_are_rejected(self):
        evaluator = SafeEvaluator()
        with self.assertRaises(CalculationError):
            evaluator.evaluate("1+2 # +999")
        with self.assertRaises(CalculationError):
            evaluator.evaluate("0x10")


class ScientificAndBoundaryRegressionTests(unittest.TestCase):
    def test_degree_tangent_rejects_large_odd_quarter_turns(self):
        with self.assertRaisesRegex(CalculationError, "正切.*无定义"):
            SafeEvaluator("DEG").evaluate("tan(18090)")

    def test_trigonometric_quadrant_values_do_not_leak_float_noise(self):
        degree_evaluator = SafeEvaluator("DEG")
        radian_evaluator = SafeEvaluator("RAD")
        for expression in ("sin(180)", "cos(90)", "cos(270)"):
            with self.subTest(mode="DEG", expression=expression):
                self.assertEqual(degree_evaluator.evaluate(expression), 0.0)
        for expression in ("sin(pi)", "cos(pi/2)"):
            with self.subTest(mode="RAD", expression=expression):
                self.assertEqual(radian_evaluator.evaluate(expression), 0.0)
        self.assertNotEqual(degree_evaluator.evaluate("sin(1e-14)"), 0.0)
        self.assertNotEqual(radian_evaluator.evaluate("sin(1e-16)"), 0.0)

    def test_exactly_four_thousand_digit_integer_is_allowed(self):
        value = SafeEvaluator().evaluate("7^4733")
        self.assertEqual(value, 7**4733)
        self.assertEqual(len(str(value)), 4000)

    def test_oversized_integer_formatting_uses_calculation_error(self):
        with self.assertRaisesRegex(CalculationError, "超出可表示范围"):
            format_number(10**5000)

    def test_floating_power_overflow_reports_range_error(self):
        with self.assertRaisesRegex(CalculationError, "超出可表示范围"):
            SafeEvaluator().evaluate("10.0^400")

    def test_nonzero_float_underflow_reports_range_error(self):
        evaluator = SafeEvaluator()
        for expression in (
            "1e-400",
            "1e-300×1e-300",
            "1e-300÷1e300",
            "10^−400",
            "exp(−1000)",
        ):
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(CalculationError, "超出可表示范围"):
                    evaluator.evaluate(expression)
        self.assertEqual(evaluator.evaluate("0×1e-300"), 0.0)
        self.assertEqual(evaluator.evaluate("1e-300−1e-300"), 0.0)
        self.assertGreater(evaluator.evaluate("5e-324"), 0.0)

    def test_exact_large_integer_division_does_not_overflow_float(self):
        value = SafeEvaluator().evaluate("10^400÷10")
        self.assertEqual(value, 10**399)

    def test_huge_factorial_input_reports_function_limit(self):
        with self.assertRaisesRegex(CalculationError, "阶乘数值过大"):
            SafeEvaluator().evaluate("fact(10^400)")


class CalculatorStateRegressionTests(unittest.TestCase):
    def test_percent_uses_the_complete_last_operand(self):
        cases = {
            "200+.5": "201",
            "200+−10": "180",
            "200+(5+5)": "220",
            "200×−10": "-20",
            "200+1e-2": "200.02",
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                model = CalculatorModel(expression=expression)
                model.apply_percent()
                self.assertEqual(model.equals(), expected)

    def test_scientific_exponent_sign_is_not_treated_as_binary_operator(self):
        model = CalculatorModel(expression="1e−3")
        model.toggle_sign()
        self.assertEqual(model.equals(), "-0.001")

    def test_toggle_sign_preserves_adjacent_implicit_multiplication(self):
        model = CalculatorModel(expression="−(1)(−5)")
        model.toggle_sign()
        self.assertEqual(model.equals(), "-5")

    def test_repeated_percent_divides_by_one_hundred_each_time(self):
        model = CalculatorModel(expression="50")
        model.apply_percent()
        model.apply_percent()
        self.assertEqual(model.equals(), "0.005")

    def test_button_input_after_ascii_formula_tracks_current_operand(self):
        model = CalculatorModel(expression="1.2+3")
        model.input(".")
        model.input("5")
        self.assertEqual(model.equals(), "4.7")

        model = CalculatorModel(expression="1e3")
        model.input(".")
        self.assertEqual(model.equals(), "1000")

    def test_full_width_formula_supports_follow_up_model_actions(self):
        model = CalculatorModel(expression="１＋２．５")
        model.toggle_sign()
        self.assertEqual(model.equals(), "-1.5")

        model = CalculatorModel(expression="２×（３＋４")
        self.assertEqual(model.equals(), "14")

        model = CalculatorModel(expression="１＋")
        model.input("×")
        model.input("2")
        self.assertEqual(model.equals(), "2")

        model = CalculatorModel(expression="１＋２．")
        model.input(".")
        model.input("5")
        self.assertEqual(model.equals(), "3.5")

        model = CalculatorModel(expression="（２")
        model.input(")")
        self.assertEqual(model.expression, "（２)")

    def test_transformations_cannot_grow_expression_past_safety_limit(self):
        model = CalculatorModel(expression="1")
        with self.assertRaisesRegex(CalculationError, "表达式过长"):
            for _ in range(100):
                model.wrap("sqrt(")
        self.assertLessEqual(len(model.expression), 512)

    def test_large_result_continuation_uses_exact_value(self):
        model = CalculatorModel(expression="99999999999999999")
        self.assertEqual(model.equals(), "1e17")
        for token in ("−", "99999999999999999"):
            model.input(token)
        self.assertEqual(model.equals(), "0")

        model = CalculatorModel(expression="10^400")
        self.assertEqual(model.equals(), "1e400")
        for token in ("−", "10^400"):
            model.input(token)
        self.assertEqual(model.equals(), "0")

    def test_external_result_replacement_discards_stale_exact_value(self):
        model = CalculatorModel(expression="99999999999999999")
        model.equals()
        model.expression = "1e17"
        model.just_evaluated = False
        model.input("−")
        model.input("99999999999999998")
        self.assertEqual(model.equals(), "0")

    def test_memory_preserves_integer_precision_and_autocloses_parentheses(self):
        model = CalculatorModel(expression="9007199254740993")
        model.memory_action("M+")
        model.memory_action("MR")
        self.assertEqual(model.expression, "9007199254740993")

        model = CalculatorModel(expression="2×(3+4")
        model.memory_action("M+")
        self.assertEqual(model.memory, 14)

    def test_memory_overflow_is_wrapped_and_does_not_corrupt_memory(self):
        model = CalculatorModel(expression="1e308", memory=1e308)
        with self.assertRaisesRegex(CalculationError, "超出可表示范围"):
            model.memory_action("M+")
        self.assertEqual(model.memory, 1e308)


if __name__ == "__main__":
    unittest.main()
