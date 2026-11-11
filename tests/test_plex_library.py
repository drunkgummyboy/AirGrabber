"""The Library page: Plex's titles kept per check, their posters, the page's data and its cards
(fake Plex only: tests/test_plex.py's FakePlex, never plex.tv or a real server)."""

from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from PIL import Image

from app.models import ImportResult, Show
from app.models.plex import PlexItemReply, PlexTitle
from app.services.images import ImageCache
from app.services.plex import plex_title, poster_url, resolution_name
from app.services.plex_library import (
    LibraryContents,
    LibraryFilter,
    LibraryShow,
    LibrarySort,
    PlexLibraryService,
    checked_text,
    follow_line,
    gaps_text,
    page_slice,
    show_line,
    shown_films,
    shown_shows,
)
from app.ui import theme
from tests.test_backup_dialog import DialogTestCase
from tests.test_plex import NOW, TOKEN, PlexTestCase, fixture

THUMB = "/library/metadata/1001/thumb/1699999999"


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 60), (200, 40, 40)).save(out, format="PNG")
    return out.getvalue()


def title(name: str, kind: str = "show", **values: Any) -> PlexTitle:
    return PlexTitle(kind=kind, key=values.pop("key", name), title=name, **values)


class TitleTests(unittest.TestCase):
    def test_an_item_as_a_title(self) -> None:
        item = PlexItemReply.model_validate(
            {
                "ratingKey": 1001, "title": "The Ark", "year": "2023", "thumb": THUMB,
                "addedAt": 1762500000, "childCount": 2, "leafCount": 23,
                "Guid": [{"id": "tvdb://412345"}, {"id": "imdb://tt13194362"}],
            }
        )
        found = plex_title(item, "show", 101)
        assert found is not None
        self.assertEqual((found.key, found.year, found.seasons, found.episodes), ("1001", 2023, 2, 23))
        self.assertEqual((found.show_id, found.tvdb_id, found.imdb_id), (101, 412345, "tt13194362"))
        self.assertEqual(found.added_at, datetime.fromtimestamp(1762500000, UTC))
        self.assertEqual(poster_url(found), f"plex:{THUMB}")

    def test_odd_pictures_and_resolutions(self) -> None:
        item = PlexItemReply.model_validate(
            {"ratingKey": "9", "title": "Film", "thumb": "http://elsewhere/x.jpg",
             "Media": [{"videoResolution": "4k"}]}
        )
        film = plex_title(item, "movie")
        assert film is not None
        self.assertIsNone(film.thumb)  # only the server's own picture paths
        self.assertEqual(film.resolution, "4K")
        self.assertEqual(
            [resolution_name(v) for v in ("1080", "720", "sd", "", None, "8k")],
            ["1080p", "720p", "SD", None, None, None],
        )
        self.assertIsNone(plex_title(PlexItemReply.model_validate({"title": "x"}), "movie"))


class UpgradeTests(unittest.TestCase):
    def test_a_film_below_1080p_can_be_upgraded(self) -> None:
        from app.services.plex_library import can_upgrade

        def film(resolution: str | None, tmdb_id: int | None = 7) -> PlexTitle:
            return title("Heat", "movie", resolution=resolution, tmdb_id=tmdb_id)

        self.assertEqual(
            [can_upgrade(film(r)) for r in ("SD", "720p", "1080p", "4K", None)],
            [True, True, False, False, False],  # an unknown quality never counts
        )
        self.assertFalse(can_upgrade(film("720p", tmdb_id=None)))  # no releases to look up
        self.assertFalse(can_upgrade(title("Lost", resolution="SD", tmdb_id=7)))  # a show


class PureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ark = LibraryShow(title("The Ark", year=2023, added_at=NOW - timedelta(days=1)), following=True)
        self.lost = LibraryShow(title("Lost", year=2004, added_at=NOW - timedelta(days=9)), gaps=2)
        self.ended = LibraryShow(title("Silent Tide", year=2015), ended=True)

    def test_filters_sort_and_search(self) -> None:
        shows = [self.lost, self.ended, self.ark]

        def names(found: list[LibraryShow]) -> list[str]:
            return [s.title.title for s in found]

        self.assertEqual(names(shown_shows(shows, LibraryFilter.ALL, LibrarySort.ADDED)), ["The Ark", "Lost", "Silent Tide"])
        self.assertEqual(names(shown_shows(shows, LibraryFilter.ALL, LibrarySort.NAME)), ["Lost", "Silent Tide", "The Ark"])
        self.assertEqual(names(shown_shows(shows, LibraryFilter.ALL, LibrarySort.YEAR)), ["The Ark", "Silent Tide", "Lost"])
        self.assertEqual(names(shown_shows(shows, LibraryFilter.NOT_FOLLOWED, LibrarySort.NAME)), ["Lost", "Silent Tide"])
        self.assertEqual(names(shown_shows(shows, LibraryFilter.GAPS, LibrarySort.NAME)), ["Lost"])
        self.assertEqual(names(shown_shows(shows, LibraryFilter.ENDED, LibrarySort.NAME)), ["Silent Tide"])
        self.assertEqual(names(shown_shows(shows, LibraryFilter.ALL, LibrarySort.NAME, "  ark ")), ["The Ark"])
        self.assertEqual(names(shown_shows(shows, LibraryFilter.ALL, LibrarySort.NAME, "2004")), ["Lost"])

    def test_films(self) -> None:
        films = [
            title("Dune", "movie", resolution="4K", added_at=NOW - timedelta(days=40)),
            title("Heat", "movie", resolution="1080p", added_at=NOW - timedelta(days=2)),
        ]
        uhd = shown_films(films, LibraryFilter.UHD, LibrarySort.NAME, now=NOW)
        self.assertEqual([f.title for f in uhd], ["Dune"])
        recent = shown_films(films, LibraryFilter.RECENT, LibrarySort.NAME, now=NOW)
        self.assertEqual([f.title for f in recent], ["Heat"])

    def test_texts(self) -> None:
        self.assertEqual(show_line(title("x", seasons=3, episodes=34)), "3 seasons · 34 episodes")
        self.assertEqual(show_line(title("x", seasons=1, episodes=1)), "1 season · 1 episode")
        self.assertEqual(gaps_text(2), "2 gaps")
        self.assertEqual(gaps_text(0), "")
        self.assertEqual(follow_line(title("Lost", show_id=123)), "123")
        self.assertEqual(follow_line(title("Lost", tvdb_id=73739)), "tvdb:73739 Lost")
        self.assertEqual(follow_line(title("Lost", imdb_id="tt0411008")), "imdb:tt0411008 Lost")
        self.assertEqual(follow_line(title("Lost")), "Lost")
        self.assertEqual(page_slice(list(range(5)), 3), ([0, 1, 2], 2))


