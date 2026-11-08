"""Settings › Looks › Text size: every font a little larger, from the next start."""

from __future__ import annotations

import unittest

import customtkinter as ctk

from app.models import Settings, TextSize
from app.ui import theme


class TextSizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ctk.CTk()
        self.addCleanup(self.root.destroy)
        self.addCleanup(theme.apply_text_size, "normal")

    def test_fonts_grow_with_the_setting(self) -> None:
        theme.apply_text_size("normal")
        self.assertEqual(theme.font(10).cget("size"), 10)
        theme.apply_text_size("larger")
        self.assertEqual(theme.font(10).cget("size"), 12)
        self.assertEqual(theme.current_text_size(), "larger")
        theme.apply_text_size("huge")  # one this version doesn't know
        self.assertEqual(theme.current_text_size(), "normal")

    def test_the_setting_defaults_to_normal(self) -> None:
        self.assertIs(Settings().text_size, TextSize.NORMAL)
        self.assertIs(Settings(text_size="gigantic").text_size, TextSize.NORMAL)
