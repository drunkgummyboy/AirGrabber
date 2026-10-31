"""The info window with its releases (app/ui/title_info.py, movie_details.py, show_details.py):
the `info_layout` setting, both layouts ("side" / "tabs") for a film and a series, "☰ Choose…"
and "← Episodes", the Releases tab's name, the "tabs" header's alignment, the routing from
MainWindow (every former search-window entry point opens the info window on its releases), and
the panel's tasks cancelled when it goes. Hidden Tk root, fake services (tests/test_release_panel's
and tests/test_info_window's), no network."""

from __future__ import annotations

import asyncio
import hashlib
import unittest
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from app.models import (
    CalendarEntry,
    CastMember,
    DownloadEntry,
    DownloadRecord,
    Episode,
    InfoLayout,
    MediaType,
    Movie,
    Settings,
    Show,
    ShowDetails,
    TitleExtras,
    TitleInfo,
    TorrentRelease,
)
from app.services import DownloadService, LibraryService, SettingsService, TorrentSearchService
from app.services.discovery import DiscoveryService
from app.services.tmdb import TMDBError
from app.ui.download_control import DownloadActions, DownloadStates
from app.ui.main_window import MainWindow
from app.ui.movie_details import MovieDetailsWindow, MovieWindows, movie_title
from app.ui.release_panel import MovieTarget, PanelServices, ReleasePanel, TvTarget
from app.ui.show_details import ShowDetailsWindow, ShowWindows, episode_label
from app.ui.title_info import (
    CHOOSE_FIRST_TEXT,
    MIN_SUMMARY_LINES,
    NBSP,
    SIDE_WINDOW_WIDTH,
    SUMMARY_LINES,
    WINDOW_WIDTH,
    CastList,
    CastRow,
    meta_wrapping,
    more_text,
    releases_tab_text,
    target_code,
    window_size,
)
from tests.test_info_window import FakeImages, FakeWatchlist, labels
from tests.test_release_panel import (
    FakeDiscovery,
    FakeDownloads,
    FakeLibrary,
    FakeSearch,
    TkTestCase,
    no_status,
)

NOW = datetime.now(UTC)
SHOW = Show(id=31, name="Harbour Lights", genres=("Drama",), summary="A harbour master.")
OTHER = Show(id=32, name="Harbour Lights (2023)")
FILM = Movie(id=88, title="Brand New Day", release_date=date(2026, 7, 31), overview="Short.")
CAST = (
    CastMember(name="Maren Holloway", character="Ida"),
    CastMember(name="Bram", character="Pilot"),
    CastMember(name="Saskia Delacroix-Ng", character="Customs"),
    CastMember(name="Pieter van Loon", character="Harbour master"),
    CastMember(name="Ilse", character="Lighthouse keeper"),
)


def episode(season: int, number: int | None, name: str = "", *, days_ago: float = 10) -> Episode:
    airs = NOW - timedelta(days=days_ago)
    return Episode(
        id=season * 100 + (number or 0) + (50 if number is None else 0),
        show_id=SHOW.id,
        season=season,
        number=number,
        name=name,
        airstamp=airs,
        air_date=airs.date(),
    )


EPISODES = [
    episode(3, 1, "Low Tide"),
    episode(3, 4, "Customs"),
    episode(3, 5, "The Crossing"),
    episode(3, None, "Behind the Lights"),
]


def release(title: str) -> TorrentRelease:
    info_hash = hashlib.sha1(title.encode()).hexdigest()
    return TorrentRelease(title=title, source="apibay", seeders=10, info_hash=info_hash)


# -- fakes ----------------------------------------------------------------------------------------


class LayoutSettings:
    def __init__(self, layout: InfoLayout) -> None:
        self.current = Settings(info_layout=layout)


class ShowLibrary(FakeLibrary):
    """Also what the show window asks (its details) and the windows' listeners."""

    def __init__(self) -> None:
        super().__init__({SHOW.id: list(EPISODES), OTHER.id: list(EPISODES)})

    async def show_details(self, show_id: int) -> ShowDetails:
        show = SHOW if show_id == SHOW.id else OTHER
        return ShowDetails(show=show, episodes=tuple(EPISODES), tracked=True, latest_season=3)

    async def set_auto_download(self, show_id: int, on: bool) -> None:
        return None

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        return lambda: None


