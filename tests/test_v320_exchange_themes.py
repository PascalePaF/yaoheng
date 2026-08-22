from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app_ui import COLORS, THEMES, THEME_LABELS, YaohengApp
from c2c.models import Direction
from exchange_page import (
    C2CBridgeExecution,
    C2CBridgeJob,
    C2CQuoteJob,
    ExchangeCoordinator,
    ExchangePage,
    ExchangePageState,
)
from conversion_core import DecimalConversionEngine
from rate_service import RateSnapshot
from settings_service import AppSettings, SettingsStore


KINDS = {
    "CNY": "fiat",
    "USD": "fiat",
    "EUR": "fiat",
    "JPY": "fiat",
    "HKD": "fiat",
    "BTC": "crypto",
    "USDT": "crypto",
    "ETH": "crypto",
}
RATES = {
    "CNY": "7.1",
    "USD": "1",
    "EUR": "0.91",
    "JPY": "150",
    "HKD": "7.8",
    "BTC": "0.00002",
    "USDT": "1",
    "ETH": "0.0005",
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
        coin_ids={"BTC": "bitcoin", "USDT": "tether", "ETH": "ethereum"},
    )


class SevenInputStateTests(unittest.TestCase):
    def test_any_slot_can_become_the_input_without_moving_the_primary_row(self):
        state = ExchangePageState(primary_slot=0, amount="1", mode="market")

        self.assertTrue(state.set_input(5, "2.5"))

        self.assertEqual(state.primary_slot, 0)
        self.assertEqual(state.primary_code, "CNY")
        self.assertEqual(state.active_slot, 5)
        self.assertEqual(state.active_code, "BTC")
        self.assertEqual(state.amount, "2.5")
        self.assertEqual(set(state.quote_slots), {0, 1, 2, 3, 4, 6})
        self.assertEqual(state.to_dict()["active_slot"], 5)

    def test_market_quote_uses_the_actual_edited_slot_as_source(self):
        state = ExchangePageState(active_slot=5, amount="2", mode="market")
        coordinator = ExchangeCoordinator(DecimalConversionEngine(RATES, KINDS))

        immediate, jobs = coordinator.quote_edges(state, KINDS)

        self.assertEqual(jobs, ())
        self.assertEqual({result.slot for result in immediate}, {0, 1, 2, 3, 4, 6})
        self.assertTrue(all(result.source == "BTC" for result in immediate))
        self.assertTrue(all(result.valid for result in immediate))


class C2CSeparationTests(unittest.TestCase):
    def test_direct_c2c_never_silently_falls_back_to_market(self):
        coordinator = ExchangeCoordinator(DecimalConversionEngine(RATES, KINDS))

        prepared = coordinator.prepare_edge(
            slot=5,
            generation=1,
            amount="1000",
            source="CNY",
            target="BTC",
            kinds=KINDS,
            mode="c2c",
            provider="binance",
            payment_fiat="CNY",
            settlement_fiat="CNY",
        )

        self.assertIsInstance(prepared, C2CQuoteJob)
        self.assertIs(prepared.request.direction, Direction.BUY)
        self.assertFalse(prepared.request.allow_market_fallback)

    def test_crypto_to_crypto_uses_two_c2c_legs_through_selected_fiat(self):
        calls = []

        class FakeC2C:
            def quote(self, request, *, cancel=None):
                del cancel
                calls.append(request)
                if request.direction is Direction.SELL:
                    return {
                        "provider": "binance",
                        "status": "ok",
                        "data_state": "live",
                        "match": {"price": "500000", "output_amount": "500000"},
                    }
                return {
                    "provider": "binance",
                    "status": "ok",
                    "data_state": "live",
                    "match": {"price": "25000", "output_amount": "20"},
                }

        coordinator = ExchangeCoordinator(DecimalConversionEngine(RATES, KINDS), FakeC2C())
        prepared = coordinator.prepare_edge(
            slot=6,
            generation=4,
            amount="1",
            source="BTC",
            target="USDT",
            kinds=KINDS,
            mode="c2c",
            provider="binance",
            payment_fiat="CNY",
            settlement_fiat="CNY",
        )
        self.assertIsInstance(prepared, C2CBridgeJob)

        execution = coordinator.execute_job(prepared)
        result = coordinator.finish_job(prepared, execution, kinds=KINDS)

        self.assertIsInstance(execution, C2CBridgeExecution)
        self.assertEqual([request.direction for request in calls], [Direction.SELL, Direction.BUY])
        self.assertTrue(all(not request.allow_market_fallback for request in calls))
        self.assertEqual(result.exact_value, "20")
        self.assertIn("双段", result.status)
        self.assertIn("BTC → CNY → USDT", result.details[0])


