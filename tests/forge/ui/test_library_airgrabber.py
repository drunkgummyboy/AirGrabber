"""AirGrabber's downloads in Settings › Auto-rename (B-01): "Use AirGrabber's downloads" (on for a
new setup, off for an imported one, stored on Save only; turning it on while On is chosen goes to
Trial first; a suggestion's Add makes it one of the owner's own folders), listing what is watched
and why, or what to choose."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.forge.core.airgrabber_folders import watched_folders
from app.forge.models.preset import DEFAULT_PRESETS
from app.forge.models.settings import AirGrabberDownloads, AirGrabberFolder, ForgeSettings
from app.forge.ui.settings_pages import SECTION_LIBRARY, LibrarySection
from app.i18n import Language
from app.ui.settings_dialog import SettingsDialog
from tests.forge.ui.settings_kit import forge_settings
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.ui.test_settings_library import FakeSettings, labels

SERIES = r"\\nas\video\Series"
MOVIES = r"\\nas\video\Movies"
DONE = AirGrabberFolder(path=r"D:\Torrents", why="qbittorrent", program="qBittorrent")
SUGGESTED = AirGrabberFolder(
    path=r"\\192.168.1.20\video\tv",
    why="shows",
    program="Download Station",
    suggested=True,
    reachable=True,
)
IMPORTED = "MediaForge 1.3.0, 2026-11-02"


def with_airgrabber(
    *folders: AirGrabberFolder, ask: str = "", **changes: Any
) -> ForgeSettings:
    values: dict[str, Any] = {
        "series_folder": SERIES,
        "movies_folder": MOVIES,
        "airgrabber": {"folders": [f.model_dump() for f in folders], "ask": ask},
    }
    values.update(changes)
    return ForgeSettings.model_validate(values)


class _TkCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()


class _Forge:
    """ForgeRuntime's surface, started."""

    def __init__(self, settings: FakeSettings) -> None:
        self.services = SimpleNamespace(
            settings=settings,
            presets=SimpleNamespace(list=mock.AsyncMock(return_value=list(DEFAULT_PRESETS))),
            matches=SimpleNamespace(list=mock.AsyncMock(return_value=[])),
        )
        self.reason = ""

    async def start(self) -> Any:
        return self.services


