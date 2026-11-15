"""The Library page's "Continue watching" row: what Plex says to go on with
(PlexService.continue_watching: its /hubs/continueWatching, else /library/onDeck), the row above
the tabs (hidden without Plex, empty or failing), a card playing where Plex left it without the
Resume prompt (Play.resume through MediaPlayerHost), the row heard again after the player
stopped, and ▶ Play on a Library card's hover. A fake Plex server behind httpx.MockTransport
(tests/fixtures/plex/continue_watching.json, on_deck.json: made-up titles), a fake player
process; nothing reaches the network."""

from __future__ import annotations

import logging
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest import mock

import tkinter as tk

import customtkinter as ctk
import httpx
from PIL import ImageTk

from app.models.plex import PlexContinue, PlexScan, PlexTitle, PlexVideo
from app.services.plex import CONTINUE_SIZE, PlexClient, PlexError, PlexService, PlexUnreachable
from app.services.plex_library import (
    LibraryContents,
    LibraryKindTab,
    LibraryShow,
    PlexLibraryService,
    continue_line,
)
from app.ui import theme
from app.ui.poster_card import rgb
from tests.plex_link import TOKEN
from tests.test_backup_dialog import DialogTestCase
from tests.test_media_player_host import ARK, DUNE, DUNE_VIDEO, E1, E2, E3, EPISODES, HostTestCase
from tests.test_plex_playing import PlayingTestCase, fixture

HUB = "/hubs/continueWatching"
ON_DECK = "/library/onDeck"


class ServiceTests(PlayingTestCase):
    async def test_continue_watching_reads_the_hub(self) -> None:
        self.server.pages[HUB] = fixture("continue_watching.json")
        found = await self.plex.continue_watching()
        # Plex's order; the clip (neither an episode nor a film) left out.
        self.assertEqual([item.video.key for item in found], ["7122", "7101", "7215"])
        harbour, orchard, saltmarsh = found
        self.assertEqual(
            (harbour.title.kind, harbour.title.key, harbour.title.title, harbour.title.thumb),
            ("show", "7001", "Harbour Lights", "/library/metadata/7001/thumb/1762000000"),
        )
        video = harbour.video
        self.assertEqual((video.season, video.episode, video.title), (2, 2, "The Tide Clock"))
        self.assertEqual((video.view_offset_ms, video.duration_ms), (1200000, 2700000))
        self.assertTrue(video.file.endswith("Harbour Lights - S02E02 - The Tide Clock.mkv"))
        self.assertEqual(continue_line(harbour), "S02E02 · The Tide Clock")
        self.assertEqual((orchard.title.kind, orchard.title.title), ("movie", "The Glass Orchard"))
        self.assertEqual((orchard.video.view_offset_ms, orchard.video.duration_ms), (4360000, 7980000))
        # The version ▶ Play picks: the tallest (the 4K one, listed second).
        self.assertTrue(orchard.video.file.endswith("The Glass Orchard (2025).mkv"))
        self.assertEqual(continue_line(orchard), "2025")
        self.assertEqual(saltmarsh.video.duration_ms, 3300000)
        self.assertEqual(continue_line(saltmarsh), "S01E05 · Low Water")
        [asked] = self.server.requests
        self.assertEqual(asked.url.path, HUB)
        self.assertEqual(asked.headers["X-Plex-Token"], TOKEN)
        self.assertNotIn(TOKEN, str(asked.url))
        timeout = asked.extensions["timeout"]
        self.assertEqual((timeout["read"], timeout["connect"]), (8.0, 3.0))  # SCREEN_TIMEOUT

    async def test_at_most_twelve(self) -> None:
        page = fixture("continue_watching.json")
        hub = page["MediaContainer"]["Hub"][0]
        [first] = [item for item in hub["Metadata"] if item["ratingKey"] == "7101"]
        hub["Metadata"] = [dict(first, ratingKey=str(8000 + n)) for n in range(20)]
        self.server.pages[HUB] = page
        self.assertEqual(len(await self.plex.continue_watching()), CONTINUE_SIZE)
        self.assertEqual(CONTINUE_SIZE, 12)

    async def test_a_known_title_comes_from_the_last_check(self) -> None:
        # The show as the check stored it (its ids: ▶ Play's episodes of every copy).
        stored = PlexTitle(kind="show", key="7001", title="Harbour Lights", tvdb_id=480001)
        film = PlexTitle(kind="movie", key="7101", title="The Glass Orchard", tmdb_id=990001)
        other = PlexTitle(kind="movie", key="7001", title="Not this one")  # same key, a film
        scan = PlexScan(titles=(stored, film, other, PlexTitle(kind="show", key="9", title="X")))
        await self.db.plex.replace(scan, datetime(2026, 11, 15, tzinfo=UTC))
        self.server.pages[HUB] = fixture("continue_watching.json")
        # Only the few keys the row names are read, never every stored title.
        with mock.patch.object(self.db.plex, "titles", side_effect=AssertionError("all read")):
            found = await self.plex.continue_watching()
        self.assertEqual([one.title for one in found[:2]], [stored, film])
        self.assertEqual(found[2].title.title, "Saltmarsh")  # not stored: from the item
        self.assertEqual(
            await self.db.plex.titles_by_key({("show", "7001"), ("movie", "404")}),
            {("show", "7001"): stored},
        )
        self.assertEqual(await self.db.plex.titles_by_key(()), {})

    async def test_on_deck_is_the_fallback(self) -> None:
        self.server.pages[ON_DECK] = fixture("on_deck.json")  # no hub: 404
        [item] = await self.plex.continue_watching()
        self.assertEqual([r.url.path for r in self.server.requests], [HUB, ON_DECK])
        self.assertEqual((item.title.title, item.video.key), ("Harbour Lights", "7123"))
        self.assertEqual((item.video.season, item.video.episode), (2, 3))
        self.assertEqual(item.video.view_offset_ms, 0)

    async def test_other_failures_are_no_fallback(self) -> None:
        self.server.status = 500
        with self.assertRaises(PlexUnreachable):
            await self.plex.continue_watching()
        self.assertNotIn(ON_DECK, [r.url.path for r in self.server.requests])
        self.server.status = 302  # never followed
        self.plex._unreachable_at = None
        with self.assertRaises(PlexError):
            await self.plex.continue_watching()

    async def test_nothing_is_asked_without_plex(self) -> None:
        await self.settings.update(plex_token="")
        self.assertEqual(await self.plex.continue_watching(), [])
        library = PlexLibraryService(self.plex, mock.Mock())
        self.assertEqual(await library.continue_watching(), [])
        self.assertEqual(self.server.requests, [])


