import gc
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app_ui import (
    THEMES,
    DualConverterPage,
    MarketPage,
    PriceChart,
    SearchSelect,
    SettingsPage,
    TkAfterJobs,
    TkResultBridge,
    YaohengApp,
    enable_dpi_awareness,
    normalize_amount_input,
    visible_window_position,
)
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

    def test_result_bridge_reports_callback_error_without_stranding_later_results(self):
        owner = FakeAfterOwner()
        received: list[str] = []

        def callback(value: str) -> None:
            received.append(value)
            if value == "broken":
                raise RuntimeError("callback failed")

        bridge = TkResultBridge(owner, callback, poll_ms=1)
        bridge.expect()
        bridge.expect()
        bridge.deliver("broken")
        bridge.deliver("ready")

        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            owner.run_next()
        self.assertTrue(owner.callbacks)
        owner.run_next()

        self.assertEqual(received, ["broken", "ready"])
        self.assertFalse(owner.callbacks)

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

        def market_page(key: str, visible: bool):
            page = MarketPage.__new__(MarketPage)
            page.visible = visible
            page.apply_snapshot = lambda *_args, reload_chart=True: chart_reloads[key].append(reload_chart)
            return page

        app = YaohengApp.__new__(YaohengApp)
        app.pages = {
            "fiat": Page(),
            "fiat_market": market_page("fiat_market", True),
            "crypto": Page(),
            "market": market_page("market", False),
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
        self.assertEqual(
            YaohengApp._calculator_key(SimpleNamespace(char="＝", keysym="", state=0)),
            "=",
        )
        self.assertEqual(
            YaohengApp._calculator_key(SimpleNamespace(char="＾", keysym="", state=0)),
            "xʸ",
        )
        self.assertIsNone(
            YaohengApp._calculator_key(SimpleNamespace(char="7", keysym="7", state=0x0004))
        )

    def test_full_width_amount_formula_is_normalized_before_conversion(self):
        class Value:
            def __init__(self, value="") -> None:
                self.value = value

            def get(self):
                return self.value

            def set(self, value) -> None:
                self.value = value

        rates = {"CNY": 7.2, "USD": 1.0, "EUR": 0.9}
        page = DualConverterPage.__new__(DualConverterPage)
        page.amount_a = Value("（１，２００＋３００）÷３")
        page.amount_b = Value()
        page.amount_c = Value()
        page.currency_a = Value("CNY")
        page.currency_b = Value("USD")
        page.currency_c = Value("EUR")
        page.rate_var = Value()
        page._code = lambda display: display
        page.service = SimpleNamespace(
            snapshot=SimpleNamespace(rates=rates),
            convert=lambda amount, source, target: amount / rates[source] * rates[target],
        )

        DualConverterPage.convert_from(page, "a")

        self.assertEqual(page.amount_a.get(), "500")
        self.assertTrue(DualConverterPage._validate_reference_amount("（１，２００＋３００）÷３＝"))
        self.assertEqual(normalize_amount_input("１２３．４５％"), "123.45%")

        page.amount_a.set("１，２")
        DualConverterPage.convert_from(page, "a")
        self.assertEqual(page.amount_a.get(), "１，２")
        self.assertIn("千位分隔符格式不正确", page.rate_var.get())

    def test_offscreen_geometry_is_moved_back_into_the_virtual_desktop(self):
        self.assertEqual(
            visible_window_position(9000, 8000, 1600, 900, (0, 0, 1920, 1080)),
            (320, 180),
        )
        self.assertEqual(
            visible_window_position(-1500, 80, 1400, 800, (-1920, 0, 3840, 1080)),
            (-1500, 80),
        )

    def test_dpi_setup_prefers_per_monitor_v2_before_legacy_fallbacks(self):
        calls: list[tuple[str, object | None]] = []

        class User32:
            @staticmethod
            def SetProcessDpiAwarenessContext(value):
                calls.append(("v2", value))
                return 1

            @staticmethod
            def SetProcessDPIAware():
                calls.append(("legacy", None))

        class Shcore:
            @staticmethod
            def SetProcessDpiAwareness(value):
                calls.append(("shcore", value))
                return 0

        with patch("app_ui.ctypes.windll", SimpleNamespace(user32=User32(), shcore=Shcore()), create=True):
            enable_dpi_awareness()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "v2")
        self.assertEqual(calls[0][1].value & 0xFFFFFFFF, 0xFFFFFFFC)

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
                self.chart = SimpleNamespace(redraw_calls=0)
                self.chart.redraw = lambda: setattr(
                    self.chart, "redraw_calls", self.chart.redraw_calls + 1
                )

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
        self.assertEqual(app.pages["fiat_market"].chart.redraw_calls, 1)
        self.assertEqual(app.pages["market"].chart.redraw_calls, 1)

    def test_chart_timestamps_follow_the_selected_timezone(self):
        app = YaohengApp.__new__(YaohengApp)
        app.settings = AppSettings(timezone="Asia/Tokyo")

        self.assertEqual(YaohengApp.format_chart_timestamp(app, 0), "01-01 09:00")
        app.settings.timezone = "UTC"
        self.assertEqual(YaohengApp.format_chart_timestamp(app, 0), "01-01 00:00")
        self.assertEqual(PriceChart._format_utc_timestamp(0), "01-01 00:00")


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
        page.raw_points_code = "USD"
        page.raw_points_days = 7
        page.raw_points_quote = "CNY"
        page.reference_amount_value = 1.0
        page.current_code = "USD"
        page.current_days = 7
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
        page.raw_points = []
        page.raw_points_code = ""
        page.raw_points_days = 0
        page.raw_points_quote = "CNY"
        page.chart = SimpleNamespace(set_data=lambda *_args: None)
        page.price_var = self.Value()
        page.change_var = self.Value()
        page.change_label = SimpleNamespace(configure=lambda **_kwargs: None)
        page.range_var = self.Value()

        with patch("app_ui.threading.Thread", ImmediateThread):
            MarketPage.load_chart(page)

        self.assertEqual(requested, [("CNY", 30, "EUR")])
        self.assertEqual(delivered[0][0:4], (1, "CNY", 30, "EUR"))

    def test_new_chart_request_clears_series_from_old_asset_and_period(self):
        class DeferredThread:
            def __init__(self, target, **_kwargs) -> None:
                self.target = target

            def start(self) -> None:
                return None

        chart_calls: list[tuple[list[tuple[int, float]], str]] = []
        page = MarketPage.__new__(MarketPage)
        page.mode = "crypto"
        page.service = SimpleNamespace(
            snapshot=SimpleNamespace(rates={"ETH": 0.0004}),
            fetch_market_chart=lambda *_args: [(1, 1.0), (2, 2.0)],
        )
        page.current_code = "ETH"
        page.current_days = 30
        page.chart_load_job = None
        page.chart_generation = 2
        page.loading = True
        page.raw_points = [(1, 10.0), (2, 11.0)]
        page.raw_points_code = "BTC"
        page.raw_points_days = 7
        page.raw_points_quote = "CNY"
        page.chart = SimpleNamespace(
            set_data=lambda points, quote: chart_calls.append((list(points), quote))
        )
        page.price_var = self.Value("old price")
        page.change_var = self.Value("+10%")
        page.change_label = SimpleNamespace(configure=lambda **_kwargs: None)
        page.range_var = self.Value("old range")
        page.status_var = self.Value()
        page.chart_results = SimpleNamespace(expect=lambda: None)

        with patch("app_ui.threading.Thread", DeferredThread):
            MarketPage.load_chart(page)

        self.assertEqual(page.raw_points, [])
        self.assertEqual(chart_calls, [([], "CNY")])
        self.assertEqual(page.price_var.value, "正在加载…")
        self.assertEqual(page.change_var.value, "—")
        self.assertEqual(page.range_var.value, "最高 —   最低 —")
        self.assertTrue(page.loading)

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

    def test_distinct_store_instances_share_a_lock_for_the_same_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app_settings.json"
            stores = [SettingsStore(path) for _ in range(12)]
            active_writers = 0
            peak_writers = 0
            counter_lock = threading.Lock()
            start = threading.Barrier(len(stores))
            results: list[bool | None] = [None] * len(stores)
            original_write = SettingsStore._atomic_write

            def observed_write(store, target, text):
                nonlocal active_writers, peak_writers
                with counter_lock:
                    active_writers += 1
                    peak_writers = max(peak_writers, active_writers)
                time.sleep(0.002)
                try:
                    return original_write(store, target, text)
                finally:
                    with counter_lock:
                        active_writers -= 1

            def save(index: int) -> None:
                start.wait()
                results[index] = stores[index].save(AppSettings(history_limit=index + 1))

            with patch.object(SettingsStore, "_atomic_write", observed_write):
                threads = [threading.Thread(target=save, args=(index,)) for index in range(len(stores))]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertTrue(all(results))
            self.assertEqual(peak_writers, 1)
            json.loads(path.read_text(encoding="utf-8"))
            json.loads(path.with_suffix(".json.bak").read_text(encoding="utf-8"))
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
        self.assertEqual(settings.close_action, "exit")
        self.assertEqual(settings.history_limit, 12)

    def test_timezone_names_returns_a_copy(self):
        zones = timezone_names()
        zones.clear()
        self.assertIn("UTC", timezone_names())


