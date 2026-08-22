from __future__ import annotations

import tkinter as tk
import unittest
from types import SimpleNamespace

from app_ui import SettingsPage, ThemePalettePicker
from theme_catalog import THEME_LABELS, THEME_SOURCES, THEMES


class ThemeCatalogV321Tests(unittest.TestCase):
    def test_every_theme_has_a_public_palette_source(self):
        self.assertEqual(set(THEMES), set(THEME_LABELS))
        self.assertEqual(set(THEMES), set(THEME_SOURCES))
        for name, (source, url) in THEME_SOURCES.items():
            with self.subTest(theme=name):
                self.assertTrue(source)
                self.assertTrue(url.startswith("https://"))

    def test_gallery_palettes_are_visually_distinct_and_context_readable(self):
        self.assertGreaterEqual(len({palette["bg"] for palette in THEMES.values()}), 20)
        self.assertGreaterEqual(len({palette["accent"] for palette in THEMES.values()}), 20)
        for name, palette in THEMES.items():
            with self.subTest(theme=name):
                self.assertIn(palette["text"], {"#111111", "#FFFFFF"})
                from theme_catalog import contrast_ratio
                self.assertGreaterEqual(contrast_ratio(palette["text"], palette["card"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["on_accent"], palette["accent"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["selection_text"], palette["selection"]), 4.5)


class ThemePalettePickerTkTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")

    def tearDown(self) -> None:
        root = getattr(self, "root", None)
        if root is not None:
            root.destroy()

    def test_gallery_expands_collapses_and_selects_real_swatch_row(self):
        selected: list[str] = []
        picker = ThemePalettePicker(self.root, "dark", selected.append)
        picker.pack(fill="x")
        self.root.update_idletasks()

        self.assertFalse(picker.expanded)
        self.assertEqual(picker.gallery.winfo_manager(), "")
        self.assertEqual(len(picker.rows), len(THEMES))
        row, swatch, label, _source, marker = picker.rows["dark"]
        self.assertIsInstance(swatch, tk.Canvas)
        self.assertEqual(label.cget("text"), THEME_LABELS["dark"])
        self.assertEqual(marker.cget("text"), "当前")

        picker.toggle()
        self.root.update_idletasks()
        self.assertTrue(picker.expanded)
        self.assertEqual(picker.gallery.winfo_manager(), "pack")
        self.assertEqual(int(row.grid_info()["column"]), 0)

        next_theme = tuple(THEMES)[1]
        picker.choose(next_theme)
        self.assertEqual(selected, [next_theme])
        self.assertEqual(picker.theme, next_theme)
        self.assertEqual(picker.rows[next_theme][4].cget("text"), "当前")

        picker.toggle()
        self.assertFalse(picker.expanded)
        self.assertEqual(picker.gallery.winfo_manager(), "")

    def test_header_swatch_contains_five_palette_segments_and_border(self):
        picker = ThemePalettePicker(self.root, "dark", lambda _theme: None)
        self.assertEqual(len(picker.header_swatch.find_all()), 6)


class SequentialThemeButtonTests(unittest.TestCase):
    def test_next_theme_follows_catalog_order_and_wraps(self):
        order = tuple(THEMES)
        page = SettingsPage.__new__(SettingsPage)
        page.settings = SimpleNamespace(theme=order[-1])
        chosen: list[str] = []
        page._set_theme = chosen.append

        SettingsPage._next_theme(page)

        self.assertEqual(chosen, [order[0]])
        self.assertIn(THEME_LABELS[order[0]], SettingsPage._theme_cycle_text(order[0]))
        self.assertIn(THEME_LABELS[order[1]], SettingsPage._theme_cycle_text(order[0]))


if __name__ == "__main__":
    unittest.main()
