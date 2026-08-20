"""Future-focused desktop UI for 曜衡."""

from __future__ import annotations

import ctypes
import math
import os
import shutil
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import filedialog, messagebox, ttk
from typing import Callable
from zoneinfo import ZoneInfo

from calculator_core import CalculationError, CalculatorModel, evaluate_basic_amount, format_number
from rate_service import RateService, RateSnapshot, crypto_display_name, fiat_display_name, fiat_region, portable_dir, relative_rate_change
from settings_service import AppSettings, SettingsStore, timezone_names


THEMES = {
    "dark": {
    "bg": "#090909",
    "sidebar": "#101010",
    "card": "#171717",
    "card_alt": "#202020",
    "key": "#262626",
    "key_hover": "#343434",
    "muted_key": "#353535",
    "accent": "#FF9D2E",
    "accent_hover": "#FFB45C",
    "accent_dark": "#3B2612",
    "text": "#F7F7F7",
    "muted": "#9B9B9B",
    "line": "#303030",
    "up": "#35CF8B",
    "down": "#FF5F67",
    "on_accent": "#17100A",
    "subtle": "#858585",
    "grid": "#282828",
    "up_fill": "#11251D",
    "down_fill": "#2A1517",
    "up_row": "#176A46",
    "down_row": "#812D36",
    "tooltip": "#272727",
    "selection": "#B8DDFC",
    "selection_text": "#102436",
    },
    "light": {
    "bg": "#F1F3F6",
    "sidebar": "#FFFFFF",
    "card": "#FFFFFF",
    "card_alt": "#F6F7F9",
    "key": "#E6E9ED",
    "key_hover": "#D8DCE2",
    "muted_key": "#D9DDE2",
    "accent": "#B44100",
    "accent_hover": "#C94F00",
    "accent_dark": "#FFE3CF",
    "text": "#17191C",
    "muted": "#68707C",
    "line": "#D3D8DE",
    "up": "#08653E",
    "down": "#A91F2C",
    "on_accent": "#FFFFFF",
    "subtle": "#646C78",
    "grid": "#E4E7EB",
    "up_fill": "#E2F5EC",
    "down_fill": "#FCE7E9",
    "up_row": "#C7EAD8",
    "down_row": "#F2C8CE",
    "tooltip": "#FFFFFF",
    "selection": "#B8DDFC",
    "selection_text": "#102436",
    },
}
COLORS = dict(THEMES["dark"])

FONT = "Microsoft YaHei UI"
BRAND_ORANGE = "#FF9D2E"
BRAND_DARK = "#171717"
_FULL_WIDTH_INPUT_TRANSLATION = str.maketrans({
    **{chr(ord("０") + index): str(index) for index in range(10)},
    "＋": "+", "－": "-", "＊": "*", "／": "/", "％": "%",
    "（": "(", "）": ")", "＾": "^", "，": ",", "．": ".", "＝": "=",
})


def normalize_amount_input(value: str) -> str:
    """Normalize IME/full-width amount input before UI-side validation."""
    return value.translate(_FULL_WIDTH_INPUT_TRANSLATION)


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
            "normal": (COLORS["key"], COLORS["text"], COLORS["key_hover"]),
            "muted": (COLORS["muted_key"], COLORS["text"], COLORS["key_hover"]),
            "accent": (COLORS["accent"], COLORS["on_accent"], COLORS["accent_hover"]),
            "outline": (COLORS["card"], COLORS["accent"], COLORS["accent_dark"]),
            "soft_accent": (COLORS["accent_dark"], COLORS["accent"], COLORS["card_alt"]),
            "ghost": (COLORS["card"], COLORS["muted"], COLORS["card_alt"]),
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
            font=(FONT, size, "bold" if kind in {"accent", "outline", "soft_accent"} else "normal"),
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

    def set_values(self, values: list[str]) -> None:
        updated = list(values)
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
        self.filtered_values = self.values if not needle else [value for value in self.values if needle in value.casefold()]
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


