from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app_version import SETTINGS_SCHEMA_VERSION
from settings_service import (
    CONVERSION_HISTORY_LIMIT,
    CONVERSION_HISTORY_RETENTION_DAYS,
    DEFAULT_EXCHANGE_CURRENCIES,
    DEFAULT_SAVE_DEBOUNCE_MS,
    LOCAL_API_DEFAULT_PORT,
    LOCAL_API_LOOPBACK,
    AppSettings,
    SettingsStore,
)


def legacy_payload(**overrides):
    payload = {
        "theme": "light",
        "timezone": "UTC",
        "data_dir": "",
        "keep_data_with_app": True,
        "auto_refresh_enabled": False,
        "fiat_refresh_minutes": 120,
        "crypto_refresh_minutes": 15,
        "refresh_when_minimized": False,
        "close_action": "minimize",
        "startup_page": "market",
        "remember_last_page": True,
        "last_page": "crypto",
        "remember_window_geometry": True,
        "window_geometry": "1600x900-120+80",
        "default_calculator_mode": "professional",
        "remember_calculator_mode": True,
        "last_calculator_mode": "professional",
        "calculator_angle_mode": "RAD",
        "history_limit": 12,
        "retain_history": True,
        "calculator_history": [["1+1", "2"]],
        "copy_result_format": "formula",
        "cache_limit_mb": 256,
        "favorite_fiats": ["JPY"],
        "pinned_fiats": ["CNY"],
        "favorite_cryptos": ["BTC"],
        "pinned_cryptos": ["ETH"],
        "existing_extension": {"kept": True},
    }
    payload.update(overrides)
    return payload


class SettingsMigrationV2Tests(unittest.TestCase):
    def test_v1_migration_preserves_legacy_fields_and_creates_private_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            original_payload = legacy_payload()
            original_text = json.dumps(original_payload, ensure_ascii=False, indent=2)
            path.write_text(original_text, encoding="utf-8")
            store = SettingsStore(path)

            settings = store.load()

            self.assertTrue(store.last_load_migrated)
            self.assertEqual(settings.schema_version, SETTINGS_SCHEMA_VERSION)
            self.assertEqual(settings.theme, original_payload["theme"])
            self.assertEqual(settings.calculator_history, original_payload["calculator_history"])
            self.assertEqual(settings.favorite_cryptos, ["BTC"])
            self.assertEqual(settings._extra_fields["existing_extension"], {"kept": True})
            self.assertEqual(
                json.loads(store.pre_v2_backup_path.read_text(encoding="utf-8")),
                original_payload,
            )
            self.assertEqual(json.loads(store.backup_path.read_text(encoding="utf-8")), original_payload)
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], SETTINGS_SCHEMA_VERSION)
            self.assertEqual(migrated["existing_extension"], {"kept": True})

    def test_pre_v2_backup_is_never_overwritten_by_later_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            path.write_text(json.dumps(legacy_payload(theme="dark")), encoding="utf-8")
            store = SettingsStore(path)
            store.load()
            original_backup = store.pre_v2_backup_path.read_bytes()

            self.assertTrue(store.save(AppSettings(theme="light")))
            self.assertTrue(store.save(AppSettings(theme="dark")))

            self.assertEqual(store.pre_v2_backup_path.read_bytes(), original_backup)

    def test_corrupt_primary_can_recover_from_the_pre_v2_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            path.write_text(json.dumps(legacy_payload(theme="light")), encoding="utf-8")
            store = SettingsStore(path)
            store.load()
            path.write_text("{broken", encoding="utf-8")
            store.backup_path.write_text("{also-broken", encoding="utf-8")

            recovered = store.load()

            self.assertEqual(recovered.theme, "light")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 2)

    def test_future_schema_is_loaded_safely_but_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            future_payload = legacy_payload(schema_version=99, theme="light")
            original = json.dumps(future_payload, ensure_ascii=False, indent=2).encode("utf-8")
            path.write_bytes(original)
            store = SettingsStore(path)

            loaded = store.load()

            self.assertEqual(loaded.theme, "light")
            self.assertEqual(loaded.schema_version, 99)
            self.assertTrue(store.is_read_only)
            self.assertEqual(store.future_schema_version, 99)
            loaded.theme = "dark"
            self.assertFalse(store.save(loaded))
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(store.pre_v2_backup_path.exists())

    def test_save_detects_future_schema_even_without_a_prior_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            original = json.dumps({"schema_version": 3, "theme": "light"}).encode("utf-8")
            path.write_bytes(original)

            self.assertFalse(SettingsStore(path).save(AppSettings(theme="dark")))
            self.assertEqual(path.read_bytes(), original)


