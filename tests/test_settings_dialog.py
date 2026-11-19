"""Settings dialog: plain quality names, a validation error's words, download-mode rows, the size
entries' texts, and the dialog itself (its pages, the quality page, the TMDB key box: each change
stored as it is made) on a real (hidden) Tk root. The pages' order and where each box went:
tests/test_settings_registry.py; instant saving and Undo: tests/test_settings_commit.py.

`click`, `find` and `settle` are shared with the other Settings tests: a control clicked as the
user would (its command runs), a control by its variable, and every change started meanwhile
stored."""

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
from pydantic import ValidationError

from app.models import (
    DownloadMode,
    ProwlarrState,
    ProwlarrStatus,
    QualityProfile,
    Settings,
    ThemeMode,
)
from app.services import SettingsService
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.settings_dialog import (
    KEY_GUIDE_LINK,
    MOVIES_TEXT,
    QUALITY_NAMES,
    QUALITY_NOTE,
    SERIES_TEXT,
    THEME_NAMES,
    THEME_TEXT,
    THEME_TITLE,
    TMDB_KEY_LABEL,
    TMDB_KEY_TEXT,
    TMDB_TITLE,
    DownloadRows,
    SettingsDialog,
    _describe,
    dialog_height,
    download_rows,
    quality_from_name,
    quality_name,
    sidebar_width,
    size_text,
    theme_from_name,
    theme_name,
)
from app.ui.settings_layout import NARROW_PAGE_WIDTH, WIDE_PAGE_WIDTH, page_width
from app.ui.settings_pages_registry import (
    PAGE_ACCOUNTS,
    PAGE_LOOKS,
    PAGE_ORDER,
    PAGE_OVERVIEW,
    PAGE_PAGES,
    PAGE_PHONE,
    PAGE_PLAYING,
    PAGE_PLEX,
    PAGE_QUALITY,
    PAGE_SORTING,
    PAGE_NAMING,
    PAGE_SUBTITLES,
    PAGE_UPDATES,
    PAGES,
)
from app.ui.tasks import _running


def find(widget: Any, variable: Any) -> Any:
    """The control under `widget` that shows `variable`."""
    stack = [widget]
    while stack:
        found = stack.pop()
        if getattr(found, "_variable", None) is variable:
            return found
        stack.extend(found.winfo_children())
    raise LookupError("no control shows that variable")


def click(widget: Any, value: str | None = None) -> None:
    """What the user's click does: a choice of a row of choices or a menu (`value`), a tick, a
    switch, a radio button; its command runs as with a real click."""
    if isinstance(widget, ctk.CTkSegmentedButton):
        widget.set(value, from_button_callback=True)
    elif isinstance(widget, ctk.CTkOptionMenu):
        widget._dropdown_callback(value)
    elif isinstance(widget, ctk.CTkRadioButton):
        widget.invoke()
    else:
        widget.toggle()


