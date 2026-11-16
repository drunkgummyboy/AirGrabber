"""Settings → Library's "Your own folders" box (app/ui/folder_library_settings.py,
mockups/folder-library.html states 1–3): MediaForge's folders suggested and not saved, "Use these
folders" switching on, the counts / couldn't-recognise / can't-reach lines, "Stop using folders"
asked first, the one line while Plex is connected; and the Library section in the Settings
window (the boxes' order, the search still finding Plex). On a hidden Tk root with a stand-in
FolderLibraryService; nothing reads a folder."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models import Settings
from app.models.folder_library import FolderLibraryStatus, SwitchOutcome
from app.services.playback import LibraryFolders
from app.ui import theme
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.settings_layout import SettingsPage
from tests.test_backup_dialog import FakeProwlarr, FakeRepository, FakeShortcut, buttons, texts
from tests.test_plex_ui import CONNECTED, NOW, FakePlex, FakeSettings, TkTestCase

MOVIES = r"\\NAS\Media\Movies"
SERIES = r"\\NAS\Media\Series"
TOKEN = "made-up-plex-token"


class FakeFolders:
    """FolderLibraryService's face for the box: a status, what the buttons asked, and a
    switch-on that saves like the real one (only once it is on)."""

    def __init__(
        self,
        settings: FakeSettings,
        status: FolderLibraryStatus = FolderLibraryStatus(),
        outcome: SwitchOutcome = "on",
    ) -> None:
        self.settings = settings
        self.value = status
        self.outcome: SwitchOutcome = outcome
        self.listeners: list[Any] = []
        self.calls: list[tuple[Any, ...]] = []
        self.gate: asyncio.Event | None = None

    @property
    def status(self) -> FolderLibraryStatus:
        return self.value

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None

    def set(self, status: FolderLibraryStatus) -> None:
        self.value = status
        for listener in list(self.listeners):
            listener()

    async def switch_on(self, films: Any, series: Any) -> SwitchOutcome:
        self.calls.append(("on", tuple(films), tuple(series)))
        self.set(self.value.model_copy(update={"checking": True}))
        if self.gate is not None:
            await self.gate.wait()
        if self.outcome == "on":
            await self.settings.update(
                folder_library_on=True, film_folders=tuple(films), series_folders=tuple(series)
            )
            self.set(FolderLibraryStatus(on=True, films=2, shows=1, episodes=5, checked_at=NOW))
        else:
            unreachable = (*films, *series) if self.outcome == "unreachable" else ()
            slow = (*films, *series) if self.outcome == "slow" else ()
            self.set(
                self.value.model_copy(
                    update={"checking": False, "unreachable": unreachable, "slow": slow}
                )
            )
        return self.outcome

    async def switch_off(self) -> None:
        self.calls.append(("off",))
        await self.settings.update(folder_library_on=False)
        self.set(FolderLibraryStatus())

    def check_soon(self, delay_s: float = 0) -> None:
        self.calls.append(("check",))


class FolderBoxTests(TkTestCase):
    async def box(
        self,
        folders: FakeFolders,
        settings: FakeSettings,
        *,
        suggestion: LibraryFolders | None = None,
        picked: str = "",
        shown: bool = True,
    ) -> Any:
        from app.ui.folder_library_settings import FolderLibraryBox

        frame = ctk.CTkFrame(self.root)
        self.keep(frame)
        page = SettingsPage(frame, "Library", "A note")
        page.frame.pack()
        self.asked = 0
        self.states: list[str] = []

        async def suggest() -> LibraryFolders | None:
            self.asked += 1
            return suggestion

        box = FolderLibraryBox(
            page,
            folders,
            settings,  # type: ignore[arg-type]
            suggest=suggest,
            pick_folder=lambda _owner: picked,
            clock=lambda: NOW,
            on_state=self.states.append,
        )
        await box.loaded()
        if shown:
            box.page_shown()  # Settings shows the Library page
            await box.loaded()
        await self.settle()
        return box

    async def test_off_suggests_mediaforges_folders_without_saving(self) -> None:
        from app.ui.folder_library_settings import (
            ADD_TEXT,
            INTRO_TEXT,
            OFF_TEXT,
            SUGGESTED_TEXT,
            USE_TEXT,
        )

        settings = FakeSettings()
        folders = FakeFolders(settings)
        suggestion = LibraryFolders(series=SERIES, movies=MOVIES)
        # MediaForge is asked only once the Library page shows (asking may start it).
        unseen = await self.box(folders, settings, suggestion=suggestion, shown=False)
        self.assertEqual((self.asked, unseen.films, unseen.series), (0, [], []))
        unseen.page_shown()
        await unseen.loaded()
        self.assertEqual((self.asked, unseen.films), (1, [MOVIES]))
        box = await self.box(folders, settings, suggestion=suggestion, picked="D:\\Films")
        self.assertEqual(box.state, "off")
        self.assertEqual(self.states[-1], "off")
        self.assertEqual((box.films, box.series), ([MOVIES], [SERIES]))
        shown = texts(box.box.frame)
        for text in (OFF_TEXT, INTRO_TEXT, MOVIES, SERIES, SUGGESTED_TEXT):
            self.assertIn(text, shown)
        found = buttons(box.box.frame)
        self.assertIn(USE_TEXT, found)
        self.assertEqual(tuple(found[USE_TEXT].cget("fg_color")), tuple(theme.ACCENT))  # accent
        # "+ Add a folder" (Windows' folder picker) and ✕ change the list, nothing is saved.
        assert box.add_buttons["film"] is not None
        self.assertEqual(box.add_buttons["film"].cget("text"), ADD_TEXT)
        box.add_buttons["film"].invoke()
        await self.settle()
        self.assertEqual(box.films, [MOVIES, "D:\\Films"])
        box.remove_buttons["film"][0].invoke()
        await self.settle()
        self.assertEqual(box.films, ["D:\\Films"])
        self.assertEqual(settings.changes, [])
        self.assertEqual(folders.calls, [])
        # Folders set before (switched off later) are shown instead; MediaForge isn't asked.
        saved = FakeSettings(Settings(film_folders=("D:\\Films",)))
        again = await self.box(FakeFolders(saved), saved, suggestion=suggestion)
        self.assertEqual((again.films, again.series), (["D:\\Films"], []))
        self.assertEqual(self.asked, 0)
        self.assertNotIn(SUGGESTED_TEXT, texts(again.box.frame))

    async def test_use_these_folders_switches_on(self) -> None:
        from app.ui.folder_library_settings import (
            ON_TEXT,
            READING_TEXT,
            UNREACHABLE_ALL_TEXT,
            USE_TEXT,
        )

        settings = FakeSettings()
        folders = FakeFolders(settings)
        folders.gate = asyncio.Event()
        box = await self.box(folders, settings, suggestion=LibraryFolders(SERIES, MOVIES))
        assert box.use_button is not None
        box.use_button.invoke()
        await self.settle()
        self.assertEqual(folders.calls, [("on", (MOVIES,), (SERIES,))])
        # Reading: the button waits, nothing switched on yet.
        assert box.use_button is not None
        self.assertEqual(box.use_button.cget("text"), READING_TEXT)
        self.assertEqual(box.use_button.cget("state"), "disabled")
        self.assertEqual(settings.changes, [])
        folders.gate.set()
        await box.loaded()
        await self.settle()
        self.assertEqual(box.state, "on")
        self.assertIn(ON_TEXT, texts(box.box.frame))
        self.assertTrue(settings.current.folder_library_on)
        self.assertEqual(settings.current.film_folders, (MOVIES,))

        # No folder could be reached: a plain line, nothing switched on, the folders kept.
        settings = FakeSettings()
        folders = FakeFolders(settings, outcome="unreachable")
        box = await self.box(folders, settings, suggestion=LibraryFolders(SERIES, MOVIES))
        assert box.use_button is not None
        box.use_button.invoke()
        await box.loaded()
        await self.settle()
        self.assertEqual(box.state, "off")
        self.assertIn(UNREACHABLE_ALL_TEXT, texts(box.box.frame))
        self.assertEqual((box.films, box.series), ([MOVIES], [SERIES]))
        self.assertIn(USE_TEXT, buttons(box.box.frame))
        self.assertEqual(settings.changes, [])

    async def test_on_shows_counts_and_unreachable(self) -> None:
        from app.ui.folder_library_settings import (
            CHECK_NOW_TEXT,
            ON_TEXT,
            STOP_TEXT,
            counts_text,
        )

        settings = FakeSettings(
            Settings(folder_library_on=True, film_folders=(MOVIES, "D:\\Films"), series_folders=(SERIES,))
        )
        status = FolderLibraryStatus(
            on=True,
            films=412,
            shows=38,
            episodes=1906,
            unrecognised=tuple(f"D:\\Films\\clip{n}.mp4" for n in range(6)),
            unreachable=(MOVIES,),
            checked_at=NOW - timedelta(minutes=3),
        )
        folders = FakeFolders(settings, status)
        box = await self.box(folders, settings)
        self.assertEqual(box.state, "on")
        self.assertEqual(self.states[-1], "on")
        shown = texts(box.box.frame)
        self.assertIn(ON_TEXT, shown)
        self.assertIn("412 films · 38 series · 1,906 episodes · checked 3 min ago", shown)
        self.assertIn("6 files couldn't be recognised: they're listed on the Library page.", shown)
        self.assertIn(
            "Can't reach \\\\NAS\\Media\\Movies: what was found there last time still counts.",
            shown,
        )
        self.assertEqual((box.films, box.series), ([MOVIES, "D:\\Films"], [SERIES]))
        self.assertEqual(
            counts_text(FolderLibraryStatus(films=1, shows=1, episodes=1, checked_at=NOW), NOW),
            "1 film · 1 series · 1 episode · checked just now",
        )
        found = buttons(box.box.frame)
        self.assertIn(STOP_TEXT, found)
        found[CHECK_NOW_TEXT].invoke()
        self.assertEqual(folders.calls, [("check",)])
        # One file: its own words.
        folders.set(status.model_copy(update={"unrecognised": ("D:\\Films\\a.mp4",), "unreachable": ()}))
        await box.loaded()
        await self.settle()
        shown = texts(box.box.frame)
        self.assertIn("1 file couldn't be recognised: it's listed on the Library page.", shown)
        self.assertFalse(any(text.startswith("Can't reach") for text in shown))

    async def test_stop_using_folders_asks_first(self) -> None:
        from app.ui.folder_library_settings import STOP_TEXT, STOP_TITLE, STOP_QUESTION

        settings = FakeSettings(Settings(folder_library_on=True, film_folders=(MOVIES,)))
        folders = FakeFolders(settings, FolderLibraryStatus(on=True, films=3, checked_at=NOW))
        box = await self.box(folders, settings)
        buttons(box.box.frame)[STOP_TEXT].invoke()
        await self.settle()
        dialog = box.confirm
        assert isinstance(dialog, ConfirmDialog)
        self.keep(dialog)
        self.assertEqual(dialog.title(), STOP_TITLE)
        self.assertEqual(dialog.text.cget("text"), STOP_QUESTION)
        self.assertEqual(
            f"{STOP_TITLE} {STOP_QUESTION}",
            "Stop using your folders? The marks and the Library page go until you switch them "
            "on again. No file is touched.",
        )
        self.assertEqual(folders.calls, [])  # asked, not done
        dialog.confirm()
        await self.settle()
        await box.loaded()
        self.assertEqual(folders.calls, [("off",)])
        self.assertEqual(box.state, "off")
        self.assertEqual(box.films, [MOVIES])  # the folders stay, for switching on again

    async def test_plex_connected_shows_one_line(self) -> None:
        from app.ui.folder_library_settings import PLEX_LINE_TEXT

        settings = FakeSettings(
            Settings(plex_token=TOKEN, folder_library_on=True, film_folders=(MOVIES,))
        )
        folders = FakeFolders(settings)
        box = await self.box(folders, settings, suggestion=LibraryFolders(SERIES, MOVIES))
        self.assertEqual(box.state, "plex")
        self.assertEqual(self.states[-1], "plex")
        self.assertEqual(texts(box.box.frame), [PLEX_LINE_TEXT])
        self.assertEqual(buttons(box.box.frame), {})
        self.assertEqual(self.asked, 0)
        # Plex disconnected: the folders count again (the service tells its listeners).
        settings.current = Settings(folder_library_on=True, film_folders=(MOVIES,))
        folders.set(FolderLibraryStatus(on=True, films=3, checked_at=NOW))
        await box.loaded()
        await self.settle()
        self.assertEqual(box.state, "on")


class FixRoundTests(TkTestCase):
    """Task 5's fix round: what the box shows is what is saved, no edits during a switch, and
    Settings closed mid-switch doesn't stop it."""

    async def box(self, folders: Any, settings: Any, **extra: Any) -> Any:
        from app.ui.folder_library_settings import FolderLibraryBox

        self.frame = ctk.CTkFrame(self.root)
        self.keep(self.frame)
        page = SettingsPage(self.frame, "Library", "A note")
        page.frame.pack()
        box = FolderLibraryBox(page, folders, settings, clock=lambda: NOW, **extra)
        box.page_shown()
        await box.loaded()
        await self.settle()
        return box

    async def test_an_unsaved_change_while_on_shows_the_saved_folders(self) -> None:
        from app.ui.folder_library_settings import UNREACHABLE_CHANGE_TEXT

        for outcome in ("unreachable", "overtaken"):
            with self.subTest(outcome=outcome):
                saved = Settings(folder_library_on=True, film_folders=(MOVIES, "D:\\Films"))
                settings = FakeSettings(saved)
                folders = FakeFolders(settings, FolderLibraryStatus(on=True, films=3, checked_at=NOW))
                folders.outcome = outcome
                box = await self.box(folders, settings)
                box.remove_buttons["film"][1].invoke()  # D:\Films out: only the asleep NAS left
                await box.loaded()
                await self.settle()
                self.assertEqual(folders.calls, [("on", (MOVIES,), ())])
                self.assertEqual(box.state, "on")
                self.assertEqual(box.films, [MOVIES, "D:\\Films"])  # as saved, as read
                self.assertEqual(settings.changes, [])
                shown = texts(box.box.frame)
                self.assertIn("D:\\Films", shown)
                self.assertEqual(UNREACHABLE_CHANGE_TEXT in shown, outcome == "unreachable")

    async def test_no_folder_changes_while_reading(self) -> None:
        settings = FakeSettings()
        folders = FakeFolders(settings)
        folders.gate = asyncio.Event()
        box = await self.box(
            folders, settings, suggest=None, pick_folder=lambda _owner: "D:\\Other"
        )
        box.films, box.series = [MOVIES], [SERIES]
        box.show()
        assert box.use_button is not None
        box.use_button.invoke()
        await self.settle()
        await asyncio.sleep(0.05)
        self.root.update()
        add = box.add_buttons["film"]
        assert add is not None
        self.assertEqual(add.cget("state"), "disabled")
        self.assertTrue(all(b.cget("state") == "disabled" for b in box.remove_buttons["film"]))
        box.add("film")  # even called: nothing changes
        box.remove("series", 0)
        self.assertEqual((box.films, box.series), ([MOVIES], [SERIES]))
        folders.gate.set()
        await box.loaded()
        await self.settle()
        self.assertEqual(box.state, "on")
        self.assertEqual(box.films, [MOVIES])
        add = box.add_buttons["film"]
        assert add is not None
        self.assertEqual(add.cget("state"), "normal")

    async def test_settings_reopened_mid_switch_shows_the_saved_folders(self) -> None:
        # The switch outlives its Settings window: a box built meanwhile shows neither an old
        # copy nor MediaForge's suggestion, and the saved folders once it's on.
        settings = FakeSettings()
        folders = FakeFolders(settings)
        folders.gate = asyncio.Event()
        asked: list[int] = []

        async def suggest() -> LibraryFolders:
            asked.append(1)
            return LibraryFolders(series=SERIES, movies="D:\\Suggested")

        first = await self.box(folders, settings, suggest=None)
        first_frame = self.frame
        first.films = [MOVIES]
        first.use()
        await self.settle()
        await asyncio.sleep(0.05)
        first_frame.destroy()  # Settings closed; the service's switch goes on (still checking)
        await self.settle()
        second = await self.box(folders, settings, suggest=suggest)  # Settings opened again
        self.assertTrue(second.busy)
        self.assertEqual((second.state, second.films, second.series), ("off", [], []))
        self.assertEqual(asked, [])  # no suggestion while a switch runs
        # The switch ends as the service's does: saved, then on.
        await settings.update(folder_library_on=True, film_folders=(MOVIES,), series_folders=())
        folders.set(FolderLibraryStatus(on=True, films=2, checked_at=NOW))
        await second.loaded()
        await self.settle()
        self.assertEqual((second.state, second.films, second.series), ("on", [MOVIES], []))
        self.assertFalse(second.suggested)
        self.assertEqual(asked, [])
        self.assertNotIn("D:\\Suggested", texts(second.box.frame))
        self.assertIn(MOVIES, texts(second.box.frame))

    async def test_settings_closed_mid_switch_still_switches_on(self) -> None:
        # The real service: the box's task is cancelled with the box, the switch isn't.
        import tempfile
        from pathlib import Path
        from typing import cast

        from app.database import Database
        from app.models.plex import PlexScan
        from app.services.folder_library.service import FolderLibraryService
        from app.services.library import LibraryService
        from app.services.settings import SettingsService
        from app.services.tmdb import TMDBClient
        from tests.test_folder_library_service import FakeLibrary, make_tmdb

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        films = root / "Films"
        (films / "Dune (2021)").mkdir(parents=True)
        (films / "Dune (2021)" / "Dune (2021) - 1080p.mkv").write_bytes(b"")
        db = await Database.open(root / "airgrabber.db")
        self.addAsyncCleanup(db.close)
        settings = await SettingsService.load(db.settings)
        library = FakeLibrary()
        service = FolderLibraryService(
            settings, db.library_files, db.plex, cast(TMDBClient, make_tmdb()),
            cast(LibraryService, library), quiet_s=0.0,
        )
        self.addAsyncCleanup(service.aclose)
        storing, go_on = asyncio.Event(), asyncio.Event()
        real_replace = db.plex.replace

        async def replace(scan_: PlexScan, when: Any) -> None:
            storing.set()
            await go_on.wait()
            await real_replace(scan_, when)

        with mock.patch.object(db.plex, "replace", replace):
            box = await self.box(service, settings, suggest=None)
            box.films = [str(films)]
            box.use()
            for _ in range(500):
                if storing.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(storing.is_set())
            self.frame.destroy()  # Settings closed (Save) mid-switch
            await asyncio.sleep(0.05)
            go_on.set()
            for _ in range(500):
                if not service.status.checking:
                    break
                await asyncio.sleep(0.01)
        self.assertTrue(settings.current.folder_library_on)
        self.assertEqual(settings.current.film_folders, (str(films),))
        self.assertEqual(library.notified, 1)  # the pages heard it
        self.assertTrue(service.status.on)


