"""Plex in the UI: Settings → Plex (app/ui/plex_settings.py: not connected, connected with its
counts, libraries and buttons, a problem, several servers, Disconnect's question), the connect
window (app/ui/plex_connect.py: the code, plex.tv/link, the token arriving, an expired code,
Plex not answering, Cancel), the show window's and the film info window's quiet ✓ "In Plex"
(no options: there are no marks of your own), the film card's, and the show window following
Plex being connected or disconnected (3.1.0: without Plex no have / missing cues at all). On a
hidden Tk root with a stand-in PlexService; nothing reaches plex.tv."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app.models import Episode, Settings, Show, ShowDetails
from app.models.plex import (
    PlexCounts,
    PlexFilms,
    PlexLibrary,
    PlexPin,
    PlexProblem,
    PlexServer,
    PlexStatus,
)
from app.services.plex import LINK_URL, PlexError
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.missing_control import HAVE_MARK, PLEX_TIP
from app.ui.plex_connect import (
    EXPIRED_TEXT,
    NO_ANSWER_TEXT,
    WAITING_TEXT,
    PlexConnectWindow,
)
from app.ui.plex_settings import (
    CHECK_NOW_TEXT,
    CHECKING_TEXT,
    CONNECT_TEXT,
    DISCONNECT_TEXT,
    INTRO_TEXT,
    NOT_CHECKED_TEXT,
    PlexBox,
    counts_text,
    problem_text,
    server_names,
    status_text,
)
from app.ui.settings_layout import SettingsPage
from app.ui.show_details import mark_text, row_mark
from tests.test_backup_dialog import FakeProwlarr, FakeRepository, FakeShortcut, buttons, texts
from tests.test_season_downloads import SeasonWindowTestCase

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
SERVER = PlexServer(machine_id="a1b2", name="Alex-NAS")
LAPTOP = PlexServer(machine_id="b2b2", name="Alex-Laptop")
LIBRARIES = (
    PlexLibrary(key="1", title="TV Shows", kind="show"),
    PlexLibrary(key="2", title="Movies", kind="movie"),
    PlexLibrary(key="3", title="Home videos", kind="movie", on=False),
)
COUNTS = PlexCounts(
    episodes=812, shows=24, films=146, unmatched_shows=3, checked_at=NOW - timedelta(minutes=3)
)
CONNECTED = PlexStatus(
    connected=True, server=SERVER, servers=(SERVER,), libraries=LIBRARIES, counts=COUNTS
)


class FakePlex:
    """PlexService's face for the UI: a status to show, and what the buttons asked."""

    def __init__(self, status: PlexStatus = PlexStatus()) -> None:
        self.value = status
        self.listeners: list[Any] = []
        self.calls: list[tuple[Any, ...]] = []
        self.pin = PlexPin(id=1, code="K7QX", expires_at=NOW + timedelta(minutes=15))
        self.pin_error: Exception | None = None
        self.answer: asyncio.Future[bool] | None = None
        self.waits = 0
        self.cancelled = False

    async def status(self) -> PlexStatus:
        return self.value

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None

    def set(self, status: PlexStatus) -> None:
        self.value = status
        for listener in list(self.listeners):
            listener()

    async def check(self, *, again: bool = False) -> None:
        self.calls.append(("check",))

    async def choose_server(self, machine_id: str) -> None:
        self.calls.append(("choose", machine_id))

    async def set_library(self, key: str, on: bool) -> None:
        self.calls.append(("library", key, on))

    async def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    async def new_pin(self) -> PlexPin:
        if self.pin_error is not None:
            raise self.pin_error
        return self.pin

    async def wait_for_pin(self, pin: PlexPin) -> bool:
        self.waits += 1
        self.answer = asyncio.get_running_loop().create_future()
        try:
            return await self.answer
        except asyncio.CancelledError:
            self.cancelled = True
            raise


# -- texts (pure) -----------------------------------------------------------------------------------


