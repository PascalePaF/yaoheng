import gc
import json
import tempfile
import threading
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
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

        MarketPage._finish_chart(page, 2, "BTC", 7, "CNY", [(1, 1.0), (2, 2.0)], None)

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


class MarketFiatWatchRegressionTests(unittest.TestCase):
    class Value:
        def __init__(self, value="") -> None:
            self.value = value

        def get(self):
            return self.value

        def set(self, value) -> None:
            self.value = value

    @staticmethod
    def snapshot():
        return SimpleNamespace(
            rates={"USD": 1.0, "CNY": 7.2, "EUR": 0.9, "JPY": 150.0},
            names={"USD": "美元", "CNY": "人民币", "EUR": "欧元", "JPY": "日元"},
            kinds={code: "fiat" for code in ("USD", "CNY", "EUR", "JPY")},
            changes={"USD": 0.0, "CNY": 2.0, "EUR": -1.0, "JPY": None},
            fetched_at="2026-08-19T00:00:00+00:00",
        )

    @classmethod
    def page(cls, snapshot, quote="CNY", mode="fiat"):
        quote_state = cls.Value(quote)

        def convert(amount, source, target):
            return amount / snapshot.rates[source] * snapshot.rates[target]

        page = MarketPage.__new__(MarketPage)
        page.mode = mode
        page.service = SimpleNamespace(snapshot=snapshot, convert=convert)
        page.reference_amount_value = 1.0
        page.watch_default_rows = []
        page.watch_rows = []
        page.watch_sort_reverse = {"price": True}
        page.period_changes = {}
        page._fiat_code = quote_state.get
        page._set_watch_heading_arrows = lambda *_args: None
        page._render_watch = lambda *_args: None
        return page, quote_state

    @staticmethod
    def rows_by_code(page):
        return {values[0]: (values, tags) for values, tags in page.watch_rows}

    def test_fiat_rows_use_batch_24h_changes_before_any_chart_load(self):
        snapshot = self.snapshot()
        page, _quote = self.page(snapshot)

        MarketPage._refresh_watchlist(page, snapshot)
        rows = self.rows_by_code(page)

        self.assertEqual(rows["USD"][0][3], "+2.0%")
        self.assertEqual(rows["USD"][1], ("up",))
        self.assertEqual(rows["EUR"][0][3], "+3.0%")
        self.assertEqual(rows["EUR"][1], ("up",))
        self.assertEqual(rows["CNY"][0][3], "0.0%")
        self.assertEqual(rows["CNY"][1], ("flat",))
        self.assertEqual(rows["JPY"][0][3], "—")
        self.assertEqual(rows["JPY"][1], ())

    def test_switching_quote_recomputes_prices_and_cross_change_direction(self):
        snapshot = self.snapshot()
        page, quote = self.page(snapshot)
        MarketPage._refresh_watchlist(page, snapshot)
        cny_rows = self.rows_by_code(page)

        quote.set("EUR")
        MarketPage._refresh_watchlist(page, snapshot)
        eur_rows = self.rows_by_code(page)

        self.assertEqual(cny_rows["USD"][0][2], "7.20")
        self.assertEqual(eur_rows["USD"][0][2], "0.9")
        self.assertEqual(eur_rows["CNY"][0][3], "-2.9%")
        self.assertEqual(eur_rows["CNY"][1], ("down",))
        self.assertEqual(eur_rows["EUR"][0][3], "0.0%")
        self.assertEqual(eur_rows["EUR"][1], ("flat",))

    def test_missing_change_is_not_zero_but_identity_and_real_zero_are_flat(self):
        snapshot = SimpleNamespace(changes={"USD": 0.0, "GBP": 0.0, "JPY": None})

        self.assertEqual(MarketPage._fiat_watch_change(snapshot, "GBP", "USD"), 0.0)
        self.assertIsNone(MarketPage._fiat_watch_change(snapshot, "JPY", "USD"))
        self.assertIsNone(MarketPage._fiat_watch_change(snapshot, "USD", "JPY"))
        self.assertEqual(MarketPage._fiat_watch_change(snapshot, "JPY", "JPY"), 0.0)

    def test_detail_period_change_does_not_pollute_fiat_list_24h_change(self):
        snapshot = self.snapshot()
        page, _quote = self.page(snapshot)
        page.raw_points = [(1, 5.0), (2, 7.5)]
        page.raw_points_quote = "CNY"
        page.reference_amount_value = 1.0
        page.current_code = "USD"
        page.price_var = self.Value()
        page.change_var = self.Value()
        page.range_var = self.Value()
        page.change_label = SimpleNamespace(configure=lambda **_kwargs: None)
        page.chart = SimpleNamespace(set_data=lambda *_args: None)
        page.period_changes = {"USD": 99.0}

        MarketPage.rerender_currency(page)
        rows = self.rows_by_code(page)

        self.assertEqual(page.change_var.value, "+50.00%")
        self.assertEqual(rows["USD"][0][3], "+2.0%")

    def test_fiat_change_heading_is_explicitly_24h(self):
        headings: dict[str, str] = {}
        page = MarketPage.__new__(MarketPage)
        page.mode = "fiat"
        page.watch_table = SimpleNamespace(
            heading=lambda column, **kwargs: headings.__setitem__(column, kwargs["text"])
        )

        MarketPage._set_watch_heading_arrows(page, "change", "↓")

        self.assertEqual(headings["change"], "24h ↓")

    def test_crypto_rows_keep_using_their_direct_24h_change(self):
        snapshot = SimpleNamespace(
            rates={"USD": 1.0, "CNY": 7.2, "BTC": 0.00002, "ETH": 0.0004},
            names={"BTC": "Bitcoin", "ETH": "Ethereum"},
            kinds={"USD": "fiat", "CNY": "fiat", "BTC": "crypto", "ETH": "crypto"},
            changes={"BTC": 1.5, "ETH": 0.0},
        )
        page, _quote = self.page(snapshot, mode="crypto")

        MarketPage._refresh_watchlist(page, snapshot)
        rows = self.rows_by_code(page)

        self.assertEqual(rows["BTC"][0][3], "+1.5%")
        self.assertEqual(rows["BTC"][1], ("up",))
        self.assertEqual(rows["ETH"][0][3], "+0.0%")
        self.assertEqual(rows["ETH"][1], ("up",))

    def test_fiat_chart_request_uses_only_the_selected_quote_pair(self):
        delivered: list[tuple] = []
        requested: list[tuple[str, int, str]] = []

        def fetch_fiat_chart(code, days, quote):
            requested.append((code, days, quote))
            return [(1, 0.12), (2, 0.13)]

        class Results:
            @staticmethod
            def expect() -> None:
                return None

            @staticmethod
            def deliver(*args) -> None:
                delivered.append(args)

        class ImmediateThread:
            def __init__(self, target, **_kwargs) -> None:
                self.target = target

            def start(self) -> None:
                self.target()

        page = MarketPage.__new__(MarketPage)
        page.mode = "fiat"
        page.service = SimpleNamespace(
            snapshot=SimpleNamespace(rates={"CNY": 7.2, "EUR": 0.9}),
            fetch_fiat_chart=fetch_fiat_chart,
        )
        page._fiat_code = lambda: "EUR"
        page.current_code = "CNY"
        page.current_days = 30
        page.chart_load_job = None
        page.chart_generation = 0
        page.loading = False
        page.status_var = self.Value()
        page.chart_results = Results()

        with patch("app_ui.threading.Thread", ImmediateThread):
            MarketPage.load_chart(page)

        self.assertEqual(requested, [("CNY", 30, "EUR")])
        self.assertEqual(delivered[0][0:4], (1, "CNY", 30, "EUR"))

    def test_late_chart_result_for_old_quote_is_rejected(self):
        page = MarketPage.__new__(MarketPage)
        page.mode = "fiat"
        page.loading = True
        page.chart_generation = 4
        page.current_code = "USD"
        page.current_days = 7
        page.raw_points = [(0, 1.0)]
        page._fiat_code = lambda: "EUR"
        queued: list[int] = []
        page._queue_chart_load = lambda delay=0: queued.append(delay)

        MarketPage._finish_chart(page, 4, "USD", 7, "CNY", [(1, 7.0), (2, 7.2)], None)

        self.assertFalse(page.loading)
        self.assertEqual(page.raw_points, [(0, 1.0)])
        self.assertEqual(queued, [0])

    def test_real_tk_fiat_watch_lifecycle_when_display_is_available(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        page = None
        try:
            snapshot = self.snapshot()

            def convert(amount, source, target):
                return amount / snapshot.rates[source] * snapshot.rates[target]

            service = SimpleNamespace(snapshot=snapshot, convert=convert)
            page = MarketPage(root, service, lambda: None, lambda value: value, mode="fiat")
            page.pack()
            page.apply_snapshot(snapshot, reload_chart=False)
            root.update_idletasks()

            first_rows = {
                page.watch_table.item(item, "values")[0]: page.watch_table.item(item)
                for item in page.watch_table.get_children()
            }
            self.assertEqual(page.watch_table.heading("change", "text"), "24h ↕")
            self.assertEqual(first_rows["USD"]["values"][3], "+2.0%")
            self.assertEqual(first_rows["CNY"]["tags"], ["flat"])

            eur_display = next(
                display for display, code in page.fiat_display_to_code.items() if code == "EUR"
            )
            page.fiat_combo.set(eur_display)
            page.rerender_currency()
            root.update_idletasks()
            switched_rows = {
                page.watch_table.item(item, "values")[0]: page.watch_table.item(item)
                for item in page.watch_table.get_children()
            }
            self.assertEqual(switched_rows["CNY"]["values"][3], "-2.9%")
            self.assertEqual(switched_rows["CNY"]["tags"], ["down"])
        finally:
            if page is not None:
                page.destroy()
                root.update_idletasks()
            root.destroy()
            page = None
            root = None
            gc.collect()


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
