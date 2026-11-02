"""MediaForge Settings › Library & watching (app/forge/ui/settings_pages.py) in MediaForge's own
Settings window (app/forge/ui/settings_window.py): the sections from FORGE_SECTIONS, opening at
the section, the form ported from MediaForge's Settings › Library (watched folders, library
folders, Off / Trial / On, the options), changes kept until Save (D9; Cancel / ✕ change
nothing), the folder rules from `library_problems`, waiting for Forge to start and saying when
it can't."""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.forge.models.preset import DEFAULT_PRESETS
from app.forge.models.ignored import IgnoredPath
from app.forge.models.settings import ForgeSettings
from app.forge.ui.settings_naming import SECTION_NAMING
from app.forge.ui.settings_pages import (
    FORGE_GROUP,
    FORGE_SECTIONS,
    GETTING_READY,
    SAVE_FAILED,
    SECTION_LIBRARY,
    LibrarySection,
)
from app.forge.ui.settings_window import ForgeSettingsWindow
from app.i18n import Language
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until

ROOT = Path(__file__).resolve().parents[3]

READY = ForgeSettings(
    watch_folders=[r"D:\Downloads\tv"],
    series_folder=r"\\nas\video\Series",
    movies_folder=r"\\nas\video\Movies",
)
NOT_READY_TEXT = "MediaForge isn't ready yet (test)."


