from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_ui import CalculatorPage, MarketPage, YaohengApp
from exchange_page import ExchangePage
from localization import (
    LANGUAGE_LABELS,
    SUPPORTED_LANGUAGES,
    localized_asset_name,
    normalize_language,
    set_language,
    timezone_display_name,
    timezone_search_text,
    tr,
)
from rate_service import RateSnapshot
from settings_service import AppSettings, SettingsStore
from theme_catalog import THEMES, contrast_ratio


def _snapshot() -> RateSnapshot:
    rates = {"CNY": 7.1, "USD": 1.0, "EUR": 0.91, "JPY": 150.0, "BTC": 0.00002, "USDT": 1.0}
    return RateSnapshot(
        rates=rates,
        rate_strings={code: str(value) for code, value in rates.items()},
        names={"CNY": "人民币", "USD": "美元", "EUR": "欧元", "JPY": "日元", "BTC": "比特币", "USDT": "泰达币"},
        kinds={"CNY": "fiat", "USD": "fiat", "EUR": "fiat", "JPY": "fiat", "BTC": "crypto", "USDT": "crypto"},
        changes={code: 0.0 for code in rates},
        fetched_at=datetime.now().astimezone().isoformat(),
        errors=[],
        coin_ids={"BTC": "bitcoin", "USDT": "tether"},
    )


class LocalizationV3211Tests(unittest.TestCase):
    def tearDown(self) -> None:
        set_language("zh_CN")

    def test_supported_languages_and_safe_settings_fallback(self):
        self.assertEqual(SUPPORTED_LANGUAGES, ("zh_CN", "zh_TW", "en_US", "ja_JP"))
        self.assertEqual(set(LANGUAGE_LABELS), set(SUPPORTED_LANGUAGES))
        self.assertEqual(normalize_language("en"), "en_US")
        self.assertEqual(normalize_language("ja-jp"), "ja_JP")
        self.assertEqual(SettingsStore.validate(AppSettings(language="invalid")).language, "zh_CN")

    def test_core_navigation_and_financial_explanations_are_translated(self):
        for language in ("en_US", "ja_JP"):
            with self.subTest(language=language):
                self.assertNotEqual(tr("计算器", language), "计算器")
                self.assertNotEqual(tr("设置", language), "设置")
                translated = tr("C2C 页面不使用普通虚拟币行情降级；法币↔虚拟币查单广告，虚拟币↔虚拟币经结算法币双段查询。", language)
                self.assertNotIn("页面不使用", translated)
                self.assertNotIn("查单广告", translated)

    def test_traditional_chinese_and_asset_names_do_not_change_machine_codes(self):
        self.assertEqual(tr("设置", "zh_TW"), "設定")
        self.assertEqual(tr("软件更新与缓存", "zh_TW"), "軟體更新與快取")
        self.assertEqual(localized_asset_name("CNY", "人民币", "en_US"), "Chinese Yuan")
        self.assertEqual(localized_asset_name("JPY", "日元", "ja_JP"), "日本円")
        self.assertEqual(localized_asset_name("XYZ", "未知币", "en_US"), "XYZ")

    def test_timezone_is_display_localized_but_chinese_search_is_always_available(self):
        self.assertIn("上海", timezone_search_text("Asia/Shanghai"))
        self.assertNotRegex(timezone_display_name("Asia/Shanghai", "en_US"), r"[\u3400-\u9fff]")
        self.assertIn("上海", timezone_display_name("Asia/Shanghai", "zh_CN"))


class ThemeSemanticsV3211Tests(unittest.TestCase):
    def test_rebuilt_catalog_has_explicit_text_mode_and_readable_semantic_roles(self):
        self.assertGreaterEqual(len(THEMES), 20)
        role_set = set(next(iter(THEMES.values())))
        self.assertGreaterEqual(len(role_set), 50)
        for name, palette in THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(set(palette), role_set)
                self.assertIn(palette["text"], {"#111111", "#FFFFFF"})
                for foreground, background, minimum in (
                    ("text", "bg", 4.5), ("text", "card", 4.5),
                    ("input_text", "input_bg", 4.5), ("button_text", "button_bg", 4.5),
                    ("calc_number_text", "calc_number_bg", 4.5),
                    ("calc_function_text", "calc_function_bg", 4.5),
                    ("calc_operator_text", "calc_operator_bg", 4.5),
                    ("selection_text", "selection", 4.5),
                ):
                    self.assertGreaterEqual(contrast_ratio(palette[foreground], palette[background]), minimum)