async def settle(timeout: float = 2.0) -> None:
    """Wait for every change (and what it started) to be stored; a failure is raised. Tasks
    that wait for something else (a test's gate) are left after `timeout`."""
    loop = asyncio.get_running_loop()
    for _ in range(500):
        pending = [task for task in _running if not task.done() and task.get_loop() is loop]
        if not pending:
            return
        done, _waiting = await asyncio.wait(
            pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
        if not done:
            return
        await asyncio.sleep(0)


def validation_error(**fields: object) -> ValidationError:
    try:
        Settings.model_validate(fields)
    except ValidationError as exc:
        return exc
    raise AssertionError(f"{fields} validated")


class QualityNameTests(unittest.TestCase):
    def test_plain_names(self) -> None:
        self.assertEqual(quality_name(QualityProfile.FULL_HD), "Full HD (1080p)")
        self.assertEqual(quality_name(QualityProfile.HD), "HD (720p)")
        self.assertEqual(quality_name(QualityProfile.UHD), "4K (2160p)")
        self.assertEqual(quality_name(QualityProfile.HEVC), "Smaller files (1080p x265)")
        self.assertEqual(quality_name(QualityProfile.ANY), "Any quality")

    def test_menu_order(self) -> None:
        self.assertEqual(
            list(QUALITY_NAMES),
            [
                QualityProfile.FULL_HD,
                QualityProfile.HD,
                QualityProfile.UHD,
                QualityProfile.HEVC,
                QualityProfile.ANY,
            ],
        )

    def test_round_trip_keeps_the_stored_values(self) -> None:
        self.assertEqual(set(QUALITY_NAMES), set(QualityProfile))
        for profile in QualityProfile:
            with self.subTest(profile=profile):
                self.assertIs(quality_from_name(quality_name(profile)), profile)
                chosen = quality_from_name(quality_name(profile))
                stored = Settings(quality_tv=chosen, quality_movie=chosen).model_dump(mode="json")
                self.assertEqual(stored["quality_tv"], profile.value)
                self.assertEqual(stored["quality_movie"], profile.value)

    def test_names_are_unique(self) -> None:
        self.assertEqual(len(set(QUALITY_NAMES.values())), len(QUALITY_NAMES))

    def test_unknown_name(self) -> None:
        with self.assertRaises(ValueError):
            quality_from_name("1080p")


class ThemeNameTests(unittest.TestCase):
    def test_follow_windows_first_then_dark_and_light(self) -> None:
        self.assertEqual(list(THEME_NAMES.values()), ["Follow Windows", "Dark", "Light"])
        self.assertEqual(list(THEME_NAMES), list(ThemeMode))

    def test_round_trip(self) -> None:
        for mode in ThemeMode:
            with self.subTest(mode=mode):
                self.assertIs(theme_from_name(theme_name(mode)), mode)

    def test_unknown_name(self) -> None:
        with self.assertRaises(ValueError):
            theme_from_name("Sepia")


class SizeTextTests(unittest.TestCase):
    def test_what_the_entries_show(self) -> None:
        self.assertEqual(size_text(None), "")
        self.assertEqual(size_text(2.5), "2.5")
        self.assertEqual(size_text(4.0), "4")
        self.assertEqual(size_text(15), "15")

    def test_shown_text_validates_back(self) -> None:
        for value in (None, 0.7, 2.5, 4.0, 40.0):
            with self.subTest(value=value):
                settings = Settings(size_tv_min_gb=size_text(value))
                self.assertEqual(settings.size_tv_min_gb, value)


class SectionTests(unittest.TestCase):
    def test_every_page_has_an_icon_and_a_note(self) -> None:
        self.assertEqual(PAGE_ORDER[0], PAGE_OVERVIEW)
        for page in PAGE_ORDER:
            with self.subTest(page=page):
                self.assertTrue(PAGES[page].icon)
                self.assertTrue(PAGES[page].note)



class DownloadRowsTests(unittest.TestCase):
    def test_torrent_program_shows_its_status_only(self) -> None:
        self.assertEqual(
            download_rows(DownloadMode.APP),
            DownloadRows(torrent_status=True, remote_form=False, folder=False),
        )

    def test_remote_program_shows_its_form_only(self) -> None:
        self.assertEqual(
            download_rows(DownloadMode.REMOTE),
            DownloadRows(torrent_status=False, remote_form=True, folder=False),
        )

    def test_folder_shows_the_folder_only(self) -> None:
        self.assertEqual(
            download_rows(DownloadMode.FOLDER),
            DownloadRows(torrent_status=False, remote_form=False, folder=True),
        )


class DialogHeightTests(unittest.TestCase):
    WANTED = 785  # the tallest page and padding at full size

    def test_1080p_at_150_percent(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 720), 640)

    def test_1366x768_at_100_percent(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 768), 688)

    def test_1366x768_at_150_percent(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 512), 432)

    def test_never_taller_than_its_content(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 1067), self.WANTED)
        self.assertEqual(dialog_height(self.WANTED, self.WANTED + 80), self.WANTED)