class FakeSettings:
    """ForgeSettingsService's surface: current, update (validated, listeners told), subscribe."""

    def __init__(self, current: ForgeSettings) -> None:
        self.current = current
        self.listeners: list[Callable[[ForgeSettings], None]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail = False

    async def update(self, **changes: Any) -> ForgeSettings:
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("disk full")
        self.updates.append(changes)
        self.current = ForgeSettings.model_validate({**self.current.model_dump(), **changes})
        for listener in list(self.listeners):
            listener(self.current)
        return self.current

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class FakeIgnored:
    """IgnoreService's surface for the section: entries, subscribe, stop_ignoring."""

    def __init__(self, *entries: IgnoredPath) -> None:
        self.entries: tuple[IgnoredPath, ...] = entries
        self.listeners: list[Callable[[tuple[IgnoredPath, ...]], None]] = []
        self.stopped: list[Path] = []

    def subscribe(
        self, listener: Callable[[tuple[IgnoredPath, ...]], None]
    ) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def set(self, *entries: IgnoredPath) -> None:
        """Ignored (or stopped) elsewhere meanwhile."""
        self.entries = entries
        for listener in list(self.listeners):
            listener(entries)

    async def stop_ignoring(self, path: Path) -> bool:
        await asyncio.sleep(0)
        self.stopped.append(path)
        self.set(*(e for e in self.entries if e.path != path))
        return True


def ignored_entry(path: str, folder: bool = False) -> IgnoredPath:
    return IgnoredPath.model_validate({"path": path, "folder": folder, "added_at": 0})


class FakeForge:
    """ForgeRuntime's surface: `services` (None until started), `start()`, `reason`."""

    def __init__(
        self,
        settings: FakeSettings | None,
        *,
        started: bool = True,
        ignored: FakeIgnored | None = None,
    ) -> None:
        # The other MediaForge sections (Naming, Right-click menu) read the presets and matches.
        self._parts = (
            None
            if settings is None
            else SimpleNamespace(
                settings=settings,
                presets=SimpleNamespace(list=mock.AsyncMock(return_value=list(DEFAULT_PRESETS))),
                matches=SimpleNamespace(list=mock.AsyncMock(return_value=[])),
                ignored=ignored if ignored is not None else FakeIgnored(),
            )
        )
        self.services = self._parts if started else None
        self.reason = ""
        self.gate: asyncio.Future[None] | None = None
        self.starts = 0

    async def start(self) -> Any:
        self.starts += 1
        if self.gate is not None:
            await asyncio.shield(self.gate)  # as ForgeRuntime: a cancelled caller doesn't stop it
        if self._parts is None:
            self.reason = NOT_READY_TEXT
        self.services = self._parts
        return self._parts


def labels(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel) and child.winfo_manager():
            found.append(str(child.cget("text")))
        if child.winfo_manager():
            found += labels(child)
    return found


class LibrarySettingsCase(ForgeAsyncTestCase):
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
        self.chosen: list[str] = []  # what the fake folder picker answers, in turn
        self.asked: list[tuple[str, str]] = []

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # the dialog's Tk variables, on the main thread

    def dialog(
        self, forge: FakeForge, *, section: str | None = SECTION_LIBRARY
    ) -> ForgeSettingsWindow:
        return ForgeSettingsWindow(self.root, cast(Any, forge), section=section)

    def section(self, dialog: ForgeSettingsWindow) -> LibrarySection:
        part = dialog.parts[SECTION_LIBRARY]
        assert isinstance(part, LibrarySection)
        part.ask_folder = self._ask
        return part

    def _ask(self, title: str, start: str) -> str:
        self.asked.append((title, start))
        return self.chosen.pop(0) if self.chosen else ""

    def ready(
        self, settings: ForgeSettings = READY
    ) -> tuple[ForgeSettingsWindow, LibrarySection, FakeSettings]:
        fake = FakeSettings(settings)
        dialog = self.dialog(FakeForge(fake))
        return dialog, self.section(dialog), fake


class LibrarySettingsTests(LibrarySettingsCase):
    # --- the sections and this one ---------------------------------------------------------------

    async def test_the_sidebar_lists_forges_sections_and_opens_at_library(self) -> None:
        dialog, _part, _fake = self.ready()
        self.assertEqual(dialog.sections, list(FORGE_SECTIONS))
        # The group's label, then an icon and a name per section.
        self.assertEqual(labels(dialog._sidebar)[0], "MEDIAFORGE")
        self.assertEqual(len(labels(dialog._sidebar)), 1 + 2 * len(FORGE_SECTIONS))
        self.assertEqual(dialog.section, SECTION_LIBRARY)
        self.assertTrue(dialog._items[SECTION_LIBRARY].selected)
        texts = labels(dialog.page(SECTION_LIBRARY))
        self.assertEqual(texts[0], "Library & watching")
        self.assertTrue(texts[1])
        dialog.show_section(SECTION_NAMING)
        self.assertEqual(dialog.section, SECTION_NAMING)
        dialog.show_section(SECTION_LIBRARY)  # an open window asked for the section again
        self.assertEqual(dialog.section, SECTION_LIBRARY)
        self.assertEqual(FORGE_GROUP, "MediaForge")

    async def test_shows_the_stored_settings(self) -> None:
        dialog, part, _fake = self.ready()
        texts = labels(dialog.page(SECTION_LIBRARY))
        for folder in (*READY.watch_folders, READY.series_folder, READY.movies_folder):
            self.assertIn(folder, texts)
        self.assertNotIn(t_en(GETTING_READY), texts)
        self.assertEqual(part.mode.get(), "Off")
        self.assertEqual(str(part.mode.cget("state")), "normal")
        self.assertEqual(part.draft()["clean_downloads"], True)
        self.assertEqual(part.changes(), {})

    async def test_nothing_chosen_yet(self) -> None:
        dialog, part, _fake = self.ready(ForgeSettings())
        texts = labels(dialog.page(SECTION_LIBRARY))
        self.assertIn("None yet", texts)
        self.assertEqual(texts.count("Not chosen yet"), 2)
        # What is missing shows under the switch, which can't leave Off yet.
        self.assertIn("Choose a download folder to watch.", texts[-2].splitlines())
        self.assertEqual(str(part.mode.cget("state")), "disabled")

    # --- changes wait for Save -------------------------------------------------------------------

    async def test_changes_are_stored_on_save_only(self) -> None:
        dialog, part, fake = self.ready()
        self.chosen = [r"E:\More downloads", r"\\nas\video\Films"]
        part.add_watch_folder()
        part.choose_library("movies")
        part._needs.set(False)
        self.assertEqual(fake.updates, [])  # nothing stored yet
        self.assertEqual(
            part.changes(),
            {
                "watch_folders": [r"D:\Downloads\tv", r"E:\More downloads"],
                "movies_folder": r"\\nas\video\Films",
                "notify_needs": False,
            },
        )
        self.assertTrue(await dialog._save_all())
        # Only what changed; renaming on its own stays off.
        self.assertEqual(
            fake.updates,
            [
                {
                    "watch_folders": [r"D:\Downloads\tv", r"E:\More downloads"],
                    "movies_folder": r"\\nas\video\Films",
                    "notify_needs": False,
                }
            ],
        )
        self.assertEqual(fake.current.automatic, "off")
        self.assertEqual(part.changes(), {})  # what was stored is what the form shows now

    async def test_cancel_changes_nothing(self) -> None:
        dialog, part, fake = self.ready()
        part.remove_watch_folder(READY.watch_folders[0])
        part.set_mode("trial")
        dialog.destroy()  # Cancel, ✕ and Escape all destroy the dialog
        await pump(self.root, 0.1)
        self.assertEqual(fake.updates, [])
        self.assertEqual(fake.listeners, [])  # it stopped listening

    async def test_an_unchanged_form_saves_nothing(self) -> None:
        dialog, _part, fake = self.ready()
        self.assertTrue(await dialog._save_all())
        self.assertEqual(fake.updates, [])

    async def test_add_and_remove_watched_folders(self) -> None:
        dialog, part, _fake = self.ready()
        self.chosen = ["d:/downloads/TV/", ""]  # the same folder written otherwise, then Cancel
        part.add_watch_folder()
        part.add_watch_folder()
        self.assertEqual(part.draft()["watch_folders"], [r"D:\Downloads\tv"])
        self.assertEqual(self.asked[0][0], "Choose a download folder to watch")
        part.remove_watch_folder(r"D:\Downloads\tv")
        self.assertEqual(part.draft()["watch_folders"], [])
        self.assertIn("None yet", labels(dialog.page(SECTION_LIBRARY)))

    async def test_browse_starts_in_the_folder_shown(self) -> None:
        _dialog, part, _fake = self.ready()
        self.chosen = ["F:/Series"]
        part.choose_library("series")
        self.assertEqual(self.asked, [("Choose the Series library folder", READY.series_folder)])
        self.assertEqual(part.draft()["series_folder"], r"F:\Series")
        self.assertEqual(part.folder_labels["series"].cget("text"), r"F:\Series")

    # --- Off / Trial / On ------------------------------------------------------------------------

    async def test_on_straight_from_off_becomes_trial_first(self) -> None:
        dialog, part, fake = self.ready()
        part._mode_clicked("On")
        self.assertEqual(part.mode.get(), "Trial")
        self.assertIn("Trial first", str(part.note_label.cget("text")))
        self.assertTrue(part.note_label.winfo_manager())
        self.assertTrue(await dialog._save_all())
        self.assertEqual(fake.updates, [{"automatic": "trial"}])
        part.set_mode("on")  # from a stored Trial, On is On
        self.assertEqual(part.mode.get(), "On")
        self.assertEqual(str(part.note_label.cget("text")), "")

    async def test_the_switch_can_always_go_back_to_off(self) -> None:
        _dialog, part, _fake = self.ready(READY.model_copy(update={"automatic": "on"}))
        part.remove_watch_folder(READY.watch_folders[0])  # now something is missing
        self.assertEqual(str(part.mode.cget("state")), "normal")
        part.set_mode("off")
        self.assertEqual(str(part.mode.cget("state")), "disabled")

    # --- the folder rules on Save ----------------------------------------------------------------

    async def test_a_watched_folder_inside_a_library_folder_never_saves(self) -> None:
        dialog, part, fake = self.ready()
        dialog.show_section(SECTION_NAMING)
        self.chosen = [r"\\nas\video\Series\incoming"]
        part.add_watch_folder()
        self.assertFalse(await dialog._save_all())
        self.assertEqual(fake.updates, [])
        self.assertEqual(dialog.section, SECTION_LIBRARY)  # the page with the problem shows
        self.assertIn("inside a library folder", str(dialog._error.cget("text")))

    async def test_trial_needs_every_folder(self) -> None:
        dialog, part, fake = self.ready(READY.model_copy(update={"automatic": "trial"}))
        self.assertTrue(await dialog._save_all())  # unchanged: never stops Save
        part.remove_watch_folder(READY.watch_folders[0])
        self.assertFalse(await dialog._save_all())
        self.assertEqual(str(dialog._error.cget("text")), "Choose a download folder to watch.")
        part.set_mode("off")  # Off with a folder missing is fine
        self.assertTrue(await dialog._save_all())
        self.assertEqual(fake.updates, [{"watch_folders": [], "automatic": "off"}])

    async def test_a_failed_save_says_so_and_keeps_the_dialog(self) -> None:
        dialog, part, fake = self.ready()
        part._summary.set(False)
        fake.fail = True
        with self.assertLogs("app.forge.ui.settings_pages", "ERROR") as logged:
            self.assertFalse(await dialog._save_all())
        self.assertEqual(str(dialog._error.cget("text")), t_en(SAVE_FAILED))
        self.assertEqual(logged.records[0].getMessage().count("disk full"), 0)  # the type only
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(str(dialog._save_button.cget("state")), "normal")

    # --- saved elsewhere meanwhile ---------------------------------------------------------------

    async def test_a_change_saved_elsewhere_shows_unless_changed_here(self) -> None:
        _dialog, part, fake = self.ready()
        part._clean.set(False)  # changed here
        await fake.update(automatic="trial", clean_downloads=True, kept_back=["x"])
        self.assertEqual(part.mode.get(), "Trial")  # the Forge window's switch shows
        self.assertEqual(part.draft()["clean_downloads"], False)  # the form's change stays
        self.assertEqual(part.changes(), {"clean_downloads": False})

    # --- before Forge has started ----------------------------------------------------------------

    async def test_waits_for_forge_to_start(self) -> None:
        fake = FakeSettings(READY)
        forge = FakeForge(fake, started=False)
        forge.gate = asyncio.get_running_loop().create_future()
        dialog = self.dialog(forge)
        part = self.section(dialog)
        await pump(self.root, 0.05)
        self.assertEqual(forge.starts, len(FORGE_SECTIONS))  # each section asks once
        self.assertFalse(part.ready)
        self.assertIn("Getting MediaForge ready…", labels(dialog.page(SECTION_LIBRARY)))
        self.assertNotIn(READY.series_folder, labels(dialog.page(SECTION_LIBRARY)))
        self.assertTrue(await dialog._save_all())  # nothing of Forge's to store yet
        forge.gate.set_result(None)
        await pump_until(self.root, lambda: part.ready)
        texts = labels(dialog.page(SECTION_LIBRARY))
        self.assertNotIn("Getting MediaForge ready…", texts)
        self.assertIn(READY.series_folder, texts)

    async def test_not_ready_says_why(self) -> None:
        forge = FakeForge(None, started=False)  # start() gives no services: NOT_READY
        dialog = self.dialog(forge)
        part = self.section(dialog)
        await pump_until(self.root, lambda: NOT_READY_TEXT in labels(dialog.page(SECTION_LIBRARY)))
        self.assertFalse(part.ready)
        self.assertIsNone(part.problem())
        self.assertTrue(await dialog._save_all())

    async def test_closing_while_waiting_stops_waiting(self) -> None:
        forge = FakeForge(FakeSettings(READY), started=False)
        forge.gate = asyncio.get_running_loop().create_future()
        dialog = self.dialog(forge)
        part = self.section(dialog)
        await pump(self.root, 0.05)
        task = part._task
        assert task is not None
        dialog.destroy()
        await pump(self.root, 0.05)
        self.assertTrue(task.cancelled())
        self.assertFalse(forge.gate.cancelled())  # Forge's own start goes on (it is shielded)

    # --- Dutch, the foot ------------------------------------------------------------------------

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        dialog, part, _fake = self.ready()
        texts = labels(dialog.page(SECTION_LIBRARY))
        self.assertEqual(texts[0], "Bibliotheek & bewaken")
        self.assertIn("Downloadmappen om te bewaken", texts)
        self.assertEqual(part.mode.get(), "Uit")
        self.assertIn("MEDIAFORGE", labels(dialog._sidebar))

    async def test_the_foot_is_hidden_until_used(self) -> None:
        dialog, part, _fake = self.ready()
        self.assertEqual(part.foot.winfo_manager(), "")
        ctk.CTkLabel(part.foot, text="Remove the old MediaForge").pack()
        part.foot.grid()  # F-27 shows it under the boxes
        self.assertEqual(labels(dialog.page(SECTION_LIBRARY))[-1], "Remove the old MediaForge")


class IgnoredBoxTests(LibrarySettingsCase):
    """The "Ignored" box under "Download folders to watch": what Auto-rename leaves alone, each
    with "Stop ignoring" (stored on Save, D9); "Nothing ignored." without a gap when empty."""

    COURSE = r"D:\Downloads\tv\Python Course"
    CLIP = r"D:\Downloads\tv\Holiday.Clip.mp4"

    def ignored_ready(
        self, ignored: FakeIgnored
    ) -> tuple[ForgeSettingsWindow, LibrarySection, FakeSettings]:
        fake = FakeSettings(READY)
        dialog = self.dialog(FakeForge(fake, ignored=ignored))
        return dialog, self.section(dialog), fake

    async def test_empty_says_nothing_ignored_without_a_gap(self) -> None:
        _dialog, part, _fake = self.ignored_ready(FakeIgnored())
        self.assertEqual(labels(part.ignored_list), ["Nothing ignored."])
        box = part.ignored_list.master
        box.update_idletasks()
        scaling = ctk.ScalingTracker.get_widget_scaling(box)
        # The heading, its line and "Nothing ignored." only (an empty CTkFrame would keep 200).
        self.assertLess(box.winfo_reqheight(), 140 * scaling)

    async def test_the_box_follows_download_folders_to_watch(self) -> None:
        dialog, _part, _fake = self.ignored_ready(FakeIgnored())
        texts = labels(dialog.page(SECTION_LIBRARY))
        watch = texts.index("Download folders to watch")
        ignored = texts.index("Ignored")
        self.assertLess(watch, ignored)
        self.assertLess(ignored, texts.index("AirGrabber's downloads"))
        self.assertEqual(texts[ignored + 1], "Files and folders Auto-rename leaves alone.")

    async def test_lists_entries_and_stop_ignoring_waits_for_save(self) -> None:
        source = FakeIgnored(ignored_entry(self.COURSE, folder=True), ignored_entry(self.CLIP))
        dialog, part, fake = self.ignored_ready(source)
        self.assertEqual(labels(part.ignored_list), [f"📁  {self.COURSE}", self.CLIP])
        buttons = [w for w in part.ignored_list.winfo_children() if isinstance(w, ctk.CTkButton)]
        self.assertEqual([b.cget("text") for b in buttons], ["Stop ignoring", "Stop ignoring"])
        buttons[0].invoke()
        self.assertEqual(labels(part.ignored_list), [self.CLIP])
        self.assertEqual(source.stopped, [])  # kept until Save
        self.assertEqual(part.unignored, [Path(self.COURSE)])
        self.assertTrue(await dialog._save_all())
        self.assertEqual(source.stopped, [Path(self.COURSE)])
        self.assertEqual(fake.updates, [])  # no setting changed
        self.assertEqual(part.unignored, [])
        self.assertEqual(labels(part.ignored_list), [self.CLIP])

    async def test_cancel_keeps_them_ignored(self) -> None:
        source = FakeIgnored(ignored_entry(self.CLIP))
        dialog, part, _fake = self.ignored_ready(source)
        part.stop_ignoring(Path(self.CLIP))
        self.assertEqual(labels(part.ignored_list), ["Nothing ignored."])
        dialog.destroy()
        await pump(self.root, 0.05)
        self.assertEqual(source.stopped, [])
        self.assertEqual(source.listeners, [])  # it stopped listening

    async def test_ignored_meanwhile_shows(self) -> None:
        source = FakeIgnored()
        _dialog, part, _fake = self.ignored_ready(source)
        source.set(ignored_entry(self.CLIP))
        self.assertEqual(labels(part.ignored_list), [self.CLIP])

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        dialog, part, _fake = self.ignored_ready(FakeIgnored(ignored_entry(self.CLIP)))
        texts = labels(dialog.page(SECTION_LIBRARY))
        self.assertIn("Genegeerd", texts)
        self.assertIn("Bestanden en mappen die automatisch hernoemen met rust laat.", texts)
        buttons = [w for w in part.ignored_list.winfo_children() if isinstance(w, ctk.CTkButton)]
        self.assertEqual(buttons[0].cget("text"), "Niet meer negeren")
        part.stop_ignoring(Path(self.CLIP))
        self.assertEqual(labels(part.ignored_list), ["Niets genegeerd."])


class ImportCostTests(ForgeAsyncTestCase):
    def test_airgrabbers_settings_load_none_of_forges_settings(self) -> None:
        """AirGrabber's Settings is imported with the main window at start: it has no
        MediaForge sections any more, so none of Forge's settings pages, core, forge.db or
        guessit come along (INTEGRATION.md 3.6)."""
        code = (
            "import sys\n"
            "import app.ui.settings_dialog\n"
            "bad = [m for m in sys.modules if m == 'guessit' or m.startswith('app.forge.db')\n"
            "       or m.startswith('app.forge.ui.settings')\n"
            "       or m in ('app.forge.core.auto', 'app.forge.services',\n"
            "                'app.forge.ui.library_page')]\n"
            "print(bad)\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")


def t_en(text: str) -> str:
    return i18n.t(text)