class WindowDiscovery(FakeDiscovery):
    """The panel's details plus the windows' extras (a cast when `cast`)."""

    def __init__(self, *, with_cast: bool = True) -> None:
        super().__init__()
        self.has_tmdb_key = True
        self._cast = CAST if with_cast else ()
        self.movies = {FILM.id: FILM}

    def _extras(self, overview: str) -> TitleExtras:
        return TitleExtras(info=TitleInfo(id=1, overview=overview), cast=self._cast)

    async def show_extras(self, show: Show) -> TitleExtras:
        return self._extras(show.summary)

    async def movie_extras(self, movie_id: int) -> TitleExtras:
        return self._extras(self.movies[movie_id].overview)

    async def movie(self, movie_id: int) -> Movie:
        if movie_id not in self.movies:
            raise TMDBError("not found")
        return self.movies[movie_id]


def no_subscribe(listener: Callable[[], None]) -> Callable[[], None]:
    return lambda: None


class WindowsTestCase(TkTestCase):
    """ShowWindows and MovieWindows on fake services, in a layout."""

    def make(
        self,
        layout: InfoLayout,
        *,
        search: FakeSearch | None = None,
        with_cast: bool = True,
        film: Movie = FILM,
    ) -> tuple[ShowWindows, MovieWindows, FakeSearch]:
        search = search or FakeSearch()
        library = ShowLibrary()
        discovery = WindowDiscovery(with_cast=with_cast)
        discovery.movies[film.id] = film
        services = PanelServices(
            search=cast_to(TorrentSearchService, search),
            downloads=cast_to(DownloadService, FakeDownloads()),
            library=cast_to(LibraryService, library),
            discovery=cast_to(DiscoveryService, discovery),
            settings=cast_to(SettingsService, LayoutSettings(layout)),
            status=no_status,
        )
        holder: dict[str, ShowWindows] = {}

        def choose(entry: CalendarEntry) -> None:  # as MainWindow._search_episode
            main = cast(Any, SimpleNamespace(_show_windows=holder["shows"]))
            MainWindow._search_episode(main, entry)

        states = DownloadStates()
        shows = ShowWindows(
            self.root,
            library=cast_to(LibraryService, library),
            discovery=cast_to(DiscoveryService, discovery),
            images=cast(Any, FakeImages()),
            status=no_status,
            episode_actions=DownloadActions[CalendarEntry](
                download=lambda entry: None, choose=choose, states=states
            ),
            downloads_changed=no_subscribe,
            releases=services,
        )
        holder["shows"] = shows
        movies = MovieWindows(
            self.root,
            images=cast(Any, FakeImages()),
            discovery=cast_to(DiscoveryService, discovery),
            watchlist=cast(Any, FakeWatchlist()),
            actions=DownloadActions[Movie](
                download=lambda m: None, choose=lambda m: None, states=states
            ),
            releases=services,
        )
        self.addCleanup(shows.close)
        self.addCleanup(movies.close)
        return shows, movies, search

    async def settle(self) -> None:
        for _ in range(3):
            await super().settle()
            await asyncio.sleep(0.01)

    @staticmethod
    def show_window(shows: ShowWindows, show_id: int = SHOW.id) -> ShowDetailsWindow:
        window = shows._live().get(show_id)
        assert window is not None
        return window

    @staticmethod
    def film_window(movies: MovieWindows, movie_id: int = FILM.id) -> MovieDetailsWindow:
        window = movies._live().get(movie_id)
        assert window is not None
        return window


def cast_to(kind: Any, value: object) -> Any:
    return cast(kind, value)


def tab_names(window: Any) -> list[str]:
    return list(window.tabs._name_list)


# -- the setting and the texts ------------------------------------------------------------------


