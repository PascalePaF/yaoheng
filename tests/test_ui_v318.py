from __future__ import annotations

import gc
import json
import socket
import tempfile
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from types import MethodType
from unittest.mock import patch

from app_ui import AppC2CService, DualConverterPage, MarketPage, SettingsPage, YaohengApp
from command_service import CommandService
from exchange_page import DEFAULT_EXCHANGE_CURRENCIES, ExchangePage, ExchangePageState
from local_api import LocalAPIPortInUseError
from rate_service import RateService, RateSnapshot
from secret_store import SecretStore
from settings_service import AppSettings, SettingsStore


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _snapshot() -> RateSnapshot:
    strings = {
        "USD": "1",
        "CNY": "7.1",
        "EUR": "0.91",
        "JPY": "150",
        "HKD": "7.8",
        "BTC": "0.00002",
        "USDT": "1",
        "ETH": "0.0005",
    }
    kinds = {
        code: ("crypto" if code in {"BTC", "USDT", "ETH"} else "fiat")
        for code in strings
    }
    return RateSnapshot(
        rates={code: float(value) for code, value in strings.items()},
        rate_strings=strings,
        names={code: code for code in strings},
        kinds=kinds,
        changes={code: 0.0 for code in strings},
        fetched_at=datetime.now().astimezone().isoformat(),
        errors=[],
        coin_ids={"BTC": "bitcoin", "USDT": "tether", "ETH": "ethereum"},
    )


class ExchangeStatePersistenceTests(unittest.TestCase):
    def test_transient_invalid_amount_keeps_last_valid_persisted_amount(self):
        state = ExchangePageState(amount="12.5000")

        state.set_amount(".")

        self.assertEqual(state.amount, ".")
        self.assertEqual(state.to_dict()["amount"], "12.5")

        state.set_amount("")
        self.assertEqual(state.to_dict()["amount"], "")

    def test_exchange_settings_invalid_fields_fall_back_independently(self):
        settings = SettingsStore.from_payload({
            "schema_version": 2,
            "pages": {
                "exchange": {
                    "currencies": list(DEFAULT_EXCHANGE_CURRENCIES),
                    "primary_slot": 3,
                    "amount": "25",
                    "mode": "c2c",
                    "provider": "not-a-provider",
                    "payment_method": "WIRE_X",
                }
            },
        })

        exchange = settings.pages["exchange"]
        self.assertEqual(exchange["primary_slot"], 3)
        self.assertEqual(exchange["amount"], "25")
        self.assertEqual(exchange["mode"], "c2c")
        self.assertEqual(exchange["provider"], "auto")
        self.assertEqual(exchange["payment_method"], "WIRE_X")


class LocalAPIIntegrationTests(unittest.TestCase):
    def _make_app(self, directory: str) -> YaohengApp:
        app = YaohengApp.__new__(YaohengApp)
        app.settings_store = SettingsStore(Path(directory) / "app_settings.json")
        app.settings = AppSettings(
            keep_data_with_app=False,
            data_dir=directory,
            auto_refresh_enabled=False,
        )
        app.settings.local_api = {
            "enabled": False,
            "host": "127.0.0.1",
            "port": _free_loopback_port(),
        }
        app.pages = {}
        app.persistence_warning_shown = False
        app.local_api_command = CommandService()
        app.api_security_warnings = []
        app.local_api_last_error = ""
        app.secret_store = SecretStore(
            app.settings_store.path.parent / "private" / "local_api_token.json",
            warning_callback=app._record_api_security_warning,
        )
        app.local_api_server = None
        app.exiting = False
        return app

    def test_enable_health_disable_and_settings_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._make_app(directory)
            token = app.issue_local_api_token("generate")
            self.addCleanup(lambda: app._stop_local_api() if app.local_api_server else None)

            self.assertTrue(app.secret_store.verify(token))
            self.assertIn("运行中", app.set_local_api_enabled(True))
            self.assertIsNotNone(app.local_api_server)
            self.assertIn("连接成功", app.test_local_api_connection())
            self.assertIn("不验证令牌", app.test_local_api_connection())
            self.assertIn("已关闭", app.set_local_api_enabled(False))
            self.assertIsNone(app.local_api_server)

            payload = json.loads(app.settings_store.path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload["local_api"]), {"enabled", "host", "port"})
            self.assertNotIn(token, json.dumps(payload, ensure_ascii=False))

    def test_new_token_is_not_lost_when_listener_start_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._make_app(directory)
            app.settings.local_api["enabled"] = True

            def fail_start(_self: YaohengApp) -> int:
                raise LocalAPIPortInUseError("fixture conflict")

            app._start_local_api = MethodType(fail_start, app)
            token = app.issue_local_api_token("generate")

            self.assertTrue(token)
            self.assertTrue(app.secret_store.verify(token))
            self.assertIn("令牌已更新", app.local_api_last_error)
            self.assertIn("被占用", app.local_api_last_error)

    def test_invalid_public_port_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._make_app(directory)
            original = app.settings.local_api["port"]

            with self.assertRaises(ValueError):
                app.set_local_api_port(70000)

            self.assertEqual(app.settings.local_api["port"], original)


