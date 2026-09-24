from __future__ import annotations

import threading
import tkinter as tk
import unittest
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app_ui import YaohengApp
from rate_service import RateService, RateSnapshot
from ui_runtime import PageHost, affected_rate_pages
from ui_table import reconcile_rows, sort_financial_rows


class PageHostTests(unittest.TestCase):
    def test_constructs_once_and_coalesces_hidden_snapshots(self):
        made: list[str] = []
        shown: list[tuple[str, object, bool]] = []
        host = PageHost(
            {name: (lambda key=name: made.append(key) or object()) for name in (
                "calculator", "exchange", "fiat", "crypto", "market"
            )},
            lambda _page: None,
            lambda name, _page, update, visible: shown.append((name, update.snapshot, visible)),
        )
        self.assertIs(host.ensure("calculator"), host.ensure("calculator"))
        host.publish("first", False, "all", "calculator")
        host.publish("second", False, "all", "calculator")
        self.assertEqual(made, ["calculator"])
        self.assertEqual(shown, [])
        self.assertIn("market", host.pending_names())

        exchange = host.ensure("exchange")
        self.assertIs(exchange, host.ensure("exchange"))
        host.present_pending("exchange")
        self.assertEqual(shown, [("exchange", "second", False)])

        host.publish("crypto", False, "crypto", "exchange")
        self.assertEqual(shown[-1], ("exchange", "crypto", True))
        self.assertEqual(affected_rate_pages("crypto"), (
            "exchange", "market_exchange", "crypto", "market"
        ))
        self.assertIn("fiat", host.pending_names())

    def test_cold_navigation_shows_feedback_before_constructing_page(self):
        app = YaohengApp.__new__(YaohengApp)
        made: list[str] = []
        app.page_host = PageHost(
            {"fiat": lambda: made.append("fiat") or object()},
            lambda _page: None,
            lambda *_args: None,
        )
        app.pages = app.page_host.pages
        app.root = MagicMock()
        app.root.after.return_value = "navigation-job"
        app.loading_overlay = MagicMock()
        app._navigation_job = None
        app._navigation_target = ""
        app.show_page = MagicMock()

        YaohengApp.navigate(app, "fiat")
        self.assertEqual(made, [])
        self.assertEqual(app._navigation_target, "fiat")
        app.loading_overlay.tkraise.assert_called_once_with()
        app.root.after.assert_called_once_with(24, app._finish_navigation)

        YaohengApp._finish_navigation(app)
        app.show_page.assert_called_once_with("fiat")
        app.loading_overlay.grid_remove.assert_called_once_with()

    def test_failing_presentation_keeps_pending_update(self):
        def fail(*_args):
            raise RuntimeError("render failed")

        host = PageHost({"fiat": object}, lambda _page: None, fail)
        host.ensure("fiat")
        with self.assertRaises(RuntimeError):
            host.publish("rates", False, "fiat", "fiat")
        self.assertIn("fiat", host.pending_names())

    def test_late_network_result_cannot_repaint_an_older_snapshot(self):
        app = YaohengApp.__new__(YaohengApp)
        old = SimpleNamespace(fetched_at="old", errors=[])
        new = SimpleNamespace(fetched_at="new", errors=[])
        app.exiting = False
        app.loading_rates = True
        app.active_rate_section = "all"
        app.pending_rate_section = None
        app.service = SimpleNamespace(snapshot=new)
        app.network_button = MagicMock()
        app._set_network_status = MagicMock()
        app.format_timestamp = lambda value: value
        app.apply_snapshot = MagicMock()
        app.schedule_auto_refresh = MagicMock()

        YaohengApp._finish_rates(app, old, None, "all")

        app.apply_snapshot.assert_called_once_with(new, False, animated=False, section="all")
        self.assertEqual(app.last_network_at, "new")