class SettingTests(unittest.TestCase):
    def test_side_is_the_default(self) -> None:
        self.assertIs(Settings().info_layout, InfoLayout.SIDE)
        self.assertIs(Settings.model_validate({"card_size": "s"}).info_layout, InfoLayout.SIDE)

    def test_stored_values(self) -> None:
        cases = {"tabs": InfoLayout.TABS, " TABS ": InfoLayout.TABS, "side": InfoLayout.SIDE}
        for stored, expected in cases.items():
            with self.subTest(stored=stored):
                self.assertIs(Settings.model_validate({"info_layout": stored}).info_layout, expected)

    def test_an_unknown_value_is_the_default(self) -> None:
        for stored in ("sideways", "", "grid"):
            with self.subTest(stored=stored):
                loaded = Settings.model_validate({"info_layout": stored, "card_size": "xl"})
                self.assertIs(loaded.info_layout, InfoLayout.SIDE)
                self.assertEqual(loaded.card_size, "xl")  # the rest is kept


class TextTests(unittest.TestCase):
    def test_releases_tab_text(self) -> None:
        self.assertEqual(releases_tab_text(None, None), "Releases")
        self.assertEqual(releases_tab_text(MovieTarget(FILM), None), "Releases")
        self.assertEqual(releases_tab_text(MovieTarget(FILM), 18), "Releases (18)")
        self.assertEqual(releases_tab_text(TvTarget(SHOW, 3, 5), 7), "Releases · S03E05")
        self.assertEqual(releases_tab_text(TvTarget(SHOW, 3, None), None), "Releases · S03")

    def test_target_code_and_the_bar_label(self) -> None:
        self.assertEqual(target_code(TvTarget(SHOW, 3, 5)), "S03E05")
        self.assertEqual(target_code(TvTarget(SHOW, 12, None)), "S12")
        self.assertEqual(episode_label(TvTarget(SHOW, 3, 5), EPISODES), "S03E05 · The Crossing")
        self.assertEqual(episode_label(TvTarget(SHOW, 3, 6), EPISODES), "S03E06")  # unknown
        self.assertEqual(episode_label(TvTarget(SHOW, 3, None), EPISODES), "Season 3 · Pack")

    def test_meta_wraps_only_between_its_parts(self) -> None:
        wrapped = meta_wrapping("2026 · 2h 25m · Out digitally since 6 Oct")
        self.assertEqual(wrapped.replace(NBSP, " "), "2026 · 2h 25m · Out digitally since 6 Oct")
        # The only plain spaces left are right after the dots.
        last = NBSP.join(["Out", "digitally", "since", "6", "Oct"])
        self.assertEqual(wrapped.split(" "), [f"2026{NBSP}·", f"2h{NBSP}25m{NBSP}·", last])

    def test_more_text(self) -> None:
        self.assertEqual(more_text(5), "+5 more")

    def test_window_sizes(self) -> None:
        # 1920 × 1080 at 150 %: both layouts fit the work area (about 1280 × 690).
        self.assertEqual(window_size(1280, 690, SIDE_WINDOW_WIDTH), (1200, 586))
        self.assertEqual(window_size(1280, 690, WINDOW_WIDTH), (1200, 586))
        # A big screen: "side" up to SIDE_WINDOW_WIDTH, "tabs" as before.
        self.assertEqual(window_size(1706, 1000, SIDE_WINDOW_WIDTH)[0], SIDE_WINDOW_WIDTH)
        self.assertEqual(window_size(1706, 1000, WINDOW_WIDTH)[0], WINDOW_WIDTH)


# -- films --------------------------------------------------------------------------------------


FOUND = (release("Brand.New.Day.2026.1080p.WEB-DL"), release("Brand.New.Day.2026.1080p.x265"))


