"""The light / dark palette (app/ui/theme.py): every colour a (light, dark) pair whose dark half is
the look AirGrabber always had, `pick`, light text readable on light backgrounds (WCAG contrast),
the choice buttons' colours, applying a mode, `on_mode_change`, and the tab view whose pages
keep the colour pair."""

from __future__ import annotations

import re
import tkinter as tk
import unittest
from unittest import mock

import customtkinter as ctk

from app.models import ThemeMode
from app.ui import theme
from app.ui.widgets import Tabview

# The dark look before there was a light one: these values must never change.
DARK_BEFORE = {
    "BG": "#161616",
    "SURFACE": "#242424",
    "TAB_BG": "#1C1C1C",
    "EDGE": "#4E6A85",
    "ACCENT": "#4E6A85",
    "ACCENT_HOVER": "#427839",
    "TEXT": "#FFFFFF",
    "TEXT_MUTED": "#A4B2C6",
    "TEXT_DIM": "gray60",
    "DANGER": "#E74C3C",
    "SUCCESS": "#2FA572",
    "LINK": "#5D8AA8",
    "DANGER_BUTTON": "#C0392B",
    "DANGER_BUTTON_HOVER": "#922B21",
    "PLACEHOLDER": "gray20",
    "CELL_BG": "#121620",
    "CELL_BORDER": "#1C222E",
    "TODAY_BG": "#182133",
    "SUGGESTION_BG": "#14121A",
    "SUGGESTION_BORDER": "#2A2438",
    "GRABBED": "#2E7D4F",
    "NOT_AIRED": "gray25",
    "SECTION_HOT": "#F39C12",
    "SECTION_NEW": "#2ECC71",
    "WARNING": "#F39C12",
    "PANEL_EDGE": "gray20",
    "CHIP": "gray15",
    "CHIP_HOVER": "gray25",
    "PENDING": "#F1C40F",
    "ROW_SELECTED": "#2A3A4D",
    "TEXT_FAINT": "gray40",
    # Colours that were CTk's own defaults or raw values in the widgets before.
    "BUTTON_TEXT": "#DCE4EE",  # CTkButton's text
    "ON_ACCENT": "#DCE4EE",
    "HOVER": "#427839",  # ACCENT_HOVER
    "MENU_HOVER": "#4E6A85",  # ACCENT
    "SELECTED": "#4E6A85",  # ACCENT
    "SELECTED_HOVER": "#427839",  # ACCENT_HOVER
    "SCROLLBAR": "#242424",  # SURFACE
    "SUGGESTION_TEXT": "gray40",
    "SUGGESTION_BUTTON_EDGE": "gray30",
    "SUGGESTION_BUTTON_TEXT": "gray60",
    "NOT_AIRED_TEXT": "gray60",  # CTkButton's disabled text
    "TEXT_QUIET": "gray50",
    # The calendar's stack cards (2.0.5): the cards peeking out behind them.
    "STACK_NEAR": "#212830",
    "STACK_NEAR_EDGE": "#46607A",
    "STACK_FAR": "#1E2329",
    "STACK_FAR_EDGE": "#3A4E63",
}

HEX = re.compile(r"#[0-9A-Fa-f]{6}")


def pairs() -> dict[str, theme.Pair]:
    found = {}
    for name in dir(theme):
        value = getattr(theme, name)
        if name.isupper() and isinstance(value, tuple) and len(value) == 2:
            found[name] = value
    return found


def luminance(color: str) -> float:
    """WCAG relative luminance of "#rrggbb"."""

    def channel(index: int) -> float:
        value = int(color[1 + 2 * index : 3 + 2 * index], 16) / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(0) + 0.7152 * channel(1) + 0.0722 * channel(2)


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def light(color: theme.Color) -> str:
    return theme.pick(color, dark=False)


