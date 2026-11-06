"""Forge's look (app/forge/ui/look.py): AirGrabber's own accent tokens as a value, read when asked,
so whatever accent Settings has; readable in both looks with every accent; copper is gone; the shared
widgets (dialog_kit's buttons, Tabview's underline, popup_menu) painting with the look when
given it. Hidden Tk root for those."""

from __future__ import annotations

import re
from dataclasses import fields

import customtkinter as ctk

from app.forge.ui.look import ForgeLook, app_look, mix
from app.forge.ui.widgets.chips import option_style
from app.models import Accent
from app.ui import palettes, theme
from app.ui.dialog_kit import primary_button, secondary_button
from app.ui.missing_control import PopupMenu, popup_menu
from app.ui.palettes import ACCENT_TOKENS, PALETTES
from app.ui.widgets import Tabview
from tests.forge.base import ForgeTestCase
from tests.test_accent import contrast
from tests.test_quick_setup import TkTestCase

HEX = re.compile(r"#[0-9A-F]{6}")
LIGHT, DARK = 0, 1


class AppLookTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(theme.apply_accent, Accent.SLATE)

    def test_every_token_is_theme_s_now(self) -> None:
        self.assertEqual([f.name for f in fields(ForgeLook)], list(ACCENT_TOKENS))
        for accent in Accent:
            theme.apply_accent(accent)
            look = app_look()
            for name in ACCENT_TOKENS:
                with self.subTest(accent=accent, name=name):
                    self.assertEqual(getattr(look, name), getattr(theme, name))

    def test_read_when_asked_after_the_accent_is_applied(self) -> None:
        theme.apply_accent(Accent.SLATE)
        slate = app_look()
        theme.apply_accent(Accent.TEAL)
        self.assertNotEqual(app_look().ACCENT, slate.ACCENT)
        self.assertEqual(app_look().ACCENT, (PALETTES[Accent.TEAL].accent,) * 2)

    def test_copper_is_gone(self) -> None:
        self.assertFalse(hasattr(palettes, "FORGE"))
        self.assertFalse(hasattr(palettes, "FORGE_ACCENT"))
        self.assertNotIn("Copper", [palette.name for palette in PALETTES.values()])

    def test_readable_with_every_accent(self) -> None:
        for accent in Accent:
            theme.apply_accent(accent)
            look = app_look()
            for half in (LIGHT, DARK):
                pick = {
                    name: theme.pick(getattr(look, name), dark=bool(half)) for name in ACCENT_TOKENS
                }
                text = theme.pick(theme.TEXT, dark=bool(half))
                muted = theme.pick(theme.TEXT_MUTED, dark=bool(half))
                with self.subTest(accent=accent, half=half):
                    self.assertGreaterEqual(contrast(theme.ON_COLOR, pick["ACCENT"]), 4.5)
                    self.assertGreaterEqual(contrast(text, pick["SURFACE"]), 4.5)
                    self.assertGreaterEqual(contrast(muted, pick["SURFACE"]), 4.5)
                    self.assertGreaterEqual(contrast(pick["LINK"], pick["SURFACE"]), 3.0)

    def test_the_needs_you_badge_stays_amber_and_readable(self) -> None:
        for half in (LIGHT, DARK):
            amber, text = theme.WARNING[half], theme.ON_WARNING[half]
            with self.subTest(half=half):
                self.assertGreaterEqual(contrast(text, amber), 4.5)

    def test_extras_and_tidy_up_stay_readable_with_every_accent(self) -> None:
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())
        for accent in Accent:
            theme.apply_accent(accent)
            look = app_look()
            for mode in ("light", "dark"):
                ctk.set_appearance_mode(mode)
                on = option_style(look, on=True, hover=False)
                off = option_style(look, on=False, hover=False)
                with self.subTest(accent=accent, mode=mode):
                    self.assertGreaterEqual(contrast(on.text, on.surface), 4.5)
                    self.assertGreaterEqual(contrast(off.text, off.surface), 4.5)
                    self.assertGreaterEqual(contrast(on.light, on.surface), 3.0)
            # The Forge button while a run goes: white on its dark shade and on the bar.
            self.assertGreaterEqual(contrast(theme.ON_COLOR, look.STACK_FAR[1]), 4.5, accent)

    def test_mix_comes_from_tokens(self) -> None:
        self.assertEqual(mix("#000000", "#FFFFFF", 0.5), "#808080")
        self.assertRegex(mix(theme.SURFACE[1], theme.BG[1], 0.45), HEX)


class SharedWidgetsTests(TkTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.look = app_look()

    def test_dialog_buttons(self) -> None:
        primary = primary_button(self.root, "Go", lambda: None, look=self.look)
        self.assertEqual(tuple(primary.cget("fg_color")), self.look.ACCENT)
        self.assertEqual(tuple(primary.cget("hover_color")), self.look.ACCENT_HOVER)
        secondary = secondary_button(self.root, "Back", lambda: None, look=self.look)
        self.assertEqual(tuple(secondary.cget("border_color")), self.look.EDGE)
        self.assertEqual(tuple(secondary.cget("hover_color")), self.look.SURFACE)
        # Without a look: AirGrabber's own accent, as before.
        plain = primary_button(self.root, "Go", lambda: None)
        self.assertEqual(tuple(plain.cget("fg_color")), theme.ACCENT)

    def test_underline_tabs(self) -> None:
        underline = Tabview(self.root, look="underline", colors=self.look)
        underline.add("Inbox")
        bar = underline.underline
        assert bar is not None
        self.assertEqual(tuple(bar._marks["Inbox"].cget("fg_color")), self.look.ACCENT)
        self.assertEqual(tuple(underline.cget("fg_color")), self.look.BG)
        underline.destroy()  # before the root, as tests/test_accent.py

    def test_popup_menu(self) -> None:
        menu = popup_menu(self.root, [("Open", lambda: None)], look=self.look)
        assert menu is not None
        self.addCleanup(menu.close)
        self.assertIsInstance(menu, PopupMenu)
        self.assertEqual(tuple(menu.buttons[0].cget("hover_color")), self.look.MENU_HOVER)
        self.assertEqual(menu._top.cget("bg").upper(), theme.pick(self.look.EDGE).upper())
        self.assertIsInstance(menu.buttons[0], ctk.CTkButton)
