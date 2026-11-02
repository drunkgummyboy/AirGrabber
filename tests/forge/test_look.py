"""The Forge window's copper look (app/forge/ui/look.py, palettes.FORGE): every token in both
looks, derived from Slate (no hand-picked colours), readable, AirGrabber's own accent and its
ten palettes untouched; the shared widgets (dialog_kit's buttons, Tabview's pills and
underline, popup_menu) painting in copper when given the look. Hidden Tk root for those."""

from __future__ import annotations

import colorsys
import re
from dataclasses import fields

import customtkinter as ctk

from app.forge.ui.look import ForgeLook, forge_look
from app.models import Accent
from app.ui import theme
from app.ui.dialog_kit import primary_button, secondary_button
from app.ui.missing_control import PopupMenu, popup_menu
from app.ui.palettes import (
    ACCENT_TOKENS,
    FORGE,
    FORGE_ACCENT,
    PALETTES,
    Shades,
    derived_palette,
    palette_tokens,
    shifted,
    to_hex,
    tokens,
)
from app.ui.widgets import Tabview
from tests.forge.base import ForgeTestCase
from tests.test_accent import SLATE_BEFORE, contrast, luminance
from tests.test_quick_setup import TkTestCase

HEX = re.compile(r"#[0-9A-F]{6}")
LIGHT, DARK = 0, 1
# The mock-up's tint for the dark "Forge" brand text (INTEGRATION D1): a reference only.
MOCKUP_BRAND = "#F0B080"