class PaletteTests(unittest.TestCase):
    def test_dark_is_the_look_it_always_was(self) -> None:
        for name, dark in DARK_BEFORE.items():
            with self.subTest(name=name):
                self.assertEqual(theme.pick(getattr(theme, name), dark=True), dark)

    def test_every_pair_is_known_and_light_values_are_hex(self) -> None:
        for name, (light_color, _dark) in pairs().items():
            with self.subTest(name=name):
                self.assertIn(name, DARK_BEFORE)
                self.assertRegex(light_color, HEX)

    def test_brand_colours_stay_the_same_in_both_modes(self) -> None:
        self.assertEqual(theme.IMDB, "#F5C518")
        self.assertEqual(theme.ON_COLOR, "#FFFFFF")

    def test_accent_keeps_its_slate_blue(self) -> None:
        self.assertEqual(theme.ACCENT, ("#4E6A85", "#4E6A85"))


class PickTests(unittest.TestCase):
    def test_one_colour_for_both_modes_passes_through(self) -> None:
        self.assertEqual(theme.pick("#F5C518", dark=True), "#F5C518")
        self.assertEqual(theme.pick("transparent", dark=False), "transparent")

    def test_a_pair_gives_the_modes_colour(self) -> None:
        self.assertEqual(theme.pick(("#111111", "#222222"), dark=False), "#111111")
        self.assertEqual(theme.pick(("#111111", "#222222"), dark=True), "#222222")

    def test_follows_customtkinters_mode(self) -> None:
        for mode, expected in (("Light", "#111111"), ("Dark", "#222222")):
            with self.subTest(mode=mode):
                with mock.patch.object(ctk, "get_appearance_mode", return_value=mode):
                    self.assertEqual(theme.is_dark(), mode == "Dark")
                    self.assertEqual(theme.pick(("#111111", "#222222")), expected)


class ContrastTests(unittest.TestCase):
    """Light mode: body text at least 4.5:1 on what it sits on (WCAG AA)."""

    BACKGROUNDS = (theme.BG, theme.SURFACE, theme.TAB_BG, theme.CELL_BG, theme.TODAY_BG)
    TEXTS = (
        theme.TEXT,
        theme.TEXT_MUTED,
        theme.TEXT_DIM,
        theme.ACCENT,
        theme.LINK,
        theme.DANGER,
        theme.SUCCESS,
        theme.WARNING,  # amber: missing episodes, "No torrent program found"
        theme.SECTION_NEW,
        theme.PENDING,
        theme.BUTTON_TEXT,
        theme.TEXT_QUIET,
    )

    def assert_readable(self, text: theme.Color, background: theme.Color) -> None:
        ratio = contrast(light(text), light(background))
        self.assertGreaterEqual(ratio, 4.5, f"{light(text)} on {light(background)}: {ratio:.2f}")

    def test_text_on_the_backgrounds(self) -> None:
        for background in self.BACKGROUNDS:
            for text in self.TEXTS:
                with self.subTest(text=light(text), background=light(background)):
                    self.assert_readable(text, background)

    def test_text_on_filled_buttons_and_badges(self) -> None:
        for fill in (
            theme.ACCENT,
            theme.ACCENT_HOVER,
            theme.GRABBED,
            theme.DANGER_BUTTON,
            theme.SUCCESS,
        ):
            with self.subTest(fill=light(fill)):
                self.assert_readable(theme.ON_ACCENT, fill)
                self.assert_readable(theme.ON_COLOR, fill)

    def test_text_on_quiet_buttons_and_selections(self) -> None:
        for fill in (
            theme.SELECTED,
            theme.SELECTED_HOVER,
            theme.CHIP,
            theme.CHIP_HOVER,
            theme.HOVER,
            theme.MENU_HOVER,
        ):
            with self.subTest(fill=light(fill)):
                self.assert_readable(theme.BUTTON_TEXT, fill)
        for text in (theme.TEXT, theme.TEXT_MUTED):
            self.assert_readable(text, theme.ROW_SELECTED)  # the search window's chosen row
        self.assert_readable(theme.NOT_AIRED_TEXT, theme.NOT_AIRED)

    def test_suggestions(self) -> None:
        for text in (theme.TEXT_DIM, theme.TEXT_MUTED, theme.SUGGESTION_TEXT):
            self.assert_readable(text, theme.SUGGESTION_BG)
        self.assert_readable(theme.SUGGESTION_BUTTON_TEXT, theme.SUGGESTION_BG)

    def test_borders_stand_out_from_the_cards(self) -> None:
        # WCAG's 3:1 for the edges of controls (inputs, outlined buttons).
        self.assertGreaterEqual(contrast(light(theme.EDGE), light(theme.SURFACE)), 3.0)


