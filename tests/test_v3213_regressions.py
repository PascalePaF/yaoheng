from __future__ import annotations

import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app_ui import FONT, CalculatorPage, DualConverterPage, MarketPage, YaohengApp
from calculator_core import CalculatorModel
from localization import tr
from rate_service import RateService, RateSnapshot
from settings_service import AppSettings, SettingsStore


class V3213StateAndNavigationTests(unittest.TestCase):
    def test_old_minimize_choice_is_migrated_to_unambiguous_exit(self):
        settings = SettingsStore.from_payload({"close_action": "minimize"})
        self.assertEqual(settings.close_action, "exit")
        self.assertEqual(SettingsStore.validate(AppSettings(close_action="minimize")).close_action, "exit")

    def test_show_page_debounces_settings_and_defers_page_refresh(self):
        app = object.__new__(YaohengApp)
        app.root = MagicMock()
        app.root._active_search_select = None
        app.settings = SimpleNamespace(remember_last_page=True, last_page="calculator")
        app.settings_store = MagicMock()
        app.pages = {"calculator": MagicMock(), "fiat": MagicMock()}
        app.nav_buttons = {}
        app.current_page = "calculator"
        app.history_open = False
        app._page_open_refresh_enabled = True
        app._persist_settings = MagicMock()
        app._queue_page_open_refresh = MagicMock()

        YaohengApp.show_page(app, "fiat")

        self.assertEqual(app.settings.last_page, "fiat")
        app.settings_store.schedule_save.assert_called_once_with(app.settings)
        app._persist_settings.assert_not_called()
        app._queue_page_open_refresh.assert_called_once_with("fiat")

    def test_rapid_navigation_coalesces_refresh_until_page_is_visible(self):
        app = object.__new__(YaohengApp)
        app.root = MagicMock()
        app.root.after.side_effect = ["job-one", "job-two"]
        app.page_open_refresh_job = None
        app.current_page = "market"
        app.exiting = False
        app._refresh_page_on_open = MagicMock()

        YaohengApp._queue_page_open_refresh(app, "fiat")
        YaohengApp._queue_page_open_refresh(app, "market")

        app.root.after_cancel.assert_called_once_with("job-one")
        callback = app.root.after.call_args_list[-1].args[1]
        callback()
        app._refresh_page_on_open.assert_called_once_with("market")
        self.assertIsNone(app.page_open_refresh_job)

    def test_refresh_indicators_keep_existing_rows_visible(self):
        converter = object.__new__(DualConverterPage)
        converter.refreshing = False
        converter.table = MagicMock()
        converter.refresh_button = MagicMock()

        DualConverterPage.begin_refresh(converter)

        converter.table.delete.assert_not_called()
        converter.refresh_button.configure.assert_called_once_with(text="正在刷新…", state="disabled")

        market = object.__new__(MarketPage)
        market.mode = "fiat"
        market.watch_table = MagicMock()
        market.price_var = MagicMock()
        market.change_var = MagicMock()
        market.refresh_button = MagicMock()

        MarketPage.begin_refresh(market)

        market.watch_table.delete.assert_not_called()
        market.price_var.set.assert_not_called()
        market.change_var.set.assert_not_called()

    def test_fiat_refresh_updates_mixed_dependencies_without_animation(self):
        app = object.__new__(YaohengApp)
        app.pages = {
            name: MagicMock()
            for name in ("exchange", "market_exchange", "fiat", "fiat_market", "crypto", "market")
        }
        snapshot = MagicMock()

        YaohengApp.apply_snapshot(app, snapshot, False, animated=False, section="fiat")

        for name in ("exchange", "market_exchange", "fiat", "fiat_market", "crypto", "market"):
            app.pages[name].apply_snapshot.assert_called_once_with(snapshot, False, False)

    def test_hidden_market_page_does_not_start_chart_reload(self):
        app = object.__new__(YaohengApp)
        page = object.__new__(MarketPage)
        page.visible = False
        page.apply_snapshot = MagicMock()
        app.pages = {"market": page}
        snapshot = MagicMock()

        YaohengApp.apply_snapshot(app, snapshot, False, animated=False, section="crypto")

        page.apply_snapshot.assert_called_once_with(
            snapshot, False, False, reload_chart=False
        )