class SettingsPartitionAndPrivacyTests(unittest.TestCase):
    def test_exchange_defaults_have_seven_unique_currencies_and_one_primary_slot(self):
        exchange = AppSettings().pages["exchange"]
        self.assertEqual(tuple(exchange["currencies"]), DEFAULT_EXCHANGE_CURRENCIES)
        self.assertEqual(len(set(exchange["currencies"])), 7)
        self.assertIsInstance(exchange["primary_slot"], int)
        self.assertEqual(exchange["primary_slot"], 0)
        self.assertIsInstance(exchange["amount"], str)
        self.assertEqual(exchange["mode"], "market")
        self.assertEqual(exchange["provider"], "auto")

    def test_one_corrupt_page_resets_only_that_page(self):
        settings = SettingsStore.from_payload({
            "schema_version": 2,
            "pages": {
                "exchange": {
                    "currencies": ["USD"] * 7,
                    "primary_slot": 10,
                    "amount": "NaN",
                    "mode": "broken",
                    "provider": "AUTO!",
                },
                "market": {"chart_days": 30, "selected": "BTC"},
                "settings": {"section": "storage"},
            },
        })

        self.assertEqual(tuple(settings.pages["exchange"]["currencies"]), DEFAULT_EXCHANGE_CURRENCIES)
        self.assertEqual(settings.pages["market"], {"chart_days": 30, "selected": "BTC"})
        self.assertEqual(settings.pages["settings"], {"section": "storage"})

    def test_nonfinite_value_resets_just_its_page_partition(self):
        settings = SettingsStore.from_payload({
            "schema_version": 2,
            "pages": {
                "market": {"selected": "BTC", "bad": math.nan},
                "settings": {"section": "appearance"},
            },
        })
        self.assertEqual(settings.pages["market"], {})
        self.assertEqual(settings.pages["settings"], {"section": "appearance"})

    def test_legacy_page_state_is_mirrored_without_breaking_flat_ui_fields(self):
        settings = SettingsStore.from_payload(legacy_payload())
        self.assertEqual(settings.pages["calculator"]["angle_mode"], "RAD")
        self.assertEqual(settings.pages["calculator"]["history_limit"], 12)
        self.assertEqual(settings.pages["fiat"]["favorites"], ["JPY"])
        self.assertEqual(settings.pages["crypto"]["pinned"], ["ETH"])

        settings.history_limit = 44
        settings.favorite_fiats = ["USD"]
        SettingsStore.validate(settings)
        self.assertEqual(settings.pages["calculator"]["history_limit"], 44)
        self.assertEqual(settings.pages["fiat"]["favorites"], ["USD"])

    def test_exchange_partition_does_not_become_a_317_navigation_target(self):
        settings = SettingsStore.from_payload({
            "schema_version": 2,
            "startup_page": "exchange",
            "last_page": "exchange",
        })
        self.assertEqual(settings.startup_page, "calculator")
        self.assertEqual(settings.last_page, "calculator")
        self.assertIn("exchange", settings.pages)

    def test_history_and_local_api_defaults_are_private_and_safe(self):
        settings = AppSettings()
        self.assertEqual(settings.conversion_history, {
            "enabled": False,
            "limit": CONVERSION_HISTORY_LIMIT,
            "retention_days": CONVERSION_HISTORY_RETENTION_DAYS,
            "entries": [],
        })
        self.assertEqual(settings.local_api, {
            "enabled": False,
            "host": LOCAL_API_LOOPBACK,
            "port": LOCAL_API_DEFAULT_PORT,
        })

    def test_tokens_c2c_identity_and_credentials_never_reach_export(self):
        now = datetime.now(timezone.utc).isoformat()
        settings = SettingsStore.from_payload({
            "schema_version": 2,
            "oauth_token": "top-secret-token",
            "local_api": {
                "enabled": True,
                "host": "0.0.0.0",
                "port": 17891,
                "token": "local-token",
                "token_hash": "local-hash",
            },
            "conversion_history": {
                "enabled": True,
                "limit": 500,
                "retention_days": 999,
                "entries": [{
                    "timestamp": now,
                    "source": "usd",
                    "target": "cny",
                    "amount": "01.2300",
                    "result": "8.9000",
                    "mode": "c2c",
                    "provider": "auto",
                    "advertisement_id": "ad-1",
                    "merchant": "merchant-name",
                    "payment_identity": "bank-account",
                    "credential": "credential-value",
                }],
            },
            "pages": {
                "exchange": {
                    "currencies": list(DEFAULT_EXCHANGE_CURRENCIES),
                    "primary_slot": 0,
                    "amount": "2.5000",
                    "mode": "market",
                    "provider": "auto",
                    "merchant": "must-not-persist",
                }
            },
        })
        payload = SettingsStore.to_payload(settings)
        encoded = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["local_api"], {
            "enabled": True,
            "host": LOCAL_API_LOOPBACK,
            "port": 17891,
        })
        self.assertEqual(payload["conversion_history"]["limit"], 50)
        self.assertEqual(payload["conversion_history"]["retention_days"], 30)
        self.assertEqual(payload["conversion_history"]["entries"][0]["amount"], "1.23")
        self.assertNotIn("top-secret-token", encoded)
        self.assertNotIn("local-token", encoded)
        self.assertNotIn("local-hash", encoded)
        self.assertNotIn("ad-1", encoded)
        self.assertNotIn("merchant-name", encoded)
        self.assertNotIn("bank-account", encoded)
        self.assertNotIn("credential-value", encoded)

    def test_history_is_empty_when_disabled_and_prunes_expired_entries(self):
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        base_entry = {
            "timestamp": old,
            "source": "USD",
            "target": "CNY",
            "amount": "1",
            "result": "7.2",
        }
        disabled = SettingsStore.from_payload({
            "conversion_history": {"enabled": False, "entries": [base_entry]},
        })
        enabled = SettingsStore.from_payload({
            "conversion_history": {"enabled": True, "entries": [base_entry]},
        })
        self.assertEqual(disabled.conversion_history["entries"], [])
        self.assertEqual(enabled.conversion_history["entries"], [])

    def test_debounced_interface_can_flush_the_latest_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            store = SettingsStore(path)
            first = AppSettings(theme="dark")
            second = AppSettings(theme="light")

            store.schedule_save(first, delay_ms=DEFAULT_SAVE_DEBOUNCE_MS)
            store.schedule_save(second, delay_ms=DEFAULT_SAVE_DEBOUNCE_MS)
            self.assertTrue(store.flush_pending_save())

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["theme"], "light")
            self.assertTrue(store.flush_pending_save())

    def test_v2_example_is_valid_and_contains_no_private_fields(self):
        example_path = Path(__file__).resolve().parents[1] / "app_settings.example.json"
        payload = json.loads(example_path.read_text(encoding="utf-8"))
        settings = SettingsStore.from_payload(payload)
        encoded = json.dumps(payload).casefold()

        self.assertEqual(settings.schema_version, 2)
        self.assertEqual(settings.local_api["host"], LOCAL_API_LOOPBACK)
        self.assertNotIn("token", encoded)
        self.assertNotIn("merchant", encoded)
        self.assertNotIn("credential", encoded)


if __name__ == "__main__":
    unittest.main()