class CalculatorPage(tk.Frame):
    """4x5 keypad that animates into a compact professional layout."""

    STANDARD_KEYS = [
        ["AC", "←", "%", "÷"],
        ["7", "8", "9", "×"],
        ["4", "5", "6", "−"],
        ["1", "2", "3", "+"],
        ["专业", "0", ".", "="],
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
        self.result_var = tk.StringVar(value="0")
        self.mode_var = tk.StringVar(value="标准模式 · 4 × 5 键盘 · 可直接输入公式")
        self._updating_expression = False
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
        self.copy_button = AppButton(header, "复制结果", self.copy_current_result, "ghost", 9)
        self.copy_button.grid(row=0, column=1, rowspan=2, padx=(0, 8), ipadx=8, ipady=7)
        self.history_button = AppButton(header, "打开历史记录", self.history_callback, "outline", 10)
        self.history_button.grid(row=0, column=2, rowspan=2, ipadx=10, ipady=7)

        stage = tk.Frame(self, bg=COLORS["bg"])
        stage.grid(row=1, column=0, sticky="nsew", padx=34, pady=(0, 28))
        stage.grid_columnconfigure(0, weight=1)
        stage.grid_rowconfigure(1, weight=1)
        display = tk.Frame(stage, bg=COLORS["card"], height=150)
        display.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        display.grid_propagate(False)
        display.grid_columnconfigure(0, weight=1)
        self.expression_entry = tk.Entry(
            display, textvariable=self.expression_var, bg=COLORS["card"], fg=COLORS["muted"],
            insertbackground=COLORS["text"], insertwidth=2,
            selectbackground=COLORS["selection"], selectforeground=COLORS["selection_text"],
            font=("Segoe UI", 18), justify="right", relief="flat", bd=0,
            highlightthickness=0,
        )
        self.expression_entry.grid(row=0, column=0, sticky="ew", padx=26, pady=(23, 0))
        self.expression_entry.bind("<FocusIn>", self._expression_focus_in)
        self.expression_entry.bind("<KeyPress>", self._expression_keypress, add="+")
        self.expression_entry.bind("<Return>", self._evaluate_manual_expression)
        self.expression_entry.bind("<KP_Enter>", self._evaluate_manual_expression)
        self.expression_var.trace_add("write", self._manual_expression_changed)
        tk.Label(display, textvariable=self.result_var, bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 39, "bold"), anchor="e", padx=24).grid(row=1, column=0, sticky="ew", pady=(2, 18))

        self.keypad_area = tk.Frame(stage, bg=COLORS["bg"])
        self.keypad_area.grid(row=1, column=0, sticky="nsew")
        self.standard_frame = tk.Frame(self.keypad_area, bg=COLORS["bg"])
        self.pro_frame = tk.Frame(self.keypad_area, bg=COLORS["bg"])
        self._build_key_frame(self.standard_frame, self.STANDARD_KEYS, professional=False)
        self._build_key_frame(self.pro_frame, self.PRO_KEYS, professional=True)
        self.standard_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

    def _build_key_frame(self, frame: tk.Frame, rows: list[list[str]], professional: bool) -> None:
        for column in range(4):
            frame.grid_columnconfigure(column, weight=1, uniform="calc")
        for row in range(len(rows)):
            frame.grid_rowconfigure(row, weight=1, uniform="calc")
        for row, values in enumerate(rows):
            for column, label in enumerate(values):
                if label == "=":
                    kind = "accent"
                elif label in {"÷", "×", "−", "+"}:
                    kind = "outline"
                elif professional or label in {"AC", "←", "%", "专业"}:
                    kind = "muted"
                else:
                    kind = "normal"
                button = AppButton(frame, label, lambda key=label: self.handle(key), kind, 13 if professional else 14)
                button.grid(row=row, column=column, sticky="nsew", padx=5, pady=5)
                if label == "专业":
                    self.mode_button = button

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
        self.mode_var.set("专业模式 · 动态科学键盘 · 可直接输入公式" if professional else "标准模式 · 4 × 5 键盘 · 可直接输入公式")

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
            result = f"{self.expression_var.get()} = {result}"
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
            self.mode_var.set("专业模式 · 动态科学键盘 · 可直接输入公式" if self.professional else "标准模式 · 4 × 5 键盘 · 可直接输入公式")
            self.mode_changed("professional" if self.professional else "standard")
            if not self.professional:
                self.pro_frame.place_forget()
                self.standard_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

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
        if normalize_amount_input(event.char) == "=":
            self.after_jobs.schedule(0, self._evaluate_manual_expression, idle=True)
            return "break"
        return None

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
        self.title = "货币换算" if mode == "fiat" else "虚拟币换算"
        self.amount_a = tk.StringVar(value="1000" if mode == "fiat" else "10000")
        self.amount_b = tk.StringVar(value="")
        self.amount_c = tk.StringVar(value="")
        self.currency_a = tk.StringVar()
        self.currency_b = tk.StringVar()
        self.currency_c = tk.StringVar()
        self.table_base_var = tk.StringVar()
        self.reference_amount_var = tk.StringVar(value="1")
        self.reference_amount_value = 1.0
        meaningful = (
            "支持全球货币 A/B/C 三端联动；金额框可直接计算加减乘除、除余和括号。"
            if mode == "fiat" else
            "法币与虚拟币可自由组合为 A/B/C 三端；金额框可直接输入基础算式。"
        )
        self.status_var = tk.StringVar(value=meaningful)
        self.refresh_stamp_var = tk.StringVar(value="最新刷新：等待联网")
        self.search_var = tk.StringVar()
        self.rate_var = tk.StringVar(value="在任意一端输入金额或算式，按回车、= 或点击输入框外完成计算与换算")
        self.active_side = "a"
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

        body = tk.Frame(self, bg=COLORS["bg"])
        body.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 26))
        body.grid_columnconfigure(0, weight=5, uniform="convert")
        body.grid_columnconfigure(1, weight=7, uniform="convert")
        body.grid_rowconfigure(0, weight=1)

        converter = tk.Frame(body, bg=COLORS["card"])
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

        list_card = tk.Frame(body, bg=COLORS["card"])
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

        reference_row = tk.Frame(list_card, bg=COLORS["card"])
        reference_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=22, pady=(3, 10))
        reference_row.grid_columnconfigure(2, weight=1)
        tk.Label(reference_row, text="参考币种", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9, "bold")).grid(row=0, column=0, padx=(0, 8))
        self.table_base_selector = SearchSelect(
            reference_row, self.table_base_var, command=lambda _value: self.table_base_changed(),
            width=12, font_size=9, max_rows=7,
        )
        self.table_base_selector.grid(row=0, column=1, sticky="w")
        AppButton(reference_row, "默认顺序", self.reset_table_order, "soft_accent", 8).grid(
            row=0, column=3, padx=(12, 10), ipady=5,
        )
        tk.Label(reference_row, text="参考币数额", bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9, "bold")).grid(row=0, column=4, padx=(0, 8))
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
        entry.bind("<FocusIn>", lambda _e, which=side: setattr(self, "active_side", which))
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
        except CalculationError as exc:
            self.reference_amount_var.set(self._format(self.reference_amount_value))
            self.rate_var.set(f"参考币数额输入有误：{exc}")
        return "break"

    def apply_snapshot(self, snapshot: RateSnapshot, from_cache: bool = False, animated: bool = False) -> None:
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
        default_a = old_a if old_a in converter_codes else ("CNY" if "CNY" in converter_codes else converter_codes[0] if converter_codes else "")
        candidates_b = converter_codes
        preferred_b = "USD" if self.mode == "fiat" else "BTC"
        default_b = old_b if old_b in candidates_b else (preferred_b if preferred_b in candidates_b else candidates_b[0] if candidates_b else "")
        preferred_c = "EUR" if self.mode == "fiat" else "ETH"
        default_c = old_c if old_c in converter_codes else (preferred_c if preferred_c in converter_codes else converter_codes[0] if converter_codes else "")
        default_table_base = old_table_base if old_table_base in fiats else ("CNY" if "CNY" in fiats else fiats[0] if fiats else "")
        self.combo_a.set(self.code_to_display.get(default_a, ""))
        self.combo_b.set(self.code_to_display.get(default_b, ""))
        self.combo_c.set(self.code_to_display.get(default_c, ""))
        self.table_base_selector.set(self.code_to_display.get(default_table_base, ""))
        self.refresh_stamp_var.set(f"最新刷新：{self.timestamp_formatter(snapshot.fetched_at)}")
        self.update_table(snapshot, animate=animated)
        self.convert_from(self.active_side)
        self.refreshing = False
        if self.spinner_job:
            try:
                self.after_cancel(self.spinner_job)
            except tk.TclError:
                pass
            self.spinner_job = None
        self.refresh_button.configure(text="↻  刷新汇率", state="normal")

    def _register_currency(self, code: str, snapshot: RateSnapshot, crypto: bool) -> str:
        name = snapshot.names.get(code, code) if crypto else fiat_display_name(code, snapshot.names.get(code, code))
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
        text = normalize_amount_input(input_var.get()).strip()
        if not text:
            for other_side, output_var in variables.items():
                if other_side != side:
                    output_var.set("")
            return
        try:
            value = evaluate_basic_amount(text)
            if not from_code or any(not code for code in codes.values()):
                raise ValueError
            input_var.set(self._format(value))
            unit_texts: list[str] = []
            for other_side in ("a", "b", "c"):
                if other_side == side:
                    continue
                to_code = codes[other_side]
                result = self.service.convert(value, from_code, to_code)
                variables[other_side].set(self._format(result))
                unit = self.service.convert(1, from_code, to_code)
                unit_texts.append(f"{self._format(unit)} {to_code}")
            self.active_side = side
            self.rate_var.set(f"1 {from_code} = {'  =  '.join(unit_texts)}\n已按 {side.upper()} 端输入同步换算另外两端")
        except CalculationError as exc:
            self.rate_var.set(f"{side.upper()} 端金额输入有误：{exc}")
        except (ValueError, KeyError):
            self.rate_var.set("请输入有效金额或算式，并为 A、B、C 三端选择币种")

    def selection_changed(self, side: str) -> None:
        # Changing a target currency must not turn its previously calculated
        # amount into the new source. The last amount field the user edited wins.
        self.convert_from(self.active_side)

    def table_base_changed(self) -> None:
        self.update_table(self.service.snapshot)

    def swap_pair(self, left: str, right: str) -> None:
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
        self.convert_from(self.active_side)

    def rotate(self) -> None:
        displays = (self.currency_a.get(), self.currency_b.get(), self.currency_c.get())
        values = (self.amount_a.get(), self.amount_b.get(), self.amount_c.get())
        self.combo_a.set(displays[2])
        self.combo_b.set(displays[0])
        self.combo_c.set(displays[1])
        self.amount_a.set(values[2])
        self.amount_b.set(values[0])
        self.amount_c.set(values[1])
        self.active_side = {"a": "b", "b": "c", "c": "a"}.get(self.active_side, "a")
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
                name = fiat_display_name(code, snapshot.names.get(code, code))
                rows.append(((code, name, self._format(converted), change_text, fiat_region(code), "", ""), tags))
        else:
            codes = [code for code in snapshot.rates if snapshot.kinds.get(code) == "crypto"]
            for code in codes:
                converted = self.service.convert(amount, base, code)
                change = snapshot.changes.get(code)
                change_text = "—" if change is None else f"{change:+.2f}%"
                tags = () if change is None else (("up",) if change >= 0 else ("down",))
                rows.append(((code, snapshot.names.get(code, code), self._format(converted), change_text, "", ""), tags))
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
            target_combo = {"a": self.combo_a, "b": self.combo_b, "c": self.combo_c}[target_side]
            target_combo.set(self.code_to_display[code])
            self.convert_from(self.active_side)

    def _destroy_jobs(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self.refreshing = False
        self.render_generation += 1
        self.action_fade_generation += 1
        self.after_jobs.cancel_all()
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
    ) -> None:
        super().__init__(master, bg=COLORS["bg"])
        self.service = service
        self.refresh_callback = refresh_callback
        self.timestamp_formatter = timestamp_formatter
        self.chart_timestamp_formatter = chart_timestamp_formatter
        self.mode = mode
        self.current_code = "USD" if mode == "fiat" else "BTC"
        self.current_days = 7
        self.fiat_var = tk.StringVar()
        self.reference_amount_var = tk.StringVar(value="1")
        self.reference_amount_value = 1.0
        self.status_var = tk.StringVar(value=(
            "全球货币周期趋势与多币种计价将在这里同步呈现。" if mode == "fiat" else
            "虚拟币批量行情、周期趋势与多币种计价将在这里同步呈现。"
        ))
        self.refresh_stamp_var = tk.StringVar(value="最新刷新：等待联网")
        self.market_search_var = tk.StringVar()
        self.price_var = tk.StringVar(value="—")
        self.change_var = tk.StringVar(value="—")
        self.range_var = tk.StringVar(value="最高 —   最低 —")
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
        body.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 25))
        watch_width = 365 if self.mode == "fiat" else 420
        body.grid_columnconfigure(0, weight=0, minsize=watch_width)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        watch = tk.Frame(body, bg=COLORS["card"], width=watch_width)
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
            watch_header, self.market_search_var, input_callback=lambda _value: self._render_watch(False),
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
        return self.fiat_display_to_code.get(self.fiat_var.get(), "CNY")

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
            display = f"{code} · {fiat_display_name(code, snapshot.names.get(code, code))}"
            values.append(display)
            self.fiat_display_to_code[display] = code
        self.fiat_values = values
        self.fiat_combo.set_values(values)
        asset_kind = "fiat" if self.mode == "fiat" else "crypto"
        asset_values = [
            f"{code} · {fiat_display_name(code, snapshot.names.get(code, code)) if self.mode == 'fiat' else crypto_display_name(code, snapshot.names.get(code, code))}"
            for code in snapshot.rates if snapshot.kinds.get(code) == asset_kind
        ]
        self.market_search_selector.set_values(asset_values)
        asset_codes = [code for code in snapshot.rates if snapshot.kinds.get(code) == asset_kind]
        if self.current_code not in asset_codes and asset_codes:
            self.current_code = asset_codes[0]
            name = snapshot.names.get(self.current_code, self.current_code)
            if self.mode == "crypto":
                name = crypto_display_name(self.current_code, name)
            self.coin_label.configure(text=f"{self.current_code} / {name}")
        target_fiat = old_fiat if old_fiat in fiats else ("CNY" if "CNY" in fiats else fiats[0] if fiats else "")
        for display, code in self.fiat_display_to_code.items():
            if code == target_fiat:
                self.fiat_combo.set(display)
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
                name = fiat_display_name(code, snapshot.names.get(code, code))
                rows.append(((code, name, self.compact(price), change_text), tags))
            else:
                name = crypto_display_name(code, snapshot.names.get(code, code))
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
        name = self.service.snapshot.names.get(self.current_code, self.current_code)
        if self.mode == "fiat":
            name = fiat_display_name(self.current_code, name)
        else:
            name = crypto_display_name(self.current_code, name)
        self.coin_label.configure(text=f"{self.current_code} / {name}")
        self.load_chart()

    def change_days(self, days: int) -> None:
        self.current_days = days
        self._highlight_days()
        self.load_chart()

    def change_quote(self, _value: str | None = None) -> None:
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
        "calculator": "计算器", "fiat": "货币", "fiat_market": "货币行情趋势",
        "crypto": "虚拟币", "market": "虚拟币行情趋势", "settings": "设置",
    }
    CLOSE_LABELS = {"exit": "关闭时退出应用", "minimize": "关闭时最小化"}
    MODE_LABELS = {"standard": "标准模式", "professional": "专业模式"}
    COPY_LABELS = {"number": "纯数字", "grouped": "带千位分隔符", "formula": "完整算式与结果"}

    def __init__(
        self,
        master: tk.Misc,
        settings: AppSettings,
        theme_callback: Callable[[str], None],
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
        exit_callback: Callable[[], None],
    ) -> None:
        super().__init__(master, bg=COLORS["bg"])
        self.settings = settings
        self.theme_callback = theme_callback
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
        self.exit_callback = exit_callback
        self.theme_var = tk.StringVar(value=settings.theme)
        self.timezone_var = tk.StringVar()
        self.timezone_clock_var = tk.StringVar()
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
        self.cache_size_var = tk.StringVar(value="正在统计…")
        self.app_path_var = tk.StringVar(value=str(portable_dir()))
        self.data_path_var = tk.StringVar(value=str(settings.resolved_data_dir()))
        self.zones = timezone_names()
        self.zone_infos = {zone: ZoneInfo(zone) for zone in self.zones}
        self.zone_display_to_name: dict[str, str] = {}
        self.zone_name_to_display: dict[str, str] = {}
        self.timezone_values = self._timezone_displays()
        self.timezone_options_hour = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%d%H")
        self.timezone_var.set(self.zone_name_to_display.get(settings.timezone, settings.timezone))
        self.theme_buttons: dict[str, AppButton] = {}
        self._build()
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
        self.settings_window = self.settings_canvas.create_window((0, 0), window=body, anchor="nw")
        body.grid_columnconfigure(0, weight=1, uniform="settings")
        body.grid_columnconfigure(1, weight=1, uniform="settings")
        body.bind("<Configure>", lambda _e: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")))
        self.settings_canvas.bind("<Configure>", lambda e: self.settings_canvas.itemconfigure(self.settings_window, width=e.width))

        appearance = self._card(body, "外观模式", "白天与黑夜模式均采用高对比度字体", 0, 0)
        theme_row = tk.Frame(appearance, bg=COLORS["card"])
        theme_row.pack(fill="x", padx=20, pady=(12, 18))
        for value, label in (("dark", "黑夜模式"), ("light", "白天模式")):
            button = AppButton(theme_row, ("●  " if value == self.settings.theme else "○  ") + label, lambda selected=value: self._set_theme(selected), "outline" if value == self.settings.theme else "ghost", 10)
            button.pack(side="left", expand=True, fill="x", padx=(0, 5) if value == "dark" else (5, 0), ipady=7)
            self.theme_buttons[value] = button

        timezone_card = self._card(body, "刷新显示时区", "UTC 偏移 · IANA 时区 · 所选时区当前时间", 0, 1)
        self.timezone_combo = SearchSelect(timezone_card, self.timezone_var, values=self.timezone_values, command=self._set_timezone, width=34, font_size=10, max_rows=8)
        self.timezone_combo.pack(fill="x", padx=20, pady=(12, 7))
        tk.Label(
            timezone_card, textvariable=self.timezone_clock_var, bg=COLORS["card_alt"], fg=COLORS["accent"],
            font=(FONT, 9, "bold"), padx=10, pady=6,
        ).pack(fill="x", padx=20, pady=(0, 18))

        refresh_card = self._card(body, "自动刷新", "货币与虚拟币使用独立分钟间隔", 1, 0)
        self._check(refresh_card, "启用自动刷新", self.auto_refresh_var, "auto_refresh_enabled")
        intervals = tk.Frame(refresh_card, bg=COLORS["card"])
        intervals.pack(fill="x", padx=20, pady=(5, 7))
        self._number_field(intervals, "货币", self.fiat_minutes_var, "fiat_refresh_minutes", 1, 1440, 60).pack(side="left", expand=True, fill="x", padx=(0, 5))
        self._number_field(intervals, "虚拟币", self.crypto_minutes_var, "crypto_refresh_minutes", 1, 1440, 10).pack(side="left", expand=True, fill="x", padx=(5, 0))
        self._check(refresh_card, "最小化后继续按设定时间刷新", self.refresh_minimized_var, "refresh_when_minimized", pady=(2, 16))

        startup = self._card(body, "启动与关闭", "默认页面、窗口记忆与关闭按钮行为", 1, 1)
        self._select_row(startup, "默认启动页面", self.startup_page_var, list(self.PAGE_LABELS.values()), lambda value: self._save("startup_page", self._reverse(self.PAGE_LABELS, value)))
        self._select_row(startup, "点击关闭按钮", self.close_action_var, list(self.CLOSE_LABELS.values()), lambda value: self._save("close_action", self._reverse(self.CLOSE_LABELS, value)))
        self._check(startup, "记住上次打开的页面", self.remember_page_var, "remember_last_page")
        self._check(startup, "记住窗口大小和位置", self.remember_geometry_var, "remember_window_geometry", pady=(2, 16))

        calculator = self._card(body, "计算器", "模式、角度、历史记录与复制格式", 2, 0, columnspan=2)
        calc_grid = tk.Frame(calculator, bg=COLORS["card"])
        calc_grid.pack(fill="x", padx=20, pady=(9, 4))
        for column in range(3):
            calc_grid.grid_columnconfigure(column, weight=1, uniform="calc_settings")
        self._select_cell(calc_grid, 0, "默认模式", self.default_mode_var, list(self.MODE_LABELS.values()), lambda value: self._save("default_calculator_mode", self._reverse(self.MODE_LABELS, value)))
        self._select_cell(calc_grid, 1, "角度模式", self.angle_mode_var, ["DEG", "RAD"], lambda value: self._save("calculator_angle_mode", value))
        self._select_cell(calc_grid, 2, "复制结果格式", self.copy_format_var, list(self.COPY_LABELS.values()), lambda value: self._save("copy_result_format", self._reverse(self.COPY_LABELS, value)))
        calc_checks = tk.Frame(calculator, bg=COLORS["card"])
        calc_checks.pack(fill="x", padx=20, pady=(4, 14))
        self._check(calc_checks, "记住上次标准/专业模式", self.remember_mode_var, "remember_calculator_mode", pack_side="left")
        self._check(calc_checks, "退出后保留历史记录", self.retain_history_var, "retain_history", pack_side="left")
        history_field = self._number_field(calc_checks, "历史记录条数", self.history_limit_var, "history_limit", 1, 200, 30)
        history_field.pack(side="right", fill="x")

        storage = self._card(body, "应用与缓存位置", "绿色版可整体迁移，也可单独指定数据目录", 3, 0, columnspan=2)
        storage_content = tk.Frame(storage, bg=COLORS["card"])
        storage_content.pack(fill="x")
        storage_content.grid_columnconfigure(0, weight=1)
        self._path_row(storage_content, 0, "应用文件夹", self.app_path_var, [("打开文件夹", self.open_app_callback), ("迁移应用与数据…", self.migrate_callback)])
        self._path_row(storage_content, 1, "缓存与行情数据文件夹", self.data_path_var, [("打开文件夹", self.open_data_callback), ("更改缓存目录…", self.choose_data_callback)])
        keep = tk.Checkbutton(storage_content, text="应用与相关数据全部放在同一文件夹", variable=self.keep_var, command=lambda: self.data_callback(self.keep_var.get()), **self._check_style())
        keep.grid(row=6, column=0, columnspan=3, sticky="w", padx=20, pady=(2, 16))

        cache = self._card(body, "缓存与设置管理", "限制缓存占用、清理缓存以及导入导出设置", 4, 0, columnspan=2)
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
        card.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", padx=(0, 7) if column == 0 and columnspan == 1 else (7, 0) if column == 1 else 0, pady=(0, 10))
        tk.Label(card, text=title, bg=COLORS["card"], fg=COLORS["text"], font=(FONT, 14, "bold")).pack(anchor="w", padx=20, pady=(17, 4))
        tk.Label(card, text=subtitle, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9)).pack(anchor="w", padx=20)
        return card

    @staticmethod
    def _reverse(mapping: dict[str, str], value: str) -> str:
        return next((key for key, label in mapping.items() if label == value), next(iter(mapping)))

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

    def _select_row(self, parent: tk.Misc, label: str, variable: tk.StringVar, values: list[str], callback: Callable[[str], None]) -> None:
        row = tk.Frame(parent, bg=COLORS["card"])
        row.pack(fill="x", padx=20, pady=(8, 2))
        tk.Label(row, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9, "bold"), width=12, anchor="w").pack(side="left")
        combo = SearchSelect(row, variable, values=values, command=callback, width=20, font_size=9, max_rows=7)
        combo.pack(side="left", expand=True, fill="x")

    def _select_cell(self, parent: tk.Misc, column: int, label: str, variable: tk.StringVar, values: list[str], callback: Callable[[str], None]) -> None:
        cell = tk.Frame(parent, bg=COLORS["card"])
        cell.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column == 0 else (6, 6) if column == 1 else (6, 0))
        tk.Label(cell, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 8, "bold")).pack(anchor="w")
        combo = SearchSelect(cell, variable, values=values, command=callback, width=18, font_size=9, max_rows=6)
        combo.pack(fill="x", pady=(4, 0))

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

    def _set_theme(self, theme: str) -> None:
        self.theme_var.set(theme)
        self.theme_callback(theme)
        for value, button in self.theme_buttons.items():
            selected = value == theme
            label = "黑夜模式" if value == "dark" else "白天模式"
            button.configure(text=("●  " if selected else "○  ") + label, bg=COLORS["card"], fg=COLORS["accent"] if selected else COLORS["muted"], activebackground=COLORS["accent_dark"] if selected else COLORS["card_alt"], activeforeground=COLORS["accent"] if selected else COLORS["muted"], font=(FONT, 10, "bold" if selected else "normal"))

    def _timezone_displays(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(ZoneInfo("UTC"))
        display_to_name: dict[str, str] = {}
        name_to_display: dict[str, str] = {}
        values: list[str] = []
        for zone in self.zones:
            local = now.astimezone(self.zone_infos[zone])
            offset = local.strftime("%z") or "+0000"
            display = f"UTC{offset[:3]}:{offset[3:]}  ·  {zone}"
            display_to_name[display] = zone
            name_to_display[zone] = display
            values.append(display)
        self.zone_display_to_name = display_to_name
        self.zone_name_to_display = name_to_display
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
                self.timezone_combo.set_values(self.timezone_values)
                self.timezone_combo.set(self.zone_name_to_display.get(self.settings.timezone, self.settings.timezone))
                self.timezone_options_hour = hour
        zone = self.settings.timezone if self.settings.timezone in self.zone_infos else "UTC"
        local = utc_now.astimezone(self.zone_infos.get(zone, ZoneInfo("UTC")))
        self.timezone_clock_var.set(f"所选时区当前时间：{local:%Y年%m月%d日 %H:%M:%S}")
        self.timezone_job = self.after(1000, self._update_timezone_time)

    def _set_timezone(self, display: str) -> None:
        zone = self.zone_display_to_name.get(display, display)
        if zone in self.zones:
            self.timezone_callback(zone)
            local = datetime.now(self.zone_infos[zone])
            self.timezone_clock_var.set(f"所选时区当前时间：{local:%Y年%m月%d日 %H:%M:%S}")

    def _destroy_jobs(self, event: tk.Event) -> None:
        if event.widget is self and self.timezone_job is not None:
            try:
                self.after_cancel(self.timezone_job)
            except tk.TclError:
                pass
            self.timezone_job = None

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
        COLORS.clear()
        COLORS.update(THEMES[self.settings.theme])
        self.root = tk.Tk()
        self.root.title("曜衡")
        geometry = self.settings.window_geometry if self.settings.remember_window_geometry else "1380x820"
        try:
            self.root.geometry(geometry)
        except tk.TclError:
            self.root.geometry("1380x820")
        self.root.minsize(1240, 740)
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
        self.service = RateService(self.settings.resolved_data_dir())
        self.service.set_cache_limit(self.settings.cache_limit_mb)
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
        self.auto_jobs: dict[str, str | None] = {"fiat": None, "crypto": None}
        self.geometry_job: str | None = None
        self.exiting = False
        self.persistence_warning_shown = False
        self.rate_results = TkResultBridge(self.root, self._finish_rates)
        self._styles()
        self._shell()
        start_page = self.settings.last_page if self.settings.remember_last_page else self.settings.startup_page
        self.show_page(start_page if start_page in self.pages else "calculator")
        self.root.bind("<Key>", self.on_key)
        self.root.bind("<Configure>", self._queue_geometry_save, add="+")
        self.root.bind_all("<Button-1>", self._dismiss_search_popup, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_request)
        if self.service.snapshot.rates:
            self.apply_snapshot(self.service.snapshot, True)
        self.startup_job = self.root.after(350, lambda: self.refresh_rates("all"))

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
            focuscolor="#000000", focusthickness=2, font=(FONT, 9, "bold"),
        )
        style.map("Treeview", background=[], foreground=[])
        style.configure(
            "Market.Treeview", background=COLORS["card"], fieldbackground=COLORS["card"],
            foreground=COLORS["text"], rowheight=31, borderwidth=0, relief="flat",
            bordercolor=COLORS["accent"], lightcolor=COLORS["accent"], darkcolor=COLORS["accent"],
            focuscolor="#000000", focusthickness=2, font=(FONT, 9, "bold"),
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
        self.sidebar.grid_rowconfigure(7, weight=1)
        brand = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(26, 32))
        self.logo_canvas = tk.Canvas(brand, width=42, height=42, bg=COLORS["sidebar"], bd=0, highlightthickness=0)
        self.logo_canvas.pack(side="left")
        self._draw_logo()
        names = tk.Frame(brand, bg=COLORS["sidebar"])
        names.pack(side="left", padx=10)
        tk.Label(names, text="曜衡", bg=COLORS["sidebar"], fg=COLORS["text"], font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(names, text="精准计算 · 实时金融", bg=COLORS["sidebar"], fg=COLORS["accent"], font=(FONT, 7, "bold")).pack(anchor="w")

        items = [
            ("calculator", "▦   计算器", 11, 22),
            ("fiat", "¥   货币", 11, 22),
            ("fiat_market", "　　¥⌁  货币行情趋势", 9, 22),
            ("crypto", "₿   虚拟币", 11, 22),
            ("market", "　　₿⌁  虚拟币行情趋势", 9, 22),
            ("settings", "⚙   设置", 11, 22),
        ]
        for row, (key, text, font_size, left_pad) in enumerate(items, start=1):
            button = tk.Button(
                self.sidebar, text=text, command=lambda page=key: self.show_page(page), anchor="w",
                bg=COLORS["sidebar"], fg=COLORS["muted"], activebackground=COLORS["card_alt"],
                activeforeground=COLORS["text"], relief="flat", bd=0, highlightthickness=0,
                font=(FONT, font_size, "bold"), padx=left_pad, pady=(10 if font_size < 11 else 13), cursor="hand2",
            )
            button.grid(row=row, column=0, sticky="ew", padx=10, pady=3)
            self.nav_buttons[key] = button
        footer = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        footer.grid(row=8, column=0, sticky="sew", padx=22, pady=20)
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
            footer, text="等待首次连接", bg=COLORS["sidebar"], fg=COLORS["muted"],
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
            content, self.toggle_history, self.refresh_history,
            initial_professional=calc_mode == "professional", mode_changed=self.calculator_mode_changed,
            angle_mode=self.settings.calculator_angle_mode, history_limit=self.settings.history_limit,
            initial_history=history, copy_result_format=self.settings.copy_result_format,
        )
        self.pages["fiat"] = DualConverterPage(
            content, self.service, "fiat", self.refresh_rates, self.format_timestamp,
            self.settings.favorite_fiats, self.settings.pinned_fiats, self.save_currency_preferences,
        )
        self.pages["fiat_market"] = MarketPage(
            content, self.service, self.refresh_rates, self.format_timestamp, "fiat",
            self.format_chart_timestamp,
        )
        self.pages["crypto"] = DualConverterPage(
            content, self.service, "crypto", self.refresh_rates, self.format_timestamp,
            self.settings.favorite_cryptos, self.settings.pinned_cryptos, self.save_currency_preferences,
        )
        self.pages["market"] = MarketPage(
            content, self.service, self.refresh_rates, self.format_timestamp, "crypto",
            self.format_chart_timestamp,
        )
        self.pages["settings"] = SettingsPage(
            content, self.settings, self.set_theme, self.set_timezone, self.set_keep_data_with_app,
            self.save_setting, self.choose_data_directory, self.migrate_application,
            self.service.cache_size_bytes, self.clear_cache, self.export_settings, self.import_settings,
            self.reset_settings, lambda: self.open_folder(portable_dir()),
            lambda: self.open_folder(self.settings.resolved_data_dir()), self.force_exit,
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

    def format_timestamp(self, value: str) -> str:
        try:
            stamp = datetime.fromisoformat(value)
            return stamp.astimezone(ZoneInfo(self.settings.timezone)).strftime("%Y年%m月%d日 %H:%M:%S")
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
        self.current_page = page
        if self.settings.remember_last_page:
            self.settings.last_page = page
            self._persist_settings(notify=False)
        self.pages[page].tkraise()
        for key, button in self.nav_buttons.items():
            active = key == page
            button.configure(bg=COLORS["accent_dark"] if active else COLORS["sidebar"], fg=COLORS["accent"] if active else COLORS["muted"])

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
        page_keys = ("fiat", "fiat_market") if section == "fiat" else ("crypto", "market") if section == "crypto" else ("fiat", "fiat_market", "crypto", "market")
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
                page_keys = ("fiat", "fiat_market") if section == "fiat" else ("crypto", "market") if section == "crypto" else ("fiat", "fiat_market", "crypto", "market")
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
            text, bg, fg = "当前网络连接成功！", COLORS["up_fill"], COLORS["up"]
        elif connected is False:
            text, bg, fg = "当前网络连接失败！", COLORS["down_fill"], COLORS["down"]
        elif connected == "partial":
            text, bg, fg = "部分数据更新成功", COLORS["accent_dark"], COLORS["accent"]
        else:
            text, bg, fg = "正在更新网络状态…", COLORS["accent_dark"], COLORS["accent"]
        self.network_button.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
        self.network_status.configure(text=text, bg=bg, fg=fg)
        self.network_time.configure(text=time_text)

    def apply_snapshot(self, snapshot: RateSnapshot, from_cache: bool, animated: bool = False, section: str = "all") -> None:
        keys = (
            ("fiat", "fiat_market", "crypto", "market") if section == "fiat" else
            ("crypto", "market") if section == "crypto" else
            ("fiat", "fiat_market", "crypto", "market")
        )
        for key in keys:
            page = self.pages[key]
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

    def clear_history(self) -> None:
        page = self.pages.get("calculator")
        if isinstance(page, CalculatorPage):
            page.model.history.clear()
            self.refresh_history()

    def use_history_result(self, result: str) -> None:
        page = self.pages.get("calculator")
        if isinstance(page, CalculatorPage):
            page.model.expression = result.replace("-", "−")
            page.model.just_evaluated = False
            page.refresh_display()

    def _persist_settings(self, notify: bool = True) -> bool:
        saved = self.settings_store.save(self.settings)
        if not saved and notify and not self.persistence_warning_shown:
            self.persistence_warning_shown = True
            messagebox.showerror(
                "设置未保存",
                "无法写入应用设置文件；本次更改仅在当前会话有效。请检查应用文件夹权限。",
            )
        return saved

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
        self._persist_settings()

    def calculator_mode_changed(self, mode: str) -> None:
        self.settings.last_calculator_mode = mode
        if self.settings.remember_calculator_mode:
            self._persist_settings(notify=False)

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
        if self.settings.close_action == "minimize" and not self.exiting:
            if self.history_open:
                self.toggle_history()
            self.root.iconify()
            return
        self.force_exit()

    def force_exit(self) -> None:
        if self.exiting:
            return
        self.exiting = True
        page = self.pages.get("calculator")
        if isinstance(page, CalculatorPage):
            self.settings.last_calculator_mode = "professional" if page.professional else "standard"
            self.settings.calculator_history = [list(item) for item in page.model.history[:self.settings.history_limit]] if self.settings.retain_history else []
        if self.settings.remember_window_geometry and not self.history_open and self.root.state() == "normal":
            self.settings.window_geometry = self.root.geometry()
        self._persist_settings(notify=False)
        self.rate_results.close()
        self.loading_rates = False
        self.active_rate_section = None
        self.pending_rate_section = None
        for attr in ("startup_job", "geometry_job"):
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
            messagebox.showinfo("导入完成", "设置已导入，重新启动曜衡后全部生效。")
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            messagebox.showerror("导入失败", f"该文件不是有效的曜衡设置：\n{exc}")

    def reset_settings(self) -> None:
        if not messagebox.askyesno("恢复默认设置", "确定恢复所有默认设置吗？\n收藏、置顶和计算历史也会重置。"):
            return
        if self.settings_store.save(AppSettings()):
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

    def set_theme(self, theme: str) -> None:
        if theme not in THEMES or theme == self.settings.theme:
            return
        old = dict(COLORS)
        self.settings.theme = theme
        self._persist_settings()
        COLORS.clear()
        COLORS.update(THEMES[theme])
        keys_by_color: dict[str, list[str]] = {}
        for key, value in old.items():
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
            if len(candidates) == 1:
                return candidates[0]
            # Light mode intentionally shares white between cards, the sidebar,
            # accent-button text and chart tooltips. Widget context disambiguates
            # those roles when returning to the dark palette.
            if option in {"foreground", "activeforeground", "selectforeground", "disabledforeground"}:
                if "on_accent" in candidates:
                    return "on_accent"
            if hasattr(self, "sidebar") and belongs_to(widget, self.sidebar) and "sidebar" in candidates:
                return "sidebar"
            if "card" in candidates:
                return "card"
            return candidates[0]

        def recolor(widget: tk.Misc) -> None:
            for option in ("background", "foreground", "activebackground", "activeforeground", "insertbackground", "highlightbackground", "highlightcolor", "selectbackground", "selectforeground", "readonlybackground", "selectcolor", "disabledforeground"):
                try:
                    current = str(widget.cget(option))
                    key = theme_key(widget, option, current)
                    if key and key in COLORS:
                        widget.configure(**{option: COLORS[key]})
                except (tk.TclError, TypeError):
                    pass
            for child in widget.winfo_children():
                recolor(child)

        self.root.configure(bg=COLORS["bg"])
        recolor(self.root)
        self._styles()
        self._draw_logo()

        def retheme_selects(widget: tk.Misc) -> None:
            if isinstance(widget, SearchSelect):
                widget.apply_theme()
            for child in widget.winfo_children():
                retheme_selects(child)

        retheme_selects(self.root)
        for key in ("fiat", "crypto"):
            page = self.pages.get(key)
            if isinstance(page, DualConverterPage):
                page.table.tag_configure("up", background=COLORS["up_row"], foreground=COLORS["text"])
                page.table.tag_configure("down", background=COLORS["down_row"], foreground=COLORS["text"])
                page._show_row_actions()
        for key in ("fiat_market", "market"):
            market = self.pages.get(key)
            if isinstance(market, MarketPage):
                market.watch_table.tag_configure("up", background=COLORS["up_row"], foreground=COLORS["text"])
                market.watch_table.tag_configure("down", background=COLORS["down_row"], foreground=COLORS["text"])
                market.watch_table.tag_configure("flat", background=COLORS["card_alt"], foreground=COLORS["text"])
                market.chart.configure(bg=COLORS["card"])
                market.chart.redraw()
                market._highlight_days()

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