class SlowFolderTests(TkTestCase):
    """The final review's finding 1: a folder that answers but isn't read in time is never
    called unreachable."""

    async def box(self, folders: Any, settings: Any, **extra: Any) -> Any:
        from app.ui.folder_library_settings import FolderLibraryBox

        self.frame = ctk.CTkFrame(self.root)
        self.keep(self.frame)
        page = SettingsPage(self.frame, "Library", "A note")
        page.frame.pack()
        box = FolderLibraryBox(page, folders, settings, clock=lambda: NOW, **extra)
        box.page_shown()
        await box.loaded()
        await self.settle()
        return box

    async def test_a_slow_folder_has_its_own_words(self) -> None:
        from app.ui.folder_library_settings import (
            CANT_REACH_TEXT,
            SLOW_ALL_TEXT,
            SLOW_CHANGE_TEXT,
            SLOW_TEXT,
            UNREACHABLE_ALL_TEXT,
        )

        settings = FakeSettings(Settings(folder_library_on=True, film_folders=(MOVIES,)))
        status = FolderLibraryStatus(on=True, films=3, slow=(MOVIES,), checked_at=NOW)
        folders = FakeFolders(settings, status)
        box = await self.box(folders, settings)
        shown = texts(box.box.frame)
        self.assertIn(
            "Reading \\\\NAS\\Media\\Movies takes a while: what was found there last time still "
            "counts until it's done.",
            shown,
        )
        self.assertFalse(any(text.startswith("Can't reach") for text in shown))
        self.assertNotEqual(SLOW_TEXT, CANT_REACH_TEXT)
        # Switching on: none read in time, but they answered.
        settings = FakeSettings()
        folders = FakeFolders(settings, outcome="slow")
        box = await self.box(folders, settings, suggest=None)
        box.films = [MOVIES]
        box.use()
        await box.loaded()
        await self.settle()
        shown = texts(box.box.frame)
        self.assertIn(SLOW_ALL_TEXT, shown)
        self.assertNotIn(UNREACHABLE_ALL_TEXT, shown)
        self.assertEqual(box.state, "off")
        # A change while on.
        saved = Settings(folder_library_on=True, film_folders=(MOVIES, "D:\\Films"))
        settings = FakeSettings(saved)
        folders = FakeFolders(settings, FolderLibraryStatus(on=True, films=3, checked_at=NOW))
        folders.outcome = "slow"
        box = await self.box(folders, settings)
        box.remove_buttons["film"][1].invoke()
        await box.loaded()
        await self.settle()
        self.assertIn(SLOW_CHANGE_TEXT, texts(box.box.frame))