class TextTests(unittest.TestCase):
    def test_counts(self) -> None:
        self.assertEqual(
            counts_text(COUNTS, NOW),
            "812 episodes of 24 of your series · 146 films · checked 3 min ago",
        )
        one = PlexCounts(episodes=1, shows=1, films=1, checked_at=NOW)
        self.assertEqual(counts_text(one, NOW), "1 episode of 1 of your series · 1 film · checked just now")
        self.assertEqual(counts_text(PlexCounts(), NOW), NOT_CHECKED_TEXT)

    def test_problems_in_plain_words(self) -> None:
        two_hours = PlexCounts(checked_at=NOW - timedelta(hours=2))
        self.assertEqual(
            problem_text(PlexProblem.UNREACHABLE, two_hours, NOW),
            "Plex didn't answer: the last list from 2 h ago is used",
        )
        self.assertEqual(problem_text(PlexProblem.UNREACHABLE, PlexCounts(), NOW), "Plex didn't answer")
        self.assertEqual(
            problem_text(PlexProblem.SIGNED_OUT, two_hours, NOW), "Plex asks to connect again"
        )
        self.assertEqual(
            problem_text(PlexProblem.FAILED, two_hours, NOW),
            "Plex's answer didn't read: the last list from 2 h ago is used",
        )

    def test_status_line(self) -> None:
        text, _colour = status_text(CONNECTED, NOW)
        self.assertEqual(text, "Connected to Alex-NAS")
        nameless = CONNECTED.model_copy(update={"server": None})
        self.assertEqual(status_text(nameless, NOW)[0], "Connected to your Plex server")

    def test_server_names(self) -> None:
        twin = PlexServer(machine_id="c3c3c3c3", name="Alex-NAS")
        self.assertEqual(
            server_names((SERVER, LAPTOP, twin)),
            {"Alex-NAS": "a1b2", "Alex-Laptop": "b2b2", "Alex-NAS (c3c3c3)": "c3c3c3c3"},
        )


# -- on a Tk root -------------------------------------------------------------------------------------


class TkTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 10.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def settle(self) -> None:
        for _ in range(8):
            await asyncio.sleep(0.01)
            self.root.update()

    def keep(self, widget: Any) -> None:
        self.addCleanup(lambda: widget.destroy() if widget.winfo_exists() else None)


class BoxTests(TkTestCase):
    async def box(self, fake: FakePlex) -> PlexBox:
        frame = ctk.CTkFrame(self.root)
        self.keep(frame)
        page = SettingsPage(frame, "Plex", "A note")
        page.frame.pack()
        box = PlexBox(page, fake, clock=lambda: NOW)  # type: ignore[arg-type]
        await box.loaded()
        return box

    async def test_not_connected(self) -> None:
        box = await self.box(FakePlex())
        self.assertIn(INTRO_TEXT, texts(box.content))
        self.assertEqual(list(buttons(box.content)), [CONNECT_TEXT])
        self.assertIsNone(box.check_button)

    async def test_connected(self) -> None:
        fake = FakePlex(CONNECTED)
        box = await self.box(fake)
        assert box.status_label is not None and box.counts_label is not None
        self.assertEqual(box.status_label.cget("text"), "Connected to Alex-NAS")
        self.assertEqual(
            box.counts_label.cget("text"),
            "812 episodes of 24 of your series · 146 films · checked 3 min ago",
        )
        self.assertEqual(
            {key: box.library_boxes[key].get() for key in box.library_boxes},
            {"1": 1, "2": 1, "3": 0},
        )
        self.assertEqual(set(buttons(box.content)), {CHECK_NOW_TEXT, DISCONNECT_TEXT})
        self.assertIsNone(box.server_menu)  # one server: no menu

        box.library_boxes["2"].toggle()
        assert box.check_button is not None
        box.check_button.invoke()
        self.assertEqual(box.check_button.cget("text"), CHECKING_TEXT)
        await self.settle()
        self.assertEqual(fake.calls, [("library", "2", False), ("check",)])

    async def test_it_follows_the_service(self) -> None:
        fake = FakePlex()
        box = await self.box(fake)
        fake.set(CONNECTED.model_copy(update={"checking": True}))
        await box.loaded()
        assert box.check_button is not None
        self.assertEqual(box.check_button.cget("text"), CHECKING_TEXT)
        self.assertEqual(str(box.check_button.cget("state")), "disabled")
        box.content.destroy()
        self.assertEqual(fake.listeners, [])  # it stops listening when it goes

    async def test_a_problem_and_connecting_again(self) -> None:
        unreachable = CONNECTED.model_copy(
            update={
                "problem": PlexProblem.UNREACHABLE,
                "counts": COUNTS.model_copy(update={"checked_at": NOW - timedelta(hours=2)}),
            }
        )
        box = await self.box(FakePlex(unreachable))
        assert box.status_label is not None
        self.assertEqual(
            box.status_label.cget("text"), "Plex didn't answer: the last list from 2 h ago is used"
        )
        self.assertIsNone(box.connect_button)
        refused = CONNECTED.model_copy(update={"problem": PlexProblem.SIGNED_OUT})
        box = await self.box(FakePlex(refused))
        assert box.status_label is not None
        self.assertEqual(box.status_label.cget("text"), "Plex asks to connect again")
        self.assertIsNotNone(box.connect_button)

    async def test_several_servers(self) -> None:
        fake = FakePlex(
            CONNECTED.model_copy(
                update={
                    "server": None,
                    "servers": (SERVER, LAPTOP),
                    "problem": PlexProblem.CHOOSE_SERVER,
                    "libraries": (),
                    "counts": PlexCounts(),
                }
            )
        )
        box = await self.box(fake)
        assert box.server_menu is not None
        self.assertEqual(box.server_menu.cget("values"), ["Alex-NAS", "Alex-Laptop"])
        box.server_menu._command("Alex-Laptop")  # the menu's choice
        await self.settle()
        self.assertEqual(fake.calls, [("choose", "b2b2")])

    async def test_disconnect_asks_first(self) -> None:
        fake = FakePlex(CONNECTED)
        box = await self.box(fake)
        buttons(box.content)[DISCONNECT_TEXT].invoke()
        dialog = box.confirm
        assert isinstance(dialog, ConfirmDialog)
        self.keep(dialog)
        self.assertEqual(fake.calls, [])
        dialog.confirm()
        await self.settle()
        self.assertEqual(fake.calls, [("disconnect",)])
        self.assertFalse(dialog.winfo_exists())

    async def test_connect_opens_the_window(self) -> None:
        fake = FakePlex()
        box = await self.box(fake)
        buttons(box.content)[CONNECT_TEXT].invoke()
        window = box.connect_window
        assert isinstance(window, PlexConnectWindow)
        self.keep(window)
        await self.settle()
        assert fake.answer is not None
        fake.answer.set_result(True)
        await self.settle()
        self.assertFalse(window.winfo_exists())
        self.assertEqual(fake.calls, [("check",)])  # the first check runs


