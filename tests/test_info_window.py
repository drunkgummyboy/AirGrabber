"""The info window (app/ui/title_info.py, movie_details.py, show_details.py): its texts, its
layout and baked images, and the film window on a real (hidden) Tk root with a fake
DiscoveryService (no network: the ImageCache is a fake too)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk
from PIL import Image

from app import i18n
from app.i18n import Language
from app.models import (
    CastMember,
    InfoLayout,
    Movie,
    MovieDetails,
    Show,
    TitleExtras,
    TitleInfo,
    Video,
)
from app.services.tmdb import TMDBError, TMDBNotConfigured
from app.ui.movie_details import (
    MovieDetailsWindow,
    MovieWindows,
    movie_genres,
    movie_meta_line,
    movie_synopsis,
)
from app.ui.show_details import genre_names, synopsis
from app.ui.title_info import (
    HERO_HEIGHT,
    NBSP,
    NO_KEY_HINT,
    POSTER_GAP,
    POSTER_HEIGHT,
    POSTER_WIDTH,
    SIDE_BAND,
    SIDE_LEFT_WIDTH,
    SIDE_OVERLAP,
    SIDE_PAD,
    SIDE_POSTER_GAP,
    SIDE_POSTER_HEIGHT,
    TEXT_RISE,
    CastList,
    FramedPoster,
    backdrop_size,
    compose_hero,
    day_text,
    flow_rows,
    frame_poster,
    hero_face,
    initials,
    join_meta,
    masthead_layout,
    rating_text,
    release_text,
    round_photo,
    trailer_pill,
)

TODAY = date(2026, 10, 26)
FILM = Movie(
    id=4242,
    title="Harbour Lights",
    overview="A harbourmaster finds that someone steers her ships the wrong way.",
    release_date=date(2026, 8, 14),
    poster_path="/madeup_poster.jpg",
    vote_average=7.4,
    vote_count=312,
    genre_ids=(18, 80),
)
INFO = TitleInfo(
    id=4242,
    overview="Wanneer de vuurtoren weer gaat branden…",
    genres=("Drama", "Mysterie"),
    backdrop_path="/madeup_backdrop.jpg",
    runtime=128,
    status="Released",
    vote_average=7.6,
    vote_count=400,
)
TRAILER = Video(key="officialNl4", site="YouTube", type="Trailer", official=True, language="nl")
CAST = (
    CastMember(name="Maren Holloway", character="Ida Marlowe", profile_path="/madeup_1.jpg"),
    CastMember(name="Bram", character=""),
)


class TextTests(unittest.TestCase):
    def test_join_meta_and_rating(self) -> None:
        self.assertEqual(join_meta(["2024", None, "", "★ 6.8"]), "2024 · ★ 6.8")
        self.assertEqual(join_meta([]), "")
        self.assertEqual(rating_text(6.84), "★ 6.8")
        self.assertIsNone(rating_text(None))

    def test_day_text_has_the_year_only_when_another(self) -> None:
        self.assertEqual(day_text(date(2026, 12, 24), TODAY), "24 Dec")
        self.assertEqual(day_text(date(2024, 12, 24), TODAY), "24 Dec 2024")

    def test_release_text(self) -> None:
        out = MovieDetails(id=1, digital_release=date(2026, 9, 1))
        later = MovieDetails(id=1, digital_release=date(2026, 11, 3))
        self.assertEqual(release_text(out, "Released", TODAY), "Out digitally since 1 Sep")
        self.assertEqual(release_text(later, "Released", TODAY), "Out digitally on 3 Nov")
        self.assertEqual(release_text(None, "Post Production", TODAY), "Post-production")
        self.assertIsNone(release_text(None, "Released", TODAY))
        self.assertIsNone(release_text(MovieDetails(id=1), None, TODAY))

    def test_initials(self) -> None:
        self.assertEqual(initials("Maren Holloway"), "MH")
        self.assertEqual(initials("Saskia Delacroix-Ng"), "SN")
        self.assertEqual(initials("Pieter van Loon"), "PL")
        self.assertEqual(initials("Bram"), "B")
        self.assertEqual(initials("  "), "?")

    def test_backdrop_size(self) -> None:
        self.assertEqual(backdrop_size(700), "w780")
        self.assertEqual(backdrop_size(1245), "w1280")
        self.assertEqual(backdrop_size(3000), "w1280")  # never the (huge) original

    def test_flow_rows(self) -> None:
        self.assertEqual(flow_rows([50, 50, 50], 160, 6), [[0, 1], [2]])
        self.assertEqual(flow_rows([50, 50, 50], 200, 6), [[0, 1, 2]])
        self.assertEqual(flow_rows([300, 20], 100, 6), [[0], [1]])  # too wide: alone
        self.assertEqual(flow_rows([], 100, 6), [])


class FilmTextTests(unittest.TestCase):
    def test_meta_line_before_and_after_tmdb(self) -> None:
        self.assertEqual(movie_meta_line(FILM, None, None, TODAY), "2026 · ★ 7.4")
        details = MovieDetails(id=4242, runtime=128, digital_release=date(2026, 10, 1))
        extras = TitleExtras(info=INFO)
        self.assertEqual(
            movie_meta_line(FILM, details, extras, TODAY),
            "2026 · 2h 8m · ★ 7.6 · Out digitally since 1 Oct",
        )
        # Without the details, TMDB's runtime; a film not out yet says so.
        coming = TitleExtras(info=INFO.model_copy(update={"status": "In Production"}))
        self.assertEqual(
            movie_meta_line(FILM, None, coming, TODAY), "2026 · 2h 8m · ★ 7.6 · In production"
        )

    def test_genres_and_synopsis(self) -> None:
        self.assertEqual(movie_genres(FILM, None), ("Drama", "Crime"))  # the card's chips
        self.assertEqual(movie_genres(FILM, TitleExtras(info=INFO)), ("Drama", "Mysterie"))
        self.assertEqual(movie_synopsis(FILM, None), FILM.overview)
        self.assertEqual(movie_synopsis(FILM, TitleExtras(info=INFO)), INFO.overview)
        no_text = TitleExtras(info=INFO.model_copy(update={"overview": ""}))
        self.assertEqual(movie_synopsis(FILM, no_text), FILM.overview)


class SeriesTextTests(unittest.TestCase):
    SHOW = Show(id=1, name="The Quiet Orchard", genres=("Drama", "Science-Fiction", "Kaiju"),
                summary="TVMaze's summary.")

    def test_genres_from_tmdb_else_tvmaze_in_plain_words(self) -> None:
        self.assertEqual(genre_names(self.SHOW, None), ("Drama", "Science fiction", "Kaiju"))
        self.assertEqual(genre_names(self.SHOW, TitleExtras(info=INFO)), ("Drama", "Mysterie"))

    def test_dutch_tvmaze_genres(self) -> None:
        before = i18n.current_language()
        i18n.set_language(Language.NL)
        self.addCleanup(i18n.set_language, before)
        self.assertEqual(genre_names(self.SHOW, None), ("Drama", "Sciencefiction", "Kaiju"))

    def test_synopsis_tmdb_first_then_tvmaze(self) -> None:
        self.assertEqual(synopsis(self.SHOW, TitleExtras(info=INFO)), INFO.overview)
        empty = TitleExtras(info=INFO.model_copy(update={"overview": ""}))
        self.assertEqual(synopsis(self.SHOW, empty), "TVMaze's summary.")
        self.assertEqual(synopsis(self.SHOW, None), "TVMaze's summary.")


class LayoutTests(unittest.TestCase):
    def test_tabs_the_poster_and_the_title_start_level_under_the_backdrop(self) -> None:
        layout = masthead_layout(1245, 1.5, hero=True, layout=InfoLayout.TABS)
        self.assertEqual(layout.hero_height, round(HERO_HEIGHT * 1.5))
        # About 230 units of backdrop above where the text starts.
        self.assertEqual(layout.text[1], layout.hero_height - round(TEXT_RISE * 1.5))
        self.assertEqual(HERO_HEIGHT - TEXT_RISE, 230)
        self.assertEqual(layout.poster[1], layout.text[1])  # the poster's top is the title's
        self.assertEqual(layout.poster_size, (round(POSTER_WIDTH * 1.5), round(POSTER_HEIGHT * 1.5)))
        self.assertEqual(layout.text[0], layout.poster[0] + round((POSTER_WIDTH + POSTER_GAP) * 1.5))
        self.assertEqual(layout.text_width, 1245 - layout.text[0] - layout.poster[0])
        self.assertEqual(layout.pill_centre[0], 1245 // 2)

    def test_without_one_everything_starts_at_the_top(self) -> None:
        layout = masthead_layout(860, 1.0, hero=False)
        self.assertEqual(layout.hero_height, 0)
        self.assertEqual(layout.poster[1], layout.text[1])

    def test_side_the_poster_reaches_into_the_band_and_the_title_starts_under_it(self) -> None:
        layout = masthead_layout(1800, 1.5, hero=True, layout=InfoLayout.SIDE)
        self.assertEqual(layout.hero_height, round(SIDE_BAND * 1.5))
        self.assertEqual(layout.poster, (round(SIDE_PAD * 1.5), layout.hero_height - round(SIDE_OVERLAP * 1.5)))
        poster_bottom = layout.poster[1] + round(SIDE_POSTER_HEIGHT * 1.5)
        self.assertEqual(layout.text, (layout.poster[0], poster_bottom + round(SIDE_POSTER_GAP * 1.5)))
        self.assertEqual(layout.text_width, round(SIDE_LEFT_WIDTH * 1.5) - 2 * round(SIDE_PAD * 1.5))
        self.assertEqual(layout.pill_centre[0], 900)  # centred on the band
        self.assertLess(layout.pill_centre[1], layout.hero_height)
        bare = masthead_layout(1800, 1.5, hero=False, layout=InfoLayout.SIDE)
        self.assertEqual(bare.hero_height, 0)
        self.assertGreater(bare.poster[1], 0)


class ImageTests(unittest.TestCase):
    BG = (22, 22, 22)

    def test_the_hero_fades_into_the_background(self) -> None:
        size = (300, 120)
        picture = Image.new("RGB", size, (200, 40, 40))
        face = hero_face(size, self.BG, 1.0, blur=False)
        hero = compose_hero(picture, face)
        self.assertEqual(hero.size, size)
        self.assertEqual(hero.getpixel((150, 2)), (200, 40, 40))  # untouched at the top
        self.assertEqual(hero.getpixel((150, size[1] - 1)), self.BG)  # solid at the bottom
        self.assertEqual(hero.getpixel((150, face.solid_from)), self.BG)  # where the text starts

    def test_a_poster_standing_in_is_blurred_and_dimmed(self) -> None:
        size = (300, 120)
        picture = Image.new("RGB", size, (200, 40, 40))
        face = hero_face(size, self.BG, 1.0, blur=True)
        self.assertGreater(face.blur, 0)
        top = compose_hero(picture, face).getpixel((150, 2))
        assert isinstance(top, tuple)
        self.assertLess(top[0], 200)  # dimmed towards the background

    def test_the_framed_poster_has_see_through_corners(self) -> None:
        face = FramedPoster(radius=10, border=2, edge=(80, 90, 100))
        framed = frame_poster(Image.new("RGB", (60, 90), (1, 2, 3)), face)
        self.assertEqual(framed.mode, "RGBA")
        self.assertEqual(framed.getpixel((0, 0))[3], 0)  # type: ignore[index]
        self.assertEqual(framed.getpixel((30, 45)), (1, 2, 3, 255))
        self.assertEqual(framed.getpixel((30, 0))[:3], (80, 90, 100))  # type: ignore[index]
        placeholder = frame_poster(None, face, (60, 90), (9, 9, 9))
        self.assertEqual(placeholder.getpixel((30, 45)), (9, 9, 9, 255))

    def test_round_photos(self) -> None:
        portrait = Image.new("RGB", (60, 90), (120, 130, 140))
        photo = round_photo(portrait, 40)
        self.assertEqual((photo.size, photo.mode), ((40, 40), "RGBA"))
        self.assertEqual(photo.getpixel((0, 0))[3], 0)  # type: ignore[index]
        self.assertEqual(photo.getpixel((20, 20))[3], 255)  # type: ignore[index]

    def test_the_trailer_pill(self) -> None:
        pill = trailer_pill((210, 69), 1.5, False)
        self.assertEqual((pill.size, pill.mode), ((210, 69), "RGBA"))
        self.assertEqual(pill.getpixel((0, 0))[3], 0)  # type: ignore[index]
        alpha = pill.getpixel((150, 34))[3]  # type: ignore[index]
        self.assertTrue(100 < alpha < 255)  # see-through dark fill


# -- the film window on Tk ---------------------------------------------------------------------


class FakeImages:
    """An ImageCache that never has a picture (no network); records what was asked."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str | None, size: tuple[int, int], *, finish: Any = None) -> None:
        if url:
            self.urls.append(url)
        return None