class SettingsSectionTests(_TkCase):
    def ready(
        self, settings: ForgeSettings
    ) -> tuple[SettingsDialog, LibrarySection, FakeSettings]:
        fake = FakeSettings(settings)
        dialog = forge_settings(self.root, _Forge(fake), section=SECTION_LIBRARY)
        part = dialog.parts[SECTION_LIBRARY]
        assert isinstance(part, LibrarySection)
        return dialog, part, fake

    @staticmethod
    def click_use(part: LibrarySection, on: bool) -> None:
        part._use.set(on)  # pyright: ignore[reportPrivateUsage]
        part._use_toggled()  # pyright: ignore[reportPrivateUsage]

    async def test_lists_airgrabbers_folders_and_why(self) -> None:
        _dialog, part, _fake = self.ready(
            with_airgrabber(DONE, SUGGESTED, watch_folders=[r"E:\Mine"])
        )
        self.assertEqual(
            labels(part.airgrabber_list),
            [
                DONE.path,
                "qBittorrent on this PC saves AirGrabber's downloads here",
                SUGGESTED.path,
                "Suggested: Download Station saves AirGrabber's downloads here. This PC can "
                "reach it.",
            ],
        )
        self.assertEqual(labels(part.watch_list), [r"E:\Mine"])

    async def test_says_what_to_choose(self) -> None:
        _dialog, part, _fake = self.ready(with_airgrabber(ask="finished"))
        self.assertTrue(labels(part.airgrabber_list)[0].startswith("AirGrabber doesn't know"))

    async def test_nothing_of_airgrabbers_while_not_used(self) -> None:
        _dialog, part, _fake = self.ready(
            with_airgrabber(DONE, ask="finished", use_airgrabber_downloads=False)
        )
        self.assertNotIn("AirGrabber doesn't know", "\n".join(labels(part.airgrabber_list)))

    async def test_on_for_a_new_setup_and_nothing_saved_untouched(self) -> None:
        dialog, part, fake = self.ready(with_airgrabber(DONE, SUGGESTED))
        self.assertTrue(part._use.get())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(str(part.use_box.cget("text")), "Use AirGrabber's downloads")
        shown = labels(part.airgrabber_list)
        self.assertEqual(shown[:2], [DONE.path, "qBittorrent on this PC saves AirGrabber's downloads here"])
        self.assertEqual(part.changes(), {})
        self.assertTrue(await dialog._save_all())
        self.assertEqual(fake.updates, [])
        self.assertIsNone(fake.current.use_airgrabber_downloads)

    async def test_off_for_an_imported_one_until_chosen(self) -> None:
        dialog, part, fake = self.ready(with_airgrabber(DONE, imported_from=IMPORTED))
        self.assertFalse(part._use.get())  # pyright: ignore[reportPrivateUsage]
        self.click_use(part, True)
        self.assertEqual(fake.updates, [])  # kept until Save
        self.assertTrue(await dialog._save_all())
        self.assertEqual(fake.updates, [{"use_airgrabber_downloads": True}])
        self.assertEqual(watched_folders(fake.current), [DONE.path])

    async def test_turning_it_on_while_on_is_trial_first(self) -> None:
        stored = with_airgrabber(
            DONE, watch_folders=[r"E:\Mine"], use_airgrabber_downloads=False, automatic="on"
        )
        dialog, part, fake = self.ready(stored)
        self.click_use(part, True)
        self.assertEqual(part.mode.get(), "Trial")
        self.assertIn("Trial first", str(part.note_label.cget("text")))
        self.assertTrue(await dialog._save_all())
        self.assertEqual(fake.updates, [{"use_airgrabber_downloads": True, "automatic": "trial"}])

    async def test_turning_it_off_keeps_on(self) -> None:
        _dialog, part, _fake = self.ready(
            with_airgrabber(DONE, watch_folders=[r"E:\Mine"], automatic="on")
        )
        self.click_use(part, False)
        self.assertEqual(part.mode.get(), "On")
        self.assertEqual(part.changes(), {"use_airgrabber_downloads": False})

    async def test_add_a_suggestion(self) -> None:
        dialog, part, fake = self.ready(with_airgrabber(SUGGESTED, watch_folders=[r"E:\Mine"]))
        self.assertTrue(labels(part.airgrabber_list)[1].startswith("Suggested: Download Station"))
        part.add_suggested(SUGGESTED.path)
        self.assertEqual(part.draft()["watch_folders"], [r"E:\Mine", SUGGESTED.path])
        self.assertIn("Watched already", "\n".join(labels(part.airgrabber_list)))
        self.assertTrue(await dialog._save_all())
        self.assertEqual(fake.updates, [{"watch_folders": [r"E:\Mine", SUGGESTED.path]}])

    async def test_airgrabbers_folders_derived_meanwhile_show(self) -> None:
        _dialog, part, fake = self.ready(with_airgrabber())
        self.assertEqual(labels(part.airgrabber_list), [])
        await fake.update(airgrabber=AirGrabberDownloads(ask="finished"))
        self.assertTrue(labels(part.airgrabber_list)[0].startswith("AirGrabber doesn't know"))
        await fake.update(airgrabber=AirGrabberDownloads(folders=[DONE]))
        self.assertEqual(labels(part.airgrabber_list)[0], DONE.path)
        self.assertEqual(part.changes(), {})

    async def test_without_airgrabbers_folders_the_box_ends_under_the_tick_box(self) -> None:
        # An empty CTkFrame keeps its default 200 units: the empty list is taken out.
        _dialog, part, fake = self.ready(with_airgrabber())
        body = part.use_box.master
        body.update_idletasks()
        self.assertEqual(part.airgrabber_list.winfo_manager(), "")
        empty = body.winfo_reqheight()
        scaling = ctk.ScalingTracker.get_widget_scaling(body)
        self.assertLess(empty, 200 * scaling)  # the heading, its text and the tick box only
        await fake.update(airgrabber=AirGrabberDownloads(folders=[DONE]))
        body.update_idletasks()
        self.assertEqual(part.airgrabber_list.winfo_manager(), "grid")
        self.assertGreater(body.winfo_reqheight(), empty)
        await fake.update(airgrabber=AirGrabberDownloads())  # gone again: no room kept
        body.update_idletasks()
        self.assertEqual(part.airgrabber_list.winfo_manager(), "")
        self.assertEqual(body.winfo_reqheight(), empty)