class FullWindowV3211TkTests(unittest.TestCase):
    def test_calculator_language_and_all_responsive_breakpoints_share_one_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "app_settings.json")
            settings = AppSettings(data_dir=directory, auto_refresh_enabled=False, language="zh_CN")
            store.save(settings)
            app: YaohengApp | None = None
            try:
                with patch("app_ui.SettingsStore", return_value=store):
                    app = YaohengApp()
            except tk.TclError as exc:
                self.skipTest(f"Tk display unavailable: {exc}")
            try:
                app.root.withdraw()
                for attr in ("startup_job", "api_start_job"):
                    job = getattr(app, attr, None)
                    if job:
                        app.root.after_cancel(job)
                        setattr(app, attr, None)
                current = _snapshot()
                app.service.snapshot = current
                app.apply_snapshot(current, False)
                app.root.update_idletasks()

                calculator = app.pages["calculator"]
                self.assertIsInstance(calculator, CalculatorPage)
                zero_key = next(key for key in calculator.keys if key.label == "0")
                self.assertEqual(int(zero_key.grid_info()["columnspan"]), 2)
                operator_labels = {key.label for key in calculator.keys if key.kind == "operator"}
                self.assertEqual(operator_labels, {"÷", "×", "−", "+", "="})

                app.show_page("calculator")
                calculator.handle("AC")
                event = SimpleNamespace(char="7", keysym="7", state=0)
                with patch.object(app.root, "focus_get", return_value=None):
                    self.assertEqual(app.on_key(event), "break")
                self.assertEqual(calculator.model.expression, "7")

                settings_page = app.pages["settings"]
                settings_row = int(app.nav_buttons["settings"].grid_info()["row"])
                theme_row = int(app.sidebar_theme_button.grid_info()["row"])
                self.assertGreater(theme_row, settings_row)

                app._responsive_window_changed(SimpleNamespace(widget=app.root, width=1000))
                self.assertEqual(int(app.sidebar.cget("width")), 76)
                app._responsive_window_changed(SimpleNamespace(widget=app.root, width=1400))
                self.assertEqual(int(app.sidebar.cget("width")), 216)

                exchange = app.pages["exchange"]
                self.assertIsInstance(exchange, ExchangePage)
                exchange._canvas_resized(SimpleNamespace(width=520))
                self.assertEqual(exchange._card_columns, 1)
                exchange._canvas_resized(SimpleNamespace(width=700))
                self.assertEqual(exchange._card_columns, 2)
                exchange._canvas_resized(SimpleNamespace(width=1000))
                self.assertEqual(exchange._card_columns, 3)

                settings_page._settings_canvas_resized(SimpleNamespace(width=700))
                self.assertTrue(all(int(card.grid_info()["column"]) == 0 for card, *_ in settings_page._settings_cards))
                settings_page._settings_canvas_resized(SimpleNamespace(width=1000))
                self.assertTrue(any(int(card.grid_info()["column"]) == 1 for card, *_ in settings_page._settings_cards))

                fiat = app.pages["fiat"]
                fiat._responsive_reference_controls(SimpleNamespace(width=500))
                self.assertEqual(int(fiat.reference_amount_entry.grid_info()["row"]), 1)
                fiat._responsive_reference_controls(SimpleNamespace(width=700))
                self.assertEqual(int(fiat.reference_amount_entry.grid_info()["row"]), 0)

                market = app.pages["fiat_market"]
                self.assertIsInstance(market, MarketPage)
                market._market_body_resized(SimpleNamespace(width=700))
                self.assertTrue(market.market_compact)
                market._market_body_resized(SimpleNamespace(width=1000))
                self.assertFalse(market.market_compact)

                app.set_language("en_US")
                app._responsive_window_changed(SimpleNamespace(widget=app.root, width=1400))
                self.assertIn("Calculator", app.nav_buttons["calculator"].cget("text"))
                self.assertEqual(settings_page.language_var.get(), "English")
                settings_page.timezone_combo._filter("北京")
                self.assertTrue(any("Asia/Shanghai" in value for value in settings_page.timezone_combo.filtered_values))
                self.assertNotRegex(exchange.code_to_display["CNY"], r"[\u3400-\u9fff]")

                app.set_language("ja_JP")
                self.assertIn("電卓", app.nav_buttons["calculator"].cget("text"))
                self.assertEqual(settings_page.language_var.get(), "日本語")
                app.set_language("zh_TW")
                self.assertIn("計算器", app.nav_buttons["calculator"].cget("text"))
            finally:
                set_language("zh_CN")
                if app is not None and not app.exiting:
                    app.force_exit()


if __name__ == "__main__":
    unittest.main()