class FilmLayoutTests(WindowsTestCase):
    async def test_side_the_panel_beside_the_info_starts_when_the_window_opens(self) -> None:
        _shows, movies, search = self.make(InfoLayout.SIDE)
        movies.open_movie(FILM)
        await self.settle()
        window = self.film_window(movies)
        self.assertIs(window.layout, InfoLayout.SIDE)
        self.assertEqual(search.requests, [("movie",)])  # searched at once
        panel = window.panel
        assert panel is not None
        self.assertEqual(panel.target, MovieTarget(FILM))
        self.assertIs(panel.master.master, window.right)  # on the right side
        self.assertIsNone(window.tabs)
        self.assertIsInstance(window.cast, CastList)
        assert isinstance(window.cast, CastList)
        self.assertEqual(len(window.cast.entries), 3)  # three, then "+2 more"
        self.assertIn(more_text(2), labels(window))
        window.cast.unfold()
        self.assertEqual([entry.member.name for entry in window.cast.entries], [m.name for m in CAST])
        self.assertFalse(window.cast.folded)

    async def test_tabs_releases_with_the_count_then_cast(self) -> None:
        _shows, movies, search = self.make(InfoLayout.TABS, search=FakeSearch(FOUND))
        movies.open_movie(FILM)
        await self.settle()
        window = self.film_window(movies)
        self.assertIs(window.layout, InfoLayout.TABS)
        self.assertEqual(search.requests, [("movie",)])
        self.assertEqual(tab_names(window), ["Releases (2)", "Cast"])
        assert window.tabs is not None
        self.assertEqual(window.tabs.get(), "Releases (2)")  # opens on the releases
        self.assertEqual(window.releases_tab, "Releases (2)")
        self.assertIsInstance(window.cast, CastRow)
        assert window.cast is not None
        self.assertEqual(len(window.cast.entries), len(CAST))

    async def test_tabs_without_a_cast_has_no_cast_tab(self) -> None:
        _shows, movies, _search = self.make(InfoLayout.TABS, with_cast=False)
        movies.open_movie(FILM)
        await self.settle()
        window = self.film_window(movies)
        self.assertEqual(tab_names(window), ["Releases (0)"])
        self.assertIsNone(window.cast)

    async def test_choose_raises_the_open_window_on_its_releases(self) -> None:
        _shows, movies, search = self.make(InfoLayout.TABS)
        movies.open_movie(FILM)
        await self.settle()
        window = self.film_window(movies)
        assert window.tabs is not None
        window.tabs.set("Cast")
        movies.open_movie(FILM, releases=True)  # a film's ☰ Choose…
        await self.settle()
        self.assertEqual(len(movies._live()), 1)
        self.assertEqual(window.tabs.get(), window.releases_tab)
        self.assertEqual(search.requests, [("movie",)])  # not searched again

    async def test_a_film_known_only_by_its_id_opens_on_its_releases(self) -> None:
        _shows, movies, search = self.make(InfoLayout.TABS)
        self.assertTrue(await movies.open_movie_id(FILM.id, releases=True))
        await self.settle()
        window = self.film_window(movies)
        assert window.tabs is not None
        self.assertEqual(window.tabs.get(), window.releases_tab)
        self.assertTrue(await movies.open_movie_id(FILM.id, releases=True))  # open: raised
        self.assertEqual(len(movies._live()), 1)
        with self.assertLogs("app.ui.movie_details", "INFO"):
            self.assertFalse(await movies.open_movie_id(999, releases=True))

    async def test_closing_the_window_cancels_the_panels_search(self) -> None:
        search = FakeSearch(block=True)
        _shows, movies, _search = self.make(InfoLayout.SIDE, search=search)
        movies.open_movie(FILM)
        await self.settle()
        window = self.film_window(movies)
        panel = window.panel
        assert panel is not None
        task = cast(Any, panel)._search_task
        self.assertFalse(task.done())
        window.destroy()
        await self.settle()
        self.assertTrue(task.cancelled())
        self.assertTrue(search.cancelled)