class ChoiceColorTests(unittest.TestCase):
    def test_chosen_is_the_accent_with_light_text(self) -> None:
        self.assertEqual(
            theme.choice_colors(True),
            {"fg_color": theme.ACCENT, "hover_color": theme.ACCENT_HOVER, "text_color": theme.ON_ACCENT},
        )

    def test_others_are_quiet_chips(self) -> None:
        self.assertEqual(
            theme.choice_colors(False),
            {"fg_color": theme.CHIP, "hover_color": theme.CHIP_HOVER, "text_color": theme.BUTTON_TEXT},
        )


class ApplyAppearanceTests(unittest.TestCase):
    def test_each_choice_sets_customtkinters_mode(self) -> None:
        expected = {ThemeMode.SYSTEM: "system", ThemeMode.DARK: "dark", ThemeMode.LIGHT: "light"}
        for mode, ctk_mode in expected.items():
            with self.subTest(mode=mode):
                with (
                    mock.patch.object(ctk, "set_appearance_mode") as set_mode,
                    mock.patch.object(ctk.AppearanceModeTracker, "init_appearance_mode") as detect,
                ):
                    theme.apply_appearance(mode)
                set_mode.assert_called_once_with(ctk_mode)
                # Following Windows looks at its setting at once, not at CTk's next check.
                self.assertEqual(detect.called, mode is ThemeMode.SYSTEM)

    def test_accent_buttons_get_white_text_in_light_only(self) -> None:
        with mock.patch.object(ctk, "set_appearance_mode"):
            theme.apply_appearance(ThemeMode.DARK)
            theme.apply_appearance(ThemeMode.DARK)  # twice changes nothing more
        text = ctk.ThemeManager.theme["CTkButton"]["text_color"]
        self.assertEqual(list(text), [theme.ON_ACCENT[0], theme.ON_ACCENT[1]])


class ModeChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_repaints_while_the_widget_lives_and_stops_when_it_goes(self) -> None:
        frame = tk.Frame(self.root)
        calls: list[str] = []
        before = list(ctk.AppearanceModeTracker.callback_list)
        theme.on_mode_change(frame, lambda: calls.append("repaint"))
        (callback,) = [c for c in ctk.AppearanceModeTracker.callback_list if c not in before]
        callback("Light")
        self.assertEqual(calls, ["repaint"])
        child = tk.Frame(frame)
        child.destroy()  # a child going is not the widget going
        self.assertIn(callback, ctk.AppearanceModeTracker.callback_list)
        frame.destroy()
        self.assertNotIn(callback, ctk.AppearanceModeTracker.callback_list)

    def test_a_toplevel_keeps_it_when_a_child_goes(self) -> None:
        top = tk.Toplevel(self.root)
        before = list(ctk.AppearanceModeTracker.callback_list)
        theme.on_mode_change(top, lambda: None)
        (callback,) = [c for c in ctk.AppearanceModeTracker.callback_list if c not in before]
        tk.Frame(top).destroy()  # a Toplevel hears its children's <Destroy> too
        self.assertIn(callback, ctk.AppearanceModeTracker.callback_list)
        top.destroy()
        self.assertNotIn(callback, ctk.AppearanceModeTracker.callback_list)


class TabviewTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    def test_pages_keep_the_colour_pair(self) -> None:
        tabs = Tabview(self.root, fg_color=theme.TAB_BG)
        page = tabs.add("One")
        inner = ctk.CTkFrame(page, fg_color="transparent")
        self.assertEqual(page.cget("fg_color"), theme.TAB_BG)
        self.assertEqual(inner.cget("bg_color"), theme.TAB_BG)  # not one mode's colour
        tabs._draw()  # CTk hands the pages one colour here; the pair comes back
        self.assertEqual(page.cget("fg_color"), theme.TAB_BG)
        self.assertEqual(inner.cget("bg_color"), theme.TAB_BG)


if __name__ == "__main__":
    unittest.main()