class V3213CalculatorModelTests(unittest.TestCase):
    def test_button_number_replaces_result_but_operator_continues_it(self):
        model = CalculatorModel()
        for token in ("2", "+", "3"):
            model.input(token)
        self.assertEqual(model.equals(), "5")

        model.input("7")
        self.assertEqual(model.expression, "7")
        model.input("+")
        model.input("1")
        self.assertEqual(model.equals(), "8")

        model.input("×")
        model.input("2")
        self.assertEqual(model.equals(), "16")


class V3213RateBatchTests(unittest.TestCase):
    def test_exact_conversion_engine_is_reused_until_snapshot_changes(self):
        service = RateService.__new__(RateService)
        from threading import RLock

        service._state_lock = RLock()
        service._decimal_engine_snapshot = None
        service._decimal_engine = None
        service.snapshot = RateSnapshot(
            {"USD": 1.0, "CNY": 7.2, "JPY": 150.0},
            {"USD": "美元", "CNY": "人民币", "JPY": "日元"},
            {"USD": "fiat", "CNY": "fiat", "JPY": "fiat"},
            {},
            "first",
            rate_strings={"USD": "1", "CNY": "7.2", "JPY": "150"},
        )

        self.assertEqual(service.convert_exact("10", "USD", "CNY"), "72")
        first_engine = service._decimal_engine
        for _ in range(200):
            service.convert(1.0, "CNY", "JPY")
        self.assertIs(service._decimal_engine, first_engine)

        service.snapshot = RateSnapshot(
            {"USD": 1.0, "CNY": 7.3, "JPY": 151.0},
            {"USD": "美元", "CNY": "人民币", "JPY": "日元"},
            {"USD": "fiat", "CNY": "fiat", "JPY": "fiat"},
            {},
            "second",
            rate_strings={"USD": "1", "CNY": "7.3", "JPY": "151"},
        )
        self.assertEqual(service.convert_exact("10", "USD", "CNY"), "73")
        self.assertIsNot(service._decimal_engine, first_engine)


class V3213CalculatorTkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.page = CalculatorPage(self.root, lambda: None, lambda: None)

    def tearDown(self):
        page = getattr(self, "page", None)
        if page is not None:
            try:
                page.destroy()
            except tk.TclError:
                pass
        root = getattr(self, "root", None)
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def test_initial_and_post_result_keyboard_input_replace_display(self):
        event = SimpleNamespace(char="７", state=0, keysym="7")
        self.assertEqual(self.page._expression_keypress(event), "break")
        self.assertEqual(self.page.expression_var.get(), "7")

        self.page._insert_expression_token("+")
        self.page._insert_expression_token("3")
        self.page._evaluate_manual_expression()
        self.assertEqual(self.page.expression_var.get(), "10")
        self.assertTrue(self.page.model.just_evaluated)

        self.page.expression_entry.selection_range(0, tk.END)
        self.page._insert_expression_token("4")
        self.assertEqual(self.page.expression_var.get(), "4")
        self.page._insert_expression_token("+")
        self.page._insert_expression_token("6")
        self.page._evaluate_manual_expression()
        self.assertEqual(self.page.expression_var.get(), "10")

        self.page._insert_expression_token("×")
        self.assertEqual(self.page.expression_var.get(), "10×")

        self.page.expression_var.set("4+5")
        self.page._evaluate_manual_expression()
        function_event = SimpleNamespace(char="s", state=0, keysym="s")
        self.assertEqual(self.page._expression_keypress(function_event), "break")
        self.assertEqual(self.page.expression_var.get(), "s")

    def test_paste_replaces_result_and_cjk_text_uses_ui_font(self):
        self.page.expression_var.set("6×7")
        self.page._evaluate_manual_expression()
        self.root.clipboard_clear()
        self.root.clipboard_append("１２３")

        self.assertEqual(self.page._paste_expression(), "break")
        self.assertEqual(self.page.expression_var.get(), "123")
        self.assertIn(FONT, str(self.page.formula_label.cget("font")))
        self.assertTrue(all(FONT in str(label.cget("font")) for label in self.page.inline_history_labels))


class V3213SimplifiedChineseTests(unittest.TestCase):
    def test_simplified_source_language_and_no_mojibake(self):
        self.assertEqual(tr("计算器", "zh_CN"), "计算器")
        self.assertEqual(tr("设置", "zh_CN"), "设置")
        root = Path(__file__).resolve().parents[1]
        for name in ("app_ui.py", "exchange_page.py", "calculator_core.py", "settings_service.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("�", text, name)


if __name__ == "__main__":
    unittest.main()