class TabsHeaderTests(WindowsTestCase):
    """"tabs": the poster's top level with the title's, its bottom with the actions'."""

    async def header(
        self, overview: str, *, title: str = FILM.title, width: int = 1100
    ) -> MovieDetailsWindow:
        film = FILM.model_copy(update={"overview": overview, "title": title})
        _shows, movies, _search = self.make(InfoLayout.TABS, film=film)
        movies.open_movie(film)
        await self.settle()
        window = self.film_window(movies)
        window.geometry(f"{width}x600")
        await self.settle()
        window._fit_block()
        return window

    async def test_a_short_synopsis_the_actions_sit_at_the_posters_bottom(self) -> None:
        window = await self.header("A short one.")
        poster = window.masthead.poster_size[1]
        info = window._info
        layout = window.masthead._layout
        assert layout is not None
        self.assertEqual(layout.poster[1], layout.text[1])  # the tops level
        self.assertEqual(info.winfo_reqheight(), poster)  # the bottoms level
        buttons = window.buttons
        self.assertEqual(buttons.winfo_y() + buttons.winfo_height(), info.winfo_height())

    async def test_a_long_synopsis_gets_the_lines_that_fit(self) -> None:
        long_text = " ".join(["The harbour lights went out one night and nobody knew why."] * 20)
        # A title on two lines leaves the synopsis less than its usual six lines.
        title = "The Night the Harbour Lights Went Out"
        window = await self.header(long_text, title=title, width=640)
        poster = window.masthead.poster_size[1]
        self.assertLess(window._summary_shown, SUMMARY_LINES)
        self.assertGreaterEqual(window._summary_shown, MIN_SUMMARY_LINES)
        self.assertTrue(window._more.winfo_manager())  # cut: More shows the rest
        if window._summary_shown > MIN_SUMMARY_LINES:
            self.assertEqual(window._info.winfo_reqheight(), poster)
        window._expand_summary()  # More: all of it, below the poster if need be
        window._fit_block()
        self.assertFalse(window._more.winfo_manager())
        self.assertGreater(window._info.winfo_reqheight(), poster)


# -- series -------------------------------------------------------------------------------------


class SeriesSideTests(WindowsTestCase):
    async def test_choose_swaps_the_right_side_and_back(self) -> None:
        search = FakeSearch(block=True)
        shows, _movies, _search = self.make(InfoLayout.SIDE, search=search)
        shows.open_show(SHOW)
        await self.settle()
        window = self.show_window(shows)
        assert window._episodes_view is not None and window._releases_view is not None
        self.assertTrue(window._episodes_view.shown)
        self.assertIsNone(window.panel)
        self.assertFalse(window.showing_releases)

        window.choose(TvTarget(SHOW, 3, 5))
        await self.settle()
        panel = window.panel
        assert panel is not None and window._target_label is not None
        self.assertEqual(search.requests, [("episode", 3, 5)])  # started
        self.assertTrue(window.showing_releases)
        self.assertFalse(window._episodes_view.shown)
        self.assertEqual(window._target_label.cget("text"), "S03E05 · The Crossing")
        # The panel's own chips pick the season's pack: the label follows.
        panel.set_target(TvTarget(SHOW, 3, None))
        await self.settle()
        self.assertEqual(window._target_label.cget("text"), "Season 3 · Pack")
        self.assertEqual(search.requests[-1], ("season", 3))

        task = cast(Any, panel)._search_task
        window.back_button.invoke()  # "← Episodes"
        await self.settle()
        self.assertTrue(task.cancelled())  # its search went with it
        self.assertFalse(panel.winfo_exists())
        self.assertIsNone(window.panel)
        self.assertTrue(window._episodes_view.shown)
        self.assertFalse(window._releases_view.winfo_manager())

        window.choose(TvTarget(SHOW, 3, 4))  # another Choose…: a new panel
        await self.settle()
        assert window.panel is not None
        self.assertEqual(window.panel.target, TvTarget(SHOW, 3, 4))
        self.assertEqual(window._target_label.cget("text"), "S03E04 · Customs")