class ValidationErrorTests(unittest.TestCase):
    def test_plain_labels(self) -> None:
        self.assertEqual(
            _describe(validation_error(download_dir="")),
            "Save .torrent files to a folder: Choose a folder",
        )
        self.assertEqual(
            _describe(validation_error(remote_url="nas.local")),
            "Address: Use a full address like https://nas.local:5001",
        )
        self.assertTrue(_describe(validation_error(remote_kind="x")).startswith("Program: "))
        self.assertTrue(
            _describe(validation_error(excluded_keywords=5)).startswith(
                "Skip releases with these words: "
            )
        )
        self.assertTrue(
            _describe(validation_error(quality_tv="x")).startswith("Series quality: ")
        )
        self.assertTrue(
            _describe(validation_error(quality_movie="x")).startswith("Movies quality: ")
        )
        self.assertEqual(
            _describe(validation_error(size_tv_min_gb="lots")),
            "Series size: Use a number, like 2.5",
        )
        self.assertEqual(
            _describe(validation_error(size_movie_min_gb=9, size_movie_max_gb="3")),
            "Movies size: The first number can't be bigger than the second",
        )
        self.assertTrue(
            _describe(validation_error(tmdb_api_key=5)).startswith(f"{TMDB_KEY_LABEL}: ")
        )

# -- the dialog --------------------------------------------------------------------------------


class FakeRepository:
    async def save(self, settings: Settings) -> None:
        pass


class FakeProwlarr:
    async def status(self) -> ProwlarrStatus:
        return ProwlarrStatus(state=ProwlarrState.NOT_INSTALLED)


class FakeShortcut:
    def is_enabled(self) -> bool:
        return False

    async def set_enabled(self, on: bool) -> None:
        raise AssertionError("not changed here")


def descendants(widget: Any) -> list[Any]:
    found: list[Any] = []
    for child in widget.winfo_children():
        found += [child, *descendants(child)]
    return found


def labels(widget: Any) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found += labels(child)
    return found


class DialogCase(unittest.IsolatedAsyncioTestCase):
    """A Settings window on fakes (`dialog`), for the tests here and Settings' search's."""

    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.discovery = mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True))

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # collect the dialog's Tk variables on the main thread

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
            updates=SimpleNamespace(current="2.0.4", previous_version=None),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=self.discovery,
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
        )
        self.addCleanup(self._close, dialog)
        return dialog, settings

    @staticmethod
    def _close(dialog: SettingsDialog) -> None:
        if dialog.winfo_exists():
            dialog.destroy()