class StoredTests(PlexTestCase):
    def serve_extras(self) -> None:
        shows = [dict(item) for item in fixture("shows.json")["MediaContainer"]["Metadata"]]
        shows[0].update(thumb=THUMB, addedAt=1762500000, childCount=2, leafCount=23)
        films = [dict(item) for item in fixture("movies.json")["MediaContainer"]["Metadata"]]
        films[0]["Media"] = [{"videoResolution": "4k"}]
        self.fake.items[("1", 2)] = shows
        self.fake.items[("2", 1)] = films

    async def test_a_check_keeps_the_titles_and_disconnect_forgets_them(self) -> None:
        self.serve_extras()
        await self.plex.check()
        titles = await self.plex.titles()
        shows = [t for t in titles if t.kind == "show"]
        films = [t for t in titles if t.kind == "movie"]
        self.assertEqual(len(shows), len(fixture("shows.json")["MediaContainer"]["Metadata"]))
        self.assertEqual(len(films), len(fixture("movies.json")["MediaContainer"]["Metadata"]))
        ark = next(t for t in shows if t.title == "The Ark")
        self.assertEqual((ark.show_id, ark.thumb, ark.episodes), (101, THUMB, 23))
        self.assertEqual(next(f for f in films if f.title == "Dune: Part Two").resolution, "4K")
        self.assertEqual(await self.plex.checked_at(), NOW)
        await self.plex.disconnect()
        self.assertEqual(await self.db.plex.titles(), [])
        self.assertEqual(await self.plex.titles(), [])

    async def test_a_poster_comes_from_the_server_with_the_token_in_its_header(self) -> None:
        await self.plex.check()
        picture = png()
        answered: list[httpx.Request] = []
        server = self.fake._server

        def with_photos(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/photo/:/transcode":
                answered.append(request)
                if request.headers.get("X-Plex-Token") != TOKEN:
                    return httpx.Response(401)
                return httpx.Response(200, content=picture)
            return server(request)

        self.fake._server = with_photos  # type: ignore[method-assign]
        self.assertEqual(await self.plex.poster(f"plex:{THUMB}"), picture)
        [request] = answered
        self.assertEqual(request.url.params["url"], THUMB)
        self.assertNotIn(TOKEN, str(request.url))
        # Not a title's picture path, not ours, or no Plex: nothing is asked.
        self.assertIsNone(await self.plex.poster("plex:/etc/passwd"))
        self.assertIsNone(await self.plex.poster(f"https://elsewhere{THUMB}"))
        self.assertEqual(len(answered), 1)
        # Through an ImageCache, as the page loads it.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            cache = ImageCache(self.http, Path(folder), fetch=self.plex.poster)
            image = await cache.get(f"plex:{THUMB}", (20, 30))
            assert image is not None
            self.assertEqual(image.size, (20, 30))

    async def test_the_page_contents(self) -> None:
        await self.plex.check()
        library = mock.Mock()
        library.tracked_ids = mock.AsyncMock(return_value=frozenset({101}))
        library.gaps = mock.AsyncMock(return_value=[])
        library.cached_shows = mock.AsyncMock(
            return_value={104: Show(id=104, name="Frieren", status="Ended")}
        )
        service = PlexLibraryService(self.plex, library)
        contents = await service.contents(profile_id=1)
        by_name = {s.title.title: s for s in contents.shows}
        self.assertTrue(by_name["The Ark"].following)
        self.assertFalse(by_name["Frieren"].following)
        self.assertTrue(by_name["Frieren"].ended)
        self.assertEqual(contents.checked_at, NOW)
        # Following one AirGrabber didn't know checks Plex again.
        library.import_shows = mock.AsyncMock(return_value=ImportResult(added=("New",)))
        unknown = next(s.title for s in contents.shows if s.title.show_id is None)
        with mock.patch.object(self.plex, "check", mock.AsyncMock()) as check:
            self.assertTrue(await service.follow(unknown, profile_id=1))
        check.assert_awaited_once()
        library.import_shows.assert_awaited_once_with([follow_line(unknown)], profile_id=1)


class NoPlexTests(PlexTestCase):
    connected = False

    async def test_nothing_without_plex(self) -> None:
        service = PlexLibraryService(self.plex, mock.Mock())
        self.assertFalse(service.available)
        self.assertEqual(await service.contents(profile_id=1), LibraryContents())
        self.assertIsNone(await self.plex.poster(f"plex:{THUMB}"))


class FakeService:
    def __init__(self, contents: LibraryContents) -> None:
        self._contents = contents
        self.followed: list[str] = []
        self.available = True

    async def contents(self, *, profile_id: int) -> LibraryContents:
        return self._contents

    async def follow(self, plex_title: PlexTitle, *, profile_id: int) -> bool:
        self.followed.append(plex_title.title)
        return True


class ViewTests(DialogTestCase):
    async def test_cards_filters_more_and_follow(self) -> None:
        from app.ui.views.library_view import PAGE_SIZE, LibraryView

        shows = tuple(
            LibraryShow(title(f"Show {n:03d}", key=str(n), seasons=1, episodes=n + 1), following=n == 0)
            for n in range(PAGE_SIZE + 5)
        )
        films = (title("Dune", "movie", year=2021, resolution="4K"),)
        service = FakeService(LibraryContents(shows=shows, films=films, checked_at=NOW))
        opened: list[str] = []
        checked: list[str] = []

        async def open_show(found: PlexTitle) -> bool:
            opened.append(found.title)
            return True

        view = LibraryView(
            self.root,
            service=service,  # type: ignore[arg-type]
            posters=mock.Mock(get=mock.AsyncMock(return_value=None)),
            settings=self.settings(),
            status=lambda text, clear_after_ms=None: None,
            on_open_show=open_show,
            on_open_film=lambda found: opened.append(found.title),
            on_checked=checked.append,
        )
        view.pack(fill="both", expand=True)
        self.keep(view)
        await view.load()
        await self.settle()
        self.assertEqual(checked, [checked_text(NOW)])  # for the main window's footer
        self.assertEqual(len(view.cards), PAGE_SIZE)
        assert view.more_button is not None
        self.assertEqual(view.more_button.cget("text"), "Show more (5)")
        view.show_more()
        await self.settle()
        self.assertEqual(len(view.cards), PAGE_SIZE + 5)
        tabs = [(tab.name.cget("text"), tab.count.cget("text").strip(), tab.selected) for tab in view.kind_tabs.values()]
        self.assertEqual(tabs, [("Shows", str(PAGE_SIZE + 5), True), ("Films", "1", False)])
        self.assertEqual(
            [chip.cget("text") for chip in view.filter_chips.values()],
            ["All", "Not followed", "With gaps", "Ended"],
        )
        card = next(c for c in view.cards if c.title.title == "Show 001")
        assert card.follow_button is not None
        self.assertEqual(card.follow_button.cget("text"), "+ Follow")
        card.follow_button.invoke()
        await self.settle()
        self.assertEqual(service.followed, ["Show 001"])
        self.assertEqual(card.follow_button.cget("text"), "✓ Following")
        view.search.insert(0, "show 064")
        view._searched()
        await self.settle()
        self.assertEqual([c.title.title for c in view.cards], ["Show 064"])
        self.root.deiconify()  # a generated click reaches only a shown window
        self.root.attributes("-alpha", 0.0)
        await self.settle()
        view.kind_tabs[view_kind_films()].name._label.event_generate("<Button-1>")  # its tab
        await self.settle()
        self.assertEqual([tab.selected for tab in view.kind_tabs.values()], [False, True])
        self.assertEqual(
            [chip.cget("text") for chip in view.filter_chips.values()],
            ["All", "4K", "Recently added"],
        )
        self.assertEqual([c.title.title for c in view.cards], [])  # the search still holds
        view.search.delete(0, "end")
        view._searched()
        await self.settle()
        [film] = view.cards
        self.assertIsNone(film.follow_button)
        view.filter_chips[LibraryFilter.UHD].invoke()  # the 4K chip
        await self.settle()
        self.assertEqual([c.title.title for c in view.cards], ["Dune"])
        self.assertEqual(view.filter_chips[LibraryFilter.UHD].cget("fg_color"), theme.ACCENT)
        view.sort_menu.set("A–Z")
        view._choose_sort("A–Z")  # as the menu's pick
        await self.settle()
        self.assertEqual([c.title.title for c in view.cards], ["Dune"])
        [film] = view.cards
        film._clicked(None)
        await asyncio.sleep(0)
        self.assertEqual(opened, ["Dune"])

    async def test_a_film_below_1080p_offers_upgrade(self) -> None:
        from app.ui.views.library_view import LibraryView

        films = (
            title("Heat", "movie", key="h", year=1995, resolution="720p", tmdb_id=949),
            title("Dune", "movie", key="d", year=2021, resolution="4K", tmdb_id=438631),
        )
        service = FakeService(LibraryContents(films=films, checked_at=NOW))
        upgraded: list[str] = []
        sent: list[str] = []
        view = LibraryView(
            self.root,
            service=service,  # type: ignore[arg-type]
            posters=mock.Mock(get=mock.AsyncMock(return_value=None)),
            settings=self.settings(),
            status=lambda text, clear_after_ms=None: None,
            on_open_show=mock.AsyncMock(return_value=True),
            on_open_film=lambda found: None,
            on_upgrade_film=lambda found: upgraded.append(found.title),
            on_send=lambda found: sent.append(found.title),
            can_send=lambda: True,
        )
        view.pack(fill="both", expand=True)
        self.keep(view)
        await view.load()
        view.set_kind(view_kind_films())
        await self.settle()
        cards = {card.title.title: card for card in view.cards}
        heat, dune = cards["Heat"], cards["Dune"]
        assert heat.upgrade_button is not None
        self.assertEqual(heat.upgrade_button.cget("text"), "⬆ Upgrade")
        self.assertIsNotNone(heat.send_button)  # the round phone beside it
        self.assertIsNone(dune.upgrade_button)
        assert dune.send_button is not None
        self.assertEqual(dune.send_button.cget("text"), "Send to phone")  # no emoji box
        heat.upgrade_button.invoke()
        self.assertEqual(upgraded, ["Heat"])

    async def test_a_card_is_the_other_pages_poster_card(self) -> None:
        # The same size whatever its title or buttons, the gaps as a pill in the corner.
        from app.ui.card_scale import FULL
        from app.ui.poster_card import PosterCard, card_size
        from app.ui.views.library_view import LibraryCard

        def card(name: str, **options: Any) -> LibraryCard:
            made = LibraryCard(
                self.root, title(name, seasons=1, episodes=1),
                posters=mock.Mock(get=mock.AsyncMock(return_value=None)),
                scale=FULL, line="1 season · 1 episode", on_open=lambda: None, **options,
            )
            made.pack()
            self.keep(made)
            return made

        show = card("Reacher", following=False, tag="2 gaps", on_send=lambda: None)
        long = card("Georgie & Mandy's First Marriage and a Much Longer Title Than Fits", following=True)
        film = card("Dune")
        await self.settle()
        self.assertIsInstance(show, PosterCard)
        sizes = {(made.winfo_reqwidth(), made.winfo_reqheight()) for made in (show, long, film)}
        self.assertEqual(len(sizes), 1)
        width = card_size(FULL)[0] * show._get_widget_scaling()
        self.assertAlmostEqual(show.winfo_reqwidth(), width, delta=2)
        self.assertEqual(show.badge_text, "2 gaps")
        self.assertIsNone(film.badge_text)
        self.assertTrue(long.title_text.endswith("…"))
        self.assertEqual(show.meta_text, "1 season · 1 episode")
        assert long.follow_button is not None
        self.assertEqual(long.follow_button.cget("text"), "✓ Following")
        self.assertIsNone(film.follow_button)
        # The send button: a round one the row's height, with a drawn phone (no emoji: it
        # showed as a box), and it follows a new Windows scaling with its card.
        from app.ui.cards import RoundButton, phone_icon

        phone = show.send_button
        assert isinstance(phone, RoundButton)
        self.assertEqual((phone.text, phone._icon), ("", phone_icon))
        self.assertEqual(phone.winfo_reqwidth(), phone.winfo_reqheight())
        assert show.follow_button is not None
        self.assertEqual(phone.winfo_reqheight(), show.follow_button.winfo_reqheight())
        self.assertIn(phone.rescale, show._rescales)


def view_kind_films() -> Any:
    from app.services.plex_library import LibraryKindTab

    return LibraryKindTab.FILMS


if __name__ == "__main__":
    unittest.main()
