"""The Library page with the folder library (docs/folder-library/, mockups/folder-library.html
state 4): it shows with either source and follows the folder library's status, says where its
titles come from in the folders' words, has no 📱 on its cards, and lists the files it couldn't
recognise at the bottom with "Tidy up in MediaForge" (the paths handed to MediaForge's page,
nothing moved here). On a hidden Tk root with stand-ins; nothing reads a folder."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.i18n import t
from app.i18n_nl import NL
from app.models import Settings
from app.models.folder_library import FolderLibraryStatus
from app.models.plex import PlexTitle
from app.services.plex_library import LibraryContents
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


class FakeService:
    def __init__(self, contents: LibraryContents) -> None:
        self._contents = contents
        self.available = True

    async def contents(self, *, profile_id: int) -> LibraryContents:
        return self._contents

    async def follow(self, plex_title: PlexTitle, *, profile_id: int) -> bool:
        return True


def film(name: str) -> PlexTitle:
    return PlexTitle(kind="movie", key=f"folder:movie:{name}", title=name, year=2024, tmdb_id=1)


class FolderPageTests(DialogTestCase):
    def view(self, contents: LibraryContents, **extra: Any) -> Any:
        from app.ui.views.library_view import LibraryView

        view = LibraryView(
            self.root,
            service=FakeService(contents),  # type: ignore[arg-type]
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

        # No 📱 on a folder library's cards, even with a phone to send to (ruling 3).
        phone = SimpleNamespace(available=True, phones=lambda: ["a phone"])
        stand_in: Any = SimpleNamespace(
            _services=SimpleNamespace(phone=phone, settings=service)
        )
        self.assertFalse(MainWindow._can_send_to_phone(stand_in))
        stand_in._services.settings = SimpleNamespace(current=Settings(plex_token="made-up"))
        self.assertTrue(MainWindow._can_send_to_phone(stand_in))

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
        from app.services.plex_library import LibraryKindTab

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
