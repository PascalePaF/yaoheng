import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app_ui import MarketPage, SettingsPage, TkAfterJobs, TkResultBridge, YaohengApp, visible_window_position
from settings_service import MAX_SETTINGS_FILE_BYTES, AppSettings, SettingsStore, timezone_names


class FakeAfterOwner:
    """Small main-thread scheduler used to exercise TkResultBridge headlessly."""

    def __init__(self) -> None:
        self.main_thread = threading.get_ident()
        self.callbacks: dict[str, object] = {}
        self.cancelled: list[str] = []
        self.counter = 0

    def after(self, _delay: int, callback):
        if threading.get_ident() != self.main_thread:
            raise AssertionError("a worker thread touched the Tk scheduler")
        self.counter += 1
        job = f"job-{self.counter}"
        self.callbacks[job] = callback
        return job

    def after_cancel(self, job: str) -> None:
        if threading.get_ident() != self.main_thread:
            raise AssertionError("a worker thread touched the Tk scheduler")
        self.cancelled.append(job)
        self.callbacks.pop(job, None)

    def run_next(self) -> None:
        job, callback = next(iter(self.callbacks.items()))
        self.callbacks.pop(job)
        callback()


class TkTaskRegressionTests(unittest.TestCase):
    def test_owned_after_callbacks_are_cancelled_on_destroy(self):
        owner = FakeAfterOwner()
        called: list[bool] = []
        jobs = TkAfterJobs(owner)
        jobs.schedule(100, lambda: called.append(True))

        jobs.cancel_all()

        self.assertFalse(owner.callbacks)
        self.assertFalse(called)

    def test_worker_delivery_never_calls_tk_and_close_ignores_late_result(self):
        owner = FakeAfterOwner()
        received: list[tuple[str, int]] = []
        bridge = TkResultBridge(owner, lambda label, value: received.append((label, value)), poll_ms=1)

        bridge.expect()
        worker = threading.Thread(target=lambda: bridge.deliver("ready", 7))
        worker.start()
        worker.join()
        self.assertEqual(owner.counter, 1)

        owner.run_next()
        self.assertEqual(received, [("ready", 7)])

        bridge.expect()
        bridge.close()
        bridge.deliver("late", 9)
        self.assertFalse(owner.callbacks)
        self.assertEqual(received, [("ready", 7)])

    def test_stale_chart_result_queues_latest_selection(self):
        page = MarketPage.__new__(MarketPage)
        page.loading = True
        page.chart_generation = 3
        page.current_code = "ETH"
        page.current_days = 30
        queued: list[int] = []
        page._queue_chart_load = lambda delay=0: queued.append(delay)

        MarketPage._finish_chart(page, 2, "BTC", 7, [(1, 1.0), (2, 2.0)], None)

        self.assertFalse(page.loading)
        self.assertEqual(queued, [0])

    def test_partial_snapshot_updates_dependencies_without_reloading_unrelated_chart(self):
        class Page:
            def __init__(self) -> None:
                self.calls = 0

            def apply_snapshot(self, *_args) -> None:
                self.calls += 1

        chart_reloads: dict[str, list[bool]] = {"fiat_market": [], "market": []}

        def market_page(key: str):
            page = MarketPage.__new__(MarketPage)
            page.apply_snapshot = lambda *_args, reload_chart=True: chart_reloads[key].append(reload_chart)
            return page

        app = YaohengApp.__new__(YaohengApp)
        app.pages = {
            "fiat": Page(),
            "fiat_market": market_page("fiat_market"),
            "crypto": Page(),
            "market": market_page("market"),
        }

        YaohengApp.apply_snapshot(app, object(), False, section="fiat")

        self.assertEqual(app.pages["fiat"].calls, 1)
        self.assertEqual(app.pages["crypto"].calls, 1)
        self.assertEqual(chart_reloads["fiat_market"], [True])
        self.assertEqual(chart_reloads["market"], [False])

    def test_manual_refresh_for_other_section_is_queued(self):
        app = YaohengApp.__new__(YaohengApp)
        app.loading_rates = True
        app.active_rate_section = "fiat"
        app.pending_rate_section = None

        YaohengApp.refresh_rates(app, "crypto")
        self.assertEqual(app.pending_rate_section, "crypto")

        YaohengApp.refresh_rates(app, "all")
        self.assertEqual(app.pending_rate_section, "all")

    def test_numpad_and_full_width_keyboard_input_are_normalized(self):
        self.assertEqual(
            YaohengApp._calculator_key(SimpleNamespace(char="", keysym="KP_Decimal", state=0)),
            ".",
        )
        self.assertEqual(
            YaohengApp._calculator_key(SimpleNamespace(char="７", keysym="", state=0)),
            "7",
        )
        self.assertIsNone(
            YaohengApp._calculator_key(SimpleNamespace(char="7", keysym="7", state=0x0004))
        )

    def test_offscreen_geometry_is_moved_back_into_the_virtual_desktop(self):
        self.assertEqual(
            visible_window_position(9000, 8000, 1600, 900, (0, 0, 1920, 1080)),
            (320, 180),
        )
        self.assertEqual(
            visible_window_position(-1500, 80, 1400, 800, (-1920, 0, 3840, 1080)),
            (-1500, 80),
        )

    def test_timezone_change_only_reformats_timestamps(self):
        class Store:
            @staticmethod
            def save(_settings) -> bool:
                return True

        class Value:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        class Page:
            def __init__(self) -> None:
                self.refresh_stamp_var = Value()

            def apply_snapshot(self, *_args) -> None:
                raise AssertionError("timezone changes must not rebuild market pages")

        app = YaohengApp.__new__(YaohengApp)
        app.settings = AppSettings(timezone="UTC")
        app.settings_store = Store()
        app.persistence_warning_shown = False
        app.service = SimpleNamespace(snapshot=SimpleNamespace(fetched_at="2026-08-19T00:00:00+00:00"))
        app.pages = {key: Page() for key in ("fiat", "fiat_market", "crypto", "market")}
        app.last_network_at = ""
        app.last_network_detail = ""
        app.format_timestamp = lambda _value: "本地时间"

        YaohengApp.set_timezone(app, "Asia/Tokyo")

        self.assertEqual(app.settings.timezone, "Asia/Tokyo")
        self.assertTrue(all(page.refresh_stamp_var.value == "最新刷新：本地时间" for page in app.pages.values()))


