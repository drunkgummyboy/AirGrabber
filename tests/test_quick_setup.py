"""Quick setup (app/ui/quick_setup.py), the Card size preview (app/ui/card_preview.py), the
header's drawn settings icon (app/ui/icons.py), and how Settings → Looks and the welcome use
them; the widgets on a real (hidden) Tk root."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.i18n import Language
from app.models import CardSize, LibraryEntry, QualityProfile, Settings, Show, ThemeMode
from app.services import SettingsService
from app.ui import dialog_kit, theme
from app.ui.card_preview import (
    CardSizePreview,
    dashed_outline,
    outline_label,
    preview_room,
)
from app.ui.card_scale import CardScale
from app.ui.dialog_kit import reactivate
from app.ui.icons import SLIDERS, IconButton, draw_lines, sliders_icon
from app.ui.poster_card import card_size
from app.ui.quick_setup import (
    QuickChoices,
    QuickSetupDialog,
    QuickSetupPanel,
    quick_changes,
    quick_defaults,
    swatch_image,
)
from app.ui.settings_dialog import SECTION_LOOKS, SettingsDialog, quality_name
from app.ui.welcome_dialog import finish_changes


class QuickChoiceTests(unittest.TestCase):
    def test_defaults_from_settings(self) -> None:
        chosen = quick_defaults(Settings())
        self.assertEqual(
            chosen,
            QuickChoices(
                ThemeMode.SYSTEM, QualityProfile.FULL_HD, QualityProfile.FULL_HD, CardSize.M, Language.EN
            ),
        )

    def test_series_and_films_each_keep_their_quality(self) -> None:
        mixed = Settings(quality_tv="1080p", quality_movie="2160p")
        chosen = quick_defaults(mixed)
        self.assertEqual((chosen.quality_tv, chosen.quality_movie), (QualityProfile.FULL_HD, QualityProfile.UHD))
        self.assertEqual(quick_changes(mixed, chosen), {})

    def test_a_quality_or_size_not_offered_stays_unchosen(self) -> None:
        mixed = Settings(quality_tv="Any", quality_movie="720p", card_size="xs")
        chosen = quick_defaults(mixed)
        self.assertIsNone(chosen.quality_tv)
        self.assertIs(chosen.quality_movie, QualityProfile.HD)
        self.assertIsNone(chosen.size)
        self.assertEqual(quick_changes(mixed, chosen), {})  # nothing clicked: nothing changes

    def test_changes_are_only_what_differs(self) -> None:
        current = Settings(quality_tv="1080p", quality_movie="2160p", size_tv_max_gb=4)
        chosen = QuickChoices(
            ThemeMode.DARK, QualityProfile.UHD, QualityProfile.UHD, CardSize.L, Language.NL
        )
        changes = quick_changes(current, chosen)
        self.assertEqual(
            changes,
            {
                "theme_mode": ThemeMode.DARK,
                "quality_tv": QualityProfile.UHD,  # the film one had it already
                "card_size": CardSize.L,
                "language": Language.NL,
            },
        )
        separate = QuickChoices(
            ThemeMode.SYSTEM, QualityProfile.HD, QualityProfile.UHD, CardSize.M, Language.EN
        )
        self.assertEqual(
            quick_changes(Settings(), separate),
            {"quality_tv": QualityProfile.HD, "quality_movie": QualityProfile.UHD},
        )
        saved = Settings.model_validate({**current.model_dump(), **changes})
        self.assertEqual(saved.size_tv_max_gb, 4.0)  # the sizes stay
        self.assertEqual(quick_changes(current, quick_defaults(current)), {})

    def test_the_welcome_saves_both(self) -> None:
        chosen = QuickChoices(
            ThemeMode.LIGHT, QualityProfile.HD, QualityProfile.HD, CardSize.S, Language.EN
        )
        changes = {**quick_changes(Settings(), chosen), **finish_changes("key")}
        self.assertEqual(changes["quality_movie"], QualityProfile.HD)
        self.assertTrue(changes["setup_done"])
        self.assertEqual(changes["tmdb_api_key"], "key")


class PictureTests(unittest.TestCase):
    def test_sliders_icon(self) -> None:
        icon = sliders_icon(36, (220, 228, 238))
        self.assertEqual((icon.size, icon.mode), ((36, 36), "RGBA"))
        self.assertEqual(icon.getpixel((0, 0))[3], 0)  # type: ignore[index]
        # The middle slider's line runs down x = 12 of 24 (at 36 px: 18).
        self.assertGreater(icon.getpixel((18, 30))[3], 200)  # type: ignore[index]
        self.assertEqual(icon.getpixel((18, 30))[:3], (220, 228, 238))  # type: ignore[index]
        self.assertEqual(len(SLIDERS), 9)
        self.assertEqual(draw_lines((), 24, (0, 0, 0)).getbbox(), None)

    def test_swatches(self) -> None:
        dark = swatch_image(ThemeMode.DARK, (46, 28), "#4E6A85", 6)
        self.assertEqual(dark.getpixel((23, 14))[:3], (22, 22, 22))  # type: ignore[index]
        both = swatch_image(ThemeMode.SYSTEM, (46, 28), "#4E6A85", 6)
        self.assertEqual(both.getpixel((10, 14))[:3], (242, 244, 247))  # type: ignore[index]
        self.assertEqual(both.getpixel((36, 14))[:3], (22, 22, 22))  # type: ignore[index]
        self.assertEqual(both.getpixel((0, 0))[3], 0)  # type: ignore[index]

    def test_dashed_outline(self) -> None:
        outline = dashed_outline((96, 144), 7, 1.5, 6, 4, (125, 135, 146))
        self.assertEqual(outline.size, (96, 144))
        self.assertEqual(outline.getpixel((48, 72))[3], 0)  # type: ignore[index]
        top = [outline.getpixel((x, 0))[3] for x in range(10, 86)]  # type: ignore[index]
        self.assertTrue(any(alpha > 128 for alpha in top))  # dashes
        self.assertTrue(any(alpha == 0 for alpha in top))  # and gaps

    def test_outline_label(self) -> None:
        self.assertEqual(outline_label(CardSize.L, CardSize.S), "S (now)")
        self.assertIsNone(outline_label(CardSize.M, CardSize.M))

    def test_the_preview_keeps_the_largest_cards_room(self) -> None:
        width, height = preview_room()
        xl_width, xl_height = card_size(CardScale.of(CardSize.XL))
        self.assertEqual(width, 2 * xl_width + 16)
        self.assertGreater(height, xl_height)


class FakeRepository:
    async def save(self, settings: Settings) -> None:
        pass


class FakeProwlarr:
    async def status(self) -> Any:
        from app.models import ProwlarrState, ProwlarrStatus

        return ProwlarrStatus(state=ProwlarrState.NOT_INSTALLED)


class FakeShortcut:
    def is_enabled(self) -> bool:
        return False

    async def set_enabled(self, on: bool) -> None:
        raise AssertionError("not changed here")


class FakeLibrary:
    def __init__(self, *shows: Show) -> None:
        self.shows = shows

    async def tracked(self) -> list[LibraryEntry]:
        from datetime import UTC, datetime

        now = datetime(2026, 10, 26, tzinfo=UTC)
        return [LibraryEntry(show=show, tracked_at=now) for show in self.shows]


def labels(widget: Any) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found += labels(child)
    return found


class TkTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.images = mock.MagicMock(get=mock.AsyncMock(return_value=None))
        mode = ctk.get_appearance_mode()
        self.addCleanup(ctk.set_appearance_mode, mode)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def settle(self) -> None:
        for _ in range(4):
            await asyncio.sleep(0.01)
            self.root.update()


class IconButtonTests(TkTestCase):
    async def test_drawn_at_the_physical_size_in_both_looks(self) -> None:
        button = IconButton(self.root, draw=sliders_icon, width=40, height=40)
        scaling = ctk.ScalingTracker.get_widget_scaling(button)
        side = round(24 * scaling)
        assert button.icon is not None
        self.assertEqual(button.icon.cget("size"), (24, 24))
        light = button.icon.cget("light_image")
        dark = button.icon.cget("dark_image")
        self.assertEqual((light.size, dark.size), ((side, side), (side, side)))
        self.assertNotEqual(light.getpixel((side // 2, side - 3)), dark.getpixel((side // 2, side - 3)))
        self.assertEqual(button.cget("text"), "")


class PreviewWidgetTests(TkTestCase):
    async def test_the_card_follows_the_chosen_size(self) -> None:
        preview = CardSizePreview(self.root, images=self.images, chosen=CardSize.S, saved=CardSize.S)
        await self.settle()
        assert preview.card is not None
        self.assertEqual(preview.card.title_text, "Your show")
        self.assertIsNone(preview.label_text)  # no outline while they're the same
        preview.show(CardSize.L)
        await self.settle()
        self.assertEqual(preview.chosen, CardSize.L)
        self.assertEqual(preview.card.scale, CardScale.of(CardSize.L))
        self.assertEqual(preview.label_text, "S (now)")

    async def test_a_library_show_on_the_card(self) -> None:
        preview = CardSizePreview(self.root, images=self.images, chosen=CardSize.M, saved=CardSize.M)
        preview.use_show(Show(id=7, name="Nowhere Lane", premiered="2024-03-01", rating=8.1,
                              poster_url="https://static.tvmaze.com/uploads/images/medium_portrait/1/2.jpg"))
        await self.settle()
        assert preview.card is not None
        self.assertEqual(preview.card.title_text, "Nowhere Lane")
        self.assertEqual(preview.card.meta_text, "2024 | ★ 8.1")
        self.images.get.assert_awaited()


class QuickPanelTests(TkTestCase):
    async def test_choices_and_the_live_look(self) -> None:
        panel = QuickSetupPanel(self.root, current=Settings())
        self.assertTrue(panel.look.choices[ThemeMode.SYSTEM].selected)
        self.assertTrue(panel.quality_tv.choices[QualityProfile.FULL_HD].selected)
        self.assertTrue(panel.quality_movie.choices[QualityProfile.FULL_HD].selected)
        self.assertTrue(panel.card_size.choices[CardSize.M].selected)
        with mock.patch.object(theme, "apply_appearance") as apply:
            panel.look.choose(ThemeMode.DARK)
            apply.assert_called_once_with(ThemeMode.DARK)
            panel.revert_look(ThemeMode.SYSTEM)
            apply.assert_called_with(ThemeMode.SYSTEM)
        panel.quality_tv.choose(QualityProfile.UHD)
        panel.quality_movie.choose(QualityProfile.HD)
        panel.card_size.choose(CardSize.S)
        self.assertFalse(panel.quality_tv.choices[QualityProfile.FULL_HD].selected)
        self.assertTrue(panel.quality_movie.choices[QualityProfile.HD].selected)
        self.assertEqual(
            panel.changes(Settings()),
            {
                "theme_mode": ThemeMode.DARK,
                "quality_tv": QualityProfile.UHD,
                "quality_movie": QualityProfile.HD,
                "card_size": CardSize.S,
            },
        )
        self.assertIn("Series", labels(panel))
        self.assertIn("Movies", labels(panel))

    async def test_the_quality_rows_start_from_settings(self) -> None:
        panel = QuickSetupPanel(self.root, current=Settings(quality_tv="720p", quality_movie="2160p"))
        self.assertTrue(panel.quality_tv.choices[QualityProfile.HD].selected)
        self.assertTrue(panel.quality_movie.choices[QualityProfile.UHD].selected)
        self.assertEqual(panel.changes(Settings(quality_tv="720p", quality_movie="2160p")), {})

    async def test_another_language_offers_a_restart(self) -> None:
        restart = mock.Mock()
        panel = QuickSetupPanel(self.root, current=Settings(), on_restart=restart)
        self.assertFalse(panel.restart_row.winfo_manager())
        panel.language.choose(Language.NL)
        self.assertTrue(panel.restart_row.winfo_manager())
        self.assertIn("Restart AirGrabber to switch the language", labels(panel.restart_row))
        assert panel.restart_button is not None
        panel.restart_button.invoke()
        restart.assert_called_once()
        panel.language.choose(Language.EN)
        self.assertFalse(panel.restart_row.winfo_manager())


class ModeSwitchTests(TkTestCase):
    """A switch between light and dark (Windows: CTk hides and re-shows every window) leaves a
    dialog on top with the keyboard focus and the grab (app/ui/dialog_kit.py `reactivate`)."""

    def setUp(self) -> None:
        super().setUp()
        # On screen (a hidden main window means "in the tray": nothing is shown then), but
        # see-through.
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("600x400+40+40")
        self.root.deiconify()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.settle()  # CTk only re-shows a main window that has been on screen

    def _destroy_root(self) -> None:
        # Tcl's own cancel: after_cancel() would also delete the callbacks' commands, which the
        # CTkToplevels that scheduled them delete again when destroyed.
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.root.destroy()
        gc.collect()

    def dialog(self, master: Any) -> ctk.CTkToplevel:
        dialog = ctk.CTkToplevel(master)
        dialog.attributes("-alpha", 0.0)
        dialog.transient(master)
        return dialog

    @staticmethod
    def grab(widget: Any) -> str:
        """The window with the grab ("" for none)."""
        return " ".join(str(w) for w in widget.tk.splitlist(widget.tk.call("grab", "current")))

    async def test_a_hidden_dialog_comes_back_with_the_grab(self) -> None:
        dialog = self.dialog(self.root)
        await self.settle()
        dialog.grab_set()
        dialog.withdraw()  # what the switch left behind
        reactivate(dialog)
        self.assertEqual(dialog.wm_state(), "normal")
        self.assertEqual(self.grab(dialog), str(dialog))

    async def test_a_dialog_over_a_dialog(self) -> None:
        settings = self.dialog(self.root)
        quick = self.dialog(settings)
        await self.settle()
        quick.grab_set()
        settings.withdraw()
        quick.withdraw()
        reactivate(settings)  # Settings' turn first: shown again, the grab stays with Quick setup's
        self.assertEqual(settings.wm_state(), "normal")
        self.assertEqual(self.grab(quick), str(quick))
        reactivate(quick)
        self.assertEqual((settings.wm_state(), quick.wm_state()), ("normal", "normal"))
        self.assertEqual(self.grab(quick), str(quick))

    async def test_nothing_while_the_main_window_is_in_the_tray(self) -> None:
        dialog = self.dialog(self.root)
        await self.settle()
        self.root.withdraw()
        dialog.withdraw()
        reactivate(dialog)
        self.assertEqual(dialog.wm_state(), "withdrawn")

    async def test_choosing_a_look_keeps_quick_setup_on_top(self) -> None:
        start = "Dark" if ctk.get_appearance_mode() == "Light" else "Light"
        ctk.set_appearance_mode(start)
        await self.settle()
        settings = SettingsService(FakeRepository(), Settings())  # type: ignore[arg-type]
        quick = QuickSetupDialog(self.root, settings=settings)
        quick.attributes("-alpha", 0.0)
        for _ in range(30):  # activate_later: 150 ms
            await asyncio.sleep(0.01)
            self.root.update()
        other = ThemeMode.LIGHT if start == "Dark" else ThemeMode.DARK
        with mock.patch.object(dialog_kit, "reactivate", wraps=dialog_kit.reactivate) as spy:
            quick.panel.look.choose(other)  # applies at once: CTk hides and re-shows the windows
            for _ in range(40):
                await asyncio.sleep(0.01)
                self.root.update()
        spy.assert_called_with(quick)
        self.assertEqual(quick.wm_state(), "normal")
        self.assertEqual(self.grab(quick), str(quick))
        quick.destroy()


class SettingsLooksTests(TkTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def dialog(self, library: FakeLibrary | None = None, **values: Any) -> tuple[SettingsDialog, SettingsService]:
        settings = SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder, **values),
        )
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.5"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            images=self.images,
            library=library,  # type: ignore[arg-type]
        )
        self.addCleanup(self._close, dialog)
        return dialog, settings

    @staticmethod
    def _close(dialog: SettingsDialog) -> None:
        if dialog.winfo_exists():
            dialog.destroy()

    async def test_the_preview_follows_the_size_buttons(self) -> None:
        dialog, _settings = self.dialog(card_size="s")
        preview = dialog._card_preview
        assert preview is not None
        self.assertEqual(preview.chosen, CardSize.S)
        dialog._card_size.set("XL")
        await self.settle()
        self.assertEqual(preview.chosen, CardSize.XL)
        self.assertEqual(preview.label_text, "S (now)")

    async def test_the_preview_shows_a_followed_show(self) -> None:
        show = Show(id=3, name="The Quiet Orchard", poster_url="https://static.tvmaze.com/p.jpg")
        dialog, _settings = self.dialog(FakeLibrary(Show(id=2, name="No poster"), show))
        await self.settle()
        preview = dialog._card_preview
        assert preview is not None and preview.card is not None
        self.assertEqual(preview.card.title_text, "The Quiet Orchard")

    async def test_quick_setup_from_looks_saves_and_shows_its_choices(self) -> None:
        dialog, settings = self.dialog()
        self.assertIn("Quick setup", labels(dialog.page(SECTION_LOOKS)))
        dialog._open_quick_setup()
        quick = dialog._quick_dialog
        assert quick is not None
        with mock.patch.object(theme, "apply_appearance"):
            quick.panel.look.choose(ThemeMode.LIGHT)
        quick.panel.quality_tv.choose(QualityProfile.HD)
        quick.panel.quality_movie.choose(QualityProfile.UHD)
        quick.panel.card_size.choose(CardSize.L)
        self.assertTrue(await quick.save())
        current = settings.current
        self.assertEqual(
            (current.theme_mode, current.quality_tv, current.quality_movie, current.card_size),
            (ThemeMode.LIGHT, QualityProfile.HD, QualityProfile.UHD, CardSize.L),
        )
        self.assertFalse(quick.winfo_exists())
        # Settings shows them, so its own Save keeps them.
        self.assertEqual(dialog._theme_mode.get(), "Light")
        self.assertEqual(dialog._quality_tv.get(), quality_name(QualityProfile.HD))
        self.assertEqual(dialog._quality_movie.get(), quality_name(QualityProfile.UHD))
        self.assertEqual(dialog._card_size.get(), "L")
        await dialog._save()
        self.assertIs(settings.current.quality_tv, QualityProfile.HD)

    async def test_cancel_takes_the_look_back(self) -> None:
        dialog, settings = self.dialog(theme_mode="dark")
        quick = QuickSetupDialog(dialog, settings=settings)
        with mock.patch.object(theme, "apply_appearance") as apply:
            quick.panel.look.choose(ThemeMode.LIGHT)
            quick.cancel()
            apply.assert_called_with(ThemeMode.DARK)
        self.assertIs(settings.current.theme_mode, ThemeMode.DARK)
        self.assertFalse(quick.winfo_exists())


if __name__ == "__main__":
    unittest.main()
