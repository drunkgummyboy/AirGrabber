"""The Library page with the folder library (docs/folder-library/, mockups/folder-library.html
state 4): it shows with either source and follows the folder library's status, says where its
titles come from in the folders' words, offers Send to phone in a title's window while the
phone connection is there (part 2; on the cards until the owner's choice A + 2,
mockups/library-calmer.html), and lists the files it couldn't
recognise at the bottom with "Tidy up in MediaForge" (the paths handed to MediaForge's page,
nothing moved here). On a hidden Tk root with stand-ins; nothing reads a folder."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk
from PIL import ImageTk

from app.i18n import t
from app.i18n_nl import NL
from app.models import Settings
from app.models.folder_library import FolderLibraryStatus
from app.models import PairedPhone, Profile
from app.models.plex import PlexTitle, PlexVideo
from app.services.plex_library import (
    LibraryContents,
    LibraryFilter,
    LibraryKindTab,
    LibraryShow,
    Shelf,
)
from app.single_instance import ForgeRequest
from app.ui import library_words, theme
from app.ui.main_window import MainWindow, Mode
from app.ui.poster_card import rgb
from tests.test_backup_dialog import DialogTestCase, buttons, texts

NOW = datetime(2026, 11, 15, 8, 53, tzinfo=UTC)
FILES = (
    "D:\\Films\\VID_20190811_142233.mp4",
    "\\\\NAS\\Media\\Movies\\New folder\\movie_final_v2.mkv",
    "\\\\NAS\\Media\\Series\\Misc\\ep3.avi",
)
FOLDERS_ON = Settings(folder_library_on=True, film_folders=("D:\\Films",))


PERSON = Profile(id=1, name="Alex", created_at=NOW)
PHONE = PairedPhone(
    id="a", name="Alex's phone", token_hash="ha", profile_id=1, paired_at=NOW, manager=False
)


class FakeSends:
    """PhoneSends as the Send to phone window sees it."""

    def taken_keys(self, phone_id: str) -> frozenset[str]:
        return frozenset()

    def phone_sized_possible(self, film: PlexTitle) -> bool:
        return False


class FakeService:
    """PlexLibraryService with the folders as its one shelf (`shelves`: those on, each its
    contents; `contents` for the folders alone)."""

    def __init__(
        self, contents: LibraryContents, shelves: dict[Shelf, LibraryContents] | None = None
    ) -> None:
        self.on: dict[Shelf, LibraryContents] = shelves or {"downloaded": contents}
        self.asked: list[Shelf | None] = []

    @property
    def available(self) -> bool:
        return bool(self.on)

    def shelves(self) -> tuple[Shelf, ...]:
        return tuple(self.on)

    async def contents(self, *, profile_id: int, shelf: Shelf | None = None) -> LibraryContents:
        self.asked.append(shelf)
        chosen = shelf if shelf is not None else next(iter(self.on), None)
        return self.on.get(chosen, LibraryContents()) if chosen is not None else LibraryContents()

    async def follow(self, plex_title: PlexTitle, *, profile_id: int) -> bool:
        return True


def film(name: str) -> PlexTitle:
    return PlexTitle(kind="movie", key=f"folder:movie:{name}", title=name, year=2024, tmdb_id=1)


class PageCase(DialogTestCase):
    """A Library page on stand-ins (`view`)."""

    def view(
        self, contents: LibraryContents, *, service: FakeService | None = None, **extra: Any
    ) -> Any:
        from app.ui.views.library_view import LibraryView

        options: dict[str, Any] = {
            "on_open_show": mock.AsyncMock(return_value=True),
            "on_open_film": lambda found: None,
        }
        options.update(extra)
        view = LibraryView(
            self.root,
            service=service or FakeService(contents),  # type: ignore[arg-type]
            posters=mock.Mock(get=mock.AsyncMock(return_value=None)),
            settings=self.settings(),
            status=lambda text, clear_after_ms=None: None,
            **options,
        )
        view.pack(fill="both", expand=True)
        self.keep(view)
        return view


class FolderPageTests(PageCase):
    async def test_the_library_page_shows_with_the_folder_library(self) -> None:
        from app.ui.views.library_view import EMPTY_FOLDERS_TITLE

        # The switch's Library part follows the folder library's status, not only Plex's.
        calls: list[Any] = []
        window: Any = SimpleNamespace(
            _services=SimpleNamespace(plex_library=SimpleNamespace(available=True)),
            _band=SimpleNamespace(set_library_shown=lambda shown: calls.append(("shown", shown))),
            _mode=Mode.SERIES,
            _go_home=lambda: calls.append("home"),
            _library_view=SimpleNamespace(
                refresh_unrecognised=lambda: calls.append("unrecognised")
            ),
        )
        window._show_library_part = lambda: MainWindow._show_library_part(window)
        MainWindow._on_folder_library_changed(window)
        self.assertEqual(calls, [("shown", True), "unrecognised"])
        calls.clear()
        window._services.plex_library.available = False  # switched off: the page goes
        window._mode = Mode.LIBRARY
        MainWindow._on_folder_library_changed(window)
        self.assertEqual(calls, [("shown", False), "home", "unrecognised"])

        # Its words are the folders', never "From Plex".
        service = SimpleNamespace(current=FOLDERS_ON)
        library_words.follow(service)  # type: ignore[arg-type]
        self.addCleanup(library_words.follow, None)
        checked: list[str] = []
        view = self.view(
            LibraryContents(films=(film("Nightfall"),), checked_at=NOW), on_checked=checked.append
        )
        await view.load()
        await self.settle()
        local = NOW.astimezone().strftime("%H:%M")
        self.assertEqual(checked, [f"From your folders · checked {local}"])
        self.assertFalse(any("Plex" in text for text in texts(view)))
        empty = self.view(LibraryContents())
        await empty.load()
        await self.settle()
        self.assertIn(EMPTY_FOLDERS_TITLE, texts(empty))


    async def test_the_library_page_offers_send_to_phone_for_folder_titles(self) -> None:
        from app.ui.send_to_phone import (
            LOADING_FOLDERS_TEXT,
            NO_EPISODES_FOLDERS_TEXT,
            SendToPhoneDialog,
        )

        # Send to phone with the folder library too, while the phone connection is there.
        phone = SimpleNamespace(available=True, phones=lambda: ["a phone"])
        stand_in: Any = SimpleNamespace(
            _services=SimpleNamespace(phone=phone, settings=SimpleNamespace(current=FOLDERS_ON))
        )
        self.assertTrue(MainWindow._can_send_to_phone(stand_in))
        stand_in._services.settings = SimpleNamespace(current=Settings(plex_token="made-up"))
        self.assertTrue(MainWindow._can_send_to_phone(stand_in))
        phone.available = False  # no phone connection (no marker, or switched off): none
        self.assertFalse(MainWindow._can_send_to_phone(stand_in))
        phone.available = True
        stand_in._services.settings = SimpleNamespace(current=Settings())  # no library at all
        self.assertFalse(MainWindow._can_send_to_phone(stand_in))

        # The card has no button for it (only its poster): the film's window has it.
        view = self.view(LibraryContents(films=(film("Nightfall"),), checked_at=NOW))
        view.set_kind(LibraryKindTab.FILMS)
        await view.load()
        await self.settle()
        [card] = view.cards
        self.assertEqual(buttons(card), {})
        sent: list[PlexTitle] = []
        overs: list[object] = []

        def send_to_phone(title: PlexTitle, *, over: object = None) -> None:
            sent.append(title)
            overs.append(over)

        opened = mock.AsyncMock(return_value=True)
        can = [True]
        library_window: Any = SimpleNamespace(
            _can_send_to_phone=lambda: can[0],
            _send_to_phone=send_to_phone,
            _movie_windows=SimpleNamespace(open_movie_id=opened),
        )
        library_window._phone_send = lambda title: MainWindow._phone_send(library_window, title)
        nightfall = film("Nightfall")
        MainWindow._open_plex_film(library_window, nightfall)
        await self.settle()
        opened.assert_awaited_once()
        assert opened.await_args is not None
        self.assertEqual(opened.await_args.args, (1,))
        opened.await_args.kwargs["send"]("film window")  # the window's "Send to phone"
        self.assertEqual([one.key for one in sent], ["folder:movie:Nightfall"])
        self.assertEqual(overs, ["film window"])  # the dialog opens over the window
        # A show's window the same, looked up on TVmaze first.
        harbour = PlexTitle(kind="show", key="folder:show:1399", title="Harbour", tmdb_id=1399)
        show_windows = SimpleNamespace(open_show=mock.Mock())
        library_window._services = SimpleNamespace(
            plex_library=SimpleNamespace(show=mock.AsyncMock(return_value="the show"))
        )
        library_window._show_windows = show_windows
        library_window.winfo_exists = lambda: True
        self.assertTrue(await MainWindow._open_plex_show(library_window, harbour))
        [(shown,), kwargs] = show_windows.open_show.call_args
        self.assertEqual(shown, "the show")
        kwargs["send"]("show window")
        self.assertEqual([one.key for one in sent][-1], "folder:show:1399")
        self.assertEqual(overs[-1], "show window")
        # No phone to send to: no button in either window.
        can[0] = False
        self.assertTrue(await MainWindow._open_plex_show(library_window, harbour))
        self.assertIsNone(show_windows.open_show.call_args.kwargs["send"])
        opened.reset_mock()
        MainWindow._open_plex_film(library_window, nightfall)
        await self.settle()
        assert opened.await_args is not None
        self.assertIsNone(opened.await_args.kwargs["send"])

        # The window lists the folder show's episodes, the PC's person's, in the folders' words.
        show = PlexTitle(kind="show", key="folder:show:1399", title="Harbour", tmdb_id=1399)
        videos = [
            PlexVideo(key="episode:1399:1:1", file="x", season=1, episode=1, title="One"),
            PlexVideo(key="episode:1399:1:2", file="y", season=1, episode=2),
        ]
        asked: list[tuple[str, int]] = []
        gate = asyncio.Event()

        async def show_videos(title: PlexTitle, *, profile_id: int) -> list[PlexVideo]:
            asked.append((title.key, profile_id))
            await gate.wait()
            return videos if title.key == show.key else []

        plex_episodes = mock.AsyncMock(return_value=[])
        window: Any = SimpleNamespace(
            _services=SimpleNamespace(
                phone=SimpleNamespace(available=True, phones=lambda: [PHONE], people=[PERSON]),
                profiles=SimpleNamespace(current_id=PERSON.id),
                phone_sends=FakeSends(),
                plex=SimpleNamespace(episodes=plex_episodes),
                folder_playing=SimpleNamespace(show_videos=show_videos),
            ),
            set_status=lambda text, clear_after_ms=None: None,
        )
        with mock.patch("app.ui.main_window.SendToPhoneDialog") as made:
            MainWindow._send_to_phone(window, show)
        self.assertIs(made.call_args.args[0], window)  # no window given: over the main window
        # From a show's or film's window: over that window (its toplevel).
        opener: Any = SimpleNamespace(winfo_toplevel=lambda: "the show's window")
        with mock.patch("app.ui.main_window.SendToPhoneDialog") as over_window:
            MainWindow._send_to_phone(window, show, over=opener)
        self.assertEqual(over_window.call_args.args[0], "the show's window")
        # The phone gone meanwhile: no dialog, the status bar says so.
        from app.ui.send_to_phone import NO_PHONE_TEXT

        said: list[str] = []
        window.set_status = lambda text, clear_after_ms=None: said.append(text)
        for gone in (
            SimpleNamespace(available=True, phones=lambda: [], people=[PERSON]),
            SimpleNamespace(available=False, phones=lambda: [PHONE], people=[PERSON]),
        ):
            window._services.phone = gone
            with mock.patch("app.ui.main_window.SendToPhoneDialog") as none:
                MainWindow._send_to_phone(window, show, over=opener)
            none.assert_not_called()
        self.assertEqual(said, [t(NO_PHONE_TEXT)] * 2)
        self.assertEqual(NL[NO_PHONE_TEXT], "Er is geen telefoon verbonden")
        window._services.phone = SimpleNamespace(
            available=True, phones=lambda: [PHONE], people=[PERSON]
        )
        episodes = made.call_args.kwargs["episodes"]
        dialog = SendToPhoneDialog(
            self.root, title=show, phones=[PHONE], people=[PERSON], current_person=PERSON.id,
            sends=FakeSends(), episodes=episodes, on_sent=lambda text: None,  # type: ignore[arg-type]
        )
        self.addCleanup(dialog.close)
        self.assertEqual(str(dialog.note.cget("text")), t(LOADING_FOLDERS_TEXT))
        gate.set()
        await dialog.settled()
        self.assertEqual(list(dialog.ticks), ["episode:1399:1:1", "episode:1399:1:2"])
        self.assertEqual(asked, [(show.key, PERSON.id)])
        plex_episodes.assert_not_called()
        empty = show.model_copy(update={"key": "folder:show:7", "tmdb_id": 7})
        nothing = SendToPhoneDialog(
            self.root, title=empty, phones=[PHONE], people=[PERSON], current_person=PERSON.id,
            sends=FakeSends(), episodes=episodes, on_sent=lambda text: None,  # type: ignore[arg-type]
        )
        self.addCleanup(nothing.close)
        await nothing.settled()
        self.assertEqual(str(nothing.note.cget("text")), t(NO_EPISODES_FOLDERS_TEXT))
        for text in (LOADING_FOLDERS_TEXT, NO_EPISODES_FOLDERS_TEXT):
            self.assertIn(text, NL)
        # A Plex show still asks Plex.
        plex_show = PlexTitle(kind="show", key="700", title="VisionQuest")
        self.assertEqual(await episodes(plex_show), [])
        plex_episodes.assert_awaited_once_with(plex_show)

    async def test_the_page_says_which_folders_it_cant_reach(self) -> None:
        # Ruling R13: the Library page says it too, in the Settings box's words.
        from app.ui.folder_library_settings import CANT_REACH_TEXT, SLOW_TEXT

        status = FolderLibraryStatus(
            on=True, unreachable=("\\\\NAS\\Media\\Movies",), slow=("E:\\Series",)
        )
        window: Any = SimpleNamespace(_services=SimpleNamespace(folder_library=SimpleNamespace(
            status=status
        )))
        lines = MainWindow._folder_reach_lines(window)
        self.assertEqual(
            lines,
            [
                "Can't reach \\\\NAS\\Media\\Movies: what was found there last time still counts.",
                "Reading E:\\Series takes a while: what was found there last time still counts "
                "until it's done.",
            ],
        )
        self.assertEqual(lines[0], t(CANT_REACH_TEXT, folder="\\\\NAS\\Media\\Movies"))
        self.assertEqual(lines[1], t(SLOW_TEXT, folder="E:\\Series"))
        window._services.folder_library.status = status.model_copy(update={"on": False})
        self.assertEqual(MainWindow._folder_reach_lines(window), [])

        shown_lines = list(lines)
        view = self.view(
            LibraryContents(films=(film("Nightfall"),), checked_at=NOW),
            reach=lambda: shown_lines,
        )
        await view.load()
        await self.settle()
        shown = texts(view)
        for line in lines:
            self.assertEqual(shown.count(line), 1)  # once
        self.assertEqual(view.reach_labels[0].cget("text_color"), theme.WARNING)
        # The NAS is back: the next check's status takes the lines away.
        shown_lines.clear()
        view.refresh_unrecognised()
        await self.settle()
        self.assertFalse(any(text.startswith(("Can't reach", "Reading")) for text in texts(view)))
        # In Dutch too, as the Settings box says it.
        self.assertIn(CANT_REACH_TEXT, NL)
        self.assertIn(SLOW_TEXT, NL)

    async def test_couldnt_recognise_lists_files_and_tidies_up_in_mediaforge(self) -> None:
        from app.ui.views.library_view import TIDY_TEXT, UNRECOGNISED_TEXT

        status = FolderLibraryStatus(on=True, unrecognised=FILES)
        handed: list[tuple[str, ...]] = []
        view = self.view(
            LibraryContents(films=(film("Nightfall"),), checked_at=NOW),
            unrecognised=lambda: status.unrecognised,
            on_tidy=lambda paths: handed.append(tuple(paths)),
        )
        await view.load()
        await self.settle()
        shown = texts(view)
        self.assertIn("Couldn't recognise (3)", shown)
        self.assertIn(UNRECOGNISED_TEXT, shown)
        for name, where in (
            ("VID_20190811_142233.mp4", "D:\\Films"),
            ("movie_final_v2.mkv", "\\\\NAS\\Media\\Movies\\New folder"),
            ("ep3.avi", "\\\\NAS\\Media\\Series\\Misc"),
        ):
            self.assertIn(name, shown)
            self.assertIn(where, shown)
        buttons(view)[TIDY_TEXT].invoke()
        self.assertEqual(handed, [FILES])
        # A deep path is shortened in the middle, its start and end kept; one font for every redraw.
        from app.ui.views.library_view import FOLDER_ROOM, NAME_ROOM

        deep = "\\\\NAS\\" + "\\".join(f"Folder number {n}" for n in range(40))
        long_name = "A very long file name " * 12 + ".mkv"
        status = FolderLibraryStatus(on=True, unrecognised=(f"{deep}\\{long_name}",))
        view.refresh_unrecognised()
        await self.settle()
        name_font, folder_font = view._fonts()
        self.assertIs(view._fonts()[0], name_font)
        shown = texts(view)
        name = next(text for text in shown if text.startswith("A very long"))
        folder = next(text for text in shown if text.startswith("\\\\NAS"))
        self.assertIn("…", name)
        self.assertTrue(name.endswith(".mkv"))
        self.assertIn("…", folder)
        self.assertTrue(folder.endswith("Folder number 39"))
        self.assertLessEqual(name_font.measure(name), NAME_ROOM)
        self.assertLessEqual(folder_font.measure(folder), FOLDER_ROOM)
        self.assertEqual(name_font.cget("family"), "Consolas")
        status = FolderLibraryStatus(on=True, unrecognised=FILES)
        view.refresh_unrecognised()
        # Still there after a tab change (the grid made anew).
        view.set_kind(LibraryKindTab.FILMS)
        await self.settle()
        self.assertIn("Couldn't recognise (3)", texts(view))
        # Nothing in it: no section.
        status = FolderLibraryStatus(on=True)
        view.refresh_unrecognised()
        await self.settle()
        self.assertFalse(any(text.startswith("Couldn't recognise") for text in texts(view)))
        self.assertNotIn(TIDY_TEXT, buttons(view))

        # The main window hands the files to MediaForge's page, as an Explorer drop does.
        requests: list[ForgeRequest] = []
        window: Any = SimpleNamespace(_forge=SimpleNamespace(handle=requests.append))
        MainWindow._tidy_up(window, FILES)
        self.assertEqual(requests, [ForgeRequest(paths=FILES)])


class ResyncTests(PageCase):
    """The header's round ↻ ("Check now"): the source read again, then the page."""

    async def test_no_button_without_resync(self) -> None:
        view = self.view(LibraryContents())
        self.assertIsNone(view.resync_button)

    async def test_check_now_reads_again_then_loads_the_page(self) -> None:
        from app.ui.cards import RoundButton
        from app.ui.views.library_view import RESYNC_TIP, RESYNCING_TIP

        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def resync() -> None:
            calls.append("resync")
            started.set()
            await release.wait()

        view = self.view(LibraryContents(), resync=resync)
        view.load = mock.AsyncMock(side_effect=lambda: calls.append("load"))
        # A round ↻ at the header's right, after the search box; its tip says what it does.
        button = view.resync_button
        self.assertIsInstance(button, RoundButton)
        self.assertEqual(button.text, "↻")
        self.assertIs(button.master, view.header)
        self.assertGreater(button.grid_info()["column"], view.search.grid_info()["column"])
        self.assertEqual(view._resync_tip.text, t(RESYNC_TIP))
        button.invoke()
        await asyncio.wait_for(started.wait(), 2)
        self.assertTrue(view.resyncing)
        self.assertEqual(view._resync_tip.text, t(RESYNCING_TIP))
        self.assertEqual(button._text_color, theme.TEXT_FAINT)  # faint meanwhile
        view.resync()  # a second click while it runs: nothing more
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not view.resyncing:
                break
        self.assertEqual(calls, ["resync", "load"])
        self.assertEqual(view._resync_tip.text, t(RESYNC_TIP))
        self.assertEqual(button._text_color, theme.TEXT_MUTED)
        self.assertEqual(NL[RESYNC_TIP], "Nu controleren")

    async def test_a_failed_check_gives_the_button_back(self) -> None:
        async def resync() -> None:
            raise OSError("NAS asleep")

        view = self.view(LibraryContents(), resync=resync)
        with self.assertLogs("app.ui.tasks", level="ERROR"):
            view.resync_button.invoke()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if not view.resyncing:
                    break
        self.assertFalse(view.resyncing)

    async def test_the_window_reads_the_librarys_source_again(self) -> None:
        plex = SimpleNamespace(check=mock.AsyncMock())
        folders = SimpleNamespace(check=mock.AsyncMock())
        settings = SimpleNamespace(current=Settings(plex_token="token"))
        window: Any = SimpleNamespace(
            _services=SimpleNamespace(plex=plex, folder_library=folders, settings=settings)
        )
        await MainWindow._resync_library(window)
        plex.check.assert_awaited_once_with(again=True)
        folders.check.assert_not_awaited()
        settings.current = FOLDERS_ON
        await MainWindow._resync_library(window)
        folders.check.assert_awaited_once_with()
        settings.current = Settings()
        await MainWindow._resync_library(window)  # no library: nothing
        self.assertEqual((plex.check.await_count, folders.check.await_count), (1, 1))

    async def test_check_now_checks_both(self) -> None:
        # Both shelves: Plex's check and the folders' run together (each waits for the other
        # to have started; one after the other would never get there).
        plex_started, folders_started = asyncio.Event(), asyncio.Event()

        async def plex_check(*, again: bool) -> None:
            plex_started.set()
            await asyncio.wait_for(folders_started.wait(), 2)

        async def folder_check() -> None:
            folders_started.set()
            await asyncio.wait_for(plex_started.wait(), 2)

        plex = SimpleNamespace(check=mock.AsyncMock(side_effect=plex_check))
        folders = SimpleNamespace(check=mock.AsyncMock(side_effect=folder_check))
        settings = SimpleNamespace(
            current=Settings(
                plex_token="token", folder_library_on=True, film_folders=FOLDERS_ON.film_folders
            )
        )
        window: Any = SimpleNamespace(
            _services=SimpleNamespace(plex=plex, folder_library=folders, settings=settings)
        )
        await asyncio.wait_for(MainWindow._resync_library(window), 5)
        plex.check.assert_awaited_once_with(again=True)
        folders.check.assert_awaited_once_with()
        # One failing (a NAS asleep): the other still ends, then the failure is the button's.
        done: list[str] = []

        async def slow_plex(*, again: bool) -> None:
            await asyncio.sleep(0.05)
            done.append("plex")

        plex.check = mock.AsyncMock(side_effect=slow_plex)
        folders.check = mock.AsyncMock(side_effect=OSError("NAS asleep"))
        with self.assertRaises(OSError):
            await MainWindow._resync_library(window)
        self.assertEqual(done, ["plex"])


