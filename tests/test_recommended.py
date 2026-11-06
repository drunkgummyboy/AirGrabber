"""The release list's ★ Recommended row and speed dots: picking.speed (the thresholds),
picking.recommend (⬇ Auto's very pick, with its reasons), the reason line (EN + NL), the row
pictures, and the panel on a hidden Tk root: the mark and the bar while results stream in, per
version and sort, its Download button, the legend; the same pick as QuickDownloadService."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest import mock

from PIL import Image

from app import i18n
from app.i18n import Language
from app.models import (
    GrabOutcome,
    GrabResult,
    GrabTarget,
    MediaType,
    Movie,
    QualityProfile,
    SearchRequest,
    SearchUpdate,
    Settings,
    Show,
    TorrentRelease,
)
from app.services import DownloadService, SettingsService, TorrentSearchService
from app.services.picking import (
    FAST_SEEDERS,
    STALL_SEEDERS,
    Recommendation,
    SeedNote,
    SizeNote,
    Speed,
    pick_best,
    recommend,
    speed,
)
from app.services.quick_download import QuickDownloadService
from app.services.search import episode_request, movie_request
from app.ui import theme
from app.ui.release_panel import (
    NO_SELECTION_TEXT,
    IndexerTarget,
    MovieTarget,
    ReleasePanel,
    TvTarget,
    picture_key,
    recommendation_reason,
    recommended_tint,
    release_id,
    rgb,
    row_picture,
    speed_color,
    tag_look,
    version_text,
)
from tests.test_release_panel import (
    FakeDiscovery,
    FakeDownloads,
    FakeLibrary,
    FakeSearch,
    TkTestCase,
    no_status,
    request_key,
)
from tests.test_theme import contrast

GIB = 1024**3
_counter = iter(range(1, 1_000_000))

FILM = Movie(id=7, title="Dune Part Two")
SHOW = Show(id=1, name="Severance")
MOVIE = movie_request(FILM)
EPISODE = episode_request(SHOW, 2, 1)
DEFAULTS = Settings()  # 1080p for both kinds, no size ranges


def rel(title: str, *, seeders: int = 10, gb: float = 2.0, **fields: Any) -> TorrentRelease:
    """A release with its own info hash."""
    return TorrentRelease(
        title=title,
        source=fields.pop("source", "apibay"),
        seeders=seeders,
        size_bytes=round(gb * GIB),
        info_hash=f"{next(_counter):040x}",
        **fields,
    )


def note(
    release: TorrentRelease,
    *,
    seeds: SeedNote | None = SeedNote.LOTS,
    size: SizeNote | None = SizeNote.NORMAL,
    preferred: bool = False,
    pack: bool = False,
) -> Recommendation:
    return Recommendation(release=release, seeds=seeds, size=size, preferred=preferred, pack=pack)


# -- the thresholds ---------------------------------------------------------------------------


class SpeedTests(unittest.TestCase):
    def test_the_thresholds(self) -> None:
        self.assertEqual((FAST_SEEDERS, STALL_SEEDERS), (20, 2))
        for seeders, level in (
            (1, Speed.STALL),
            (2, Speed.STALL),
            (3, Speed.SLOWER),
            (19, Speed.SLOWER),
            (20, Speed.FAST),
            (979, Speed.FAST),
        ):
            with self.subTest(seeders=seeders):
                self.assertIs(speed(rel("Dune.Part.Two.2024.1080p", seeders=seeders)), level)

    def test_no_count_is_unknown(self) -> None:
        # The indexers drop the releases nobody seeds: a 0 is a source that didn't say.
        self.assertIs(speed(rel("Dune.Part.Two.2024.1080p", seeders=0)), Speed.UNKNOWN)

    def test_dot_colours(self) -> None:
        self.assertEqual(speed_color(Speed.FAST), theme.SUCCESS)
        self.assertEqual(speed_color(Speed.SLOWER), theme.WARNING)
        self.assertEqual(speed_color(Speed.STALL), theme.DANGER)
        self.assertEqual(speed_color(Speed.UNKNOWN), theme.TEXT_FAINT)


# -- recommend: the pick and its reasons --------------------------------------------------------


class RecommendPickTests(unittest.TestCase):
    """`recommend` picks exactly what `pick_best` (⬇ Auto) picks."""

    CASES: tuple[tuple[str, list[TorrentRelease], SearchRequest, Settings], ...] = (
        (
            "quality, then seeders",
            [
                rel("Dune.Part.Two.2024.2160p.WEB", seeders=900, gb=20),
                rel("Dune.Part.Two.2024.1080p.WEB-A", seeders=40),
                rel("Dune.Part.Two.2024.1080p.WEB-B", seeders=300),
            ],
            MOVIE,
            DEFAULTS,
        ),
        (
            "fakes and pre-release copies out",
            [
                rel("Dune.Part.Two.2024.1080p.WEB.exe", seeders=5000),
                rel("Dune.Part.Two.2024.1080p.HDTS", seeders=4000),
                rel("Dune.Part.Two.2024.1080p.WEB", seeders=10, gb=0.01),
                rel("Dune.Part.Two.2024.1080p.BluRay", seeders=12),
            ],
            MOVIE,
            DEFAULTS,
        ),
        (
            "the size range",
            [
                rel("Dune.Part.Two.2024.1080p.Remux", seeders=800, gb=30),
                rel("Dune.Part.Two.2024.1080p.WEB", seeders=50, gb=6),
            ],
            MOVIE,
            Settings(size_movie_min_gb=2, size_movie_max_gb=10),
        ),
        (
            "a preferred group",
            [
                rel("Severance.S02E01.1080p.WEB-ELiTE", seeders=900),
                rel("Severance.S02E01.1080p.WEB-FLUX", seeders=30),
            ],
            EPISODE,
            Settings(preferred_groups=("FLUX",)),
        ),
        (
            "a single episode before a pack",
            [
                rel("Severance.S02E01-E03.1080p.WEB", seeders=900, gb=6),
                rel("Severance.S02E01.1080p.WEB", seeders=8),
            ],
            EPISODE,
            DEFAULTS,
        ),
    )

    def test_the_same_pick_as_pick_best(self) -> None:
        for name, releases, request, settings in self.CASES:
            with self.subTest(name):
                picked = pick_best(releases, request, settings)
                recommendation = recommend(releases, request, settings)
                self.assertIsNotNone(picked)
                assert recommendation is not None
                self.assertIs(recommendation.release, picked)

    def test_nothing_to_pick(self) -> None:
        self.assertIsNone(recommend([], MOVIE, DEFAULTS))
        fakes = [rel("Dune.Part.Two.2024.1080p.WEB.exe"), rel("Another.Film.2024.1080p")]
        self.assertIsNone(recommend(fakes, MOVIE, DEFAULTS))


class SeedNoteTests(unittest.TestCase):
    def seeds(self, *counts: int) -> SeedNote | None:
        """The note for the first release's count among releases of these counts (the first is
        always picked: the 1080p one, the others 720p)."""
        first = rel("Dune.Part.Two.2024.1080p.WEB", seeders=counts[0])
        others = [
            rel(f"Dune.Part.Two.2024.720p.WEB-{i}", seeders=n) for i, n in enumerate(counts[1:])
        ]
        recommendation = recommend([first, *others], MOVIE, DEFAULTS)
        assert recommendation is not None and recommendation.release is first
        return recommendation.seeds

    def test_most_lots_few(self) -> None:
        self.assertIs(self.seeds(447, 979), SeedNote.LOTS)
        self.assertIs(self.seeds(447, 200, 12), SeedNote.MOST)
        self.assertIs(self.seeds(12, 5), SeedNote.MOST)  # slower, but more than the others
        self.assertIs(self.seeds(12, 40), SeedNote.FEW)
        self.assertIs(self.seeds(400), SeedNote.LOTS)  # alone: nothing to have the most of

    def test_a_stall_count_is_few_even_when_it_is_the_most(self) -> None:
        self.assertIs(self.seeds(2, 1), SeedNote.FEW)

    def test_no_count_says_nothing(self) -> None:
        self.assertIsNone(self.seeds(0))


class SizeNoteTests(unittest.TestCase):
    def size(self, gb: float, *others: float, settings: Settings = DEFAULTS) -> SizeNote | None:
        first = rel("Dune.Part.Two.2024.1080p.WEB", seeders=500, gb=gb)
        rest = [rel(f"Dune.Part.Two.2024.720p.WEB-{i}", gb=size) for i, size in enumerate(others)]
        recommendation = recommend([first, *rest], MOVIE, settings)
        assert recommendation is not None and recommendation.release is first
        return recommendation.size

    def test_against_the_others_without_a_range(self) -> None:
        self.assertIs(self.size(12.91, 18.09, 17.53, 13.98, 4.23, 2.85), SizeNote.NORMAL)
        self.assertIs(self.size(2, 8, 9, 10), SizeNote.SMALL)
        self.assertIs(self.size(30, 8, 9, 10), SizeNote.LARGE)
        self.assertIsNone(self.size(8))  # nothing to compare with
        self.assertIs(self.size(0, 8), SizeNote.UNKNOWN)

    def test_against_the_size_range(self) -> None:
        settings = Settings(size_movie_min_gb=4, size_movie_max_gb=10)
        self.assertIs(self.size(6, 30, settings=settings), SizeNote.RIGHT)
        self.assertIs(self.size(2.5, settings=settings), SizeNote.BELOW_RANGE)
        self.assertIs(self.size(14, settings=settings), SizeNote.ABOVE_RANGE)
        self.assertIs(self.size(0, settings=settings), SizeNote.UNKNOWN)


class OtherNoteTests(unittest.TestCase):
    def test_preferred_group(self) -> None:
        releases = [rel("Severance.S02E01.1080p.WEB-FLUX", seeders=30)]
        recommendation = recommend(releases, EPISODE, Settings(preferred_groups=("flux",)))
        assert recommendation is not None
        self.assertTrue(recommendation.preferred)
        recommendation = recommend(releases, EPISODE, DEFAULTS)
        assert recommendation is not None
        self.assertFalse(recommendation.preferred)

    def test_a_pack_for_an_episode(self) -> None:
        pack = rel("Severance.S02E01-E03.1080p.WEB", gb=6)
        recommendation = recommend([pack], EPISODE, DEFAULTS)
        assert recommendation is not None
        self.assertTrue(recommendation.pack)
        single = recommend([rel("Severance.S02E01.1080p.WEB")], EPISODE, DEFAULTS)
        assert single is not None
        self.assertFalse(single.pack)


# -- the reason line --------------------------------------------------------------------------


class ReasonTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    def test_versions(self) -> None:
        self.assertEqual(version_text(rel("Dune.Part.Two.2024.2160p.WEB")), "4K")
        self.assertEqual(version_text(rel("Dune.Part.Two.2024.1080p.WEB")), "1080p")
        self.assertEqual(version_text(rel("Dune.Part.Two.2024.720p.x265")), "720p x265")
        self.assertEqual(version_text(rel("Dune.Part.Two.2024.HEVC")), "x265")
        self.assertEqual(version_text(rel("Dune.Part.Two.2024.WEB")), "")

    def test_the_mock_ups_examples(self) -> None:
        uhd = rel("The Dog Stars 2026 SDR 2160p WEB-POKE")
        self.assertEqual(
            recommendation_reason(note(uhd, seeds=SeedNote.LOTS, size=SizeNote.NORMAL)),
            "4K, lots of seeders, normal size",
        )
        full_hd = rel("Severance.S02E01.1080p.WEB")
        self.assertEqual(
            recommendation_reason(note(full_hd, seeds=SeedNote.MOST, size=SizeNote.RIGHT)),
            "1080p, most seeders, right size",
        )

    def test_the_other_notes_and_what_nobody_knows(self) -> None:
        hevc = rel("Severance.S02E01-E03.720p.x265-FLUX")
        self.assertEqual(
            recommendation_reason(
                note(hevc, seeds=SeedNote.FEW, size=SizeNote.UNKNOWN, preferred=True, pack=True)
            ),
            "720p x265, few seeders, size unknown, your preferred group, a pack with this episode",
        )
        self.assertEqual(
            recommendation_reason(note(rel("Dune.Part.Two.2024.WEB"), seeds=None, size=None)), ""
        )
        self.assertEqual(
            recommendation_reason(note(rel("X.1080p"), seeds=None, size=SizeNote.ABOVE_RANGE)),
            "1080p, bigger than your size range",
        )

    def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        uhd = rel("The Dog Stars 2026 SDR 2160p WEB-POKE")
        self.assertEqual(
            recommendation_reason(note(uhd, seeds=SeedNote.LOTS, size=SizeNote.NORMAL)),
            "4K, veel seeders, normale grootte",
        )
        self.assertEqual(tag_look(dark=True).text, "Aanbevolen")


# -- the row pictures ---------------------------------------------------------------------------


def pixel(picture: Image.Image, x: int, y: int) -> tuple[int, ...]:
    return cast(tuple[int, ...], picture.getpixel((x, y)))


class PictureTests(unittest.TestCase):
    def test_keys(self) -> None:
        self.assertEqual(picture_key(Speed.FAST, recommended=False), "fast")
        self.assertEqual(picture_key(Speed.STALL, recommended=True), "stall+tag")

    def test_a_dot_at_physical_size(self) -> None:
        picture = row_picture((47, 165, 114), 1.5)
        self.assertEqual(picture.size, (21, 27))  # 12 px dot + 9 px room, the tag's height
        self.assertEqual(pixel(picture, 6, 13), (47, 165, 114, 255))  # the dot's middle
        self.assertEqual(pixel(picture, 0, 0)[3], 0)  # transparent around it
        self.assertEqual(pixel(picture, 18, 13)[3], 0)

    def test_the_tag_follows_the_dot(self) -> None:
        look = tag_look(dark=True)
        picture = row_picture((47, 165, 114), 1.5, look)
        plain = row_picture((47, 165, 114), 1.5)
        self.assertEqual(picture.height, plain.height)
        self.assertGreater(picture.width, plain.width + 60)
        fill = pixel(picture, plain.width + 4, 13)  # inside the pill, before the ★
        for got, wanted in zip(fill, look.fill, strict=True):  # see-through: the row shows
            self.assertAlmostEqual(got, wanted, delta=3)

    def test_gold_reads_on_its_tag_in_both_looks(self) -> None:
        for dark in (False, True):
            with self.subTest(dark=dark):
                look = tag_look(dark=dark)
                row = rgb(recommended_tint(dark=dark))
                share = look.fill[3] / 255
                under = tuple(round(f * share + r * (1 - share)) for f, r in zip(look.fill, row))
                text = "#%02X%02X%02X" % look.color
                self.assertGreaterEqual(contrast(text, "#%02X%02X%02X" % under), 4.5)

    def test_the_rows_tint(self) -> None:
        # A little of the accent in the table's background, more in dark.
        self.assertEqual(recommended_tint(dark=True), "#2B3844")
        self.assertEqual(recommended_tint(dark=False), "#EAEDF0")


# -- the panel ----------------------------------------------------------------------------------


class StreamSearch(FakeSearch):
    """Hands out the updates put on `updates` one at a time; None ends the search."""

    def __init__(self) -> None:
        super().__init__()
        self.updates: asyncio.Queue[SearchUpdate | None] = asyncio.Queue()

    async def search(self, request: object) -> AsyncIterator[SearchUpdate]:
        self.requests.append(request_key(cast(SearchRequest, request)))
        while (update := await self.updates.get()) is not None:
            yield update


class Prefs:
    def __init__(self, settings: Settings = DEFAULTS) -> None:
        self.current = settings


FILM_RELEASES = (
    rel("Dune.Part.Two.2024.2160p.WEB-DL.H265-BTM", seeders=979, gb=18.09),
    rel("Dune.Part.Two.2024.2160p.WEB.H265-POKE", seeders=447, gb=12.91),
    rel("Dune.Part.Two.2024.2160p.HDR.WEB.H265.mkv", seeders=5, gb=17.53),
    rel("Dune.Part.Two.2024.1080p.WEB.H264-ETHEL", seeders=44, gb=6.1),
    rel("Dune.Part.Two.2024.1080p.WEB.H264-FLUX", seeders=150, gb=7.0),
    rel("Dune.Part.Two.2024.1080p.BluRay.x264-SPARKS", seeders=2, gb=11.0),
    rel("Dune.Part.Two.2024.1080p.WEB.exe", seeders=5000, gb=0.01),  # a fake: never picked
    rel("Dune.Part.Two.2024.720p.WEB-NOGRP", seeders=0, gb=3.0),  # a source without a count
)


def picture_of(panel: ReleasePanel, release: TorrentRelease) -> str:
    """The key of the picture the release's row shows."""
    state: Any = panel
    tree = state._tree
    name = str(tree.tk.splitlist(tree.item(release_id(release), "image"))[0])
    keys: list[str] = [key for key, photo in state._pictures.items() if str(photo) == name]
    assert len(keys) == 1, keys
    return keys[0]


