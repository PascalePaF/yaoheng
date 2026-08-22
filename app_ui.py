"""Future-focused desktop UI for 曜衡."""

from __future__ import annotations

import ctypes
import http.client
import json
import math
import os
import shutil
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from app_version import APP_VERSION
from calculator_core import CalculationError, CalculatorModel, evaluate_basic_amount, format_number
from c2c import BinanceP2PAdapter, C2CQuoteService, OkxP2PAdapter
from command_service import CommandService
from exchange_page import (
    PAYMENT_ALL_LABEL,
    PAYMENT_UNKNOWN_LABEL,
    PROVIDER_LABELS,
    C2CBridgeJob,
    C2CQuoteJob,
    ExchangeCoordinator,
    ExchangeEdgeResult,
    ExchangePage,
    ExchangePageState,
)
from local_api import LocalAPIError, LocalAPIPortInUseError, LocalAPIServer
from localization import (
    LANGUAGE_LABELS,
    format_datetime,
    get_language,
    install_tk_localization,
    localized_asset_name,
    normalize_language,
    refresh_widget_tree,
    set_language as set_ui_language,
    timezone_display_name,
    timezone_search_text,
    tr,
)
from rate_service import RateService, RateSnapshot, crypto_display_name, fiat_display_name, fiat_region, portable_dir, relative_rate_change
from secret_store import SecretAlreadyExistsError, SecretStore, SecretStoreError
from settings_service import AppSettings, SettingsStore, timezone_names
from theme_catalog import THEME_LABELS, THEME_SOURCES, THEMES, theme_label
from update_service import DownloadedUpdate, GitHubUpdateService, UpdateError, UpdateInfo


DisplayStringVar = install_tk_localization(tk, ttk, messagebox, filedialog)

COLORS = dict(THEMES["dark"])

FONT = "Microsoft YaHei UI"
BRAND_ORANGE = "#FF9D2E"
BRAND_DARK = "#171717"
NETWORK_STATUS_PALETTES: Mapping[object, tuple[str, str]] = {
    True: ("#0B3A2A", "#71F6B5"),
    False: ("#471A23", "#FF9AA8"),
    "partial": ("#44340C", "#FFD66B"),
    None: ("#14324A", "#8FD3FF"),
}
_FULL_WIDTH_INPUT_TRANSLATION = str.maketrans({
    **{chr(ord("０") + index): str(index) for index in range(10)},
    "＋": "+", "－": "-", "＊": "*", "／": "/", "％": "%",
    "（": "(", "）": ")", "＾": "^", "，": ",", "．": ".", "＝": "=",
})


def normalize_amount_input(value: str) -> str:
    """Normalize IME/full-width amount input before UI-side validation."""
    return value.translate(_FULL_WIDTH_INPUT_TRANSLATION)


def connection_status_state(value: object) -> bool | str | None:
    """Classify visible connection text without borrowing theme colors."""

    text = str(value or "").casefold()
    failure_terms = (
        "失败", "错误", "不可用", "未运行", "无法", "failed", "error",
        "unavailable", "not running", "失敗", "利用できません",
    )
    success_terms = ("连接成功", "运行中", "connected", "running", "接続済み")
    partial_terms = ("部分", "警告", "已启用但", "partial", "warning", "一部")
    if any(term in text for term in failure_terms):
        return False
    if any(term in text for term in success_terms):
        return True
    if any(term in text for term in partial_terms):
        return "partial"
    return None


def visible_window_position(
    x: int,
    y: int,
    width: int,
    height: int,
    screen_bounds: tuple[int, int, int, int],
    minimum_visible: int = 80,
) -> tuple[int, int]:
    """Return a usable position when a remembered window is fully off-screen."""
    left, top, screen_width, screen_height = screen_bounds
    right, bottom = left + max(1, screen_width), top + max(1, screen_height)
    visible_width = max(0, min(x + width, right) - max(x, left))
    visible_height = max(0, min(y + height, bottom) - max(y, top))
    if visible_width >= min(minimum_visible, width) and visible_height >= min(minimum_visible, height):
        return x, y
    max_x = max(left, right - width)
    max_y = max(top, bottom - height)
    return max(left, min(x, max_x)), max(top, min(y, max_y))


def enable_dpi_awareness() -> None:
    try:
        context = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(context):
            return
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # PER_MONITOR_DPI_AWARE
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def register_windows_restart() -> bool:
    """Let Windows Restart Manager reopen a packaged app after an upgrade."""

    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        function = ctypes.windll.kernel32.RegisterApplicationRestart
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        function.restype = ctypes.c_long
        return int(function(None, 0)) >= 0
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def format_chinese_datetime(value: datetime) -> str:
    """Backward-compatible locale-aware timestamp formatter."""

    return format_datetime(value)


def set_windows_window_icon(root: tk.Tk, icon_path: Path) -> list[int]:
    """Bind both Windows taskbar icon sizes to the same 曜衡 ICO asset."""
    if sys.platform != "win32" or not icon_path.exists():
        return []
    try:
        user32 = ctypes.windll.user32
        load_image = user32.LoadImageW
        load_image.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        load_image.restype = ctypes.c_void_p
        send_message = user32.SendMessageW
        send_message.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        send_message.restype = ctypes.c_void_p
        get_parent = user32.GetParent
        get_parent.argtypes = [ctypes.c_void_p]
        get_parent.restype = ctypes.c_void_p

        image_icon = 1
        load_from_file = 0x0010
        wm_seticon = 0x0080
        icon_small = 0
        icon_big = 1
        small_size = (
            user32.GetSystemMetrics(49),  # SM_CXSMICON
            user32.GetSystemMetrics(50),  # SM_CYSMICON
        )
        big_size = (
            user32.GetSystemMetrics(11),  # SM_CXICON
            user32.GetSystemMetrics(12),  # SM_CYICON
        )
        small_handle = load_image(None, str(icon_path), image_icon, *small_size, load_from_file)
        big_handle = load_image(None, str(icon_path), image_icon, *big_size, load_from_file)
        if not small_handle or not big_handle:
            return []

        client_hwnd = root.winfo_id()
        wrapper_hwnd = get_parent(client_hwnd) or client_hwnd
        for hwnd in {client_hwnd, wrapper_hwnd}:
            send_message(hwnd, wm_seticon, icon_small, small_handle)
            send_message(hwnd, wm_seticon, icon_big, big_handle)
        return [int(small_handle), int(big_handle)]
    except (AttributeError, OSError, tk.TclError):
        return []


class TkResultBridge:
    """Move worker results to Tk without invoking any Tcl command off-thread."""

    def __init__(
        self,
        owner: tk.Misc,
        callback: Callable[..., None],
        poll_ms: int = 25,
    ) -> None:
        self.owner = owner
        self.callback = callback
        self.poll_ms = max(1, poll_ms)
        self._poll_delay = self.poll_ms
        self._results: SimpleQueue[tuple[object, ...]] = SimpleQueue()
        self._pending = 0
        self._poll_job: str | None = None
        self._closed = False

    def expect(self) -> None:
        if self._closed:
            return
        self._pending += 1
        self._ensure_poll()

    def deliver(self, *payload: object) -> None:
        if not self._closed:
            self._results.put(payload)

    def _ensure_poll(self) -> None:
        if self._closed or self._poll_job is not None:
            return
        try:
            self._poll_job = self.owner.after(self._poll_delay, self._poll)
        except tk.TclError:
            self.close()

    def _poll(self) -> None:
        self._poll_job = None
        if self._closed:
            return
        delivered = False
        while self._pending:
            try:
                payload = self._results.get_nowait()
            except Empty:
                break
            self._pending -= 1
            delivered = True
            try:
                self.callback(*payload)
            except Exception:
                # Tk will report the original callback exception. Keep polling
                # separately so one bad result cannot strand later deliveries.
                self._poll_delay = self.poll_ms
                if self._pending and not self._closed:
                    self._ensure_poll()
                raise
            if self._closed:
                return
        if self._pending:
            self._poll_delay = self.poll_ms if delivered else min(250, max(self.poll_ms, self._poll_delay * 2))
            self._ensure_poll()
        else:
            self._poll_delay = self.poll_ms

    def close(self) -> None:
        self._closed = True
        self._pending = 0
        if self._poll_job is not None:
            try:
                self.owner.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None


class TkAfterJobs:
    """Own short-lived Tk callbacks so widget destruction can cancel them cleanly."""

    def __init__(self, owner: tk.Misc) -> None:
        self.owner = owner
        self.jobs: set[str] = set()
        self.closed = False

    def schedule(self, delay: int, callback: Callable[[], None], idle: bool = False) -> str | None:
        if self.closed:
            return None
        holder: list[str] = []

        def run() -> None:
            if holder:
                self.jobs.discard(holder[0])
            if not self.closed:
                callback()

        try:
            job = self.owner.after_idle(run) if idle else self.owner.after(delay, run)
        except tk.TclError:
            return None
        holder.append(job)
        self.jobs.add(job)
        return job

    def cancel_all(self) -> None:
        self.closed = True
        for job in tuple(self.jobs):
            try:
                self.owner.after_cancel(job)
            except tk.TclError:
                pass
        self.jobs.clear()


class AppC2CService:
    """Application facade that keeps adapters private and UI parsing-free."""

    def __init__(self, rate_service: RateService) -> None:
        self.rate_service = rate_service
        self.providers = {
            "binance": BinanceP2PAdapter(),
            "okx": OkxP2PAdapter(),
        }
        self.service = C2CQuoteService(
            self.providers,
            market_fallback=self._market_fallback,
        )

    def _market_fallback(self, request: object) -> dict[str, str]:
        asset = str(getattr(request, "asset", ""))
        fiat = str(getattr(request, "fiat", ""))
        return {
            "price": self.rate_service.convert_exact("1", asset, fiat),
            "source": "ordinary_market",
        }

    def quote(self, request: object, *, cancel: object | None = None) -> object:
        return self.service.quote(request, cancel=cancel)  # type: ignore[arg-type]

    def capabilities(self) -> object:
        return self.service.capabilities()

    def clear_memory_cache(self) -> None:
        self.service.clear_memory_cache()

    def payment_methods(self, provider: str, fiat: str) -> tuple[object, ...]:
        """Fetch official provider identifiers; callers run this off the Tk thread."""

        selected = str(provider or "auto").lower()
        candidates = (
            tuple(self.providers.values())
            if selected == "auto"
            else (self.providers.get(selected),)
        )
        methods: list[object] = []
        identifiers: set[str] = set()
        for adapter in candidates:
            if adapter is None:
                continue
            capability = adapter.capability
            if not capability.enabled or not capability.configured or not capability.trade_methods:
                continue
            loader = getattr(adapter, "list_trade_methods", None)
            if not callable(loader):
                continue
            try:
                rows = tuple(loader(fiat))
            except Exception:
                continue
            for row in rows:
                identifier = str(getattr(row, "identifier", ""))
                if identifier and identifier not in identifiers:
                    identifiers.add(identifier)
                    methods.append(row)
        return tuple(methods)


class AppButton(tk.Button):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        kind: str = "normal",
        size: int = 13,
        **kwargs,
    ) -> None:
        palettes = {
            "normal": (COLORS["button_bg"], COLORS["button_text"], COLORS["button_hover"]),
            "muted": (COLORS["calc_function_bg"], COLORS["calc_function_text"], COLORS["calc_function_hover"]),
            "accent": (COLORS["accent"], COLORS["on_accent"], COLORS["accent_hover"]),
            "outline": (COLORS["card"], COLORS["accent"], COLORS["accent_dark"]),
            "soft_accent": (COLORS["accent_dark"], COLORS["accent"], COLORS["card_alt"]),
            "ghost": (COLORS["card"], COLORS["muted"], COLORS["card_alt"]),
            "operator": (COLORS["calc_operator_bg"], COLORS["calc_operator_text"], COLORS["calc_operator_hover"]),
        }
        bg, fg, active = palettes[kind]
        outline_width = 1 if kind == "outline" else 0
        super().__init__(
            master,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            disabledforeground=COLORS["subtle"],
            relief="flat",
            bd=0,
            highlightthickness=outline_width,
            highlightbackground=COLORS["accent"] if outline_width else bg,
            highlightcolor=COLORS["accent"] if outline_width else bg,
            cursor="hand2",
            font=(FONT, size, "bold" if kind in {"accent", "outline", "soft_accent", "operator"} else "normal"),
            **kwargs,
        )


class SearchSelect(tk.Frame):
    """Editable selector with a live-filtered, keyboard-driven dropdown."""

    def __init__(
        self,
        master: tk.Misc,
        variable: tk.StringVar,
        values: list[str] | None = None,
        command: Callable[[str], None] | None = None,
        input_callback: Callable[[str], None] | None = None,
        allow_free_text: bool = False,
        width: int = 20,
        font_size: int = 10,
        max_rows: int = 8,
        search_aliases: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            master, bg=COLORS["card_alt"], bd=0, highlightthickness=1,
            highlightbackground=COLORS["accent"], highlightcolor=COLORS["accent"],
        )
        self.variable = variable
        self.values = list(values or [])
        self.filtered_values = list(self.values)
        self.command = command
        self.input_callback = input_callback
        self.allow_free_text = allow_free_text
        self.max_rows = max_rows
        self.search_aliases = dict(search_aliases or {})
        self.selected_value = variable.get()
        self.popup: tk.Toplevel | None = None
        self.listbox: tk.Listbox | None = None
        self.active_index = 0
        self._hiding_job: str | None = None
        self._follow_job: str | None = None
        self._focus_job: str | None = None
        self._open_job: str | None = None
        self._position_job: str | None = None
        self._suppress_focus_open = False
        self.grid_columnconfigure(0, weight=1)
        self.entry = tk.Entry(
            self, textvariable=variable, width=width, bg=COLORS["card_alt"], fg=COLORS["text"],
            insertbackground=COLORS["text"], insertwidth=2,
            selectbackground=COLORS["selection"], selectforeground=COLORS["selection_text"],
            relief="flat", bd=0, highlightthickness=0, font=(FONT, font_size, "bold"),
        )
        self.entry.grid(row=0, column=0, sticky="nsew", padx=(9, 2), pady=7)
        self.arrow = tk.Button(
            self, text="⌄", command=self.focus_input, bg=COLORS["card_alt"], fg=COLORS["accent"],
            activebackground=COLORS["accent_dark"], activeforeground=COLORS["accent"],
            relief="flat", bd=0, highlightthickness=0, cursor="hand2", takefocus=False,
            font=("Segoe UI Symbol", 10, "bold"), padx=7,
        )
        self.arrow.grid(row=0, column=1, sticky="ns")
        self.entry.bind("<Button-1>", self._entry_clicked, add="+")
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<KeyRelease>", self._typed)
        self.entry.bind("<Down>", lambda _e: self._move(1))
        self.entry.bind("<Up>", lambda _e: self._move(-1))
        self.entry.bind("<Return>", self._confirm)
        self.entry.bind("<Escape>", lambda _e: self.close())
        self.entry.bind("<MouseWheel>", self._wheel)
        root = self.winfo_toplevel()
        self._root_configure_binding = root.bind("<Configure>", self._anchor_changed, add="+")
        self._root_unmap_binding = root.bind("<Unmap>", self._root_unmapped, add="+")
        self.bind("<Configure>", self._anchor_changed, add="+")
        self.bind("<Destroy>", self._destroy_popup, add="+")

    def get(self) -> str:
        return self.variable.get()

    def set(self, value: str, notify: bool = False) -> None:
        self.variable.set(value)
        if value in self.values:
            self.selected_value = value
        if notify:
            if self.input_callback:
                self.input_callback(value)
            if self.command:
                self.command(value)

    def set_values(self, values: list[str], search_aliases: Mapping[str, str] | None = None) -> None:
        updated = list(values)
        if search_aliases is not None:
            self.search_aliases = dict(search_aliases)
        if updated == self.values:
            return
        self.values = updated
        self._filter(self.variable.get() if self.allow_free_text else "")

    def focus_input(self) -> None:
        if self.focus_get() is not self.entry:
            self._suppress_focus_open = True
        self.entry.focus_set()
        self.entry.selection_range(0, tk.END)
        self.open()

    def open(self) -> None:
        if not self.winfo_exists() or not self.values:
            return
        self._filter(self.variable.get() if self.allow_free_text else "")
        if not self.filtered_values and not self.allow_free_text:
            self._filter("")
        self._show_popup()

    def _show_popup(self) -> None:
        if not self.filtered_values:
            self.close()
            return
        root = self.winfo_toplevel()
        active = getattr(root, "_active_search_select", None)
        if active is not None and active is not self:
            active.close()
        setattr(root, "_active_search_select", self)
        if self.popup is None or not self.popup.winfo_exists():
            self._create_popup()
        self._fill_listbox()
        self._position_popup()
        assert self.popup is not None
        self.popup.deiconify()
        self.popup.lift()
        if self._follow_job is None:
            self._follow_job = self.after(30, self._follow_anchor)

    def close(self) -> str:
        try:
            if self.popup is not None and self.popup.winfo_exists():
                self.popup.withdraw()
        except tk.TclError:
            pass
        if self._follow_job is not None:
            try:
                self.after_cancel(self._follow_job)
            except tk.TclError:
                pass
            self._follow_job = None
        try:
            root = self.winfo_toplevel()
            if getattr(root, "_active_search_select", None) is self:
                setattr(root, "_active_search_select", None)
        except tk.TclError:
            pass
        return "break"

    def _create_popup(self) -> None:
        self.popup = tk.Toplevel(self.winfo_toplevel())
        self.popup.withdraw()
        self.popup.overrideredirect(True)
        self.popup.transient(self.winfo_toplevel())
        self.popup.configure(bg=COLORS["subtle"])
        panel = tk.Frame(self.popup, bg=COLORS["line"], bd=0)
        panel.pack(fill="both", expand=True, padx=(0, 4), pady=(0, 4))
        self.listbox = tk.Listbox(
            panel, bg=COLORS["card_alt"], fg=COLORS["text"],
            selectbackground=COLORS["accent"], selectforeground=COLORS["on_accent"],
            activestyle="none", exportselection=False, relief="flat", bd=0,
            highlightthickness=0, font=(FONT, 10, "bold"), cursor="hand2",
        )
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        self.popup_scrollbar = scrollbar
        self.listbox.bind("<ButtonRelease-1>", self._choose_mouse)
        self.listbox.bind("<Motion>", self._hover)
        self.listbox.bind("<Return>", self._confirm)
        self.listbox.bind("<Escape>", lambda _e: self.close())
        self.listbox.bind("<Down>", lambda _e: self._move(1))
        self.listbox.bind("<Up>", lambda _e: self._move(-1))
        self.listbox.bind("<MouseWheel>", self._wheel)

    def _position_popup(self) -> None:
        assert self.popup is not None
        self.update_idletasks()
        width = max(self.winfo_width(), 180)
        rows = min(max(len(self.filtered_values), 1), self.max_rows)
        height = rows * 29 + 6
        x = self.winfo_rootx() + 3
        y = self.winfo_rooty() + self.winfo_height() + 3
        screen_height = self.winfo_screenheight()
        if y + height > screen_height - 20:
            y = self.winfo_rooty() - height - 3
        geometry = f"{width}x{height}+{x}+{y}"
        if self.popup.geometry() != geometry:
            self.popup.geometry(geometry)

    def _anchor_changed(self, _event: tk.Event | None = None) -> None:
        if self.popup is not None and self.popup.winfo_exists() and self.popup.winfo_viewable():
            if self._position_job is None:
                self._position_job = self.after_idle(self._position_popup_idle)

    def _position_popup_idle(self) -> None:
        self._position_job = None
        try:
            if self.winfo_exists() and self.popup is not None and self.popup.winfo_exists():
                self._position_popup()
        except tk.TclError:
            return

    def _follow_anchor(self) -> None:
        self._follow_job = None
        try:
            if self.popup is None or not self.popup.winfo_exists() or not self.popup.winfo_viewable():
                return
            self._position_popup()
            self._follow_job = self.after(30, self._follow_anchor)
        except tk.TclError:
            return

    def _root_unmapped(self, event: tk.Event) -> None:
        if event.widget is self.winfo_toplevel():
            self.close()

    def _filter(self, query: str) -> None:
        needle = query.strip().casefold()
        self.filtered_values = self.values if not needle else [
            value
            for value in self.values
            if needle in f"{value} {self.search_aliases.get(value, '')}".casefold()
        ]
        exact = self.variable.get()
        self.active_index = self.filtered_values.index(exact) if exact in self.filtered_values else 0

    def _fill_listbox(self) -> None:
        if self.listbox is None:
            return
        self.listbox.delete(0, tk.END)
        if self.filtered_values:
            self.listbox.insert(tk.END, *self.filtered_values)
        self._highlight_active()

    def _highlight_active(self) -> None:
        if self.listbox is None or not self.filtered_values:
            return
        self.active_index = max(0, min(self.active_index, len(self.filtered_values) - 1))
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(self.active_index)
        self.listbox.activate(self.active_index)
        self.listbox.see(self.active_index)

    def _focus_in(self, _event: tk.Event) -> None:
        if self._suppress_focus_open:
            self._suppress_focus_open = False
            return
        if self._focus_job is None:
            self._focus_job = self.after_idle(self._select_all_and_open)

    def _entry_clicked(self, _event: tk.Event) -> None:
        try:
            if self.focus_get() is self.entry and self._open_job is None:
                self._open_job = self.after_idle(self._open_idle)
        except tk.TclError:
            return

    def _open_idle(self) -> None:
        self._open_job = None
        try:
            if self.winfo_exists() and self.focus_get() is self.entry:
                self.open()
        except tk.TclError:
            return

    def _select_all_and_open(self) -> None:
        self._focus_job = None
        try:
            if not self.winfo_exists() or self.focus_get() is not self.entry:
                return
            self.entry.selection_range(0, tk.END)
            self.entry.icursor(tk.END)
            self.open()
        except tk.TclError:
            return

    def _focus_out(self, _event: tk.Event) -> None:
        if self._hiding_job:
            try:
                self.after_cancel(self._hiding_job)
            except tk.TclError:
                pass
        self._hiding_job = self.after(150, self._resolve_and_close)

    def _resolve_and_close(self) -> None:
        self._hiding_job = None
        focus = self.focus_get()
        if self.popup is not None and focus is not None and str(focus).startswith(str(self.popup)):
            return
        text = self.variable.get().strip()
        exact = next((value for value in self.values if value.casefold() == text.casefold()), None)
        match = exact or next((value for value in self.values if text and text.casefold() in value.casefold()), None)
        if match and not self.allow_free_text:
            self._select_value(match, refocus=False)
        elif not self.allow_free_text and self.selected_value:
            self.variable.set(self.selected_value)
        self.close()

    def _typed(self, event: tk.Event) -> None:
        if event.keysym in {"Up", "Down", "Return", "Escape", "Tab", "Shift_L", "Shift_R"}:
            return
        value = self.variable.get()
        self._filter(value)
        if self.input_callback:
            self.input_callback(value)
        self._show_popup()

    def _move(self, delta: int) -> str:
        if self.popup is None or not self.popup.winfo_viewable():
            current = self.variable.get()
            query = "" if current in self.values and not self.allow_free_text else current
            self._filter(query)
            self._show_popup()
        if not self.filtered_values:
            return "break"
        self.active_index = (self.active_index + delta) % len(self.filtered_values)
        self._highlight_active()
        return "break"

    def _confirm(self, _event: tk.Event | None = None) -> str:
        if self.filtered_values:
            self._select_value(self.filtered_values[self.active_index])
        elif self.allow_free_text and self.input_callback:
            self.input_callback(self.variable.get())
            self.close()
        return "break"

    def _select_value(self, value: str, refocus: bool = True) -> None:
        if self._hiding_job:
            try:
                self.after_cancel(self._hiding_job)
            except tk.TclError:
                pass
            self._hiding_job = None
        self.variable.set(value)
        self.selected_value = value
        if self.input_callback:
            self.input_callback(value)
        if self.command:
            self.command(value)
        self.close()
        if refocus:
            already_focused = self.focus_get() is self.entry
            if not already_focused:
                self._suppress_focus_open = True
            self.entry.focus_set()
            self.entry.icursor(tk.END)

    def _choose_mouse(self, event: tk.Event) -> None:
        if self.listbox is None:
            return
        index = self.listbox.nearest(event.y)
        if 0 <= index < len(self.filtered_values):
            self.active_index = index
            self._select_value(self.filtered_values[index])

    def _hover(self, event: tk.Event) -> None:
        if self.listbox is None or not self.filtered_values:
            return
        self.active_index = max(0, min(self.listbox.nearest(event.y), len(self.filtered_values) - 1))
        self._highlight_active()

    def _wheel(self, event: tk.Event) -> str:
        if not self.values:
            return "break"
        if self.popup is not None and self.popup.winfo_viewable():
            return self._move(-1 if event.delta > 0 else 1)
        current = self.selected_value if self.selected_value in self.values else self.variable.get()
        index = self.values.index(current) if current in self.values else 0
        index = max(0, min(len(self.values) - 1, index + (-1 if event.delta > 0 else 1)))
        self._select_value(self.values[index])
        return "break"

    def apply_theme(self) -> None:
        self.configure(
            bg=COLORS["card_alt"], highlightbackground=COLORS["accent"],
            highlightcolor=COLORS["accent"],
        )
        self.entry.configure(
            bg=COLORS["card_alt"], fg=COLORS["text"], insertbackground=COLORS["text"],
            selectbackground=COLORS["selection"], selectforeground=COLORS["selection_text"],
        )
        self.arrow.configure(
            bg=COLORS["card_alt"], fg=COLORS["accent"],
            activebackground=COLORS["accent_dark"], activeforeground=COLORS["accent"],
        )
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.configure(bg=COLORS["subtle"])
            if self.listbox is not None:
                self.listbox.configure(
                    bg=COLORS["card_alt"], fg=COLORS["text"],
                    selectbackground=COLORS["accent"], selectforeground=COLORS["on_accent"],
                )

    def _destroy_popup(self, event: tk.Event) -> None:
        if event.widget is self:
            for attr in ("_hiding_job", "_follow_job", "_focus_job", "_open_job", "_position_job"):
                job = getattr(self, attr, None)
                if job is not None:
                    try:
                        self.after_cancel(job)
                    except tk.TclError:
                        pass
                    setattr(self, attr, None)
            try:
                root = self.winfo_toplevel()
                if self._root_configure_binding:
                    root.unbind("<Configure>", self._root_configure_binding)
                if self._root_unmap_binding:
                    root.unbind("<Unmap>", self._root_unmap_binding)
            except tk.TclError:
                pass
            try:
                if self.popup is not None and self.popup.winfo_exists():
                    self.popup.destroy()
            except tk.TclError:
                pass


