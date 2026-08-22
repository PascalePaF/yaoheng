from __future__ import annotations

import tkinter as tk
import unittest
from datetime import datetime
from types import SimpleNamespace

from app_ui import CalculatorPage, THEMES, THEME_LABELS, YaohengApp
from conversion_core import DecimalConversionEngine
from exchange_page import ExchangeCoordinator, ExchangePage, ExchangePageState
from rate_service import RateSnapshot
from settings_service import AppSettings, SettingsStore
from theme_catalog import contrast_ratio


KINDS = {
    "CNY": "fiat", "USD": "fiat", "EUR": "fiat", "JPY": "fiat",
    "HKD": "fiat", "BTC": "crypto", "USDT": "crypto",
}
RATES = {
    "CNY": "7.1", "USD": "1", "EUR": "0.91", "JPY": "150",
    "HKD": "7.8", "BTC": "0.00002", "USDT": "1",
}


def snapshot() -> RateSnapshot:
    return RateSnapshot(
        rates={code: float(value) for code, value in RATES.items()},
        rate_strings=dict(RATES),
        names={code: code for code in RATES},
        kinds=dict(KINDS),
        changes={code: 0.0 for code in RATES},
        fetched_at=datetime.now().astimezone().isoformat(),
        errors=[],
        coin_ids={"BTC": "bitcoin", "USDT": "tether"},
    )


class ComfortableThemeTests(unittest.TestCase):
    def test_at_least_twenty_complete_readable_palettes(self):
        self.assertGreaterEqual(len(THEMES), 20)
        self.assertEqual(set(THEMES), set(THEME_LABELS))
        roles = set(THEMES["dark"])
        for name, palette in THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(set(palette), roles)
                self.assertEqual(SettingsStore.validate(AppSettings(theme=name)).theme, name)
                self.assertGreaterEqual(contrast_ratio(palette["text"], palette["bg"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["text"], palette["card"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["muted"], palette["card"]), 3.0)
                self.assertGreaterEqual(contrast_ratio(palette["on_accent"], palette["accent"]), 4.5)
                self.assertGreaterEqual(
                    contrast_ratio(palette["selection_text"], palette["selection"]), 4.5
                )

    def test_large_surfaces_are_not_pure_black_or_white(self):
        for name, palette in THEMES.items():
            with self.subTest(theme=name):
                for role in ("bg", "sidebar", "card", "card_alt"):
                    self.assertNotIn(palette[role].upper(), {"#000000", "#FFFFFF"})


class KeyboardRegressionTests(unittest.TestCase):
    def test_numeric_keypad_digits_and_operators_are_mapped(self):
        for digit in "0123456789":
            with self.subTest(digit=digit):
                self.assertEqual(
                    YaohengApp._calculator_key(
                        SimpleNamespace(char="", keysym=f"KP_{digit}", state=0)
                    ),
                    digit,
                )
        self.assertEqual(
            YaohengApp._calculator_key(SimpleNamespace(char="", keysym="KP_Add", state=0)),
            "+",
        )

    def test_expression_entry_accepts_keypad_and_enter_calculates(self):
        root: tk.Tk | None = None
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        try:
            page = CalculatorPage(root, lambda: None, lambda: None)
            page.expression_var.set("")
            page.expression_entry.icursor(0)
            self.assertEqual(
                page._expression_keypress(
                    SimpleNamespace(char="", keysym="KP_7", state=0)
                ),
                "break",
            )
            self.assertEqual(page.expression_var.get(), "7")
            page.expression_var.set("1+2*3")
            self.assertEqual(page._evaluate_manual_expression(), "break")
            self.assertEqual(page.result_var.get(), "7")
        finally:
            if root is not None:
                root.destroy()


class ExchangeCardStabilityTests(unittest.TestCase):
    def test_lower_amount_edit_and_primary_result_do_not_recreate_entries(self):
        root: tk.Tk | None = None
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        try:
            state = ExchangePageState(mode="market", primary_slot=0, active_slot=0, amount="1")
            page = ExchangePage(
                root,
                ExchangeCoordinator(DecimalConversionEngine(RATES, KINDS)),
                state,
                lambda: None,
                lambda _value: None,
                lambda value: value,
                colors=THEMES["dark"],
            )
            page.pack(fill="both", expand=True)
            page.apply_snapshot(snapshot())
            root.update_idletasks()
            original_entries = dict(page.amount_entries)
            edited = original_entries[5]
            edited.icursor(0)

            page.amount_vars[5].set("(2+3)")
            page._commit_amount_expression(slot=5)
            page._refresh_target_cards((state.primary_slot,))
            root.update_idletasks()

            self.assertEqual(state.active_slot, 5)
            self.assertEqual(state.amount, "5")
            self.assertEqual(page.amount_vars[5].get(), "5")
            for slot, entry in original_entries.items():
                self.assertIs(page.amount_entries[slot], entry)
                self.assertTrue(entry.winfo_exists())
        finally:
            if root is not None:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
