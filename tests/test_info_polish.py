"""The info window's polish round (app/ui/title_info.py, movie_details.py, show_details.py,
missing_control.py, row_actions.py): the bigger poster (centred in "side", narrowing in "tabs"
where the text needs the room), the taller band's long soft fade, the title / meta line /
genres / actions centred in "side", no ☰ Choose… for a film in "side", and an episode's quiet
marks (a dot, a thin amber line, a grey ✓) with their options under the ⋯ or a right-click.
Hidden Tk root, fake services (tests/test_info_layouts' and tests/test_rows_207's), no
network."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import customtkinter as ctk
from PIL import Image

from app.models import CalendarEntry, Episode, InfoLayout, ShowDetails
from app.ui import theme
from app.ui.download_control import Base, DownloadActions, DownloadStates
from app.ui.missing_control import PopupMenu
from app.ui.row_actions import RowActions
from app.ui.show_details import (
    EPISODE_ROW_HEIGHT,
    ROW_RADIUS,
    ShowDetailsWindow,
    ShowWindows,
    _EpisodeRow,
    stripe_image,
)
from app.ui.title_info import (
    PILL_HEIGHT,
    POSTER_HEIGHT,
    POSTER_MIN_WIDTH,
    POSTER_WIDTH,
    SIDE_BAND,
    SIDE_FADE,
    SIDE_LEFT_WIDTH,
    TABS_TEXT_MIN,
    compose_hero,
    fade_column,
    hero_face,
    masthead_layout,
)
from tests.test_info_layouts import (
    FILM,
    SHOW,
    ShowLibrary,
    WindowDiscovery,
    WindowsTestCase,
    cast_to,
    no_subscribe,
)
from tests.test_info_window import FakeImages
from tests.test_release_panel import no_status
from tests.test_rows_207 import ENTRY, EPISODE, KEY, WidgetTestCase

BG = (22, 22, 22)


# -- geometry and the fade (pure) ---------------------------------------------------------------


class GeometryTests(unittest.TestCase):
    def test_side_the_poster_is_60_percent_bigger_centred_and_high_in_the_band(self) -> None:
        layout = masthead_layout(1800, 1.5, hero=True, layout=InfoLayout.SIDE)
        width, height = layout.poster_size
        self.assertAlmostEqual(width / round(112 * 1.5), 1.6, delta=0.05)  # 2.0.x: 112 wide
        self.assertEqual(height, round(width * 1.5))
        column = round(SIDE_LEFT_WIDTH * 1.5)
        self.assertLessEqual(abs(2 * layout.poster[0] + width - column), 1)  # centred
        self.assertAlmostEqual(SIDE_BAND / 130, 1.3, delta=0.02)  # the band 30 % taller
        self.assertAlmostEqual(layout.poster[1] / layout.hero_height, 0.4, delta=0.01)
        # "▶ Watch trailer" in the picture, above where it has mostly faded.
        pill_bottom = layout.pill_centre[1] + PILL_HEIGHT * 1.5 / 2
        self.assertLess(pill_bottom, layout.hero_height * 0.58)
        self.assertGreater(layout.pill_centre[1] - PILL_HEIGHT * 1.5 / 2, 0)

    def test_side_without_a_backdrop_the_poster_is_still_centred(self) -> None:
        layout = masthead_layout(1800, 1.25, hero=False, layout=InfoLayout.SIDE)
        column = round(SIDE_LEFT_WIDTH * 1.25)
        self.assertLessEqual(abs(2 * layout.poster[0] + layout.poster_size[0] - column), 1)

    def test_tabs_the_poster_is_bigger_and_narrows_when_the_text_needs_the_room(self) -> None:
        scaling = 1.5
        wide = masthead_layout(round(1400 * scaling), scaling, hero=True)
        self.assertEqual(wide.poster_size, (round(POSTER_WIDTH * scaling), round(POSTER_HEIGHT * scaling)))
        self.assertAlmostEqual(POSTER_WIDTH / 176, 1.6, delta=0.01)  # 2.0.x: 176 wide
        for units in (560, 640, 720, 800):
            width = round(units * scaling)
            for hero in (True, False):
                with self.subTest(units=units, hero=hero):
                    layout = masthead_layout(width, scaling, hero=hero)
                    poster_w, poster_h = layout.poster_size
                    self.assertLessEqual(poster_w, wide.poster_size[0])
                    self.assertGreaterEqual(poster_w, round(POSTER_MIN_WIDTH * scaling))
                    self.assertLessEqual(abs(poster_h - poster_w * 1.5), 1)
                    self.assertEqual(layout.text[0], layout.poster[0] + poster_w + round(24 * scaling))
                    if poster_w > round(POSTER_MIN_WIDTH * scaling):  # shrunk for the text
                        self.assertGreaterEqual(layout.text_width, round(TABS_TEXT_MIN * scaling) - 1)
                    self.assertLessEqual(layout.text[0] + layout.text_width, width)  # no overflow
        narrow = masthead_layout(round(640 * scaling), scaling, hero=True)
        self.assertLess(narrow.poster_size[0], wide.poster_size[0])


class FadeTests(unittest.TestCase):
    def test_the_side_fade_follows_its_stops_smoothly(self) -> None:
        height = 255
        stops = tuple((round((height - 1) * row), share) for row, share in SIDE_FADE)
        values = list(fade_column(height, stops))
        self.assertEqual(values, sorted(values))  # it only ever fades further
        self.assertEqual(values[-1], 255)  # all background on the last row
        for row, share in stops:
            with self.subTest(row=row):
                self.assertAlmostEqual(values[min(row, height - 1)] / 255, share, delta=0.03)
        steps = [after - before for before, after in zip(values, values[1:])]
        self.assertLessEqual(max(steps), 4)  # no visible line anywhere
        self.assertLessEqual(steps[-1], 1)  # it eases into the background

    def test_two_stops_are_the_smoothstep_it_always_was(self) -> None:
        def smooth(x: float) -> float:
            x = min(1.0, max(0.0, x))
            return x * x * (3 - 2 * x)

        fade_from, solid_from = 36, 104
        expected = bytes(
            255 if row >= solid_from
            else round(255 * smooth((row + 0.5 - fade_from) / (solid_from - fade_from)))
            if row >= fade_from
            else 0
            for row in range(120)
        )
        self.assertEqual(fade_column(120, ((fade_from, 0.0), (solid_from, 1.0))), expected)

    def test_the_side_band_fades_all_the_way_down_tabs_as_before(self) -> None:
        size = (300, round(SIDE_BAND * 1.5))
        picture = Image.new("RGB", size, (200, 40, 40))
        face = hero_face(size, BG, 1.5, blur=False, layout=InfoLayout.SIDE)
        hero = compose_hero(picture, face)
        self.assertEqual(hero.getpixel((150, size[1] - 1)), BG)  # where the content starts
        top = hero.getpixel((150, 0))
        assert isinstance(top, tuple)
        self.assertAlmostEqual(top[0], 200 - 0.05 * (200 - 22), delta=2)  # 5 % background
        middle = hero.getpixel((150, round((size[1] - 1) * 0.58)))
        assert isinstance(middle, tuple)
        self.assertAlmostEqual(middle[0], 200 - 0.70 * (200 - 22), delta=6)  # 70 % background
        tabs = hero_face((300, 369), BG, 1.5, blur=False)
        self.assertEqual(tabs.stops, ())  # "tabs" keeps its fade


# -- the windows ------------------------------------------------------------------------------


class SideWindowTests(WindowsTestCase):
    async def test_a_films_side_window_has_auto_but_no_choose(self) -> None:
        _shows, movies, _search = self.make(InfoLayout.SIDE)
        movies.open_movie(FILM)
        await self.settle()
        download = self.film_window(movies).download
        self.assertTrue(download.auto.winfo_manager())
        self.assertFalse(download.choose.winfo_manager())

    async def test_in_tabs_choose_stays(self) -> None:
        _shows, movies, _search = self.make(InfoLayout.TABS)
        movies.open_movie(FILM)
        await self.settle()
        download = self.film_window(movies).download
        self.assertTrue(download.auto.winfo_manager())
        self.assertTrue(download.choose.winfo_manager())

    async def test_side_centres_the_poster_title_meta_genres_and_actions(self) -> None:
        _shows, movies, _search = self.make(InfoLayout.SIDE)
        movies.open_movie(FILM)
        await self.settle()
        window = self.film_window(movies)
        window.geometry("1100x700")
        window.set_genres(("Drama", "Mystery", "Science Fiction", "Thriller"))
        await self.settle()
        window.update_idletasks()
        for widget in (window._title_label, window._meta, window.genres, window.buttons):
            with self.subTest(widget=type(widget).__name__):
                self.assertEqual(widget.grid_info()["sticky"], "ew")
        self.assertEqual(str(window._title_label._label.cget("justify")), "center")
        # The synopsis stays left-aligned.
        self.assertEqual(window._summary.grid_info()["sticky"], "w")
        self.assertEqual(str(window._summary._label.cget("justify")), "left")
        # The poster's centre is the text column's.
        masthead = window.masthead
        x, _y = masthead.coords(masthead._poster_item)
        poster_centre = masthead.winfo_rootx() + x + masthead.poster_size[0] / 2
        info = window._info
        info_centre = info.winfo_rootx() + info.winfo_width() / 2
        self.assertLessEqual(abs(poster_centre - info_centre), 4)
        # Each row of the actions is centred in it.
        row = window.buttons._rows[0]
        row_centre = row.winfo_rootx() + row.winfo_width() / 2
        self.assertLessEqual(abs(row_centre - info_centre), 2)

    async def test_tabs_the_poster_is_baked_again_when_it_narrows(self) -> None:
        _shows, movies, _search = self.make(InfoLayout.TABS)
        movies.open_movie(FILM)
        await self.settle()
        window = self.film_window(movies)
        window.geometry("1300x700")
        await self.settle()
        wide = window.masthead.poster_size
        window.geometry("620x700")
        await self.settle()
        narrow = window.masthead.poster_size
        self.assertLess(narrow[0], wide[0])
        await asyncio.sleep(0.3)  # baked again once the resizing settles
        for _ in range(3):
            await self.settle()
        photo = window.masthead._poster_photo
        assert photo is not None
        self.assertEqual((photo.width(), photo.height()), narrow)


# -- an episode's marks -------------------------------------------------------------------------


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class EpisodeMarkTests(WidgetTestCase):
    """With Plex (the window passes the marks): an episode's quiet mark, no options."""

    def row(self, **kwargs: Any) -> _EpisodeRow:
        actions = DownloadActions[CalendarEntry](
            download=lambda item: None, choose=lambda item: None, states=DownloadStates()
        )
        row = _EpisodeRow(
            self.root,
            ENTRY,
            state=kwargs.pop("state", "downloadable"),
            now=NOW,
            actions=actions,
            **kwargs,
        )
        row.pack(fill="x")
        return row

    async def test_missing_a_dot_and_an_amber_line(self) -> None:
        row = self.row(mark="missing", marks_shown=True)
        await self.settle()
        assert row._mark_slot is not None and row._stripe is not None
        [label] = row._mark_slot.winfo_children()
        self.assertEqual(str(label.cget("text")), "Missing")
        self.assertIsNotNone(label.cget("image"))  # the dot
        self.assertNotIsInstance(label, ctk.CTkButton)  # a plain mark, no pill around it
        self.assertEqual(label.cget("text_color"), theme.WARNING)
        self.assertEqual(row._stripe.winfo_x(), 0)  # along the left edge
        self.assertEqual(row._stripe.winfo_height(), row.winfo_height())
        self.assertFalse(hasattr(row, "more"))  # no ⋯: no marks of your own (3.1.0)

    async def test_in_plex_is_a_quiet_check_new_is_blue_without_the_line(self) -> None:
        row = self.row(mark="plex", marks_shown=True)
        await self.settle()
        assert row._mark_slot is not None
        [label] = row._mark_slot.winfo_children()
        self.assertEqual(str(label.cget("text")), "✓")
        self.assertIsNone(label.cget("image"))
        self.assertEqual(label.cget("text_color"), theme.TEXT_MUTED)
        self.assertIsNone(row._stripe)
        later = EPISODE.model_copy(update={"airstamp": NOW - timedelta(hours=2)})
        row.set_state(ENTRY.model_copy(update={"episode": later}), "downloadable", NOW, mark="new", marks_shown=True)
        await self.settle()
        [label] = row._mark_slot.winfo_children()
        self.assertEqual(str(label.cget("text")), "New · aired today")
        self.assertIsNotNone(label.cget("image"))
        self.assertEqual(label.cget("text_color"), theme.LINK)
        self.assertIsNone(row._stripe)

    async def test_rows_without_a_mark_keep_its_room(self) -> None:
        marked = self.row(mark="missing", marks_shown=True, slot_width=220)
        plain = self.row(mark=None, marks_shown=True, slot_width=220)
        later = EPISODE.model_copy(update={"airstamp": NOW + timedelta(days=3)})
        upcoming = self.row(mark=None, marks_shown=True, state="upcoming", slot_width=220)
        upcoming.set_state(ENTRY.model_copy(update={"episode": later}), "upcoming", NOW, marks_shown=True)
        await self.settle()
        xs = {row._slot.winfo_rootx() for row in (marked, plain, upcoming)}
        self.assertEqual(len(xs), 1)  # the rows stay aligned

    async def test_without_plex_no_mark_slot_at_all(self) -> None:
        row = self.row()  # the window shows no marks without Plex
        await self.settle()
        self.assertIsNone(row._mark_slot)
        self.assertIsNone(row._stripe)

    async def test_a_marked_row_turning_plain_loses_its_line(self) -> None:
        row = self.row(mark="missing", marks_shown=True)
        await self.settle()
        row.set_state(ENTRY, "downloadable", NOW, mark=None, marks_shown=True)
        await self.settle()
        self.assertIsNone(row._stripe)

    def test_the_stripe_image(self) -> None:
        image = stripe_image((12, 78), 12, 4, line=(200, 150, 0), fill=(36, 36, 36), outside=BG)
        self.assertEqual(image.size, (12, 78))
        self.assertEqual(image.getpixel((0, 0)), BG)  # outside the rounded corner
        self.assertEqual(image.getpixel((1, 39)), (200, 150, 0))  # the line
        self.assertEqual(image.getpixel((8, 39)), (36, 36, 36))  # the row after it
        self.assertEqual(image.getpixel((2, 2)), BG)  # the corner curves
        self.assertEqual(image.getpixel((1, 77)), BG)
        self.assertEqual(ROW_RADIUS * 1.5, 12)
        self.assertEqual(round(EPISODE_ROW_HEIGHT * 1.5), 78)