class SeriesTabsTests(WindowsTestCase):
    async def test_episodes_first_then_choose_switches_to_the_releases_tab(self) -> None:
        shows, _movies, search = self.make(InfoLayout.TABS)
        shows.open_show(SHOW)
        await self.settle()
        window = self.show_window(shows)
        assert window.tabs is not None
        self.assertEqual(tab_names(window), ["Episodes", "Releases", "Cast"])
        self.assertEqual(window.tabs.get(), "Episodes")
        self.assertIn(CHOOSE_FIRST_TEXT, labels(window.tabs.tab("Releases")))
        self.assertEqual(search.requests, [])  # nothing searched before Choose…

        window.choose(TvTarget(SHOW, 3, 5))
        await self.settle()
        self.assertEqual(window.tabs.get(), "Releases · S03E05")
        self.assertEqual(tab_names(window), ["Episodes", "Releases · S03E05", "Cast"])
        self.assertNotIn(CHOOSE_FIRST_TEXT, labels(window))
        self.assertEqual(search.requests, [("episode", 3, 5)])
        panel = window.panel
        assert panel is not None

        window.show_episodes()
        self.assertEqual(window.tabs.get(), "Episodes")
        self.assertIs(window.panel, panel)  # kept on its tab
        window.choose(TvTarget(SHOW, 3, 4))  # retargeted, not rebuilt
        await self.settle()
        self.assertIs(window.panel, panel)
        self.assertEqual(window.tabs.get(), "Releases · S03E04")
        self.assertEqual(search.requests[-1], ("episode", 3, 4))

    async def test_closing_the_window_cancels_the_panels_search(self) -> None:
        search = FakeSearch(block=True)
        shows, _movies, _search = self.make(InfoLayout.TABS, search=search)
        shows.open_show(SHOW, target=TvTarget(SHOW, 3, 5))
        await self.settle()
        window = self.show_window(shows)
        assert window.panel is not None
        task = cast(Any, window.panel)._search_task
        self.assertFalse(task.done())
        window.destroy()
        await self.settle()
        self.assertTrue(task.cancelled())


# -- routing from the main window ---------------------------------------------------------------


