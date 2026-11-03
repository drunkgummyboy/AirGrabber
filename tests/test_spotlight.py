"""The header's spotlight band: which titles it picks (app/services/spotlight.py), its texts in
English and Dutch, when it turns (`Rotation`), where everything goes at 1220 units and wider,
the glow without a TMDB key, the baked pictures (the scrim, the shadows), "Grabber"'s colour in
every palette, the band on a hidden Tk root, the Series / Movies switch on the tabs row under
the band (`ModeSwitch`, right-aligned, level with the tabs, in every mode), and the main pages'
tabs (widgets.Tabview: the "pills" look, the "underline" one still there)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from datetime import UTC, date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import customtkinter as ctk
from PIL import Image

from app import i18n
from app.i18n import Language
from app.models import (
    Accent,
    CalendarEntry,
    DiscoverShow,
    DiscoverShowEntry,
    DownloadEntry,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    Episode,
    FreeSpace,
    MediaType,
    Movie,
    MovieDashboard,
    Show,
    Spotlight,
    SpotlightItem,
    SpotlightReason,
    SpotlightSide,
    TitleExtras,
    TitleInfo,
    TVDashboard,
)
from app.services.spotlight import MAX_ITEMS, SpotlightService, pick_episodes, pick_films
from app.services.tmdb import TMDBError, TMDBNotConfigured
from app.ui import palettes, theme
from app.ui.backdrop import (
    SCRIM_STOPS,
    apply_scrim,
    backdrop_face,
    compose_scene,
    crossfade_frames,
    drop_shadow,
    glow_layer,
    rounded_box,
    scrim_alpha,
)
from app.ui.main_window import build_tabs_row, show_tabs
from app.forge.ui.look import forge_look
from app.ui.spotlight import (
    BADGE_OVERHANG,
    BAND_HEIGHT,
    BUTTON_GAP,
    PAD_X,
    PILL_PAD,
    POSTER_HEIGHT,
    POSTER_TOP,
    ROW_HEIGHT,
    ROW_TOP,
    SEARCH_MIN_WIDTH,
    SEARCH_WIDTH,
    SEGMENT_FONT_SIZE,
    SEGMENT_PAD_X,
    SEGMENT_PAD_Y,
    SWITCH_MARGIN,
    Hit,
    HitKind,
    Rect,
    Rotation,
    band_layout,
    controls,
    film_line,
    hit_target,
    sub_line,
    switch_hit,
    switch_layout,
    tag_text,
    title_text,
)
from app.ui.spotlight_band import SWITCH_ALPHA, ModeSwitch, SpotlightBand
from app.ui.widgets import PILLS_GAP, PILLS_LEFT, UNDERLINE_LEFT, UNDERLINE_TAB_GAP, Tabview

CEST = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=CEST)  # a Friday, noon
TODAY = NOW.date()


def show(show_id: int, name: str) -> Show:
    return Show(id=show_id, name=name, poster_url=f"https://posters.invalid/{show_id}.jpg")


def entry(
    show_id: int,
    name: str,
    *,
    air_date: date | None = TODAY,
    airstamp: datetime | None = None,
    episode_id: int | None = None,
    number: int | None = 5,
    title: str = "Children",
    tracked: bool = True,
) -> CalendarEntry:
    return CalendarEntry(
        show=show(show_id, name),
        episode=Episode(
            id=episode_id if episode_id is not None else show_id * 100 + (number or 0),
            show_id=show_id,
            season=3,
            number=number,
            name=title,
            air_date=air_date,
            airstamp=airstamp,
        ),
        is_tracked=tracked,
    )


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=CEST)


def film(
    film_id: int,
    title: str,
    backdrop: str | None = None,
    *,
    released: date | None = None,
    rating: float = 0.0,
    votes: int = 0,
) -> Movie:
    return Movie(
        id=film_id,
        title=title,
        poster_path=f"/p{film_id}.jpg",
        backdrop_path=backdrop,
        release_date=released,
        vote_average=rating,
        vote_count=votes,
    )


def tmdb_show(show_id: int, name: str) -> DiscoverShow:
    return DiscoverShow(id=show_id, name=name, poster_path=f"/t{show_id}.jpg")


def extras(backdrop: str | None) -> TitleExtras:
    return TitleExtras(info=TitleInfo(id=1, backdrop_path=backdrop))


class FakeLibrary:
    def __init__(self, entries: list[CalendarEntry]) -> None:
        self.entries = entries
        self.asked: list[tuple[date, date]] = []
        self.people: list[int] = []  # the person each calendar was asked for

    async def calendar(self, start: date, end: date, *, profile_id: int) -> list[CalendarEntry]:
        self.asked.append((start, end))
        self.people.append(profile_id)
        return list(self.entries)


class FakeDiscovery:
    def __init__(self, *, key: bool = True) -> None:
        self.has_tmdb_key = key
        self.tv = TVDashboard(
            trending=tuple(
                DiscoverShowEntry(show=tmdb_show(i, f"Trend {i}"), is_tracked=False)
                for i in range(1, 8)
            )
        )
        self.films = MovieDashboard(
            just_dropped=(film(1, "Fresh One"), film(2, "Fresh Two", "/b2.jpg")),
            trending=(film(2, "Fresh Two"), film(3, "Hot Three"), film(4, "Hot Four"),
                      film(5, "Hot Five"), film(6, "Hot Six")),
        )  # fmt: skip
        self.error: TMDBError | None = None
        self.calls: list[str] = []
        self.show_extras_result: TitleExtras | None = extras("/show.jpg")

    async def tv_dashboard(self, *, profile_id: int) -> TVDashboard:
        self.calls.append("tv_dashboard")
        if self.error:
            raise self.error
        return self.tv

    async def movie_dashboard(self) -> MovieDashboard:
        self.calls.append("movie_dashboard")
        if self.error:
            raise self.error
        return self.films

    async def movie_extras(self, movie_id: int) -> TitleExtras:
        self.calls.append(f"movie_extras {movie_id}")
        if self.error:
            raise self.error
        return extras(f"/m{movie_id}.jpg")

    async def tv_extras(self, tv_id: int) -> TitleExtras:
        self.calls.append(f"tv_extras {tv_id}")
        if self.error:
            raise self.error
        return extras(f"/tv{tv_id}.jpg")

    async def show_extras(self, show: Show) -> TitleExtras | None:
        self.calls.append(f"show_extras {show.id}")
        if self.error:
            raise self.error
        return self.show_extras_result


def service(entries: list[CalendarEntry], discovery: FakeDiscovery | None = None) -> SpotlightService:
    return SpotlightService(
        FakeLibrary(entries),  # type: ignore[arg-type]
        discovery or FakeDiscovery(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


class PickingTests(unittest.IsolatedAsyncioTestCase):
    def test_todays_episodes_in_airing_order(self) -> None:
        entries = [
            entry(1, "Late Show", airstamp=at(TODAY, 22)),
            entry(2, "No Time Yet"),  # air date only: after those with a time
            entry(3, "Early Show", airstamp=at(TODAY, 9)),
            entry(4, "Untracked", airstamp=at(TODAY, 8), tracked=False),
            entry(5, "Next Week", air_date=TODAY + timedelta(days=3), airstamp=at(TODAY + timedelta(days=3), 21)),
        ]  # fmt: skip
        items, airing = pick_episodes(entries, NOW)
        self.assertEqual([item.title for item in items], ["Early Show", "Late Show", "No Time Yet"])
        self.assertEqual(airing, 3)
        self.assertTrue(all(item.reason is SpotlightReason.TODAY for item in items))
        self.assertEqual(items[0].show, entries[2].show)
        self.assertEqual(items[0].poster_url, "https://posters.invalid/3.jpg")
        self.assertEqual(items[0].air_day, TODAY)

    def test_the_local_day_counts(self) -> None:
        # Listed for yesterday on its network, but it airs at 01:30 here today.
        overnight = entry(
            1, "Overnight", air_date=TODAY - timedelta(days=1), airstamp=at(TODAY, 1, 30)
        )
        # Listed for today, but here it airs tomorrow at 02:00.
        late = entry(2, "US Late", airstamp=at(TODAY + timedelta(days=1), 2))
        items, airing = pick_episodes([overnight, late], NOW)
        self.assertEqual([item.title for item in items], ["Overnight"])
        self.assertEqual(airing, 1)

    def test_at_most_five_today_but_all_counted(self) -> None:
        entries = [entry(i, f"Show {i}", airstamp=at(TODAY, 12 + i)) for i in range(1, 8)]
        items, airing = pick_episodes(entries, NOW)
        self.assertEqual(len(items), MAX_ITEMS)
        self.assertEqual(airing, 7)
        self.assertEqual(items[0].title, "Show 1")

    def test_none_today_the_coming_week(self) -> None:
        later = TODAY + timedelta(days=2)
        entries = [
            entry(1, "Too Far", air_date=TODAY + timedelta(days=8), airstamp=at(TODAY + timedelta(days=8), 20)),
            entry(2, "Saturday", air_date=TODAY + timedelta(days=1), airstamp=at(TODAY + timedelta(days=1), 20)),
            entry(3, "Sunday", air_date=later, airstamp=at(later, 20)),
            entry(4, "Aired", air_date=TODAY - timedelta(days=1), airstamp=at(TODAY - timedelta(days=1), 20)),
        ]  # fmt: skip
        items, airing = pick_episodes(entries, NOW)
        self.assertEqual([item.title for item in items], ["Saturday", "Sunday"])
        self.assertEqual(airing, 2)
        self.assertTrue(all(item.reason is SpotlightReason.COMING for item in items))

    async def test_series_asks_a_day_either_side(self) -> None:
        spotlight_service = service([entry(1, "Tonight", airstamp=at(TODAY, 21))])
        spotlight = await spotlight_service.spotlight(SpotlightSide.SERIES, profile_id=1)
        self.assertEqual(spotlight.side, SpotlightSide.SERIES)
        self.assertEqual(spotlight.airing, 1)
        self.assertEqual(spotlight.reason, SpotlightReason.TODAY)
        library = spotlight_service._library
        self.assertEqual(library.asked, [(TODAY - timedelta(days=1), TODAY + timedelta(days=8))])  # type: ignore[attr-defined]

    async def test_no_episodes_trending_shows(self) -> None:
        discovery = FakeDiscovery()
        spotlight = await service([], discovery).spotlight(SpotlightSide.SERIES, profile_id=1)
        self.assertEqual([item.title for item in spotlight.items], [f"Trend {i}" for i in range(1, 6)])
        self.assertTrue(all(item.reason is SpotlightReason.TRENDING for item in spotlight.items))
        self.assertEqual(spotlight.airing, 0)
        self.assertEqual(spotlight.items[0].poster_url, "https://image.tmdb.org/t/p/w185/t1.jpg")

    async def test_no_key_or_tmdb_failing_nothing(self) -> None:
        no_key = FakeDiscovery(key=False)
        spotlight = await service([], no_key).spotlight(SpotlightSide.SERIES, profile_id=1)
        self.assertEqual(spotlight.items, ())
        self.assertEqual(no_key.calls, [])  # nothing asked without a key
        self.assertEqual((await service([], no_key).spotlight(SpotlightSide.MOVIES, profile_id=1)).items, ())
        failing = FakeDiscovery()
        failing.error = TMDBError("TMDB is down")
        self.assertEqual((await service([], failing).spotlight(SpotlightSide.SERIES, profile_id=1)).items, ())
        self.assertEqual((await service([], failing).spotlight(SpotlightSide.MOVIES, profile_id=1)).items, ())

    async def test_followed_episodes_without_a_key(self) -> None:
        spotlight = await service(
            [entry(1, "Mine", airstamp=at(TODAY, 21))], FakeDiscovery(key=False)
        ).spotlight(SpotlightSide.SERIES, profile_id=1)
        self.assertEqual([item.title for item in spotlight.items], ["Mine"])

    def test_films_new_first_then_trending(self) -> None:
        items = pick_films(FakeDiscovery().films)
        self.assertEqual(
            [item.title for item in items],
            ["Fresh One", "Fresh Two", "Hot Three", "Hot Four", "Hot Five"],
        )
        self.assertEqual(
            [item.reason for item in items],
            [SpotlightReason.NEW_TO_STREAM] * 2 + [SpotlightReason.TRENDING] * 3,
        )
        self.assertEqual(items[0].movie, film(1, "Fresh One"))
        only_trending = pick_films(MovieDashboard(trending=(film(9, "Nine"),)))
        self.assertEqual([(i.title, i.reason) for i in only_trending], [("Nine", SpotlightReason.TRENDING)])

    async def test_movies_side(self) -> None:
        spotlight = await service([]).spotlight(SpotlightSide.MOVIES, profile_id=1)
        self.assertEqual(spotlight.side, SpotlightSide.MOVIES)
        self.assertEqual(len(spotlight.items), MAX_ITEMS)
        self.assertEqual(spotlight.reason, SpotlightReason.NEW_TO_STREAM)

    async def test_backdrops(self) -> None:
        discovery = FakeDiscovery()
        spot = service([], discovery)
        with_backdrop = SpotlightItem(reason=SpotlightReason.TRENDING, title="F", movie=film(2, "F", "/b2.jpg"))
        self.assertEqual(await spot.backdrop_url(with_backdrop, "w1280"), "https://image.tmdb.org/t/p/w1280/b2.jpg")
        self.assertEqual(discovery.calls, [])  # the list's own backdrop: no request
        without = SpotlightItem(reason=SpotlightReason.TRENDING, title="G", movie=film(7, "G"))
        self.assertEqual(await spot.backdrop_url(without, "w780"), "https://image.tmdb.org/t/p/w780/m7.jpg")
        trending = SpotlightItem(reason=SpotlightReason.TRENDING, title="T", discover_show=tmdb_show(4, "T"))
        self.assertEqual(await spot.backdrop_url(trending, "w1280"), "https://image.tmdb.org/t/p/w1280/tv4.jpg")
        followed = SpotlightItem(reason=SpotlightReason.TODAY, title="S", show=show(8, "S"))
        self.assertEqual(await spot.backdrop_url(followed, "w1280"), "https://image.tmdb.org/t/p/w1280/show.jpg")
        self.assertEqual(discovery.calls, ["movie_extras 7", "tv_extras 4", "show_extras 8"])
        discovery.show_extras_result = None  # no TMDB match for the show
        self.assertIsNone(await spot.backdrop_url(followed, "w1280"))
        discovery.error = TMDBNotConfigured("no key")
        self.assertIsNone(await spot.backdrop_url(trending, "w1280"))
        no_key = service([], FakeDiscovery(key=False))
        self.assertIsNone(await no_key.backdrop_url(followed, "w1280"))
        self.assertIsNotNone(await no_key.backdrop_url(with_backdrop, "w1280"))  # in the list


def episode_item(
    airs_at: datetime | None,
    day: date,
    reason: SpotlightReason,
    name: str = "Children",
    episode_id: int = 1,
) -> SpotlightItem:
    return SpotlightItem(
        reason=reason,
        title="The Bear",
        show=show(1, "The Bear"),
        episode=Episode(
            id=episode_id, show_id=1, season=3, number=5, name=name, air_date=day, airstamp=airs_at
        ),
        airs_at=airs_at,
        air_day=day,
        poster_url="https://posters.invalid/1.jpg",
    )


class TextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    def tags(self) -> list[str]:
        tomorrow = TODAY + timedelta(days=1)
        friday = date(2026, 11, 6)
        return [
            tag_text(episode_item(at(TODAY, 21), TODAY, SpotlightReason.TODAY), NOW),
            tag_text(episode_item(at(TODAY, 14), TODAY, SpotlightReason.TODAY), NOW),
            tag_text(episode_item(None, TODAY, SpotlightReason.TODAY), NOW),
            tag_text(episode_item(at(tomorrow, 20), tomorrow, SpotlightReason.COMING), NOW),
            tag_text(episode_item(at(friday, 20), friday, SpotlightReason.COMING), NOW),
            tag_text(SpotlightItem(reason=SpotlightReason.TRENDING, title="X"), NOW),
            tag_text(SpotlightItem(reason=SpotlightReason.NEW_TO_STREAM, title="X"), NOW),
        ]

    def test_tags(self) -> None:
        self.assertEqual(
            self.tags(),
            ["TONIGHT · 21:00", "TODAY · 14:00", "TODAY", "TOMORROW · 20:00", "FRI 6 NOV",
             "TRENDING", "NEW TO STREAM"],
        )  # fmt: skip
        i18n.set_language(Language.NL)
        self.assertEqual(
            self.tags(),
            ["VANAVOND · 21:00", "VANDAAG · 14:00", "VANDAAG", "MORGEN · 20:00", "VR 6 NOV",
             "POPULAIR", "NET DIGITAAL UIT"],
        )  # fmt: skip

    def test_the_time_is_local(self) -> None:
        # 03:00 UTC is 05:00 here, still today.
        utc = datetime.combine(TODAY, time(3, 0), tzinfo=UTC)
        self.assertEqual(tag_text(episode_item(utc, TODAY, SpotlightReason.TODAY), NOW), "TODAY · 05:00")

    def test_titles(self) -> None:
        item = episode_item(None, TODAY, SpotlightReason.TODAY)
        self.assertEqual(title_text(item), "The Bear — S03E05 “Children”")
        self.assertEqual(title_text(episode_item(None, TODAY, SpotlightReason.TODAY, name="")), "The Bear — S03E05")
        self.assertEqual(title_text(SpotlightItem(reason=SpotlightReason.TRENDING, title="Dune")), "Dune")
        i18n.set_language(Language.NL)
        self.assertEqual(title_text(item), "The Bear — S03E05 ‘Children’")

    def test_sub_lines(self) -> None:
        item = episode_item(None, TODAY, SpotlightReason.TODAY)
        today = Spotlight(side=SpotlightSide.SERIES, items=(item,), airing=3)
        one = Spotlight(side=SpotlightSide.SERIES, items=(item,), airing=1)
        coming = Spotlight(
            side=SpotlightSide.SERIES,
            items=(episode_item(None, TODAY, SpotlightReason.COMING),),
            airing=4,
        )
        trending = Spotlight(
            side=SpotlightSide.SERIES,
            items=(SpotlightItem(reason=SpotlightReason.TRENDING, title="X"),),
        )
        # What airs; how many downloads are on their way is the ⬇ button's badge, not this line's.
        self.assertEqual(sub_line(today, item), "3 episodes today")
        self.assertEqual(sub_line(one, item), "1 episode today")
        self.assertEqual(sub_line(coming), "4 episodes in the next 7 days")
        self.assertEqual(sub_line(trending, trending.items[0]), "")
        self.assertEqual(sub_line(None), "")
        i18n.set_language(Language.NL)
        self.assertEqual(sub_line(today, item), "3 afleveringen vandaag")
        self.assertEqual(sub_line(one), "1 aflevering vandaag")
        self.assertEqual(sub_line(coming), "4 afleveringen in de komende 7 dagen")

    def test_a_films_year_and_rating(self) -> None:
        dune = film(1, "Dune", released=date(2026, 3, 1), rating=7.04, votes=120)
        movies = Spotlight(
            side=SpotlightSide.MOVIES,
            items=(SpotlightItem(reason=SpotlightReason.NEW_TO_STREAM, title="Dune", movie=dune),),
        )
        self.assertEqual(sub_line(movies, movies.items[0]), "2026 · ★ 7.0")
        self.assertEqual(film_line(dune), "2026 · ★ 7.0")
        # Whatever is known: no votes = no rating; neither = nothing.
        self.assertEqual(film_line(film(2, "X", released=date(2025, 1, 1), rating=8.0)), "2025")
        self.assertEqual(film_line(film(3, "Y", rating=6.55, votes=3)), "★ 6.5")
        self.assertEqual(film_line(film(4, "Z")), "")
        i18n.set_language(Language.NL)
        self.assertEqual(film_line(dune), "2026 · ★ 7,0")


class RotationTests(unittest.TestCase):
    def test_turns_every_interval(self) -> None:
        rotation = Rotation(interval=8.0)
        rotation.reset(3, 0, now=100.0)
        self.assertFalse(rotation.due(107.9))
        self.assertTrue(rotation.due(108.0))
        self.assertEqual(rotation.advance(108.0), 1)
        self.assertFalse(rotation.due(115.0))
        self.assertEqual(rotation.advance(116.0), 2)
        self.assertEqual(rotation.advance(124.0), 0)  # round again

    def test_one_title_never_turns(self) -> None:
        rotation = Rotation()
        rotation.reset(1, 0, now=0.0)
        self.assertFalse(rotation.due(1000.0))
        rotation.reset(0, 3, now=0.0)
        self.assertEqual(rotation.index, 0)

    def test_paused_under_the_pointer(self) -> None:
        rotation = Rotation(interval=8.0)
        rotation.reset(3, 0, now=0.0)
        rotation.hover(True, now=5.0)
        self.assertFalse(rotation.due(20.0))
        rotation.hover(False, now=20.0)  # a full interval after leaving
        self.assertFalse(rotation.due(27.0))
        self.assertTrue(rotation.due(28.0))

    def test_motion_off_stays_but_dots_jump(self) -> None:
        rotation = Rotation(interval=8.0, motion=False)
        rotation.reset(3, 0, now=0.0)
        self.assertFalse(rotation.due(100.0))
        self.assertTrue(rotation.jump(2, now=100.0))
        self.assertEqual(rotation.index, 2)
        self.assertFalse(rotation.jump(2, now=101.0))  # already showing
        self.assertFalse(rotation.jump(5, now=101.0))  # no such title
        rotation.set_motion(True, now=200.0)
        self.assertFalse(rotation.due(207.0))
        self.assertTrue(rotation.due(208.0))

    def test_a_jump_starts_a_full_interval(self) -> None:
        rotation = Rotation(interval=8.0)
        rotation.reset(3, 0, now=0.0)
        rotation.jump(1, now=7.0)
        self.assertFalse(rotation.due(10.0))
        self.assertTrue(rotation.due(15.0))


# Roughly "AirGrabber" at 18 bold; "Movies" at 17 bold plus 30 either side, a 17 bold line.
WORD, SEGMENT, LINE = 100.0, 120.0, 23.0


class LayoutTests(unittest.TestCase):
    def check(self, width: float, *, dots: int = 5, current: int = 0) -> None:
        layout = band_layout(width, word_width=WORD, dots=dots, current=current)
        parts = controls(layout)
        for index, part in enumerate(parts):
            self.assertGreaterEqual(part.x, PAD_X - 0.01, (width, part))
            self.assertLessEqual(part.right, width - PAD_X + 0.01, (width, part))
            for other in parts[index + 1 :]:
                self.assertFalse(part.overlaps(other), (width, part, other))
        self.assertLessEqual(layout.entry.right, layout.search.right)
        self.assertGreaterEqual(layout.entry.x, layout.magnifier.right)
        self.assertLess(layout.text_x, layout.text_right)
        if layout.dots:
            self.assertAlmostEqual(layout.dots[-1].right, width - PAD_X)
            self.assertLess(layout.text_right, layout.dots[0].x)
            self.assertEqual(layout.dots[current].w, max(dot.w for dot in layout.dots))
        for rect in (layout.poster, *layout.dots):
            self.assertLessEqual(rect.bottom, BAND_HEIGHT)

    def test_room_above_the_row_and_around_the_spotlight(self) -> None:
        layout = band_layout(1220, word_width=WORD)
        self.assertEqual(ROW_TOP, 28)  # 14 more than 2.0.8's: the row isn't pressed to the top
        self.assertEqual(layout.search.y, ROW_TOP)
        self.assertEqual(layout.logo.y, ROW_TOP)
        self.assertEqual(POSTER_TOP - (ROW_TOP + ROW_HEIGHT), 22)  # the row → the spotlight
        self.assertEqual(BAND_HEIGHT - (POSTER_TOP + POSTER_HEIGHT), 18)  # → the page tabs

    def test_at_the_narrowest_window(self) -> None:
        self.check(1220)
        layout = band_layout(1220, word_width=WORD, dots=5)
        self.assertEqual(layout.search.w, SEARCH_WIDTH)  # room for the full search field
        # The top row: only the logo with its name, the search field and the three buttons.
        self.assertEqual(
            controls(layout),
            (layout.brand, layout.search, layout.forge, layout.downloads, layout.settings),
        )

    def test_the_forge_button_left_of_downloads(self) -> None:
        """D2: 🔨 MediaForge just left of ⬇ Downloads, the same size, the search field before it;
        its badge's corner where ⬇'s is on ⬇."""
        for width in (1220, 1650, 1920, 2560, 900):
            with self.subTest(width):
                layout = band_layout(width, word_width=WORD, dots=3)
                forge, downloads = layout.forge, layout.downloads
                self.assertEqual((forge.w, forge.h, forge.y), (downloads.w, downloads.h, downloads.y))
                self.assertAlmostEqual(forge.right + BUTTON_GAP, downloads.x)
                self.assertLess(layout.search.right, forge.x)
                self.assertEqual(
                    layout.forge_badge, (forge.right + BADGE_OVERHANG, forge.y - BADGE_OVERHANG)
                )
                self.assertLess(layout.forge_badge[0], downloads.x)  # in the gap, not on ⬇

    def test_wide_windows(self) -> None:
        for width in (1650, 1920, 2560):
            with self.subTest(width):
                self.check(width, dots=3, current=2)
                layout = band_layout(width, word_width=WORD)
                self.assertEqual(layout.search.w, SEARCH_WIDTH)
                self.assertEqual(layout.dots, ())

    def test_narrower_still_never_overlaps(self) -> None:
        for width in (1100, 1000, 900):
            with self.subTest(width):
                self.check(width)
                layout = band_layout(width, word_width=WORD)
                self.assertGreaterEqual(layout.search.w, SEARCH_MIN_WIDTH)
        # Far narrower than the window gets (550: since 🔨 joined the row): the search field
        # gives way first.
        self.check(550)
        self.assertEqual(band_layout(550, word_width=WORD).search.w, SEARCH_MIN_WIDTH)

    def test_what_the_pointer_is_over(self) -> None:
        layout = band_layout(1220, word_width=WORD, dots=3, current=1)

        def middle(rect: Any) -> tuple[float, float]:
            return rect.x + rect.w / 2, rect.y + rect.h / 2

        self.assertIsNone(hit_target(layout, 610, ROW_TOP + ROW_HEIGHT / 2, items=3))  # no switch up top
        self.assertEqual(hit_target(layout, *middle(layout.magnifier), items=3), Hit(HitKind.SEARCH))
        self.assertEqual(hit_target(layout, layout.search.right - 4, layout.search.y + 20, items=3), Hit(HitKind.FIELD))
        self.assertEqual(hit_target(layout, *middle(layout.downloads), items=3), Hit(HitKind.DOWNLOADS))
        self.assertEqual(hit_target(layout, *middle(layout.forge), items=3), Hit(HitKind.FORGE))
        gap = (layout.forge.right + layout.downloads.x) / 2  # between 🔨 and ⬇: neither
        self.assertIsNone(hit_target(layout, gap, layout.forge.y + 20, items=3))
        self.assertEqual(hit_target(layout, *middle(layout.settings), items=3), Hit(HitKind.SETTINGS))
        self.assertEqual(hit_target(layout, *middle(layout.dots[2]), items=3), Hit(HitKind.DOT, 2))
        self.assertEqual(hit_target(layout, *middle(layout.poster), items=3), Hit(HitKind.SPOT))
        self.assertEqual(hit_target(layout, 600, BAND_HEIGHT - 5, items=3), Hit(HitKind.SPOT))
        self.assertIsNone(hit_target(layout, *middle(layout.poster), items=0))  # nothing to open
        self.assertIsNone(hit_target(layout, *middle(layout.logo), items=3))

    def test_the_switch(self) -> None:
        """Bigger than 2.0.9's (17 bold, 30 x 9 around each name, 4 around both), on a canvas of
        its own with room for its shadow."""
        self.assertEqual((SEGMENT_FONT_SIZE, SEGMENT_PAD_X, SEGMENT_PAD_Y, PILL_PAD), (17, 30, 9, 4))
        layout = switch_layout(segment_width=SEGMENT, line_height=LINE)
        series, movies = layout.segments
        self.assertEqual(layout.pill, Rect(SWITCH_MARGIN, SWITCH_MARGIN, 2 * SEGMENT + 8, LINE + 18 + 8))
        self.assertEqual(series, Rect(SWITCH_MARGIN + 4, SWITCH_MARGIN + 4, SEGMENT, LINE + 18))
        self.assertEqual(movies.x, series.right)
        self.assertEqual(layout.size, (layout.pill.right + SWITCH_MARGIN, layout.pill.bottom + SWITCH_MARGIN))
        self.assertEqual(switch_hit(layout, series.x + 5, series.y + 5), 0)
        self.assertEqual(switch_hit(layout, movies.right - 5, movies.bottom - 5), 1)
        self.assertIsNone(switch_hit(layout, 1, 1))  # the shadow's margin
        self.assertIsNone(switch_hit(layout, layout.pill.x + 1, layout.pill.y + 1))  # the pill's edge


class PictureTests(unittest.TestCase):
    BG, ACCENT = (22, 22, 22), (78, 106, 133)

    def test_the_glow_without_a_key(self) -> None:
        glow = glow_layer((400, 60), self.BG, self.ACCENT, True)
        self.assertEqual(glow.size, (400, 60))
        self.assertEqual(glow.mode, "RGB")
        # The accent shows near the top right of the middle, the bottom is the background.
        tinted = glow.getpixel((288, 15))
        assert isinstance(tinted, tuple)
        self.assertGreater(tinted[2], self.BG[2] + 20)
        bottom = glow.getpixel((288, 59))
        assert isinstance(bottom, tuple)
        for channel, expected in zip(bottom, self.BG, strict=True):
            self.assertLessEqual(abs(channel - expected), 3)
        light = glow_layer((400, 60), (242, 244, 247), self.ACCENT, False)
        self.assertNotEqual(light.getpixel((288, 15)), (242, 244, 247))

    def test_scene_and_crossfade(self) -> None:
        layer = Image.new("RGB", (200, 80), (10, 10, 10))
        poster = rounded_box((20, 30), 4, (200, 0, 0, 255))
        scene = compose_scene(layer, poster, (10, 20), radius=4, shadow_blur=3, shadow_offset=2)
        self.assertEqual(scene.getpixel((20, 35)), (200, 0, 0))
        self.assertEqual(layer.getpixel((20, 35)), (10, 10, 10))  # the layer stays as it was
        self.assertEqual(compose_scene(layer, None, (0, 0), radius=1, shadow_blur=1, shadow_offset=0).size, (200, 80))
        frames = crossfade_frames(layer, scene, 8)
        self.assertEqual(len(frames), 8)
        self.assertIs(frames[-1], scene)
        middle = frames[3].getpixel((20, 35))
        assert isinstance(middle, tuple)
        self.assertTrue(10 < middle[0] < 200)
        self.assertEqual(crossfade_frames(layer, Image.new("RGB", (100, 80)), 8)[0].size, (100, 80))

    def test_the_scrim_over_a_backdrop(self) -> None:
        self.assertEqual(SCRIM_STOPS, ((0.0, 0.78), (0.38, 0.45), (0.70, 0.25), (1.0, 0.45)))
        self.assertAlmostEqual(scrim_alpha(0.0), 0.78)
        self.assertAlmostEqual(scrim_alpha(0.38), 0.45)
        self.assertAlmostEqual(scrim_alpha(0.70), 0.25)
        self.assertAlmostEqual(scrim_alpha(1.0), 0.45)
        self.assertAlmostEqual(scrim_alpha(0.19), (0.78 + 0.45) / 2)
        white = Image.new("RGB", (400, 60), (255, 255, 255))
        dark = apply_scrim(white, self.BG)
        light = apply_scrim(Image.new("RGB", (400, 60), (0, 0, 0)), (242, 244, 247))
        left, middle = dark.getpixel((0, 30)), dark.getpixel((280, 30))
        assert isinstance(left, tuple) and isinstance(middle, tuple)
        self.assertLess(left[0], middle[0])  # darkest behind the logo
        self.assertAlmostEqual(left[0], 255 * 0.22 + 22 * 0.78, delta=3)
        washed = light.getpixel((0, 30))
        assert isinstance(washed, tuple)
        self.assertAlmostEqual(washed[0], 242 * 0.78, delta=3)  # light: a light wash
        # The finish step: the fade into the background (the bottom row), then the scrim.
        face = backdrop_face((400, 60), self.BG)
        self.assertEqual(face, backdrop_face((400, 60), self.BG))  # a cache key
        finished = face(white)
        bottom, top_left, top_middle = (finished.getpixel(xy) for xy in ((200, 59), (0, 0), (280, 0)))
        assert isinstance(bottom, tuple) and isinstance(top_left, tuple) and isinstance(top_middle, tuple)
        self.assertLessEqual(abs(bottom[0] - self.BG[0]), 3)
        self.assertLess(top_left[0], top_middle[0])

    def test_drop_shadow(self) -> None:
        logo = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        logo.paste((200, 30, 30, 255), (4, 4, 16, 16))
        shadowed, pad = drop_shadow(logo, 2.0, 2, 0.6)
        self.assertEqual(shadowed.size, (20 + 2 * pad, 20 + 2 * pad))
        self.assertEqual(shadowed.getpixel((pad + 10, pad + 10)), (200, 30, 30, 255))  # the logo
        under = shadowed.getpixel((pad + 10, pad + 17))  # just under it: the shadow
        assert isinstance(under, tuple)
        self.assertEqual(under[:3], (0, 0, 0))
        self.assertTrue(0 < under[3] <= round(255 * 0.6))
        self.assertEqual(shadowed.getpixel((0, 0))[3], 0)  # type: ignore[index]

    def test_rounded_box_corners_see_through(self) -> None:
        box = rounded_box((40, 40), 10, (36, 36, 36, 255), (78, 106, 133, 255), 2)
        self.assertEqual(box.mode, "RGBA")
        self.assertEqual(box.getpixel((0, 0))[3], 0)  # type: ignore[index]
        self.assertEqual(box.getpixel((20, 20)), (36, 36, 36, 255))
        self.assertEqual(box.getpixel((20, 0))[:3], (78, 106, 133))  # type: ignore[index]


# -- the band on a hidden Tk root ----------------------------------------------------------------


class FakeStatus:
    def __init__(self) -> None:
        self.latest: DownloadsOverview | None = None
        self.listeners: list[Any] = []
        self.space_listeners: list[Any] = []

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def subscribe_space(self, listener: Any) -> Any:
        self.space_listeners.append(listener)
        return lambda: self.space_listeners.remove(listener)

    def publish(self, overview: DownloadsOverview) -> None:
        self.latest = overview
        for listener in list(self.listeners):
            listener(overview)


class FakeImages:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def get(self, url: str | None, size: tuple[int, int], *, finish: Any = None) -> Image.Image | None:
        if not url:
            return None
        self.asked.append(url)
        image = Image.new("RGB", size, (120, 60, 30))
        return finish(image) if finish is not None else image


class FakeSpotlight:
    def __init__(self) -> None:
        self.people: list[int] = []  # the person each side was asked for
        self.sides: dict[SpotlightSide, Spotlight] = {
            SpotlightSide.SERIES: Spotlight(
                side=SpotlightSide.SERIES,
                items=tuple(
                    episode_item(
                        at(TODAY, 20 + i), TODAY, SpotlightReason.TODAY, f"Ep {i}", episode_id=i
                    )
                    for i in range(3)
                ),
                airing=3,
            ),
            SpotlightSide.MOVIES: Spotlight(
                side=SpotlightSide.MOVIES,
                items=(
                    SpotlightItem(
                        reason=SpotlightReason.NEW_TO_STREAM,
                        title="Dune",
                        movie=film(1, "Dune", released=date(2026, 9, 1), rating=7.0, votes=50),
                    ),
                ),
            ),
        }
        self.backdrops: dict[str, str | None] = {}

    async def spotlight(self, side: SpotlightSide, *, profile_id: int) -> Spotlight:
        self.people.append(profile_id)
        return self.sides[side]

    async def backdrop_url(self, item: SpotlightItem, size: str) -> str | None:
        return self.backdrops.get(item.title, f"https://backdrops.invalid/{item.title}/{size}.jpg")


def downloading(*states: DownloadState) -> DownloadsOverview:
    return DownloadsOverview(
        entries=tuple(
            DownloadEntry(
                record=DownloadRecord(
                    id=index,
                    media_type=MediaType.TV,
                    media_id=1,
                    season=1,
                    episode=index,
                    release_title=f"R{index}",
                    downloaded_at=datetime(2026, 10, 30, tzinfo=UTC),
                ),
                title=f"Show S01E0{index}",
                state=state,
            )
            for index, state in enumerate(states, 1)
        )
    )


def option(band: SpotlightBand, item: int, name: str) -> str:
    """A canvas item's option (tkinter's itemcget is untyped)."""
    canvas: Any = band
    return str(canvas.itemcget(item, name))


class BandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0
        self.status = FakeStatus()
        self.status.latest = downloading(DownloadState.DOWNLOADING)
        self.service = FakeSpotlight()
        self.images = FakeImages()
        self.events: list[Any] = []
        self.person = 1  # the selected person the band reads

    def band(self, *, motion: bool = True) -> SpotlightBand:
        band = SpotlightBand(
            self.root,
            spotlight=self.service,  # type: ignore[arg-type]
            downloads=self.status,  # type: ignore[arg-type]
            images=self.images,  # type: ignore[arg-type]
            motion=motion,
            on_downloads=lambda: self.events.append("downloads"),
            on_forge=lambda: self.events.append("forge"),
            on_settings=lambda: self.events.append("settings"),
            on_search=lambda: self.events.append("search"),
            on_open=lambda item: self.events.append(("open", item.title)),
            report=lambda text, ms: self.events.append(("report", text, ms)),
            profile=lambda: self.person,
            on_person=lambda: self.events.append("person"),
            on_home=lambda: self.events.append("home"),
            clock=lambda: NOW,
        )
        band.pack(fill="x")
        scale = float(ctk.ScalingTracker.get_widget_scaling(band))
        width = round(1220 * scale)
        self.root.geometry(f"{width}x{round(300 * scale)}")  # CTk scales this; the band follows
        band._on_configure(SimpleNamespace(width=width))
        return band

    async def settle(self, rounds: int = 40) -> None:
        for _ in range(rounds):
            self.root.update()
            await asyncio.sleep(0.01)

    def text(self, band: SpotlightBand, item: int) -> str:
        return option(band, item, "text")

    async def test_shows_tonights_titles_and_turns(self) -> None:
        band = self.band()
        band.start()
        await self.settle()
        self.assertEqual(band.current, self.service.sides[SpotlightSide.SERIES].items[0])
        self.assertEqual(self.text(band, band._tag_item), "TONIGHT · 20:00")
        self.assertEqual(self.text(band, band._title_item), "The Bear — S03E05 “Ep 0”")
        self.assertEqual(self.text(band, band._sub_item), "3 episodes today")
        self.assertEqual(len(band._dot_items), 3)
        self.assertIsNotNone(band._shown)
        assert band._shown is not None
        self.assertEqual(band._shown.size, (band._width, band._px(BAND_HEIGHT)))
        self.assertIn("https://backdrops.invalid/The Bear/w1280.jpg", self.images.asked)
        band.jump(2)
        await self.settle(80)  # the crossfade
        self.assertEqual(self.text(band, band._title_item), "The Bear — S03E05 “Ep 2”")
        self.assertEqual(band.rotation.index, 2)

    async def test_without_a_backdrop_the_glow(self) -> None:
        self.service.backdrops["The Bear"] = None
        band = self.band()
        band.start()
        await self.settle()
        self.assertIn("https://posters.invalid/1.jpg", self.images.asked)  # the poster still
        self.assertFalse(any("backdrops.invalid" in url for url in self.images.asked))
        self.assertIsNotNone(band._shown)
        self.assertEqual(self.text(band, band._title_item), "The Bear — S03E05 “Ep 0”")  # still shown

    async def test_the_controls(self) -> None:
        band = self.band()
        band.start()
        await self.settle()
        for kind in (HitKind.SEARCH, HitKind.FORGE, HitKind.DOWNLOADS, HitKind.SETTINGS, HitKind.SPOT):
            band._activate(Hit(kind))
        self.assertEqual(self.events, ["search", "forge", "downloads", "settings", ("open", "The Bear")])
        layout = band.layout
        assert layout is not None
        scale = band._scale
        x, y = layout.downloads.x + 20, layout.downloads.y + 20
        self.assertEqual(band._hit_at(round(x * scale), round(y * scale)), Hit(HitKind.DOWNLOADS))
        x, y = layout.forge.x + 20, layout.forge.y + 20
        self.assertEqual(band._hit_at(round(x * scale), round(y * scale)), Hit(HitKind.FORGE))
        # 🔨 drawn like ⬇: the same box at its own place, the glyph in its middle.
        self.assertEqual(self.text(band, band._forge_glyph), "🔨")
        self.assertEqual(band.coords(band._forge_item), [band._px(layout.forge.x), band._px(layout.forge.y)])
        forge_box = band._photo_sources["forge"]
        self.assertEqual(forge_box.size, band._photo_sources["settings"].size)
        self.assertEqual(
            band.coords(band._forge_glyph),
            [band._px(layout.forge.x + layout.forge.w / 2), band._px(layout.forge.y + layout.forge.h / 2)],
        )
        self.assertEqual(option(band, band._forge_glyph, "fill"), theme.pick(theme.BUTTON_TEXT))

    async def test_the_logo_and_the_wordmark_go_home(self) -> None:
        band = self.band()
        band.start()
        await self.settle()
        layout = band.layout
        assert layout is not None
        scale = band._scale

        def at(x: float, y: float) -> SimpleNamespace:
            return SimpleNamespace(x=round(x * scale), y=round(y * scale))

        logo = at(layout.logo.x + layout.logo.w / 2, layout.logo.y + layout.logo.h / 2)
        word = at(layout.brand.right - 4, layout.brand.y + layout.brand.h / 2)
        for point in (logo, word):
            band._on_motion(point)
            self.assertEqual(str(band.cget("cursor")), "hand2")
            band._on_press(point)
            band._on_release(point)
        self.assertEqual(self.events, ["home", "home"])
        # Off it: the normal pointer again, and no home.
        band._on_leave(None)
        self.assertEqual(str(band.cget("cursor")), "")
        band._on_motion(logo)
        band._on_motion(at(layout.brand.right + 2, 4))  # the background up top
        self.assertEqual(str(band.cget("cursor")), "")
        band._on_press(logo)
        band._on_release(at(layout.brand.right + 2, 4))  # let go elsewhere
        self.assertEqual(self.events, ["home", "home"])
        # The field's cursor still follows its own rule next to it.
        field = at(layout.search.x + layout.search.w / 2, layout.search.y + layout.search.h / 2)
        band._on_motion(field)
        self.assertEqual(str(band.cget("cursor")), "xterm")

    async def test_the_forge_tip(self) -> None:
        band = self.band()
        band.start()
        await self.settle()
        layout = band.layout
        assert layout is not None
        band._hover = Hit(HitKind.FORGE)
        shown: list[str] = []
        band._tip.show = lambda rect, text, gap: shown.append(text)  # type: ignore[method-assign]
        band.winfo_ismapped = lambda: True  # type: ignore[method-assign]
        band._show_tip(Hit(HitKind.FORGE))
        self.assertEqual(shown, ["MediaForge"])

    async def test_the_forge_badge_in_copper(self) -> None:
        band = self.band()
        band.start()
        await self.settle()
        self.assertEqual(option(band, band._forge_badge_item, "state"), "hidden")  # none yet
        band.set_forge_count(3)
        self.assertEqual(self.text(band, band._forge_badge_text), "3")
        self.assertEqual(option(band, band._forge_badge_item, "state"), "normal")
        self.assertEqual(option(band, band._forge_badge_text, "fill"), theme.pick(theme.ON_COLOR))
        badge = band._photo_sources["forge_badge"]
        middle: Any = badge.getpixel((badge.width // 2, badge.height // 2))
        copper = theme.pick(forge_look().ACCENT)
        self.assertEqual(middle[:3], tuple(int(copper[i : i + 2], 16) for i in (1, 3, 5)))
        layout = band.layout
        assert layout is not None
        right, top = layout.forge_badge
        x, y = band.coords(band._forge_badge_item)
        self.assertEqual((round(x) + badge.width, round(y)), (band._px(right), band._px(top)))
        band.set_forge_count(150)
        self.assertEqual(self.text(band, band._forge_badge_text), "99+")
        self.assertEqual(self.text(band, band._badge_text), "1")  # ⬇'s own badge untouched
        band.set_forge_count(0)
        self.assertEqual(option(band, band._forge_badge_item, "state"), "hidden")
        self.assertEqual(option(band, band._forge_badge_text, "state"), "hidden")
        band.set_forge_count(2)
        band._repaint()  # light ↔ dark and scaling paint it again, still shown
        band._rescaled(1.0, 1.0)
        self.assertEqual(option(band, band._forge_badge_item, "state"), "normal")
        self.assertEqual(self.text(band, band._forge_badge_text), "2")

    def test_the_tray_badge_is_copper_too(self) -> None:
        from app.ui import tray

        picture = tray.with_badge(Image.new("RGBA", (128, 128), (0, 0, 0, 0)), 2)
        radius = 128 // 4
        # Off the digit, inside the dot: Forge's copper (one colour in both modes).
        pixel: Any = picture.getpixel((127 - radius, 127 - 2 * radius + 3))
        copper = theme.pick(forge_look().ACCENT, dark=True)
        self.assertEqual(pixel[:3], tuple(int(copper[i : i + 2], 16) for i in (1, 3, 5)))

    async def test_switching_sides_and_the_downloads_page(self) -> None:
        band = self.band()
        band.start()
        await self.settle()
        band.select(SpotlightSide.MOVIES)
        await self.settle(80)
        self.assertEqual(band.side, SpotlightSide.MOVIES)
        self.assertEqual(self.text(band, band._title_item), "Dune")
        self.assertEqual(self.text(band, band._tag_item), "NEW TO STREAM")
        self.assertEqual(self.text(band, band._sub_item), "2026 · ★ 7.0")  # a film's year, rating
        self.assertEqual(band._dot_items, [])  # one title: no dots
        band.select(None)  # the Downloads page keeps the Movies side's title
        await self.settle()
        self.assertEqual(band.side, SpotlightSide.MOVIES)
        self.assertEqual(self.text(band, band._title_item), "Dune")

    async def test_badge_line_and_space_notice(self) -> None:
        band = self.band()
        band.start()
        await self.settle()
        self.assertEqual(self.text(band, band._badge_text), "1")
        self.status.publish(downloading(DownloadState.DOWNLOADING, DownloadState.WAITING, DownloadState.READY))
        self.assertEqual(self.text(band, band._badge_text), "2")
        self.assertEqual(option(band, band._badge_item, "state"), "normal")
        self.assertEqual(self.text(band, band._sub_item), "3 episodes today")  # only the badge
        self.status.publish(downloading(DownloadState.READY))
        self.assertEqual(option(band, band._badge_item, "state"), "hidden")
        self.assertEqual(self.text(band, band._sub_item), "3 episodes today")
        for listener in self.status.space_listeners:
            listener(FreeSpace(free_bytes=12 * 1024**3, on_nas=True, low=True))
        self.assertEqual(self.events, [("report", "⚠ Your NAS is almost full: 12 GB free", 15_000)])
        band.destroy()
        self.assertEqual(self.status.listeners, [])
        self.assertEqual(self.status.space_listeners, [])

    async def test_motion_off_and_reload_keeps_the_title(self) -> None:
        band = self.band(motion=False)
        band.start()
        await self.settle()
        self.assertFalse(band.rotation.motion)
        band.jump(1)
        await self.settle()  # no fade: at once
        self.assertEqual(self.text(band, band._title_item), "The Bear — S03E05 “Ep 1”")
        band.set_motion(True)
        self.assertTrue(band.rotation.motion)
        band._apply(self.service.sides[SpotlightSide.SERIES])  # the same titles again
        self.assertEqual(band.rotation.index, 1)

    async def test_a_reload_bakes_the_title_showing_again(self) -> None:
        self.service.backdrops["The Bear"] = None  # no key yet: the glow
        band = self.band()
        band.start()
        await self.settle()
        glow = band._shown
        self.service.backdrops.pop("The Bear")  # a key now: TMDB has a backdrop
        band.reload()
        band._reload_now()  # as the debounce timer would
        await self.settle(80)
        self.assertIsNot(band._shown, glow)
        self.assertIn("https://backdrops.invalid/The Bear/w1280.jpg", self.images.asked)
        self.assertEqual(self.text(band, band._title_item), "The Bear — S03E05 “Ep 0”")

    async def test_the_name_and_the_logo(self) -> None:
        band = self.band()
        band.start()
        await self.settle()
        layout = band.layout
        assert layout is not None
        # "Grabber" in BRAND; the logo over its shadow, placed so the logo itself sits on its
        # spot.
        self.assertEqual(option(band, band._word_items[1], "fill"), theme.pick(theme.BRAND))
        self.assertGreater(band._logo_pad, 0)
        x, y = band.coords(band._logo_item)
        self.assertEqual(round(x) + band._logo_pad, band._px(layout.logo.x))
        self.assertEqual(round(y) + band._logo_pad, band._px(layout.logo.y))

    async def test_fits_at_1220_in_dutch(self) -> None:
        i18n.set_language(Language.NL)
        band = self.band()
        layout = band.layout
        assert layout is not None
        self.assertAlmostEqual(layout.width, 1220, delta=1)
        parts = controls(layout)
        for index, part in enumerate(parts):
            for other in parts[index + 1 :]:
                self.assertFalse(part.overlaps(other), (part, other))


def item_option(canvas: tk.Canvas, item: int, name: str) -> str:
    widget: Any = canvas
    return str(widget.itemcget(item, name))


class SwitchTests(unittest.TestCase):
    """The Series / Movies switch (ModeSwitch) and the tabs row it sits on."""

    def setUp(self) -> None:
        try:
            self.root = ctk.CTk(fg_color=theme.BG)
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.sides: list[SpotlightSide] = []

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def press(self, switch: ModeSwitch, rect: Rect) -> None:
        scale = switch._scale
        event = SimpleNamespace(x=round((rect.x + rect.w / 2) * scale), y=round((rect.y + rect.h / 2) * scale))
        switch._on_press(event)
        switch._on_release(event)

    def test_its_solid_pill_the_lit_side_and_the_clicks(self) -> None:
        switch = ModeSwitch(self.root, on_side=self.sides.append)
        switch.pack()
        self.root.update()
        layout = switch.layout
        width, height = layout.size
        self.assertEqual(int(switch.cget("width")), switch._px(width))
        self.assertEqual(int(switch.cget("height")), switch._px(height))
        self.assertEqual(str(switch.cget("background")), theme.pick(theme.BG))  # the page's
        # The pill: nearly opaque SURFACE with its shadow around it.
        track = switch._photo_sources["track"]
        self.assertGreater((track.width - switch._px(layout.pill.w)) // 2, 0)
        middle = track.getpixel((track.width // 2, track.height // 2))
        assert isinstance(middle, tuple)
        surface = switch._rgba(theme.SURFACE)
        for channel, value in zip(middle, (*surface[:3], SWITCH_ALPHA), strict=True):
            self.assertLessEqual(abs(channel - value), 2)
        # Series lit (white on the accent), Movies in TEXT.
        active = switch._photo_sources["active"]
        lit = active.getpixel((active.width // 2, active.height // 2))
        assert isinstance(lit, tuple)
        self.assertEqual(lit[:3], switch._rgba(theme.ACCENT)[:3])
        texts = switch._text_items
        self.assertEqual(item_option(switch, texts[SpotlightSide.SERIES], "fill"), theme.pick(theme.ON_COLOR))
        self.assertEqual(item_option(switch, texts[SpotlightSide.MOVIES], "fill"), theme.pick(theme.TEXT))
        self.assertEqual(item_option(switch, texts[SpotlightSide.MOVIES], "text"), "Movies")
        # Hover on the other side: a light fill; not on the lit one.
        self.assertEqual(item_option(switch, switch._hover_item, "state"), "hidden")
        switch._set_hover(SpotlightSide.MOVIES)
        self.assertEqual(item_option(switch, switch._hover_item, "state"), "normal")
        switch._set_hover(SpotlightSide.SERIES)
        self.assertEqual(item_option(switch, switch._hover_item, "state"), "hidden")
        switch._set_hover(None)
        # A click on a part asks for that side; MainWindow lights it.
        series, movies = layout.segments
        self.press(switch, movies)
        self.press(switch, series)
        self.assertEqual(self.sides, [SpotlightSide.MOVIES, SpotlightSide.SERIES])
        switch.select(SpotlightSide.MOVIES)
        self.assertEqual(item_option(switch, texts[SpotlightSide.MOVIES], "fill"), theme.pick(theme.ON_COLOR))
        x, _y = switch.coords(switch._active_item)
        self.assertEqual(round(x), switch._px(movies.x))
        switch.select(None)  # the Downloads page: neither lit
        self.assertEqual(item_option(switch, switch._active_item, "state"), "hidden")

    def test_light_and_dark_repaint(self) -> None:
        switch = ModeSwitch(self.root, on_side=self.sides.append)
        switch.pack()
        self.root.update()
        before = (str(switch.cget("background")), switch._photo_sources["track"])
        ctk.set_appearance_mode("light")
        self.addCleanup(ctk.set_appearance_mode, "dark")
        self.root.update()
        switch._repaint()  # as theme.on_mode_change does
        self.assertEqual(str(switch.cget("background")), theme.pick(theme.BG))
        self.assertNotEqual(str(switch.cget("background")), before[0])
        self.assertIsNot(switch._photo_sources["track"], before[1])
        track = switch._photo_sources["track"]
        middle = track.getpixel((track.width // 2, track.height // 2))
        assert isinstance(middle, tuple)
        self.assertEqual(middle[:3], switch._rgba(theme.SURFACE)[:3])

    def test_in_dutch(self) -> None:
        i18n.set_language(Language.NL)
        switch = ModeSwitch(self.root, on_side=self.sides.append)
        self.assertEqual(item_option(switch, switch._text_items[SpotlightSide.MOVIES], "text"), "Films")

    def tabs_row(self, width: int) -> tuple[ctk.CTkFrame, ModeSwitch, Tabview]:
        """The main window's tabs row at `width` units with the Series tabs on it."""
        scale = ctk.ScalingTracker.get_widget_scaling(self.root)
        self.root.geometry(f"{width}x300")  # CTk scales this
        self.root.grid_columnconfigure(0, weight=1)
        row, switch = build_tabs_row(self.root, on_side=self.sides.append)
        row.grid(row=0, column=0, sticky="ew")
        tabs = Tabview(self.root, look="pills", pills_master=row)
        for name in ("Calendar", "Discover", "Tracked"):
            tabs.add(name)
        tabs.grid(row=1, column=0, sticky="nsew")
        assert tabs.pills is not None
        show_tabs(tabs.pills, True)
        self.root.deiconify()
        self.root.update()
        self.assertAlmostEqual(row.winfo_width(), width * scale, delta=2)
        return row, switch, tabs

    def test_on_the_tabs_row_right_aligned(self) -> None:
        for width in (1650, 1220):
            with self.subTest(width):
                row, switch, tabs = self.tabs_row(width)
                pills = tabs.pills
                assert pills is not None
                scale = switch._scale
                # The pill's right edge PAD_X from the window's edge, level with the band's
                # settings button.
                pill = switch.layout.pill
                pill_right = switch.winfo_x() + switch._px(pill.right)
                self.assertAlmostEqual(pill_right, row.winfo_width() - PAD_X * scale, delta=2)
                # On the same row as the tabs, their middles level; the pill a little taller.
                self.assertEqual(int(switch.grid_info()["row"]), int(pills.grid_info()["row"]))
                self.assertIs(pills.master, row)
                switch_middle = switch.winfo_y() + switch._px(pill.y + pill.h / 2)
                pills_middle = pills.winfo_y() + pills.winfo_height() / 2
                self.assertAlmostEqual(switch_middle, pills_middle, delta=2)
                self.assertGreater(switch._px(pill.h), pills.winfo_height())
                self.assertLess(switch._px(pill.h), 1.7 * pills.winfo_height())
                # Never over the tabs.
                last = max(button.winfo_rootx() + button.winfo_width() for button in pills.buttons.values())
                self.assertLess(last, switch.winfo_rootx() + switch._px(pill.x) - 100)
                # Movies / Downloads: the tabs hidden, the switch stays where it was.
                where = (switch.winfo_rootx(), switch.winfo_rooty(), row.winfo_height())
                show_tabs(pills, False)
                self.root.update()
                self.assertEqual(pills.winfo_manager(), "")
                self.assertEqual((switch.winfo_rootx(), switch.winfo_rooty(), row.winfo_height()), where)
                row.destroy()
                tabs.destroy()


class BrandColourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(theme.apply_accent, Accent.SLATE)

    def test_grabber_reads_on_a_dark_picture_in_every_palette(self) -> None:
        """BRAND: in dark a light tint of the palette's accent, well lighter than LINK; in light
        LINK itself (on the light wash)."""

        def lightness(colour: str) -> float:
            hexed = palettes.to_hex(colour)
            red, green, blue = (int(hexed[i : i + 2], 16) for i in (1, 3, 5))
            return (max(red, green, blue) + min(red, green, blue)) / 510

        self.assertEqual(theme.BRAND, ("#2D6390", "#8FB0D0"))  # Slate: the design's colour
        for accent in Accent:
            theme.apply_accent(accent)
            with self.subTest(accent=accent):
                self.assertEqual(theme.BRAND[0], theme.LINK[0])
                self.assertGreater(lightness(theme.BRAND[1]), 0.6)
                self.assertGreater(lightness(theme.BRAND[1]), lightness(theme.ACCENT[1]) + 0.2)


class UnderlineTabTests(unittest.TestCase):
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

    def test_underline_tabs(self) -> None:
        changes: list[str] = []
        tabs: Tabview | None = None

        def changed() -> None:
            assert tabs is not None
            changes.append(tabs.get())

        tabs = Tabview(self.root, look="underline", command=changed)
        for name in ("Calendar", "Discover", "Tracked"):
            tabs.add(name)
        tabs.pack(fill="both", expand=True)
        self.root.update()
        bar = tabs.underline
        assert bar is not None
        self.assertEqual(bar.names, ("Calendar", "Discover", "Tracked"))
        self.assertEqual(bar.current, "Calendar")
        self.assertEqual(tabs._segmented_button.winfo_manager(), "")  # CTk's buttons hidden
        # Left-aligned: only the spare column after the names stretches.
        self.assertEqual(bar._line.winfo_manager(), "grid")  # the thin line under the names
        self.assertEqual(str(bar._line.cget("background")), theme.pick(theme.PANEL_EDGE))
        weights = [int(bar.grid_columnconfigure(column)["weight"]) for column in range(4)]
        self.assertEqual(weights, [0, 0, 0, 1])
        # Each name's column as wide as the name and the gap after it (the accent line under
        # it no wider).
        scale = ctk.ScalingTracker.get_widget_scaling(bar)
        for column, name in enumerate(bar.names):
            room = bar._labels[name].winfo_reqwidth() + round((UNDERLINE_TAB_GAP + UNDERLINE_LEFT) * scale)
            self.assertLessEqual(bar.grid_bbox(column, 0)[2], room + 2, name)
        bar._on_select("Tracked")  # a click on the name
        self.assertEqual(tabs.get(), "Tracked")
        self.assertEqual(changes, ["Tracked"])
        self.assertEqual(bar.current, "Tracked")
        self.assertNotEqual(bar._marks["Tracked"].cget("fg_color"), "transparent")
        self.assertEqual(bar._marks["Calendar"].cget("fg_color"), "transparent")
        tabs.set("Discover")
        self.assertEqual(bar.current, "Discover")
        self.assertEqual(tabs.tab("Discover").winfo_manager(), "grid")
        self.root.after(150)  # CTk's set() hides the other pages 100 ms later (its own timer)
        self.root.update()
        self.assertEqual(tabs.tab("Tracked").winfo_manager(), "")

    def test_pill_tabs(self) -> None:
        """The main window's look: rounded buttons left-aligned, the chosen one on the accent
        with white text, the others on a faint CHIP fill (CHIP_HOVER under the pointer) in
        BUTTON_TEXT; no line under them."""
        changes: list[str] = []
        tabs: Tabview | None = None

        def changed() -> None:
            assert tabs is not None
            changes.append(tabs.get())

        tabs = Tabview(self.root, look="pills", command=changed)
        for name in ("Calendar", "Discover", "Tracked"):
            tabs.add(name)
        tabs.pack(fill="both", expand=True)
        self.root.update()
        bar = tabs.pills
        assert bar is not None
        self.assertIsNone(tabs.underline)
        self.assertEqual(bar.names, ("Calendar", "Discover", "Tracked"))
        self.assertEqual(bar.current, "Calendar")
        self.assertEqual(tabs._segmented_button.winfo_manager(), "")  # CTk's buttons hidden
        buttons = bar.buttons
        chosen, other = buttons["Calendar"], buttons["Discover"]
        self.assertEqual(chosen.cget("fg_color"), theme.ACCENT)
        self.assertEqual(chosen.cget("text_color"), theme.ON_COLOR)
        self.assertEqual(other.cget("fg_color"), theme.CHIP)
        self.assertEqual(other.cget("hover_color"), theme.CHIP_HOVER)
        self.assertEqual(other.cget("text_color"), theme.BUTTON_TEXT)
        self.assertEqual(chosen.cget("corner_radius"), 9)
        # Left-aligned, one after the other with a small gap.
        scale = ctk.ScalingTracker.get_widget_scaling(bar)
        x = round(PILLS_LEFT * scale)
        for name in bar.names:
            button = buttons[name]
            self.assertLessEqual(abs(button.winfo_x() - x), 2, name)
            x += button.winfo_width() + round(PILLS_GAP * scale)
        weights = [int(bar.grid_columnconfigure(column)["weight"]) for column in range(4)]
        self.assertEqual(weights, [0, 0, 0, 1])
        # Snug around the name: 16 units either side.
        label_width = chosen._text_label.winfo_reqwidth() if chosen._text_label else 0
        self.assertLessEqual(chosen.winfo_reqwidth(), label_width + round(2 * 16 * scale) + 2)
        # … and 6 units above and under it.
        font = theme.font(16, bold=True)
        snug = round((int(font.metrics("linespace")) + 2 * 6) * scale)
        self.assertLessEqual(chosen.winfo_reqheight(), snug + 3)  # (the scaled font rounds up)
        bar._on_select("Tracked")  # a click
        self.assertEqual(tabs.get(), "Tracked")
        self.assertEqual(changes, ["Tracked"])
        self.assertEqual(buttons["Tracked"].cget("fg_color"), theme.ACCENT)
        self.assertEqual(chosen.cget("fg_color"), theme.CHIP)
        tabs.set("Discover")
        self.assertEqual(bar.current, "Discover")
        self.assertEqual(tabs.tab("Discover").winfo_manager(), "grid")
        self.root.after(150)  # CTk's set() hides the other pages 100 ms later (its own timer)
        self.root.update()
        self.assertEqual(tabs.tab("Tracked").winfo_manager(), "")

    def test_segmented_stays_the_default(self) -> None:
        tabs = Tabview(self.root)
        tabs.add("One")
        tabs.pack()
        self.root.update()
        self.assertIsNone(tabs.underline)
        self.assertIsNone(tabs.pills)
        self.assertEqual(tabs._segmented_button.winfo_manager(), "grid")


if __name__ == "__main__":
    unittest.main()
