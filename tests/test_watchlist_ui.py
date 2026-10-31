"""The watchlist in the UI: the status texts, the button's looks, the movie events' notices,
and WatchlistButton / WatchlistShelf on a real (hidden) Tk root over a real WatchlistService."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import customtkinter as ctk

from app.database import Database
from app.models import (
    AutoMovieDownloaded,
    AutoMovieGaveUp,
    Movie,
    MovieDetails,
    WatchlistEntry,
)
from app.services.watchlist import WatchlistService
from app.ui import theme
from app.ui.download_control import DownloadActions, DownloadStates
from app.ui.main_window import AutoNotice, AutoNoticeThrottle, MainWindow, auto_notice
from app.ui.watchlist_button import (
    ADD_TEXT,
    ADD_TIP,
    ON_TEXT,
    ON_TIP,
    WatchlistButton,
    watch_look,
    watch_text,
)
from app.ui.watchlist_cards import (
    WatchlistShelf,
    WatchStatus,
    film_count,
    imdb_url,
    release_text,
    searched_ago,
    tone_color,
    watch_status,
)
from tests.test_watchlist import FakeDiscovery, downloads_service

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


def movie(movie_id: int = 1, title: str = "Dune: Part Three", **fields: object) -> Movie:
    return Movie.model_validate({"id": movie_id, "title": title, **fields})


def entry(**fields: object) -> WatchlistEntry:
    data: dict[str, object] = {"movie": movie(), "added_at": NOW - timedelta(days=10)}
    data.update(fields)
    return WatchlistEntry.model_validate(data)


async def until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=5)


# -- texts -------------------------------------------------------------------------------------


class TextTests(unittest.TestCase):
    def test_film_count(self) -> None:
        self.assertEqual(film_count(1), "1 film")
        self.assertEqual(film_count(4), "4 films")

    def test_searched_ago(self) -> None:
        cases = {
            timedelta(seconds=20): "just now",
            timedelta(minutes=25): "25 min ago",
            timedelta(hours=2, minutes=40): "2 h ago",
            timedelta(days=1, hours=3): "1 day ago",
            timedelta(days=3): "3 days ago",
            timedelta(minutes=-5): "just now",  # a clock that went back
        }
        for age, text in cases.items():
            with self.subTest(age=age):
                self.assertEqual(searched_ago(NOW - age, NOW), text)

    def test_release_text(self) -> None:
        self.assertEqual(
            release_text(entry(digital_release="2026-11-12"), TODAY), "Out digitally 12 Nov"
        )
        self.assertEqual(
            release_text(entry(digital_release="2027-01-05"), TODAY), "Out digitally 05 Jan 2027"
        )
        self.assertEqual(
            release_text(entry(theatrical_release="2026-12-16"), TODAY), "In cinemas 16 Dec"
        )
        # The cinema release has been, no digital date: AirGrabber's estimate (60 days on).
        self.assertEqual(
            release_text(entry(theatrical_release="2026-10-01"), TODAY), "Expected 30 Nov"
        )
        self.assertEqual(
            release_text(entry(movie=movie(release_date="2026-12-25")), TODAY), "In cinemas 25 Dec"
        )
        self.assertEqual(release_text(entry(), TODAY), "Release date unknown")

    def test_imdb_url(self) -> None:
        self.assertEqual(
            imdb_url(entry(imdb_id="tt15239678")), "https://www.imdb.com/title/tt15239678/"
        )
        self.assertTrue(imdb_url(entry()).startswith("https://www.imdb.com/find/?q=Dune"))


class WatchStatusTests(unittest.TestCase):
    def test_not_out_yet(self) -> None:
        self.assertEqual(
            watch_status(entry(digital_release="2026-11-12"), TODAY, NOW),
            WatchStatus("Out digitally 12 Nov", "soon", chip=True, bottom="switch"),
        )
        # The switch shows either way: it can be turned on before the film is out.
        status = watch_status(entry(digital_release="2026-11-12", auto_download=False), TODAY, NOW)
        self.assertEqual(status.bottom, "switch")

    def test_out_and_looked_for(self) -> None:
        self.assertEqual(
            watch_status(entry(digital_release="2026-10-01"), TODAY, NOW),
            WatchStatus("Waiting for a good release", "waiting", chip=False, bottom="switch"),
        )
        searched = entry(digital_release="2026-10-01", tries=1, last_try=NOW - timedelta(hours=2))
        self.assertEqual(
            watch_status(searched, TODAY, NOW).text, "Waiting for a good release (searched 2 h ago)"
        )

    def test_out_with_the_switch_off(self) -> None:
        self.assertEqual(
            watch_status(entry(digital_release="2026-10-01", auto_download=False), TODAY, NOW),
            WatchStatus("Out now", "out", chip=True, bottom="download"),
        )

    def test_given_up(self) -> None:
        film = entry(digital_release="2026-09-01", gave_up=True, auto_download=False, tries=9)
        self.assertEqual(
            watch_status(film, TODAY, NOW),
            WatchStatus("No good release found in 30 days", "gave_up", chip=False, bottom="download"),
        )

    def test_tones(self) -> None:
        self.assertEqual(tone_color("out"), theme.WARNING)
        self.assertEqual(tone_color("gave_up"), theme.WARNING)
        self.assertEqual(tone_color("soon"), theme.TEXT_MUTED)
        self.assertEqual(tone_color("waiting"), theme.TEXT_MUTED)


class WatchLookTests(unittest.TestCase):
    def test_off_and_on(self) -> None:
        off, on = watch_look(False), watch_look(True)
        self.assertEqual((off.texts, off.color, off.tip), ((ADD_TEXT, "+"), theme.ACCENT, ADD_TIP))
        self.assertEqual((on.texts, on.color, on.tip), ((ON_TEXT, "✓"), theme.GRABBED, ON_TIP))

    def test_narrow_buttons_show_the_sign_only_never_elided(self) -> None:
        def measure(text: str) -> float:
            return len(text) * 7.0

        self.assertEqual(watch_text((ON_TEXT, "✓"), 200, measure), ON_TEXT)
        self.assertEqual(watch_text((ON_TEXT, "✓"), 50, measure), "✓")
        self.assertEqual(watch_text((ON_TEXT, "✓"), 3, measure), "✓")


class MovieNoticeTests(unittest.TestCase):
    def test_downloaded(self) -> None:
        self.assertEqual(
            auto_notice(AutoMovieDownloaded(title="Dune: Part Three", quality="2160p")),
            AutoNotice(
                title="Downloaded Dune: Part Three",
                body="2160p · picked automatically from your watchlist",
                status="Downloaded Dune: Part Three (picked automatically from your watchlist)",
            ),
        )
        notice = auto_notice(AutoMovieDownloaded(title="Dune: Part Three"))
        self.assertEqual(notice.body, "Picked automatically from your watchlist")

    def test_gave_up(self) -> None:
        notice = auto_notice(AutoMovieGaveUp(title="Weapons"))
        self.assertEqual(notice.title, "Couldn't find Weapons")
        self.assertEqual(
            notice.body,
            "No good download turned up in 30 days. "
            "Choose one yourself from your watchlist on the Movies page.",
        )

    def test_a_downloaded_film_also_goes_to_the_status_bar(self) -> None:
        notes: list[tuple[str, str]] = []
        status: list[str] = []
        window: Any = SimpleNamespace(
            _auto_throttle=AutoNoticeThrottle(clock=lambda: 0.0),
            tray=SimpleNamespace(running=True, notify=lambda t, b: notes.append((t, b))),
            _report=lambda text, ms: status.append(text),
        )
        MainWindow._on_auto_event(window, AutoMovieDownloaded(title="Sinners", quality="1080p"))
        MainWindow._on_auto_event(window, AutoMovieGaveUp(title="Weapons"))
        self.assertEqual(
            [title for title, _ in notes], ["Downloaded Sinners", "Couldn't find Weapons"]
        )
        self.assertEqual(status, ["Downloaded Sinners (picked automatically from your watchlist)"])


# -- widgets -----------------------------------------------------------------------------------


class FakeImages:
    async def get(self, *args: object, **kwargs: object) -> None:
        return None


class WidgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building cards is slow
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.discovery = FakeDiscovery()
        downloads = downloads_service(self.db, [], Path(tmp.name) / "torrents")
        self.addAsyncCleanup(downloads._http.aclose)
        self.watchlist = WatchlistService(self.db.watchlist, self.discovery, downloads)
        self.actions: DownloadActions[Movie] = DownloadActions(
            download=lambda _m: None, choose=lambda _m: None, states=DownloadStates()
        )

    def shelf(self, body: ctk.CTkFrame) -> WatchlistShelf:
        images: Any = FakeImages()
        return WatchlistShelf(body, watchlist=self.watchlist, images=images, actions=self.actions)

    def pump(self) -> None:
        self.root.update()

    async def test_button_puts_the_film_on_the_list_and_takes_it_off(self) -> None:
        film = movie(1, release_date="2026-12-18")
        button = WatchlistButton(self.root, movie=film, watchlist=self.watchlist)
        button.pack(fill="x")
        other = WatchlistButton(self.root, movie=film, watchlist=self.watchlist)
        self.pump()
        self.assertEqual(button.cget("text"), ADD_TEXT)
        self.assertEqual(button.cget("fg_color"), theme.ACCENT)
        button._click()
        await until(lambda: self.watchlist.contains(1))
        self.assertEqual(button.cget("text"), ON_TEXT)
        self.assertEqual(button.cget("fg_color"), theme.GRABBED)
        self.assertEqual(other.cget("fg_color"), theme.GRABBED)  # every card follows
        other._click()
        await until(lambda: not self.watchlist.contains(1))
        self.assertEqual(button.cget("text"), ADD_TEXT)
        other.destroy()  # unsubscribes
        await self.watchlist.add(film)
        self.assertEqual(button.cget("text"), ON_TEXT)

    async def test_shelf_hides_while_empty_and_follows_the_list(self) -> None:
        body = ctk.CTkFrame(self.root)
        body.pack(fill="both", expand=True)
        shelf = self.shelf(body)
        below = ctk.CTkLabel(body, text="Trending")
        below.pack()
        self.assertFalse(shelf.winfo_manager())  # not started
        shelf.start()
        self.assertFalse(shelf.winfo_manager())  # empty

        self.discovery.details[1] = MovieDetails(id=1, digital_release=TODAY + timedelta(days=20))
        await self.watchlist.add(movie(1, "Clayface"))
        await self.watchlist.add(movie(2, "Sinners", release_date="2026-01-01"), auto_download=False)
        self.assertEqual(body.pack_slaves()[0], shelf)  # first, above the TMDB shelves
        self.assertEqual(shelf._count.cget("text"), "2 films")
        self.assertEqual(list(shelf.cards), [2, 1])  # newest first
        card = shelf.cards[1]
        assert card.status is not None
        self.assertEqual(card.status.bottom, "switch")
        self.assertTrue(card._switch.winfo_manager())
        self.assertFalse(card._control.winfo_manager())
        self.assertEqual(card.watch_button.cget("text"), "✓")  # the narrow button
        sinners = shelf.cards[2]
        assert sinners.status is not None
        self.assertEqual(sinners.status.text, "Out now")
        self.assertTrue(sinners._control.winfo_manager())

        # A change to a film repaints its card in place.
        await self.watchlist.set_auto_download(1, False)
        self.assertIs(shelf.cards[1], card)
        self.assertFalse(card._auto_var.get())

        await self.watchlist.remove(2)
        self.assertEqual(list(shelf.cards), [1])
        self.assertEqual(shelf._count.cget("text"), "1 film")
        await self.watchlist.remove(1)
        self.assertFalse(shelf.winfo_manager())
        shelf.destroy()
        await self.watchlist.add(movie(3))  # no longer listening: nothing breaks

    async def test_the_switch_changes_the_film(self) -> None:
        self.discovery.details[1] = MovieDetails(id=1, digital_release=TODAY - timedelta(days=1))
        await self.watchlist.add(movie(1))
        body = ctk.CTkFrame(self.root)
        body.pack()
        shelf = self.shelf(body)
        shelf.start()
        card = shelf.cards[1]
        assert card.status is not None
        self.assertEqual(card.status.tone, "waiting")
        card._switch.toggle()
        await until(lambda: not (self.watchlist.entry(1) or entry()).auto_download)
        assert card.status is not None
        self.assertEqual(card.status.text, "Out now")
        self.assertTrue(card._control.winfo_manager())



if __name__ == "__main__":
    unittest.main()
