"""The Library page with the folder library (docs/folder-library/, mockups/folder-library.html
state 4): it shows with either source and follows the folder library's status, says where its
titles come from in the folders' words, offers 📱 Send to phone on its cards while the phone
connection is there (part 2), and lists the files it couldn't
recognise at the bottom with "Tidy up in MediaForge" (the paths handed to MediaForge's page,
nothing moved here). On a hidden Tk root with stand-ins; nothing reads a folder."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.i18n import t
from app.i18n_nl import NL
from app.models import Settings
from app.models.folder_library import FolderLibraryStatus
from app.models import PairedPhone, Profile
from app.models.plex import PlexTitle, PlexVideo
from app.services.plex_library import LibraryContents, LibraryKindTab, LibraryShow, Shelf
from app.single_instance import ForgeRequest
from app.ui import library_words, theme
from app.ui.main_window import MainWindow, Mode
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

        view = LibraryView(
            self.root,
            service=service or FakeService(contents),  # type: ignore[arg-type]
            posters=mock.Mock(get=mock.AsyncMock(return_value=None)),
            settings=self.settings(),
            status=lambda text, clear_after_ms=None: None,
            on_open_show=mock.AsyncMock(return_value=True),
            on_open_film=lambda found: None,
            **extra,
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

        # 📱 with the folder library too, while the phone connection is there (part 2).
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

        # The card has it.
        sent: list[PlexTitle] = []
        view = self.view(
            LibraryContents(films=(film("Nightfall"),), checked_at=NOW),
            on_send=sent.append,
            can_send=lambda: True,
        )
        view.set_kind(LibraryKindTab.FILMS)
        await view.load()
        await self.settle()
        [card] = view.cards
        assert card.send_button is not None
        card.send_button.invoke()
        self.assertEqual([one.key for one in sent], ["folder:movie:Nightfall"])

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
                phone=SimpleNamespace(phones=lambda: [PHONE], people=[PERSON]),
                profiles=SimpleNamespace(current_id=PERSON.id),
                phone_sends=FakeSends(),
                plex=SimpleNamespace(episodes=plex_episodes),
                folder_playing=SimpleNamespace(show_videos=show_videos),
            ),
            set_status=lambda text, clear_after_ms=None: None,
        )
        with mock.patch("app.ui.main_window.SendToPhoneDialog") as made:
            MainWindow._send_to_phone(window, show)
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


if __name__ == "__main__":
    unittest.main()


class ResyncTests(PageCase):
    """The header's "↻ Check now": the source read again, then the page."""

    async def test_no_button_without_resync(self) -> None:
        view = self.view(LibraryContents())
        self.assertIsNone(view.resync_button)

    async def test_check_now_reads_again_then_loads_the_page(self) -> None:
        from app.ui.views.library_view import RESYNC_TEXT, RESYNCING_TEXT

        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def resync() -> None:
            calls.append("resync")
            started.set()
            await release.wait()

        view = self.view(LibraryContents(), resync=resync)
        view.load = mock.AsyncMock(side_effect=lambda: calls.append("load"))
        view.resync_button.invoke()
        await asyncio.wait_for(started.wait(), 2)
        self.assertEqual(view.resync_button.cget("state"), "disabled")
        self.assertEqual(view.resync_button.cget("text"), t(RESYNCING_TEXT))
        view.resync()  # a second click while it runs: nothing more
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if view.resync_button.cget("state") == "normal":
                break
        self.assertEqual(calls, ["resync", "load"])
        self.assertEqual(view.resync_button.cget("text"), t(RESYNC_TEXT))

    async def test_a_failed_check_gives_the_button_back(self) -> None:
        async def resync() -> None:
            raise OSError("NAS asleep")

        view = self.view(LibraryContents(), resync=resync)
        with self.assertLogs("app.ui.tasks", level="ERROR"):
            view.resync_button.invoke()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if view.resync_button.cget("state") == "normal":
                    break
        self.assertEqual(view.resync_button.cget("state"), "normal")

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

        # Both: the switch, above the Shows / Films tabs, each shelf with its count.
        service.on = dict(shelves)
        await view.load()
        await self.settle()
        self.assertTrue(view.shelf_switch_shown)
        self.assertLess(
            view.shelf_switch.grid_info()["row"], view.kind_tabs_row.grid_info()["row"]
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