def _hls_hue(color: str) -> float:
    hexed = to_hex(color)
    red, green, blue = (int(hexed[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(red, green, blue)[0]


class ForgeLookTokenTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(theme.apply_accent, Accent.SLATE)
        self.look = forge_look()

    def test_every_token_in_both_looks(self) -> None:
        self.assertEqual([f.name for f in fields(ForgeLook)], list(ACCENT_TOKENS))
        for name in ACCENT_TOKENS:
            pair = getattr(self.look, name)
            with self.subTest(name=name):
                self.assertEqual(len(pair), 2)
                for color in pair:
                    self.assertRegex(color, HEX)
        self.assertIs(forge_look(), self.look)  # computed once

    def test_the_accent_is_d1s_copper(self) -> None:
        self.assertEqual(FORGE_ACCENT, "#A0612B")
        self.assertEqual(self.look.ACCENT, ("#A0612B", "#A0612B"))
        self.assertEqual(self.look.ACCENT_HOVER, (FORGE.hover, FORGE.hover))
        self.assertEqual(FORGE.name, "Copper")

    def test_derived_from_slate_not_hand_picked(self) -> None:
        self.assertEqual(FORGE, derived_palette("Copper", "#A0612B"))
        slate = PALETTES[Accent.SLATE]
        self.assertEqual(FORGE.hover, shifted(slate.hover, slate.accent, FORGE_ACCENT))
        for look in ("light", "dark"):
            own, anchors = getattr(FORGE, look), getattr(slate, look)
            for field in fields(Shades):
                moved = shifted(getattr(anchors, field.name), slate.accent, FORGE_ACCENT)
                with self.subTest(look=look, shade=field.name):
                    if field.name != "link":
                        self.assertEqual(getattr(own, field.name), moved)
                        continue
                    # The link: the moved one, only its lightness corrected until it reads as
                    # well as Slate's on the same grounds.
                    self.assertAlmostEqual(_hls_hue(own.link), _hls_hue(moved), delta=0.01)
                    darker = look == "light"
                    if darker:
                        self.assertLessEqual(luminance(own.link), luminance(moved))
                    else:
                        self.assertGreaterEqual(luminance(own.link), luminance(moved))
                    for ground in ("bg", "surface", "chip", "todaybg"):
                        self.assertGreaterEqual(
                            contrast(own.link, getattr(own, ground)),
                            contrast(anchors.link, getattr(anchors, ground)),
                            ground,
                        )
        self.assertEqual(
            {name: getattr(self.look, name) for name in ACCENT_TOKENS},
            palette_tokens(FORGE, SLATE_BEFORE),
        )

    def test_a_warm_tint(self) -> None:
        """Copper, not slate: the tinted backgrounds lean red / orange."""
        for name in ("BG", "CHIP", "TODAY_BG", "ROW_SELECTED", "EDGE", "LINK"):
            for half in (LIGHT, DARK):
                color = to_hex(getattr(self.look, name)[half])
                red, blue = int(color[1:3], 16), int(color[5:7], 16)
                with self.subTest(name=name, half=half):
                    self.assertGreater(red, blue)

    def test_the_dark_brand_text_is_a_light_copper_tint(self) -> None:
        brand = self.look.BRAND[DARK]
        self.assertGreater(luminance(brand), 0.35)  # light over a dark picture, like #F0B080
        self.assertAlmostEqual(_hls_hue(brand), _hls_hue(MOCKUP_BRAND), delta=0.03)
        self.assertGreaterEqual(contrast(brand, self.look.BG[DARK]), 4.5)
        self.assertEqual(self.look.BRAND[LIGHT], self.look.LINK[LIGHT])  # as Slate's BRAND

    def test_light_text_readable(self) -> None:
        """As tests/test_accent.py checks every palette, in the light look (WCAG AA, 4.5:1)."""
        light = {name: theme.pick(getattr(self.look, name), dark=False) for name in ACCENT_TOKENS}
        for name in ("TEXT", "TEXT_MUTED", "BUTTON_TEXT"):
            light[name] = theme.pick(getattr(theme, name), dark=False)
        for background in ("BG", "SURFACE", "TAB_BG", "CELL_BG", "TODAY_BG"):
            for text in ("TEXT", "TEXT_MUTED", "LINK", "BUTTON_TEXT"):
                with self.subTest(text=text, on=background):
                    self.assertGreaterEqual(contrast(light[text], light[background]), 4.5)
        for fill in ("SELECTED", "SELECTED_HOVER", "CHIP", "CHIP_HOVER", "HOVER", "ROW_SELECTED"):
            with self.subTest(fill=fill):
                self.assertGreaterEqual(contrast(light["BUTTON_TEXT"], light[fill]), 4.5)

    def test_dark_text_readable(self) -> None:
        dark = {name: theme.pick(getattr(self.look, name), dark=True) for name in ACCENT_TOKENS}
        for name in ("TEXT", "TEXT_MUTED", "BUTTON_TEXT"):
            dark[name] = theme.pick(getattr(theme, name), dark=True)
        for background in ("BG", "SURFACE", "TAB_BG"):
            for text in ("TEXT", "TEXT_MUTED", "LINK", "BUTTON_TEXT"):
                with self.subTest(text=text, on=background):
                    self.assertGreaterEqual(contrast(dark[text], dark[background]), 4.5)
        for fill in ("CHIP", "CHIP_HOVER", "ROW_SELECTED"):
            with self.subTest(fill=fill):
                self.assertGreaterEqual(contrast(dark["BUTTON_TEXT"], dark[fill]), 4.5)

    def test_the_needs_you_badge_and_buttons_readable(self) -> None:
        """White (ON_COLOR) on the copper ACCENT: the badge F-22 draws, the primary buttons and
        the chosen pill, in both looks."""
        for half in (LIGHT, DARK):
            with self.subTest(half=half):
                self.assertGreaterEqual(contrast(theme.ON_COLOR, self.look.ACCENT[half]), 4.5)

    def test_airgrabbers_accent_untouched(self) -> None:
        before = {name: getattr(theme, name) for name in ACCENT_TOKENS}
        forge_look.cache_clear()
        self.addCleanup(forge_look.cache_clear)
        fresh = forge_look()
        self.assertEqual({name: getattr(theme, name) for name in ACCENT_TOKENS}, before)
        self.assertIs(theme.current_accent(), Accent.SLATE)
        # Whatever accent AirGrabber runs with, the Forge window stays copper.
        theme.apply_accent(Accent.TEAL)
        forge_look.cache_clear()
        self.assertEqual(forge_look(), fresh)
        self.assertEqual(dict(theme.SLATE_TOKENS), SLATE_BEFORE)

    def test_palettes_unchanged(self) -> None:
        self.assertEqual(list(PALETTES), list(Accent))
        self.assertEqual(len(PALETTES), 10)
        self.assertNotIn(FORGE, PALETTES.values())
        self.assertNotIn("Copper", [palette.name for palette in PALETTES.values()])
        # tokens() still gives what it did (palette_tokens is the same calculation).
        for accent in Accent:
            with self.subTest(accent=accent):
                if accent is not Accent.SLATE:
                    self.assertEqual(
                        tokens(accent, SLATE_BEFORE), palette_tokens(PALETTES[accent], SLATE_BEFORE)
                    )
        self.assertEqual(tokens(Accent.SLATE, SLATE_BEFORE), SLATE_BEFORE)

    def test_slate_tokens_read_only(self) -> None:
        with self.assertRaises(TypeError):
            theme.SLATE_TOKENS["ACCENT"] = ("#000000", "#000000")  # type: ignore[index]


class SharedWidgetsInCopperTests(TkTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.look = forge_look()

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

    def test_pills_and_underline(self) -> None:
        pills = Tabview(self.root, look="pills", colors=self.look)
        for name in ("Inbox", "History"):
            pills.add(name)
        row = pills.pills
        assert row is not None
        chosen = row.buttons["Inbox"]
        other = row.buttons["History"]
        self.assertEqual(tuple(chosen.cget("fg_color")), self.look.ACCENT)
        self.assertEqual(tuple(other.cget("fg_color")), self.look.CHIP)
        self.assertEqual(tuple(other.cget("hover_color")), self.look.CHIP_HOVER)
        self.assertEqual(tuple(pills.cget("fg_color")), self.look.BG)
        pills.set("History")
        self.assertEqual(tuple(row.buttons["History"].cget("fg_color")), self.look.ACCENT)
        underline = Tabview(self.root, look="underline", colors=self.look)
        underline.add("Inbox")
        bar = underline.underline
        assert bar is not None
        self.assertEqual(tuple(bar._marks["Inbox"].cget("fg_color")), self.look.ACCENT)
        plain = Tabview(self.root, look="pills")
        plain.add("Calendar")
        assert plain.pills is not None
        self.assertEqual(tuple(plain.pills.buttons["Calendar"].cget("fg_color")), theme.ACCENT)
        for tabs in (pills, underline, plain):  # before the root, as tests/test_accent.py does
            tabs.destroy()

    def test_popup_menu(self) -> None:
        menu = popup_menu(self.root, [("Open", lambda: None)], look=self.look)
        assert menu is not None
        self.addCleanup(menu.close)
        self.assertIsInstance(menu, PopupMenu)
        self.assertEqual(tuple(menu.buttons[0].cget("hover_color")), self.look.MENU_HOVER)
        self.assertEqual(menu._top.cget("bg").upper(), theme.pick(self.look.EDGE).upper())
        self.assertIsInstance(menu.buttons[0], ctk.CTkButton)