class RowActionsTests(WidgetTestCase):
    async def test_without_choose_only_auto_shows(self) -> None:
        actions = DownloadActions[CalendarEntry](
            download=lambda item: None, choose=lambda item: None, states=DownloadStates()
        )
        row = RowActions(
            self.root, item=ENTRY, key=KEY, base=Base("download"), actions=actions, align="left",
            choose=False,
        )
        row.pack(side="left")
        await self.settle()
        self.assertTrue(row.auto.winfo_manager())
        self.assertFalse(row.choose.winfo_manager())
        self.assertIsNone(PopupMenu._open)


# -- the show window's ⋯ menu -------------------------------------------------------------------


def marked_episode(number: int, *, hours_ago: float) -> Episode:
    airs = datetime.now(UTC) - timedelta(hours=hours_ago)
    return Episode(
        id=300 + number, show_id=SHOW.id, season=3, number=number, name=f"Part {number}",
        airstamp=airs, air_date=airs.date(),
    )


MARKED = (
    marked_episode(1, hours_ago=24 * 9),  # downloaded
    marked_episode(2, hours_ago=24 * 6),  # missing
    marked_episode(3, hours_ago=24 * 3),  # in Plex
    marked_episode(4, hours_ago=3),  # new
)


class MarkedLibrary(ShowLibrary):
    plex = True

    async def show_details(self, show_id: int, *, profile_id: int) -> ShowDetails:
        ids = [episode.id for episode in MARKED]
        if not self.plex:
            return ShowDetails(
                show=SHOW, episodes=MARKED, tracked=True, downloaded=frozenset({ids[0]})
            )
        return ShowDetails(
            show=SHOW,
            episodes=MARKED,
            tracked=True,
            plex_connected=True,
            latest_season=3,
            downloaded=frozenset({ids[0]}),
            missing=frozenset({ids[1]}),
            in_plex=frozenset({ids[2]}),
            new=frozenset({ids[3]}),
        )