def item(key: str, *, show: bool = True, offset: int = 600000) -> PlexContinue:
    title = (
        PlexTitle(kind="show", key="7001", title="Harbour Lights")
        if show
        else PlexTitle(kind="movie", key=key, title="The Glass Orchard", year=2025)
    )
    video = PlexVideo(
        key=key, file=f"/volume1/video/{key}.mkv", season=2 if show else None,
        episode=int(key[-1]) if show else None, title=f"Part {key}",
        view_offset_ms=offset, duration_ms=2400000,
    )
    return PlexContinue(title=title, video=video)


class FakePlay:
    """play_control.Play as the Library page uses it."""

    def __init__(self) -> None:
        self.resumed: list[str] = []
        self.films: list[str] = []
        self.picked: list[str] = []

    def resume(self, chosen: PlexContinue) -> None:
        self.resumed.append(chosen.video.key)

    def film(self, title: PlexTitle) -> None:
        self.films.append(title.key)

    def pick(self, master: Any, title: PlexTitle) -> Any:
        self.picked.append(title.key)
        return None

    def close(self) -> None:
        pass


class FakeService:
    def __init__(self, contents: LibraryContents) -> None:
        self._contents = contents
        self.available = True

    async def contents(self, *, profile_id: int) -> LibraryContents:
        return self._contents

    async def follow(self, plex_title: PlexTitle, *, profile_id: int) -> bool:
        return True