class FakeWatchlist:
    def contains(self, movie_id: int) -> bool:
        return False

    def subscribe(self, listener: Any) -> Any:
        return lambda: None


class FakeDiscovery:
    def __init__(self, extras: TitleExtras | Exception, *, key: bool = True) -> None:
        self.extras = extras
        self.has_tmdb_key = key
        self.details = MovieDetails(id=4242, runtime=128, imdb_id="tt9900042")
        self.movies: dict[int, Movie] = {FILM.id: FILM}

    async def movie_details(self, movie_id: int) -> MovieDetails:
        if not self.has_tmdb_key:
            raise TMDBNotConfigured("no key")
        return self.details

    async def movie_extras(self, movie_id: int) -> TitleExtras:
        if isinstance(self.extras, Exception):
            raise self.extras
        return self.extras

    async def movie(self, movie_id: int) -> Movie:
        if movie_id not in self.movies:
            raise TMDBError("not found")
        return self.movies[movie_id]


def labels(widget: Any) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found += labels(child)
    return found


class FilmWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.images = FakeImages()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def windows(self, discovery: FakeDiscovery) -> MovieWindows:
        actions = mock.MagicMock()
        actions.states.get.return_value = None
        windows = MovieWindows(
            self.root,
            images=self.images,  # type: ignore[arg-type]
            discovery=discovery,  # type: ignore[arg-type]
            watchlist=FakeWatchlist(),  # type: ignore[arg-type]
            actions=actions,
        )
        self.addCleanup(windows.close)
        return windows

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0.01)
            self.root.update()

    def window(self, windows: MovieWindows) -> MovieDetailsWindow:
        [window] = windows._live().values()
        return window

    async def test_with_tmdb_extras(self) -> None:
        windows = self.windows(FakeDiscovery(TitleExtras(info=INFO, trailer=TRAILER, cast=CAST)))
        windows.open_movie(FILM)
        await self.settle()
        window = self.window(windows)
        self.assertEqual(window.title(), "Harbour Lights")
        texts = [text.replace(NBSP, " ") for text in labels(window)]  # the meta line's
        self.assertIn("Harbour Lights", texts)
        self.assertIn("2026 · 2h 8m · ★ 7.6", texts)
        self.assertEqual(window.genres.names, ("Drama", "Mysterie"))
        self.assertEqual(window.summary_text, INFO.overview)
        self.assertTrue(window.masthead.hero_shown)
        self.assertEqual(window.masthead.trailer_url, "https://www.youtube.com/watch?v=officialNl4")
        assert isinstance(window.cast, CastList)  # "side", the default
        self.assertEqual([entry.member.name for entry in window.cast.entries], ["Maren Holloway", "Bram"])
        self.assertIn("MH", texts)  # initials until (or without) a photo
        self.assertFalse(window.hint_shown)
        self.assertEqual(window.imdb_url, "https://www.imdb.com/title/tt9900042/")
        # The backdrop and a cast photo were asked for at TMDB's image sizes, once the masthead
        # has a width (the hidden test root may never map the window).
        window.masthead._on_configure(SimpleNamespace(width=1245))
        await asyncio.sleep(0.25)  # the backdrop waits for the resizing to stop
        await self.settle()
        backdrops = [url for url in self.images.urls if url.endswith("/madeup_backdrop.jpg")]
        self.assertTrue(backdrops)
        self.assertTrue(all("/w1280/" in url or "/w780/" in url for url in backdrops))
        self.assertIn("https://image.tmdb.org/t/p/w185/madeup_1.jpg", self.images.urls)

    async def test_one_window_per_film(self) -> None:
        windows = self.windows(FakeDiscovery(TitleExtras(info=INFO)))
        windows.open_movie(FILM)
        windows.open_movie(FILM)
        await self.settle()
        self.assertEqual(len(windows._live()), 1)

    async def test_without_a_trailer_or_cast(self) -> None:
        windows = self.windows(FakeDiscovery(TitleExtras(info=INFO)))
        windows.open_movie(FILM)
        await self.settle()
        window = self.window(windows)
        self.assertIsNone(window.masthead.trailer_url)
        assert window.cast is not None
        self.assertEqual(window.cast.entries, [])
        self.assertFalse(window.cast.winfo_manager())

    async def test_without_a_tmdb_key(self) -> None:
        windows = self.windows(FakeDiscovery(TMDBNotConfigured("no key"), key=False))
        windows.open_movie(FILM)
        await self.settle()
        window = self.window(windows)
        self.assertFalse(window.masthead.hero_shown)  # no backdrop
        self.assertTrue(window.hint_shown)
        self.assertIn(NO_KEY_HINT, labels(window))
        self.assertEqual(window.summary_text, FILM.overview)  # what the card knew
        self.assertEqual(window.genres.names, ("Drama", "Crime"))
        self.assertTrue(window.imdb_url.startswith("https://www.imdb.com/find/?q="))

    async def test_tmdb_failing_keeps_the_cards_data(self) -> None:
        windows = self.windows(FakeDiscovery(TMDBError("TMDB did not answer")))
        with self.assertLogs("app.ui.movie_details", "INFO"):
            windows.open_movie(FILM)
            await self.settle()
        window = self.window(windows)
        self.assertFalse(window.hint_shown)
        self.assertTrue(window.masthead.hero_shown)  # the poster stands in, blurred
        self.assertEqual(window.summary_text, FILM.overview)

    async def test_a_film_known_only_by_its_id(self) -> None:
        discovery = FakeDiscovery(TitleExtras(info=INFO))
        windows = self.windows(discovery)
        self.assertTrue(await windows.open_movie_id(FILM.id))
        await self.settle()
        self.assertEqual(self.window(windows).title(), "Harbour Lights")
        with self.assertLogs("app.ui.movie_details", "INFO"):
            self.assertFalse(await windows.open_movie_id(999))  # TMDB can't: IMDb instead


if __name__ == "__main__":
    unittest.main()