class UsabilityRegressionTests(unittest.TestCase):
    @staticmethod
    def _contrast(first: str, second: str) -> float:
        def luminance(color: str) -> float:
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [
                value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                for value in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        bright, dark = sorted((luminance(first), luminance(second)), reverse=True)
        return (bright + 0.05) / (dark + 0.05)

    def test_accent_buttons_and_disabled_keys_remain_readable_in_both_themes(self):
        for name, colors in THEMES.items():
            with self.subTest(theme=name):
                self.assertGreaterEqual(self._contrast(colors["accent"], colors["on_accent"]), 4.5)
                self.assertGreaterEqual(self._contrast(colors["accent"], colors["accent_dark"]), 4.5)
                self.assertGreaterEqual(self._contrast(colors["key"], colors["subtle"]), 3.0)

    def test_missing_fiat_change_is_not_colored_from_exchange_rate_magnitude(self):
        class Value:
            def __init__(self, value="") -> None:
                self.value = value

            def get(self):
                return self.value

            def set(self, value) -> None:
                self.value = value

        snapshot = SimpleNamespace(
            rates={"CNY": 7.2, "USD": 1.0},
            names={"CNY": "人民币", "USD": "美元"},
            kinds={"CNY": "fiat", "USD": "fiat"},
            changes={"CNY": 0.5, "USD": None},
        )
        page = DualConverterPage.__new__(DualConverterPage)
        page.mode = "fiat"
        page.reference_amount_value = 1.0
        page.table_base_var = Value("CNY")
        page.rate_var = Value()
        page.service = SimpleNamespace(
            convert=lambda amount, source, target: amount / snapshot.rates[source] * snapshot.rates[target]
        )
        page._code = lambda display: display
        page.sort_reverse = {}
        page._set_table_heading_arrows = lambda *_args: None
        page._render_rows = lambda *_args: None

        DualConverterPage.update_table(page, snapshot)
        rows = {values[0]: (values, tags) for values, tags in page.table_rows}

        self.assertEqual(rows["USD"][0][3], "—")
        self.assertEqual(rows["USD"][1], ())

    def test_empty_search_closes_a_stale_dropdown(self):
        selector = SearchSelect.__new__(SearchSelect)
        selector.filtered_values = []
        closed: list[bool] = []
        selector.close = lambda: closed.append(True) or "break"

        SearchSelect._show_popup(selector)

        self.assertEqual(closed, [True])

    def test_missing_numeric_values_stay_last_in_both_sort_directions(self):
        converter = DualConverterPage.__new__(DualConverterPage)
        converter.table_rows = [
            (("MISS", "", "", "—", "", "", ""), ()),
            (("LOW", "", "", "-2.0%", "", "", ""), ()),
            (("HIGH", "", "", "+5.0%", "", "", ""), ()),
        ]
        converter.sort_reverse = {}
        converter._set_table_heading_arrows = lambda *_args: None
        converter._render_rows = lambda *_args: None

        DualConverterPage.sort_table(converter, "change", True)
        self.assertEqual([row[0][0] for row in converter.table_rows], ["LOW", "HIGH", "MISS"])
        DualConverterPage.sort_table(converter, "change", True)
        self.assertEqual([row[0][0] for row in converter.table_rows], ["HIGH", "LOW", "MISS"])

        market = MarketPage.__new__(MarketPage)
        market.watch_rows = [
            (("MISS", "", "", "—"), ()),
            (("LOW", "", "", "-2.0%"), ()),
            (("HIGH", "", "", "+5.0%"), ()),
        ]
        market.watch_sort_reverse = {}
        market._set_watch_heading_arrows = lambda *_args: None
        market._render_watch = lambda *_args: None

        MarketPage.sort_watch(market, "change", True)
        self.assertEqual([row[0][0] for row in market.watch_rows], ["LOW", "HIGH", "MISS"])
        MarketPage.sort_watch(market, "change", True)
        self.assertEqual([row[0][0] for row in market.watch_rows], ["HIGH", "LOW", "MISS"])


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
        self.assertEqual(first[0], "UTC+00:00  ·  协调世界时  ·  UTC")
        self.assertIn("日本", page.timezone_search_aliases[first[1]])


if __name__ == "__main__":
    unittest.main()