class RoutingTests(WindowsTestCase):
    """MainWindow's former search-window entry points, on a stand-in main window."""

    def main(self, shows: ShowWindows, movies: MovieWindows) -> Any:
        reports: list[str] = []
        stand_in = SimpleNamespace(
            _show_windows=shows,
            _movie_windows=movies,
            _report=lambda text, ms: reports.append(text),
            reports=reports,
        )
        stand_in._film_again = MainWindow._film_again.__get__(stand_in)
        stand_in._search_episode = MainWindow._search_episode.__get__(stand_in)
        return stand_in

    async def test_an_episodes_choose_opens_its_show_on_its_releases(self) -> None:
        for layout in InfoLayout:
            with self.subTest(layout=layout):
                shows, movies, search = self.make(layout)
                main = self.main(shows, movies)
                entry = CalendarEntry(show=SHOW, episode=EPISODES[2], is_tracked=True)
                MainWindow._search_episode(main, entry)
                await self.settle()
                window = self.show_window(shows)
                self.assertIs(window.layout, layout)
                assert window.panel is not None
                self.assertEqual(window.panel.target, TvTarget(SHOW, 3, 5))
                self.assertTrue(window.showing_releases)
                self.assertEqual(search.requests, [("episode", 3, 5)])
                window.destroy()

    async def test_a_special_opens_on_its_seasons_pack(self) -> None:
        shows, movies, search = self.make(InfoLayout.SIDE)
        special = CalendarEntry(show=SHOW, episode=EPISODES[3], is_tracked=True)
        MainWindow._search_episode(self.main(shows, movies), special)
        await self.settle()
        panel = self.show_window(shows).panel
        assert panel is not None
        self.assertEqual(panel.target, TvTarget(SHOW, 3, None))
        self.assertEqual(search.requests, [("season", 3)])

    async def test_an_open_window_is_raised_and_switched(self) -> None:
        shows, movies, search = self.make(InfoLayout.TABS)
        shows.open_show(SHOW)
        await self.settle()
        window = self.show_window(shows)
        main = self.main(shows, movies)
        entry = CalendarEntry(show=SHOW, episode=EPISODES[1], is_tracked=True)
        MainWindow._search_episode(main, entry)
        await self.settle()
        self.assertEqual(len(shows._live()), 1)
        self.assertIs(self.show_window(shows), window)
        assert window.tabs is not None
        self.assertEqual(window.tabs.get(), "Releases · S03E04")
        entry = CalendarEntry(show=SHOW, episode=EPISODES[2], is_tracked=True)
        MainWindow._search_episode(main, entry)
        await self.settle()
        self.assertEqual(window.tabs.get(), "Releases · S03E05")
        self.assertEqual(search.requests, [("episode", 3, 4), ("episode", 3, 5)])

    async def test_a_rows_choose_in_the_window_itself(self) -> None:
        shows, _movies, _search = self.make(InfoLayout.SIDE)
        shows.open_show(SHOW)
        await self.settle()
        window = self.show_window(shows)
        row = window._rows[EPISODES[2].id]
        assert row.row_actions is not None
        row.row_actions._choose()  # its ☰ Choose…
        await self.settle()
        self.assertEqual(len(shows._live()), 1)
        assert window.panel is not None
        self.assertEqual(window.panel.target, TvTarget(SHOW, 3, 5))
        self.assertTrue(window.showing_releases)

    async def test_fix_match_opens_the_right_shows_window(self) -> None:
        shows, _movies, _search = self.make(InfoLayout.SIDE)
        shows.open_show(SHOW, target=TvTarget(SHOW, 3, 5))
        await self.settle()
        panel = self.show_window(shows).panel
        assert panel is not None
        on_fix_match = cast(Any, panel)._on_fix_match
        self.assertIsNotNone(on_fix_match)  # "✎ Fix Match" is offered
        on_fix_match(TvTarget(OTHER, 3, 5))  # as the panel does once the right show is tracked
        await self.settle()
        other = self.show_window(shows, OTHER.id)
        assert other.panel is not None
        self.assertEqual(other.panel.target, TvTarget(OTHER, 3, 5))
        self.assertTrue(other.showing_releases)

    async def test_a_films_choose(self) -> None:
        _shows, movies, search = self.make(InfoLayout.SIDE)
        MainWindow._grab_movie(cast(Any, self.main(_shows, movies)), FILM)
        await self.settle()
        window = self.film_window(movies)
        assert window.panel is not None
        self.assertEqual(window.panel.target, MovieTarget(FILM))
        self.assertEqual(search.requests, [("movie",)])

    async def test_download_again_choose_from_the_downloads_page(self) -> None:
        shows, movies, search = self.make(InfoLayout.SIDE)
        main = self.main(shows, movies)
        episode_entry = DownloadEntry(
            record=DownloadRecord(
                media_type=MediaType.TV, media_id=SHOW.id, season=3, episode=5, release_title="x"
            ),
            title="Harbour Lights S03E05",
            show=SHOW,
        )
        MainWindow._download_entry_again(main, episode_entry, False)
        await self.settle()
        panel = self.show_window(shows).panel
        assert panel is not None
        self.assertEqual(panel.target, TvTarget(SHOW, 3, 5))
        # A film known by its TMDB id.
        film_entry = DownloadEntry(
            record=DownloadRecord(media_type=MediaType.MOVIE, media_id=FILM.id, release_title="y"),
            title=movie_title(FILM),
        )
        MainWindow._download_entry_again(main, film_entry, False)
        await self.settle()
        film = self.film_window(movies)
        assert film.panel is not None
        self.assertEqual(film.panel.target, MovieTarget(FILM))
        # TMDB doesn't know it: the status bar says so.
        lost = DownloadEntry(
            record=DownloadRecord(media_type=MediaType.MOVIE, media_id=999, release_title="z"),
            title="Lost Film (1999)",
        )
        with self.assertLogs("app.ui.movie_details", "INFO"):
            MainWindow._download_entry_again(main, lost, False)
            await self.settle()
        self.assertEqual(main.reports, ["Couldn't look up Lost Film (1999) on TMDB"])


class PanelHostTests(TkTestCase):
    """The window builds a ReleasePanel only with the panel's services."""

    async def test_without_services_there_is_no_panel(self) -> None:
        discovery = WindowDiscovery()
        window = MovieDetailsWindow(
            self.root,
            FILM,
            images=cast(Any, FakeImages()),
            discovery=cast_to(DiscoveryService, discovery),
            watchlist=cast(Any, FakeWatchlist()),
            actions=DownloadActions[Movie](
                download=lambda m: None, choose=lambda m: None, states=DownloadStates()
            ),
        )
        self.addCleanup(self.destroy_widget, window)
        await self.settle()
        self.assertIsNone(window.panel)
        self.assertIsNone(window.show_releases())
        self.assertNotIsInstance(window.panel, ReleasePanel)


if __name__ == "__main__":
    unittest.main()