class PanelTests(TkTestCase):
    def panel(
        self,
        target: IndexerTarget,
        search: FakeSearch,
        *,
        settings: Settings = DEFAULTS,
        downloads: FakeDownloads | None = None,
    ) -> ReleasePanel:
        panel = ReleasePanel(
            self.root,
            target=target,
            search=cast(TorrentSearchService, search),
            downloads=cast(DownloadService, downloads or FakeDownloads()),
            library=cast(Any, FakeLibrary()),
            discovery=cast(Any, FakeDiscovery()),
            settings=cast(SettingsService, Prefs(settings)),
            status=no_status,
        )
        panel.pack(fill="both", expand=True)
        self.addCleanup(self.destroy_widget, panel)
        return panel

    @staticmethod
    def marked(panel: ReleasePanel) -> list[str]:
        """The ids of the rows tagged recommended."""
        tree = cast(Any, panel)._tree
        return [iid for iid in tree.get_children() if "recommended" in tree.item(iid, "tags")]

    async def test_the_same_pick_as_auto(self) -> None:
        """The film's ★ row is the release QuickDownloadService (⬇ Auto) grabs."""
        for profile in (QualityProfile.FULL_HD, QualityProfile.UHD):
            with self.subTest(profile=profile):
                settings = Settings(quality_movie=profile)
                panel = self.panel(MovieTarget(FILM), FakeSearch(FILM_RELEASES), settings=settings)
                panel.start()
                await self.settle()

                grabber = mock.Mock(spec=DownloadService)
                grabber.has_grabbed = mock.AsyncMock(return_value=False)
                grabber.grab = mock.AsyncMock(
                    return_value=GrabResult(outcome=GrabOutcome.SAVED, message="Saved")
                )
                auto = QuickDownloadService(
                    cast(TorrentSearchService, FakeSearch(FILM_RELEASES)),
                    grabber,
                    cast(SettingsService, Prefs(settings)),
                )
                target = GrabTarget(media_type=MediaType.MOVIE, media_id=FILM.id)
                result = await auto.download(MOVIE, target)
                self.assertTrue(result.found)
                grabbed = grabber.grab.await_args.args[0]
                self.assertEqual(self.marked(panel), [release_id(grabbed)])
                self.assertEqual(cast(Any, panel)._recommended_iid, release_id(grabbed))
                panel.destroy()

    async def test_the_mark_and_the_bar_follow_the_results_as_they_arrive(self) -> None:
        search = StreamSearch()
        panel = self.panel(MovieTarget(FILM), search)
        state: Any = panel
        panel.start()
        await self.settle()
        self.assertEqual(self.marked(panel), [])  # still searching: no rows, today's text
        self.assertEqual(state._bar_caption.cget("text"), NO_SELECTION_TEXT)
        self.assertEqual(state._download_button.cget("state"), "disabled")
        self.assertEqual(state._legend.winfo_manager(), "")

        slow, fast = FILM_RELEASES[3], FILM_RELEASES[4]  # 1080p: 44 and 150 seeders
        search.updates.put_nowait(SearchUpdate(statuses=(), releases=(slow,), done=False))
        await self.settle()
        self.assertEqual(self.marked(panel), [release_id(slow)])
        self.assertEqual(state._selected_name, f"★ Recommended: {slow.title}")
        self.assertEqual(state._bar_detail.cget("text"), "1080p, lots of seeders")
        self.assertEqual(state._bar_caption.winfo_manager(), "")
        self.assertEqual(state._download_button.cget("state"), "normal")
        self.assertEqual(state._legend.winfo_manager(), "grid")

        search.updates.put_nowait(SearchUpdate(statuses=(), releases=(fast, slow), done=True))
        await self.settle()
        self.assertEqual(self.marked(panel), [release_id(fast)])
        self.assertEqual(state._selected_name, f"★ Recommended: {fast.title}")
        self.assertEqual(state._bar_detail.cget("text"), "1080p, most seeders, normal size")
        self.assertEqual(picture_of(panel, fast), picture_key(Speed.FAST, recommended=True))
        self.assertEqual(picture_of(panel, slow), picture_key(Speed.FAST, recommended=False))
        self.assertEqual(state._tree.item(release_id(fast), "text"), fast.title)
        search.updates.put_nowait(None)

    async def test_the_version_and_the_sort(self) -> None:
        panel = self.panel(MovieTarget(FILM), FakeSearch(FILM_RELEASES))
        state: Any = panel
        panel.start()
        await self.settle()
        flux = release_id(FILM_RELEASES[4])
        self.assertEqual(self.marked(panel), [flux])  # 1080p: the most seeders, no fake

        state._sort_by("name")
        await self.settle()
        self.assertEqual(self.marked(panel), [flux])  # sorting moves the row, not the mark
        self.assertEqual(state._tree.get_children()[0], release_id(FILM_RELEASES[5]))

        state._select_quality("2160p")
        await self.settle()
        self.assertEqual(self.marked(panel), [release_id(FILM_RELEASES[0])])
        self.assertEqual(state._bar_detail.cget("text"), "4K x265, most seeders, normal size")

        state._select_quality("480p")  # nothing listed: today's text
        await self.settle()
        self.assertEqual(self.marked(panel), [])
        self.assertEqual(state._bar_caption.cget("text"), NO_SELECTION_TEXT)
        self.assertEqual(state._download_button.cget("state"), "disabled")
        self.assertEqual(state._legend.winfo_manager(), "")

    async def test_nothing_qualifies_no_mark(self) -> None:
        fake = FILM_RELEASES[6]
        panel = self.panel(MovieTarget(FILM), FakeSearch((fake,)))
        state: Any = panel
        panel.start()
        await self.settle()
        self.assertEqual(state._tree.get_children(), (release_id(fake),))  # listed, not picked
        self.assertEqual(self.marked(panel), [])
        self.assertEqual(state._bar_caption.cget("text"), NO_SELECTION_TEXT)
        self.assertEqual(state._download_button.cget("state"), "disabled")
        self.assertEqual(state._legend.winfo_manager(), "grid")

    async def test_dots_per_row(self) -> None:
        panel = self.panel(MovieTarget(FILM), FakeSearch(FILM_RELEASES))
        state: Any = panel
        state._select_quality("any")
        panel.start()
        await self.settle()
        for release in FILM_RELEASES:
            with self.subTest(release.title):
                recommended = release_id(release) == state._recommended_iid
                key = picture_key(speed(release), recommended=recommended)
                self.assertEqual(picture_of(panel, release), key)
        self.assertEqual(speed(FILM_RELEASES[7]), Speed.UNKNOWN)  # the grey one is there
        self.assertEqual(speed(FILM_RELEASES[5]), Speed.STALL)

    async def test_download_without_a_selection_grabs_the_recommendation(self) -> None:
        downloads = FakeDownloads()
        panel = self.panel(MovieTarget(FILM), FakeSearch(FILM_RELEASES), downloads=downloads)
        state: Any = panel
        panel.start()
        await self.settle()
        recommended = FILM_RELEASES[4]

        other = release_id(FILM_RELEASES[3])
        state._tree.selection_set(other)  # another row: the bar shows it
        await self.settle()
        self.assertEqual(state._bar_caption.cget("text"), "Selected")
        self.assertEqual(state._selected_name, FILM_RELEASES[3].title)
        self.assertEqual(self.marked(panel), [release_id(recommended)])  # the mark stays

        state._tree.selection_set(())  # nothing selected again: back to the recommendation
        await self.settle()
        self.assertEqual(state._selected_name, f"★ Recommended: {recommended.title}")

        state._download_button.invoke()
        await self.settle()
        self.assertEqual(downloads.grabbed, [recommended.title])
        self.assertEqual(state._download_button.cget("state"), "disabled")  # Downloading…
        downloads.release.set()
        await self.settle()
        self.assertEqual(state._download_button.cget("text"), "✓ Saved")
        self.assertEqual(state._selected_name, f"★ Recommended: {recommended.title}")

    async def test_an_episode_and_its_pack_fallback(self) -> None:
        pack = rel("Severance.S02E01-E03.1080p.WEB", seeders=900, gb=6)
        single = rel("Severance.S02E01.1080p.WEB", seeders=8, gb=1.5)
        search = FakeSearch((pack, single))
        panel = self.panel(TvTarget(SHOW, 2, 1), search)
        state: Any = panel
        panel.start()
        await self.settle()
        self.assertEqual(search.requests, [("episode", 2, 1)])
        self.assertEqual(self.marked(panel), [release_id(single)])  # a single episode first
        self.assertEqual(state._bar_detail.cget("text"), "1080p, few seeders, small size")

    async def test_the_pictures_are_repainted_in_place(self) -> None:
        panel = self.panel(MovieTarget(FILM), FakeSearch(FILM_RELEASES))
        state: Any = panel
        before = dict(state._pictures)
        self.assertEqual(len(before), 2 * len(Speed))
        state._paint_pictures()  # as on a switch between light and dark
        self.assertEqual(
            {key: id(photo) for key, photo in state._pictures.items()},
            {key: id(photo) for key, photo in before.items()},
        )


if __name__ == "__main__":
    unittest.main()