def plex_show(name: str, key: str) -> LibraryShow:
    return LibraryShow(PlexTitle(kind="show", key=key, title=name, show_id=int(key)))


class ShelfTests(PageCase):
    """Two shelves (docs/folder-library/2026-11-16-alongside-plex-design.md): Plex and
    Downloaded, a switch above the Shows / Films tabs only when both are on, never mixed."""

    def both(self) -> dict[Shelf, LibraryContents]:
        plex = LibraryContents(
            shows=(plex_show("The Ark", "1001"),),
            films=(PlexTitle(kind="movie", key="3001", title="Dune"),),
            checked_at=NOW,
        )
        downloaded = LibraryContents(
            shows=(LibraryShow(PlexTitle(kind="show", key="folder:show:7", title="The Ark")),),
            films=(film("Nightfall"), film("Iron Coast")),
            checked_at=NOW,
        )
        return {"plex": plex, "downloaded": downloaded}

    def names(self, view: Any) -> list[str]:
        return [card.title.title for card in view.cards]

    async def test_the_switch_shows_only_with_both(self) -> None:
        from app.ui.views.library_view import DOWNLOADED_SHELF, PLEX_SHELF

        shelves = self.both()
        service = FakeService(LibraryContents(), {"plex": shelves["plex"]})
        checked: list[str] = []
        reach = [r"Can't reach \\NAS\Media: what was found there last time still counts."]
        view = self.view(
            LibraryContents(), service=service, on_checked=checked.append,
            unrecognised=lambda: FILES, on_tidy=lambda paths: None, reach=lambda: reach,
        )
        local = NOW.astimezone().strftime("%H:%M")
        # Plex alone: no switch, the page as before.
        await view.load()
        await self.settle()
        self.assertFalse(view.shelf_switch_shown)
        self.assertEqual(view.shelf, "plex")
        self.assertEqual(checked[-1], f"From Plex · checked {local}")
        self.assertEqual(self.names(view), ["The Ark"])
        # "Couldn't recognise" and "Can't reach" are the folders': not on Plex's shelf.
        self.assertFalse(any(text.startswith("Couldn't recognise") for text in texts(view)))
        self.assertEqual(view.reach_labels, [])

        # Both: the switch, right beside "Library" in the header (above Continue watching and
        # the Shows / Films tabs), each shelf with its count.
        service.on = dict(shelves)
        await view.load()
        await self.settle()
        self.assertTrue(view.shelf_switch_shown)
        self.assertIs(view.shelf_switch.master, view.header)
        self.assertEqual(view.shelf_switch.grid_info()["column"], 1)  # after the title
        self.assertLess(
            view.header.grid_info()["row"], view.kind_tabs_row.grid_info()["row"]
        )
        segments = view.shelf_switch.segments
        self.assertEqual(
            [(segments[s].name.cget("text"), segments[s].count.cget("text").strip()) for s in segments],
            [(t(PLEX_SHELF), "2"), (t(DOWNLOADED_SHELF), "3")],
        )
        self.assertEqual(view.shelf, "plex")  # Plex's first
        self.assertTrue(segments["plex"].selected)
        self.assertEqual(self.names(view), ["The Ark"])
        self.assertFalse(any(text.startswith("Couldn't recognise") for text in texts(view)))

        # Downloaded: only what the folders hold, in the folders' words, with their lines.
        segments["downloaded"].command()  # a click on it
        await self.settle()
        self.assertEqual(view.shelf, "downloaded")
        self.assertTrue(segments["downloaded"].selected)
        self.assertFalse(segments["plex"].selected)
        self.assertEqual(checked[-1], f"From your folders · checked {local}")
        self.assertEqual(self.names(view), ["The Ark"])  # the folder's own card
        self.assertEqual(view.cards[0].title.key, "folder:show:7")
        view.set_kind(LibraryKindTab.FILMS)
        await self.settle()
        self.assertEqual(sorted(self.names(view)), ["Iron Coast", "Nightfall"])
        self.assertIn("Couldn't recognise (3)", texts(view))
        self.assertEqual([label.cget("text") for label in view.reach_labels], reach)
        view.set_shelf("plex")
        await self.settle()
        self.assertEqual(self.names(view), ["Dune"])  # still on Films
        self.assertEqual(view.reach_labels, [])
        view.set_shelf("downloaded")

        # Kept while the app runs: a load again, Plex away and back.
        await view.load()
        self.assertEqual(view.shelf, "downloaded")
        service.on = {"downloaded": shelves["downloaded"]}  # the folders alone: no switch
        await view.load()
        await self.settle()
        self.assertFalse(view.shelf_switch_shown)
        self.assertEqual(view.shelf, "downloaded")
        service.on = {"plex": shelves["plex"]}
        await view.load()
        self.assertEqual(view.shelf, "plex")  # the only one there
        service.on = dict(shelves)
        await view.load()
        await self.settle()
        self.assertEqual(view.shelf, "downloaded")  # the one chosen, back
        self.assertEqual(sorted(self.names(view)), ["Iron Coast", "Nightfall"])
        # In Dutch.
        self.assertEqual(NL[DOWNLOADED_SHELF], "Gedownload")
        self.assertIn(PLEX_SHELF, NL)

    async def test_opening_downloaded_reads_the_folders_again(self) -> None:
        read = mock.Mock()
        service = FakeService(LibraryContents(), self.both())
        view = self.view(LibraryContents(), service=service, read_downloaded=read)
        view.refresh_if_stale()  # the page shown on Plex's shelf: the folders aren't read
        await self.settle()
        read.assert_not_called()
        view.set_shelf("downloaded")
        await self.settle()
        self.assertEqual(read.call_count, 1)
        view.set_shelf("downloaded")  # already open: nothing more
        view.set_shelf("plex")
        self.assertEqual(read.call_count, 1)
        view.set_shelf("downloaded")
        self.assertEqual(read.call_count, 2)
        # The page shown again on Downloaded: read again (cheap: unchanged files aren't).
        view.refresh_if_stale()
        await self.settle()
        self.assertEqual(read.call_count, 3)
        # The folders alone are the Downloaded shelf too.
        view.set_shelf("plex")  # Plex's chosen, then Plex goes
        self.assertEqual(read.call_count, 3)
        service.on = {"downloaded": self.both()["downloaded"]}
        view.invalidate()
        view.refresh_if_stale()
        await self.settle()
        self.assertEqual(read.call_count, 4)
        # The main window hands it the folder library's "check soon".
        folders = SimpleNamespace(check_soon=mock.Mock())
        window: Any = SimpleNamespace(_services=SimpleNamespace(folder_library=folders))
        MainWindow._read_downloaded(window)
        folders.check_soon.assert_called_once_with()