class ShowWindowMarkTests(WindowsTestCase):
    async def open_marked(self, library: MarkedLibrary) -> ShowDetailsWindow:
        shows = ShowWindows(
            self.root,
            library=cast_to(Any, library),
            discovery=cast_to(Any, WindowDiscovery()),
            images=cast(Any, FakeImages()),
            status=no_status,
            episode_actions=DownloadActions[CalendarEntry](
                download=lambda entry: None, choose=lambda entry: None, states=DownloadStates()
            ),
            downloads_changed=no_subscribe,
            profile=lambda: 1,
        )
        self.addCleanup(shows.close)
        shows.open_show(SHOW)
        for _ in range(4):
            await self.settle()
        window = shows._live().get(SHOW.id)
        assert isinstance(window, ShowDetailsWindow)
        return window

    async def test_with_plex_the_marks_and_no_options(self) -> None:
        window = await self.open_marked(MarkedLibrary())
        rows = [window._rows[episode.id] for episode in MARKED]
        # The downloaded one gets no ✓: only Plex counts as had.
        self.assertEqual([row.mark for row in rows], [None, "missing", "plex", "new"])
        self.assertEqual([row._stripe is not None for row in rows], [False, True, False, False])
        rows[2].event_generate("<Button-3>", x=5, y=5)  # a right-click opens nothing
        self.assertIsNone(PopupMenu._open)
        assert window._missing_label is not None
        self.assertEqual(window._missing_label.cget("text"), "1 missing")

    async def test_without_plex_no_marks_at_all(self) -> None:
        library = MarkedLibrary()
        library.plex = False
        window = await self.open_marked(library)
        rows = [window._rows[episode.id] for episode in MARKED]
        self.assertEqual([row.mark for row in rows], [None] * 4)
        self.assertTrue(all(row._mark_slot is None and row._stripe is None for row in rows))
        self.assertIsNone(window._missing_label)
        # The first was grabbed: its controls look like any other (no ✓ Downloaded).
        kinds = [row.row_actions.look.kind for row in rows if row.row_actions and row.row_actions.look]
        self.assertEqual(kinds, ["download"] * 4)


if __name__ == "__main__":
    unittest.main()
