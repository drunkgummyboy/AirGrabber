"""Settings → Folders' "Count what's in them" box (app/ui/folder_library_settings.py; docs/settings/
2026-11-19-settings-redesign.md §4): the switch reading the folders before it turns on (refused in
red when none could be read, the switch going back), switching off with Undo, the counts /
couldn't-recognise / can't-reach / takes-a-while lines, the words beside Plex; and the boxes in the
Settings window (their pages, the search finding them). On a hidden Tk root with a stand-in
FolderLibraryService (and once the real one on temporary folders); nothing real is read."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models import Settings
from app.models.folder_library import FolderLibraryStatus, SwitchOutcome
from app.services import SettingsService
from app.ui.settings_commit import Committer
from app.ui.settings_layout import SettingsPage
from tests.test_backup_dialog import FakeProwlarr, FakeRepository, FakeShortcut, buttons, texts
from tests.test_plex_ui import CONNECTED, NOW, FakePlex, TkTestCase

MOVIES = r"\\NAS\Media\Movies"
SERIES = r"\\NAS\Media\Series"
TOKEN = "made-up-plex-token"


class FakeFolders:
    """FolderLibraryService's face for Settings: a status, what was asked, and a switch-on that
    saves like the real one (only once it is on)."""

    def __init__(
        self,
        settings: Any,
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


def service(settings: Settings | None = None) -> SettingsService:
    return SettingsService(FakeRepository(), settings or Settings())  # type: ignore[arg-type]


class CountBoxTests(TkTestCase):
    async def box(self, folders: Any, settings: Any) -> Any:
        from app.ui.folder_library_settings import FolderLibraryBox

        self.frame = ctk.CTkFrame(self.root)
        self.keep(self.frame)
        page = SettingsPage(self.frame, "Folders", "A note")
        page.frame.pack()
        self.committer = Committer()
        box = FolderLibraryBox(page, folders, settings, self.committer, clock=lambda: NOW)
        await box.loaded()
        await self.settle()
        return box

    async def test_the_switch_reads_before_it_turns_on(self) -> None:
        from app.ui.folder_library_settings import (
            INTRO_TEXT,
            READING_TEXT,
            SWITCH_TEXT,
            TITLE_TEXT,
        )

        settings = service(Settings(film_folders=(MOVIES,), series_folders=(SERIES,)))
        folders = FakeFolders(settings)
        folders.gate = asyncio.Event()
        box = await self.box(folders, settings)
        self.assertEqual(box.state, "off")
        self.assertEqual(box.switch.cget("text"), SWITCH_TEXT)
        shown = texts(box.box.frame)
        self.assertIn(TITLE_TEXT, shown)
        self.assertIn(INTRO_TEXT, shown)
        box.switch.toggle()
        await self.settle()
        self.assertEqual(folders.calls, [("on", (MOVIES,), (SERIES,))])
        # Reading: it says so, nothing is switched on yet, the switch waits.
        self.assertIn(READING_TEXT, texts(box.box.frame))
        self.assertEqual(box.switch.cget("state"), "disabled")
        self.assertFalse(settings.current.folder_library_on)
        folders.gate.set()
        await box.loaded()
        await self.settle()
        self.assertEqual(box.state, "on")
        self.assertTrue(settings.current.folder_library_on)
        self.assertEqual(box.switch.get(), 1)
        self.assertTrue(self.committer.can_undo)
        # Undo switches off again (the same way).
        self.assertTrue(await self.committer.run_undo())
        await self.settle()
        self.assertEqual(folders.calls[-1], ("off",))
        self.assertEqual(box.switch.get(), 0)

    async def test_nothing_could_be_read_is_refused_and_the_switch_goes_back(self) -> None:
        from app.ui.folder_library_settings import SLOW_ALL_TEXT, UNREACHABLE_ALL_TEXT

        for outcome, words in (("unreachable", UNREACHABLE_ALL_TEXT), ("slow", SLOW_ALL_TEXT)):
            with self.subTest(outcome=outcome):
                settings = service(Settings(film_folders=(MOVIES,)))
                folders = FakeFolders(settings, outcome=outcome)  # type: ignore[arg-type]
                box = await self.box(folders, settings)
                box.switch.toggle()
                await box.loaded()
                await self.settle()
                self.assertEqual(box.box.refusal, words)
                self.assertEqual(box.switch.get(), 0)
                self.assertFalse(settings.current.folder_library_on)
                self.assertFalse(self.committer.can_undo)

    async def test_the_switch_needs_a_folder(self) -> None:
        settings = service()
        box = await self.box(FakeFolders(settings), settings)
        self.assertEqual(box.switch.cget("state"), "disabled")
        await settings.update(series_folders=(SERIES,))  # the Series box added one
        await self.settle()
        self.assertEqual(box.switch.cget("state"), "normal")

    async def test_on_shows_counts_and_unreachable(self) -> None:
        from app.ui.folder_library_settings import CHECK_NOW_TEXT, counts_text

        settings = service(
            Settings(
                folder_library_on=True,
                film_folders=(MOVIES, "D:\\Films"),
                series_folders=(SERIES,),
            )
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
        self.assertEqual(box.switch.get(), 1)
        shown = texts(box.box.frame)
        self.assertIn("412 films · 38 series · 1,906 episodes · checked 3 min ago", shown)
        self.assertIn("6 files couldn't be recognised: they're listed on the Library page.", shown)
        self.assertIn(
            "Can't reach \\\\NAS\\Media\\Movies: what was found there last time still counts.",
            shown,
        )
        self.assertEqual(
            counts_text(FolderLibraryStatus(films=1, shows=1, episodes=1, checked_at=NOW), NOW),
            "1 film · 1 series · 1 episode · checked just now",
        )
        buttons(box.box.frame)[CHECK_NOW_TEXT].invoke()  # an action: no Undo
        self.assertEqual(folders.calls, [("check",)])
        self.assertFalse(self.committer.can_undo)
        # One file: its own words.
        folders.set(status.model_copy(update={"unrecognised": ("D:\\Films\\a.mp4",), "unreachable": ()}))
        await box.loaded()
        await self.settle()
        shown = texts(box.box.frame)
        self.assertIn("1 file couldn't be recognised: it's listed on the Library page.", shown)
        self.assertFalse(any(text.startswith("Can't reach") for text in shown))

    async def test_switching_off_forgets_what_was_read_with_undo(self) -> None:
        settings = service(Settings(folder_library_on=True, film_folders=(MOVIES,)))
        folders = FakeFolders(settings, FolderLibraryStatus(on=True, films=3, checked_at=NOW))
        box = await self.box(folders, settings)
        box.switch.toggle()
        await box.loaded()
        await self.settle()
        self.assertEqual(folders.calls, [("off",)])
        self.assertEqual(box.state, "off")
        self.assertEqual(settings.current.film_folders, (MOVIES,))  # the folders stay
        self.assertTrue(await self.committer.run_undo())  # read again and on
        await self.settle()
        self.assertEqual(folders.calls[-1], ("on", (MOVIES,), ()))
        self.assertEqual(box.state, "on")

    async def test_the_words_beside_plex(self) -> None:
        from app.ui.folder_library_settings import (
            BESIDE_PLEX_TEXT,
            INTRO_TEXT,
            SWITCH_BESIDE_PLEX_TEXT,
        )

        self.assertEqual(
            BESIDE_PLEX_TEXT,
            "A second shelf beside Plex: what's in these folders shows under Downloaded and "
            "counts as had.",
        )
        settings = service(Settings(plex_token=TOKEN, film_folders=(MOVIES,)))
        folders = FakeFolders(settings)
        box = await self.box(folders, settings)
        self.assertEqual(box.switch.cget("text"), SWITCH_BESIDE_PLEX_TEXT)
        shown = texts(box.box.frame)
        self.assertIn(BESIDE_PLEX_TEXT, shown)
        self.assertNotIn(INTRO_TEXT, shown)  # "No Plex? …" isn't for a PC with Plex
        box.switch.toggle()
        await box.loaded()
        await self.settle()
        self.assertEqual(box.state, "on")
        self.assertIn("2 films · 1 series · 5 episodes · checked just now", texts(box.box.frame))
        self.assertEqual(settings.current.plex_token.get_secret_value(), TOKEN)

    async def test_a_slow_folder_has_its_own_words(self) -> None:
        from app.ui.folder_library_settings import CANT_REACH_TEXT, SLOW_TEXT

        settings = service(Settings(folder_library_on=True, film_folders=(MOVIES,)))
        status = FolderLibraryStatus(on=True, films=3, slow=(MOVIES,), checked_at=NOW)
        box = await self.box(FakeFolders(settings, status), settings)
        shown = texts(box.box.frame)
        self.assertIn(
            "Reading \\\\NAS\\Media\\Movies takes a while: what was found there last time still "
            "counts until it's done.",
            shown,
        )
        self.assertFalse(any(text.startswith("Can't reach") for text in shown))
        self.assertNotEqual(SLOW_TEXT, CANT_REACH_TEXT)

    async def test_settings_closed_mid_switch_still_switches_on(self) -> None:
        # The real service: the box goes with the window, the switch doesn't.
        import tempfile
        from pathlib import Path
        from typing import cast

        from app.database import Database
        from app.models.plex import PlexScan
        from app.services.folder_library.service import FolderLibraryService
        from app.services.library import LibraryService
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
        await settings.update(film_folders=(str(films),))
        library = FakeLibrary()
        real = FolderLibraryService(
            settings, db.library_files, db.folders, cast(TMDBClient, make_tmdb()),
            cast(LibraryService, library), quiet_s=0.0,
        )
        self.addAsyncCleanup(real.aclose)
        storing, go_on = asyncio.Event(), asyncio.Event()
        real_replace = db.folders.replace

        async def replace(scan_: PlexScan, when: Any) -> None:
            storing.set()
            await go_on.wait()
            await real_replace(scan_, when)

        with mock.patch.object(db.folders, "replace", replace):
            box = await self.box(real, settings)
            box.set_counting(True)
            for _ in range(500):
                if storing.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(storing.is_set())
            self.frame.destroy()  # Settings closed mid-switch
            await asyncio.sleep(0.05)
            go_on.set()
            for _ in range(500):
                if not real.status.checking:
                    break
                await asyncio.sleep(0.01)
        self.assertTrue(settings.current.folder_library_on)
        self.assertEqual(settings.current.film_folders, (str(films),))
        self.assertEqual(library.notified, 1)  # the pages heard it
        self.assertTrue(real.status.on)


class FoldersInTheWindowTests(TkTestCase):
    def dialog(self, settings: Settings, plex: FakePlex) -> Any:
        from app.ui.settings_dialog import SettingsDialog

        settings_service = service(settings)
        folders = FakeFolders(settings_service, FolderLibraryStatus(on=settings.folder_library_on, films=37, shows=6))
        dialog = SettingsDialog(
            self.root,
            settings_service,
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

    def pages(self, dialog: Any) -> dict[str, str]:
        """Which page each box is on."""
        boxes = {
            "plex": dialog.plex_box.box.frame,
            "count": dialog.folder_box.box.frame,
            "series": dialog.folders_page.boxes["series"].frame,
            "line": dialog.folders_line.box.frame,
            "play": dialog.plex_box.play_box.frame,
        }
        return {
            name: next(page for page in dialog.sections if frame.master is dialog.page(page))
            for name, frame in boxes.items()
        }

    async def test_the_boxes_and_their_pages(self) -> None:
        from app.ui.settings_pages_registry import PAGE_FOLDERS, PAGE_PLAYING, PAGE_PLEX

        where = {
            "plex": PAGE_PLEX,
            "count": PAGE_FOLDERS,
            "series": PAGE_FOLDERS,
            "line": PAGE_PLEX,
            "play": PAGE_PLAYING,
        }
        for settings, plex in (
            (Settings(folder_library_on=True, film_folders=(MOVIES,)), FakePlex()),
            (Settings(plex_token=TOKEN, film_folders=(MOVIES,)), FakePlex(CONNECTED)),
        ):
            dialog = self.dialog(settings, plex)
            await dialog.plex_box.loaded()
            self.root.update()
            self.assertEqual(self.pages(dialog), where)

    async def test_plex_and_library_says_where_the_folders_are_set(self) -> None:
        from app.ui.plex_settings import FOLDERS_PAGE_LINK
        from app.ui.settings_pages_registry import PAGE_FOLDERS, PAGE_PLEX

        dialog = self.dialog(Settings(plex_token=TOKEN, folder_library_on=True, film_folders=(MOVIES,)), FakePlex(CONNECTED))
        dialog.show_section(PAGE_PLEX)
        await dialog.plex_box.loaded()
        self.root.update()
        line = dialog.folders_line
        self.assertTrue(line.shown)
        self.assertEqual(
            line.label.cget("text"), "37 films · 6 series from your own folders count beside Plex."
        )
        self.assertEqual(line.link.cget("text"), FOLDERS_PAGE_LINK)
        line.open()  # the link
        self.assertEqual(dialog.section, PAGE_FOLDERS)
        # Off: no line.
        dialog = self.dialog(Settings(film_folders=(MOVIES,)), FakePlex())
        self.assertFalse(dialog.folders_line.shown)

    async def test_settings_search_finds_the_folders(self) -> None:
        from app.ui.folder_library_settings import TITLE_TEXT
        from app.ui.settings_pages_registry import PAGE_FOLDERS, PAGE_PLEX
        from app.ui.settings_search import search

        dialog = self.dialog(Settings(plex_token=TOKEN), FakePlex(CONNECTED))
        await dialog.plex_box.loaded()
        await asyncio.sleep(0.05)
        self.root.update()
        matches = search("Plex", dialog._search_places())
        self.assertEqual((matches[0].section, matches[0].title), (PAGE_PLEX, "Plex"))
        for query in ("count what", "second shelf"):
            with self.subTest(query=query):
                found = [
                    match
                    for match in search(query, dialog._search_places())
                    if match.section == PAGE_FOLDERS and match.title == TITLE_TEXT
                ]
                self.assertTrue(found)
                self.assertIs(found[0].frame, dialog.folder_box.box.frame)
        titles = [m.title for m in search("folders movies", dialog._search_places())]
        self.assertIn("Movies", titles)  # the Folders page's list of film folders