class V320SettingsTests(unittest.TestCase):
    def test_v319_exchange_state_migrates_to_two_independent_pages(self):
        legacy = AppSettings(
            pages={
                "exchange": {
                    "currencies": ["JPY", "USD", "EUR", "CNY", "HKD", "BTC", "ETH"],
                    "primary_slot": 3,
                    "active_slot": 5,
                    "amount": "0.25",
                    "mode": "market",
                    "provider": "binance",
                    "settlement_fiat": "JPY",
                }
            }
        )

        validated = SettingsStore.validate(legacy)

        self.assertEqual(validated.pages["exchange"]["mode"], "c2c")
        self.assertEqual(validated.pages["market_exchange"]["mode"], "market")
        for page in ("exchange", "market_exchange"):
            self.assertEqual(validated.pages[page]["currencies"][0], "JPY")
            self.assertEqual(validated.pages[page]["active_slot"], 5)
            self.assertEqual(validated.pages[page]["amount"], "0.25")

    def test_fiat_crypto_and_both_exchange_pages_round_trip_last_selections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            store = SettingsStore(path)
            settings = AppSettings()
            settings.pages["fiat"].update({
                "currencies": ["JPY", "CNY", "USD"], "active_side": "b", "amounts": ["", "88", ""],
            })
            settings.pages["crypto"].update({
                "currencies": ["CNY", "ETH", "USDT"], "active_side": "c", "amounts": ["", "", "25"],
            })
            settings.pages["exchange"].update({"currencies": ["CNY", "JPY", "USD", "EUR", "HKD", "ETH", "BTC"]})
            settings.pages["market_exchange"].update({"currencies": ["USD", "CNY", "EUR", "JPY", "HKD", "BTC", "USDT"]})

            self.assertTrue(store.save(settings))
            loaded = store.load()

            self.assertEqual(loaded.pages["fiat"]["currencies"], ["JPY", "CNY", "USD"])
            self.assertEqual(loaded.pages["crypto"]["currencies"], ["CNY", "ETH", "USDT"])
            self.assertEqual(loaded.pages["exchange"]["currencies"][5:], ["ETH", "BTC"])
            self.assertEqual(loaded.pages["market_exchange"]["currencies"][:2], ["USD", "CNY"])

    def test_all_theme_names_and_palette_roles_are_valid(self):
        required = set(THEMES["dark"])
        self.assertEqual(set(THEME_LABELS), set(THEMES))
        for name, palette in THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(set(palette), required)
                settings = SettingsStore.validate(AppSettings(theme=name))
                self.assertEqual(settings.theme, name)


class V320TkTests(unittest.TestCase):
    def test_two_pages_seven_editable_amounts_and_fast_theme_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "app_settings.json")
            settings = AppSettings(data_dir=directory, auto_refresh_enabled=False)
            settings.pages["exchange"]["provider"] = "okx"
            settings.pages["crypto"]["provider"] = "okx"
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
                current = snapshot()
                app.service.snapshot = current
                app.apply_snapshot(current, False)

                c2c_page = app.pages["exchange"]
                market_page = app.pages["market_exchange"]
                self.assertIsInstance(c2c_page, ExchangePage)
                self.assertIsInstance(market_page, ExchangePage)
                self.assertEqual(c2c_page.state.mode, "c2c")
                self.assertEqual(market_page.state.mode, "market")

                app.show_page("market_exchange")
                app.root.update_idletasks()
                self.assertEqual(len(market_page.amount_entries), 7)
                self.assertTrue(all(isinstance(entry, tk.Entry) for entry in market_page.amount_entries.values()))
                market_page.amount_vars[5].set("(2+3)")
                market_page._commit_amount_expression(slot=5)
                self.assertEqual(market_page.state.active_slot, 5)
                self.assertEqual(market_page.state.amount, "5")
                self.assertTrue(all(market_page.amount_vars[slot].get() != "—" for slot in market_page.state.quote_slots))

                for theme in ("ocean", "forest", "plum", "light", "dark"):
                    app.set_theme(theme)
                    self.assertEqual(dict(COLORS), THEMES[theme])
                    self.assertLess(app.last_theme_switch_ms, 100)
            finally:
                if app is not None and not app.exiting:
                    app.force_exit()


if __name__ == "__main__":
    unittest.main()
