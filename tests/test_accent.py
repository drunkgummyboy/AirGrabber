"""The accent colours (app/ui/palettes.py, theme.apply_accent): every palette sets every token in
both looks, Slate is today's look exactly, the design's values, green / amber / red and the text
kept, the stored setting (an unknown one is Slate), no module keeping a token from import time;
the swatches in Settings → Looks (with "Applies after a restart") and in Quick setup, and the
two other Looks boxes (Info window layout, the spotlight switch). Hidden Tk root, fake services."""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.i18n import Language, set_language
from app.models import Accent, InfoLayout, Settings
from app.services import SettingsService
from app.ui import theme
from app.ui.accent_picker import SWATCH, AccentSwatches, row_width, swatch_image
from app.ui.palettes import ACCENT_TOKENS, PALETTES, accent_name, shifted, to_hex, tokens
from app.ui.quick_setup import (
    ACCENT_RESTART_TEXT,
    BOTH_RESTART_TEXT,
    RESTART_TEXT,
    QuickSetupPanel,
    quick_changes,
    quick_defaults,
    restart_text,
)
from app.ui.settings_dialog import (
    INFO_LAYOUT_NAMES,
    SECTION_LOOKS,
    SPOTLIGHT_SWITCH,
    SettingsDialog,
    accent_needs_restart,
    section_for_field,
)
from tests.test_quick_setup import FakeProwlarr, FakeRepository, FakeShortcut, TkTestCase, labels

ROOT = Path(__file__).resolve().parent.parent

# Today's look (2.0.8), both halves: Slate must stay exactly this.
SLATE_BEFORE: dict[str, theme.Pair] = {
    "BG": ("#F2F4F7", "#161616"),
    "SURFACE": ("#FFFFFF", "#242424"),
    "TAB_BG": ("#F8F9FB", "#1C1C1C"),
    "EDGE": ("#768DA4", "#4E6A85"),
    "ACCENT": ("#4E6A85", "#4E6A85"),
    "ACCENT_HOVER": ("#427839", "#427839"),
    "LINK": ("#2D6390", "#5D8AA8"),
    "PLACEHOLDER": ("#DCE2E9", "gray20"),
    "HOVER": ("#DEE6EF", "#427839"),
    "MENU_HOVER": ("#DEE6EF", "#4E6A85"),
    "SELECTED": ("#C9D8E8", "#4E6A85"),
    "SELECTED_HOVER": ("#B8CADD", "#427839"),
    "SCROLLBAR": ("#D5DCE4", "#242424"),
    "CELL_BG": ("#ECF0F5", "#121620"),
    "CELL_BORDER": ("#D5DDE7", "#1C222E"),
    "TODAY_BG": ("#DEE8F3", "#182133"),
    "STACK_NEAR": ("#F6F8FB", "#212830"),
    "STACK_NEAR_EDGE": ("#9AABBD", "#46607A"),
    "STACK_FAR": ("#EEF2F6", "#1E2329"),
    "STACK_FAR_EDGE": ("#B9C6D3", "#3A4E63"),
    "CHIP": ("#E6EBF1", "gray15"),
    "CHIP_HOVER": ("#D5DDE6", "gray25"),
    "ROW_SELECTED": ("#D7E3F0", "#2A3A4D"),
    "NOT_AIRED": ("#E1E6EC", "gray25"),
}
# What no palette changes: green (downloaded), amber (missing), red (remove), the text.
KEPT = (
    "GRABBED",
    "SUCCESS",
    "SECTION_NEW",
    "WARNING",
    "SECTION_HOT",
    "PENDING",
    "DANGER",
    "DANGER_BUTTON",
    "DANGER_BUTTON_HOVER",
    "CLOSE_BUTTON_HOVER",
    "TEXT",
    "TEXT_MUTED",
    "TEXT_DIM",
    "BUTTON_TEXT",
    "ON_ACCENT",
    "IMDB",
)
HEX = re.compile(r"#[0-9A-F]{6}")
GREY = re.compile(r"gray\d{1,3}")