class FullWindowLifecycleSmokeTests(unittest.TestCase):
    def test_exchange_crypto_and_settings_pages_share_one_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "app_settings.json")
            settings = AppSettings(
                keep_data_with_app=False,
                data_dir=directory,
                auto_refresh_enabled=False,
                remember_window_geometry=False,
            )
            # Keep this lifecycle smoke test fully offline.  OKX stays fail-closed
            # until an approved merchant API contract is configured, so showing
            # the C2C pages cannot leave real HTTP workers behind after Tk exits.
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

                snapshot = _snapshot()
                app.service.snapshot = snapshot
                exchange = app.pages["exchange"]
                fiat = app.pages["fiat"]
                crypto = app.pages["crypto"]
                market = app.pages["market"]
                self.assertIsInstance(exchange, ExchangePage)
                self.assertIsInstance(fiat, DualConverterPage)
                self.assertIsInstance(crypto, DualConverterPage)
                self.assertIsInstance(market, MarketPage)

                exchange.apply_snapshot(snapshot)
                fiat.apply_snapshot(snapshot)
                crypto.apply_snapshot(snapshot)
                market.apply_snapshot(snapshot, reload_chart=False)
                app.show_page("exchange")
                app.root.update_idletasks()
                primary_entry = exchange.primary_entry

                self.assertTrue(exchange.visible)
                self.assertEqual(len(exchange.card_widgets), 7)
                self.assertIsNotNone(primary_entry)
                exchange.recalculate_now()
                self.assertIs(exchange.primary_entry, primary_entry)

                app.show_page("crypto")
                app.root.update_idletasks()
                self.assertFalse(exchange.visible)
                self.assertTrue(crypto.visible)
                self.assertIsNotNone(crypto.provider_combo)
                self.assertIsNotNone(crypto.payment_combo)

                app.show_page("settings")
                app.root.update_idletasks()
                self.assertFalse(crypto.visible)
                self.assertIsInstance(app.pages["settings"], SettingsPage)
                self.assertIn("exchange", app.nav_buttons)
                self.assertIsNone(app.local_api_server)
                self.assertIs(app.exchange_coordinator.rate_service, app.service)
                self.assertIs(app.exchange_coordinator.c2c_service, app.c2c_service)
                self.assertIsInstance(app.c2c_service, AppC2CService)
                self.assertIs(app.local_api_command._conversion, app.service)

                market.current_code = "ETH"
                market.current_days = 30
                market.reference_amount_var.set("2.5")
                market.market_search_var.set("eth")
                market.flush_state()
                app.settings_store.flush_pending_save()
                restored = app.settings_store.load().pages["market"]
                self.assertEqual(restored["selected_code"], "ETH")
                self.assertEqual(restored["days"], 30)
                self.assertEqual(restored["reference_amount"], "2.5")
                self.assertEqual(restored["search"], "eth")
            finally:
                if app is not None and not app.exiting:
                    app.force_exit()
                app = None
                gc.collect()


if __name__ == "__main__":
    unittest.main()