class RateCacheCommitTests(unittest.TestCase):
    @staticmethod
    def snapshot(stamp: str) -> RateSnapshot:
        return RateSnapshot(
            {"USD": 1.0, "CNY": 7.0},
            {"USD": "美元", "CNY": "人民币"},
            {"USD": "fiat", "CNY": "fiat"},
            {}, stamp,
            rate_strings={"USD": "1", "CNY": "7"},
        )

    def test_old_commit_cannot_overwrite_newer_cache(self):
        service = RateService.__new__(RateService)
        service._cache_lock = threading.RLock()
        service._state_lock = threading.RLock()
        old = self.snapshot("old")
        new = self.snapshot("new")
        service.snapshot = new
        written: list[str] = []
        service.save_cache = lambda snapshot: written.append(snapshot.fetched_at) or True

        self.assertTrue(service._save_committed_snapshot(old))
        self.assertTrue(service._save_committed_snapshot(new))
        self.assertEqual(written, ["new"])

    def test_cache_write_does_not_hold_conversion_state_lock(self):
        service = RateService.__new__(RateService)
        service._cache_lock = threading.RLock()
        service._state_lock = threading.RLock()
        service.snapshot = self.snapshot("new")
        entered = threading.Event()
        finish = threading.Event()

        def slow_save(_snapshot):
            entered.set()
            self.assertTrue(finish.wait(2))
            return True

        service.save_cache = slow_save
        worker = threading.Thread(
            target=lambda: service._save_committed_snapshot(service.snapshot), daemon=True
        )
        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            self.assertTrue(service._state_lock.acquire(timeout=0.1))
            service._state_lock.release()
        finally:
            finish.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())


class StableTreeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.tree = ttk.Treeview(self.root, columns=("code", "price"), show="headings")

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    @staticmethod
    def key(values: tuple[str, ...], tags: tuple[str, ...]) -> str:
        return f"{'pin' if 'pinned_copy' in tags else 'row'}:{values[0]}"

    def test_preserves_selection_and_item_ids_when_values_change(self):
        rows = [(("USD", "7"), ()), (("EUR", "8"), ())]
        first = reconcile_rows(self.tree, rows, self.key)
        usd_id = next(item for item, key in first.items() if key == "row:USD")
        self.tree.selection_set(usd_id)
        with patch.object(self.tree, "delete", wraps=self.tree.delete) as deleting:
            second = reconcile_rows(
                self.tree,
                [(("EUR", "8"), ()), (("USD", "7.1"), ("up",))],
                self.key,
            )
            deleting.assert_not_called()
        self.assertEqual(second[usd_id], "row:USD")
        self.assertEqual(self.tree.selection(), (usd_id,))
        self.assertEqual(tuple(self.tree.item(usd_id, "values")), ("USD", "7.1"))
        self.assertEqual(tuple(self.tree.get_children()), (
            next(item for item, key in second.items() if key == "row:EUR"), usd_id
        ))

    def test_pinned_copy_is_distinct_from_normal_row(self):
        mapping = reconcile_rows(
            self.tree,
            [(("USD", "7"), ("pinned_copy",)), (("USD", "7"), ())],
            self.key,
        )
        self.assertEqual(set(mapping.values()), {"pin:USD", "row:USD"})
        self.assertEqual(len(self.tree.get_children()), 2)
        with self.assertRaises(ValueError):
            reconcile_rows(
                self.tree,
                [(("USD", "7"), ()), (("USD", "8"), ())],
                self.key,
            )


class FinancialSortTests(unittest.TestCase):
    def test_compact_numeric_sort_keeps_missing_values_at_end(self):
        rows = [
            (("A", "1.2K"), ()),
            (("B", "—"), ()),
            (("C", "950"), ()),
            (("D", "2M"), ()),
        ]
        ascending = sort_financial_rows(rows, 1, numeric=True, reverse=False, compact=True)
        descending = sort_financial_rows(rows, 1, numeric=True, reverse=True, compact=True)
        self.assertEqual([row[0][0] for row in ascending], ["C", "A", "D", "B"])
        self.assertEqual([row[0][0] for row in descending], ["D", "A", "C", "B"])