def luminance(color: str) -> float:
    hexed = to_hex(color)

    def channel(index: int) -> float:
        value = int(hexed[1 + 2 * index : 3 + 2 * index], 16) / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(0) + 0.7152 * channel(1) + 0.0722 * channel(2)


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class AccentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(theme.apply_accent, Accent.SLATE)


class PaletteTests(AccentCase):
    def test_ten_palettes_slate_first(self) -> None:
        self.assertEqual(list(PALETTES), list(Accent))
        self.assertEqual(len(PALETTES), 10)
        self.assertIs(next(iter(PALETTES)), Accent.SLATE)
        names = [accent_name(accent) for accent in PALETTES]
        self.assertEqual(
            names,
            ["Slate", "Ocean", "Petrol", "Teal", "Sage", "Indigo", "Plum", "Rose", "Mocha", "Graphite"],
        )

    def test_every_palette_sets_every_token_in_both_looks(self) -> None:
        slate = dict(SLATE_BEFORE)
        for accent in Accent:
            pairs = tokens(accent, slate)
            with self.subTest(accent=accent):
                self.assertEqual(set(pairs), set(ACCENT_TOKENS))
                for name, (light, dark) in pairs.items():
                    for color in (light, dark):
                        self.assertTrue(HEX.fullmatch(color) or GREY.fullmatch(color), (name, color))
                        to_hex(color)  # a colour Pillow and Tk read

    def test_slate_is_todays_look(self) -> None:
        self.assertEqual(set(SLATE_BEFORE), set(ACCENT_TOKENS))
        theme.apply_accent(Accent.TEAL)
        theme.apply_accent(Accent.SLATE)  # and back
        for name, pair in SLATE_BEFORE.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(theme, name), pair)
        self.assertEqual(theme.PANEL_EDGE, SLATE_BEFORE["PLACEHOLDER"])
        self.assertEqual(theme.CLOSE_BUTTON, SLATE_BEFORE["CHIP"])
        self.assertIs(theme.current_accent(), Accent.SLATE)
        self.assertIs(Settings().accent, Accent.SLATE)

    def test_a_palette_takes_the_designs_colours(self) -> None:
        theme.apply_accent(Accent.TEAL)
        self.assertIs(theme.current_accent(), Accent.TEAL)
        self.assertEqual(theme.ACCENT, ("#2E7A76", "#2E7A76"))
        self.assertEqual(theme.ACCENT_HOVER, ("#3B918C", "#3B918C"))
        self.assertEqual(theme.BG, ("#F0F5F4", "#131716"))
        self.assertEqual(theme.SURFACE, ("#FFFFFF", "#1E2423"))
        self.assertEqual(theme.CHIP, ("#E0ECEA", "#26302E"))
        self.assertEqual(theme.PLACEHOLDER, ("#D3E2DF", "#2A3432"))
        self.assertEqual(theme.EDGE, ("#6E9E99", "#3E7C77"))
        self.assertEqual(theme.ROW_SELECTED, ("#CFE6E3", "#1E3937"))
        self.assertEqual(theme.TODAY_BG, ("#DCEFEC", "#142826"))
        self.assertEqual(theme.LINK, ("#23706B", "#6FBDB6"))
        # Following tokens: the same as in Slate, where the token is that colour.
        self.assertEqual(theme.PANEL_EDGE, theme.PLACEHOLDER)
        self.assertEqual(theme.CLOSE_BUTTON, theme.CHIP)
        self.assertEqual(theme.MENU_HOVER[1], theme.ACCENT[1])
        self.assertEqual(theme.SELECTED[1], theme.ACCENT[1])
        self.assertEqual((theme.HOVER[1], theme.SELECTED_HOVER[1]), ("#3B918C", "#3B918C"))
        self.assertEqual(theme.SCROLLBAR[1], theme.SURFACE[1])

    def test_the_other_tokens_take_the_palettes_tint(self) -> None:
        derived = [name for name in ACCENT_TOKENS if name not in ("SURFACE",)]
        for accent in (Accent.TEAL, Accent.ROSE, Accent.PLUM):
            pairs = tokens(accent, SLATE_BEFORE)
            with self.subTest(accent=accent):
                for name in derived:
                    self.assertNotEqual(pairs[name], SLATE_BEFORE[name], name)

    def test_green_amber_red_and_the_text_stay(self) -> None:
        before = {name: getattr(theme, name) for name in KEPT}
        for accent in Accent:
            theme.apply_accent(accent)
            with self.subTest(accent=accent):
                self.assertEqual({name: getattr(theme, name) for name in KEPT}, before)

    def test_light_text_stays_readable_in_every_palette(self) -> None:
        """WCAG AA (4.5:1) for the body text, links and quiet buttons' text in the light look."""
        for accent in Accent:
            theme.apply_accent(accent)
            light = {
                name: theme.pick(getattr(theme, name), dark=False)
                for name in (*ACCENT_TOKENS, "TEXT", "TEXT_MUTED", "BUTTON_TEXT")
            }
            backgrounds = ("BG", "SURFACE", "TAB_BG", "CELL_BG", "TODAY_BG")
            for background in backgrounds:
                for text in ("TEXT", "TEXT_MUTED", "LINK", "BUTTON_TEXT"):
                    with self.subTest(accent=accent, text=text, on=background):
                        self.assertGreaterEqual(contrast(light[text], light[background]), 4.5)
            for fill in ("SELECTED", "SELECTED_HOVER", "CHIP", "CHIP_HOVER", "HOVER", "ROW_SELECTED"):
                with self.subTest(accent=accent, fill=fill):
                    self.assertGreaterEqual(contrast(light["BUTTON_TEXT"], light[fill]), 4.5)
            with self.subTest(accent=accent, fill="ACCENT"):
                self.assertGreaterEqual(contrast(theme.ON_COLOR, light["ACCENT"]), 4.5)