class SettingsPersistenceRegressionTests(unittest.TestCase):
    def test_corrupt_primary_recovers_last_known_good_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            store = SettingsStore(path)
            store.save(AppSettings(theme="dark", timezone="UTC"))
            store.save(AppSettings(theme="light", timezone="Asia/Tokyo"))
            path.write_text("{broken", encoding="utf-8")

            recovered = store.load()

            self.assertEqual(recovered.theme, "dark")
            self.assertEqual(recovered.timezone, "UTC")
            restored_payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(restored_payload["theme"], "dark")

    def test_invalid_encoding_and_oversized_files_fall_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            store = SettingsStore(path)

            path.write_bytes(b"\xff\xfe\xfa")
            self.assertEqual(store.load(), AppSettings())

            path.write_bytes(b" " * (MAX_SETTINGS_FILE_BYTES + 1))
            self.assertEqual(store.load(), AppSettings())

    def test_atomic_saves_never_leave_partial_json_under_contention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            store = SettingsStore(path)
            threads = [
                threading.Thread(
                    target=store.save,
                    args=(AppSettings(theme="light" if index % 2 else "dark", history_limit=index + 1),),
                )
                for index in range(12)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(payload["theme"], {"dark", "light"})
            self.assertGreaterEqual(payload["history_limit"], 1)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_geometry_and_custom_data_path_are_strictly_validated(self):
        for geometry in ("0x0", "1380x820+10", "999999x820", "1380x820+999999+0"):
            with self.subTest(geometry=geometry):
                validated = SettingsStore.validate(AppSettings(window_geometry=geometry))
                self.assertEqual(validated.window_geometry, AppSettings().window_geometry)

        valid = SettingsStore.validate(AppSettings(window_geometry="1600x900-1200+80"))
        self.assertEqual(valid.window_geometry, "1600x900-1200+80")

        relative = SettingsStore.validate(
            AppSettings(keep_data_with_app=False, data_dir="relative/cache")
        )
        self.assertEqual(relative.data_dir, "")
        self.assertTrue(relative.resolved_data_dir().is_absolute())

        invalid_windows_path = SettingsStore.validate(
            AppSettings(keep_data_with_app=False, data_dir="D:/bad|cache")
        )
        self.assertEqual(invalid_windows_path.data_dir, "")

    def test_unhashable_json_values_are_recovered_individually(self):
        settings = SettingsStore.from_payload({
            "theme": [],
            "startup_page": {},
            "close_action": "minimize",
            "history_limit": 12,
        })
        self.assertEqual(settings.theme, "dark")
        self.assertEqual(settings.startup_page, "calculator")
        self.assertEqual(settings.close_action, "minimize")
        self.assertEqual(settings.history_limit, 12)

    def test_timezone_names_returns_a_copy(self):
        zones = timezone_names()
        zones.clear()
        self.assertIn("UTC", timezone_names())


class SettingsUiPerformanceTests(unittest.TestCase):
    def test_timezone_options_are_stable_while_selected_clock_can_tick(self):
        page = SettingsPage.__new__(SettingsPage)
        page.zones = ["UTC", "Asia/Tokyo"]
        page.zone_infos = {zone: ZoneInfo(zone) for zone in page.zones}

        first = SettingsPage._timezone_displays(
            page, datetime(2026, 8, 19, 1, 2, 3, tzinfo=ZoneInfo("UTC"))
        )
        second = SettingsPage._timezone_displays(
            page, datetime(2026, 8, 19, 1, 2, 59, tzinfo=ZoneInfo("UTC"))
        )

        self.assertEqual(first, second)
        self.assertEqual(first[0], "UTC+00:00  ·  UTC")


if __name__ == "__main__":
    unittest.main()