class DialogTests(DialogCase):
    async def test_opens_on_overview_and_switches_pages(self) -> None:
        dialog, _settings = self.dialog()
        self.assertEqual(dialog.section, PAGE_OVERVIEW)
        self.assertTrue(dialog.page(PAGE_OVERVIEW).winfo_manager())
        # Without their services there are no Plex & library, Playing or Phone connection
        # pages, and without Forge no Sorting, Naming or Subtitles.
        left_out = (PAGE_PLEX, PAGE_PLAYING, PAGE_PHONE, PAGE_SORTING, PAGE_NAMING, PAGE_SUBTITLES)
        self.assertEqual(list(dialog._items), [p for p in PAGE_ORDER if p not in left_out])
        self.assertNotIn("WATCHING", labels(dialog._sidebar))  # a group with no page
        dialog._items[PAGE_LOOKS].click()
        self.assertEqual(dialog.section, PAGE_LOOKS)
        self.assertTrue(dialog._items[PAGE_LOOKS].selected)
        self.assertFalse(dialog._items[PAGE_OVERVIEW].selected)
        self.assertTrue(dialog.page(PAGE_LOOKS).winfo_manager())
        self.assertFalse(dialog.page(PAGE_OVERVIEW).winfo_manager())

    async def test_every_page_has_a_heading_and_a_muted_line(self) -> None:
        dialog, _settings = self.dialog()
        for section in dialog._items:
            with self.subTest(section=section):
                texts = labels(dialog.page(section))
                self.assertEqual(texts[0], section)
                self.assertTrue(texts[1])

    def on_a_screen(self, width: float) -> SettingsDialog:
        """The dialog opened over a main window on a screen `width` wide (logical units)."""
        scaling = ctk.ScalingTracker.get_window_scaling(self.root)
        area = (0, 0, round(width * scaling), round(1000 * scaling))
        with mock.patch("app.ui.settings_dialog.screen_area", return_value=area):
            dialog, _settings = self.dialog()
        return dialog

    @staticmethod
    def width(dialog: SettingsDialog) -> float:
        dialog.update_idletasks()
        return int(dialog.winfo_reqwidth()) / float(ctk.ScalingTracker.get_window_scaling(dialog))

    async def test_fits_a_1920x1080_screen_at_175_percent(self) -> None:
        dialog = self.on_a_screen(1920 / 1.75)  # 1097
        width = self.width(dialog)
        self.assertLessEqual(width, 1920 / 1.75 - 20)  # room for the window's own edges
        self.assertGreaterEqual(width, 1000)  # the wider window (the owner's wish, 2026-11-19)
        self.assertEqual(page_width(), WIDE_PAGE_WIDTH)
        self.assertGreaterEqual(sidebar_width(), 220)

    async def test_fits_a_1366x768_screen_at_150_percent(self) -> None:
        # Its pages narrow (not below the old width): the whole window fits the screen.
        dialog = self.on_a_screen(1366 / 1.5)  # 910
        self.assertLessEqual(self.width(dialog), 1366 / 1.5 - 20)
        self.assertLess(page_width(), WIDE_PAGE_WIDTH)
        self.assertGreaterEqual(page_width(), NARROW_PAGE_WIDTH)
        # What its pages wrap at follows: the Quality page's note, a box's explanation.
        note = next(
            child for child in descendants(dialog.page(PAGE_QUALITY))
            if isinstance(child, ctk.CTkLabel) and child.cget("text") == QUALITY_NOTE
        )
        self.assertEqual(int(note.cget("wraplength")), page_width())
        dialog.destroy()
        self.assertEqual(page_width(), WIDE_PAGE_WIDTH)  # the next window decides its own

    async def test_never_narrower_than_the_old_page(self) -> None:
        self.on_a_screen(700)
        self.assertEqual(page_width(), NARROW_PAGE_WIDTH)

    async def test_the_quality_page(self) -> None:
        dialog, settings = self.dialog(
            quality_tv="720p", quality_movie="2160p", size_movie_min_gb=6, size_movie_max_gb=15
        )
        texts = labels(dialog.page(PAGE_QUALITY))
        for text in ("Series", SERIES_TEXT, "Movies", MOVIES_TEXT, QUALITY_NOTE, "between", "GB"):
            self.assertIn(text, texts)
        self.assertEqual(dialog._quality_tv.get(), "HD (720p)")
        self.assertEqual(dialog._quality_movie.get(), "4K (2160p)")
        self.assertEqual((dialog._size_tv_min.get(), dialog._size_tv_max.get()), ("", ""))
        self.assertEqual((dialog._size_movie_min.get(), dialog._size_movie_max.get()), ("6", "15"))
        click(find(dialog.page(PAGE_QUALITY), dialog._quality_tv), "Full HD (1080p)")
        await settle()
        self.assertIs(settings.current.quality_tv, QualityProfile.FULL_HD)  # at once
        dialog._size_tv_min.set("2,5")
        dialog._size_tv_max.set("4")
        self.assertIsNone(settings.current.size_tv_min_gb)  # typing stores nothing
        dialog._size_fields["tv"].commit_now()  # Enter, or the keyboard leaving them
        dialog._size_movie_max.set("")
        dialog._size_fields["movie"].commit_now()
        await settle()
        current = settings.current
        self.assertIs(current.quality_movie, QualityProfile.UHD)
        self.assertEqual((current.size_tv_min_gb, current.size_tv_max_gb), (2.5, 4.0))
        self.assertEqual((current.size_movie_min_gb, current.size_movie_max_gb), (6.0, None))
        self.assertTrue(dialog.winfo_exists())

    async def test_a_bad_size_is_refused_in_its_box(self) -> None:
        dialog, settings = self.dialog()
        dialog._size_tv_min.set("5")
        dialog._size_tv_max.set("3")
        dialog._size_fields["tv"].commit_now()
        await settle()
        self.assertEqual(
            dialog.box(PAGE_QUALITY, "Series").refusal,
            "Series size: The first number can't be bigger than the second",
        )
        self.assertIsNone(settings.current.size_tv_max_gb)
        self.assertEqual(dialog._size_tv_max.get(), "3")  # what was typed stays

    async def test_the_tmdb_key_box(self) -> None:
        dialog, settings = self.dialog(tmdb_api_key="old")
        texts = labels(dialog.page(PAGE_ACCOUNTS))
        for text in (TMDB_TITLE, TMDB_KEY_TEXT, KEY_GUIDE_LINK):
            self.assertIn(text, texts)
        self.assertEqual(dialog._tmdb_check.cget("text"), "Check")
        self.assertNotIn("Movies key", texts)
        self.assertEqual(dialog._tmdb_entry.cget("show"), "•")
        dialog._tmdb_key.set("  newkey  ")
        dialog._check_tmdb_key()
        self.assertEqual(dialog._tmdb_result.cget("text"), "Checking…")
        await dialog._tests["tmdb"]
        self.discovery.check_tmdb_key.assert_awaited_once_with("newkey")
        self.assertEqual(dialog._tmdb_result.cget("text"), "✓ The key works")
        self.assertEqual(dialog._tmdb_result.cget("text_color"), theme.SUCCESS)
        dialog._tmdb_key.set("other")  # an edit forgets the result
        self.assertFalse(dialog._tmdb_result.winfo_manager())
        dialog._tmdb_field.commit_now()  # stored unchecked: Check stays optional
        await settle()
        self.assertEqual(settings.current.tmdb_api_key.get_secret_value(), "other")

    async def test_a_refused_or_unreachable_key(self) -> None:
        dialog, _settings = self.dialog()
        self.discovery.check_tmdb_key.return_value = False
        dialog._tmdb_key.set("wrong")
        dialog._check_tmdb_key()
        await dialog._tests["tmdb"]
        self.assertIn("didn't work", dialog._tmdb_result.cget("text"))
        self.discovery.check_tmdb_key.side_effect = TMDBError("TMDB did not answer.")
        dialog._tmdb_key.set("again")
        with self.assertLogs("app.ui.settings_dialog", "INFO") as logs:
            dialog._check_tmdb_key()
            await dialog._tests["tmdb"]
        self.assertIn("Couldn't reach", dialog._tmdb_result.cget("text"))
        self.assertNotIn("again", "\n".join(logs.output))  # never the key

    async def test_an_empty_key_is_not_checked(self) -> None:
        dialog, _settings = self.dialog()
        dialog._check_tmdb_key()
        self.assertNotIn("tmdb", dialog._tests)
        self.discovery.check_tmdb_key.assert_not_awaited()

    async def test_a_key_saved_by_the_guide_shows_as_working(self) -> None:
        dialog, _settings = self.dialog()
        dialog._key_saved("fromguide")
        self.assertEqual(dialog._tmdb_key.get(), "fromguide")
        self.assertEqual(dialog._tmdb_result.cget("text"), "✓ The key works")

    async def test_the_card_size_on_looks_and_the_week_on_pages(self) -> None:
        dialog, settings = self.dialog()
        self.assertEqual(dialog._card_size.get(), "M")  # the default
        self.assertIn("Card size", labels(dialog.page(PAGE_LOOKS)))
        texts = labels(dialog.page(PAGE_PAGES))
        self.assertLess(texts.index("Calendar"), texts.index("Week starts on"))  # one box
        click(find(dialog.page(PAGE_LOOKS), dialog._card_size), "XL")
        click(find(dialog.page(PAGE_PAGES), dialog._first_day), "Sunday")
        await settle()
        self.assertEqual(settings.current.card_size.value, "xl")
        self.assertEqual(settings.current.first_day.value, "Sunday")

    async def test_the_theme_box_comes_first_on_looks(self) -> None:
        dialog, settings = self.dialog()
        self.assertEqual(dialog._theme_mode.get(), "Follow Windows")  # the default
        texts = labels(dialog.page(PAGE_LOOKS))
        self.assertLess(texts.index(THEME_TITLE), texts.index("Card size"))
        self.assertIn(THEME_TEXT, texts)
        click(find(dialog.page(PAGE_LOOKS), dialog._theme_mode), "Light")
        await settle()
        self.assertIs(settings.current.theme_mode, ThemeMode.LIGHT)

    async def test_a_stored_theme_shows(self) -> None:
        dialog, _settings = self.dialog(theme_mode=ThemeMode.DARK)
        self.assertEqual(dialog._theme_mode.get(), "Dark")

    async def test_updates_page_shows_the_version(self) -> None:
        dialog, settings = self.dialog(auto_update=True)
        self.assertIn("Version 2.0.4", labels(dialog.page(PAGE_UPDATES)))
        click(find(dialog.page(PAGE_UPDATES), dialog._auto_update))
        await settle()
        self.assertFalse(settings.current.auto_update)


if __name__ == "__main__":
    unittest.main()