class ShiftTests(unittest.TestCase):
    def test_no_move_is_no_change(self) -> None:
        self.assertEqual(shifted("#DEE6EF", "#DEE8F3", "#DEE8F3"), "#DEE6EF")
        self.assertEqual(shifted("#212830", "#182133", "#182133"), "#212830")

    def test_the_hue_turns_with_the_anchor(self) -> None:
        # A blue tint moved like Slate's today cell to Teal's: a teal tint, as light.
        moved = shifted("#DEE6EF", "#DEE8F3", "#DCEFEC")
        red, green, blue = (int(moved[i : i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(green, red)
        self.assertGreater(green, blue - 2)
        self.assertAlmostEqual(luminance(moved), luminance("#DEE6EF"), delta=0.03)

    def test_a_grey_takes_the_palettes_tint(self) -> None:
        tinted = shifted("gray25", "#2A2B2D", "#30282B")  # Slate's chip → Rose's
        red, green, blue = (int(tinted[i : i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(red, green)
        self.assertNotEqual(tinted, "#404040")

    def test_tk_greys(self) -> None:
        self.assertEqual(to_hex("gray15"), "#262626")
        self.assertEqual(to_hex("gray20"), "#333333")
        self.assertEqual(to_hex("gray25"), "#404040")
        self.assertEqual(to_hex("#abcdef"), "#ABCDEF")
        with self.assertRaises(ValueError):
            to_hex("blue")


class StoredSettingTests(unittest.TestCase):
    def test_default_and_older_settings_are_slate(self) -> None:
        self.assertIs(Settings().accent, Accent.SLATE)
        self.assertIs(Settings.model_validate({"card_size": "s"}).accent, Accent.SLATE)

    def test_stored_values(self) -> None:
        for stored, expected in {"teal": Accent.TEAL, " Rose ": Accent.ROSE, "GRAPHITE": Accent.GRAPHITE}.items():
            with self.subTest(stored=stored):
                self.assertIs(Settings.model_validate({"accent": stored}).accent, expected)

    def test_an_unknown_value_is_slate(self) -> None:
        for stored in ("neon", "", "slate-blue"):
            with self.subTest(stored=stored):
                loaded = Settings.model_validate({"accent": stored, "card_size": "xl"})
                self.assertIs(loaded.accent, Accent.SLATE)
                self.assertEqual(loaded.card_size, "xl")  # the rest is kept
        self.assertEqual(Settings(accent=Accent.PLUM).model_dump(mode="json")["accent"], "plum")


_ACCENT_NAMES = frozenset((*ACCENT_TOKENS, "PANEL_EDGE", "CLOSE_BUTTON"))


def _import_time_tokens(tree: ast.AST) -> list[tuple[int, str]]:
    """`theme.X` accent tokens read while a module is imported: at module level, in a class
    body, a default argument or a decorator (not inside a function or lambda body)."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                args = child.args
                defaults = [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
                decorators = [] if isinstance(child, ast.Lambda) else child.decorator_list
                for part in (*defaults, *decorators):
                    found.extend(_reads(part))
                continue
            if isinstance(child, ast.Attribute):
                found.extend(_reads(child))
                continue
            visit(child)

    visit(tree)
    return found


def _reads(node: ast.AST) -> list[tuple[int, str]]:
    return [
        (n.lineno, n.attr)
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "theme"
        and n.attr in _ACCENT_NAMES
    ]


class ImportTimeTests(unittest.TestCase):
    def test_no_module_keeps_an_accent_token_from_import_time(self) -> None:
        """The palette is set at startup, after every module was imported: a copy made while
        importing would keep Slate's colour."""
        frozen: list[str] = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for line, name in _import_time_tokens(tree):
                frozen.append(f"{path.relative_to(ROOT).as_posix()}:{line}: theme.{name}")
        self.assertEqual(frozen, [], "read the token when the widget is made or painted")


# -- the swatches -----------------------------------------------------------------------------


class SwatchPictureTests(unittest.TestCase):
    def test_a_disc_and_the_chosen_ones_ring(self) -> None:
        plain = swatch_image(34, "#2E7A76", None, 1.0)
        self.assertEqual(plain.size, (34, 34))
        self.assertEqual(plain.getpixel((17, 17))[:3], (0x2E, 0x7A, 0x76))  # type: ignore[index]
        self.assertEqual(plain.getpixel((0, 0))[3], 0)  # type: ignore[index]
        self.assertLess(plain.getpixel((17, 1))[3], 16)  # type: ignore[index]  # no ring
        ringed = swatch_image(34, "#2E7A76", "#FFFFFF", 1.0)
        self.assertEqual(ringed.getpixel((17, 0))[:3], (255, 255, 255))  # type: ignore[index]
        self.assertGreater(ringed.getpixel((17, 0))[3], 128)  # type: ignore[index]
        self.assertLess(ringed.getpixel((17, 3))[3], 128)  # type: ignore[index]  # the gap
        self.assertEqual(row_width(), 10 * SWATCH + 9 * 2)


class SwatchWidgetTests(TkTestCase):
    async def test_the_chosen_one_is_ringed_and_a_click_tells(self) -> None:
        chosen: list[Accent] = []
        row = AccentSwatches(self.root, value=Accent.SLATE, background=theme.TAB_BG, command=chosen.append)
        self.assertEqual(list(row.swatches), list(Accent))
        self.assertTrue(row.swatches[Accent.SLATE].chosen)
        self.assertEqual(row.name.cget("text"), "Slate")
        row.swatches[Accent.ROSE].invoke()
        self.assertEqual(chosen, [Accent.ROSE])
        self.assertIs(row.value, Accent.ROSE)
        self.assertTrue(row.swatches[Accent.ROSE].chosen)
        self.assertFalse(row.swatches[Accent.SLATE].chosen)
        self.assertEqual(row.name.cget("text"), "Rose")
        row.swatches[Accent.ROSE].invoke()  # the same again: nothing new
        self.assertEqual(chosen, [Accent.ROSE])
        row.set(Accent.TEAL)  # shown, not told
        self.assertEqual(chosen, [Accent.ROSE])
        self.assertTrue(row.swatches[Accent.TEAL].chosen)

    async def test_each_swatch_names_its_colour(self) -> None:
        with mock.patch("app.ui.accent_picker.add_tooltip") as tip:
            AccentSwatches(self.root, value=Accent.SLATE, background=theme.TAB_BG)
        self.assertEqual([c.args[1] for c in tip.call_args_list][:3], ["Slate", "Ocean", "Petrol"])
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        with mock.patch("app.ui.accent_picker.add_tooltip") as tip:
            row = AccentSwatches(self.root, value=Accent.TEAL, background=theme.TAB_BG)
        self.assertEqual([c.args[1] for c in tip.call_args_list][3], "Groenblauw")
        self.assertEqual(row.name.cget("text"), "Groenblauw")


class QuickSetupAccentTests(TkTestCase):
    def test_restart_texts(self) -> None:
        self.assertIsNone(restart_text(False, False))
        self.assertEqual(restart_text(True, False), RESTART_TEXT)
        self.assertEqual(restart_text(False, True), ACCENT_RESTART_TEXT)
        self.assertEqual(restart_text(True, True), BOTH_RESTART_TEXT)

    def test_defaults_and_changes(self) -> None:
        current = Settings(accent=Accent.SAGE)
        chosen = quick_defaults(current)
        self.assertIs(chosen.accent, Accent.SAGE)
        self.assertEqual(quick_changes(current, chosen), {})

    async def test_the_swatches_under_the_look_and_the_restart_note(self) -> None:
        restart = mock.Mock()
        panel = QuickSetupPanel(self.root, current=Settings(), on_restart=restart)
        self.assertTrue(panel.accent.swatches[Accent.SLATE].chosen)
        self.assertFalse(panel.restart_row.winfo_manager())
        panel.accent.swatches[Accent.PLUM].invoke()
        self.assertIs(panel.chosen.accent, Accent.PLUM)
        self.assertTrue(panel.restart_row.winfo_manager())
        self.assertIn(ACCENT_RESTART_TEXT, labels(panel.restart_row))
        panel.language.choose(Language.NL)
        self.assertIn(BOTH_RESTART_TEXT, labels(panel.restart_row))
        self.assertEqual(panel.changes(Settings()), {"accent": Accent.PLUM, "language": Language.NL})
        panel.language.choose(Language.EN)
        panel.accent.swatches[Accent.SLATE].invoke()
        self.assertFalse(panel.restart_row.winfo_manager())


class SettingsLooksAccentTests(TkTestCase):
    """Settings → Looks: Accent colour, Info window layout and the spotlight switch."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.restarted: list[Accent] = []

    def dialog(self, **values: Any) -> tuple[SettingsDialog, SettingsService]:
        settings = SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder, **values),
        )
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.8"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            on_restart=lambda: self.restarted.append(settings.current.accent),
        )
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        return dialog, settings

    async def test_accent_swatches_restart_note_and_restart_now(self) -> None:
        dialog, settings = self.dialog()
        picker = dialog._accent_picker
        assert picker is not None
        self.assertIn("Accent colour", labels(dialog.page(SECTION_LOOKS)))
        self.assertTrue(picker.swatches[Accent.SLATE].chosen)
        self.assertFalse(dialog._accent_restart_row.winfo_manager())
        picker.swatches[Accent.TEAL].invoke()
        self.assertTrue(dialog._accent_restart_row.winfo_manager())
        self.assertIn("Applies after a restart", labels(dialog._accent_restart_row))
        self.assertIn("Restart now", button_texts(dialog._accent_restart_row))
        self.assertIs(theme.current_accent(), Accent.SLATE)  # nothing changes before a restart
        await dialog._restart_now()  # saves first, then restarts
        self.assertIs(settings.current.accent, Accent.TEAL)
        self.assertEqual(self.restarted, [Accent.TEAL])

    async def test_going_back_to_the_running_colour_hides_the_note(self) -> None:
        dialog, settings = self.dialog(accent="rose")
        picker = dialog._accent_picker
        assert picker is not None
        self.assertTrue(picker.swatches[Accent.ROSE].chosen)
        self.assertTrue(dialog._accent_restart_row.winfo_manager())  # Slate is running
        picker.swatches[Accent.SLATE].invoke()
        self.assertFalse(dialog._accent_restart_row.winfo_manager())
        await dialog._save()
        self.assertIs(settings.current.accent, Accent.SLATE)
        self.assertEqual(self.restarted, [])

    async def test_info_layout_and_spotlight(self) -> None:
        dialog, settings = self.dialog(info_layout="tabs", spotlight_motion=False)
        self.assertIn("Info window layout", labels(dialog.page(SECTION_LOOKS)))
        self.assertEqual(dialog._info_layout.get(), "Tabs")
        self.assertFalse(dialog._spotlight_motion.get())
        self.assertIn(SPOTLIGHT_SWITCH, switch_texts(dialog.page(SECTION_LOOKS)))
        dialog._info_layout.set("Side by side")
        dialog._spotlight_motion.set(True)
        await dialog._save()
        self.assertIs(settings.current.info_layout, InfoLayout.SIDE)
        self.assertTrue(settings.current.spotlight_motion)
        self.assertEqual(list(INFO_LAYOUT_NAMES), [InfoLayout.SIDE, InfoLayout.TABS])

    async def test_dutch_names(self) -> None:
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        dialog, settings = self.dialog(info_layout="tabs")
        self.assertEqual(dialog._info_layout.get(), "Tabbladen")
        page = labels(dialog.page(SECTION_LOOKS))
        self.assertIn("Indeling infovenster", page)
        self.assertIn("Accentkleur", page)
        self.assertIn("Spotlight wisselt vanzelf naar de volgende titel", switch_texts(dialog.page(SECTION_LOOKS)))
        dialog._info_layout.set("Naast elkaar")
        await dialog._save()
        self.assertIs(settings.current.info_layout, InfoLayout.SIDE)

    async def test_quick_setups_colour_shows_in_settings(self) -> None:
        dialog, settings = self.dialog()
        await settings.update(accent=Accent.INDIGO)
        dialog._quick_saved()
        picker = dialog._accent_picker
        assert picker is not None
        self.assertTrue(picker.swatches[Accent.INDIGO].chosen)
        self.assertTrue(dialog._accent_restart_row.winfo_manager())

    def test_sections_and_restart_rule(self) -> None:
        for name in ("accent", "info_layout", "spotlight_motion"):
            self.assertEqual(section_for_field(name), SECTION_LOOKS)
        self.assertTrue(accent_needs_restart(Accent.TEAL, Accent.SLATE))
        self.assertFalse(accent_needs_restart(Accent.SLATE, Accent.SLATE))


def button_texts(widget: Any) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton):
            found.append(str(child.cget("text")))
        found += button_texts(child)
    return found


def switch_texts(widget: Any) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkSwitch):
            found.append(str(child.cget("text")))
        found += switch_texts(child)
    return found



class UnderlineTabsTests(TkTestCase):
    async def test_compact_and_left_aligned_in_any_palette_and_width(self) -> None:
        """The main window's page tabs (widgets.Tabview, "underline"): the names one after the
        other from the left, whatever the palette and however wide the window."""
        from app.ui.widgets import UNDERLINE_LEFT, UNDERLINE_TAB_GAP, Tabview

        self.root.attributes("-alpha", 0.0)  # on screen (laid out at its width), see-through
        self.root.deiconify()
        for accent in (Accent.SLATE, Accent.TEAL):
            theme.apply_accent(accent)
            self.addCleanup(theme.apply_accent, Accent.SLATE)
            tabs = Tabview(self.root, look="underline")
            for name in ("Calendar", "Discover", "Tracked"):
                tabs.add(name)
            tabs.pack(fill="both", expand=True)
            for width in (900, 1650):
                self.root.geometry(f"{width}x500")
                await self.settle()
                bar = tabs.underline
                assert bar is not None
                scale = ctk.ScalingTracker.get_widget_scaling(bar)
                with self.subTest(accent=accent, width=width):
                    self.assertGreater(bar.winfo_width(), width * scale * 0.9)  # the full width
                    x = round(UNDERLINE_LEFT * scale)
                    for name in bar.names:
                        label = bar._labels[name]
                        self.assertLessEqual(abs(label.winfo_x() - x), 2, name)
                        mark = bar._marks[name]
                        self.assertLessEqual(mark.winfo_width(), label.winfo_width() + 2, name)
                        x += label.winfo_width() + round(UNDERLINE_TAB_GAP * scale)
            tabs.destroy()