class CalmerTests(PageCase):
    """The owner's choice A + 2 (mockups/library-calmer.html): the top in two rows, the cards
    the poster only (the follow mark, ⬆ Upgrade as a pill)."""

    async def test_the_second_row_has_tabs_then_quiet_filters_then_sort(self) -> None:
        from app.ui.views.library_view import FilterChip

        shows = (plex_show("The Ark", "1001"), plex_show("Saltmarsh", "1002"))
        view = self.view(LibraryContents(shows=shows, checked_at=NOW))
        await view.load()
        await self.settle()
        row = view.kind_tabs_row
        tab_columns = [tab.grid_info()["column"] for tab in view.kind_tabs.values()]
        words = view._chip_row.grid_info()
        sort = view.sort_menu.master.grid_info()
        self.assertIs(view._chip_row.master, row)
        self.assertIs(view.sort_menu.master.master, row)
        self.assertEqual(words["row"], sort["row"])
        self.assertLess(max(tab_columns), words["column"])
        self.assertLess(words["column"], sort["column"])
        # The filters are words: no fill, muted, the chosen one in the text's colour with the
        # tabs' line under it.
        chips = view.filter_chips
        self.assertTrue(all(isinstance(chip, FilterChip) for chip in chips.values()))
        self.assertEqual([chip.text for chip in chips.values()], ["All", "Not followed", "With gaps", "Ended"])
        chosen, other = chips[LibraryFilter.ALL], chips[LibraryFilter.NOT_FOLLOWED]
        self.assertEqual(chosen.cget("fg_color"), "transparent")
        self.assertEqual(chosen.label.cget("text_color"), theme.TEXT)
        self.assertEqual(chosen.underline.cget("fg_color"), theme.BRAND)
        self.assertEqual(other.label.cget("text_color"), theme.TEXT_MUTED)
        self.assertEqual(other.underline.cget("fg_color"), "transparent")
        other.invoke()
        await self.settle()
        self.assertTrue(other.selected)
        self.assertFalse(chosen.selected)
        self.assertEqual(other.underline.cget("fg_color"), theme.BRAND)
        # Continue watching between the two rows (once it shows).
        view.continue_row.grid()
        self.assertLess(view.header.grid_info()["row"], view.continue_row.grid_info()["row"])
        self.assertLess(view.continue_row.grid_info()["row"], row.grid_info()["row"])

    async def test_a_show_card_has_the_follow_mark_and_no_buttons(self) -> None:
        from app.ui.views.library_view import FOLLOW_TIP, FOLLOWING_TIP, MARK_TAG, mark_look

        followed = LibraryShow(
            PlexTitle(kind="show", key="1", title="Saltmarsh", show_id=1), following=True
        )
        not_yet = LibraryShow(
            PlexTitle(kind="show", key="2", title="Copper Hills", show_id=2), gaps=3
        )
        service = FakeService(LibraryContents(), {"plex": LibraryContents(shows=(followed, not_yet))})
        gate = asyncio.Event()
        asked: list[str] = []

        async def follow(plex_title: PlexTitle, *, profile_id: int) -> bool:
            asked.append(plex_title.title)
            await gate.wait()
            return True

        service.follow = follow  # type: ignore[method-assign]
        opened: list[str] = []

        async def open_show(title: PlexTitle) -> bool:
            opened.append(title.title)
            return True

        view = self.view(LibraryContents(), service=service, on_open_show=open_show)
        await view.load()
        await self.settle()
        cards = {card.title.title: card for card in view.cards}
        done, card = cards["Saltmarsh"], cards["Copper Hills"]
        for one in (done, card):
            self.assertEqual(buttons(one), {})  # the poster only
            self.assertEqual(one._rows_height, 0)  # the text at the bottom, as Get started's
        # Followed: a green ✓ on a green edge; not yet: a muted + on a quiet edge.
        sheet = done._sheet
        assert done.mark_items is not None and card.mark_items is not None
        circle, sign = done.mark_items
        self.assertEqual(sheet.itemcget(sign, "text"), "✓")
        self.assertEqual(sheet.itemcget(sign, "fill"), theme.pick(theme.SUCCESS))
        self.assertEqual(card._sheet.itemcget(card.mark_items[1], "text"), "+")
        self.assertEqual(card._sheet.itemcget(card.mark_items[1], "fill"), theme.pick(theme.TEXT_MUTED))
        self.assertEqual(mark_look("tracked").edge, theme.SUCCESS)
        self.assertEqual(mark_look("idle").edge, theme.EDGE)
        self.assertEqual((mark_look("idle").tip, mark_look("tracked").tip), (t(FOLLOW_TIP), t(FOLLOWING_TIP)))
        self.assertEqual((NL[FOLLOW_TIP], NL[FOLLOWING_TIP]), ("Volgen", "Volgend"))
        # In the cover's top-right corner, as big as the gaps pill is tall, level with it.
        scaling = card.scaling
        x, y = card._sheet.coords(card.mark_items[0])
        side = card._mark_photo.width()
        self.assertAlmostEqual(x + side, round(card._units[0] * scaling) - round(10 * scaling), delta=1)
        gap_pill = card._badge_photos[0]
        self.assertEqual(side, gap_pill.height())
        self.assertEqual(y, card._sheet.coords(card._badge_items[0][0])[1])
        self.assertEqual(card.badge_text, "3 gaps")
        # A click on the +: "…" while it is added, then ✓, and the window doesn't open.
        with mock.patch.object(card._sheet, "find_withtag", return_value=card.mark_items):
            card._clicked(None)
        self.assertEqual(opened, [])
        card.press_follow()
        self.assertEqual(card.follow_state, "busy")
        self.assertEqual(card._sheet.itemcget(card.mark_items[1], "text"), "…")
        card.press_follow()  # a second click meanwhile: nothing more
        gate.set()
        await self.settle()
        self.assertEqual(asked, ["Copper Hills"])
        self.assertTrue(card.following)
        self.assertEqual(card._sheet.itemcget(card.mark_items[1], "text"), "✓")
        card.press_follow()  # ✓ does nothing, as the disabled ✓ Following button didn't
        await self.settle()
        self.assertEqual(asked, ["Copper Hills"])
        # Elsewhere on the cover: the window opens.
        card._clicked(None)
        await self.settle()
        self.assertEqual(opened, ["Copper Hills"])
        # The canvas binds the mark's click under its own tag.
        self.assertTrue(card._sheet.tag_bind(MARK_TAG, "<Button-1>"))
        # The hand over a +, the arrow over a ✓; its tip.
        done._set_mark_hot(True)
        self.assertEqual(str(done._sheet.cget("cursor")), "arrow")
        done._set_mark_hot(False)
        self.assertEqual(str(done._sheet.cget("cursor")), "hand2")
        # Light ↔ dark: the mark drawn again in the other colours.
        was = ctk.get_appearance_mode()
        ctk.set_appearance_mode("light" if was == "Dark" else "dark")
        try:
            await self.settle()
            self.assertEqual(sheet.itemcget(sign, "fill"), theme.pick(theme.SUCCESS))
            photo = done._mark_photo
            assert photo is not None
            middle = ImageTk.getimage(photo).getpixel((photo.width() // 2, photo.height() // 2))
            assert isinstance(middle, tuple)
            self.assertEqual(middle[:3], rgb(done, theme.BG))
        finally:
            ctk.set_appearance_mode(was.lower())
        # A film has no mark.
        view.set_kind(LibraryKindTab.FILMS)
        await self.settle()
        self.assertEqual(view.cards, [])

    async def test_upgrade_is_a_pill_on_the_cover(self) -> None:
        from app.ui.views.library_view import UPGRADE_TIP

        heat = PlexTitle(kind="movie", key="h", title="Heat", year=1995, resolution="720p", tmdb_id=949)
        dune = PlexTitle(kind="movie", key="d", title="Dune", year=2021, resolution="4K", tmdb_id=438631)
        service = FakeService(LibraryContents(), {"plex": LibraryContents(films=(heat, dune))})
        upgraded: list[str] = []
        opened: list[str] = []
        view = self.view(
            LibraryContents(), service=service,
            on_upgrade_film=lambda found: upgraded.append(found.title),
            on_open_film=lambda found: opened.append(found.title),
        )
        view.set_kind(LibraryKindTab.FILMS)
        await view.load()
        await self.settle()
        cards = {card.title.title: card for card in view.cards}
        heat_card, dune_card = cards["Heat"], cards["Dune"]
        self.assertEqual(buttons(heat_card), {})
        self.assertTrue(heat_card.can_upgrade)
        self.assertFalse(dune_card.can_upgrade)
        self.assertIsNone(dune_card.badge_text)
        [pill] = heat_card.badges
        self.assertEqual((pill.text, pill.tip), ("⬆ Upgrade", t(UPGRADE_TIP)))
        self.assertEqual(pill.color, theme.WARNING)  # amber
        self.assertIsNone(heat_card.mark_items)
        # A click on it: the upgrade, not the info window.
        items = heat_card._badge_items[0]
        self.assertTrue(heat_card._sheet.tag_bind(items[0], "<Button-1>"))
        with mock.patch.object(heat_card._sheet, "find_withtag", return_value=items):
            heat_card._clicked(None)
        heat_card.press_upgrade()
        self.assertEqual((upgraded, opened), (["Heat"], []))
        heat_card._clicked(None)  # elsewhere on the cover
        self.assertEqual(opened, ["Heat"])


class RealClickTests(PageCase):
    """Clicks as Tk delivers them (pointer moved there, button pressed and released on the
    cover's canvas): on the follow mark's "+" and on "⬆ Upgrade" they do their own thing and
    never open the window; elsewhere on the cover they open it."""

    async def shown(self, view: Any) -> None:
        self.root.deiconify()  # a generated click reaches only a shown window
        self.root.attributes("-alpha", 0.0)
        for _ in range(3):
            await self.settle()

    def click(self, card: Any, x: float, y: float) -> None:
        sheet = card._sheet
        x, y = round(x), round(y)
        sheet.event_generate("<Motion>", x=x, y=y)
        sheet.event_generate("<ButtonPress-1>", x=x, y=y)
        sheet.event_generate("<ButtonRelease-1>", x=x, y=y)
        sheet.update()

    def middle_of(self, card: Any, item: int) -> tuple[float, float]:
        box = card._sheet.bbox(item)
        assert box is not None
        return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2

    async def test_a_click_on_the_plus_follows_without_opening(self) -> None:
        show = LibraryShow(PlexTitle(kind="show", key="2", title="Copper Hills", show_id=2))
        service = FakeService(LibraryContents(), {"plex": LibraryContents(shows=(show,))})
        followed: list[str] = []

        async def follow(plex_title: PlexTitle, *, profile_id: int) -> bool:
            followed.append(plex_title.title)
            return True

        service.follow = follow  # type: ignore[method-assign]
        opened = mock.AsyncMock(return_value=True)
        view = self.view(LibraryContents(), service=service, on_open_show=opened)
        await view.load()
        await self.shown(view)
        [card] = view.cards
        assert card.mark_items is not None
        self.click(card, *self.middle_of(card, card.mark_items[0]))
        await self.settle()
        self.assertEqual(followed, ["Copper Hills"])
        self.assertTrue(card.following)
        opened.assert_not_called()
        # Clicked again, now a ✓: nothing happens, still no window.
        self.click(card, *self.middle_of(card, card.mark_items[0]))
        await self.settle()
        self.assertEqual(followed, ["Copper Hills"])
        opened.assert_not_called()
        # The middle of the cover: the show's window.
        width, height = card._sheet.winfo_width(), card._sheet.winfo_height()
        self.click(card, width / 2, height / 2)
        await self.settle()
        opened.assert_awaited_once()

    async def test_a_follow_that_fails_gives_the_plus_back(self) -> None:
        show = LibraryShow(PlexTitle(kind="show", key="2", title="Copper Hills", show_id=2))
        service = FakeService(LibraryContents(), {"plex": LibraryContents(shows=(show,))})

        async def follow(plex_title: PlexTitle, *, profile_id: int) -> bool:
            raise OSError("TVmaze away")

        service.follow = follow  # type: ignore[method-assign]
        view = self.view(LibraryContents(), service=service)
        await view.load()
        await self.settle()
        [card] = view.cards
        with self.assertLogs("app.ui.tasks", level="ERROR"):  # reported as before, by spawn
            card.press_follow()
            for _ in range(5):
                await self.settle()
        self.assertEqual(card.follow_state, "idle")  # "+" again, never stuck on "…"
        assert card.mark_items is not None
        self.assertEqual(card._sheet.itemcget(card.mark_items[1], "text"), "+")

    async def test_a_click_on_upgrade_upgrades_without_opening(self) -> None:
        heat = PlexTitle(kind="movie", key="h", title="Heat", year=1995, resolution="720p", tmdb_id=949)
        service = FakeService(LibraryContents(), {"plex": LibraryContents(films=(heat,))})
        upgraded: list[str] = []
        opened: list[str] = []
        view = self.view(
            LibraryContents(), service=service,
            on_upgrade_film=lambda found: upgraded.append(found.title),
            on_open_film=lambda found: opened.append(found.title),
        )
        view.set_kind(LibraryKindTab.FILMS)
        await view.load()
        await self.shown(view)
        [card] = view.cards
        pill, _text = card._badge_items[0]
        self.click(card, *self.middle_of(card, pill))
        await self.settle()
        self.assertEqual((upgraded, opened), (["Heat"], []))
        width, height = card._sheet.winfo_width(), card._sheet.winfo_height()
        self.click(card, width / 2, height / 2)
        await self.settle()
        self.assertEqual((upgraded, opened), (["Heat"], ["Heat"]))


if __name__ == "__main__":
    unittest.main()
