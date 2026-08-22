from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app_ui import (
    NETWORK_STATUS_PALETTES,
    CalculatorPage,
    YaohengApp,
    connection_status_state,
)
from settings_service import AppSettings, SettingsStore
from theme_catalog import THEMES, contrast_ratio


class V3212StateTests(unittest.TestCase):
    def test_legacy_crypto_c2c_selection_migrates_to_market_rates(self):
        settings = SettingsStore.from_payload({
            "schema_version": 3,
            "pages": {
                "crypto": {
                    "currencies": ["CNY", "BTC", "ETH"],
                    "mode": "c2c",
                    "provider": "okx",
                    "payment_method": "WIRE_X",
                }
            },
        })

        self.assertEqual(settings.pages["crypto"]["mode"], "market")
        self.assertEqual(settings.pages["crypto"]["provider"], "auto")
        self.assertEqual(settings.pages["crypto"]["payment_method"], "")

    def test_connection_semantics_and_colors_are_theme_independent(self):
        self.assertIs(connection_status_state("当前网络连接成功！"), True)
        self.assertIs(connection_status_state("连接失败：超时"), False)
        self.assertEqual(connection_status_state("部分数据已更新"), "partial")
        self.assertIsNone(connection_status_state("正在重新连接"))
        for state, (background, foreground) in NETWORK_STATUS_PALETTES.items():
            with self.subTest(state=state):
                self.assertGreaterEqual(contrast_ratio(background, foreground), 4.5)
                self.assertTrue(all(
                    (background, foreground) != (palette["accent_dark"], palette["accent"])
                    for palette in THEMES.values()
                ))

    def test_every_rate_page_open_maps_to_one_refresh_scope(self):
        app = object.__new__(YaohengApp)
        app.exiting = False
        app.startup_job = "startup"
        app.root = MagicMock()
        app.refresh_rates = MagicMock()

        expected = {
            "exchange": "all",
            "market_exchange": "all",
            "fiat": "fiat",
            "fiat_market": "fiat",
            "crypto": "crypto",
            "market": "crypto",
        }
        for page, section in expected.items():
            with self.subTest(page=page):
                YaohengApp._refresh_page_on_open(app, page)
                app.refresh_rates.assert_called_once_with(section)
                app.refresh_rates.reset_mock()

        app.root.after_cancel.assert_called_once_with("startup")
        self.assertIsNone(app.startup_job)

    def test_previous_and_next_theme_wrap_in_both_directions(self):
        order = tuple(THEMES)
        app = object.__new__(YaohengApp)
        app.settings = SimpleNamespace(theme=order[0])
        app.set_theme = MagicMock()

        YaohengApp.cycle_theme(app, -1)
        app.set_theme.assert_called_once_with(order[-1])
        app.set_theme.reset_mock()
        app.settings.theme = order[-1]
        YaohengApp.cycle_theme(app, 1)
        app.set_theme.assert_called_once_with(order[0])


class V3212CalculatorTkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")

    def tearDown(self):
        root = getattr(self, "root", None)
        if root is not None:
            try:
                root.update_idletasks()
                root.destroy()
            except tk.TclError:
                pass

    def test_large_editable_result_formula_two_prior_rows_and_durable_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "app_settings.json")
            settings = AppSettings(retain_history=True, history_limit=30)
            self.assertTrue(store.save(settings))

            app = object.__new__(YaohengApp)
            app.settings = settings
            app.settings_store = store
            app.persistence_warning_shown = False
            page = CalculatorPage(self.root, lambda: None, lambda: None)
            app.pages = {"calculator": page}
            page.history_changed = lambda: YaohengApp.calculator_history_changed(app)

            for expression in ("2+3", "10÷2", "7×8"):
                page.expression_var.set(expression)
                page._evaluate_manual_expression()

            self.assertEqual(page.expression_entry.grid_info()["row"], 3)
            self.assertEqual(page.formula_label.grid_info()["row"], 2)
            self.assertEqual(page.expression_var.get(), "56")
            self.assertEqual(page.formula_var.get(), "7×8 =")
            self.assertEqual(
                [variable.get() for variable in page.inline_history_vars],
                ["2+3 = 5", "10÷2 = 5"],
            )

            page._resize_stage(SimpleNamespace(width=1100, height=720))
            self.assertEqual(int(float(page.stage.place_info()["width"])), 1092)
            self.assertGreaterEqual(int(str(page.expression_entry.cget("font")).split()[-1]), 30)
            page.set_mode_immediate(True)
            page._resize_stage(SimpleNamespace(width=920, height=620))
            self.assertEqual(int(float(page.stage.place_info()["width"])), 912)

            restored = store.load()
            self.assertEqual(restored.calculator_history[0], ["7×8", "56"])
            self.assertEqual(len(restored.calculator_history), 3)
            page.destroy()


if __name__ == "__main__":
    unittest.main()