class RowTests(DialogTestCase):
    def make(self, source: Any, *, play: FakePlay | None = None, contents: LibraryContents | None = None) -> Any:
        from app.ui.views.library_view import LibraryView

        view = LibraryView(
            self.root,
            service=FakeService(contents or LibraryContents()),  # type: ignore[arg-type]
            posters=mock.Mock(get=mock.AsyncMock(return_value=None)),
            settings=self.settings(),
            status=lambda text, clear_after_ms=None: None,
            on_open_show=mock.AsyncMock(return_value=True),
            on_open_film=lambda found: None,
            continue_watching=source,
            play=play,
        )
        view.pack(fill="both", expand=True)
        self.keep(view)
        return view

    async def test_the_row_shows_plexs_order_and_plays_a_card(self) -> None:
        items = [item("7122"), item("7101", show=False, offset=0), item("7215")]
        play = FakePlay()
        view = self.make(mock.AsyncMock(return_value=items), play=play)
        await view.load()
        await self.settle()
        row = view.continue_row
        self.assertTrue(row.shown)
        self.assertEqual(row.heading.cget("text"), "Continue watching")
        self.assertEqual([card.item.video.key for card in row.cards], ["7122", "7101", "7215"])
        first = row.cards[0]
        self.assertEqual(first.title_text, "Harbour Lights")
        self.assertEqual(first.meta_text, "S02E02 · Part 7122")
        assert first.progress is not None  # a quarter watched: the picker's progress line
        self.assertAlmostEqual(first.progress.get(), 0.25, places=2)
        self.assertIsNone(row.cards[1].progress)  # not begun: no line
        first._clicked(None)
        self.assertEqual(play.resumed, ["7122"])
        # Above the tabs.
        self.assertLess(row.grid_info()["row"], view.kind_tabs_row.grid_info()["row"])

    async def test_the_row_is_hidden_without_plex(self) -> None:
        settings = self.settings()  # no token
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        self.addAsyncCleanup(http.aclose)
        plex = PlexService(PlexClient(http, client_id=lambda: "x"), mock.Mock(), settings)
        library = PlexLibraryService(plex, mock.Mock())
        view = self.make(library.continue_watching)
        await view.load()
        await self.settle()
        self.assertFalse(view.continue_row.shown)
        self.assertEqual(view.continue_row.cards, [])

    async def test_an_empty_or_failing_row_is_hidden_with_one_quiet_line(self) -> None:
        source = mock.AsyncMock(return_value=[item("7122")])
        view = self.make(source)
        await view.load()
        await self.settle()
        self.assertTrue(view.continue_row.shown)
        source.side_effect = PlexUnreachable("away")
        with self.assertLogs("app.ui.views.library_view", logging.INFO) as logs:
            view.refresh_continue()
            await self.settle()
        self.assertFalse(view.continue_row.shown)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("Harbour", logs.output[0])
        source.side_effect = None
        source.return_value = []
        view.refresh_continue()
        await self.settle()
        self.assertFalse(view.continue_row.shown)

    async def test_showing_the_page_reads_the_row_again(self) -> None:
        source = mock.AsyncMock(return_value=[item("7122")])
        view = self.make(source)
        view.refresh_if_stale()  # the first visit: the page loads (and its row with it)
        await self.settle()
        self.assertEqual(source.await_count, 1)
        view.refresh_if_stale()  # shown again, nothing stale: only the row
        await self.settle()
        self.assertEqual(source.await_count, 2)

    async def test_without_a_source_there_is_no_row(self) -> None:
        view = self.make(None)
        await view.load()
        await self.settle()
        self.assertFalse(view.continue_row.shown)

    async def test_library_cards_play_on_hover(self) -> None:
        play = FakePlay()
        shows = (LibraryShow(PlexTitle(kind="show", key="7001", title="Harbour Lights"), following=False),)
        films = (PlexTitle(kind="movie", key="7101", title="The Glass Orchard"),)
        view = self.make(None, play=play, contents=LibraryContents(shows=shows, films=films))
        await view.load()
        await self.settle()
        [card] = view.cards
        assert card.play_items is not None
        sheet = card._sheet
        pill, label = card.play_items
        # Drawn on the cover's canvas: no widget of its own.
        self.assertEqual((sheet.type(pill), sheet.type(label)), ("image", "text"))
        self.assertEqual(sheet.itemcget(label, "text"), "▶ Play")
        self.assertFalse(card.play_shown)  # only on hover
        self.assertEqual(sheet.itemcget(pill, "state"), "hidden")
        card._play_pressed()  # hidden: a click there is none
        self.assertEqual(play.picked, [])
        card.hover(True)
        self.assertTrue(card.play_shown)
        self.assertEqual(sheet.itemcget(pill, "state"), "normal")
        self.assertEqual(sheet.find_withtag("play")[-1], sheet.find_all()[-1])  # on top
        # The pill: accent-filled, centred across the card, the accent's text colour.
        x, y = sheet.coords(pill)
        self.assertAlmostEqual(x, round(card._units[0] * card.scaling) / 2, delta=1)
        self.assertEqual(sheet.itemcget(label, "fill"), theme.pick(theme.ON_ACCENT))
        photo = card._play_photo
        assert photo is not None
        middle = ImageTk.getimage(photo).getpixel((photo.width() // 2, 2))
        assert isinstance(middle, tuple)
        self.assertEqual(middle[:3], rgb(card, theme.ACCENT))
        card._play_pressed()  # its tag binding
        self.assertEqual(play.picked, ["7001"])  # a show: the episode picker
        # A click on the pill doesn't also open the show.
        opened: list[str] = []
        card._set_open(lambda: opened.append("open"))
        with mock.patch.object(card, "_over_play", return_value=True):
            card._clicked(None)
        self.assertEqual(opened, [])
        card._clicked(None)  # elsewhere on the cover: opens
        self.assertEqual(opened, ["open"])
        # The hand and the hover colour on the pill.
        card._set_play_hot(True)
        self.assertEqual(str(sheet.cget("cursor")), "hand2")
        card._set_play_hot(False)
        card.hover(False)
        self.assertFalse(card.play_shown)
        # Light ↔ dark: the pill is drawn again in the other colours.
        was = ctk.get_appearance_mode()
        ctk.set_appearance_mode("light" if was == "Dark" else "dark")
        try:
            await self.settle()
            card.hover(True)
            self.assertEqual(sheet.itemcget(label, "fill"), theme.pick(theme.ON_ACCENT))
            photo = card._play_photo
            assert photo is not None
            middle = ImageTk.getimage(photo).getpixel((photo.width() // 2, 2))
            assert isinstance(middle, tuple)
            self.assertEqual(middle[:3], rgb(card, theme.ACCENT))
        finally:
            ctk.set_appearance_mode(was.lower())
        view.set_kind(LibraryKindTab.FILMS)
        await self.settle()
        [film] = view.cards
        film.hover(True)
        film._play_pressed()
        self.assertEqual(play.films, ["7101"])  # a film: at once

    async def test_leaving_the_card_hides_play(self) -> None:
        films = (PlexTitle(kind="movie", key="7101", title="The Glass Orchard"),)
        view = self.make(None, play=FakePlay(), contents=LibraryContents(films=films))
        view.set_kind(LibraryKindTab.FILMS)
        await view.load()
        await self.settle()
        [film] = view.cards
        # Bound on the card's own Tk frame (not only CTk's canvas): leaving through the action
        # row, or the page scrolled under the pointer, reaches it.
        self.assertIn("lambda", tk.Misc.bind(film, "<Leave>"))
        film.hover(True)
        with mock.patch.object(film, "winfo_pointerxy", return_value=(-500, -500)):
            film._left()
        self.assertFalse(film.play_shown)
        film.hover(True)
        inside = (film.winfo_rootx(), film.winfo_rooty())
        with mock.patch.object(film, "winfo_pointerxy", return_value=inside):
            film._left()  # still on the card (on its row, say): stays
        self.assertTrue(film.play_shown)

    async def test_no_play_without_a_player(self) -> None:
        films = (PlexTitle(kind="movie", key="7101", title="The Glass Orchard"),)
        view = self.make(None, contents=LibraryContents(films=films))
        view.set_kind(LibraryKindTab.FILMS)
        await view.load()
        await self.settle()
        [film] = view.cards
        self.assertIsNone(film.play_items)


class StoppedGateTests(unittest.TestCase):
    def test_only_the_library_page_on_screen_reads_the_row_again(self) -> None:
        # MainWindow._on_player_stopped (the player's `on_stopped`): only while the Library page
        # shows; any other page leaves it to the next showing (`refresh_if_stale`).
        from app.ui.main_window import MainWindow, Mode

        for mode in Mode:
            window = mock.Mock(_mode=mode)
            MainWindow._on_player_stopped(window)
            calls = window._library_view.refresh_continue.call_count
            self.assertEqual(calls, 1 if mode is Mode.LIBRARY else 0, mode)
        window = mock.Mock(_mode=None)  # before the first page
        MainWindow._on_player_stopped(window)
        window._library_view.refresh_continue.assert_not_called()


class PlayTests(HostTestCase):
    """Play.resume through the real host on a fake player process."""

    def make_play(self) -> Any:
        from app.ui.play_control import Play

        lines: list[str] = []
        play = Play(self.playback, self.host, lambda text, clear_after_ms=None: lines.append(text))
        return play, lines

    async def test_a_card_plays_from_its_position(self) -> None:
        play, lines = self.make_play()
        play.resume(PlexContinue(title=DUNE, video=DUNE_VIDEO))
        await play.settled()
        await self.settle()
        process = self.starter.processes[-1]
        [sent] = process.plays()
        # Where Plex left it, without Resume / Start over: the card said where.
        self.assertEqual((sent.start_ms, sent.ask_resume), (4360000, False))
        self.assertEqual(lines, ["▶ Finding Dune: Part Two…"])

    async def test_an_episode_plays_from_its_position_with_the_next_one(self) -> None:
        play, _lines = self.make_play()
        e2 = PlexVideo(**{**E2.model_dump(), "view_offset_ms": 900000})
        self.server.pages["/library/metadata/1001/allLeaves"] = {
            "MediaContainer": {
                "Metadata": [
                    {
                        "ratingKey": one.key, "parentIndex": one.season, "index": one.episode,
                        "title": one.title, "duration": one.duration_ms,
                        "Media": [{"Part": [{"file": one.file}]}],
                    }
                    for one in (E1, E2, E3)
                ]
            }
        }
        play.resume(PlexContinue(title=ARK, video=e2))
        await play.settled()
        await self.settle()
        [sent] = self.starter.processes[-1].plays()
        self.assertEqual((sent.start_ms, sent.ask_resume), (900000, False))
        self.assertEqual((sent.line, sent.next_label), ("S02E02 · Part 2", "S02E03"))

    async def test_the_show_not_answering_still_plays_it(self) -> None:
        play, _lines = self.make_play()
        self.server.status = 500  # the show's episodes don't read: no next episode then
        self.server.pages.clear()

        async def episodes(title: PlexTitle) -> list[PlexVideo]:
            raise PlexUnreachable("away")

        with mock.patch.object(self.playback, "episodes", episodes):
            play.resume(PlexContinue(title=ARK, video=E3))
            await play.settled()
            await self.settle()
        [sent] = self.starter.processes[-1].plays()
        self.assertEqual((sent.start_ms, sent.ask_resume, sent.next_label), (30000, False, None))

    async def test_a_plain_play_still_asks(self) -> None:
        process = await self.play(ARK, E3, EPISODES)
        [sent] = process.plays()
        self.assertTrue(sent.ask_resume)

    async def test_stopped_is_heard_after_plex_took_it(self) -> None:
        heard: list[int] = []
        self.host.on_stopped = lambda: heard.append(len(self.timeline()))
        process = await self.play(DUNE, DUNE_VIDEO)
        from app.media_protocol import Position, Stopped

        await self.tell(process, Position(ms=4400000, duration_ms=9000000, state="playing"))
        self.assertEqual(heard, [])
        await self.tell(process, Stopped(ms=4500000, duration_ms=9000000))
        self.assertEqual(heard, [len(self.timeline())])  # after the "stopped" report
        self.assertEqual(self.timeline()[-1][1], "stopped")


if __name__ == "__main__":
    unittest.main()