class LibrarySectionTests(TkTestCase):
    def dialog(self, settings: Settings, plex: FakePlex) -> Any:
        from app.services import SettingsService
        from app.ui.settings_dialog import SettingsDialog

        service = SettingsService(FakeRepository(), settings)  # type: ignore[arg-type]
        folders = FakeFolders(service, FolderLibraryStatus(on=settings.folder_library_on))  # type: ignore[arg-type]
        dialog = SettingsDialog(
            self.root,
            service,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="3.1.0", previous_version=None),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            plex=plex,  # type: ignore[arg-type]
            folder_library=folders,  # type: ignore[arg-type]
        )
        self.keep(dialog)
        return dialog

    def order(self, dialog: Any) -> list[str]:
        """The page's boxes top to bottom: plex, folders, play (the rest left out)."""
        names = {
            str(dialog.plex_box.box.frame): "plex",
            str(dialog.folder_box.box.frame): "folders",
            str(dialog.plex_box.play_box.frame): "play",
        }
        frames = [f for f in dialog.page("Library").winfo_children() if str(f) in names]
        frames.sort(key=lambda f: int(f.grid_info()["row"]))
        return [names[str(f)] for f in frames]

    async def test_settings_search_still_finds_plex(self) -> None:
        from app.ui.settings_dialog import SECTION_LIBRARY, SECTION_NOTES
        from app.ui.settings_search import search

        self.assertEqual(SECTION_LIBRARY, "Library")
        self.assertEqual(
            SECTION_NOTES[SECTION_LIBRARY],
            "What you already have, from Plex or from your own folders, isn't missing and isn't "
            "downloaded again.",
        )
        dialog = self.dialog(Settings(), FakePlex())
        await dialog.plex_box.loaded()
        await dialog.folder_box.loaded()
        await asyncio.sleep(0.05)
        self.root.update()
        self.assertIn(SECTION_LIBRARY, dialog.sections)
        self.assertNotIn("Plex", dialog.sections)
        matches = search("Plex", dialog._search_places())
        self.assertTrue(matches)
        self.assertEqual(matches[0].section, SECTION_LIBRARY)
        self.assertEqual(matches[0].title, "Plex")
        self.assertTrue(search("folders", dialog._search_places()))
        # Without Plex, switched off: Plex first (the sketch's state 1), Play in under both.
        self.assertEqual(self.order(dialog), ["plex", "folders", "play"])

    async def test_the_boxes_order_follows_the_source(self) -> None:
        on = Settings(folder_library_on=True, film_folders=(MOVIES,))
        dialog = self.dialog(on, FakePlex())
        await dialog.folder_box.loaded()
        self.root.update()
        self.assertEqual(self.order(dialog), ["folders", "plex", "play"])
        connected = Settings(plex_token=TOKEN, folder_library_on=True, film_folders=(MOVIES,))
        dialog = self.dialog(connected, FakePlex(CONNECTED))
        await dialog.folder_box.loaded()
        self.root.update()
        self.assertEqual(self.order(dialog), ["plex", "folders", "play"])