class ThemePalettePicker(tk.Frame):
    """Collapsible, keyboard-accessible theme gallery with real palette swatches."""

    SWATCH_ROLES = ("bg", "card", "button_bg", "accent", "text")

    def __init__(
        self,
        master: tk.Misc,
        theme: str,
        command: Callable[[str], None],
    ) -> None:
        super().__init__(
            master,
            bg=COLORS["card"],
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["line"],
        )
        self.theme = theme if theme in THEMES else next(iter(THEMES))
        self.command = command
        self.expanded = False
        self.rows: dict[
            str,
            tuple[tk.Frame, tk.Canvas, tk.Label, tk.Label, tk.Label],
        ] = {}
        self._rows_theme_dirty = True

        self.header = tk.Frame(
            self,
            bg=COLORS["card_alt"],
            takefocus=True,
            cursor="hand2",
        )
        self.header.pack(fill="x")
        self.header.grid_columnconfigure(1, weight=1)
        self.header_swatch = tk.Canvas(
            self.header,
            width=150,
            height=30,
            bg=COLORS["card_alt"],
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.header_swatch.grid(row=0, column=0, rowspan=2, sticky="w", padx=(12, 14), pady=10)
        self.header_name = tk.Label(
            self.header,
            bg=COLORS["card_alt"],
            fg=COLORS["text"],
            font=(FONT, 10, "bold"),
            anchor="w",
            cursor="hand2",
        )
        self.header_name.grid(row=0, column=1, sticky="sew", pady=(8, 0))
        self.header_source = tk.Label(
            self.header,
            bg=COLORS["card_alt"],
            fg=COLORS["muted"],
            font=(FONT, 8),
            anchor="w",
            cursor="hand2",
        )
        self.header_source.grid(row=1, column=1, sticky="new", pady=(1, 8))
        self.toggle_button = tk.Button(
            self.header,
            command=self.toggle,
            bg=COLORS["card_alt"],
            fg=COLORS["accent"],
            activebackground=COLORS["accent_dark"],
            activeforeground=COLORS["accent"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            font=(FONT, 9, "bold"),
            padx=12,
        )
        self.toggle_button.grid(row=0, column=2, rowspan=2, sticky="nse", padx=(8, 5), pady=6)

        self.gallery = tk.Frame(self, bg=COLORS["card"])
        self.gallery.grid_columnconfigure(0, weight=1, uniform="theme_gallery")
        self.gallery.grid_columnconfigure(1, weight=1, uniform="theme_gallery")
        for index, name in enumerate(THEMES):
            row = tk.Frame(
                self.gallery,
                bg=COLORS["card_alt"],
                bd=0,
                highlightthickness=1,
                highlightbackground=COLORS["line"],
                takefocus=True,
                cursor="hand2",
            )
            row.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=(0, 5) if index % 2 == 0 else (5, 0),
                pady=(0, 7),
            )
            row.grid_columnconfigure(1, weight=1)
            swatch = tk.Canvas(
                row,
                width=118,
                height=28,
                bg=COLORS["card_alt"],
                bd=0,
                highlightthickness=0,
                cursor="hand2",
            )
            swatch.grid(row=0, column=0, rowspan=2, sticky="w", padx=(9, 11), pady=8)
            label = tk.Label(
                row,
                text=theme_label(name, get_language()),
                bg=COLORS["card_alt"],
                fg=COLORS["text"],
                font=(FONT, 9, "bold"),
                anchor="w",
                cursor="hand2",
            )
            label.grid(row=0, column=1, sticky="sew", pady=(6, 0))
            source = tk.Label(
                row,
                text=self._source_text(name),
                bg=COLORS["card_alt"],
                fg=COLORS["muted"],
                font=(FONT, 7),
                anchor="w",
                cursor="hand2",
            )
            source.grid(row=1, column=1, sticky="new", pady=(0, 6))
            marker = tk.Label(
                row,
                text="当前" if name == self.theme else "",
                bg=COLORS["card_alt"],
                fg=COLORS["accent"],
                font=(FONT, 8, "bold"),
                width=4,
                cursor="hand2",
            )
            marker.grid(row=0, column=2, rowspan=2, sticky="e", padx=(5, 8))
            self.rows[name] = (row, swatch, label, source, marker)
            for widget in (row, swatch, label, source, marker):
                widget.bind("<Button-1>", lambda _event, selected=name: self.choose(selected), add="+")
                widget.bind("<Enter>", lambda _event, selected=name: self._set_hover(selected, True), add="+")
                widget.bind("<Leave>", lambda _event, selected=name: self._set_hover(selected, False), add="+")
            row.bind("<Return>", lambda _event, selected=name: self.choose(selected))
            row.bind("<space>", lambda _event, selected=name: self.choose(selected))

        for widget in (self.header, self.header_swatch, self.header_name, self.header_source):
            widget.bind("<Button-1>", lambda _event: self.toggle(), add="+")
        self.header.bind("<Return>", lambda _event: self.toggle())
        self.header.bind("<space>", lambda _event: self.toggle())
        self.apply_theme()

    @staticmethod
    def _draw_swatch(canvas: tk.Canvas, palette: Mapping[str, str]) -> None:
        canvas.delete("all")
        width = int(canvas.cget("width"))
        height = int(canvas.cget("height"))
        segment = width / len(ThemePalettePicker.SWATCH_ROLES)
        for index, role in enumerate(ThemePalettePicker.SWATCH_ROLES):
            left = round(index * segment)
            right = round((index + 1) * segment)
            canvas.create_rectangle(left, 0, right + 1, height, fill=palette[role], outline="")
        canvas.create_rectangle(0, 0, width - 1, height - 1, outline=palette["line"])

    def toggle(self) -> str:
        self.expanded = not self.expanded
        if self.expanded:
            self.gallery.pack(fill="x", padx=10, pady=(10, 3))
            self._apply_row_theme()
        else:
            self.gallery.pack_forget()
        self._update_header()
        return "break"

    def choose(self, theme: str) -> str:
        if theme not in THEMES:
            return "break"
        self.set_theme(theme)
        self.command(theme)
        return "break"

    def set_theme(self, theme: str) -> None:
        if theme not in THEMES:
            return
        self.theme = theme
        self.apply_theme()

    def _set_hover(self, theme: str, hovered: bool) -> None:
        if theme not in self.rows or theme == self.theme:
            return
        background = COLORS["key"] if hovered else COLORS["card_alt"]
        row, swatch, label, source, marker = self.rows[theme]
        row.configure(bg=background)
        swatch.configure(bg=background)
        label.configure(bg=background)
        source.configure(bg=background)
        marker.configure(bg=background)

    def _update_header(self) -> None:
        self.header_name.configure(text=theme_label(self.theme, get_language()))
        self.header_source.configure(text=self._source_text(self.theme))
        self.toggle_button.configure(
            text=tr("收回主题列表  ▴") if self.expanded else tr(f"展开全部 {len(THEMES)} 套  ▾")
        )
        self._draw_swatch(self.header_swatch, THEMES[self.theme])

    @staticmethod
    def _source_text(theme: str) -> str:
        source_name = THEME_SOURCES[theme][0]
        text_mode = "白色文字" if THEMES[theme]["text"] == "#FFFFFF" else "黑色文字"
        return f"{tr('配色参考')} · {source_name} · {tr(text_mode)}"

    def apply_language(self) -> None:
        for name, (_row, _swatch, label, source, _marker) in self.rows.items():
            label.configure(text=theme_label(name, get_language()))
            source.configure(text=self._source_text(name))
        self._update_header()

    def _apply_row_theme(self) -> None:
        """Refresh the expanded gallery; hidden rows are updated lazily."""

        if not self._rows_theme_dirty and all(swatch.find_all() for _, swatch, *_ in self.rows.values()):
            return
        for name, (row, swatch, label, source, marker) in self.rows.items():
            selected = name == self.theme
            background = COLORS["selection"] if selected else COLORS["card_alt"]
            foreground = COLORS["selection_text"] if selected else COLORS["text"]
            secondary = COLORS["selection_text"] if selected else COLORS["muted"]
            row.configure(bg=background, highlightbackground=COLORS["accent"] if selected else COLORS["line"])
            swatch.configure(bg=background)
            label.configure(bg=background, fg=foreground)
            source.configure(bg=background, fg=secondary)
            marker.configure(
                text="当前" if selected else "",
                bg=background,
                fg=COLORS["selection_text"] if selected else COLORS["accent"],
            )
            # Palette segments describe the candidate theme itself and never
            # change when the active theme changes, so draw each only once.
            if not swatch.find_all():
                self._draw_swatch(swatch, THEMES[name])
        self._rows_theme_dirty = False

    def apply_theme(self) -> None:
        self.configure(bg=COLORS["card"], highlightbackground=COLORS["line"])
        self.header.configure(bg=COLORS["card_alt"])
        self.header_swatch.configure(bg=COLORS["card_alt"])
        self.header_name.configure(bg=COLORS["card_alt"], fg=COLORS["text"])
        self.header_source.configure(bg=COLORS["card_alt"], fg=COLORS["muted"])
        self.toggle_button.configure(
            bg=COLORS["card_alt"],
            fg=COLORS["accent"],
            activebackground=COLORS["accent_dark"],
            activeforeground=COLORS["accent"],
        )
        self.gallery.configure(bg=COLORS["card"])
        self._rows_theme_dirty = True
        if self.expanded:
            self._apply_row_theme()
        self._update_header()


class TreeSelectionBorder:
    """Draw a persistent black outline around the focused Treeview row."""

    def __init__(self, tree: ttk.Treeview, thickness: int = 2) -> None:
        self.tree = tree
        self.thickness = thickness
        self.frames = [tk.Frame(tree, bg="#000000", bd=0, highlightthickness=0) for _ in range(4)]
        self.idle_job: str | None = None
        tree.bind("<<TreeviewSelect>>", self._schedule, add="+")
        tree.bind("<Configure>", self._schedule, add="+")
        tree.bind("<MouseWheel>", self._schedule, add="+")
        tree.bind("<KeyRelease>", self._schedule, add="+")
        tree.bind("<Destroy>", self._destroy, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        try:
            if self.idle_job is None:
                self.idle_job = self.tree.after_idle(self._refresh_idle)
        except tk.TclError:
            pass

    def _refresh_idle(self) -> None:
        self.idle_job = None
        try:
            self.refresh()
        except tk.TclError:
            return

    def refresh(self) -> None:
        if not self.tree.winfo_exists():
            return
        selected = self.tree.selection()
        bbox = self.tree.bbox(selected[0]) if selected else ""
        if not bbox:
            for frame in self.frames:
                frame.place_forget()
            return
        x, y, width, height = (int(value) for value in bbox)
        edge = self.thickness
        placements = (
            (x, y, width, edge),
            (x, y + height - edge, width, edge),
            (x, y, edge, height),
            (x + width - edge, y, edge, height),
        )
        for frame, (left, top, frame_width, frame_height) in zip(self.frames, placements):
            frame.place(x=left, y=top, width=frame_width, height=frame_height)
            frame.lift()

    def _destroy(self, event: tk.Event) -> None:
        if event.widget is self.tree:
            if self.idle_job is not None:
                try:
                    self.tree.after_cancel(self.idle_job)
                except tk.TclError:
                    pass
                self.idle_job = None


class CalculatorKey(tk.Canvas):
    """Rounded, responsive calculator key with keyboard and hover feedback."""

    def __init__(self, master: tk.Misc, label: str, command: Callable[[], None], kind: str = "number") -> None:
        super().__init__(master, bg=COLORS["calculator_bg"], bd=0, highlightthickness=0, takefocus=True, cursor="hand2")
        self.label = label
        self.command = command
        self.kind = kind
        self.hovered = False
        self.pressed = False
        self.bind("<Configure>", self._redraw, add="+")
        self.bind("<Enter>", lambda _event: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda _event: self._set_hover(False), add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self.bind("<Return>", lambda _event: self._invoke(), add="+")
        self.bind("<space>", lambda _event: self._invoke(), add="+")
        self.bind("<FocusIn>", self._redraw, add="+")
        self.bind("<FocusOut>", self._redraw, add="+")

    def _palette(self) -> tuple[str, str, str]:
        if self.kind == "operator":
            return COLORS["calc_operator_bg"], COLORS["calc_operator_text"], COLORS["calc_operator_hover"]
        if self.kind == "function":
            return COLORS["calc_function_bg"], COLORS["calc_function_text"], COLORS["calc_function_hover"]
        return COLORS["calc_number_bg"], COLORS["calc_number_text"], COLORS["calc_number_hover"]

    def _set_hover(self, value: bool) -> None:
        self.hovered = value
        self._redraw()

    def _press(self, _event: tk.Event) -> None:
        self.pressed = True
        self.focus_set()
        self._redraw()

    def _release(self, event: tk.Event) -> None:
        was_pressed = self.pressed
        self.pressed = False
        self._redraw()
        if was_pressed and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.command()

    def _invoke(self) -> str:
        self.command()
        return "break"

    def _redraw(self, _event: tk.Event | None = None) -> None:
        try:
            width = max(2, self.winfo_width())
            height = max(2, self.winfo_height())
        except tk.TclError:
            return
        background, foreground, hover = self._palette()
        fill = hover if self.hovered or self.pressed else background
        self.configure(bg=COLORS["calculator_bg"])
        self.delete("all")
        margin = 3
        radius = max(8, min((height - margin * 2) / 2, 28))
        left, top, right, bottom = margin, margin, width - margin, height - margin
        points = (
            left + radius, top, right - radius, top, right, top,
            right, top + radius, right, bottom - radius, right, bottom,
            right - radius, bottom, left + radius, bottom, left, bottom,
            left, bottom - radius, left, top + radius, left, top,
        )
        focused = self.focus_get() is self
        self.create_polygon(points, smooth=True, splinesteps=24, fill=fill, outline=COLORS["focus"] if focused else fill, width=2 if focused else 1)
        font_size = max(12, min(19, int(height * 0.29)))
        self.create_text(width / 2, height / 2, text=tr(self.label), fill=foreground, font=("Segoe UI", font_size, "bold" if self.kind != "number" else "normal"))

    def apply_theme(self) -> None:
        self._redraw()

    def apply_language(self) -> None:
        self._redraw()


class CalculatorPage(tk.Frame):
    """Apple-inspired keypad with direct formula editing and live preview."""

    STANDARD_KEYS = [
        [("AC", 1), ("±", 1), ("%", 1), ("÷", 1)],
        [("7", 1), ("8", 1), ("9", 1), ("×", 1)],
        [("4", 1), ("5", 1), ("6", 1), ("−", 1)],
        [("1", 1), ("2", 1), ("3", 1), ("+", 1)],
        [("0", 2), (".", 1), ("=", 1)],
    ]
    PRO_KEYS = [
        ["(", ")", "lg", "ln"],
        ["sin", "cos", "tan", "xʸ"],
        ["√", "x!", "±", "π"],
    ]

    def __init__(
        self,
        master: tk.Misc,
        history_callback: Callable[[], None],
        history_changed: Callable[[], None],
        initial_professional: bool = False,
        mode_changed: Callable[[str], None] | None = None,
        angle_mode: str = "DEG",
        history_limit: int = 30,
        initial_history: list[tuple[str, str]] | None = None,
        copy_result_format: str = "number",
    ) -> None:
        super().__init__(master, bg=COLORS["bg"])
        self.model = CalculatorModel(
            angle_mode=angle_mode,
            history=list(initial_history or []),
            history_limit=history_limit,
        )
        self.history_callback = history_callback
        self.history_changed = history_changed
        self.mode_changed = mode_changed or (lambda _mode: None)
        self.copy_result_format = copy_result_format
        self.professional = initial_professional
        self.animating = False
        self.expression_var = tk.StringVar(value="0")
        self.result_var = DisplayStringVar(value="0")
        self.formula_var = DisplayStringVar(value="输入算式")
        self.inline_history_vars = [DisplayStringVar(value=" ") for _ in range(2)]
        self.mode_var = DisplayStringVar(value="标准模式 · 苹果式键盘 · 直接输入公式")
        self._updating_expression = False
        self.keys: list[CalculatorKey] = []
        self.after_jobs = TkAfterJobs(self)
        self._build()
        self.bind("<Destroy>", self._destroy_jobs, add="+")
        self.set_mode_immediate(initial_professional)
        self.refresh_display()

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = tk.Frame(self, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=34, pady=(26, 12))
        header.grid_columnconfigure(0, weight=1)
        tk.Label(header, text="计算器", bg=COLORS["bg"], fg=COLORS["text"], font=(FONT, 24, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(header, textvariable=self.mode_var, bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, 10)).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.mode_button = AppButton(header, "专业", self.toggle_mode, "muted", 9)
        self.mode_button.grid(row=0, column=1, rowspan=2, padx=(0, 8), ipadx=9, ipady=7)
        self.copy_button = AppButton(header, "复制结果", self.copy_current_result, "ghost", 9)
        self.copy_button.grid(row=0, column=2, rowspan=2, padx=(0, 8), ipadx=8, ipady=7)
        self.history_button = AppButton(header, "打开历史记录", self.history_callback, "outline", 10)
        self.history_button.grid(row=0, column=3, rowspan=2, ipadx=10, ipady=7)

        self.stage_host = tk.Frame(self, bg=COLORS["calculator_bg"])
        self.stage_host.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 24))
        self.stage_host.bind("<Configure>", self._resize_stage, add="+")
        self.stage = tk.Frame(self.stage_host, bg=COLORS["calculator_bg"])
        self.stage.place(relx=0.5, rely=0, anchor="n", relheight=1.0, width=640)
        self.stage.grid_columnconfigure(0, weight=1)
        self.stage.grid_rowconfigure(1, weight=1)
        display = tk.Frame(self.stage, bg=COLORS["display_bg"], height=230)
        display.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        display.grid_propagate(False)
        display.grid_columnconfigure(0, weight=1)
        display.grid_rowconfigure(3, weight=1)
        self.display_frame = display
        self.inline_history_labels: list[tk.Label] = []
        for row, variable in enumerate(self.inline_history_vars):
            label = tk.Label(
                display, textvariable=variable, bg=COLORS["display_bg"],
                fg=COLORS["display_expression"], font=("Segoe UI", 11),
                anchor="e", padx=26,
            )
            label.grid(row=row, column=0, sticky="ew", pady=((13, 1) if row == 0 else 1))
            label.bind("<Button-1>", lambda _event: self.activate_keyboard(), add="+")
            self.inline_history_labels.append(label)
        self.formula_label = tk.Label(
            display, textvariable=self.formula_var, bg=COLORS["display_bg"],
            fg=COLORS["display_expression"], font=("Segoe UI", 16),
            anchor="e", padx=26,
        )
        self.formula_label.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.formula_label.bind("<Button-1>", lambda _event: self.activate_keyboard(), add="+")
        self.expression_entry = tk.Entry(
            display, textvariable=self.expression_var, bg=COLORS["display_bg"], fg=COLORS["display_text"],
            insertbackground=COLORS["display_text"], insertwidth=2,
            selectbackground=COLORS["selection"], selectforeground=COLORS["selection_text"],
            font=("Segoe UI", 48), justify="right", relief="flat", bd=0,
            highlightthickness=0, takefocus=True,
        )
        self.expression_entry.grid(row=3, column=0, sticky="ew", padx=24, pady=(3, 14), ipady=5)
        self.expression_entry.bind("<FocusIn>", self._expression_focus_in)
        self.expression_entry.bind("<KeyPress>", self._expression_keypress, add="+")
        self.expression_entry.bind("<Return>", self._evaluate_manual_expression)
        self.expression_entry.bind("<KP_Enter>", self._evaluate_manual_expression)
        self.expression_var.trace_add("write", self._manual_expression_changed)
        # Compatibility alias for integrations that used the former result
        # label. The editable large-number field is now the result surface.
        self.result_label = self.formula_label
        display.bind("<Button-1>", lambda _event: self.activate_keyboard(), add="+")

        self.keypad_area = tk.Frame(self.stage, bg=COLORS["calculator_bg"])
        self.keypad_area.grid(row=1, column=0, sticky="nsew")
        self.standard_frame = tk.Frame(self.keypad_area, bg=COLORS["calculator_bg"])
        self.pro_frame = tk.Frame(self.keypad_area, bg=COLORS["calculator_bg"])
        self._build_key_frame(self.standard_frame, self.STANDARD_KEYS, professional=False)
        self._build_key_frame(self.pro_frame, self.PRO_KEYS, professional=True)
        self.standard_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

    def _build_key_frame(self, frame: tk.Frame, rows: list, professional: bool) -> None:
        for column in range(4):
            frame.grid_columnconfigure(column, weight=1, uniform="calc")
        for row in range(len(rows)):
            frame.grid_rowconfigure(row, weight=1, uniform="calc")
        for row, values in enumerate(rows):
            column = 0
            for item in values:
                label, span = item if isinstance(item, tuple) else (item, 1)
                if label in {"÷", "×", "−", "+", "="}:
                    kind = "operator"
                elif professional or label in {"AC", "±", "%", "←"}:
                    kind = "function"
                else:
                    kind = "number"
                button = CalculatorKey(frame, label, lambda key=label: self.handle(key), kind)
                button.grid(row=row, column=column, columnspan=span, sticky="nsew", padx=4, pady=4)
                self.keys.append(button)
                column += span

    def _resize_stage(self, event: tk.Event) -> None:
        self._resize_stage_to(int(event.width), int(event.height))

    def _resize_stage_to(self, width: int, height: int) -> None:
        available = max(340, width - 8)
        height = max(320, height)
        self.stage.place_configure(width=available)
        display_height = max(176, min(290, int(height * (0.34 if self.professional else 0.31))))
        self.display_frame.configure(height=display_height)
        large_size = max(30, min(68, int(available / 13), int(display_height * 0.27)))
        formula_size = max(12, min(21, int(available / 38)))
        history_size = max(9, min(14, int(available / 58)))
        self.expression_entry.configure(font=("Segoe UI", large_size))
        self.formula_label.configure(font=("Segoe UI", formula_size))
        for label in self.inline_history_labels:
            label.configure(font=("Segoe UI", history_size))

    def set_history_open(self, is_open: bool) -> None:
        self.history_button.configure(text="关闭历史记录" if is_open else "打开历史记录")

    def set_mode_immediate(self, professional: bool) -> None:
        self.professional = professional
        self.animating = False
        if professional:
            self.pro_frame.place(relx=0, rely=0, relwidth=1, relheight=0.375)
            self.standard_frame.place(relx=0, rely=0.375, relwidth=1, relheight=0.625)
        else:
            self.pro_frame.place_forget()
            self.standard_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.mode_button.configure(text="标准" if professional else "专业")
        self.mode_var.set("专业模式 · 科学键盘 · 直接输入公式" if professional else "标准模式 · 苹果式键盘 · 直接输入公式")
        try:
            self._resize_stage_to(
                self.stage_host.winfo_width(), self.stage_host.winfo_height()
            )
        except (AttributeError, tk.TclError):
            pass

    def apply_calculator_settings(self, angle_mode: str, history_limit: int, copy_format: str) -> None:
        self.model.angle_mode = angle_mode if angle_mode in {"DEG", "RAD"} else "DEG"
        self.model.history_limit = max(1, min(200, int(history_limit)))
        del self.model.history[self.model.history_limit:]
        self.copy_result_format = copy_format
        self.refresh_display()

    def copy_current_result(self) -> None:
        result = self.result_var.get() or "0"
        if self.copy_result_format == "grouped":
            plain = result.replace(",", "")
            sign = "-" if plain.startswith("-") else ""
            unsigned = plain[1:] if sign else plain
            whole, dot, fraction = unsigned.partition(".")
            if whole.isdigit() and (not dot or fraction.isdigit()):
                result = sign + f"{int(whole):,}" + (dot + fraction if fraction else "")
        elif self.copy_result_format == "formula":
            expression = (
                self.model.history[0][0]
                if self.model.just_evaluated and self.model.history
                else self.expression_var.get()
            )
            result = f"{expression} = {result}"
        self.clipboard_clear()
        self.clipboard_append(result)
        self.copy_button.configure(text="已复制")
        self.after_jobs.schedule(900, lambda: self.copy_button.configure(text="复制结果"))

    def toggle_mode(self) -> None:
        if self.animating:
            return
        target_professional = not self.professional
        self.animating = True
        if target_professional:
            self.pro_frame.place(relx=0, rely=0, relwidth=1, relheight=0.01)
            self.pro_frame.lower(self.standard_frame)
        frames = 16

        def step(index: int) -> None:
            raw = index / frames
            eased = 1 - (1 - raw) ** 3
            progress = eased if target_professional else 1 - eased
            self.standard_frame.place(
                relx=0,
                rely=0.375 * progress,
                relwidth=1,
                relheight=1 - 0.375 * progress,
            )
            self.pro_frame.place(relx=0, rely=0, relwidth=1, relheight=max(0.01, 0.375 * progress))
            if index < frames:
                self.after_jobs.schedule(16, lambda: step(index + 1))
                return
            self.professional = target_professional
            self.animating = False
            self.mode_button.configure(text="标准" if self.professional else "专业")
            self.mode_var.set("专业模式 · 科学键盘 · 直接输入公式" if self.professional else "标准模式 · 苹果式键盘 · 直接输入公式")
            self.mode_changed("professional" if self.professional else "standard")
            if not self.professional:
                self.pro_frame.place_forget()
                self.standard_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            try:
                self._resize_stage_to(
                    self.stage_host.winfo_width(), self.stage_host.winfo_height()
                )
            except tk.TclError:
                pass

        step(0)

    def handle(self, key: str) -> None:
        try:
            if key in {"专业", "标准"}:
                self.toggle_mode()
            elif key == "AC":
                self.model.clear()
            elif key == "←":
                self.model.backspace()
            elif key == "%":
                self.model.input("%")
            elif key == "=":
                self.model.equals()
                self.history_changed()
            elif key == "±":
                self.model.toggle_sign()
            elif key == "lg":
                self.model.wrap("log(")
            elif key in {"ln", "sin", "cos", "tan"}:
                self.model.wrap(f"{key}(")
            elif key == "xʸ":
                self.model.input("^")
            elif key == "√":
                self.model.wrap("sqrt(")
            elif key == "x!":
                self.model.wrap("fact(")
            else:
                self.model.input(key)
            self.refresh_display()
        except CalculationError as exc:
            self.result_var.set(str(exc))
            self.after_jobs.schedule(1700, self.refresh_display)

    def _expression_focus_in(self, _event: tk.Event) -> None:
        if self.expression_var.get() == "0":
            self.expression_entry.selection_range(0, tk.END)

    def _expression_keypress(self, event: tk.Event) -> str | None:
        if int(getattr(event, "state", 0)) & 0x000C:
            return None
        raw_char = str(getattr(event, "char", ""))
        normalized = normalize_amount_input(raw_char)
        if normalized == "=":
            self.after_jobs.schedule(0, self._evaluate_manual_expression, idle=True)
            return "break"
        if normalized and normalized != raw_char:
            self._insert_expression_token(normalized)
            return "break"
        if not raw_char:
            keysym = str(getattr(event, "keysym", ""))
            keypad = {
                **{f"KP_{digit}": digit for digit in "0123456789"},
                "KP_Add": "+",
                "KP_Subtract": "-",
                "KP_Multiply": "*",
                "KP_Divide": "/",
                "KP_Decimal": ".",
                "KP_Separator": ".",
            }
            token = keypad.get(keysym)
            if token is not None:
                self._insert_expression_token(token)
                return "break"
        return None

    def _insert_expression_token(self, token: str) -> None:
        """Insert normalized keyboard text without relying on a mapped Tk window."""

        current = self.expression_var.get()
        try:
            start = int(self.expression_entry.index(tk.SEL_FIRST))
            end = int(self.expression_entry.index(tk.SEL_LAST))
        except tk.TclError:
            try:
                start = end = int(self.expression_entry.index(tk.INSERT))
            except tk.TclError:
                start = end = len(current)
        start = max(0, min(start, len(current)))
        end = max(start, min(end, len(current)))
        self.expression_var.set(current[:start] + token + current[end:])
        try:
            self.expression_entry.icursor(start + len(token))
        except tk.TclError:
            pass

    def _manual_expression_changed(self, *_args) -> None:
        if self._updating_expression:
            return
        text = self.expression_var.get()
        self.model.set_expression(text)
        if len(text) > 512:
            self._updating_expression = True
            self.expression_var.set(self.model.expression)
            self._updating_expression = False
        self.result_var.set(self.model.preview() or "0")
        self._refresh_context_lines()

    def _evaluate_manual_expression(self, _event: tk.Event | None = None) -> str:
        try:
            self.model.set_expression(self.expression_var.get())
            self.model.equals()
            self.history_changed()
            self.refresh_display()
            self.expression_entry.icursor(tk.END)
        except CalculationError as exc:
            self.result_var.set(str(exc))
            self.after_jobs.schedule(1700, self.refresh_display)
        return "break"

    def refresh_display(self) -> None:
        expression = self.model.display_expression()
        self._updating_expression = True
        self.expression_var.set(expression)
        self._updating_expression = False
        self.result_var.set(self.model.preview() or "0")
        self._refresh_context_lines()

    def _refresh_context_lines(self) -> None:
        """Render two durable prior calculations and the active formula."""

        history = list(self.model.history)
        if self.model.just_evaluated and history:
            active_expression, _active_result = history[0]
            prior = history[1:3]
            self.formula_var.set(f"{active_expression} =")
        else:
            prior = history[:2]
            expression = self.expression_var.get().strip()
            preview = self.result_var.get().strip()
            self.formula_var.set(
                f"实时结果  =  {preview}" if expression and expression != "0" else "输入算式"
            )
        rendered = [f"{expression} = {result}" for expression, result in reversed(prior)]
        rendered = ([" "] * (2 - len(rendered))) + rendered
        for variable, text in zip(self.inline_history_vars, rendered):
            variable.set(text)

    def on_show(self) -> None:
        self.after_jobs.schedule(0, self.activate_keyboard, idle=True)
        self.after_jobs.schedule(80, self.activate_keyboard)

    def activate_keyboard(self) -> None:
        try:
            self.expression_entry.focus_force()
            self.expression_entry.icursor(tk.END)
        except tk.TclError:
            pass

    def apply_language(self) -> None:
        self.set_mode_immediate(self.professional)
        for key in self.keys:
            key.apply_language()

    def _destroy_jobs(self, event: tk.Event) -> None:
        if event.widget is self:
            self.after_jobs.cancel_all()


class DualConverterPage(tk.Frame):
    """Searchable, sortable three-way fiat or crypto conversion page."""

    def __init__(
        self,
        master: tk.Misc,
        service: RateService,
        mode: str,
        refresh_callback: Callable[[], None],
        timestamp_formatter: Callable[[str], str],
        favorite_codes: list[str] | None = None,
        pinned_codes: list[str] | None = None,
        preference_callback: Callable[[str, list[str], list[str]], None] | None = None,
        coordinator: ExchangeCoordinator | None = None,
        page_state: Mapping[str, object] | None = None,
        state_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        super().__init__(master, bg=COLORS["bg"])
        self.service = service
        self.mode = mode
        self.refresh_callback = refresh_callback
        self.timestamp_formatter = timestamp_formatter
        self.favorite_codes = set(favorite_codes or [])
        self.pinned_codes = list(dict.fromkeys(pinned_codes or []))
        self.favorites_only = False
        self.preference_callback = preference_callback or (lambda _mode, _favorites, _pins: None)
        self.coordinator = coordinator
        self.state_callback = state_callback or (lambda _state: None)
        restored = dict(page_state or {})
        restored_amounts = restored.get("amounts")
        if not isinstance(restored_amounts, list) or len(restored_amounts) != 3:
            restored_amounts = ["1000" if mode == "fiat" else "10000", "", ""]
        restored_codes = restored.get("currencies")
        if not isinstance(restored_codes, list) or len(restored_codes) != 3:
            restored_codes = []
        self.restored_codes = tuple(str(code).strip().upper() for code in restored_codes)
        self.restored_table_base = str(restored.get("table_base", "")).strip().upper()
        self.title = "货币换算" if mode == "fiat" else "虚拟币换算"
        self.amount_a = tk.StringVar(value=str(restored_amounts[0])[:128])
        self.amount_b = tk.StringVar(value=str(restored_amounts[1])[:128])
        self.amount_c = tk.StringVar(value=str(restored_amounts[2])[:128])
        self.currency_a = tk.StringVar()
        self.currency_b = tk.StringVar()
        self.currency_c = tk.StringVar()
        self.table_base_var = tk.StringVar()
        restored_reference = str(restored.get("reference_amount", "1"))[:80]
        self.reference_amount_var = tk.StringVar(value=restored_reference)
        try:
            self.reference_amount_value = evaluate_basic_amount(restored_reference)
        except CalculationError:
            self.reference_amount_value = 1.0
            self.reference_amount_var.set("1")
        meaningful = (
            "支持全球货币 A/B/C 三端联动；金额框可直接计算加减乘除、除余和括号。"
            if mode == "fiat" else
            "法币与虚拟币可自由组合为 A/B/C 三端；金额框可直接输入基础算式。"
        )
        self.status_var = DisplayStringVar(value=meaningful)
        self.refresh_stamp_var = DisplayStringVar(value="最新刷新：等待联网")
        self.search_var = tk.StringVar()
        self.rate_var = DisplayStringVar(value="在任意一端输入金额或算式，按回车、= 或点击输入框外完成计算与换算")
        restored_side = str(restored.get("active_side", "a")).lower()
        self.active_side = restored_side if restored_side in {"a", "b", "c"} else "a"
        # The dedicated C2C page owns all peer-to-peer quotes. This three-way
        # crypto page deliberately mirrors Market Exchange and never switches
        # to an amount-matched C2C price.
        self.exchange_mode = "market"
        restored_provider = str(restored.get("provider", "auto")).lower()
        self.c2c_provider = restored_provider if restored_provider in PROVIDER_LABELS else "auto"
        restored_payment = str(restored.get("payment_method", ""))
        self.c2c_payment_method = restored_payment if len(restored_payment) <= 64 else ""
        self.mode_var = tk.StringVar(value=tr("C2C 按金额" if self.exchange_mode == "c2c" else "普通汇率"))
        self.provider_var = tk.StringVar(value=tr(PROVIDER_LABELS[self.c2c_provider]))
        self.payment_var = tk.StringVar(value=tr(PAYMENT_ALL_LABEL))
        self.payment_by_label: dict[str, str] = {tr(PAYMENT_ALL_LABEL): ""}
        self.payment_cache: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
        self.payment_request: tuple[str, str, int] | None = None
        self.conversion_generation = 0
        self.conversion_cancel = threading.Event()
        self.c2c_edge_results: dict[int, ExchangeEdgeResult] = {}
        self.c2c_pending_slots: set[int] = set()
        self.crypto_payment_generation = 0
        self.payment_supported_fiat = ""
        self.current_snapshot: RateSnapshot | None = None
        self.current_snapshot_from_cache = False
        self.visible = False
        self.conversion_bridge = TkResultBridge(self, self._finish_c2c_conversion)
        self.payment_bridge = TkResultBridge(self, self._finish_crypto_payment_options)
        self.display_to_code: dict[str, str] = {}
        self.code_to_display: dict[str, str] = {}
        self.combo_a_all: list[str] = []
        self.combo_b_all: list[str] = []
        self.combo_c_all: list[str] = []
        self.table_rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.table_default_rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.table_item_codes: dict[str, str] = {}
        self.render_generation = 0
        self.render_job: str | None = None
        self.action_position_job: str | None = None
        self.action_fade_generation = 0
        self.after_jobs = TkAfterJobs(self)
        self.sort_reverse: dict[str, bool] = {}
        self.refreshing = False
        self.spinner_job: str | None = None
        self._build()
        self.bind("<Destroy>", self._destroy_jobs, add="+")

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = tk.Frame(self, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(25, 14))
        header.grid_columnconfigure(0, weight=1)
        tk.Label(header, text=self.title, bg=COLORS["bg"], fg=COLORS["text"], font=(FONT, 23, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(header, textvariable=self.status_var, bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, 9)).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.refresh_button = AppButton(header, "↻  刷新汇率", self.refresh_callback, "accent", 10)
        self.refresh_button.grid(row=0, column=1, rowspan=2, padx=(10, 10), ipadx=10, ipady=8)
        tk.Label(
            header, textvariable=self.refresh_stamp_var, bg=COLORS["accent_dark"], fg=COLORS["accent"],
            font=(FONT, 9, "bold"), padx=13, pady=9,
        ).grid(row=0, column=2, rowspan=2)
        self.mode_combo: ttk.Combobox | None = None
        self.provider_combo: ttk.Combobox | None = None
        self.payment_combo: ttk.Combobox | None = None
        if self.mode == "crypto":
            tk.Label(
                header, text="公开市场参考汇率（非 C2C）", bg=COLORS["card"],
                fg=COLORS["text"], font=(FONT, 9, "bold"), padx=12, pady=8,
            ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        body = self.body = tk.Frame(self, bg=COLORS["bg"])
        body.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 26))
        body.grid_columnconfigure(0, weight=5, uniform="convert")
        body.grid_columnconfigure(1, weight=7, uniform="convert")
        body.grid_rowconfigure(0, weight=1)

        converter = self.converter_card = tk.Frame(body, bg=COLORS["card"])
        converter.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        converter.grid_columnconfigure(0, weight=1)
        tk.Label(converter, text="三端金额联动换算", bg=COLORS["card"], fg=COLORS["text"], font=(FONT, 15, "bold")).grid(row=0, column=0, sticky="w", padx=22, pady=(18, 3))
        tk.Label(converter, text="支持 +  −  ×  ÷  %  ( )；任意一端计算后另外两端同步更新", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9)).grid(row=1, column=0, sticky="w", padx=22, pady=(0, 10))

        self.entry_a = self._amount_entry(converter, "A  金额", self.amount_a, 2, "a")
        self.combo_a = SearchSelect(
            converter, self.currency_a, command=lambda _value: self.selection_changed("a"),
            width=26, font_size=11,
        )
        self.combo_a.grid(row=3, column=0, sticky="ew", padx=22, pady=(5, 7))

        AppButton(converter, "⇅  交换 A / B", lambda: self.swap_pair("a", "b"), "outline", 9).grid(
            row=4, column=0, sticky="ew", padx=22, pady=(1, 5), ipady=4,
        )

        self.entry_b = self._amount_entry(converter, "B  金额", self.amount_b, 5, "b")
        self.combo_b = SearchSelect(
            converter, self.currency_b, command=lambda _value: self.selection_changed("b"),
            width=26, font_size=11,
        )
        self.combo_b.grid(row=6, column=0, sticky="ew", padx=22, pady=(5, 7))

        AppButton(converter, "⇅  交换 B / C", lambda: self.swap_pair("b", "c"), "outline", 9).grid(
            row=7, column=0, sticky="ew", padx=22, pady=(1, 5), ipady=4,
        )

        self.entry_c = self._amount_entry(converter, "C  金额", self.amount_c, 8, "c")
        self.combo_c = SearchSelect(
            converter, self.currency_c, command=lambda _value: self.selection_changed("c"),
            width=26, font_size=11,
        )
        self.combo_c.grid(row=9, column=0, sticky="ew", padx=22, pady=(5, 8))

        rate_box = tk.Frame(converter, bg=COLORS["accent_dark"])
        rate_box.grid(row=10, column=0, sticky="ew", padx=22, pady=(0, 16))
        tk.Label(rate_box, textvariable=self.rate_var, bg=COLORS["accent_dark"], fg=COLORS["accent"], font=(FONT, 9), wraplength=410, justify="left").pack(anchor="w", padx=14, pady=10)

        list_card = self.list_card = tk.Frame(body, bg=COLORS["card"])
        list_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        list_card.grid_columnconfigure(0, weight=1)
        list_card.grid_rowconfigure(2, weight=1)
        list_title = "货币参考表" if self.mode == "fiat" else "主流虚拟币行情"
        list_header = tk.Frame(list_card, bg=COLORS["card"])
        list_header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=22, pady=(18, 3))
        list_header.grid_columnconfigure(0, weight=1)
        tk.Label(list_header, text=list_title, bg=COLORS["card"], fg=COLORS["text"], font=(FONT, 15, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            list_header, text="搜索", bg=COLORS["accent"], fg=COLORS["on_accent"],
            font=(FONT, 8, "bold"), padx=10, pady=6,
        ).grid(row=0, column=1, padx=(6, 6))
        self.search_selector = SearchSelect(
            list_header, self.search_var, input_callback=self._search_table_changed,
            allow_free_text=True, width=14, font_size=9, max_rows=7,
        )
        self.search_selector.grid(row=0, column=2, sticky="ew")

        reference_row = self.reference_row = tk.Frame(list_card, bg=COLORS["card"])
        reference_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=22, pady=(3, 10))
        reference_row.grid_columnconfigure(2, weight=1)
        self.reference_base_label = tk.Label(reference_row, text="参考币种", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9, "bold"))
        self.reference_base_label.grid(row=0, column=0, padx=(0, 8))
        self.table_base_selector = SearchSelect(
            reference_row, self.table_base_var, command=lambda _value: self.table_base_changed(),
            width=12, font_size=9, max_rows=7,
        )
        self.table_base_selector.grid(row=0, column=1, sticky="w")
        self.reset_order_button = AppButton(reference_row, "默认顺序", self.reset_table_order, "soft_accent", 8)
        self.reset_order_button.grid(
            row=0, column=3, padx=(12, 10), ipady=5,
        )
        self.reference_amount_label = tk.Label(reference_row, text="参考币数额", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9, "bold"))
        self.reference_amount_label.grid(row=0, column=4, padx=(0, 8))
        self.reference_amount_entry = tk.Entry(
            reference_row, textvariable=self.reference_amount_var, width=18,
            bg=COLORS["card_alt"], fg=COLORS["text"], insertbackground=COLORS["text"],
            selectbackground=COLORS["selection"], selectforeground=COLORS["selection_text"],
            font=("Segoe UI", 10, "bold"), bd=0, highlightthickness=1,
            highlightbackground=COLORS["accent"], highlightcolor=COLORS["accent"],
            justify="right",
            validate="key", validatecommand=(self.register(self._validate_reference_amount), "%P"),
        )
        self.reference_amount_entry.grid(row=0, column=5, sticky="e", ipady=6)
        self.reference_amount_entry.bind("<KeyPress>", self._reference_amount_keypress)
        self.reference_amount_entry.bind("<Return>", self._commit_reference_amount)
        self.reference_amount_entry.bind("<KP_Enter>", self._commit_reference_amount)
        self.reference_amount_entry.bind("<FocusOut>", self._commit_reference_amount)

        columns = ("code", "name", "rate", "change", "region", "favorite", "pin") if self.mode == "fiat" else ("code", "name", "rate", "change", "favorite", "pin")
        self.table = ttk.Treeview(list_card, columns=columns, show="headings", selectmode="browse")
        self.table.heading("code", text="代码 ↕", command=lambda: self.sort_table("code", False))
        self.table.heading("name", text="名称 ↕", command=lambda: self.sort_table("name", False))
        self.table.heading("rate", text="换算数量 ↕", command=lambda: self.sort_table("rate", True))
        self.table.heading("change", text="24h ↕", command=lambda: self.sort_table("change", True))
        self.table.column("change", width=68, anchor="e", stretch=False)
        if self.mode == "fiat":
            self.table.heading("region", text="地区 ↕", command=lambda: self.sort_table("region", False))
            self.table.column("region", width=72, anchor="center", stretch=False)
        self.table.heading("favorite", text="收藏", command=self.toggle_favorites_filter)
        self.table.heading("pin", text="置顶")
        self.table.column("code", width=58, anchor="w", stretch=False)
        self.table.column("name", width=170 if self.mode == "fiat" else 135, anchor="w")
        self.table.column("rate", width=130, anchor="e")
        self.table.column("favorite", width=38, anchor="center", stretch=False)
        self.table.column("pin", width=38, anchor="center", stretch=False)
        self.table.grid(row=2, column=0, sticky="nsew", padx=(18, 4), pady=(0, 16))
        scroll = ttk.Scrollbar(list_card, orient="vertical", command=self._table_yview)
        scroll.grid(row=2, column=1, sticky="ns", padx=(0, 12), pady=(0, 16))
        self.table.configure(yscrollcommand=lambda first, last: self._table_scrolled(scroll, first, last))
        self.table.tag_configure("up", background=COLORS["up_row"], foreground=COLORS["text"])
        self.table.tag_configure("down", background=COLORS["down_row"], foreground=COLORS["text"])
        self.table_selection_border = TreeSelectionBorder(self.table)
        self.table.bind("<Double-Button-1>", self.pick_table_item)
        self.table.bind("<<TreeviewSelect>>", self._show_row_actions, add="+")
        self.table.bind("<Configure>", self._queue_action_position, add="+")
        self.favorite_button = tk.Button(
            self.table, text="☆", command=self.toggle_favorite, bg=COLORS["card"], fg=COLORS["accent"],
            activebackground=COLORS["card"], activeforeground=COLORS["accent"], relief="flat", bd=0,
            highlightthickness=0, font=("Segoe UI Symbol", 12, "bold"), cursor="hand2",
        )
        self.pin_button = tk.Button(
            self.table, text="⇧", command=self.toggle_pin, bg=COLORS["card"], fg=COLORS["accent"],
            activebackground=COLORS["card"], activeforeground=COLORS["accent"], relief="flat", bd=0,
            highlightthickness=0, font=(FONT, 10, "bold"), cursor="hand2",
        )
        self._reference_compact: bool | None = None
        list_card.bind("<Configure>", self._responsive_reference_controls, add="+")

    @staticmethod
    def _provider_labels() -> dict[str, str]:
        return {provider: tr(label) for provider, label in PROVIDER_LABELS.items()}

    @classmethod
    def _provider_code(cls, label: str) -> str:
        return next(
            (provider for provider, display in cls._provider_labels().items() if display == label),
            "auto",
        )

    def apply_language(self) -> None:
        if self.mode == "crypto":
            self.mode_var.set(tr("C2C 按金额" if self.exchange_mode == "c2c" else "普通汇率"))
            provider_labels = self._provider_labels()
            self.provider_var.set(provider_labels.get(self.c2c_provider, provider_labels["auto"]))
            if self.mode_combo is not None:
                self.mode_combo.configure(values=(tr("普通汇率"), tr("C2C 按金额")))
            if self.provider_combo is not None:
                self.provider_combo.configure(values=tuple(provider_labels.values()))
        if self.current_snapshot is not None:
            self.apply_snapshot(
                self.current_snapshot,
                self.current_snapshot_from_cache,
                animated=False,
            )

    def _responsive_reference_controls(self, event: tk.Event | None = None) -> None:
        width = int(event.width) if event is not None else int(self.list_card.winfo_width())
        compact = width < 560
        if compact == self._reference_compact:
            return
        self._reference_compact = compact
        for column in range(6):
            self.reference_row.grid_columnconfigure(column, weight=0)
        if compact:
            self.reference_row.grid_columnconfigure(1, weight=1)
            self.reference_row.grid_columnconfigure(4, weight=1)
            self.reference_base_label.grid_configure(row=0, column=0, padx=(0, 8), pady=(0, 7))
            self.table_base_selector.grid_configure(row=0, column=1, sticky="ew", pady=(0, 7))
            self.reset_order_button.grid_configure(row=0, column=2, padx=(10, 0), pady=(0, 7))
            self.reference_amount_label.grid_configure(row=1, column=0, padx=(0, 8))
            self.reference_amount_entry.grid_configure(row=1, column=1, columnspan=2, sticky="ew")
        else:
            self.reference_row.grid_columnconfigure(2, weight=1)
            self.reference_base_label.grid_configure(row=0, column=0, padx=(0, 8), pady=0)
            self.table_base_selector.grid_configure(row=0, column=1, columnspan=1, sticky="w", pady=0)
            self.reset_order_button.grid_configure(row=0, column=3, padx=(12, 10), pady=0)
            self.reference_amount_label.grid_configure(row=0, column=4, padx=(0, 8))
            self.reference_amount_entry.grid_configure(row=0, column=5, columnspan=1, sticky="e")

    def _amount_entry(self, parent: tk.Misc, label: str, variable: tk.StringVar, row: int, side: str) -> tk.Entry:
        container = tk.Frame(parent, bg=COLORS["card"])
        container.grid(row=row, column=0, sticky="ew", padx=22)
        container.grid_columnconfigure(0, weight=1)
        tk.Label(container, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9)).grid(row=0, column=0, sticky="w")
        entry = tk.Entry(
            container, textvariable=variable, bg=COLORS["card_alt"], fg=COLORS["text"],
            insertbackground=COLORS["accent"], font=("Segoe UI", 21, "bold"), bd=0,
            highlightthickness=1, highlightbackground=COLORS["accent"], highlightcolor=COLORS["accent"],
        )
        entry.grid(row=1, column=0, sticky="ew", pady=(6, 0), ipady=9)
        entry.bind("<FocusIn>", lambda _e, which=side: self._activate_side(which))
        entry.bind("<KeyPress>", lambda event, which=side: self._amount_keypress(event, which))
        entry.bind("<Return>", lambda _e, which=side: self.convert_from(which))
        entry.bind("<KP_Enter>", lambda _e, which=side: self.convert_from(which))
        entry.bind("<FocusOut>", lambda _e, which=side: self.convert_from(which))
        return entry

    @staticmethod
    def _format(value: float) -> str:
        rendered = format_number(value)
        if "e" in rendered.lower() or abs(value) < 1000:
            return rendered
        whole, dot, fraction = rendered.partition(".")
        grouped = f"{int(whole):,}"
        return grouped + (dot + fraction if fraction else "")

    def _code(self, display: str) -> str:
        return self.display_to_code.get(display, "")

    @staticmethod
    def _validate_reference_amount(proposed: str) -> bool:
        if len(proposed) > 80:
            return False
        normalized = normalize_amount_input(proposed)
        return all(character in "0123456789,.()%+-*/×÷− =\t" for character in normalized)

    def _amount_keypress(self, event: tk.Event, side: str) -> str | None:
        if normalize_amount_input(event.char) == "=":
            self.after_jobs.schedule(0, lambda: self.convert_from(side), idle=True)
            return "break"
        return None

    def _reference_amount_keypress(self, event: tk.Event) -> str | None:
        if normalize_amount_input(event.char) == "=":
            self.after_jobs.schedule(0, self._commit_reference_amount, idle=True)
            return "break"
        return None

    def _commit_reference_amount(self, _event: tk.Event | None = None) -> str:
        try:
            value = evaluate_basic_amount(normalize_amount_input(self.reference_amount_var.get()))
            self.reference_amount_value = value
            self.reference_amount_var.set(self._format(value))
            self.update_table(self.service.snapshot)
            self._save_page_state()
        except CalculationError as exc:
            self.reference_amount_var.set(self._format(self.reference_amount_value))
            self.rate_var.set(f"参考币数额输入有误：{exc}")
        return "break"

    def _page_state(self) -> dict[str, object]:
        variables = {"a": self.amount_a, "b": self.amount_b, "c": self.amount_c}
        input_amounts = ["", "", ""]
        input_amounts[{"a": 0, "b": 1, "c": 2}[self.active_side]] = variables[self.active_side].get()[:128]
        current_codes = [
            self._code(self.currency_a.get()),
            self._code(self.currency_b.get()),
            self._code(self.currency_c.get()),
        ]
        if len(self.restored_codes) == 3:
            current_codes = [pending or current for pending, current in zip(self.restored_codes, current_codes)]
        return {
            "currencies": current_codes,
            "amounts": input_amounts,
            "active_side": self.active_side,
            "table_base": self.restored_table_base or self._code(self.table_base_var.get()),
            "reference_amount": self.reference_amount_var.get()[:80],
            "mode": self.exchange_mode if self.mode == "crypto" else "market",
            "provider": self.c2c_provider if self.mode == "crypto" else "auto",
            "payment_method": self.c2c_payment_method if self.mode == "crypto" else "",
            "favorites": sorted(self.favorite_codes),
            "pinned": list(self.pinned_codes),
        }

    def _save_page_state(self) -> None:
        callback = getattr(self, "state_callback", None)
        if not callable(callback):
            return
        try:
            callback(self._page_state())
        except (AttributeError, KeyError, TypeError, ValueError):
            return

    def _advance_conversion_generation(self) -> int:
        cancel = getattr(self, "conversion_cancel", None)
        if cancel is not None:
            cancel.set()
        self.conversion_cancel = threading.Event()
        generation = int(getattr(self, "conversion_generation", 0)) + 1
        self.conversion_generation = generation
        results = getattr(self, "c2c_edge_results", None)
        if isinstance(results, dict):
            results.clear()
        pending = getattr(self, "c2c_pending_slots", None)
        if isinstance(pending, set):
            pending.clear()
        return generation

    def _clear_pending_restored_code(self, side: str | None = None) -> None:
        pending = list(self.restored_codes) if len(self.restored_codes) == 3 else ["", "", ""]
        if side is None:
            pending = ["", "", ""]
        else:
            index = {"a": 0, "b": 1, "c": 2}.get(side)
            if index is not None:
                pending[index] = ""
        self.restored_codes = tuple(pending) if any(pending) else ()

    def _activate_side(self, side: str) -> None:
        if side not in {"a", "b", "c"} or getattr(self, "active_side", "a") == side:
            return
        self.active_side = side
        self._advance_conversion_generation()
        self._crypto_payment_context_changed()
        self._save_page_state()

    def _crypto_payment_context_changed(self) -> None:
        if getattr(self, "mode", "fiat") != "crypto":
            return
        self.c2c_payment_method = ""
        self.crypto_payment_generation += 1
        self._refresh_crypto_payment_options()

    def flush_state(self) -> None:
        self._save_page_state()

    def on_show(self) -> None:
        self.visible = True
        if self.current_snapshot is not None:
            self.convert_from(self.active_side)

    def on_hide(self) -> None:
        self.visible = False
        self._advance_conversion_generation()
        self._save_page_state()

    def _update_crypto_control_states(self) -> None:
        if self.mode != "crypto" or self.provider_combo is None or self.payment_combo is None:
            return
        enabled = self.exchange_mode == "c2c"
        self.provider_combo.configure(state="readonly" if enabled else "disabled")
        self.payment_combo.configure(
            state="readonly" if enabled and self.payment_supported_fiat else "disabled"
        )

    def _crypto_mode_changed(self, _event: tk.Event | None = None) -> None:
        self.exchange_mode = "c2c" if self.mode_var.get() == tr("C2C 按金额") else "market"
        self._advance_conversion_generation()
        self._update_crypto_control_states()
        self._refresh_crypto_payment_options()
        self._save_page_state()
        self.convert_from(self.active_side)

    def _crypto_provider_changed(self, _event: tk.Event | None = None) -> None:
        self.c2c_provider = self._provider_code(self.provider_var.get())
        self.c2c_payment_method = ""
        self._advance_conversion_generation()
        self.crypto_payment_generation += 1
        self._refresh_crypto_payment_options()
        self._save_page_state()
        self.convert_from(self.active_side)

    def _crypto_payment_changed(self, _event: tk.Event | None = None) -> None:
        self.c2c_payment_method = self.payment_by_label.get(self.payment_var.get(), "")
        self._advance_conversion_generation()
        self._save_page_state()
        self.convert_from(self.active_side)

    def _refresh_crypto_payment_options(self) -> None:
        if self.mode != "crypto" or self.payment_combo is None:
            return
        fiat = ""
        if self.current_snapshot is not None:
            codes = [self._code(display) for display in (self.currency_a.get(), self.currency_b.get(), self.currency_c.get())]
            fiats = [code for code in codes if self.current_snapshot.kinds.get(code) == "fiat"]
            active_code = codes[{"a": 0, "b": 1, "c": 2}.get(self.active_side, 0)]
            if self.current_snapshot.kinds.get(active_code) == "fiat":
                fiat = active_code
            elif len(fiats) == 1:
                fiat = fiats[0]
        self.payment_supported_fiat = fiat
        if not fiat:
            self.c2c_payment_method = ""
        key = (self.c2c_provider, fiat)
        options = self.payment_cache.get(key, ())
        if options:
            all_label = tr(PAYMENT_ALL_LABEL)
            self.payment_by_label = {all_label: ""}
            for identifier, name in options:
                self.payment_by_label[f"{name} · {identifier}"] = identifier
            selected = next(
                (label for label, identifier in self.payment_by_label.items() if identifier == self.c2c_payment_method),
                all_label,
            )
        else:
            unknown_label = tr(PAYMENT_UNKNOWN_LABEL)
            self.payment_by_label = {unknown_label: ""}
            selected = unknown_label
        self.payment_combo.configure(values=tuple(self.payment_by_label))
        self.payment_var.set(selected)
        self._update_crypto_control_states()
        request = (key[0], key[1], self.crypto_payment_generation)
        if (
            self.visible
            and self.exchange_mode == "c2c"
            and self.coordinator is not None
            and fiat
            and key not in self.payment_cache
            and request != self.payment_request
        ):
            self.payment_request = request
            self.payment_bridge.expect()

            def worker() -> None:
                loaded = self.coordinator.payment_method_options(key[0], key[1])
                self.payment_bridge.deliver(key[0], key[1], request[2], loaded)

            threading.Thread(target=worker, daemon=True, name="crypto-payment-methods").start()

    def _finish_crypto_payment_options(
        self,
        provider: object,
        fiat: object,
        generation: object,
        options: object,
    ) -> None:
        request = (str(provider), str(fiat), int(generation))
        if self.payment_request == request:
            self.payment_request = None
        if request[0] != self.c2c_provider or request[2] != self.crypto_payment_generation:
            return
        cleaned: list[tuple[str, str]] = []
        for item in tuple(options) if isinstance(options, (tuple, list)) else ():
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            identifier = str(item[0]).strip()
            name = str(item[1] or identifier).strip()[:80]
            if identifier and identifier not in {row[0] for row in cleaned}:
                cleaned.append((identifier, name))
        self.payment_cache[(request[0], request[1])] = tuple(cleaned)
        if self.c2c_payment_method and self.c2c_payment_method not in {item[0] for item in cleaned}:
            self.c2c_payment_method = ""
            self._advance_conversion_generation()
            self._save_page_state()
            self.convert_from(self.active_side)
        self._refresh_crypto_payment_options()

    def _start_crypto_quote(self, job: C2CQuoteJob | C2CBridgeJob) -> None:
        if self.coordinator is None or not self.visible:
            return
        self.conversion_bridge.expect()
        cancel = self.conversion_cancel

        def worker() -> None:
            try:
                quote = self.coordinator.execute_job(job, cancel=cancel)
                self.conversion_bridge.deliver(job, quote, None)
            except Exception as exc:
                self.conversion_bridge.deliver(job, None, exc)

        threading.Thread(target=worker, daemon=True, name=f"crypto-c2c-{job.slot}").start()

    def _finish_c2c_conversion(self, job: object, quote: object, error: object) -> None:
        if (
            not self.visible
            or self.current_snapshot is None
            or self.coordinator is None
            or not isinstance(job, (C2CQuoteJob, C2CBridgeJob))
            or job.generation != self.conversion_generation
        ):
            return
        result = self.coordinator.finish_job(
            job,
            quote,
            kinds=self.current_snapshot.kinds,
            error=error if isinstance(error, BaseException) else None,
        )
        if result.generation != self.conversion_generation:
            return
        target_side = {0: "a", 1: "b", 2: "c"}.get(result.slot)
        if target_side is None:
            return
        variables = {"a": self.amount_a, "b": self.amount_b, "c": self.amount_c}
        codes = {
            "a": self._code(self.currency_a.get()),
            "b": self._code(self.currency_b.get()),
            "c": self._code(self.currency_c.get()),
        }
        if result.source != codes.get(self.active_side) or result.target != codes.get(target_side):
            return
        variables[target_side].set(result.display_value if result.valid else "")
        self.c2c_pending_slots.discard(result.slot)
        self.c2c_edge_results[result.slot] = result
        self._render_crypto_c2c_status()

    def _render_crypto_c2c_status(self) -> None:
        codes = {
            "a": self._code(self.currency_a.get()),
            "b": self._code(self.currency_b.get()),
            "c": self._code(self.currency_c.get()),
        }
        rows: list[str] = []
        for slot, side in ((0, "a"), (1, "b"), (2, "c")):
            if side == self.active_side:
                continue
            result = self.c2c_edge_results.get(slot)
            if result is None:
                if slot in self.c2c_pending_slots:
                    rows.append(f"{codes.get(side, side.upper())}：正在获取 C2C 本金额匹配价")
                continue
            detail = "；".join(result.details[:1])
            rows.append(
                f"{result.target}：{result.status}" + (f"（{detail}）" if detail else "")
            )
        if self.c2c_pending_slots:
            rows.append("最低展示价不会冒充本金额可成交价")
        self.rate_var.set("\n".join(rows) or "C2C 按金额换算")

    def apply_snapshot(self, snapshot: RateSnapshot, from_cache: bool = False, animated: bool = False) -> None:
        self.current_snapshot = snapshot
        self.current_snapshot_from_cache = bool(from_cache)
        self.crypto_payment_generation += 1
        desired_codes = self.restored_codes if len(self.restored_codes) == 3 else ("", "", "")
        old_a = self._code(self.currency_a.get())
        old_b = self._code(self.currency_b.get())
        old_c = self._code(self.currency_c.get())
        old_table_base = self._code(self.table_base_var.get())
        fiats = [code for code in snapshot.rates if snapshot.kinds.get(code) == "fiat"]
        cryptos = [code for code in snapshot.rates if snapshot.kinds.get(code) == "crypto"]
        priority_fiat = ["CNY", "USD", "EUR", "JPY", "HKD", "GBP", "AUD", "CAD", "CHF", "SGD", "KRW"]
        fiats.sort(key=lambda code: (priority_fiat.index(code) if code in priority_fiat else 999, code))
        self.display_to_code.clear()
        self.code_to_display.clear()
        fiat_values = [self._register_currency(code, snapshot, False) for code in fiats]
        crypto_values = [self._register_currency(code, snapshot, True) for code in cryptos]

        converter_values = fiat_values if self.mode == "fiat" else fiat_values + crypto_values
        self.combo_a_all = converter_values
        self.combo_b_all = converter_values
        self.combo_c_all = converter_values
        self.combo_a.set_values(self.combo_a_all)
        self.combo_b.set_values(self.combo_b_all)
        self.combo_c.set_values(self.combo_c_all)
        self.table_base_selector.set_values(fiat_values)
        self.search_selector.set_values(fiat_values if self.mode == "fiat" else crypto_values)
        converter_codes = fiats if self.mode == "fiat" else fiats + cryptos
        default_a = desired_codes[0] if desired_codes[0] in converter_codes else old_a if old_a in converter_codes else ("CNY" if "CNY" in converter_codes else converter_codes[0] if converter_codes else "")
        candidates_b = converter_codes
        preferred_b = "USD" if self.mode == "fiat" else "BTC"
        default_b = desired_codes[1] if desired_codes[1] in candidates_b else old_b if old_b in candidates_b else (preferred_b if preferred_b in candidates_b else candidates_b[0] if candidates_b else "")
        preferred_c = "EUR" if self.mode == "fiat" else "ETH"
        default_c = desired_codes[2] if desired_codes[2] in converter_codes else old_c if old_c in converter_codes else (preferred_c if preferred_c in converter_codes else converter_codes[0] if converter_codes else "")
        default_table_base = self.restored_table_base if self.restored_table_base in fiats else old_table_base if old_table_base in fiats else ("CNY" if "CNY" in fiats else fiats[0] if fiats else "")
        self.combo_a.set(self.code_to_display.get(default_a, ""))
        self.combo_b.set(self.code_to_display.get(default_b, ""))
        self.combo_c.set(self.code_to_display.get(default_c, ""))
        self.table_base_selector.set(self.code_to_display.get(default_table_base, ""))
        pending_codes = tuple(
            desired if desired and desired not in converter_codes else ""
            for desired in desired_codes
        )
        self.restored_codes = pending_codes if any(pending_codes) else ()
        if self.restored_table_base in fiats:
            self.restored_table_base = ""
        self.refresh_stamp_var.set(f"最新刷新：{self.timestamp_formatter(snapshot.fetched_at)}")
        self.update_table(snapshot, animate=animated)
        self._refresh_crypto_payment_options()
        self.convert_from(self.active_side)
        self._save_page_state()
        self.refreshing = False
        if self.spinner_job:
            try:
                self.after_cancel(self.spinner_job)
            except tk.TclError:
                pass
            self.spinner_job = None
        self.refresh_button.configure(text="↻  刷新汇率", state="normal")

    def _register_currency(self, code: str, snapshot: RateSnapshot, crypto: bool) -> str:
        source_name = snapshot.names.get(code, code) if crypto else fiat_display_name(code, snapshot.names.get(code, code))
        name = localized_asset_name(code, source_name)
        display = f"₿ {code}  ·  {name}" if crypto else f"{code}  ·  {name}"
        self.display_to_code[display] = code
        self.code_to_display[code] = display
        return display

    def begin_refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self._hide_action_buttons()
        self._set_action_headings()
        self.table.delete(*self.table.get_children())
        self.refresh_button.configure(state="disabled")
        frames = ["刷新中 ·", "刷新中 ··", "刷新中 ···"]

        def spin(index: int = 0) -> None:
            if not self.refreshing:
                return
            self.refresh_button.configure(text=frames[index % len(frames)])
            self.spinner_job = self.after(180, lambda: spin(index + 1))

        spin()

    def finish_refresh_failure(self) -> None:
        self.refreshing = False
        if self.spinner_job:
            try:
                self.after_cancel(self.spinner_job)
            except tk.TclError:
                pass
            self.spinner_job = None
        self.refresh_button.configure(text="↻  刷新汇率", state="normal")

    def convert_from(self, side: str) -> None:
        if not self.service.snapshot.rates:
            return
        variables = {"a": self.amount_a, "b": self.amount_b, "c": self.amount_c}
        codes = {
            "a": self._code(self.currency_a.get()),
            "b": self._code(self.currency_b.get()),
            "c": self._code(self.currency_c.get()),
        }
        input_var = variables[side]
        from_code = codes[side]
        previous_active_side = getattr(self, "active_side", side)
        text = normalize_amount_input(input_var.get()).strip()
        generation = self._advance_conversion_generation()
        if not text:
            for other_side, output_var in variables.items():
                if other_side != side:
                    output_var.set("")
            self.active_side = side
            self._save_page_state()
            return
        try:
            value = evaluate_basic_amount(text)
            if not from_code or any(not code for code in codes.values()):
                raise ValueError
            input_var.set(self._format(value))
            self.active_side = side
            if previous_active_side != side:
                self._crypto_payment_context_changed()
            amount_text = self._format(value).replace(",", "")
            if (
                getattr(self, "mode", "fiat") == "crypto"
                and self.exchange_mode == "c2c"
                and self.coordinator is not None
                and self.current_snapshot is not None
            ):
                statuses: list[str] = []
                jobs: list[C2CQuoteJob | C2CBridgeJob] = []
                for other_side in ("a", "b", "c"):
                    if other_side == side:
                        continue
                    result = self.coordinator.prepare_edge(
                        slot={"a": 0, "b": 1, "c": 2}[other_side],
                        generation=generation,
                        amount=amount_text,
                        source=from_code,
                        target=codes[other_side],
                        kinds=self.current_snapshot.kinds,
                        mode="c2c",
                        provider=self.c2c_provider,
                        payment_method=self.c2c_payment_method,
                        payment_fiat=self.payment_supported_fiat,
                        settlement_fiat=self.payment_supported_fiat or "CNY",
                        from_cache=bool(getattr(self, "current_snapshot_from_cache", False)),
                    )
                    if isinstance(result, (C2CQuoteJob, C2CBridgeJob)):
                        variables[other_side].set("…" if self.visible else "")
                        jobs.append(result)
                    else:
                        variables[other_side].set(result.display_value if result.valid else "")
                        self.c2c_edge_results[result.slot] = result
                        statuses.append(f"{codes[other_side]}：{result.status}")
                if jobs and self.visible:
                    self.c2c_pending_slots.update(job.slot for job in jobs)
                    for job in jobs:
                        self._start_crypto_quote(job)
                    self._render_crypto_c2c_status()
                elif jobs:
                    statuses.append("页面打开后获取 C2C 报价")
                    self.rate_var.set("\n".join(statuses))
                else:
                    self._render_crypto_c2c_status()
                self._save_page_state()
                return
            unit_texts: list[str] = []
            for other_side in ("a", "b", "c"):
                if other_side == side:
                    continue
                to_code = codes[other_side]
                result = self.service.convert(value, from_code, to_code)
                variables[other_side].set(self._format(result))
                unit = self.service.convert(1, from_code, to_code)
                unit_texts.append(f"{self._format(unit)} {to_code}")
            self.rate_var.set(f"1 {from_code} = {'  =  '.join(unit_texts)}\n已按 {side.upper()} 端输入同步换算另外两端")
            self._save_page_state()
        except CalculationError as exc:
            self.rate_var.set(f"{side.upper()} 端金额输入有误：{exc}")
        except (ValueError, KeyError):
            self.rate_var.set("请输入有效金额或算式，并为 A、B、C 三端选择币种")

    def selection_changed(self, side: str) -> None:
        # Changing a target currency must not turn its previously calculated
        # amount into the new source. The last amount field the user edited wins.
        self._clear_pending_restored_code(side)
        self._advance_conversion_generation()
        self._crypto_payment_context_changed()
        self._save_page_state()
        self.convert_from(self.active_side)

    def table_base_changed(self) -> None:
        self.restored_table_base = ""
        self.update_table(self.service.snapshot)
        self._save_page_state()

    def swap_pair(self, left: str, right: str) -> None:
        self._clear_pending_restored_code()
        variables = {"a": self.amount_a, "b": self.amount_b, "c": self.amount_c}
        combos = {"a": self.combo_a, "b": self.combo_b, "c": self.combo_c}
        currency_values = {
            "a": self.currency_a.get(), "b": self.currency_b.get(), "c": self.currency_c.get(),
        }
        amount_values = {side: variable.get() for side, variable in variables.items()}
        combos[left].set(currency_values[right])
        combos[right].set(currency_values[left])
        variables[left].set(amount_values[right])
        variables[right].set(amount_values[left])
        if self.active_side == left:
            self.active_side = right
        elif self.active_side == right:
            self.active_side = left
        self._crypto_payment_context_changed()
        self._save_page_state()
        self.convert_from(self.active_side)

    def rotate(self) -> None:
        self._clear_pending_restored_code()
        displays = (self.currency_a.get(), self.currency_b.get(), self.currency_c.get())
        values = (self.amount_a.get(), self.amount_b.get(), self.amount_c.get())
        self.combo_a.set(displays[2])
        self.combo_b.set(displays[0])
        self.combo_c.set(displays[1])
        self.amount_a.set(values[2])
        self.amount_b.set(values[0])
        self.amount_c.set(values[1])
        self.active_side = {"a": "b", "b": "c", "c": "a"}.get(self.active_side, "a")
        self._crypto_payment_context_changed()
        self._save_page_state()
        self.convert_from(self.active_side)

    def update_table(self, snapshot: RateSnapshot, animate: bool = False) -> None:
        base = self._code(self.table_base_var.get())
        if base not in snapshot.rates:
            return
        amount = self.reference_amount_value
        rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        if self.mode == "fiat":
            codes = [code for code in snapshot.rates if snapshot.kinds.get(code) == "fiat"]
            for code in codes:
                converted = self.service.convert(amount, base, code)
                change = 0.0 if code == base else relative_rate_change(snapshot.changes.get(base), snapshot.changes.get(code))
                change_text = "—" if change is None else f"{change:+.2f}%"
                tags = () if change is None else (("up",) if change >= 0 else ("down",))
                name = localized_asset_name(code, fiat_display_name(code, snapshot.names.get(code, code)))
                rows.append(((code, name, self._format(converted), change_text, tr(fiat_region(code)), "", ""), tags))
        else:
            codes = [code for code in snapshot.rates if snapshot.kinds.get(code) == "crypto"]
            for code in codes:
                converted = self.service.convert(amount, base, code)
                change = snapshot.changes.get(code)
                change_text = "—" if change is None else f"{change:+.2f}%"
                tags = () if change is None else (("up",) if change >= 0 else ("down",))
                rows.append(((code, localized_asset_name(code, snapshot.names.get(code, code)), self._format(converted), change_text, "", ""), tags))
        self.rate_var.set(f"参考表当前按 {self._format(amount)} {base} 换算为列表中的币种数量")
        self.table_default_rows = list(rows)
        self.table_rows = list(rows)
        self.sort_reverse.clear()
        self._set_table_heading_arrows()
        self._render_rows(animate)

    def _filtered_rows(self) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
        rows = list(self.table_rows)
        if self.favorites_only:
            rows = [row for row in rows if row[0][0] in self.favorite_codes]
        query = self.search_var.get().strip().lower()
        if "·" in query:
            query = query.split("·", 1)[0].replace("₿", "").strip()
        if not query:
            return rows
        return [row for row in rows if query in row[0][0].lower() or query in row[0][1].lower()]

    def _search_table_changed(self, _value: str) -> None:
        self.favorites_only = False
        self._set_action_headings()
        self._render_rows(False)

    def toggle_favorites_filter(self) -> None:
        self.favorites_only = not self.favorites_only
        if self.favorites_only:
            self.search_var.set("")
        self._set_action_headings()
        self._render_rows(False)

    def _set_action_headings(self) -> None:
        if not hasattr(self, "table"):
            return
        self.table.heading("favorite", text="收藏 ★" if self.favorites_only else "收藏")
        self.table.heading("pin", text="置顶")

    def _render_rows(self, animate: bool = False) -> None:
        if not hasattr(self, "table"):
            return
        self.render_generation += 1
        generation = self.render_generation
        if self.render_job is not None:
            try:
                self.after_cancel(self.render_job)
            except tk.TclError:
                pass
            self.render_job = None
        self._hide_action_buttons()
        self._set_action_headings()
        self.table.delete(*self.table.get_children())
        self.table_item_codes.clear()
        rows = self._filtered_rows()
        by_code = {row[0][0]: row for row in rows}
        pinned_rows = [by_code[code] for code in self.pinned_codes if code in by_code]
        display_rows = [(row, True) for row in pinned_rows] + [(row, False) for row in rows]
        if not animate:
            for (values, tags), pinned_copy in display_rows:
                item = self.table.insert("", tk.END, values=values, tags=tags + (("pinned_copy",) if pinned_copy else ()))
                self.table_item_codes[item] = values[0]
            return

        def add(index: int = 0) -> None:
            self.render_job = None
            if generation != self.render_generation or index >= len(display_rows):
                return
            next_index = min(index + 8, len(display_rows))
            for (values, tags), pinned_copy in display_rows[index:next_index]:
                item = self.table.insert("", tk.END, values=values, tags=tags + (("pinned_copy",) if pinned_copy else ()))
                self.table_item_codes[item] = values[0]
            if next_index < len(display_rows):
                self.render_job = self.after(12, lambda: add(next_index))

        add()

    def sort_table(self, column: str, numeric: bool) -> None:
        reverse = self.sort_reverse.get(column, False)
        self.sort_reverse[column] = not reverse
        for other in tuple(self.sort_reverse):
            if other != column:
                self.sort_reverse.pop(other, None)
        index = {"code": 0, "name": 1, "rate": 2, "change": 3, "region": 4}[column]

        def key(row: tuple[tuple[str, ...], tuple[str, ...]]):
            value = row[0][index]
            return value.casefold()

        if numeric:
            valued_rows: list[tuple[float, tuple[tuple[str, ...], tuple[str, ...]]]] = []
            missing_rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
            for row in self.table_rows:
                value = row[0][index]
                try:
                    number = float(value.replace(",", "").replace("%", "").replace("+", ""))
                    if not math.isfinite(number):
                        raise ValueError
                except ValueError:
                    missing_rows.append(row)
                else:
                    valued_rows.append((number, row))
            valued_rows.sort(key=lambda item: item[0], reverse=reverse)
            self.table_rows = [row for _number, row in valued_rows] + missing_rows
        else:
            self.table_rows.sort(key=key, reverse=reverse)
        self._set_table_heading_arrows(column, "↓" if reverse else "↑")
        self._render_rows(False)

    def reset_table_order(self) -> None:
        self.table_rows = list(self.table_default_rows)
        self.sort_reverse.clear()
        self.favorites_only = False
        self.search_var.set("")
        self._set_table_heading_arrows()
        self._set_action_headings()
        self._render_rows(False)

    def _set_table_heading_arrows(self, active: str = "", arrow: str = "↕") -> None:
        labels = {
            "code": "代码", "name": "名称", "rate": "换算数量",
            "region": "地区", "change": "24h",
        }
        available = ("code", "name", "rate", "change", "region") if self.mode == "fiat" else ("code", "name", "rate", "change")
        for column in available:
            label = labels[column]
            suffix = f" {arrow}" if column == active else " ↕"
            self.table.heading(column, text=label + suffix)

    def _table_yview(self, *args) -> None:
        self.table.yview(*args)
        border = getattr(self, "table_selection_border", None)
        if border is not None:
            border._schedule()
        self._queue_action_position()

    def _table_scrolled(self, scrollbar: ttk.Scrollbar, first: str, last: str) -> None:
        scrollbar.set(first, last)
        border = getattr(self, "table_selection_border", None)
        if border is not None:
            border._schedule()
        self._queue_action_position()

    def _queue_action_position(self, _event: tk.Event | None = None) -> None:
        if self.action_position_job is None:
            self.action_position_job = self.after_jobs.schedule(0, self._run_action_position, idle=True)

    def _run_action_position(self) -> None:
        self.action_position_job = None
        self._position_action_buttons()

    def _selected_code(self) -> str:
        selected = self.table.selection()
        return self.table_item_codes.get(selected[0], "") if selected else ""

    def _show_row_actions(self, _event=None) -> None:
        code = self._selected_code()
        self._set_action_headings()
        if not code:
            self._hide_action_buttons()
            return
        row_background = self._selected_row_background()
        self.favorite_button.configure(
            text="★" if code in self.favorite_codes else "☆", fg=COLORS["muted"],
            bg=row_background, activebackground=row_background,
        )
        self.pin_button.configure(
            text="⇩" if code in self.pinned_codes else "⇧", fg=COLORS["muted"],
            bg=row_background, activebackground=row_background,
        )
        self._position_action_buttons()
        self.action_fade_generation += 1
        generation = self.action_fade_generation
        for delay, color in ((0, COLORS["muted"]), (45, COLORS["text"]), (90, COLORS["accent"])):
            self.after_jobs.schedule(delay, lambda value=color, token=generation: self._fade_action_color(value, token))

    def _fade_action_color(self, color: str, generation: int) -> None:
        if generation == self.action_fade_generation and self._selected_code():
            self.favorite_button.configure(fg=color)
            self.pin_button.configure(fg=color)

    def _selected_row_background(self) -> str:
        selected = self.table.selection()
        if not selected:
            return COLORS["card"]
        tags = set(self.table.item(selected[0], "tags"))
        if "up" in tags:
            return COLORS["up_row"]
        if "down" in tags:
            return COLORS["down_row"]
        return COLORS["card"]

    def _position_action_buttons(self) -> None:
        selected = self.table.selection()
        if not selected:
            self._hide_action_buttons()
            return
        for column, button in (("favorite", self.favorite_button), ("pin", self.pin_button)):
            bounds = self.table.bbox(selected[0], column)
            if not bounds:
                button.place_forget()
                continue
            x, y, width, height = bounds
            button.place(x=x + 2, y=y + 2, width=max(18, width - 4), height=max(18, height - 4))
            button.lift()

    def _hide_action_buttons(self) -> None:
        if hasattr(self, "favorite_button"):
            self.favorite_button.place_forget()
            self.pin_button.place_forget()

    def _save_preferences(self) -> None:
        self.preference_callback(self.mode, sorted(self.favorite_codes), list(self.pinned_codes))
        self._save_page_state()

    def toggle_favorite(self) -> None:
        code = self._selected_code()
        if not code:
            return
        if code in self.favorite_codes:
            self.favorite_codes.remove(code)
        else:
            self.favorite_codes.add(code)
        self._save_preferences()
        if self.favorites_only:
            self._render_rows(False)
        else:
            self._show_row_actions()

    def toggle_pin(self) -> None:
        code = self._selected_code()
        if not code:
            return
        if code in self.pinned_codes:
            self.pinned_codes.remove(code)
        else:
            self.pinned_codes.append(code)
        self._save_preferences()
        self._render_rows(False)
        for item, item_code in self.table_item_codes.items():
            if item_code == code:
                self.table.selection_set(item)
                self.table.focus(item)
                break
        self._show_row_actions()

    def pick_table_item(self, _event=None) -> None:
        selected = self.table.selection()
        if not selected:
            return
        code = self.table_item_codes.get(selected[0], str(self.table.item(selected[0], "values")[0]))
        if code in self.code_to_display:
            target_side = {"a": "b", "b": "c", "c": "a"}.get(self.active_side, "b")
            self._clear_pending_restored_code(target_side)
            target_combo = {"a": self.combo_a, "b": self.combo_b, "c": self.combo_c}[target_side]
            target_combo.set(self.code_to_display[code])
            self._crypto_payment_context_changed()
            self.convert_from(self.active_side)

    def _destroy_jobs(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self.refreshing = False
        self.render_generation += 1
        self.action_fade_generation += 1
        self.visible = False
        self._advance_conversion_generation()
        self.after_jobs.cancel_all()
        self.conversion_bridge.close()
        self.payment_bridge.close()
        for attr in ("spinner_job", "render_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)


class PriceChart(tk.Canvas):
    def __init__(self, master: tk.Misc, timestamp_formatter: Callable[[int], str] | None = None) -> None:
        super().__init__(master, bg=COLORS["card"], bd=0, highlightthickness=0, cursor="crosshair")
        self.points: list[tuple[int, float]] = []
        self.currency = "CNY"
        self.line_color = COLORS["accent"]
        self.timestamp_formatter = timestamp_formatter or self._format_utc_timestamp
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self.show_crosshair)
        self.bind("<Leave>", lambda _e: self.redraw())

    def set_data(self, points: list[tuple[int, float]], currency: str) -> None:
        self.points = points
        self.currency = currency
        if len(points) >= 2:
            self.line_color = COLORS["up"] if points[-1][1] >= points[0][1] else COLORS["down"]
        self.redraw()

    @staticmethod
    def number(value: float) -> str:
        if abs(value) >= 1000:
            return f"{value:,.2f}"
        if abs(value) >= 1:
            return f"{value:.4f}".rstrip("0").rstrip(".")
        return f"{value:.8f}".rstrip("0").rstrip(".")

    @staticmethod
    def _format_utc_timestamp(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp / 1000, ZoneInfo("UTC")).strftime("%m-%d %H:%M")

    def _layout(self) -> tuple[float, float, float, float, float, float]:
        width, height = max(self.winfo_width(), 400), max(self.winfo_height(), 240)
        left, top, right, bottom = 18.0, 20.0, width - 92.0, height - 34.0
        values = [value for _, value in self.points] or [0, 1]
        low, high = min(values), max(values)
        padding = (high - low) * 0.08 or max(abs(high) * 0.02, 1)
        return left, top, right, bottom, low - padding, high + padding

    def _xy(self, index: int, value: float) -> tuple[float, float]:
        left, top, right, bottom, low, high = self._layout()
        x = left + (right - left) * index / max(len(self.points) - 1, 1)
        y = bottom - (value - low) / max(high - low, 1e-12) * (bottom - top)
        return x, y

    def redraw(self) -> None:
        self.delete("all")
        if len(self.points) < 2:
            self.create_text(self.winfo_width() / 2, self.winfo_height() / 2, text="选择币种后加载趋势行情", fill=COLORS["muted"], font=(FONT, 11))
            return
        left, top, right, bottom, low, high = self._layout()
        for index in range(5):
            y = top + (bottom - top) * index / 4
            value = high - (high - low) * index / 4
            self.create_line(left, y, right, y, fill=COLORS["grid"], width=1)
            self.create_text(right + 8, y, text=self.number(value), fill=COLORS["muted"], font=("Segoe UI", 8), anchor="w")
        step = max(1, len(self.points) // 500)
        sampled_indexes = list(range(0, len(self.points), step))
        if sampled_indexes[-1] != len(self.points) - 1:
            sampled_indexes.append(len(self.points) - 1)
        coords: list[float] = []
        for original_index in sampled_indexes:
            _timestamp, value = self.points[original_index]
            x, y = self._xy(original_index, value)
            coords.extend((x, y))
        polygon = [left, bottom] + coords + [right, bottom]
        fill = COLORS["up_fill"] if self.line_color == COLORS["up"] else COLORS["down_fill"]
        self.create_polygon(polygon, fill=fill, outline="")
        self.create_line(*coords, fill=self.line_color, width=3, smooth=True)
        for position in (0, len(self.points) // 2, len(self.points) - 1):
            x, _ = self._xy(position, self.points[position][1])
            label = self.timestamp_formatter(self.points[position][0])
            self.create_text(x, bottom + 18, text=label, fill=COLORS["muted"], font=("Segoe UI", 8), anchor="w" if position == 0 else "e" if position == len(self.points) - 1 else "center")

    def show_crosshair(self, event: tk.Event) -> None:
        if len(self.points) < 2:
            return
        left, top, right, bottom, _low, _high = self._layout()
        if not (left <= event.x <= right and top <= event.y <= bottom):
            return
        index = round((event.x - left) / max(right - left, 1) * (len(self.points) - 1))
        index = max(0, min(index, len(self.points) - 1))
        timestamp, value = self.points[index]
        x, y = self._xy(index, value)
        self.redraw()
        self.create_line(x, top, x, bottom, fill="#777777", dash=(3, 4), tags="crosshair")
        self.create_line(left, y, right, y, fill="#777777", dash=(3, 4), tags="crosshair")
        text = f"{self.timestamp_formatter(timestamp)}   {self.number(value)} {self.currency}"
        box_x = min(max(x, left + 120), right - 120)
        self.create_rectangle(box_x - 115, top + 6, box_x + 115, top + 35, fill=COLORS["tooltip"], outline=COLORS["line"], tags="crosshair")
        self.create_text(box_x, top + 20, text=text, fill=COLORS["text"], font=("Segoe UI", 9), tags="crosshair")


class MarketPage(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        service: RateService,
        refresh_callback: Callable[[], None],
        timestamp_formatter: Callable[[str], str],
        mode: str = "crypto",
        chart_timestamp_formatter: Callable[[int], str] | None = None,
        page_state: Mapping[str, object] | None = None,
        state_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        super().__init__(master, bg=COLORS["bg"])
        self.service = service
        self.refresh_callback = refresh_callback
        self.timestamp_formatter = timestamp_formatter
        self.chart_timestamp_formatter = chart_timestamp_formatter
        self.mode = mode
        restored = dict(page_state or {})
        default_code = "USD" if mode == "fiat" else "BTC"
        restored_code = str(restored.get("selected_code", default_code)).strip().upper()
        self.restored_code = restored_code if 1 <= len(restored_code) <= 16 else default_code
        self.current_code = self.restored_code
        restored_days = restored.get("days", 7)
        self.current_days = restored_days if isinstance(restored_days, int) and restored_days in {1, 7, 30, 90} else 7
        restored_quote = str(restored.get("quote", "CNY")).strip().upper()
        self.restored_quote = restored_quote if 1 <= len(restored_quote) <= 16 else "CNY"
        self.state_callback = state_callback or (lambda _state: None)
        self.visible = False
        self.fiat_var = tk.StringVar()
        restored_reference = str(restored.get("reference_amount", "1"))[:80]
        self.reference_amount_var = tk.StringVar(value=restored_reference)
        try:
            self.reference_amount_value = evaluate_basic_amount(restored_reference)
        except CalculationError:
            self.reference_amount_value = 1.0
            self.reference_amount_var.set("1")
        self.status_var = DisplayStringVar(value=(
            "全球货币周期趋势与多币种计价将在这里同步呈现。" if mode == "fiat" else
            "虚拟币批量行情、周期趋势与多币种计价将在这里同步呈现。"
        ))
        self.refresh_stamp_var = DisplayStringVar(value="最新刷新：等待联网")
        self.market_search_var = tk.StringVar(value=str(restored.get("search", ""))[:128])
        self.price_var = tk.StringVar(value="—")
        self.change_var = tk.StringVar(value="—")
        self.range_var = DisplayStringVar(value="最高 —   最低 —")
        self.loading = False
        self.fiat_display_to_code: dict[str, str] = {}
        self.fiat_values: list[str] = []
        self.watch_rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.watch_default_rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.watch_render_generation = 0
        self.watch_render_job: str | None = None
        self.watch_sort_reverse: dict[str, bool] = {}
        self.day_buttons: dict[int, tk.Button] = {}
        self.chart_generation = 0
        self.chart_load_job: str | None = None
        self.after_jobs = TkAfterJobs(self)
        self.chart_results = TkResultBridge(self, self._finish_chart)
        self._build()
        self.bind("<Destroy>", self._destroy_jobs, add="+")

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = tk.Frame(self, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 13))
        header.grid_columnconfigure(0, weight=1)
        title = "货币行情趋势" if self.mode == "fiat" else "虚拟币行情趋势"
        tk.Label(header, text=title, bg=COLORS["bg"], fg=COLORS["text"], font=(FONT, 23, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(header, textvariable=self.status_var, bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, 9)).grid(row=1, column=0, sticky="w", pady=(4, 0))
        refresh_text = "↻  刷新全部汇率" if self.mode == "fiat" else "↻  刷新全部行情"
        self.refresh_button = AppButton(header, refresh_text, self.refresh_callback, "accent", 10)
        self.refresh_button.grid(row=0, column=1, rowspan=2, padx=(10, 10), ipadx=10, ipady=8)
        tk.Label(
            header, textvariable=self.refresh_stamp_var, bg=COLORS["accent_dark"], fg=COLORS["accent"],
            font=(FONT, 9, "bold"), padx=13, pady=9,
        ).grid(row=0, column=2, rowspan=2)

        body = tk.Frame(self, bg=COLORS["bg"])
        self.market_body = body
        self.market_compact: bool | None = None
        body.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 25))
        body.bind("<Configure>", self._market_body_resized, add="+")
        watch_width = 365 if self.mode == "fiat" else 420
        self.watch_width = watch_width
        body.grid_columnconfigure(0, weight=0, minsize=watch_width)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        watch = tk.Frame(body, bg=COLORS["card"], width=watch_width)
        self.watch_panel = watch
        watch.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        watch.grid_propagate(False)
        watch.grid_columnconfigure(0, weight=1)
        watch.grid_rowconfigure(2, weight=1)
        watch_header = tk.Frame(watch, bg=COLORS["card"])
        watch_header.grid(row=0, column=0, sticky="ew", padx=13, pady=(15, 3))
        watch_header.grid_columnconfigure(0, weight=1)
        list_title = "货币市场" if self.mode == "fiat" else "虚拟币市场"
        tk.Label(watch_header, text=list_title, bg=COLORS["card"], fg=COLORS["text"], font=(FONT, 14, "bold")).grid(row=0, column=0, sticky="w")
        AppButton(watch_header, "默认顺序", self.reset_watch_order, "soft_accent", 8).grid(row=0, column=1, padx=(5, 0), ipady=4)
        self.market_search_selector = SearchSelect(
            watch_header, self.market_search_var, input_callback=self._market_search_changed,
            allow_free_text=True, width=16, font_size=9, max_rows=7,
        )
        self.market_search_selector.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(7, 2))
        tk.Label(watch, text="输入代码或名称搜索 · 双击查看走势", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8, "bold")).grid(row=1, column=0, sticky="w", padx=17, pady=(0, 5))
        watch_columns = ("code", "name", "price", "change")
        watch_table_frame = tk.Frame(
            watch, bg=COLORS["card"], bd=0, highlightthickness=1,
            highlightbackground=COLORS["accent"], highlightcolor=COLORS["accent"],
        )
        watch_table_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        watch_table_frame.grid_columnconfigure(0, weight=1)
        watch_table_frame.grid_rowconfigure(0, weight=1)
        self.watch_table_frame = watch_table_frame
        self.watch_table = ttk.Treeview(
            watch_table_frame, columns=watch_columns, show="headings", selectmode="browse",
            style="Market.Treeview",
        )
        self.watch_table.heading("code", text="币种 ↕", command=lambda: self.sort_watch("code", False))
        self.watch_table.heading("name", text="名称 ↕", command=lambda: self.sort_watch("name", False))
        self.watch_table.column("name", width=112 if self.mode == "fiat" else 150, anchor="w")
        self.watch_table.heading("price", text=("汇率 ↕" if self.mode == "fiat" else "价格 ↕"), command=lambda: self.sort_watch("price", True))
        self.watch_table.heading("change", text="24h ↕", command=lambda: self.sort_watch("change", True))
        self.watch_table.column("code", width=56 if self.mode == "fiat" else 72, anchor="w")
        self.watch_table.column("price", width=88 if self.mode == "fiat" else 86, anchor="e")
        self.watch_table.column("change", width=58, anchor="e")
        self.watch_table.grid(row=0, column=0, sticky="nsew")
        self.watch_scrollbar = ttk.Scrollbar(
            watch_table_frame, orient="vertical", command=self._watch_yview,
        )
        self.watch_scrollbar.grid(row=0, column=1, sticky="ns")
        self.watch_table.configure(yscrollcommand=self._watch_scrolled)
        self.watch_table.tag_configure("up", background=COLORS["up_row"], foreground=COLORS["text"])
        self.watch_table.tag_configure("down", background=COLORS["down_row"], foreground=COLORS["text"])
        self.watch_table.tag_configure("flat", background=COLORS["card_alt"], foreground=COLORS["text"])
        self.watch_selection_border = TreeSelectionBorder(self.watch_table)
        self.watch_table.bind("<Double-Button-1>", self.select_coin)

        chart_card = tk.Frame(body, bg=COLORS["card"])
        self.chart_card = chart_card
        chart_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        chart_card.grid_columnconfigure(0, weight=1)
        chart_card.grid_rowconfigure(2, weight=1)
        tools = tk.Frame(chart_card, bg=COLORS["card"])
        tools.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 8))
        tools.grid_columnconfigure(4, weight=1)
        tk.Label(tools, text="计价货币", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9)).grid(row=0, column=0, padx=(0, 8))
        self.fiat_combo = SearchSelect(
            tools, self.fiat_var, command=self.change_quote,
            width=19, font_size=10, max_rows=7,
        )
        self.fiat_combo.grid(row=0, column=1, sticky="ew")
        tk.Label(tools, text="参考数额", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9)).grid(row=0, column=2, padx=(14, 7))
        self.reference_amount_entry = tk.Entry(
            tools, textvariable=self.reference_amount_var, width=12,
            bg=COLORS["card_alt"], fg=COLORS["text"], insertbackground=COLORS["text"],
            selectbackground=COLORS["selection"], selectforeground=COLORS["selection_text"],
            font=("Segoe UI", 10, "bold"), bd=0, highlightthickness=1,
            highlightbackground=COLORS["accent"], highlightcolor=COLORS["accent"],
            justify="right",
            validate="key", validatecommand=(self.register(DualConverterPage._validate_reference_amount), "%P"),
        )
        self.reference_amount_entry.grid(row=0, column=3, ipady=6)
        self.reference_amount_entry.bind("<KeyPress>", self._reference_amount_keypress)
        self.reference_amount_entry.bind("<Return>", self._commit_reference_amount)
        self.reference_amount_entry.bind("<KP_Enter>", self._commit_reference_amount)
        self.reference_amount_entry.bind("<FocusOut>", self._commit_reference_amount)
        day_frame = tk.Frame(tools, bg=COLORS["card"])
        day_frame.grid(row=0, column=5, sticky="e")
        for days, label in [(1, "1日"), (7, "7日"), (30, "30日"), (90, "90日")]:
            button = AppButton(day_frame, label, lambda value=days: self.change_days(value), "ghost", 9)
            button.pack(side="left", padx=2, ipadx=5, ipady=5)
            self.day_buttons[days] = button

        summary = tk.Frame(chart_card, bg=COLORS["card"])
        summary.grid(row=1, column=0, sticky="ew", padx=22, pady=(2, 2))
        summary.grid_columnconfigure(2, weight=1)
        default_label = "USD / 美元" if self.mode == "fiat" else "BTC / 比特币"
        self.coin_label = tk.Label(summary, text=default_label, bg=COLORS["card"], fg=COLORS["text"], font=(FONT, 13, "bold"))
        self.coin_label.grid(row=0, column=0, sticky="w")
        tk.Label(summary, textvariable=self.price_var, bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 24, "bold")).grid(row=1, column=0, sticky="w")
        self.change_label = tk.Label(summary, textvariable=self.change_var, bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 11, "bold"))
        self.change_label.grid(row=1, column=1, sticky="w", padx=12)
        tk.Label(summary, textvariable=self.range_var, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9)).grid(row=0, column=3, sticky="e")

        self.chart = PriceChart(chart_card, self.chart_timestamp_formatter)
        self.chart.grid(row=2, column=0, sticky="nsew", padx=16, pady=(5, 16))
        self.raw_points: list[tuple[int, float]] = []
        self.raw_points_code = ""
        self.raw_points_days = 0
        self.raw_points_quote = "CNY"
        self._highlight_days()

    def _market_body_resized(self, event: tk.Event) -> None:
        compact = int(event.width) < 860
        if compact == self.market_compact:
            return
        self.market_compact = compact
        if compact:
            self.market_body.grid_columnconfigure(0, weight=1, minsize=0)
            self.market_body.grid_columnconfigure(1, weight=0, minsize=0)
            self.market_body.grid_rowconfigure(0, weight=0, minsize=225)
            self.market_body.grid_rowconfigure(1, weight=1, minsize=250)
            self.watch_panel.configure(width=1, height=225)
            self.watch_panel.grid_configure(row=0, column=0, columnspan=2, padx=0, pady=(0, 7))
            self.chart_card.grid_configure(row=1, column=0, columnspan=2, padx=0, pady=(7, 0))
        else:
            self.market_body.grid_columnconfigure(0, weight=0, minsize=self.watch_width)
            self.market_body.grid_columnconfigure(1, weight=1, minsize=0)
            self.market_body.grid_rowconfigure(0, weight=1, minsize=0)
            self.market_body.grid_rowconfigure(1, weight=0, minsize=0)
            self.watch_panel.configure(width=self.watch_width, height=1)
            self.watch_panel.grid_configure(row=0, column=0, columnspan=1, padx=(0, 8), pady=0)
            self.chart_card.grid_configure(row=0, column=1, columnspan=1, padx=(8, 0), pady=0)

    @staticmethod
    def compact(value: float) -> str:
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if abs(value) >= 1000:
            return f"{value / 1000:.2f}K"
        if abs(value) >= 1:
            return f"{value:.2f}"
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _fiat_code(self) -> str:
        return self.fiat_display_to_code.get(self.fiat_var.get(), self.restored_quote or "CNY")

    def _page_state(self) -> dict[str, object]:
        return {
            "selected_code": self.restored_code or self.current_code,
            "days": self.current_days,
            "quote": self.restored_quote or self._fiat_code(),
            "reference_amount": self.reference_amount_var.get()[:80],
            "search": self.market_search_var.get()[:128],
        }

    def _save_page_state(self) -> None:
        callback = getattr(self, "state_callback", None)
        if callable(callback):
            callback(self._page_state())

    def flush_state(self) -> None:
        self._save_page_state()

    def on_show(self) -> None:
        self.visible = True
        if self.service.snapshot.rates:
            raw_matches = (
                bool(self.raw_points)
                and self.raw_points_code == self.current_code
                and self.raw_points_days == self.current_days
            )
            if raw_matches:
                self.rerender_currency()
            elif not self.loading:
                self.load_chart()

    def on_hide(self) -> None:
        self.visible = False
        self._save_page_state()

    def apply_language(self) -> None:
        snapshot = self.service.snapshot
        if snapshot.rates:
            self.apply_snapshot(snapshot, False, animated=False, reload_chart=False)

    def _market_search_changed(self, _value: str) -> None:
        self._render_watch(False)
        self._save_page_state()

    @staticmethod
    def _fiat_watch_change(snapshot: RateSnapshot, code: str, quote: str) -> float | None:
        if code == quote:
            return 0.0
        try:
            change = relative_rate_change(snapshot.changes.get(code), snapshot.changes.get(quote))
        except (TypeError, ValueError, OverflowError):
            return None
        return change if change is not None and math.isfinite(change) else None

    def _reference_amount_keypress(self, event: tk.Event) -> str | None:
        if normalize_amount_input(event.char) == "=":
            self.after_jobs.schedule(0, self._commit_reference_amount, idle=True)
            return "break"
        return None

    def _commit_reference_amount(self, _event: tk.Event | None = None) -> str:
        try:
            value = evaluate_basic_amount(normalize_amount_input(self.reference_amount_var.get()))
            self.reference_amount_value = value
            self.reference_amount_var.set(DualConverterPage._format(value))
            self.rerender_currency()
            self._save_page_state()
        except CalculationError as exc:
            self.reference_amount_var.set(DualConverterPage._format(self.reference_amount_value))
            self.status_var.set(f"参考数额输入有误：{exc}")
        return "break"

    def apply_snapshot(
        self,
        snapshot: RateSnapshot,
        from_cache: bool = False,
        animated: bool = False,
        reload_chart: bool = True,
    ) -> None:
        old_fiat = self._fiat_code()
        fiats = [code for code in snapshot.rates if snapshot.kinds.get(code) == "fiat"]
        priority = ["CNY", "USD", "EUR", "JPY", "HKD", "GBP", "AUD", "CAD"]
        fiats.sort(key=lambda code: (priority.index(code) if code in priority else 999, code))
        self.fiat_display_to_code.clear()
        values = []
        for code in fiats:
            display = f"{code} · {localized_asset_name(code, fiat_display_name(code, snapshot.names.get(code, code)))}"
            values.append(display)
            self.fiat_display_to_code[display] = code
        self.fiat_values = values
        self.fiat_combo.set_values(values)
        asset_kind = "fiat" if self.mode == "fiat" else "crypto"
        asset_values = [
            f"{code} · {localized_asset_name(code, fiat_display_name(code, snapshot.names.get(code, code)) if self.mode == 'fiat' else crypto_display_name(code, snapshot.names.get(code, code)))}"
            for code in snapshot.rates if snapshot.kinds.get(code) == asset_kind
        ]
        self.market_search_selector.set_values(asset_values)
        asset_codes = [code for code in snapshot.rates if snapshot.kinds.get(code) == asset_kind]
        if self.restored_code in asset_codes:
            self.current_code = self.restored_code
            self.restored_code = ""
        if self.current_code not in asset_codes and asset_codes:
            self.current_code = asset_codes[0]
        if self.current_code in asset_codes:
            name = snapshot.names.get(self.current_code, self.current_code)
            if self.mode == "crypto":
                name = crypto_display_name(self.current_code, name)
            else:
                name = fiat_display_name(self.current_code, name)
            name = localized_asset_name(self.current_code, name)
            self.coin_label.configure(text=f"{self.current_code} / {name}")
        target_fiat = self.restored_quote if self.restored_quote in fiats else old_fiat if old_fiat in fiats else ("CNY" if "CNY" in fiats else fiats[0] if fiats else "")
        for display, code in self.fiat_display_to_code.items():
            if code == target_fiat:
                self.fiat_combo.set(display)
                if code == self.restored_quote:
                    self.restored_quote = ""
                break
        self._refresh_watchlist(snapshot, animated)
        self.refresh_stamp_var.set(f"最新刷新：{self.timestamp_formatter(snapshot.fetched_at)}")
        self.status_var.set("双击币种查看趋势；计价货币、时间周期和表格顺序均可自由切换。")
        self.refresh_button.configure(text=("↻  刷新全部汇率" if self.mode == "fiat" else "↻  刷新全部行情"), state="normal")
        if reload_chart and self.current_code in snapshot.rates and snapshot.kinds.get(self.current_code) == asset_kind:
            self._queue_chart_load(120)
        elif self.raw_points:
            self.status_var.set(f"{self.current_code} · {self.current_days} 日行情 · 鼠标移入图表可查看具体时点")
            self.rerender_currency(refresh_watchlist=False)

    def begin_refresh(self) -> None:
        self.watch_table.delete(*self.watch_table.get_children())
        self.price_var.set("正在刷新…")
        self.change_var.set("—")
        self.refresh_button.configure(text=("汇率批量刷新中…" if self.mode == "fiat" else "行情批量刷新中…"), state="disabled")

    def finish_refresh_failure(self) -> None:
        self.refresh_button.configure(text=("↻  刷新全部汇率" if self.mode == "fiat" else "↻  刷新全部行情"), state="normal")
        if not self.raw_points:
            self.price_var.set("暂无联网行情")

    def _refresh_watchlist(self, snapshot: RateSnapshot, animated: bool = False) -> None:
        fiat = self._fiat_code()
        rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        asset_kind = "fiat" if self.mode == "fiat" else "crypto"
        for code in [key for key in snapshot.rates if snapshot.kinds.get(key) == asset_kind]:
            price = self.service.convert(self.reference_amount_value, code, fiat)
            change = (
                self._fiat_watch_change(snapshot, code, fiat)
                if self.mode == "fiat" else snapshot.changes.get(code)
            )
            if change is None:
                change_text, tags = "—", ()
            elif self.mode == "fiat" and round(change, 1) == 0:
                change_text, tags = "0.0%", ("flat",)
            else:
                change_text = f"{change:+.1f}%"
                tags = ("up",) if change >= 0 else ("down",)
            if self.mode == "fiat":
                name = localized_asset_name(code, fiat_display_name(code, snapshot.names.get(code, code)))
                rows.append(((code, name, self.compact(price), change_text), tags))
            else:
                name = localized_asset_name(code, crypto_display_name(code, snapshot.names.get(code, code)))
                rows.append(((code, name, self.compact(price), change_text), tags))
        self.watch_default_rows = list(rows)
        self.watch_rows = list(rows)
        self.watch_sort_reverse.clear()
        self._set_watch_heading_arrows()
        self._render_watch(animated)

    def _render_watch(self, animated: bool = False) -> None:
        self.watch_render_generation += 1
        generation = self.watch_render_generation
        if self.watch_render_job is not None:
            try:
                self.after_cancel(self.watch_render_job)
            except tk.TclError:
                pass
            self.watch_render_job = None
        self.watch_table.delete(*self.watch_table.get_children())
        query = self.market_search_var.get().strip().lower()
        if "·" in query:
            query = query.split("·", 1)[0].strip()
        rows = [row for row in self.watch_rows if not query or any(query in str(value).lower() for value in row[0][:2]) or query in self.service.snapshot.names.get(row[0][0], "").lower()]
        if not animated:
            for values, tags in rows:
                self.watch_table.insert("", tk.END, values=values, tags=tags)
            return

        def add(index: int = 0) -> None:
            self.watch_render_job = None
            if generation != self.watch_render_generation or index >= len(rows):
                return
            next_index = min(index + 8, len(rows))
            for values, tags in rows[index:next_index]:
                self.watch_table.insert("", tk.END, values=values, tags=tags)
            if next_index < len(rows):
                self.watch_render_job = self.after(12, lambda: add(next_index))

        add()

    def _watch_yview(self, *args) -> None:
        self.watch_table.yview(*args)
        border = getattr(self, "watch_selection_border", None)
        if border is not None:
            border._schedule()

    def _watch_scrolled(self, first: str, last: str) -> None:
        self.watch_scrollbar.set(first, last)
        border = getattr(self, "watch_selection_border", None)
        if border is not None:
            border._schedule()

    def sort_watch(self, column: str, numeric: bool) -> None:
        reverse = self.watch_sort_reverse.get(column, False)
        self.watch_sort_reverse[column] = not reverse
        for other in tuple(self.watch_sort_reverse):
            if other != column:
                self.watch_sort_reverse.pop(other, None)
        index = {"code": 0, "name": 1, "price": 2, "change": 3}[column]

        def key(row: tuple[tuple[str, ...], tuple[str, ...]]):
            value = row[0][index]
            return value.casefold()

        def number(row: tuple[tuple[str, ...], tuple[str, ...]]) -> float | None:
            value = row[0][index]
            scale = 1.0
            if value.endswith("K"):
                scale, value = 1_000.0, value[:-1]
            elif value.endswith("M"):
                scale, value = 1_000_000.0, value[:-1]
            try:
                result = float(value.replace("%", "").replace("+", "")) * scale
            except ValueError:
                return None
            return result if math.isfinite(result) else None

        if numeric:
            valued_rows: list[tuple[float, tuple[tuple[str, ...], tuple[str, ...]]]] = []
            missing_rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
            for row in self.watch_rows:
                value = number(row)
                if value is None:
                    missing_rows.append(row)
                else:
                    valued_rows.append((value, row))
            valued_rows.sort(key=lambda item: item[0], reverse=reverse)
            self.watch_rows = [row for _number, row in valued_rows] + missing_rows
        else:
            self.watch_rows.sort(key=key, reverse=reverse)
        self._set_watch_heading_arrows(column, "↓" if reverse else "↑")
        self._render_watch(False)

    def reset_watch_order(self) -> None:
        self.watch_rows = list(self.watch_default_rows)
        self.watch_sort_reverse.clear()
        self._set_watch_heading_arrows()
        self._render_watch(False)

    def _set_watch_heading_arrows(self, active: str = "", arrow: str = "↕") -> None:
        labels = {
            "code": "币种",
            "name": "名称",
            "price": "汇率" if self.mode == "fiat" else "价格",
            "change": "24h",
        }
        columns = ("code", "name", "price", "change")
        for column in columns:
            label = labels[column]
            self.watch_table.heading(column, text=f"{label} {arrow if column == active else '↕'}")

    def select_coin(self, _event=None) -> None:
        selected = self.watch_table.selection()
        if not selected:
            return
        self.current_code = str(self.watch_table.item(selected[0], "values")[0])
        self.restored_code = ""
        name = self.service.snapshot.names.get(self.current_code, self.current_code)
        if self.mode == "fiat":
            name = fiat_display_name(self.current_code, name)
        else:
            name = crypto_display_name(self.current_code, name)
        name = localized_asset_name(self.current_code, name)
        self.coin_label.configure(text=f"{self.current_code} / {name}")
        self._save_page_state()
        self.load_chart()

    def change_days(self, days: int) -> None:
        self.current_days = days
        self._highlight_days()
        self._save_page_state()
        self.load_chart()

    def change_quote(self, _value: str | None = None) -> None:
        self.restored_quote = ""
        self._save_page_state()
        if self.mode != "fiat":
            self.rerender_currency()
            return
        if self.service.snapshot.rates:
            self._refresh_watchlist(self.service.snapshot)
        self.load_chart()

    def _highlight_days(self) -> None:
        for days, button in self.day_buttons.items():
            active = days == self.current_days
            button.configure(bg=COLORS["accent_dark"] if active else COLORS["card"], fg=COLORS["accent"] if active else COLORS["muted"])

    def load_chart(self) -> None:
        if not self.service.snapshot.rates:
            return
        if self.chart_load_job is not None:
            try:
                self.after_cancel(self.chart_load_job)
            except tk.TclError:
                pass
            self.chart_load_job = None
        self.chart_generation += 1
        generation = self.chart_generation
        code, days = self.current_code, self.current_days
        quote = self._fiat_code() if self.mode == "fiat" else "CNY"
        raw_matches_request = (
            bool(self.raw_points)
            and getattr(self, "raw_points_code", "") == code
            and getattr(self, "raw_points_days", 0) == days
            and (self.mode != "fiat" or self.raw_points_quote == quote)
        )
        if not raw_matches_request:
            self.raw_points = []
            self.raw_points_code = ""
            self.raw_points_days = 0
            self.chart.set_data([], quote)
            self.price_var.set("正在加载…")
            self.change_var.set("—")
            self.change_label.configure(fg=COLORS["muted"])
            self.range_var.set("最高 —   最低 —")
        self.status_var.set(f"正在加载 {code} 的 {days} 日趋势…")
        if self.loading:
            return
        self.loading = True
        self.chart_results.expect()

        def worker() -> None:
            try:
                points = (
                    self.service.fetch_fiat_chart(code, days, quote) if self.mode == "fiat" else
                    self.service.fetch_market_chart(code, days)
                )
                self.chart_results.deliver(generation, code, days, quote, points, None)
            except Exception as exc:
                self.chart_results.deliver(generation, code, days, quote, [], str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_chart(
        self,
        generation: int,
        code: str,
        days: int,
        quote: str,
        points: list[tuple[int, float]],
        error: str | None,
    ) -> None:
        self.loading = False
        if (
            generation != self.chart_generation
            or code != self.current_code
            or days != self.current_days
            or (self.mode == "fiat" and quote != self._fiat_code())
        ):
            self._queue_chart_load()
            return
        if error:
            self.status_var.set(error)
            return
        self.raw_points = points
        self.raw_points_code = code
        self.raw_points_days = days
        self.raw_points_quote = quote
        self.status_var.set(f"{code} · {days} 日行情 · 鼠标移入图表可查看具体时点")
        self.rerender_currency()

    def _queue_chart_load(self, delay: int = 0) -> None:
        if self.chart_load_job is not None:
            try:
                self.after_cancel(self.chart_load_job)
            except tk.TclError:
                pass
        self.chart_load_job = self.after(delay, self._run_queued_chart_load)

    def _run_queued_chart_load(self) -> None:
        self.chart_load_job = None
        self.load_chart()

    def _destroy_jobs(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self.chart_generation += 1
        self.watch_render_generation += 1
        self.after_jobs.cancel_all()
        self.chart_results.close()
        for attr in ("chart_load_job", "watch_render_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def rerender_currency(self, refresh_watchlist: bool = True) -> None:
        raw_matches_selection = (
            bool(self.raw_points)
            and getattr(self, "raw_points_code", "") == self.current_code
            and getattr(self, "raw_points_days", 0) == self.current_days
        )
        if not raw_matches_selection:
            if refresh_watchlist:
                self._refresh_watchlist(self.service.snapshot)
            return
        fiat = self._fiat_code()
        if self.mode == "fiat":
            if self.raw_points_quote != fiat:
                if refresh_watchlist:
                    self._refresh_watchlist(self.service.snapshot)
                if not self.loading:
                    self._queue_chart_load()
                return
            converted = [
                (stamp, value * self.reference_amount_value)
                for stamp, value in self.raw_points
            ]
        else:
            converted = [
                (stamp, self.service.convert(value * self.reference_amount_value, "CNY", fiat))
                for stamp, value in self.raw_points
            ]
        self.chart.set_data(converted, fiat)
        values = [value for _, value in converted]
        first, last = values[0], values[-1]
        change = (last / first - 1) * 100 if first else 0
        self.price_var.set(f"{DualConverterPage._format(self.reference_amount_value)} {self.current_code} = {PriceChart.number(last)} {fiat}")
        self.change_var.set(f"{change:+.2f}%")
        self.change_label.configure(fg=COLORS["up"] if change >= 0 else COLORS["down"])
        self.range_var.set(f"最高 {PriceChart.number(max(values))}   最低 {PriceChart.number(min(values))}")
        if refresh_watchlist:
            self._refresh_watchlist(self.service.snapshot)


class HistoryPanel(tk.Frame):
    def __init__(self, master: tk.Misc, use_callback: Callable[[str], None], clear_callback: Callable[[], None]) -> None:
        super().__init__(master, bg=COLORS["sidebar"], width=330)
        self.use_callback = use_callback
        self.clear_callback = clear_callback
        self.grid_propagate(False)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        tk.Label(self, text="历史记录", bg=COLORS["sidebar"], fg=COLORS["text"], font=(FONT, 16, "bold")).grid(row=0, column=0, sticky="w", padx=22, pady=(27, 3))
        tk.Label(self, text="双击结果可重新使用", bg=COLORS["sidebar"], fg=COLORS["muted"], font=(FONT, 9)).grid(row=1, column=0, sticky="w", padx=22, pady=(0, 14))
        self.listbox = tk.Listbox(
            self, bg=COLORS["card"], fg=COLORS["text"], selectbackground=COLORS["accent_dark"],
            selectforeground=COLORS["accent"], bd=0, highlightthickness=1, highlightbackground=COLORS["line"],
            activestyle="none", font=("Segoe UI", 10), exportselection=False,
        )
        self.listbox.grid(row=2, column=0, sticky="nsew", padx=18)
        self.listbox.bind("<Double-Button-1>", self._use)
        AppButton(self, "清空历史记录", self.clear_callback, "ghost", 10).grid(row=3, column=0, sticky="ew", padx=18, pady=18, ipady=7)
        self.results: list[str] = []

    def refresh(self, history: list[tuple[str, str]]) -> None:
        self.listbox.delete(0, tk.END)
        self.results = []
        for expression, result in history:
            self.listbox.insert(tk.END, f"{expression}   =   {result}")
            self.results.append(result)

    def _use(self, _event=None) -> None:
        selected = self.listbox.curselection()
        if selected:
            self.use_callback(self.results[selected[0]])


class SettingsPage(tk.Frame):
    PAGE_LABELS = {
        "calculator": "计算器", "exchange": "C2C 兑换", "market_exchange": "市场兑换", "fiat": "货币", "fiat_market": "货币行情趋势",
        "crypto": "虚拟币", "market": "虚拟币行情趋势", "settings": "设置",
    }
    CLOSE_LABELS = {
        "minimize": "点击 × 时最小化到任务栏",
        "exit": "点击 × 时彻底退出软件",
    }
    MODE_LABELS = {"standard": "标准模式", "professional": "专业模式"}
    COPY_LABELS = {"number": "纯数字", "grouped": "带千位分隔符", "formula": "完整算式与结果"}

    def __init__(
        self,
        master: tk.Misc,
        settings: AppSettings,
        theme_callback: Callable[[str], None],
        language_callback: Callable[[str], None],
        timezone_callback: Callable[[str], None],
        data_callback: Callable[[bool], None],
        setting_callback: Callable[[str, object], None],
        choose_data_callback: Callable[[], None],
        migrate_callback: Callable[[], None],
        cache_size_getter: Callable[[], int],
        clear_cache_callback: Callable[[], None],
        export_callback: Callable[[], None],
        import_callback: Callable[[], None],
        reset_callback: Callable[[], None],
        open_app_callback: Callable[[], None],
        open_data_callback: Callable[[], None],
        api_status_getter: Callable[[], str],
        api_enabled_callback: Callable[[bool], str],
        api_port_callback: Callable[[int], str],
        api_token_callback: Callable[[str], str],
        api_test_callback: Callable[[], str],
        update_check_callback: Callable[[], UpdateInfo],
        update_download_callback: Callable[[UpdateInfo], DownloadedUpdate],
        update_install_callback: Callable[[DownloadedUpdate], None],
        exit_callback: Callable[[], None],
    ) -> None:
        super().__init__(master, bg=COLORS["bg"])
        self.settings = settings
        self.theme_callback = theme_callback
        self.language_callback = language_callback
        self.timezone_callback = timezone_callback
        self.data_callback = data_callback
        self.setting_callback = setting_callback
        self.choose_data_callback = choose_data_callback
        self.migrate_callback = migrate_callback
        self.cache_size_getter = cache_size_getter
        self.clear_cache_callback = clear_cache_callback
        self.export_callback = export_callback
        self.import_callback = import_callback
        self.reset_callback = reset_callback
        self.open_app_callback = open_app_callback
        self.open_data_callback = open_data_callback
        self.api_status_getter = api_status_getter
        self.api_enabled_callback = api_enabled_callback
        self.api_port_callback = api_port_callback
        self.api_token_callback = api_token_callback
        self.api_test_callback = api_test_callback
        self.update_check_callback = update_check_callback
        self.update_download_callback = update_download_callback
        self.update_install_callback = update_install_callback
        self.exit_callback = exit_callback
        self.theme_name_to_display = {name: theme_label(name, settings.language) for name in THEMES}
        self.theme_display_to_name = {label: name for name, label in self.theme_name_to_display.items()}
        self.theme_var = tk.StringVar(
            value=self.theme_name_to_display.get(settings.theme, theme_label("dark", settings.language))
        )
        self.theme_cycle_var = DisplayStringVar(value=self._theme_cycle_text(settings.theme))
        self.language_display_to_code = {label: code for code, label in LANGUAGE_LABELS.items()}
        self.language_var = tk.StringVar(value=LANGUAGE_LABELS[normalize_language(settings.language)])
        self.timezone_var = tk.StringVar()
        self.timezone_clock_var = DisplayStringVar()
        self.keep_var = tk.BooleanVar(value=settings.keep_data_with_app)
        self.auto_refresh_var = tk.BooleanVar(value=settings.auto_refresh_enabled)
        self.fiat_minutes_var = tk.StringVar(value=str(settings.fiat_refresh_minutes))
        self.crypto_minutes_var = tk.StringVar(value=str(settings.crypto_refresh_minutes))
        self.refresh_minimized_var = tk.BooleanVar(value=settings.refresh_when_minimized)
        self.close_action_var = tk.StringVar(value=self.CLOSE_LABELS[settings.close_action])
        self.startup_page_var = tk.StringVar(value=self.PAGE_LABELS[settings.startup_page])
        self.remember_page_var = tk.BooleanVar(value=settings.remember_last_page)
        self.remember_geometry_var = tk.BooleanVar(value=settings.remember_window_geometry)
        self.default_mode_var = tk.StringVar(value=self.MODE_LABELS[settings.default_calculator_mode])
        self.remember_mode_var = tk.BooleanVar(value=settings.remember_calculator_mode)
        self.angle_mode_var = tk.StringVar(value=settings.calculator_angle_mode)
        self.history_limit_var = tk.StringVar(value=str(settings.history_limit))
        self.retain_history_var = tk.BooleanVar(value=settings.retain_history)
        self.copy_format_var = tk.StringVar(value=self.COPY_LABELS[settings.copy_result_format])
        self.cache_limit_var = tk.StringVar(value=str(settings.cache_limit_mb))
        self.cache_size_var = DisplayStringVar(value="正在统计…")
        self.app_path_var = tk.StringVar(value=str(portable_dir()))
        self.data_path_var = tk.StringVar(value=str(settings.resolved_data_dir()))
        self.api_enabled_var = tk.BooleanVar(value=bool(settings.local_api.get("enabled", False)))
        self.api_port_var = tk.StringVar(value=str(settings.local_api.get("port", 17890)))
        self.api_status_var = DisplayStringVar(value=api_status_getter())
        self.api_token_once_var = tk.StringVar(value="")
        self.api_results = TkResultBridge(self, self._finish_api_action)
        self.api_action_busy = False
        self.update_status_var = DisplayStringVar(value=f"当前版本 {APP_VERSION} · 尚未检查更新")
        self.update_results = TkResultBridge(self, self._finish_update_action)
        self.update_action_busy = False
        self.available_update: UpdateInfo | None = None
        self.zones = timezone_names()
        self.zone_infos = {zone: ZoneInfo(zone) for zone in self.zones}
        self.zone_display_to_name: dict[str, str] = {}
        self.zone_name_to_display: dict[str, str] = {}
        self.timezone_search_aliases: dict[str, str] = {}
        self.timezone_values = self._timezone_displays()
        self.timezone_options_hour = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%d%H")
        self.timezone_var.set(self.zone_name_to_display.get(settings.timezone, settings.timezone))
        self._build()
        self.api_status_var.trace_add("write", self._api_status_changed)
        self._apply_api_status_style()
        self.refresh_cache_size()
        self.timezone_job: str | None = None
        self._update_timezone_time()
        self.bind("<Destroy>", self._destroy_jobs, add="+")

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = tk.Frame(self, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=34, pady=(24, 12))
        tk.Label(header, text="设置", bg=COLORS["bg"], fg=COLORS["text"], font=(FONT, 24, "bold")).pack(anchor="w")
        tk.Label(header, text="刷新、启动、计算器与便携数据管理", bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, 10)).pack(anchor="w", pady=(4, 0))

        holder = tk.Frame(self, bg=COLORS["bg"])
        holder.grid(row=1, column=0, sticky="nsew", padx=(34, 22), pady=(0, 24))
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(holder, bg=COLORS["bg"], bd=0, highlightthickness=0)
        self.settings_scroll = ttk.Scrollbar(holder, orient="vertical", command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=self.settings_scroll.set)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        self.settings_scroll.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        body = tk.Frame(self.settings_canvas, bg=COLORS["bg"])
        self.settings_body = body
        self._settings_cards: list[tuple[tk.Frame, int, int, int]] = []
        self.settings_window = self.settings_canvas.create_window((0, 0), window=body, anchor="nw")
        body.grid_columnconfigure(0, weight=1, uniform="settings")
        body.grid_columnconfigure(1, weight=1, uniform="settings")
        body.bind("<Configure>", lambda _e: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")))
        self.settings_canvas.bind("<Configure>", self._settings_canvas_resized, add="+")

        appearance = self._card(
            body,
            "外观主题",
            f"{len(THEMES)} 套全新配色；每套独立指定白色或黑色文字",
            0,
            0,
            columnspan=2,
        )
        self.theme_picker = ThemePalettePicker(appearance, self.settings.theme, self._set_theme)
        self.theme_picker.pack(fill="x", padx=20, pady=(12, 8))
        tk.Label(
            appearance,
            text="配色预览依次显示背景、卡片、按钮、强调色与文字色。",
            bg=COLORS["card"],
            fg=COLORS["muted"],
            font=(FONT, 8),
        ).pack(anchor="w", padx=20, pady=(0, 18))

        timezone_card = self._card(body, "程序语言与刷新时区", "可使用中文城市、国家/地区或 IANA 名称搜索", 1, 0)
        tk.Label(timezone_card, text="程序默认语言", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8, "bold")).pack(anchor="w", padx=20, pady=(11, 4))
        self.language_combo = SearchSelect(
            timezone_card, self.language_var, values=list(LANGUAGE_LABELS.values()),
            command=self._set_language, width=34, font_size=10, max_rows=4,
        )
        self.language_combo.pack(fill="x", padx=20, pady=(0, 9))
        tk.Label(timezone_card, text="刷新显示时区", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8, "bold")).pack(anchor="w", padx=20, pady=(0, 4))
        self.timezone_combo = SearchSelect(
            timezone_card, self.timezone_var, values=self.timezone_values,
            command=self._set_timezone, width=34, font_size=10, max_rows=8,
            search_aliases=self.timezone_search_aliases,
        )
        self.timezone_combo.pack(fill="x", padx=20, pady=(0, 7))
        tk.Label(
            timezone_card, textvariable=self.timezone_clock_var, bg=COLORS["card_alt"], fg=COLORS["accent"],
            font=(FONT, 9, "bold"), padx=10, pady=6,
        ).pack(fill="x", padx=20, pady=(0, 18))

        refresh_card = self._card(body, "自动刷新", "货币与虚拟币使用独立分钟间隔", 1, 1)
        self._check(refresh_card, "启用自动刷新", self.auto_refresh_var, "auto_refresh_enabled")
        intervals = tk.Frame(refresh_card, bg=COLORS["card"])
        intervals.pack(fill="x", padx=20, pady=(5, 7))
        self._number_field(intervals, "货币", self.fiat_minutes_var, "fiat_refresh_minutes", 1, 1440, 60).pack(side="left", expand=True, fill="x", padx=(0, 5))
        self._number_field(intervals, "虚拟币", self.crypto_minutes_var, "crypto_refresh_minutes", 1, 1440, 10).pack(side="left", expand=True, fill="x", padx=(5, 0))
        self._check(refresh_card, "最小化后继续按设定时间刷新", self.refresh_minimized_var, "refresh_when_minimized", pady=(2, 16))

        startup = self._card(body, "启动与关闭", "曜衡只运行一个窗口；重复启动会唤醒现有窗口", 2, 0, columnspan=2)
        self.page_labels = {key: tr(value) for key, value in self.PAGE_LABELS.items()}
        self.close_labels = {key: tr(value) for key, value in self.CLOSE_LABELS.items()}
        self.mode_labels = {key: tr(value) for key, value in self.MODE_LABELS.items()}
        self.copy_labels = {key: tr(value) for key, value in self.COPY_LABELS.items()}
        self.startup_page_var.set(self.page_labels[self.settings.startup_page])
        self.close_action_var.set(self.close_labels[self.settings.close_action])
        self.default_mode_var.set(self.mode_labels[self.settings.default_calculator_mode])
        self.copy_format_var.set(self.copy_labels[self.settings.copy_result_format])
        self.startup_page_combo = self._select_row(startup, "默认启动页面", self.startup_page_var, list(self.page_labels.values()), lambda value: self._save("startup_page", self._reverse(self.page_labels, value)))
        self.close_action_combo = self._select_row(startup, "点击关闭按钮", self.close_action_var, list(self.close_labels.values()), lambda value: self._save("close_action", self._reverse(self.close_labels, value)))
        self._check(startup, "记住上次打开的页面", self.remember_page_var, "remember_last_page")
        self._check(startup, "记住窗口大小和位置", self.remember_geometry_var, "remember_window_geometry", pady=(2, 16))

        calculator = self._card(body, "计算器", "模式、角度、历史记录与复制格式", 3, 0, columnspan=2)
        calc_grid = tk.Frame(calculator, bg=COLORS["card"])
        calc_grid.pack(fill="x", padx=20, pady=(9, 4))
        for column in range(3):
            calc_grid.grid_columnconfigure(column, weight=1, uniform="calc_settings")
        self.default_mode_combo = self._select_cell(calc_grid, 0, "默认模式", self.default_mode_var, list(self.mode_labels.values()), lambda value: self._save("default_calculator_mode", self._reverse(self.mode_labels, value)))
        self.angle_mode_combo = self._select_cell(calc_grid, 1, "角度模式", self.angle_mode_var, ["DEG", "RAD"], lambda value: self._save("calculator_angle_mode", value))
        self.copy_format_combo = self._select_cell(calc_grid, 2, "复制结果格式", self.copy_format_var, list(self.copy_labels.values()), lambda value: self._save("copy_result_format", self._reverse(self.copy_labels, value)))
        calc_checks = tk.Frame(calculator, bg=COLORS["card"])
        calc_checks.pack(fill="x", padx=20, pady=(4, 14))
        self._check(calc_checks, "记住上次标准/专业模式", self.remember_mode_var, "remember_calculator_mode", pack_side="left")
        self._check(calc_checks, "退出后保留历史记录", self.retain_history_var, "retain_history", pack_side="left")
        history_field = self._number_field(calc_checks, "历史记录条数", self.history_limit_var, "history_limit", 1, 200, 30)
        history_field.pack(side="right", fill="x")

        storage = self._card(body, "应用与缓存位置", "绿色版可整体迁移，也可单独指定数据目录", 4, 0, columnspan=2)
        storage_content = tk.Frame(storage, bg=COLORS["card"])
        storage_content.pack(fill="x")
        storage_content.grid_columnconfigure(0, weight=1)
        self._path_row(storage_content, 0, "应用文件夹", self.app_path_var, [("打开文件夹", self.open_app_callback), ("迁移应用与数据…", self.migrate_callback)])
        self._path_row(storage_content, 1, "缓存与行情数据文件夹", self.data_path_var, [("打开文件夹", self.open_data_callback), ("更改缓存目录…", self.choose_data_callback)])
        keep = tk.Checkbutton(storage_content, text="应用与相关数据全部放在同一文件夹", variable=self.keep_var, command=lambda: self.data_callback(self.keep_var.get()), **self._check_style())
        keep.grid(row=6, column=0, columnspan=3, sticky="w", padx=20, pady=(2, 16))

        api = self._card(body, "API 接入", "供本机微信、QQ、Telegram 等机器人桥接；曜衡本身只监听回环地址", 5, 0, columnspan=2)
        api_row = tk.Frame(api, bg=COLORS["card"])
        api_row.pack(fill="x", padx=20, pady=(10, 6))
        tk.Checkbutton(
            api_row, text="启用本机 API", variable=self.api_enabled_var,
            command=self._toggle_api, **self._check_style(),
        ).pack(side="left", padx=(0, 12))
        tk.Label(api_row, text="固定地址  127.0.0.1", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9, "bold")).pack(side="left")
        port_box = tk.Frame(api_row, bg=COLORS["card"])
        port_box.pack(side="left", padx=12)
        tk.Label(port_box, text="端口", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8, "bold")).pack(anchor="w")
        self.api_port_entry = tk.Entry(
            port_box, textvariable=self.api_port_var, width=9, bg=COLORS["card_alt"], fg=COLORS["text"],
            insertbackground=COLORS["text"], selectbackground=COLORS["selection"],
            selectforeground=COLORS["selection_text"], bd=0, highlightthickness=1,
            highlightbackground=COLORS["line"], highlightcolor=COLORS["accent"], font=("Segoe UI", 10, "bold"),
        )
        self.api_port_entry.pack(ipady=5)
        self.api_port_entry.bind("<Return>", self._commit_api_port)
        self.api_port_entry.bind("<KP_Enter>", self._commit_api_port)
        self.api_port_entry.bind("<FocusOut>", self._commit_api_port)
        AppButton(api_row, "生成令牌", lambda: self._issue_api_token("generate"), "outline", 9).pack(side="left", padx=4, ipady=5)
        AppButton(api_row, "轮换令牌", lambda: self._issue_api_token("rotate"), "outline", 9).pack(side="left", padx=4, ipady=5)
        AppButton(api_row, "连接测试", self._test_api_connection, "ghost", 9).pack(side="left", padx=4, ipady=5)
        token_row = tk.Frame(api, bg=COLORS["card"])
        token_row.pack(fill="x", padx=20, pady=(2, 6))
        tk.Label(token_row, text="一次性令牌", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8, "bold")).pack(side="left")
        tk.Entry(
            token_row, textvariable=self.api_token_once_var, state="readonly", readonlybackground=COLORS["card_alt"],
            fg=COLORS["text"], relief="flat", bd=0, font=("Consolas", 9),
        ).pack(side="left", expand=True, fill="x", padx=10, ipady=6)
        AppButton(token_row, "复制", self._copy_api_token, "outline", 8).pack(side="left", ipady=4)
        self.api_status_label = tk.Label(
            api, textvariable=self.api_status_var, bg=COLORS["accent_dark"], fg=COLORS["accent"],
            font=(FONT, 9, "bold"), anchor="w", justify="left", padx=12, pady=8,
        )
        self.api_status_label.pack(fill="x", padx=20, pady=(0, 5))
        tk.Label(
            api,
            text="明文令牌只在生成或轮换当次显示，请立即保存。支持 /health、/v1/capabilities、/v1/calculate、/v1/convert、/v1/command；不包含下单接口。",
            bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8), wraplength=960, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 16))

        update_card = self._card(
            body,
            "软件更新",
            "从曜衡官方 GitHub Release 检查更新；安装包下载后必须通过 SHA-256 校验",
            6,
            0,
            columnspan=2,
        )
        update_row = tk.Frame(update_card, bg=COLORS["card"])
        update_row.pack(fill="x", padx=20, pady=(10, 6))
        tk.Label(
            update_row,
            textvariable=self.update_status_var,
            bg=COLORS["accent_dark"],
            fg=COLORS["accent"],
            font=(FONT, 9, "bold"),
            anchor="w",
            justify="left",
            padx=12,
            pady=8,
        ).pack(side="left", expand=True, fill="x", padx=(0, 10))
        self.update_check_button = AppButton(
            update_row, "检查更新", self._check_for_update, "outline", 9,
        )
        self.update_check_button.pack(side="left", padx=4, ipady=5)
        self.update_install_button = AppButton(
            update_row, "下载并升级", self._download_and_install_update, "accent", 9,
        )
        self.update_install_button.pack(side="left", padx=(4, 0), ipady=5)
        self.update_install_button.configure(state="disabled", cursor="arrow")
        tk.Label(
            update_card,
            text="升级时会打开覆盖安装程序并安全退出曜衡；原设置、收藏、历史和缓存默认保留。",
            bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8),
            wraplength=960, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 16))

        cache = self._card(body, "缓存与设置管理", "限制缓存占用、清理缓存以及导入导出设置", 7, 0, columnspan=2)
        tools = tk.Frame(cache, bg=COLORS["card"])
        tools.pack(fill="x", padx=20, pady=(10, 8))
        tk.Label(tools, textvariable=self.cache_size_var, bg=COLORS["card_alt"], fg=COLORS["accent"], font=(FONT, 9, "bold"), padx=12, pady=8).pack(side="left")
        self._number_field(tools, "缓存上限 MB（0 为不限制）", self.cache_limit_var, "cache_limit_mb", 0, 10240, 500).pack(side="left", padx=10)
        AppButton(tools, "重新统计", self.refresh_cache_size, "ghost", 9).pack(side="left", padx=4, ipady=5)
        AppButton(tools, "清理缓存", self.clear_cache_callback, "outline", 9).pack(side="left", padx=4, ipady=5)
        actions = tk.Frame(cache, bg=COLORS["card"])
        actions.pack(fill="x", padx=20, pady=(4, 18))
        for text, callback, kind in (
            ("导出设置…", self.export_callback, "outline"), ("导入设置…", self.import_callback, "outline"),
            ("恢复默认设置", self.reset_callback, "ghost"), ("立即退出曜衡", self.exit_callback, "ghost"),
        ):
            AppButton(actions, text, callback, kind, 9).pack(side="left", padx=(0, 8), ipadx=7, ipady=6)

        self._bind_mousewheel(body)

    def _card(self, parent: tk.Misc, title: str, subtitle: str, row: int, column: int, columnspan: int = 1) -> tk.Frame:
        card = tk.Frame(parent, bg=COLORS["card"])
        self._settings_cards.append((card, row, column, columnspan))
        card.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", padx=(0, 7) if column == 0 and columnspan == 1 else (7, 0) if column == 1 else 0, pady=(0, 10))
        tk.Label(card, text=title, bg=COLORS["card"], fg=COLORS["text"], font=(FONT, 14, "bold")).pack(anchor="w", padx=20, pady=(17, 4))
        tk.Label(card, text=subtitle, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9)).pack(anchor="w", padx=20)
        return card

    def _settings_canvas_resized(self, event: tk.Event) -> None:
        self.settings_canvas.itemconfigure(self.settings_window, width=event.width)
        compact = int(event.width) < 780
        self.settings_body.grid_columnconfigure(1, weight=0 if compact else 1)
        for index, (card, row, column, columnspan) in enumerate(self._settings_cards):
            if compact:
                card.grid_configure(row=index, column=0, columnspan=1, padx=0)
            else:
                card.grid_configure(
                    row=row, column=column, columnspan=columnspan,
                    padx=(0, 7) if column == 0 and columnspan == 1 else (7, 0) if column == 1 else 0,
                )

    @staticmethod
    def _reverse(mapping: dict[str, str], value: str) -> str:
        return next((key for key, label in mapping.items() if label == value), next(iter(mapping)))

    @staticmethod
    def _theme_cycle_text(theme: str) -> str:
        order = tuple(THEMES)
        current = theme if theme in THEMES else order[0]
        following = order[(order.index(current) + 1) % len(order)]
        language = get_language()
        return f"当前：{theme_label(current, language)}    下一套：{theme_label(following, language)}"

    @staticmethod
    def _check_style() -> dict[str, object]:
        return {
            "bg": COLORS["card"], "fg": COLORS["text"], "activebackground": COLORS["card"],
            "activeforeground": COLORS["text"], "selectcolor": COLORS["accent_dark"],
            "font": (FONT, 9, "bold"), "bd": 0, "highlightthickness": 0,
        }

    def _check(self, parent: tk.Misc, text: str, variable: tk.BooleanVar, key: str, pady=(7, 3), pack_side: str | None = None) -> tk.Checkbutton:
        button = tk.Checkbutton(parent, text=text, variable=variable, command=lambda: self._save(key, variable.get()), **self._check_style())
        if pack_side:
            button.pack(side=pack_side, padx=(0, 16), pady=pady)
        else:
            button.pack(anchor="w", padx=20, pady=pady)
        return button

    def _number_field(self, parent: tk.Misc, label: str, variable: tk.StringVar, key: str, minimum: int, maximum: int, default: int) -> tk.Frame:
        frame = tk.Frame(parent, bg=COLORS["card"])
        tk.Label(frame, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8, "bold")).pack(anchor="w")
        entry = tk.Entry(frame, textvariable=variable, bg=COLORS["card_alt"], fg=COLORS["text"], insertbackground=COLORS["text"], selectbackground=COLORS["selection"], selectforeground=COLORS["selection_text"], bd=0, highlightthickness=1, highlightbackground=COLORS["line"], highlightcolor=COLORS["accent"], font=("Segoe UI", 10, "bold"), width=12)
        entry.pack(fill="x", pady=(4, 0), ipady=6)
        commit = lambda _e=None: self._commit_int(variable, key, minimum, maximum, default)
        entry.bind("<Return>", commit)
        entry.bind("<KP_Enter>", commit)
        entry.bind("<FocusOut>", commit)
        return frame

    def _select_row(self, parent: tk.Misc, label: str, variable: tk.StringVar, values: list[str], callback: Callable[[str], None]) -> SearchSelect:
        row = tk.Frame(parent, bg=COLORS["card"])
        row.pack(fill="x", padx=20, pady=(8, 2))
        tk.Label(row, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9, "bold"), width=12, anchor="w").pack(side="left")
        combo = SearchSelect(row, variable, values=values, command=callback, width=20, font_size=9, max_rows=7)
        combo.pack(side="left", expand=True, fill="x")
        return combo

    def _select_cell(self, parent: tk.Misc, column: int, label: str, variable: tk.StringVar, values: list[str], callback: Callable[[str], None]) -> SearchSelect:
        cell = tk.Frame(parent, bg=COLORS["card"])
        cell.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column == 0 else (6, 6) if column == 1 else (6, 0))
        tk.Label(cell, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8, "bold")).pack(anchor="w")
        combo = SearchSelect(cell, variable, values=values, command=callback, width=18, font_size=9, max_rows=6)
        combo.pack(fill="x", pady=(4, 0))
        return combo

    def _path_row(self, parent: tk.Frame, row: int, label: str, variable: tk.StringVar, actions: list[tuple[str, Callable[[], None]]]) -> None:
        base_row = 2 + row * 2
        tk.Label(parent, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9, "bold")).grid(row=base_row, column=0, sticky="w", padx=20, pady=(10, 0))
        tk.Entry(parent, textvariable=variable, state="readonly", readonlybackground=COLORS["card_alt"], fg=COLORS["text"], relief="flat", bd=0, font=(FONT, 9)).grid(row=base_row + 1, column=0, sticky="ew", padx=(20, 10), pady=(5, 8), ipady=7)
        for index, (text, callback) in enumerate(actions, start=1):
            AppButton(parent, text, callback, "outline", 9).grid(row=base_row + 1, column=index, sticky="ew", padx=(0, 7 if index == 1 else 20), pady=(5, 8), ipadx=7, ipady=6)

    def _save(self, key: str, value: object) -> None:
        setattr(self.settings, key, value)
        self.setting_callback(key, value)

    def _commit_int(self, variable: tk.StringVar, key: str, minimum: int, maximum: int, default: int) -> str:
        try:
            value = int(variable.get().strip())
        except ValueError:
            value = default
        value = max(minimum, min(maximum, value))
        variable.set(str(value))
        self._save(key, value)
        return "break"

    def refresh_api_status(self) -> None:
        self.api_status_var.set(self.api_status_getter())

    def _api_status_changed(self, *_args: object) -> None:
        self._apply_api_status_style()

    def _apply_api_status_style(self) -> None:
        label = getattr(self, "api_status_label", None)
        if label is None:
            return
        state = connection_status_state(self.api_status_var.get())
        bg, fg = NETWORK_STATUS_PALETTES[state]
        try:
            label.configure(bg=bg, fg=fg)
        except tk.TclError:
            pass

    def _toggle_api(self) -> None:
        status = self.api_enabled_callback(self.api_enabled_var.get())
        self.api_enabled_var.set(bool(self.settings.local_api.get("enabled", False)))
        self.api_status_var.set(status)

    def _commit_api_port(self, _event: tk.Event | None = None) -> str:
        try:
            port = int(self.api_port_var.get().strip())
        except ValueError:
            port = int(self.settings.local_api.get("port", 17890))
            self.api_port_var.set(str(port))
            self.api_status_var.set("端口必须是 1–65535 的整数，已恢复原设置。")
            return "break"
        if not 1 <= port <= 65535:
            port = int(self.settings.local_api.get("port", 17890))
            self.api_port_var.set(str(port))
            self.api_status_var.set("端口必须在 1–65535 之间，已恢复原设置。")
            return "break"
        self.api_port_var.set(str(port))
        self.api_status_var.set(self.api_port_callback(port))
        return "break"

    def _issue_api_token(self, action: str) -> None:
        if self.api_action_busy:
            self.api_status_var.set("已有 API 操作正在进行，请稍候。")
            return
        self.api_action_busy = True
        self.api_token_once_var.set("")
        self.api_status_var.set("正在安全生成令牌…")
        self.api_results.expect()

        def worker() -> None:
            try:
                self.api_results.deliver("token", self.api_token_callback(action), None)
            except Exception as exc:
                self.api_results.deliver("token", None, exc)

        threading.Thread(target=worker, daemon=True, name="local-api-token").start()

    def _copy_api_token(self) -> None:
        token = self.api_token_once_var.get()
        if not token:
            self.api_status_var.set("没有可复制的一次性令牌，请先生成或轮换。")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(token)
            self.api_status_var.set("令牌已复制到剪贴板；请保存到可信密码管理器。")
        except tk.TclError:
            self.api_status_var.set("无法复制令牌，请手动选择保存。")

    def _test_api_connection(self) -> None:
        if self.api_action_busy:
            self.api_status_var.set("已有 API 操作正在进行，请稍候。")
            return
        self.api_action_busy = True
        self.api_status_var.set("正在测试本机 API 连接…")
        self.api_results.expect()

        def worker() -> None:
            try:
                self.api_results.deliver("test", self.api_test_callback(), None)
            except Exception as exc:
                self.api_results.deliver("test", None, exc)

        threading.Thread(target=worker, daemon=True, name="local-api-health-test").start()

    def _finish_api_action(self, action: object, value: object, error: object) -> None:
        self.api_action_busy = False
        if error is not None:
            self.api_token_once_var.set("")
            if action == "token" and isinstance(error, SecretAlreadyExistsError):
                self.api_status_var.set("已有本机 API 令牌；如需新令牌，请使用“轮换令牌”。")
            else:
                self.api_status_var.set(
                    "令牌操作失败；未显示或保存任何明文。"
                    if action == "token" else "连接测试失败；本机 API 未确认可用。"
                )
            return
        if action == "token":
            self.api_token_once_var.set(str(value or ""))
            self.api_status_var.set(self.api_status_getter() + "；新令牌仅本次显示，请立即保存。")
        else:
            self.api_status_var.set(str(value or "连接测试未返回状态。"))

    def _set_update_busy(self, busy: bool) -> None:
        self.update_action_busy = busy
        self.update_check_button.configure(
            state="disabled" if busy else "normal",
            cursor="arrow" if busy else "hand2",
        )
        can_install = not busy and self.available_update is not None and self.available_update.available
        self.update_install_button.configure(
            state="normal" if can_install else "disabled",
            cursor="hand2" if can_install else "arrow",
        )

    def _check_for_update(self) -> None:
        if self.update_action_busy:
            self.update_status_var.set("更新操作正在进行，请稍候。")
            return
        self._set_update_busy(True)
        self.update_status_var.set("正在从 GitHub 检查最新正式版本…")
        self.update_results.expect()

        def worker() -> None:
            try:
                self.update_results.deliver("check", self.update_check_callback(), None)
            except Exception as exc:
                self.update_results.deliver("check", None, exc)

        threading.Thread(target=worker, daemon=True, name="github-update-check").start()

    def _download_and_install_update(self) -> None:
        info = self.available_update
        if self.update_action_busy:
            self.update_status_var.set("更新操作正在进行，请稍候。")
            return
        if info is None or not info.available:
            self.update_status_var.set("请先检查更新。")
            return
        if not messagebox.askyesno(
            "升级曜衡",
            f"将下载并校验曜衡 {info.latest_version} 安装包。\n\n"
            "校验通过后会打开覆盖安装程序并退出当前曜衡；用户设置和缓存默认保留。\n\n"
            "是否继续？",
        ):
            return
        self._set_update_busy(True)
        self.update_status_var.set(f"正在下载曜衡 {info.latest_version} 并校验 SHA-256…")
        self.update_results.expect()

        def worker() -> None:
            try:
                self.update_results.deliver("download", self.update_download_callback(info), None)
            except Exception as exc:
                self.update_results.deliver("download", None, exc)

        threading.Thread(target=worker, daemon=True, name="github-update-download").start()

    def _finish_update_action(self, action: object, value: object, error: object) -> None:
        self._set_update_busy(False)
        if error is not None:
            detail = str(error) if isinstance(error, UpdateError) else "发生未预期错误"
            self.update_status_var.set(f"更新失败：{detail}")
            return
        if action == "check" and isinstance(value, UpdateInfo):
            if value.available:
                self.available_update = value
                first_note = next(
                    (line.lstrip("-# ").strip() for line in value.release_notes.splitlines() if line.strip()),
                    "可下载新的正式版本",
                )[:120]
                self.update_status_var.set(
                    f"发现曜衡 {value.latest_version} · {first_note}"
                )
            else:
                self.available_update = None
                self.update_status_var.set(
                    f"当前已是最新版本 {APP_VERSION}（GitHub 最新：{value.latest_version}）"
                )
            self._set_update_busy(False)
            return
        if action == "download" and isinstance(value, DownloadedUpdate):
            self.update_status_var.set(
                f"曜衡 {value.info.latest_version} 已下载并通过 SHA-256 校验，正在打开覆盖安装程序…"
            )
            try:
                self.update_idletasks()
                self.update_install_callback(value)
            except UpdateError as exc:
                self.update_status_var.set(f"无法开始升级：{exc}")
                self._set_update_busy(False)
            return
        self.update_status_var.set("更新操作没有返回有效结果。")

    def on_hide(self) -> None:
        # A plaintext token must not remain visible after leaving Settings.
        self.api_token_once_var.set("")

    def on_show(self) -> None:
        self.refresh_api_status()

    def flush_state(self) -> None:
        self._commit_api_port()

    def _set_theme(self, theme: str) -> None:
        selected = self.theme_display_to_name.get(str(theme), str(theme))
        if selected not in THEMES:
            return
        self.theme_var.set(self.theme_name_to_display[selected])
        self.theme_callback(selected)
        self.theme_cycle_var.set(self._theme_cycle_text(selected))

    def _set_language(self, display: str) -> None:
        language = self.language_display_to_code.get(str(display), normalize_language(display))
        self.language_var.set(LANGUAGE_LABELS[language])
        self.language_callback(language)

    def apply_language(self) -> None:
        language = normalize_language(self.settings.language)
        self.language_var.set(LANGUAGE_LABELS[language])
        self.theme_name_to_display = {name: theme_label(name, language) for name in THEMES}
        self.theme_display_to_name = {label: name for name, label in self.theme_name_to_display.items()}
        self.theme_var.set(self.theme_name_to_display[self.settings.theme])
        self.theme_cycle_var.set(self._theme_cycle_text(self.settings.theme))
        self.page_labels = {key: tr(value) for key, value in self.PAGE_LABELS.items()}
        self.close_labels = {key: tr(value) for key, value in self.CLOSE_LABELS.items()}
        self.mode_labels = {key: tr(value) for key, value in self.MODE_LABELS.items()}
        self.copy_labels = {key: tr(value) for key, value in self.COPY_LABELS.items()}
        self.startup_page_combo.set_values(list(self.page_labels.values()))
        self.startup_page_var.set(self.page_labels[self.settings.startup_page])
        self.close_action_combo.set_values(list(self.close_labels.values()))
        self.close_action_var.set(self.close_labels[self.settings.close_action])
        self.default_mode_combo.set_values(list(self.mode_labels.values()))
        self.default_mode_var.set(self.mode_labels[self.settings.default_calculator_mode])
        self.copy_format_combo.set_values(list(self.copy_labels.values()))
        self.copy_format_var.set(self.copy_labels[self.settings.copy_result_format])
        self.timezone_values = self._timezone_displays()
        self.timezone_combo.set_values(self.timezone_values, self.timezone_search_aliases)
        self.timezone_combo.set(self.zone_name_to_display.get(self.settings.timezone, self.settings.timezone))
        self.theme_picker.apply_language()
        if self.timezone_job is not None:
            try:
                self.after_cancel(self.timezone_job)
            except tk.TclError:
                pass
            self.timezone_job = None
        self._update_timezone_time()

    def _next_theme(self) -> None:
        order = tuple(THEMES)
        current = self.settings.theme if self.settings.theme in THEMES else order[0]
        self._set_theme(order[(order.index(current) + 1) % len(order)])

    def _timezone_displays(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(ZoneInfo("UTC"))
        display_to_name: dict[str, str] = {}
        name_to_display: dict[str, str] = {}
        search_aliases: dict[str, str] = {}
        values: list[str] = []
        for zone in self.zones:
            local = now.astimezone(self.zone_infos[zone])
            offset = local.strftime("%z") or "+0000"
            display = f"UTC{offset[:3]}:{offset[3:]}  ·  {timezone_display_name(zone)}  ·  {zone}"
            display_to_name[display] = zone
            name_to_display[zone] = display
            search_aliases[display] = timezone_search_text(zone)
            values.append(display)
        self.zone_display_to_name = display_to_name
        self.zone_name_to_display = name_to_display
        self.timezone_search_aliases = search_aliases
        return values

    def _update_timezone_time(self) -> None:
        self.timezone_job = None
        utc_now = datetime.now(ZoneInfo("UTC"))
        hour = utc_now.strftime("%Y%m%d%H")
        if hour != self.timezone_options_hour:
            focus = self.focus_get()
            popup_open = self.timezone_combo.popup is not None and self.timezone_combo.popup.winfo_viewable()
            if focus is not self.timezone_combo.entry and not popup_open:
                self.timezone_values = self._timezone_displays(utc_now)
                self.timezone_combo.set_values(self.timezone_values, self.timezone_search_aliases)
                self.timezone_combo.set(self.zone_name_to_display.get(self.settings.timezone, self.settings.timezone))
                self.timezone_options_hour = hour
        zone = self.settings.timezone if self.settings.timezone in self.zone_infos else "UTC"
        local = utc_now.astimezone(self.zone_infos.get(zone, ZoneInfo("UTC")))
        self.timezone_clock_var.set(f"所选时区当前时间：{format_chinese_datetime(local)}")
        self.timezone_job = self.after(1000, self._update_timezone_time)

    def _set_timezone(self, display: str) -> None:
        zone = self.zone_display_to_name.get(display, display)
        if zone in self.zones:
            self.timezone_callback(zone)
            local = datetime.now(self.zone_infos[zone])
            self.timezone_clock_var.set(f"所选时区当前时间：{format_chinese_datetime(local)}")

    def _destroy_jobs(self, event: tk.Event) -> None:
        if event.widget is self and self.timezone_job is not None:
            try:
                self.after_cancel(self.timezone_job)
            except tk.TclError:
                pass
            self.timezone_job = None
        if event.widget is self:
            self.api_token_once_var.set("")
            self.api_results.close()
            self.update_results.close()

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_mousewheel(child)

    def _on_mousewheel(self, event: tk.Event) -> str | None:
        active = getattr(self.winfo_toplevel(), "_active_search_select", None)
        if active is not None and active.popup is not None:
            return None
        self.settings_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def refresh_cache_size(self) -> None:
        size = self.cache_size_getter()
        self.cache_size_var.set(f"当前缓存：{size / 1024 / 1024:.2f} MB")

    def update_paths(self, settings: AppSettings) -> None:
        self.settings = settings
        self.keep_var.set(settings.keep_data_with_app)
        self.app_path_var.set(str(portable_dir()))
        self.data_path_var.set(str(settings.resolved_data_dir()))
        self.refresh_cache_size()


class YaohengApp:
    def __init__(self) -> None:
        self.settings_store = SettingsStore()
        self.settings = self.settings_store.load()
        set_ui_language(self.settings.language)
        COLORS.clear()
        COLORS.update(THEMES[self.settings.theme])
        self.root = tk.Tk()
        self.root.title(f"曜衡 {APP_VERSION}")
        geometry = self.settings.window_geometry if self.settings.remember_window_geometry else "1380x820"
        try:
            self.root.geometry(geometry)
        except tk.TclError:
            self.root.geometry("1380x820")
        self.root.minsize(900, 620)
        self.root.configure(bg=COLORS["bg"])
        self.root.update_idletasks()
        self._ensure_window_visible()
        self.app_icon_image: tk.PhotoImage | None = None
        icon_png = portable_dir() / "app.png"
        if icon_png.exists():
            try:
                self.app_icon_image = tk.PhotoImage(file=str(icon_png))
                self.root.iconphoto(True, self.app_icon_image)
            except tk.TclError:
                self.app_icon_image = None
        icon = portable_dir() / "app.ico"
        if icon.exists():
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        self.root.update_idletasks()
        self.window_icon_handles = set_windows_window_icon(self.root, icon)
        self.restart_registered = register_windows_restart()
        self.service = RateService(self.settings.resolved_data_dir())
        self.service.set_cache_limit(self.settings.cache_limit_mb)
        self.update_service = GitHubUpdateService()
        self.c2c_service = AppC2CService(self.service)
        self.exchange_coordinator = ExchangeCoordinator(self.service, self.c2c_service)
        self.local_api_command = CommandService(
            conversion_service=self.service,
            c2c_service=self.c2c_service,
        )
        self.api_security_warnings: list[str] = []
        self.local_api_last_error = ""
        self.secret_store = SecretStore(
            self.settings_store.path.parent / "private" / "local_api_token.json",
            warning_callback=self._record_api_security_warning,
        )
        self.local_api_server: LocalAPIServer | None = None
        self.pages: dict[str, tk.Frame] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.current_page = "calculator"
        self.loading_rates = False
        self.active_rate_section: str | None = None
        self.pending_rate_section: str | None = None
        self.history_open = False
        self.history_width = 330
        self.last_network_at = ""
        self.last_network_detail = ""
        self.network_state: bool | str | None = None
        self.auto_jobs: dict[str, str | None] = {"fiat": None, "crypto": None}
        self.startup_job: str | None = None
        self._page_open_refresh_enabled = False
        self.geometry_job: str | None = None
        self.exiting = False
        self.update_installing = False
        self.persistence_warning_shown = False
        self.sidebar_compact: bool | None = None
        self._theme_bindings: list[tuple[tk.Misc, tuple[tuple[str, str], ...], str | None]] = []
        self._themed_selects: list[tuple[SearchSelect, str | None]] = []
        self._themed_palette_pickers: list[ThemePalettePicker] = []
        self._themed_calculator_keys: list[tuple[CalculatorKey, str | None]] = []
        self._theme_idle_job: str | None = None
        self.rate_results = TkResultBridge(self.root, self._finish_rates)
        self._styles()
        self._shell()
        self._prepare_theme_bindings(dict(COLORS))
        start_page = self.settings.last_page if self.settings.remember_last_page else self.settings.startup_page
        self.show_page(start_page if start_page in self.pages else "calculator")
        self._page_open_refresh_enabled = True
        self.root.bind_all("<KeyPress>", self.on_key, add="+")
        self.root.bind("<Configure>", self._queue_geometry_save, add="+")
        self.root.bind("<Configure>", self._responsive_window_changed, add="+")
        self.root.bind_all("<Button-1>", self._dismiss_search_popup, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_request)
        if self.service.snapshot.rates:
            self.apply_snapshot(self.service.snapshot, True)
        self.startup_job = self.root.after(350, self._startup_rate_refresh)
        self.api_start_job = self.root.after(150, self._start_local_api_if_enabled)
        self.root.after(120, self._activate_calculator_if_current)
        self.root.after_idle(self._responsive_window_changed)

    def _styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=COLORS["card_alt"], background=COLORS["card_alt"], foreground=COLORS["text"], arrowcolor=COLORS["accent"], bordercolor=COLORS["line"], lightcolor=COLORS["line"], darkcolor=COLORS["line"], padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["card_alt"])], foreground=[("readonly", COLORS["text"])])
        self.root.option_add("*TCombobox*Listbox.background", COLORS["card_alt"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent_dark"])
        try:
            style.element_create("Treeitem.blackfocus", "from", "default", "focus")
        except tk.TclError:
            pass
        style.layout("Treeview.Item", [
            ("Treeitem.blackfocus", {"sticky": "nswe", "children": [
                ("Treeitem.padding", {"sticky": "nswe", "children": [
                    ("Treeitem.indicator", {"side": "left", "sticky": ""}),
                    ("Treeitem.image", {"side": "left", "sticky": ""}),
                    ("Treeitem.text", {"sticky": "nswe"}),
                ]}),
            ]}),
        ])
        style.configure(
            "Treeview", background=COLORS["card"], fieldbackground=COLORS["card"],
            foreground=COLORS["text"], rowheight=31, borderwidth=0,
            focuscolor=COLORS["focus"], focusthickness=2, font=(FONT, 9, "bold"),
        )
        style.map("Treeview", background=[], foreground=[])
        style.configure(
            "Market.Treeview", background=COLORS["card"], fieldbackground=COLORS["card"],
            foreground=COLORS["text"], rowheight=31, borderwidth=0, relief="flat",
            bordercolor=COLORS["accent"], lightcolor=COLORS["accent"], darkcolor=COLORS["accent"],
            focuscolor=COLORS["focus"], focusthickness=2, font=(FONT, 9, "bold"),
        )
        style.map("Market.Treeview", background=[], foreground=[])
        style.configure("Treeview.Heading", background=COLORS["card_alt"], foreground=COLORS["muted"], relief="flat", borderwidth=0, font=(FONT, 9, "bold"), padding=8)
        style.map("Treeview.Heading", background=[("active", COLORS["card_alt"])])
        style.configure("Vertical.TScrollbar", background=COLORS["key"], troughcolor=COLORS["card"], bordercolor=COLORS["card"], arrowcolor=COLORS["muted"])

    def _ensure_window_visible(self) -> None:
        try:
            if sys.platform == "win32":
                user32 = ctypes.windll.user32
                bounds = (
                    int(user32.GetSystemMetrics(76)),
                    int(user32.GetSystemMetrics(77)),
                    int(user32.GetSystemMetrics(78)),
                    int(user32.GetSystemMetrics(79)),
                )
                if bounds[2] <= 0 or bounds[3] <= 0:
                    bounds = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
            else:
                bounds = (
                    self.root.winfo_vrootx(), self.root.winfo_vrooty(),
                    self.root.winfo_vrootwidth(), self.root.winfo_vrootheight(),
                )
            width, height = self.root.winfo_width(), self.root.winfo_height()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            adjusted_x, adjusted_y = visible_window_position(x, y, width, height, bounds)
            if (adjusted_x, adjusted_y) != (x, y):
                self.root.geometry(f"{width}x{height}{adjusted_x:+d}{adjusted_y:+d}")
        except (AttributeError, OSError, tk.TclError):
            return

    def _shell(self) -> None:
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_columnconfigure(2, weight=0)
        self.root.grid_rowconfigure(0, weight=1)
        self.sidebar = tk.Frame(self.root, bg=COLORS["sidebar"], width=216)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_columnconfigure(1, weight=1)
        self.sidebar.grid_rowconfigure(10, weight=1)
        brand = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        self.sidebar_brand = brand
        brand.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(26, 32))
        self.logo_canvas = tk.Canvas(brand, width=42, height=42, bg=COLORS["sidebar"], bd=0, highlightthickness=0)
        self.logo_canvas.pack(side="left")
        self._draw_logo()
        self.brand_names = tk.Frame(brand, bg=COLORS["sidebar"])
        self.brand_names.pack(side="left", padx=10)
        tk.Label(self.brand_names, text="曜衡", bg=COLORS["sidebar"], fg=COLORS["sidebar_text"], font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(self.brand_names, text="精准计算 · 实时金融", bg=COLORS["sidebar"], fg=COLORS["accent"], font=(FONT, 7, "bold")).pack(anchor="w")

        items = [
            ("calculator", "▦   计算器", 11, 22),
            ("exchange", "⇄   C2C 兑换", 11, 22),
            ("market_exchange", "⇆   市场兑换", 11, 22),
            ("fiat", "¥   货币", 11, 22),
            ("fiat_market", "　　¥⌁  货币行情趋势", 9, 22),
            ("crypto", "₿   虚拟币", 11, 22),
            ("market", "　　₿⌁  虚拟币行情趋势", 9, 22),
            ("settings", "⚙   设置", 11, 22),
        ]
        self.nav_full_text = {key: text for key, text, _font_size, _left_pad in items}
        self.nav_compact_text = {
            "calculator": "▦", "exchange": "⇄", "market_exchange": "⇆", "fiat": "¥",
            "fiat_market": "¥⌁", "crypto": "₿", "market": "₿⌁", "settings": "⚙",
        }
        for row, (key, text, font_size, left_pad) in enumerate(items, start=1):
            button = tk.Button(
                self.sidebar, text=text, command=lambda page=key: self.show_page(page), anchor="w",
                bg=COLORS["sidebar"], fg=COLORS["sidebar_muted"], activebackground=COLORS["nav_hover"],
                activeforeground=COLORS["sidebar_text"], relief="flat", bd=0, highlightthickness=0,
                font=(FONT, font_size, "bold"), padx=left_pad, pady=(10 if font_size < 11 else 13), cursor="hand2",
            )
            button.grid(row=row, column=0, columnspan=2, sticky="ew", padx=10, pady=3)
            self.nav_buttons[key] = button
        self.sidebar_previous_theme_button = tk.Button(
            self.sidebar, text="上一主题", command=lambda: self.cycle_theme(-1), anchor="center",
            bg=COLORS["sidebar"], fg=COLORS["sidebar_muted"],
            activebackground=COLORS["nav_hover"], activeforeground=COLORS["sidebar_text"],
            relief="flat", bd=0, highlightthickness=0, font=(FONT, 9, "bold"),
            padx=4, pady=9, cursor="hand2",
        )
        self.sidebar_previous_theme_button.grid(row=9, column=0, sticky="ew", padx=(10, 2), pady=(0, 3))
        self.sidebar_next_theme_button = tk.Button(
            self.sidebar, text="下一主题", command=lambda: self.cycle_theme(1), anchor="center",
            bg=COLORS["sidebar"], fg=COLORS["sidebar_muted"],
            activebackground=COLORS["nav_hover"], activeforeground=COLORS["sidebar_text"],
            relief="flat", bd=0, highlightthickness=0, font=(FONT, 9, "bold"),
            padx=4, pady=9, cursor="hand2",
        )
        self.sidebar_next_theme_button.grid(row=9, column=1, sticky="ew", padx=(2, 10), pady=(0, 3))
        # Backward-compatible name for integrations that target the former
        # one-way button.
        self.sidebar_theme_button = self.sidebar_next_theme_button
        footer = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        self.sidebar_footer = footer
        footer.grid(row=11, column=0, columnspan=2, sticky="sew", padx=22, pady=20)
        self.network_button = tk.Button(
            footer, text="●  正在准备联网  ↻", command=self.refresh_rates, anchor="center",
            bg=COLORS["accent_dark"], fg=COLORS["accent"], activebackground=COLORS["card_alt"],
            activeforeground=COLORS["accent"], relief="flat", bd=0, highlightthickness=0,
            font=(FONT, 9, "bold"), padx=10, pady=7, cursor="hand2",
        )
        self.network_button.pack(fill="x")
        self.network_status = tk.Label(
            footer, text="正在准备网络连接…", bg=COLORS["accent_dark"], fg=COLORS["accent"],
            font=(FONT, 8, "bold"), justify="center", anchor="center", padx=9, pady=8,
        )
        self.network_status.pack(fill="x", pady=(8, 0))
        self.network_time = tk.Label(
            footer, text="等待首次连接", bg=COLORS["sidebar"], fg=COLORS["sidebar_muted"],
            font=(FONT, 8), justify="left", anchor="w", wraplength=172,
        )
        self.network_time.pack(fill="x", pady=(6, 0))

        content = tk.Frame(self.root, bg=COLORS["bg"])
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        calc_mode = self.settings.last_calculator_mode if self.settings.remember_calculator_mode else self.settings.default_calculator_mode
        history = [tuple(item[:2]) for item in self.settings.calculator_history] if self.settings.retain_history else []
        self.pages["calculator"] = CalculatorPage(
            content, self.toggle_history, self.calculator_history_changed,
            initial_professional=calc_mode == "professional", mode_changed=self.calculator_mode_changed,
            angle_mode=self.settings.calculator_angle_mode, history_limit=self.settings.history_limit,
            initial_history=history, copy_result_format=self.settings.copy_result_format,
        )
        self.pages["exchange"] = ExchangePage(
            content,
            self.exchange_coordinator,
            ExchangePageState.from_mapping(self.settings.pages.get("exchange")),
            self.refresh_rates,
            lambda state: self.save_page_state("exchange", state),
            self.format_timestamp,
            colors=COLORS,
            font_name=FONT,
            currency_selector_factory=SearchSelect,
            fixed_mode="c2c",
            page_title="C2C 兑换",
        )
        self.pages["market_exchange"] = ExchangePage(
            content,
            self.exchange_coordinator,
            ExchangePageState.from_mapping(self.settings.pages.get("market_exchange")),
            self.refresh_rates,
            lambda state: self.save_page_state("market_exchange", state),
            self.format_timestamp,
            colors=COLORS,
            font_name=FONT,
            currency_selector_factory=SearchSelect,
            fixed_mode="market",
            page_title="市场兑换",
        )
        self.pages["fiat"] = DualConverterPage(
            content, self.service, "fiat", self.refresh_rates, self.format_timestamp,
            self.settings.favorite_fiats, self.settings.pinned_fiats, self.save_currency_preferences,
            coordinator=self.exchange_coordinator,
            page_state=self.settings.pages.get("fiat"),
            state_callback=lambda state: self.save_page_state("fiat", state),
        )
        self.pages["fiat_market"] = MarketPage(
            content, self.service, self.refresh_rates, self.format_timestamp, "fiat",
            self.format_chart_timestamp,
            page_state=self.settings.pages.get("fiat_market"),
            state_callback=lambda state: self.save_page_state("fiat_market", state),
        )
        self.pages["crypto"] = DualConverterPage(
            content, self.service, "crypto", self.refresh_rates, self.format_timestamp,
            self.settings.favorite_cryptos, self.settings.pinned_cryptos, self.save_currency_preferences,
            coordinator=None,
            page_state=self.settings.pages.get("crypto"),
            state_callback=lambda state: self.save_page_state("crypto", state),
        )
        self.pages["market"] = MarketPage(
            content, self.service, self.refresh_rates, self.format_timestamp, "crypto",
            self.format_chart_timestamp,
            page_state=self.settings.pages.get("market"),
            state_callback=lambda state: self.save_page_state("market", state),
        )
        self.pages["settings"] = SettingsPage(
            content, self.settings, self.set_theme, self.set_language, self.set_timezone, self.set_keep_data_with_app,
            self.save_setting, self.choose_data_directory, self.migrate_application,
            self.service.cache_size_bytes, self.clear_cache, self.export_settings, self.import_settings,
            self.reset_settings, lambda: self.open_folder(portable_dir()),
            lambda: self.open_folder(self.settings.resolved_data_dir()),
            self.local_api_status,
            self.set_local_api_enabled,
            self.set_local_api_port,
            self.issue_local_api_token,
            self.test_local_api_connection,
            self.check_application_update,
            self.download_application_update,
            self.install_application_update,
            self.force_exit,
        )
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self.history_panel = HistoryPanel(self.root, self.use_history_result, self.clear_history)
        self.history_panel.grid(row=0, column=2, sticky="nsew")
        self.history_panel.grid_remove()

    def _draw_logo(self) -> None:
        self.logo_canvas.delete("all")
        self.logo_canvas.configure(bg=COLORS["sidebar"])
        self.logo_canvas.create_polygon(21, 2, 38, 11, 38, 31, 21, 40, 4, 31, 4, 11, outline=BRAND_ORANGE, fill=BRAND_DARK, width=2)
        self.logo_canvas.create_line(10, 27, 21, 8, 32, 27, fill=BRAND_ORANGE, width=3, smooth=True)
        self.logo_canvas.create_polygon(21, 17, 27, 23, 21, 29, 15, 23, fill=BRAND_ORANGE, outline="")

    def cycle_theme(self, direction: int = 1) -> None:
        order = tuple(THEMES)
        current = self.settings.theme if self.settings.theme in THEMES else order[0]
        step = -1 if direction < 0 else 1
        self.set_theme(order[(order.index(current) + step) % len(order)])

    def _responsive_window_changed(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self.root:
            return
        width = int(event.width) if event is not None else int(self.root.winfo_width())
        compact = width < 1080
        if compact == self.sidebar_compact:
            return
        self.sidebar_compact = compact
        self.sidebar.configure(width=136 if compact else 216)
        if compact:
            self.brand_names.pack_forget()
            self.sidebar_brand.grid_configure(padx=17, pady=(20, 22))
        else:
            if not self.brand_names.winfo_manager():
                self.brand_names.pack(side="left", padx=10)
            self.sidebar_brand.grid_configure(padx=18, pady=(26, 32))
        for key, button in self.nav_buttons.items():
            button.configure(
                text=self.nav_compact_text[key] if compact else self.nav_full_text[key],
                anchor="center" if compact else "w",
                padx=4 if compact else 22,
                font=(FONT, 10 if compact else (9 if key in {"fiat_market", "market"} else 11), "bold"),
            )
        for button, label in (
            (self.sidebar_previous_theme_button, "上一主题"),
            (self.sidebar_next_theme_button, "下一主题"),
        ):
            button.configure(text=label, anchor="center", padx=2 if compact else 4)
        if compact:
            self.network_status.pack_forget()
            self.network_time.pack_forget()
            self.sidebar_footer.grid_configure(padx=10, pady=14)
            self.network_button.configure(padx=2)
        else:
            if not self.network_status.winfo_manager():
                self.network_status.pack(fill="x", pady=(8, 0))
            if not self.network_time.winfo_manager():
                self.network_time.pack(fill="x", pady=(6, 0))
            self.sidebar_footer.grid_configure(padx=22, pady=20)
            self.network_button.configure(padx=10)

    def _activate_calculator_if_current(self) -> None:
        page = self.pages.get("calculator")
        if self.current_page == "calculator" and isinstance(page, CalculatorPage):
            page.activate_keyboard()

    def format_timestamp(self, value: str) -> str:
        try:
            stamp = datetime.fromisoformat(value)
            return format_chinese_datetime(stamp.astimezone(ZoneInfo(self.settings.timezone)))
        except (ValueError, KeyError, TypeError, OverflowError, OSError):
            return "时间未知"

    def format_chart_timestamp(self, value: int) -> str:
        try:
            stamp = datetime.fromtimestamp(value / 1000, ZoneInfo("UTC"))
            return stamp.astimezone(ZoneInfo(self.settings.timezone)).strftime("%m-%d %H:%M")
        except (ValueError, KeyError, TypeError, OverflowError, OSError):
            return "时间未知"

    def show_page(self, page: str) -> None:
        active = getattr(self.root, "_active_search_select", None)
        if active is not None:
            active.close()
        if page != "calculator" and self.history_open:
            self.toggle_history()
        changed = self.current_page != page
        previous = self.pages.get(self.current_page)
        if changed and hasattr(previous, "on_hide"):
            previous.on_hide()  # type: ignore[attr-defined]
        self.current_page = page
        if self.settings.remember_last_page:
            self.settings.last_page = page
            self._persist_settings(notify=False)
        self.pages[page].tkraise()
        current = self.pages[page]
        if hasattr(current, "on_show"):
            current.on_show()  # type: ignore[attr-defined]
        for key, button in self.nav_buttons.items():
            active = key == page
            button.configure(
                bg=COLORS["nav_active"] if active else COLORS["sidebar"],
                fg=COLORS["nav_active_text"] if active else COLORS["sidebar_muted"],
            )
        if getattr(self, "_page_open_refresh_enabled", False):
            self._refresh_page_on_open(page)

    def _refresh_page_on_open(self, page: str) -> None:
        section = {
            "exchange": "all",
            "market_exchange": "all",
            "fiat": "fiat",
            "fiat_market": "fiat",
            "crypto": "crypto",
            "market": "crypto",
        }.get(page)
        if section is None or getattr(self, "exiting", False):
            return
        startup_job = getattr(self, "startup_job", None)
        if startup_job:
            try:
                self.root.after_cancel(startup_job)
            except tk.TclError:
                pass
            self.startup_job = None
        self.refresh_rates(section)

    def _startup_rate_refresh(self) -> None:
        self.startup_job = None
        if not self.exiting:
            self.refresh_rates("all")

    def _dismiss_search_popup(self, event: tk.Event) -> None:
        active = getattr(self.root, "_active_search_select", None)
        if active is None:
            return
        widget_path = str(event.widget)
        if widget_path.startswith(str(active)):
            return
        if active.popup is not None and widget_path.startswith(str(active.popup)):
            return
        active.close()

    def refresh_rates(self, section: str = "all", automatic: bool = False) -> None:
        if section not in {"all", "fiat", "crypto"}:
            section = "all"
        if self.loading_rates:
            if automatic and section in {"fiat", "crypto"}:
                self._schedule_single_auto_refresh(section, 20_000)
            elif not automatic and self.active_rate_section != "all" and section != self.active_rate_section:
                if section == "all" or self.pending_rate_section not in {None, section}:
                    self.pending_rate_section = "all"
                else:
                    self.pending_rate_section = section
            return
        self.loading_rates = True
        self.active_rate_section = section
        self.network_button.configure(text="●  正在重新连接…", state="disabled")
        scope_text = "货币" if section == "fiat" else "虚拟币" if section == "crypto" else "汇率与行情"
        self._set_network_status(None, f"正在获取最新{scope_text}")
        page_keys = ("exchange", "market_exchange", "fiat", "fiat_market") if section == "fiat" else ("exchange", "market_exchange", "crypto", "market") if section == "crypto" else ("exchange", "market_exchange", "fiat", "fiat_market", "crypto", "market")
        for key in page_keys:
            page = self.pages.get(key)
            if hasattr(page, "begin_refresh"):
                page.begin_refresh()  # type: ignore[attr-defined]
        # Repaint the last trusted batch one row at a time immediately while the
        # fresh network batch is in flight; the fresh result replaces it on arrival.
        if self.service.snapshot.rates:
            converter_keys = ("fiat",) if section == "fiat" else ("crypto",) if section == "crypto" else ("fiat", "crypto")
            for key in converter_keys:
                page = self.pages.get(key)
                if isinstance(page, DualConverterPage):
                    page.update_table(self.service.snapshot, animate=True)
            market_keys = ("fiat_market",) if section == "fiat" else ("market",) if section == "crypto" else ("fiat_market", "market")
            for key in market_keys:
                market_page = self.pages.get(key)
                if isinstance(market_page, MarketPage):
                    market_page._refresh_watchlist(self.service.snapshot, animated=True)

        def worker() -> None:
            try:
                snapshot = self.service.refresh(section)
                self.rate_results.deliver(snapshot, None, section)
            except Exception as exc:
                self.rate_results.deliver(None, str(exc), section)

        self.rate_results.expect()
        threading.Thread(target=worker, daemon=True).start()

    def _finish_rates(self, snapshot: RateSnapshot | None, error: str | None, section: str = "all") -> None:
        if self.exiting:
            return
        self.loading_rates = False
        self.active_rate_section = None
        if snapshot:
            self.last_network_at = snapshot.fetched_at
            self.apply_snapshot(snapshot, False, animated=True, section=section)
            if snapshot.errors:
                detail = "；".join(snapshot.errors[:2])
                self.last_network_detail = f"部分更新 · {detail}"
                self.network_button.configure(text="●  部分数据已更新  ↻", state="normal")
                self._set_network_status("partial", f"{self.format_timestamp(snapshot.fetched_at)} {self.last_network_detail}")
            else:
                self.last_network_detail = "已联网"
                self.network_button.configure(text="●  汇率已联网  ↻", state="normal")
                self._set_network_status(True, f"{self.format_timestamp(snapshot.fetched_at)} {self.last_network_detail}")
        else:
            now = datetime.now().astimezone().isoformat()
            self.last_network_at = now
            self.last_network_detail = "连接失败"
            self.network_button.configure(text="●  重新连接网络  ↻", state="normal")
            self._set_network_status(False, f"{self.format_timestamp(now)} {self.last_network_detail}")
            if self.service.snapshot.rates:
                self.apply_snapshot(self.service.snapshot, True, animated=True, section=section)
            else:
                page_keys = ("exchange", "market_exchange", "fiat", "fiat_market") if section == "fiat" else ("exchange", "market_exchange", "crypto", "market") if section == "crypto" else ("exchange", "market_exchange", "fiat", "fiat_market", "crypto", "market")
                for key in page_keys:
                    page = self.pages.get(key)
                    if hasattr(page, "finish_refresh_failure"):
                        page.finish_refresh_failure()  # type: ignore[attr-defined]
        if section == "all":
            self.schedule_auto_refresh()
        elif section in {"fiat", "crypto"}:
            self._schedule_single_auto_refresh(section)
        pending = self.pending_rate_section
        self.pending_rate_section = None
        if pending:
            self.refresh_rates(pending)

    def schedule_auto_refresh(self) -> None:
        for section, job in self.auto_jobs.items():
            if job:
                self.root.after_cancel(job)
                self.auto_jobs[section] = None
        if not self.settings.auto_refresh_enabled:
            return
        self._schedule_single_auto_refresh("fiat")
        self._schedule_single_auto_refresh("crypto")

    def _schedule_single_auto_refresh(self, section: str, delay_ms: int | None = None) -> None:
        job = self.auto_jobs.get(section)
        if job:
            self.root.after_cancel(job)
        if not self.settings.auto_refresh_enabled:
            self.auto_jobs[section] = None
            return
        minutes = self.settings.fiat_refresh_minutes if section == "fiat" else self.settings.crypto_refresh_minutes
        delay = delay_ms if delay_ms is not None else max(1, minutes) * 60_000
        self.auto_jobs[section] = self.root.after(delay, lambda target=section: self._auto_refresh(target))

    def _auto_refresh(self, section: str) -> None:
        self.auto_jobs[section] = None
        if not self.settings.auto_refresh_enabled:
            return
        if self.root.state() == "iconic" and not self.settings.refresh_when_minimized:
            self._schedule_single_auto_refresh(section, 60_000)
            return
        self.refresh_rates(section, automatic=True)

    def _set_network_status(self, connected: bool | str | None, time_text: str) -> None:
        if connected is True:
            text = "当前网络连接成功！"
        elif connected is False:
            text = "当前网络连接失败！"
        elif connected == "partial":
            text = "部分数据更新成功"
        else:
            text = "正在更新网络状态…"
        self.network_state = connected
        self._apply_network_status_palette()
        self.network_status.configure(text=text)
        self.network_time.configure(text=time_text)

    def _apply_network_status_palette(self) -> None:
        bg, fg = NETWORK_STATUS_PALETTES.get(
            getattr(self, "network_state", None), NETWORK_STATUS_PALETTES[None]
        )
        self.network_button.configure(
            bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
            disabledforeground=fg,
        )
        self.network_status.configure(bg=bg, fg=fg)

    def apply_snapshot(self, snapshot: RateSnapshot, from_cache: bool, animated: bool = False, section: str = "all") -> None:
        keys = (
            ("exchange", "market_exchange", "fiat", "fiat_market", "crypto", "market") if section == "fiat" else
            ("exchange", "market_exchange", "crypto", "market") if section == "crypto" else
            ("exchange", "market_exchange", "fiat", "fiat_market", "crypto", "market")
        )
        for key in keys:
            page = self.pages.get(key)
            if page is None:
                continue
            if isinstance(page, MarketPage):
                reload_chart = (
                    section == "all"
                    or (section == "fiat" and key == "fiat_market")
                    or (section == "crypto" and key == "market")
                )
                page.apply_snapshot(snapshot, from_cache, animated, reload_chart=reload_chart)
            elif hasattr(page, "apply_snapshot"):
                page.apply_snapshot(snapshot, from_cache, animated)  # type: ignore[attr-defined]

    def toggle_history(self) -> None:
        page = self.pages.get("calculator")
        if not isinstance(page, CalculatorPage):
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if not self.history_open:
            self.closed_width = width
            self.history_panel.grid()
            self.history_open = True
            self.refresh_history()
            self.root.geometry(f"{width + self.history_width}x{height}+{x}+{y}")
        else:
            self.history_panel.grid_remove()
            self.history_open = False
            self.root.geometry(f"{getattr(self, 'closed_width', width - self.history_width)}x{height}+{x}+{y}")
        page.set_history_open(self.history_open)

    def refresh_history(self) -> None:
        page = self.pages.get("calculator")
        if isinstance(page, CalculatorPage) and hasattr(self, "history_panel"):
            self.history_panel.refresh(page.model.history)

    def calculator_history_changed(self) -> None:
        page = self.pages.get("calculator")
        if not isinstance(page, CalculatorPage):
            return
        page._refresh_context_lines()
        self.settings.calculator_history = (
            [list(item) for item in page.model.history[:self.settings.history_limit]]
            if self.settings.retain_history else []
        )
        self.refresh_history()
        # History is user data, so save it at the calculation boundary instead
        # of waiting for a later full application exit.
        self._persist_settings(notify=False)

    def clear_history(self) -> None:
        page = self.pages.get("calculator")
        if isinstance(page, CalculatorPage):
            page.model.history.clear()
            self.calculator_history_changed()

    def use_history_result(self, result: str) -> None:
        page = self.pages.get("calculator")
        if isinstance(page, CalculatorPage):
            page.model.expression = result.replace("-", "−")
            page.model.just_evaluated = False
            page.refresh_display()

    def _persist_settings(self, notify: bool = True) -> bool:
        # Discard an older frozen debounce snapshot; self.settings already
        # contains the latest page state and is the authoritative final copy.
        cancel_pending = getattr(self.settings_store, "cancel_pending_save", None)
        if callable(cancel_pending):
            cancel_pending()
        saved = self.settings_store.save(self.settings)
        if not saved and notify and not self.persistence_warning_shown:
            self.persistence_warning_shown = True
            messagebox.showerror(
                "设置未保存",
                "无法写入应用设置文件；本次更改仅在当前会话有效。请检查应用文件夹权限。",
            )
        return saved

    def save_page_state(self, page: str, state: dict[str, object]) -> None:
        if page not in {"exchange", "market_exchange", "fiat", "crypto", "fiat_market", "market", "settings"}:
            return
        self.settings.pages[page] = dict(state)
        self.settings_store.schedule_save(self.settings)

    def save_setting(self, key: str, value: object) -> None:
        if key in AppSettings.__dataclass_fields__:
            setattr(self.settings, key, value)
        self.settings = self.settings_store.validate(self.settings)
        self._persist_settings()
        if key in {"auto_refresh_enabled", "fiat_refresh_minutes", "crypto_refresh_minutes", "refresh_when_minimized"}:
            self.schedule_auto_refresh()
        if key == "cache_limit_mb":
            self.service.set_cache_limit(self.settings.cache_limit_mb)
            page = self.pages.get("settings")
            if isinstance(page, SettingsPage):
                page.refresh_cache_size()
        if key in {"calculator_angle_mode", "history_limit", "copy_result_format"}:
            page = self.pages.get("calculator")
            if isinstance(page, CalculatorPage):
                page.apply_calculator_settings(
                    self.settings.calculator_angle_mode,
                    self.settings.history_limit,
                    self.settings.copy_result_format,
                )
                self.refresh_history()
        if key == "retain_history" and not self.settings.retain_history:
            self.settings.calculator_history = []
            self._persist_settings()

    def save_currency_preferences(self, mode: str, favorites: list[str], pins: list[str]) -> None:
        if mode == "fiat":
            self.settings.favorite_fiats = favorites
            self.settings.pinned_fiats = pins
        else:
            self.settings.favorite_cryptos = favorites
            self.settings.pinned_cryptos = pins
        page_state = dict(self.settings.pages.get(mode, {}))
        page_state.update({"favorites": list(favorites), "pinned": list(pins)})
        self.settings.pages[mode] = page_state
        self.settings_store.schedule_save(self.settings)

    def calculator_mode_changed(self, mode: str) -> None:
        self.settings.last_calculator_mode = mode
        if self.settings.remember_calculator_mode:
            self._persist_settings(notify=False)

    def _record_api_security_warning(self, message: str) -> None:
        text = str(message)[:300]
        if text and text not in self.api_security_warnings:
            self.api_security_warnings.append(text)

    def local_api_status(self) -> str:
        config = self.settings.local_api
        port = int(config.get("port", 17890))
        server = self.local_api_server
        warning = f"；安全警告：{self.api_security_warnings[-1]}" if self.api_security_warnings else ""
        last_error = f"；最近错误：{self.local_api_last_error}" if self.local_api_last_error else ""
        if server is not None and server.is_running:
            return f"运行中：http://127.0.0.1:{server.port}（仅本机）{warning}"
        try:
            token_exists = self.secret_store.exists()
        except SecretStoreError:
            return f"未运行：令牌校验文件不可用{warning}{last_error}"
        if bool(config.get("enabled", False)) and not token_exists:
            return f"已启用但未监听：请先生成一次性令牌{warning}{last_error}"
        if bool(config.get("enabled", False)):
            return f"已启用但未运行：检查端口 {port} 或重新启动{warning}{last_error}"
        return f"已关闭；固定地址 127.0.0.1:{port}，不会监听外网{warning}{last_error}"

    def _start_local_api_if_enabled(self) -> None:
        self.api_start_job = None
        if self.exiting or not bool(self.settings.local_api.get("enabled", False)):
            return
        try:
            if not self.secret_store.exists():
                return
            self._start_local_api()
        except LocalAPIPortInUseError:
            self.local_api_last_error = f"端口 {self.settings.local_api.get('port', 17890)} 已被占用"
            self.settings.local_api["enabled"] = False
            self._persist_settings(notify=False)
        except (LocalAPIError, SecretStoreError):
            self.local_api_last_error = "启动失败或令牌存储不可用"
            self.settings.local_api["enabled"] = False
            self._persist_settings(notify=False)
        page = self.pages.get("settings")
        if isinstance(page, SettingsPage):
            page.api_enabled_var.set(bool(self.settings.local_api.get("enabled", False)))
            page.refresh_api_status()

    def _start_local_api(self) -> int:
        if not self.secret_store.exists():
            raise SecretStoreError("启用本机 API 前必须先生成令牌")
        # A slot existing is not proof that its verifier is readable.  A
        # deliberately invalid candidate triggers bounded validation/recovery.
        self.secret_store.verify("")
        port = int(self.settings.local_api.get("port", 17890))
        current = self.local_api_server
        if current is not None and current.is_running and current.configured_port == port:
            return current.port
        if current is not None:
            current.stop()
        server = LocalAPIServer(
            command_service=self.local_api_command,
            token_verifier=self.secret_store,
            host="127.0.0.1",
            port=port,
        )
        server.start()
        self.local_api_server = server
        self.local_api_last_error = ""
        return server.port

    def _stop_local_api(self) -> bool:
        server = self.local_api_server
        if server is None:
            return False
        result = server.stop()
        if not server.is_running:
            self.local_api_server = None
        return result

    def set_local_api_enabled(self, enabled: bool) -> str:
        enabled = bool(enabled)
        if not enabled:
            try:
                self._stop_local_api()
            except LocalAPIError:
                self.local_api_last_error = "停止服务失败，请退出应用后确认端口已释放"
            self.settings.local_api["enabled"] = False
            if not self._persist_settings(notify=False):
                return "API 已在本次会话停止，但设置保存失败；下次启动前请再次确认。"
            return self.local_api_status()
        try:
            if not self.secret_store.exists():
                self.settings.local_api["enabled"] = True
                self._persist_settings(notify=False)
                return "已记录启用选择，但尚未监听：请先生成一次性令牌。"
            self._start_local_api()
        except LocalAPIPortInUseError:
            self.local_api_last_error = f"端口 {self.settings.local_api.get('port', 17890)} 已被占用"
            self.settings.local_api["enabled"] = False
            self._persist_settings(notify=False)
            return f"启用失败：端口 {self.settings.local_api.get('port', 17890)} 已被占用。"
        except (LocalAPIError, SecretStoreError):
            self.local_api_last_error = "本机 API 或令牌存储不可用"
            self.settings.local_api["enabled"] = False
            self._persist_settings(notify=False)
            return "启用失败：本机 API 或令牌存储不可用。"
        self.settings.local_api["enabled"] = True
        self.local_api_last_error = ""
        if not self._persist_settings(notify=False):
            self._stop_local_api()
            self.settings.local_api["enabled"] = False
            return "API 已停止：无法保存启用设置。"
        return self.local_api_status()

    def set_local_api_port(self, port: int) -> str:
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("端口必须在 1–65535 之间")
        previous = int(self.settings.local_api.get("port", 17890))
        if port == previous:
            return self.local_api_status()
        if self.exiting:
            # SettingsPage.flush_state may commit the last edited field while
            # the app is already closing.  Persist it, but never restart a
            # listener that force_exit is about to stop.
            self.settings.local_api["port"] = port
            return self.local_api_status()
        enabled = bool(self.settings.local_api.get("enabled", False))
        was_running = bool(self.local_api_server and self.local_api_server.is_running)
        if was_running:
            self._stop_local_api()
        self.settings.local_api["port"] = port
        try:
            if enabled and self.secret_store.exists():
                self._start_local_api()
        except (LocalAPIError, SecretStoreError):
            self.local_api_last_error = f"端口 {port} 无法使用"
            self.settings.local_api["port"] = previous
            if was_running:
                try:
                    self._start_local_api()
                except (LocalAPIError, SecretStoreError):
                    self.settings.local_api["enabled"] = False
            self._persist_settings(notify=False)
            return f"端口 {port} 无法使用，已恢复为 {previous}。"
        if not self._persist_settings(notify=False):
            return "端口已在本次会话更改，但设置保存失败。"
        return self.local_api_status()

    def issue_local_api_token(self, action: str) -> str:
        if action == "generate":
            token = self.secret_store.generate()
        elif action == "rotate":
            token = self.secret_store.rotate()
        else:
            raise ValueError("令牌操作无效")
        if bool(self.settings.local_api.get("enabled", False)):
            try:
                self._start_local_api()
            except LocalAPIPortInUseError:
                self.local_api_last_error = f"令牌已更新，但端口 {self.settings.local_api.get('port', 17890)} 被占用"
            except (LocalAPIError, SecretStoreError):
                self.local_api_last_error = "令牌已更新，但服务启动失败"
        return token

    def test_local_api_connection(self) -> str:
        server = self.local_api_server
        if server is None or not server.is_running:
            return "连接测试失败：本机 API 当前未运行。"
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2.0)
        try:
            connection.request("GET", "/health", headers={"Host": f"127.0.0.1:{server.port}"})
            response = connection.getresponse()
            body = response.read(16 * 1024 + 1)
            if len(body) > 16 * 1024:
                raise ValueError("health response too large")
            payload = json.loads(body.decode("utf-8"))
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            if response.status == 200 and isinstance(data, dict) and data.get("service_status") == "running":
                return f"连接成功：{server.base_url}/health（仅确认本机监听，不验证令牌）。"
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        return "连接测试失败：本机监听未返回有效健康状态。"

    def check_application_update(self) -> UpdateInfo:
        return self.update_service.check(APP_VERSION)

    def download_application_update(self, info: UpdateInfo) -> DownloadedUpdate:
        return self.update_service.download(info)

    def install_application_update(self, download: DownloadedUpdate) -> None:
        for page_widget in self.pages.values():
            flush = getattr(page_widget, "flush_state", None)
            if callable(flush):
                flush()
        self._persist_settings(notify=False)
        self.update_service.launch_installer(download, portable_dir())
        self.update_installing = True

    def _queue_geometry_save(self, _event: tk.Event | None = None) -> None:
        if not self.settings.remember_window_geometry or self.history_open or self.root.state() != "normal":
            return
        if self.geometry_job:
            self.root.after_cancel(self.geometry_job)
        self.geometry_job = self.root.after(500, self._save_geometry)

    def _save_geometry(self) -> None:
        self.geometry_job = None
        if self.settings.remember_window_geometry and not self.history_open and self.root.state() == "normal":
            self.settings.window_geometry = self.root.geometry()
            self._persist_settings(notify=False)

    def on_close_request(self) -> None:
        if getattr(self, "update_installing", False):
            self.force_exit()
            return
        if self.settings.close_action == "minimize" and not self.exiting:
            if self.history_open:
                self.toggle_history()
            self.root.iconify()
            return
        self.force_exit()

    def restore_window(self) -> None:
        """Restore and foreground the existing window after a repeated launch."""

        if self.exiting:
            return
        try:
            if self.root.state() in {"iconic", "withdrawn"}:
                self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)

            def clear_topmost() -> None:
                try:
                    self.root.attributes("-topmost", False)
                except (RuntimeError, tk.TclError):
                    pass

            self.root.after_idle(clear_topmost)
            self.root.focus_force()
        except (RuntimeError, tk.TclError):
            return

    def force_exit(self) -> None:
        if self.exiting:
            return
        self.exiting = True
        for page_widget in self.pages.values():
            if hasattr(page_widget, "flush_state"):
                try:
                    page_widget.flush_state()  # type: ignore[attr-defined]
                except (RuntimeError, ValueError, tk.TclError):
                    pass
        page = self.pages.get("calculator")
        if isinstance(page, CalculatorPage):
            self.settings.last_calculator_mode = "professional" if page.professional else "standard"
            self.settings.calculator_history = [list(item) for item in page.model.history[:self.settings.history_limit]] if self.settings.retain_history else []
        if self.settings.remember_window_geometry and not self.history_open and self.root.state() == "normal":
            self.settings.window_geometry = self.root.geometry()
        cancel_pending = getattr(self.settings_store, "cancel_pending_save", None)
        if callable(cancel_pending):
            cancel_pending()
        try:
            self._stop_local_api()
        except (LocalAPIError, OSError, RuntimeError):
            pass
        self._persist_settings(notify=False)
        self.rate_results.close()
        self.loading_rates = False
        self.active_rate_section = None
        self.pending_rate_section = None
        for attr in ("startup_job", "api_start_job", "geometry_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        for job in self.auto_jobs.values():
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self.root.destroy()

    def clear_cache(self) -> None:
        if not messagebox.askyesno("清理缓存", "确定清理汇率与行情缓存吗？\n应用设置、收藏和置顶不会被删除。"):
            return
        self.service.clear_cache()
        self.c2c_service.clear_memory_cache()
        page = self.pages.get("settings")
        if isinstance(page, SettingsPage):
            page.refresh_cache_size()
        messagebox.showinfo("缓存已清理", "缓存已清理。当前会话中的汇率仍可继续使用。")

    def export_settings(self) -> None:
        target = filedialog.asksaveasfilename(title="导出曜衡设置", defaultextension=".json", filetypes=[("JSON 设置", "*.json")], initialfile="曜衡设置.json")
        if not target:
            return
        try:
            if not self.settings_store.save(self.settings):
                raise OSError("当前设置无法写入应用文件夹")
            shutil.copy2(self.settings_store.path, Path(target))
            messagebox.showinfo("导出完成", f"设置已导出到：\n{target}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))

    def import_settings(self) -> None:
        source = filedialog.askopenfilename(title="导入曜衡设置", filetypes=[("JSON 设置", "*.json")])
        if not source:
            return
        try:
            imported = self.settings_store.from_file(Path(source))
            if not self.settings_store.save(imported):
                raise OSError("应用设置文件不可写")
            try:
                self._stop_local_api()
            except LocalAPIError:
                pass
            self.settings = imported
            settings_page = self.pages.get("settings")
            if isinstance(settings_page, SettingsPage):
                settings_page.settings = self.settings
                settings_page.api_enabled_var.set(bool(self.settings.local_api.get("enabled", False)))
                settings_page.api_port_var.set(str(self.settings.local_api.get("port", 17890)))
                settings_page.refresh_api_status()
            messagebox.showinfo("导入完成", "设置已导入，重新启动曜衡后全部生效。")
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            messagebox.showerror("导入失败", f"该文件不是有效的曜衡设置：\n{exc}")

    def reset_settings(self) -> None:
        if not messagebox.askyesno("恢复默认设置", "确定恢复所有默认设置吗？\n收藏、置顶和计算历史也会重置。"):
            return
        defaults = AppSettings()
        if self.settings_store.save(defaults):
            try:
                self._stop_local_api()
            except LocalAPIError:
                pass
            self.settings = defaults
            settings_page = self.pages.get("settings")
            if isinstance(settings_page, SettingsPage):
                settings_page.settings = self.settings
                settings_page.api_enabled_var.set(False)
                settings_page.api_port_var.set("17890")
                settings_page.refresh_api_status()
            messagebox.showinfo("已恢复默认设置", "默认设置已写入，重新启动曜衡后全部生效。")
        else:
            messagebox.showerror("恢复失败", "应用设置文件不可写，请检查应用文件夹权限。")

    @staticmethod
    def open_folder(path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开文件夹", str(exc))

    def _prepare_theme_bindings(self, palette: Mapping[str, str]) -> None:
        """Resolve widget color roles once so every later switch stays instant."""

        keys_by_color: dict[str, list[str]] = {}
        for key, value in palette.items():
            keys_by_color.setdefault(value, []).append(key)

        def belongs_to(widget: tk.Misc, ancestor: tk.Misc) -> bool:
            current: tk.Misc | None = widget
            while current is not None:
                if current is ancestor:
                    return True
                current = getattr(current, "master", None)
            return False

        def theme_key(widget: tk.Misc, option: str, value: str) -> str | None:
            candidates = keys_by_color.get(value, [])
            if not candidates:
                return None
            foreground_options = {"foreground", "activeforeground", "selectforeground", "disabledforeground", "insertbackground"}
            if option in foreground_options:
                if option == "selectforeground":
                    return "selection_text"
                try:
                    widget_background = str(widget.cget("background"))
                except (tk.TclError, TypeError):
                    widget_background = ""
                contextual_text = (
                    ("calc_operator_bg", "calc_operator_text"),
                    ("calc_function_bg", "calc_function_text"),
                    ("calc_number_bg", "calc_number_text"),
                    ("accent", "on_accent"),
                    ("nav_active", "nav_active_text"),
                    ("input_bg", "input_text"),
                    ("sidebar", "sidebar_text"),
                    ("display_bg", "display_text"),
                )
                for background_role, text_role in contextual_text:
                    if widget_background == palette.get(background_role) and value == palette.get(text_role):
                        return text_role
                for preferred in (
                    "muted", "subtle", "accent", "text", "sidebar_muted", "sidebar_text",
                    "input_text", "button_text", "key_text", "display_expression", "display_text",
                ):
                    if value == palette.get(preferred):
                        return preferred
            if len(candidates) == 1:
                return candidates[0]
            if "sidebar" in candidates and hasattr(self, "sidebar") and belongs_to(widget, self.sidebar):
                return "sidebar"
            if "card" in candidates:
                return "card"
            return candidates[0]

        color_options = (
            "background", "foreground", "activebackground", "activeforeground",
            "insertbackground", "highlightbackground", "highlightcolor",
            "selectbackground", "selectforeground", "readonlybackground",
            "selectcolor", "disabledforeground",
        )

        supported_options: dict[type[object], tuple[str, ...]] = {}
        self._theme_bindings.clear()
        self._themed_selects.clear()
        self._themed_palette_pickers.clear()
        self._themed_calculator_keys.clear()
        page_by_identity = {id(page): key for key, page in self.pages.items()}

        def owner_page(widget: tk.Misc) -> str | None:
            current: tk.Misc | None = widget
            while current is not None:
                page_key = page_by_identity.get(id(current))
                if page_key is not None:
                    return page_key
                current = getattr(current, "master", None)
            return None

        def capture(widget: tk.Misc) -> None:
            page_key = owner_page(widget)
            if isinstance(widget, SearchSelect):
                self._themed_selects.append((widget, page_key))
                return
            if isinstance(widget, ThemePalettePicker):
                self._themed_palette_pickers.append(widget)
                return
            if isinstance(widget, CalculatorKey):
                self._themed_calculator_keys.append((widget, page_key))
                return
            roles: list[tuple[str, str]] = []
            widget_type = type(widget)
            options = supported_options.get(widget_type)
            if options is None:
                try:
                    configuration = widget.configure()
                except (tk.TclError, TypeError):
                    configuration = {}
                options = tuple(option for option in color_options if option in configuration)
                supported_options[widget_type] = options
            for option in options:
                try:
                    current = str(widget.cget(option))
                    key = theme_key(widget, option, current)
                    if key and key in palette:
                        roles.append((option, key))
                except (tk.TclError, TypeError):
                    continue
            if roles:
                self._theme_bindings.append((widget, tuple(roles), page_key))
            for child in widget.winfo_children():
                capture(child)

        capture(self.root)

    def _apply_theme_bindings(self, *, all_pages: bool = False) -> None:
        """Apply ordinary widget colors in one Tcl batch instead of thousands of calls."""

        commands: list[str] = []
        active_bindings: list[tuple[tk.Misc, tuple[tuple[str, str], ...]]] = []
        for widget, roles, page_key in self._theme_bindings:
            if not all_pages and page_key is not None and page_key != self.current_page:
                continue
            try:
                if not widget.winfo_exists():
                    continue
            except tk.TclError:
                continue
            arguments = [str(widget), "configure"]
            for option, role in roles:
                arguments.extend((f"-{option}", COLORS[role]))
            commands.append(" ".join(arguments))
            active_bindings.append((widget, roles))
        if commands:
            try:
                self.root.tk.eval("\n".join(commands))
            except tk.TclError:
                # A dynamically destroyed widget should not leave the rest of
                # the interface on the old palette. Retry valid widgets alone.
                for widget, roles in active_bindings:
                    try:
                        widget.configure(**{option: COLORS[role] for option, role in roles})
                    except (tk.TclError, TypeError):
                        continue

    def _apply_theme_specials(self, *, all_pages: bool = False) -> None:
        for select, page_key in self._themed_selects:
            if all_pages or page_key is None or page_key == self.current_page:
                select.apply_theme()
        # The picker must expose the selected name synchronously even while
        # Settings is hidden; its collapsed header is deliberately lightweight.
        if not all_pages:
            for picker in self._themed_palette_pickers:
                picker.set_theme(self.settings.theme)
        for calculator_key, page_key in self._themed_calculator_keys:
            if all_pages or page_key is None or page_key == self.current_page:
                calculator_key.apply_theme()
        if hasattr(self, "network_button") and hasattr(self, "network_status"):
            self._apply_network_status_palette()
        settings_page = self.pages.get("settings")
        if isinstance(settings_page, SettingsPage) and (all_pages or self.current_page == "settings"):
            settings_page._apply_api_status_style()

    def _apply_financial_theme(self, *, all_pages: bool = False) -> None:
        for key in ("fiat", "crypto"):
            if not all_pages and key != self.current_page:
                continue
            page = self.pages.get(key)
            if isinstance(page, DualConverterPage):
                page.table.tag_configure("up", background=COLORS["up_row"], foreground=COLORS["text"])
                page.table.tag_configure("down", background=COLORS["down_row"], foreground=COLORS["text"])
                page._show_row_actions()
        for key in ("fiat_market", "market"):
            if not all_pages and key != self.current_page:
                continue
            market = self.pages.get(key)
            if isinstance(market, MarketPage):
                market.watch_table.tag_configure("up", background=COLORS["up_row"], foreground=COLORS["text"])
                market.watch_table.tag_configure("down", background=COLORS["down_row"], foreground=COLORS["text"])
                market.watch_table.tag_configure("flat", background=COLORS["card_alt"], foreground=COLORS["text"])
                market.chart.configure(bg=COLORS["card"])
                market._highlight_days()
                try:
                    self.root.after_idle(market.chart.redraw)
                except tk.TclError:
                    pass

    def _finish_deferred_theme(self) -> None:
        self._theme_idle_job = None
        if self.exiting:
            return
        self._apply_theme_bindings(all_pages=True)
        self._apply_theme_specials(all_pages=True)
        self._apply_financial_theme(all_pages=True)

    def set_theme(self, theme: str) -> None:
        if theme not in THEMES or theme == self.settings.theme:
            return
        started = time.perf_counter()
        self.settings.theme = theme
        # Keep the visual switch independent of disk latency. The debounced
        # snapshot is flushed synchronously during normal application exit.
        self.settings_store.schedule_save(self.settings)
        COLORS.clear()
        COLORS.update(THEMES[theme])

        self._apply_theme_bindings()
        self._styles()
        self._draw_logo()

        self._apply_theme_specials()
        for key, button in self.nav_buttons.items():
            active = key == self.current_page
            button.configure(
                bg=COLORS["nav_active"] if active else COLORS["sidebar"],
                fg=COLORS["nav_active_text"] if active else COLORS["sidebar_muted"],
                activebackground=COLORS["nav_hover"],
                activeforeground=COLORS["sidebar_text"],
            )
        for button in (self.sidebar_previous_theme_button, self.sidebar_next_theme_button):
            button.configure(
                bg=COLORS["sidebar"], fg=COLORS["sidebar_muted"],
                activebackground=COLORS["nav_hover"], activeforeground=COLORS["sidebar_text"],
            )
        settings_page = self.pages.get("settings")
        if isinstance(settings_page, SettingsPage):
            settings_page.theme_var.set(theme_label(theme, self.settings.language))
            settings_page.theme_cycle_var.set(settings_page._theme_cycle_text(theme))
        self._apply_financial_theme()
        if self._theme_idle_job is not None:
            try:
                self.root.after_cancel(self._theme_idle_job)
            except tk.TclError:
                pass
        try:
            self._theme_idle_job = self.root.after_idle(self._finish_deferred_theme)
        except tk.TclError:
            self._theme_idle_job = None
        self.last_theme_switch_ms = (time.perf_counter() - started) * 1000

    def set_language(self, language: str) -> None:
        selected = normalize_language(language)
        if selected == self.settings.language and get_language() == selected:
            return
        self.settings.language = selected
        set_ui_language(selected)
        self.settings_store.schedule_save(self.settings)
        refresh_widget_tree(self.root)
        for page in self.pages.values():
            apply_language = getattr(page, "apply_language", None)
            if callable(apply_language):
                try:
                    apply_language()
                except (RuntimeError, tk.TclError, ValueError):
                    pass
        self.root.title(f"曜衡 {APP_VERSION}")
        self.set_timezone(self.settings.timezone)

    def set_timezone(self, zone: str) -> None:
        try:
            ZoneInfo(zone)
        except (KeyError, ValueError):
            return
        self.settings.timezone = zone
        self._persist_settings()
        if self.service.snapshot.fetched_at:
            stamp = f"最新刷新：{self.format_timestamp(self.service.snapshot.fetched_at)}"
            for key in ("fiat", "fiat_market", "crypto", "market"):
                page = self.pages.get(key)
                refresh_stamp = getattr(page, "refresh_stamp_var", None)
                if refresh_stamp is not None:
                    refresh_stamp.set(stamp)
        if self.last_network_at:
            self.network_time.configure(
                text=f"{self.format_timestamp(self.last_network_at)} {self.last_network_detail}"
            )
        for key in ("fiat_market", "market"):
            market = self.pages.get(key)
            chart = getattr(market, "chart", None)
            if chart is not None:
                chart.redraw()

    def set_keep_data_with_app(self, keep: bool) -> None:
        previous_keep = self.settings.keep_data_with_app
        previous_data_dir = self.settings.data_dir
        if not keep and not previous_data_dir:
            self.choose_data_directory()
            settings_page = self.pages.get("settings")
            if isinstance(settings_page, SettingsPage):
                settings_page.update_paths(self.settings)
            return
        self.settings.keep_data_with_app = keep
        if keep:
            self.settings.data_dir = ""
        if not self._persist_settings():
            self.settings.keep_data_with_app = previous_keep
            self.settings.data_dir = previous_data_dir
        elif keep:
            self._switch_data_dir(portable_dir() / "data")
        else:
            self._switch_data_dir(self.settings.resolved_data_dir())
        settings_page = self.pages.get("settings")
        if isinstance(settings_page, SettingsPage):
            settings_page.update_paths(self.settings)

    def choose_data_directory(self) -> None:
        chosen = filedialog.askdirectory(title="选择曜衡缓存与行情数据文件夹", initialdir=str(self.settings.resolved_data_dir()))
        if not chosen:
            return
        target = Path(chosen).resolve()
        current = self.service.data_dir.resolve()
        if target == current or current in target.parents:
            messagebox.showerror("无法更改缓存位置", "请选择当前缓存文件夹之外的位置。")
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
            if current.exists() and current != target:
                shutil.copytree(current, target, dirs_exist_ok=True)
            previous_keep = self.settings.keep_data_with_app
            previous_data_dir = self.settings.data_dir
            self.settings.keep_data_with_app = False
            self.settings.data_dir = str(target)
            if not self._persist_settings():
                self.settings.keep_data_with_app = previous_keep
                self.settings.data_dir = previous_data_dir
                return
            self._switch_data_dir(target)
            settings_page = self.pages.get("settings")
            if isinstance(settings_page, SettingsPage):
                settings_page.update_paths(self.settings)
            messagebox.showinfo("缓存位置已更改", f"后续汇率、行情与缓存将保存在：\n{target}")
        except OSError as exc:
            messagebox.showerror("无法更改缓存位置", str(exc))

    def _switch_data_dir(self, target: Path) -> None:
        self.service.set_data_dir(target)
        self.service.set_cache_limit(self.settings.cache_limit_mb)
        if self.service.snapshot.rates:
            self.apply_snapshot(self.service.snapshot, True)

    def migrate_application(self) -> None:
        selected = filedialog.askdirectory(title="选择曜衡应用的新位置", initialdir=str(portable_dir().parent))
        if not selected:
            return
        source = portable_dir().resolve()
        target = Path(selected).resolve() / "曜衡"
        if source == target:
            messagebox.showinfo("无需迁移", "当前应用已经位于该文件夹。")
            return
        if source in target.parents:
            messagebox.showerror("无法迁移", "新位置不能放在当前应用文件夹内部。")
            return
        try:
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("build", "__pycache__"))
            if not self.settings.keep_data_with_app and self.service.data_dir.exists():
                shutil.copytree(self.service.data_dir, target / "data", dirs_exist_ok=True)
            copied_settings = AppSettings(**dict(self.settings.__dict__))
            copied_settings.data_dir = ""
            copied_settings.keep_data_with_app = True
            if not SettingsStore(target / "app_settings.json").save(copied_settings):
                raise OSError("无法在迁移位置写入设置")
            messagebox.showinfo("迁移完成", f"曜衡及相关数据已复制到：\n{target}\n\n关闭当前程序后，可从新文件夹启动。原文件仍保留。")
        except OSError as exc:
            messagebox.showerror("迁移失败", str(exc))

    @staticmethod
    def _calculator_key(event: tk.Event) -> str | None:
        if int(getattr(event, "state", 0)) & 0x000C:
            return None
        keysym = str(getattr(event, "keysym", ""))
        by_keysym = {
            **{f"KP_{digit}": digit for digit in "0123456789"},
            "KP_Add": "+", "KP_Subtract": "−", "KP_Multiply": "×", "KP_Divide": "÷",
            "KP_Decimal": ".", "KP_Separator": ".", "Return": "=", "KP_Enter": "=",
            "BackSpace": "←", "Delete": "AC", "Escape": "AC",
        }
        if keysym in by_keysym:
            return by_keysym[keysym]
        char = normalize_amount_input(str(getattr(event, "char", "")))
        if char in "0123456789":
            return char
        return {"+": "+", "-": "−", "*": "×", "/": "÷", "%": "%", "^": "xʸ", ".": ".", ",": ",", "(": "(", ")": ")", "=": "="}.get(char)

    def on_key(self, event: tk.Event) -> str | None:
        if self.current_page != "calculator":
            return None
        if isinstance(self.root.focus_get(), (tk.Entry, tk.Text, tk.Spinbox, ttk.Combobox)):
            return None
        page = self.pages["calculator"]
        if not isinstance(page, CalculatorPage):
            return None
        key = self._calculator_key(event)
        if key is None:
            return None
        page.handle(key)
        return "break"

    def run(self) -> None:
        self.root.mainloop()