class ConnectWindowTests(TkTestCase):
    def window(self, fake: FakePlex, **extra: Any) -> PlexConnectWindow:
        window = PlexConnectWindow(self.root, plex=fake, **extra)  # type: ignore[arg-type]
        self.keep(window)
        return window

    async def test_the_code_and_the_link(self) -> None:
        opened: list[str] = []
        connected: list[bool] = []
        fake = FakePlex()
        window = self.window(fake, open_link=opened.append, on_connected=lambda: connected.append(True))
        await self.settle()
        self.assertEqual(window.code.cget("text"), "K7QX")
        self.assertEqual(window.step, "waiting")
        self.assertEqual(window.status.cget("text"), WAITING_TEXT)
        window.open_button.invoke()
        self.assertEqual(opened, [LINK_URL])
        assert fake.answer is not None
        fake.answer.set_result(True)
        await self.settle()
        self.assertFalse(window.winfo_exists())  # closes by itself
        self.assertEqual(connected, [True])

    async def test_an_expired_code_offers_a_new_one(self) -> None:
        fake = FakePlex()
        window = self.window(fake)
        await self.settle()
        assert fake.answer is not None
        fake.answer.set_result(False)
        await self.settle()
        self.assertEqual(window.step, "expired")
        self.assertEqual(window.status.cget("text"), EXPIRED_TEXT)
        self.assertEqual(window.new_code_button.winfo_manager(), "pack")
        fake.pin = PlexPin(id=2, code="Z9Y8", expires_at=NOW + timedelta(minutes=15))
        window.new_code_button.invoke()
        await self.settle()
        self.assertEqual(window.code.cget("text"), "Z9Y8")
        self.assertEqual(window.step, "waiting")
        self.assertEqual(fake.waits, 2)

    async def test_plex_not_answering(self) -> None:
        fake = FakePlex()
        fake.pin_error = PlexError("down")
        window = self.window(fake)
        await self.settle()
        self.assertEqual(window.step, "failed")
        self.assertEqual(window.status.cget("text"), NO_ANSWER_TEXT)
        self.assertEqual(window.new_code_button.winfo_manager(), "pack")

    async def test_cancel_stops_waiting(self) -> None:
        connected: list[bool] = []
        fake = FakePlex()
        window = self.window(fake, on_connected=lambda: connected.append(True))
        await self.settle()
        window.cancel_button.invoke()
        await self.settle()
        self.assertFalse(window.winfo_exists())
        self.assertTrue(fake.cancelled)
        self.assertEqual(connected, [])


class SettingsSectionTests(TkTestCase):
    async def test_the_plex_section(self) -> None:
        from app.services import SettingsService
        from app.ui.settings_dialog import SECTION_PLEX, SettingsDialog

        dialog = SettingsDialog(
            self.root,
            SettingsService(FakeRepository(), Settings()),  # type: ignore[arg-type]
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="3.1.0"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            plex=FakePlex(CONNECTED),  # type: ignore[arg-type]
        )
        self.keep(dialog)
        items = list(dialog._items)
        self.assertIn(SECTION_PLEX, items)
        self.assertEqual(items.index(SECTION_PLEX), 3)  # last of Downloading
        self.assertNotIn("Subtitles", items)  # MediaForge's now
        assert dialog.plex_box is not None
        await dialog.plex_box.loaded()
        self.assertIn("Connected to Alex-NAS", texts(dialog.page(SECTION_PLEX)))


# -- the marks --------------------------------------------------------------------------------------


class MarkTests(unittest.TestCase):
    def test_the_show_windows_plex_mark(self) -> None:
        aired = NOW - timedelta(days=3)
        episodes = tuple(
            Episode(id=n, show_id=1, season=1, number=n, airstamp=aired, air_date=aired.date())
            for n in (1, 2, 3)
        )
        details = ShowDetails(
            show=Show(id=1, name="The Ark"),
            episodes=episodes,
            tracked=True,
            plex_connected=True,
            latest_season=1,
            downloaded=frozenset({2, 3}),
            in_plex=frozenset({1, 3}),
        )
        # 2 was grabbed but isn't in Plex yet: no ✓ (only Plex counts as had), not missing.
        self.assertEqual([row_mark(details, e) for e in episodes], ["plex", None, "plex"])
        self.assertEqual(mark_text("plex", episodes[0], NOW), HAVE_MARK)


class ShowWindowTests(SeasonWindowTestCase):
    async def test_the_plex_mark_has_its_tip_and_no_options(self) -> None:
        from app.ui import show_details
        from tests.test_season_downloads import S2, details

        info = details(in_plex=frozenset({S2[0].id}))
        from app.ui.tooltip import add_tooltip

        with mock.patch.object(show_details, "add_tooltip", wraps=add_tooltip) as tips:
            window = self.open(mock.MagicMock(), info)
            await self.ready(window, 2)
        row = window._rows[S2[0].id]
        self.assertEqual(row.mark, "plex")
        assert row._mark_slot is not None
        [label] = row._mark_slot.winfo_children()
        self.assertEqual(label.cget("text"), HAVE_MARK)
        self.assertIn(mock.call(label, PLEX_TIP), tips.call_args_list)
        # No ⋯ and no option menus: there are no marks of your own.
        self.assertFalse(hasattr(row, "more") or hasattr(window, "_open_mark_menu"))

    async def test_connecting_and_disconnecting_plex_re_renders_the_window(self) -> None:
        from tests.test_season_downloads import S2, details

        listeners: list[Any] = []
        window = self.open(mock.MagicMock(), details())
        listeners.append(self.shows.reload_all)  # as ShowWindows subscribes to the library
        await self.ready(window, 2)
        row = window._rows[S2[1].id]
        self.assertEqual(row.mark, "missing")
        assert window._missing_label is not None
        # Disconnected: the library tells its listeners (PlexService's on_change).
        self.library.info = details(
            plex_connected=False, latest_season=None, missing=frozenset(), in_plex=frozenset()
        )
        for listener in listeners:
            listener()
        await self.ready(window)
        self.assertTrue(all(r.mark is None and r._mark_slot is None for r in window._rows.values()))
        self.assertIsNone(window._missing_label)
        control = window._season_actions  # no "All missing" without Plex
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ Whole season")
        # Connected again: the cues come back.
        self.library.info = details()
        for listener in listeners:
            listener()
        await self.ready(window)
        self.assertEqual(window._rows[S2[1].id].mark, "missing")
        assert window._missing_label is not None
        self.assertEqual(window._missing_label.cget("text"), "1 missing")


class FilmCardTests(TkTestCase):
    """The quiet ✓ on the watchlist shelf's and the search results' film cards."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import tempfile
        from pathlib import Path

        from app.database import Database
        from app.services.watchlist import WatchlistService
        from app.ui.download_control import DownloadActions, DownloadStates
        from tests.test_watchlist import FakeDiscovery, downloads_service

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "library.db")
        self.addAsyncCleanup(self.db.close)
        downloads = downloads_service(self.db, [], Path(tmp.name) / "torrents")
        self.addAsyncCleanup(downloads._http.aclose)
        self.watchlist = WatchlistService(self.db.watchlist, FakeDiscovery(), downloads)
        self.actions: Any = DownloadActions(
            download=lambda _m: None, choose=lambda _m: None, states=DownloadStates()
        )
        self.films = PlexFilms(tmdb=frozenset({1}), imdb=frozenset({"tt0111161"}))

    async def plex(self) -> PlexFilms:
        return self.films

    async def test_the_watchlist_shelf(self) -> None:
        from app.models import Movie
        from app.ui.watchlist_cards import WatchlistShelf
        from tests.test_info_window import FakeImages

        for movie_id in (1, 2, 3):
            film = Movie.model_validate({"id": movie_id, "title": f"Film {movie_id}"})
            await self.watchlist.add(film, profile_id=1)
        # Film 2 is in Plex by its IMDb id only (TMDB's details gave the list its IMDb id).
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE watchlist SET imdb_id = 'tt0111161' WHERE movie_id = 2")
        await self.watchlist.load(1)
        body = ctk.CTkFrame(self.root)
        body.pack()
        self.keep(body)
        shelf = WatchlistShelf(
            body,
            watchlist=self.watchlist,
            images=cast(Any, FakeImages()),
            actions=self.actions,
            plex=self.plex,
        )
        shelf.start()
        await self.settle()
        marks = {movie_id: card.plex_mark for movie_id, card in shelf.cards.items()}
        self.assertIsNotNone(marks[1])
        self.assertIsNotNone(marks[2])
        self.assertIsNone(marks[3])
        mark = marks[1]
        assert mark is not None
        self.assertEqual((mark.cget("text"), mark.tip.text), (HAVE_MARK, PLEX_TIP))
        self.films = PlexFilms()  # gone from Plex: the next load takes the ✓ away
        shelf.start()
        await self.settle()
        self.assertEqual([card.plex_mark for card in shelf.cards.values()], [None, None, None])

    async def test_the_search_results(self) -> None:
        from app.models import Movie
        from app.services.tmdb import TMDBError
        from app.ui.media_cards import MovieCard
        from app.ui.search import SearchResultsWindow
        from tests.test_info_window import FakeImages, FakeWatchlist

        films = [Movie.model_validate({"id": n, "title": f"Film {n}"}) for n in (1, 2)]
        outer = self

        class Discovery:
            async def search_movies(self, query: str, *, limit: int) -> list[Movie]:
                return films

            async def plex_films(self) -> PlexFilms:
                return outer.films

            async def movie_details(self, movie_id: int) -> Any:
                raise TMDBError("no details here")

        class Library:
            async def search(self, query: str, *, profile_id: int, limit: int) -> list[Any]:
                return []

        window = SearchResultsWindow(
            self.root,
            library=cast(Any, Library()),
            images=cast(Any, FakeImages()),
            discovery=cast(Any, Discovery()),
            status=lambda text, clear_after_ms=0: None,
            query="film",
            movie_actions=self.actions,
            watchlist=cast(Any, FakeWatchlist()),
            on_open_settings=lambda: None,
            profile_id=1,
        )
        self.keep(window)
        await self.settle()
        await self.settle()
        cards = [w for w in _descendants(window) if isinstance(w, MovieCard)]
        self.assertEqual(len(cards), 2)
        self.assertEqual([card.plex_mark is not None for card in cards], [True, False])


def _descendants(widget: Any) -> list[Any]:
    found: list[Any] = []
    for child in widget.winfo_children():
        found.append(child)
        found += _descendants(child)
    return found


class WidgetMarkTests(TkTestCase):
    async def test_the_film_cards_mark(self) -> None:
        from app.ui.media_cards import PlexMark

        mark = PlexMark(self.root)
        self.keep(mark)
        self.assertEqual(mark.cget("text"), HAVE_MARK)
        self.assertEqual(mark.tip.text, PLEX_TIP)


if __name__ == "__main__":
    unittest.main()
