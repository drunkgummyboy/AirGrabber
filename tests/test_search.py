"""TorrentSearchService and its pure helpers: queries, relevance, filters, merging, streaming."""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from unittest import mock

import httpx

from app.config import AppPaths
from app.database import SettingsRepository
from app.indexers import (
    BaseIndexer,
    EztvIndexer,
    IndexerError,
    ProwlarrIndexer,
    default_indexers,
)
from app.models import (
    IndexerState,
    IndexerStatus,
    Movie,
    MovieDetails,
    QualityProfile,
    SearchKind,
    SearchRequest,
    SearchUpdate,
    Settings,
    Show,
    TorrentRelease,
)
from app.services import Services
from app.services.search import (
    QUALITY_KEYS,
    TorrentSearchService,
    build_queries,
    is_preferred,
    is_relevant,
    matches_quality,
    merge,
    passes_settings,
    quality_counts,
    quality_key,
)
from app.services.settings import SettingsService

FIXTURES = Path(__file__).parent / "fixtures" / "indexers"
LOGGER = "app.services.search"
MIB = 1024 * 1024

EPISODE = SearchRequest(
    kind=SearchKind.EPISODE,
    title="Severance",
    year=2022,
    season=2,
    episode=1,
    imdb_id="tt11280740",
    queries=("Severance S02E01", "Severance 2022 S02E01"),
)
SEASON = SearchRequest(kind=SearchKind.SEASON, title="Severance", season=2)
MOVIE = SearchRequest(kind=SearchKind.MOVIE, title="The Shawshank Redemption", year=1994)
SEVERANCE = Show(id=44933, name="Severance", premiered=date(2022, 2, 18), imdb_id="tt11280740")
STATUS = {"appName": "Prowlarr", "version": "1.24.3.4754"}


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def release(
    title: str,
    *,
    h: int | None = 1,
    source: str = "apibay",
    seeders: int = 10,
    leechers: int = 1,
    size: int = 1000 * MIB,
    **fields: Any,
) -> TorrentRelease:
    """A release with info hash `h` (as 40 hex digits); `h=None` makes a hashless one."""
    if h is None:
        fields.setdefault("download_url", "https://indexer.example/download/1")
    return TorrentRelease(
        title=title,
        source=source,
        seeders=seeders,
        leechers=leechers,
        size_bytes=size,
        info_hash=None if h is None else f"{h:040x}",
        **fields,
    )


def settings_service(**values: Any) -> SettingsService:
    return SettingsService(mock.Mock(spec=SettingsRepository), Settings(**values))


def relevant(title: str, request: SearchRequest) -> bool:
    return is_relevant(release(title), request)


# --- queries ----------------------------------------------------------------------------------


class BuildQueriesTests(unittest.TestCase):
    def test_movie(self) -> None:
        self.assertEqual(
            build_queries(SearchKind.MOVIE, "The Shawshank Redemption", year=1994),
            ("The Shawshank Redemption 1994", "The Shawshank Redemption"),
        )
        self.assertEqual(
            build_queries(SearchKind.MOVIE, "Spider-Man: No Way Home", year=2021),
            (
                "Spider-Man: No Way Home 2021",
                "SpiderMan No Way Home 2021",
                "Spider-Man: No Way Home",
                "SpiderMan No Way Home",
            ),
        )

    def test_movie_without_year(self) -> None:
        self.assertEqual(build_queries(SearchKind.MOVIE, "Up"), ("Up",))
        self.assertEqual(
            build_queries(SearchKind.MOVIE, "Ocean's Eleven"), ("Ocean's Eleven", "Oceans Eleven")
        )

    def test_episode(self) -> None:
        self.assertEqual(
            build_queries(SearchKind.EPISODE, "Severance", year=2022, season=2, episode=1),
            ("Severance S02E01", "Severance 2022 S02E01"),
        )
        self.assertEqual(
            build_queries(
                SearchKind.EPISODE, "Marvel's Agents of S.H.I.E.L.D.", year=2013, season=7,
                episode=12,
            ),
            (
                "Marvel's Agents of S.H.I.E.L.D. S07E12",
                "Marvel's Agents of S.H.I.E.L.D. 2013 S07E12",
                "Marvels Agents of SHIELD S07E12",
            ),
        )
        self.assertEqual(
            build_queries(SearchKind.EPISODE, "Grey's Anatomy", season=19, episode=5),
            ("Grey's Anatomy S19E05", "Greys Anatomy S19E05"),
        )

    def test_season(self) -> None:
        self.assertEqual(
            build_queries(SearchKind.SEASON, "Severance", season=2),
            ("Severance S02", "Severance Season 2", "Severance S02 Complete"),
        )
        self.assertEqual(
            build_queries(SearchKind.SEASON, "Grey's Anatomy", year=2005, season=3),
            (
                "Grey's Anatomy S03",
                "Grey's Anatomy Season 3",
                "Grey's Anatomy S03 Complete",
                "Grey's Anatomy 2005 S03",
                "Grey's Anatomy 2005 Season 3",
                "Greys Anatomy S03",
                "Greys Anatomy Season 3",
            ),
        )

    def test_whitespace_and_symbols(self) -> None:
        self.assertEqual(
            build_queries(SearchKind.EPISODE, "  Love,  Death & Robots ", season=1, episode=2),
            ("Love, Death & Robots S01E02", "Love Death Robots S01E02"),
        )
        self.assertEqual(build_queries(SearchKind.MOVIE, "Amélie", year=2001),
                         ("Amélie 2001", "Amélie"))
        self.assertEqual(build_queries(SearchKind.MOVIE, "?!", year=2001), ("?! 2001", "?!"))
        self.assertEqual(build_queries(SearchKind.MOVIE, "   ", year=2001), ())

    def test_missing_numbers(self) -> None:
        with self.assertRaises(ValueError):
            build_queries(SearchKind.EPISODE, "Severance", season=2)
        with self.assertRaises(ValueError):
            build_queries(SearchKind.EPISODE, "Severance", episode=1)
        with self.assertRaises(ValueError):
            build_queries(SearchKind.SEASON, "Severance")


# --- relevance --------------------------------------------------------------------------------


class RelevanceTests(unittest.TestCase):
    def check(self, request: SearchRequest, good: Iterable[str], bad: Iterable[str]) -> None:
        for title in good:
            with self.subTest(relevant=title):
                self.assertTrue(relevant(title, request))
        for title in bad:
            with self.subTest(irrelevant=title):
                self.assertFalse(relevant(title, request))

    def test_episode(self) -> None:
        self.check(
            EPISODE,
            good=[
                "Severance S02E01 1080p WEB H264-SuccessfulCrab",
                "severance.s02e01.1080p.web.h264-successfulcrab[EZTVx.to].mkv",
                "www.Torrenting.com - Severance S02E01 Hello Ms Cobel 720p ATVP WEB-DL",
                "Severance (2022) S02E01 (2160p ATVP WEB-DL Hybrid H265 DV HDR10+ - HONE)",
                "Severance.2x01.720p.HDTV",
                "Severance S2E1 720p",
                "Severance S02 E01 720p",
                "Severance.S02E01-02.2160p.ATVP.WEB-DL",
                "Severance S02E01E02 1080p",
                "Severance Season 2 Episode 1 1080p",
                "[ Torrent911.im ] Severance.S02E01.FRENCH.WEBRip.x264.mp4",
            ],
            bad=[
                "Severance S02E10 Cold Harbor 1080p WebRip",
                "Severance S02E11 1080p",
                "Severance S02E011 1080p",
                "Severance 2x10 720p",
                "Severance S12E01 1080p",
                "Severance S01E01 1080p",
                "Severance S02 1080p x265-ELiTE EZTV",
                "Severance Season 2 Complete 1080p",
                "Severanced S02E01 1080p",
                "Clarity.S02E01.1080p.WEB.mkv",
            ],
        )

    def test_title_words_not_substrings(self) -> None:
        lost = EPISODE.model_copy(update={"title": "Lost"})
        self.assertFalse(relevant("Severance.S02E01.1080p.rus.LostFilm.TV.mkv", lost))
        self.assertTrue(relevant("Lost.S02E01.720p.HDTV", lost))
        package = EPISODE.model_copy(update={"title": "The Severance Package"})
        self.assertFalse(relevant("Severance S02E01 1080p WEB H264-SuccessfulCrab", package))
        self.assertTrue(relevant("The.Severance.Package.S02E01.720p", package))
        self.assertTrue(relevant("Severance Package S02E01", package))  # "the" is optional

    def test_titles_with_punctuation(self) -> None:
        shield = EPISODE.model_copy(update={"title": "Marvel's Agents of S.H.I.E.L.D."})
        self.check(
            shield,
            good=[
                "Marvels.Agents.of.S.H.I.E.L.D.S02E01.720p.HDTV.x264-KILLERS",
                "Marvel's Agents of S.H.I.E.L.D. S02E01 1080p",
                "Marvels Agents of SHIELD S02E01 720p",
                "Marvels Agents of S H I E L D S02E01",
            ],
            bad=["Marvels Runaways S02E01", "Marvels.Agents.of.S.H.I.E.L.D.S02E02.720p"],
        )
        greys = EPISODE.model_copy(update={"title": "Grey's Anatomy"})
        self.check(
            greys,
            good=["Greys.Anatomy.S02E01.1080p", "Grey's Anatomy S02E01 720p",
                  "Grey s Anatomy S02E01 720p"],
            bad=["Grey Anatomy S02E01", "Station 19 S02E01"],
        )
        spider = MOVIE.model_copy(update={"title": "Spider-Man: No Way Home", "year": 2021})
        self.check(
            spider,
            good=["Spider-Man.No.Way.Home.2021.1080p.WEB", "Spiderman No Way Home 2021 720p"],
            bad=["Spider-Man.Far.From.Home.2019.1080p", "Home.Alone.2021.1080p"],
        )
        mash = SEASON.model_copy(update={"title": "M*A*S*H", "season": 3})
        self.assertTrue(relevant("M.A.S.H.S03.1080p.BluRay", mash))
        self.assertTrue(relevant("MASH Season 3 720p", mash))

    def test_season_pack(self) -> None:
        self.check(
            SEASON,
            good=[
                "Severance S02 1080p x265-ELiTE EZTV",
                "Severance.S02.COMPLETE.2160p.ATVP.WEB-DL",
                "Severance Season 2 Complete 1080p",
                "Severance Complete Season 2 720p",
                "Severance (2022) Season 02 1080p",
                "Severance S01-S02 1080p",
                "Severance S2 720p",
                "Severance 2 Complete 720p",
            ],
            bad=[
                "Severance S02E01 1080p",
                "Severance.S02E01-E10.1080p",
                "Severance 2x01 720p",
                "Severance Season 2 Episode 3 720p",
                "Severance S03 1080p",
                "Severance S20 1080p",
                "Severance Season 12 1080p",
                "Severance 1080p",
                "Severance Complete 2160p",
            ],
        )
        fourth = SEASON.model_copy(update={"season": 4})
        self.assertFalse(relevant("Severance Complete 4K", fourth))
        self.assertTrue(relevant("Severance Complete 4 720p", fourth))

    def test_movie(self) -> None:
        self.check(
            MOVIE,
            good=[
                "The Shawshank Redemption (1994) [1080p] [bluray] [x264] [YTS]",
                "The.Shawshank.Redemption.1994.REMASTERED.1080p.BluRay",
                "Shawshank Redemption 1995 720p",
                "The Shawshank Redemption 1993 DVDRip",
                "The Shawshank Redemption 1994 DDP5.1x264",
            ],
            bad=[
                "The Shawshank Redemption 1996 1080p",
                "The Shawshank Redemption 1080p BluRay",
                "The Shawshank Redemption S01E01 1994",
                "The Shawshank Redemption 1994 1x02",
                "The Shawshank 1994 1080p",
                "The Shawshank Redemption 19945 1080p",
            ],
        )
        unknown_year = MOVIE.model_copy(update={"year": None})
        self.assertTrue(relevant("The Shawshank Redemption 1080p BluRay", unknown_year))
        self.assertFalse(relevant("The Shawshank Redemption S01E01", unknown_year))

    def test_mostly_overlapping_titles(self) -> None:
        """Release names often shorten a title: 2+ shared words covering 60% of the smaller
        word set (title vs the release's name before its marker or year) are enough."""
        svu = EPISODE.model_copy(
            update={"title": "Law & Order: Special Victims Unit", "season": 27, "episode": 1}
        )
        self.assertTrue(relevant("Law.and.Order.SVU.S27E01.1080p.WEB.h264-ETHEL", svu))
        self.assertTrue(relevant("Law.and.Order.SVU.1999.S27E01.720p", svu))  # a year: no word
        self.assertFalse(relevant("Law.and.Order.SVU.S27E02.1080p", svu))  # markers still rule
        self.assertFalse(relevant("Law.and.Order.Organized.Crime.S27E01.1080p", svu))  # 2 of 4
        svu_pack = SEASON.model_copy(
            update={"title": "Law & Order: Special Victims Unit", "season": 27}
        )
        self.assertTrue(relevant("Law.and.Order.SVU.Complete.Season.27.1080p", svu_pack))

        daredevil = EPISODE.model_copy(update={"title": "Marvel's Daredevil: Born Again"})
        self.assertTrue(relevant("Daredevil.Born.Again.S02E01.1080p.WEB.h264-ETHEL", daredevil))
        self.assertTrue(relevant("Daredevil.Born.Again.2025.S02E01.720p", daredevil))
        self.assertFalse(relevant("Daredevil.S02E01.1080p.NF.WEB", daredevil))  # 1 shared word

        shield = EPISODE.model_copy(
            update={"title": "Marvel's Agents of S.H.I.E.L.D.", "season": 7, "episode": 1}
        )
        self.assertTrue(relevant("Agents.of.SHIELD.S07E01.1080p", shield))
        self.assertTrue(relevant("Agents.of.S.H.I.E.L.D.S07E01.720p.HDTV", shield))

        package = EPISODE.model_copy(update={"title": "The Severance Package"})
        self.assertFalse(relevant("Severance.S02E01.1080p.WEB", package))  # only 1 shared word
        self.assertFalse(relevant("Package.S02E01.1080p.WEB", package))

        who = EPISODE.model_copy(update={"title": "Doctor Who (2005)", "season": 1, "episode": 1})
        self.assertTrue(relevant("Doctor.Who.S01E01.Rose.720p", who))
        self.assertFalse(relevant("Doctor.Odyssey.S01E01.720p", who))

        # Known and accepted false positive: every word of "Law & Order" is in the spin-off's
        # name, so its episodes with the same number pass the title check too.
        law = EPISODE.model_copy(update={"title": "Law & Order", "season": 4, "episode": 1})
        self.assertTrue(relevant("Law.and.Order.Organized.Crime.S04E01.1080p", law))

        new_hope = MOVIE.model_copy(
            update={"title": "Star Wars: Episode IV - A New Hope", "year": 1977}
        )
        self.assertTrue(relevant("Star.Wars.A.New.Hope.1977.1080p.BluRay", new_hope))
        self.assertTrue(relevant("Star.Wars.1977.1080p", new_hope))  # its 1977 release title
        rotk = MOVIE.model_copy(
            update={"title": "The Lord of the Rings: The Return of the King", "year": 2003}
        )
        self.assertTrue(relevant("Lord.of.the.Rings.Return.of.the.King.2003.EXTENDED", rotk))
        self.assertFalse(relevant("The.Return.2003.1080p", rotk))
        self.assertFalse(relevant("The.King.2019.1080p", rotk.model_copy(update={"year": None})))


# --- settings filters ---------------------------------------------------------------------------


class SettingsFilterTests(unittest.TestCase):
    def test_default_excluded_keywords_are_words(self) -> None:
        settings = Settings()
        self.assertEqual(settings.excluded_keywords, ("cam", "telesync", "hdcam", "sample"))
        dropped = [
            "Movie 2024 CAM x264",
            "Movie.2024.HDCAM.x264",
            "Movie.2024.HD-CAM.x264",
            "Movie 2024 [Cam]",
            "Movie 2024 TeleSync",
            "Movie.2024.1080p.Sample.mkv",
        ]
        kept = [
            "Movie.2024.Camera.Obscura.1080p",
            "The Cameraman 1928 1080p",
            "Webcam Girls 2024 720p",
            "Samples of Life 2024",
        ]
        for title in dropped:
            with self.subTest(dropped=title):
                self.assertFalse(passes_settings(release(title), settings, SearchKind.MOVIE))
        for title in kept:
            with self.subTest(kept=title):
                self.assertTrue(passes_settings(release(title), settings, SearchKind.MOVIE))

    def test_phrases_and_case(self) -> None:
        settings = Settings(excluded_keywords="WEB-DL, 3d, dual audio")
        self.assertEqual(settings.excluded_keywords, ("web-dl", "3d", "dual audio"))
        cases = {
            "Show.S01E01.1080p.WEB-DL.x264": False,
            "Show S01E01 1080p WEB DL": False,
            "Show S01E01 1080p WEBDL": True,
            "Movie 2024 3D HSBS": False,
            "Movie 2024 3DS": True,
            "Movie.2024.DUAL.AUDIO.1080p": False,
            "Movie 2024 Dual 1080p Audio": True,
        }
        for title, passes in cases.items():
            with self.subTest(title=title):
                allowed = passes_settings(release(title), settings, SearchKind.EPISODE)
                self.assertIs(allowed, passes)

    def test_size_limits_by_kind(self) -> None:
        settings = Settings(excluded_keywords="", min_size_mb=500, max_size_mb=4000)
        sizes = {400 * MIB: False, 500 * MIB: True, 4000 * MIB: True, 4001 * MIB: False, 0: True}
        for kind in (SearchKind.EPISODE, SearchKind.MOVIE):
            for size, passes in sizes.items():
                with self.subTest(kind=kind, size=size):
                    self.assertIs(passes_settings(release("x", size=size), settings, kind), passes)
        for size in (100 * MIB, 40_000 * MIB):  # season packs ignore the limits
            self.assertTrue(passes_settings(release("x", size=size), settings, SearchKind.SEASON))

    def test_max_zero_means_no_upper_limit(self) -> None:
        settings = Settings(min_size_mb=100)
        huge = release("x", size=100_000 * MIB)
        self.assertTrue(passes_settings(huge, settings, SearchKind.MOVIE))
        self.assertFalse(passes_settings(release("x", size=99 * MIB), settings, SearchKind.MOVIE))

    def check_keywords(self, keywords: str, dropped: list[str], kept: list[str]) -> None:
        settings = Settings(excluded_keywords=keywords)
        for title in dropped:
            with self.subTest(keywords=keywords, dropped=title):
                self.assertFalse(passes_settings(release(title), settings, SearchKind.MOVIE))
        for title in kept:
            with self.subTest(keywords=keywords, kept=title):
                self.assertTrue(passes_settings(release(title), settings, SearchKind.MOVIE))

    def test_cam_family(self) -> None:
        dropped = [
            "Spider-Man.Brand.New.Day.2026.1080p.CAMRip.Dublado.V.5.mkv",
            "Movie 2026 CAM x264",
            "Movie.2026.HDCAM.x264",
            "Movie 2026 HDCAMRip",
            "Movie.2026.CAM-Rip.x264",
        ]
        kept = [
            "Movie.2026.Camera.1080p",
            "Cameron.2026.1080p.WEB",
            "Camp.Crystal.Lake.2026.1080p",
            "Webcam.2026.1080p",
        ]
        for keyword in ("cam", "CamRip"):
            self.check_keywords(keyword, dropped, kept)
        # A keyword without a family matches only itself.
        self.check_keywords("hdcam", ["Movie 2026 HDCAM"], ["Movie 2026 CAM", "Movie 2026 CAMRip"])

    def test_telesync_family(self) -> None:
        dropped = [
            "Spider-Man Brand New Day 2026 1080p HDTS DUAL H264-LMNTRY",
            "Movie 2026 TeleSync",
            "Movie.2026.TS.x264",
            "Movie 2026 HD-TS",
            "Movie 2026 TSRip",
            "Movie 2026 HDTSRip",
            "Movie 2026 PDVD",
            "Movie.2026.PreDVD.x264",
        ]
        kept = [
            "Shorts.2026.1080p.WEB",
            "Its.Always.Sunny.2026.1080p",
            "Movie 2026 MTS 1080p",
            "Movie.2026.1080p.BluRay.DTS-HD.MA.5.1",
        ]
        for keyword in ("telesync", "TS"):
            self.check_keywords(keyword, dropped, kept)

    def test_telecine_family(self) -> None:
        dropped = ["Movie 2026 TELECINE", "Movie.2026.TC.x264", "Movie.2026.HDTC.x264"]
        kept = ["Movie.2026.TCM.1080p", "Movie 2026 Etc 1080p"]
        for keyword in ("telecine", "tc"):
            self.check_keywords(keyword, dropped, kept)

    def test_screener_family(self) -> None:
        dropped = [
            "Movie 2026 Screener",
            "Movie.2026.SCR.x264",
            "Movie.2026.DVDScr.XviD",
            "Movie 2026 BDSCR",
            "Movie.2026.WEBSCR.1080p",
        ]
        kept = ["Scream.2026.1080p.WEB", "Movie.2026.Screenplay.Edition.1080p"]
        for keyword in ("screener", "scr"):
            self.check_keywords(keyword, dropped, kept)

    def test_other_keywords_match_only_themselves(self) -> None:
        self.check_keywords("hdrip", ["Movie.2026.HDRip.x264"], ["Movie.2026.HDTV.x264"])
        self.check_keywords(
            "sample", ["Movie.2026.1080p.Sample.mkv"], ["Movie 2026 Samples 1080p"]
        )

    def test_the_users_keywords(self) -> None:
        self.check_keywords(
            "cam, telesync, korsub, sample, hdrip",
            dropped=[
                "Spider-Man.Brand.New.Day.2026.1080p.CAMRip.Dublado.V.5.mkv",
                "Spider-Man Brand New Day 2026 1080p HDTS DUAL H264-LMNTRY",
                "Spider-Man.Brand.New.Day.2026.KORSUB.HDRip.x264",
            ],
            kept=[
                "Spider-Man.Brand.New.Day.2026.1080p.WEB-DL.x264",
                "Spider-Man Brand New Day 2026 2160p WEB H265-Camera",
            ],
        )


class PreferredGroupTests(unittest.TestCase):
    def test_groups(self) -> None:
        settings = Settings(preferred_groups="elite, NTb, successfulcrab, hone, hodl")
        cases = {
            "Severance.S02E01.1080p.x265-ELiTE": True,
            "Severance S02 1080p x265-ELiTE EZTV": True,
            "Severance.S02E01.Hello.Ms.Cobel.720p.ATVP.WEB-DL.DDPA5.1.H.264-NTb ": True,
            "severance.s02e01.1080p.web.h264-successfulcrab[EZTVx.to].mkv": True,
            "Severance (2022) S02E01 (2160p ATVP WEB-DL DDP Atmos 5.1 English - HONE)": True,
            "Severance.S02E01.Hello.Ms.Cobel.1080p.WEBRip.10bit.DDP5.1.x265-HODL.mkv": True,
            "Severance S02E01 720p WEB x265-MiNX": False,
            "Severance.S02E01.1080p-ELiTEX": False,
            "Severance S02E01 ELiTE 1080p-FLUX": False,
            "The Shawshank Redemption (1994) [1080p] [bluray] [x264] [YTS]": False,
        }
        for title, preferred in cases.items():
            with self.subTest(title=title):
                self.assertIs(is_preferred(release(title), settings), preferred)

    def test_no_preferred_groups(self) -> None:
        self.assertFalse(is_preferred(release("Severance.S02E01.1080p.x265-ELiTE"), Settings()))


# --- merging ------------------------------------------------------------------------------------


class MergeTests(unittest.TestCase):
    def test_same_hash_is_one_release(self) -> None:
        published = datetime(2025, 1, 17, tzinfo=UTC)
        apibay = release(
            "Severance S02E01 1080p WEB H264-SuccessfulCrab",
            seeders=162, leechers=8, info_url="https://thepiratebay.org/description.php?id=1",
        )
        eztv = release(
            "Severance S02E01 1080p WEB H264-SuccessfulCrab EZTV",
            source="eztv", seeders=100, leechers=50,
            magnet_uri=f"magnet:?xt=urn:btih:{1:040x}&tr=udp://tracker",
            download_url="https://zoink.ch/torrent/severance.torrent",
            published_at=published,
        )
        bitsearch = release(
            "severance.s02e01.1080p.web.h264-successfulcrab[EZTVx.to].mkv",
            source="bitsearch", seeders=2980, leechers=12,
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        other = release("Severance S02E01 720p", h=2, source="eztv")
        [merged, second] = merge([apibay, eztv, other, bitsearch])
        self.assertIs(second, other)
        self.assertEqual(merged.title, apibay.title)  # the first one is kept...
        self.assertEqual(merged.source, "apibay+eztv+bitsearch")
        self.assertEqual((merged.seeders, merged.leechers), (2980, 50))  # ...with the best counts
        self.assertEqual(merged.info_url, apibay.info_url)
        self.assertEqual(merged.magnet_uri, eztv.magnet_uri)
        self.assertEqual(merged.download_url, eztv.download_url)
        self.assertEqual(merged.published_at, published)
        self.assertEqual(merged.info_hash, apibay.info_hash)

    def test_prefers_a_torrent_file_link(self) -> None:
        kept = release("Show S01E01", source="apibay")
        prowlarr = release(
            "Show S01E01", source="prowlarr:1337x",
            download_url="http://localhost:9696/4/download?apikey=x&link=abc",
        )
        eztv = release(
            "Show S01E01", source="eztv", download_url="https://zoink.ch/t/show.torrent"
        )
        [merged] = merge([kept, prowlarr, eztv])
        self.assertEqual(merged.download_url, "https://zoink.ch/t/show.torrent")
        [merged] = merge([kept, prowlarr])
        self.assertEqual(merged.download_url, prowlarr.download_url)

    def test_keeps_its_own_fields(self) -> None:
        own = release("Show S01E01", source="yts", download_url="https://yts.example/a.torrent",
                      info_url="https://yts.example/movie")
        other = release("Show S01E01", source="apibay", download_url="https://b.example/b.torrent",
                        info_url="https://b.example/info", size=5)
        [merged] = merge([own, other])
        self.assertEqual(merged.download_url, own.download_url)
        self.assertEqual(merged.info_url, own.info_url)
        self.assertEqual(merged.size_bytes, own.size_bytes)

    def test_fills_unknown_size(self) -> None:
        [merged] = merge([release("Show S01E01", size=0), release("Show S01E01", size=123)])
        self.assertEqual(merged.size_bytes, 123)

    def test_sources_never_repeat(self) -> None:
        [merged] = merge([
            release("a", source="apibay"),
            release("a", source="eztv"),
            release("a", source="apibay"),
        ])
        self.assertEqual(merged.source, "apibay+eztv")
        [again] = merge([merged, release("a", source="eztv"), release("a", source="bitsearch")])
        self.assertEqual(again.source, "apibay+eztv+bitsearch")

    def test_hashless_dedupe_on_title_and_size(self) -> None:
        first = release("Show.S01E01.1080p.WEB-GRP", h=None, source="prowlarr:a", seeders=3)
        same = release("show s01e01 1080p web grp", h=None, source="prowlarr:b", seeders=9,
                       download_url="https://b.example/x.torrent")
        bigger = release("Show.S01E01.1080p.WEB-GRP", h=None, source="prowlarr:c", size=2)
        hashed = release("Show.S01E01.1080p.WEB-GRP", source="apibay")
        merged = merge([first, same, bigger, hashed])
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0].source, "prowlarr:a+prowlarr:b")
        self.assertEqual(merged[0].seeders, 9)
        self.assertEqual(merged[0].download_url, first.download_url)
        self.assertIs(merged[1], bigger)
        self.assertIs(merged[2], hashed)

    def test_empty(self) -> None:
        self.assertEqual(merge([]), [])


# --- quality ------------------------------------------------------------------------------------


class QualityTests(unittest.TestCase):
    def test_counts(self) -> None:
        releases = [
            release("Show S01E01 2160p WEB H265-A"),
            release("Show S01E01 1080p WEB x264-B"),
            release("Show S01E01 1080p HEVC-C"),
            release("Show S01E01 720p WEB-D"),
            release("Show S01E01 480p x264-E"),
            release("Show S01E01 XviD-F"),
        ]
        self.assertEqual(
            quality_counts(releases),
            {"2160p": 1, "1080p": 2, "720p": 1, "480p": 1, "x265": 2, "any": 6},
        )
        self.assertEqual(list(quality_counts([])), list(QUALITY_KEYS))
        self.assertEqual(quality_counts(iter(releases))["any"], 6)

    def test_matches(self) -> None:
        uhd_hevc = release("Show S01E01 2160p x265-A")
        self.assertEqual(
            [key for key in QUALITY_KEYS if matches_quality(uhd_hevc, key)],
            ["2160p", "x265", "any"],
        )
        with self.assertRaises(ValueError):
            matches_quality(uhd_hevc, "4k")

    def test_quality_key(self) -> None:
        self.assertEqual(QUALITY_KEYS, ("2160p", "1080p", "720p", "480p", "x265", "any"))
        self.assertEqual(
            {profile: quality_key(profile) for profile in QualityProfile},
            {
                QualityProfile.ANY: "any",
                QualityProfile.HD: "720p",
                QualityProfile.FULL_HD: "1080p",
                QualityProfile.UHD: "2160p",
                QualityProfile.HEVC: "x265",
            },
        )
        self.assertTrue(all(quality_key(profile) in QUALITY_KEYS for profile in QualityProfile))


# --- the service --------------------------------------------------------------------------------


class FakeIndexer(BaseIndexer):
    """Answers after an optional gate and delay with fixed releases or an exception."""

    name = "fake"
    label = "Fake"
    kinds = frozenset(SearchKind)

    def __init__(
        self,
        *,
        result: Iterable[TorrentRelease] = (),
        error: BaseException | None = None,
        delay: float = 0.0,
        gate: asyncio.Event | None = None,
    ) -> None:
        super().__init__(mock.Mock(spec=httpx.AsyncClient))
        self.result = list(result)
        self.error = error
        self.delay = delay
        self.gate = gate
        self.requests: list[SearchRequest] = []
        self.started = asyncio.Event()
        self.cancelled = False

    async def search(self, request: SearchRequest) -> list[TorrentRelease]:
        self.requests.append(request)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return list(self.result)

    def web_search_url(self, query: str) -> str | None:
        return f"https://{self.name}.example/search?q={query}"


def fake(name: str, *, kinds: Iterable[SearchKind] = SearchKind, **options: Any) -> FakeIndexer:
    attributes = {"name": name, "label": name.upper(), "kinds": frozenset(kinds)}
    cls = cast(type[FakeIndexer], type(f"Fake_{name}", (FakeIndexer,), attributes))
    return cls(**options)


def summary(update: SearchUpdate) -> list[tuple[str, IndexerState, int, str | None]]:
    return [(s.name, s.state, s.count, s.message) for s in update.statuses]


def running_searches() -> list[asyncio.Task[Any]]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("torrent search:") and not task.done()
    ]


class SearchServiceTests(unittest.IsolatedAsyncioTestCase):
    def http(self) -> httpx.AsyncClient:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected request to {request.url.host}")

        http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        self.addAsyncCleanup(http.aclose)
        return http

    async def test_update_sequence(self) -> None:
        hit = release("Severance S02E01 1080p WEB-A", source="a")
        a = fake("a", result=[hit], gate=asyncio.Event())
        b = fake("b", gate=asyncio.Event())
        c = fake("c", error=IndexerError("C: /api answered HTTP 500"), gate=asyncio.Event())
        service = TorrentSearchService([a, b, c], settings_service())
        updates = service.search(EPISODE)
        first = await anext(updates)
        self.assertEqual(
            summary(first),
            [("a", IndexerState.SEARCHING, 0, None), ("b", IndexerState.SEARCHING, 0, None),
             ("c", IndexerState.SEARCHING, 0, None)],
        )
        self.assertEqual(first.releases, ())
        self.assertEqual(first.hidden, ())
        self.assertFalse(first.done)
        self.assertEqual(
            [status.label for status in first.statuses], ["A", "B", "C"]
        )
        self.assertEqual(
            [status.web_url for status in first.statuses],
            [f"https://{name}.example/search?q=Severance S02E01" for name in "abc"],
        )

        assert b.gate is not None and c.gate is not None and a.gate is not None
        b.gate.set()
        second = await anext(updates)
        self.assertEqual(
            [status.state for status in second.statuses],
            [IndexerState.SEARCHING, IndexerState.EMPTY, IndexerState.SEARCHING],
        )
        self.assertFalse(second.done)

        c.gate.set()
        with self.assertLogs(LOGGER, "WARNING") as logs:
            third = await anext(updates)
        self.assertEqual(third.statuses[2].state, IndexerState.FAILED)
        self.assertEqual(third.statuses[2].message, "C: /api answered HTTP 500")
        self.assertIn("C search failed: C: /api answered HTTP 500", logs.output[0])
        self.assertFalse(third.done)

        a.gate.set()
        last = await anext(updates)
        self.assertEqual(
            summary(last),
            [("a", IndexerState.FOUND, 1, None), ("b", IndexerState.EMPTY, 0, None),
             ("c", IndexerState.FAILED, 0, "C: /api answered HTTP 500")],
        )
        self.assertEqual(last.releases, (hit,))
        self.assertTrue(last.done)
        with self.assertRaises(StopAsyncIteration):
            await anext(updates)
        self.assertEqual([a.requests, b.requests, c.requests], [[EPISODE]] * 3)

    async def test_simultaneous_finishes_yield_one_update_each(self) -> None:
        indexers = [fake(name) for name in "abc"]
        service = TorrentSearchService(indexers, settings_service())
        updates = [update async for update in service.search(EPISODE)]
        self.assertEqual([update.done for update in updates], [False, False, False, True])
        self.assertEqual(
            [[status.state for status in update.statuses] for update in updates[1:]],
            [
                [IndexerState.EMPTY, IndexerState.SEARCHING, IndexerState.SEARCHING],
                [IndexerState.EMPTY, IndexerState.EMPTY, IndexerState.SEARCHING],
                [IndexerState.EMPTY] * 3,
            ],
        )

    async def test_skipped_indexers(self) -> None:
        http = self.http()
        movie_only = fake("movieonly", kinds={SearchKind.MOVIE})
        tv_only = fake("tvonly", kinds={SearchKind.EPISODE, SearchKind.SEASON})
        disabled = fake("off")
        odd = fake("odd", kinds={SearchKind.SEASON, SearchKind.MOVIE})
        eztv = EztvIndexer(http)
        prowlarr = ProwlarrIndexer(http, url=lambda: "", api_key=lambda: "")
        indexers: list[BaseIndexer] = [movie_only, tv_only, disabled, odd, eztv, prowlarr]
        service = TorrentSearchService(indexers, settings_service(disabled_indexers="OFF, nope"))

        no_imdb = EPISODE.model_copy(update={"imdb_id": None})
        updates = [update async for update in service.search(no_imdb)]
        self.assertEqual(len(updates), 2)  # only tvonly searches
        self.assertEqual(
            summary(updates[0]),
            [
                ("movieonly", IndexerState.SKIPPED, 0, "movies only"),
                ("tvonly", IndexerState.SEARCHING, 0, None),
                ("off", IndexerState.SKIPPED, 0, "disabled in Settings"),
                ("odd", IndexerState.SKIPPED, 0, "not available for this search"),
                ("eztv", IndexerState.SKIPPED, 0, "needs an IMDb id"),
                ("prowlarr", IndexerState.SKIPPED, 0, "not configured"),
            ],
        )
        self.assertEqual(
            [status.web_url for status in updates[0].statuses[4:]],
            ["https://eztvx.to/search/Severance%20S02E01", None],
        )
        self.assertEqual(updates[1].statuses[1].state, IndexerState.EMPTY)
        self.assertEqual(updates[1].statuses[0], updates[0].statuses[0])
        self.assertEqual(tv_only.requests, [no_imdb])
        self.assertEqual(movie_only.requests + disabled.requests + odd.requests, [])

        movie_updates = [update async for update in service.search(MOVIE)]
        self.assertEqual(
            [(s.name, s.state, s.message) for s in movie_updates[0].statuses],
            [
                ("movieonly", IndexerState.SEARCHING, None),
                ("tvonly", IndexerState.SKIPPED, "TV only"),
                ("off", IndexerState.SKIPPED, "disabled in Settings"),
                ("odd", IndexerState.SEARCHING, None),
                ("eztv", IndexerState.SKIPPED, "TV only"),
                ("prowlarr", IndexerState.SKIPPED, "not configured"),
            ],
        )
        self.assertTrue(all(s.web_url is None for s in movie_updates[0].statuses))  # no queries

    async def test_disabled_wins_over_unsupported(self) -> None:
        service = TorrentSearchService(
            [fake("yts", kinds={SearchKind.MOVIE})], settings_service(disabled_indexers="yts")
        )
        [update] = [update async for update in service.search(EPISODE)]
        self.assertEqual(update.statuses[0].message, "disabled in Settings")

    async def test_nothing_to_search_is_one_final_update(self) -> None:
        service = TorrentSearchService([fake("a")], settings_service(disabled_indexers="a"))
        updates = [update async for update in service.search(EPISODE)]
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0].done)
        self.assertEqual(updates[0].releases, ())
        empty = TorrentSearchService([], settings_service())
        self.assertEqual(
            [update async for update in empty.search(EPISODE)],
            [SearchUpdate(statuses=(), releases=(), done=True)],
        )

    async def test_timeout(self) -> None:
        slow = fake("slow", delay=30)
        fast = fake("fast", result=[release("Severance S02E01 720p-X")])
        service = TorrentSearchService([slow, fast], settings_service(), timeout=0.05)
        with self.assertLogs(LOGGER, "WARNING") as logs:
            updates = [update async for update in service.search(EPISODE)]
        self.assertEqual(
            summary(updates[-1]),
            [("slow", IndexerState.FAILED, 0, "timed out"), ("fast", IndexerState.FOUND, 1, None)],
        )
        self.assertTrue(slow.cancelled)
        self.assertIn("SLOW search timed out", "\n".join(logs.output))
        self.assertEqual(len(updates[-1].releases), 1)

    async def test_unexpected_errors(self) -> None:
        broken = fake("broken", error=RuntimeError("boom"))
        own_timeout = fake("own", error=TimeoutError())  # not the service's deadline
        service = TorrentSearchService([broken, own_timeout], settings_service())
        with self.assertLogs(LOGGER, "ERROR") as logs:
            updates = [update async for update in service.search(EPISODE)]
        self.assertEqual(
            summary(updates[-1]),
            [("broken", IndexerState.FAILED, 0, "unexpected error"),
             ("own", IndexerState.FAILED, 0, "unexpected error")],
        )
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(logs.records[0].getMessage(), "BROKEN search failed unexpectedly")
        self.assertIsNotNone(logs.records[0].exc_info)

    async def test_results_are_merged_filtered_and_sorted_across_updates(self) -> None:
        dup_a = release("Severance S02E01 720p WEB-AAA", source="a", seeders=5)
        dup_b = release("severance.s02e01.720p.web-aaa.mkv", source="b", seeders=50, leechers=9,
                        download_url="https://b.example/1.torrent")
        wrong_episode = release("Severance S02E10 720p WEB-AAA", h=2, source="a", seeders=99)
        cam = release("Severance S02E01 HDCAM-BBB", h=3, source="a", seeders=98)
        too_big = release("Severance S02E01 2160p-DDD", h=5, source="b", seeders=97,
                          size=9000 * MIB)
        other = release("Severance S02E01 1080p WEB-CCC", h=4, source="b", seeders=20)
        a = fake("a", result=[dup_a, wrong_episode, cam], gate=asyncio.Event())
        b = fake("b", result=[dup_b, other, too_big], gate=asyncio.Event())
        service = TorrentSearchService([a, b], settings_service(max_size_mb=8000))
        assert a.gate is not None and b.gate is not None
        async with contextlib.aclosing(service.search(EPISODE)) as updates:
            await anext(updates)
            b.gate.set()  # b finishes first...
            after_b = await anext(updates)
            a.gate.set()
            final = await anext(updates)
        self.assertEqual([r.title for r in after_b.releases], [dup_b.title, other.title])
        self.assertEqual(after_b.hidden, (too_big,))
        self.assertEqual(after_b.statuses[1].count, 3)  # raw hits, before filtering
        # ...but merging follows the indexer order: a's copy is kept, both sources are named.
        self.assertEqual([r.title for r in final.releases], [dup_a.title, other.title])
        merged = final.releases[0]
        self.assertEqual(merged.source, "a+b")
        self.assertEqual((merged.seeders, merged.leechers), (50, 9))
        self.assertEqual(merged.download_url, "https://b.example/1.torrent")
        self.assertEqual(final.statuses[0].count, 3)
        # The irrelevant episode, the HDCAM and the oversized release, most seeders first.
        self.assertEqual(final.hidden, (wrong_episode, cam, too_big))
        self.assertTrue(final.done)

    async def test_settings_are_read_when_the_search_starts(self) -> None:
        settings = settings_service()
        hit = release("Severance S02E01 1080p WEB-A")
        a = fake("a", result=[hit], gate=asyncio.Event())
        service = TorrentSearchService([a], settings)
        assert a.gate is not None
        async with contextlib.aclosing(service.search(EPISODE)) as updates:
            await anext(updates)
            await settings.update(excluded_keywords="1080p", disabled_indexers="a")
            a.gate.set()
            final = await anext(updates)
        self.assertEqual(final.releases, (hit,))
        [later] = [update async for update in service.search(EPISODE)]
        self.assertEqual(later.statuses[0].state, IndexerState.SKIPPED)

    async def test_cancelling_the_consumer_cancels_running_indexers(self) -> None:
        a = fake("a", gate=asyncio.Event())
        b = fake("b", gate=asyncio.Event())
        done = fake("c")
        service = TorrentSearchService([a, b, done], settings_service())
        seen: list[SearchUpdate] = []

        async def consume() -> None:
            async for update in service.search(EPISODE):
                seen.append(update)

        consumer = asyncio.create_task(consume())
        await a.started.wait()
        await b.started.wait()
        while len(seen) < 2:  # the first update, then c's
            await asyncio.sleep(0)
        self.assertEqual(len(running_searches()), 2)
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        self.assertTrue(a.cancelled)
        self.assertTrue(b.cancelled)
        self.assertFalse(done.cancelled)
        self.assertEqual(running_searches(), [])

    async def test_aclose_cancels_running_indexers(self) -> None:
        a = fake("a", gate=asyncio.Event())
        b = fake("b", gate=asyncio.Event())
        service = TorrentSearchService([a, b], settings_service())
        updates = service.search(EPISODE)
        await anext(updates)
        await a.started.wait()
        await b.started.wait()
        await updates.aclose()
        self.assertTrue(a.cancelled)
        self.assertTrue(b.cancelled)
        self.assertEqual(running_searches(), [])

    async def test_aclose_before_the_indexers_ran(self) -> None:
        a = fake("a", gate=asyncio.Event())
        service = TorrentSearchService([a], settings_service())
        updates = service.search(EPISODE)
        await anext(updates)
        await updates.aclose()
        self.assertEqual(running_searches(), [])
        self.assertEqual(a.requests, [])

    async def test_test_prowlarr(self) -> None:
        sent: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=STATUS)

        http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        self.addAsyncCleanup(http.aclose)
        prowlarr = ProwlarrIndexer(http, url=lambda: "http://nas:9696", api_key=lambda: "KEY")
        service = TorrentSearchService([fake("a"), prowlarr], settings_service())
        self.assertEqual(await service.test_prowlarr(), "Prowlarr 1.24.3.4754")
        self.assertEqual(str(sent[0].url), "http://nas:9696/api/v1/system/status")

        unconfigured = ProwlarrIndexer(http, url=lambda: "", api_key=lambda: "")
        with self.assertRaises(IndexerError) as caught:
            await TorrentSearchService([unconfigured], settings_service()).test_prowlarr()
        self.assertIn("not set up", str(caught.exception))
        with self.assertRaises(IndexerError):
            await TorrentSearchService([fake("a")], settings_service()).test_prowlarr()
        self.assertEqual(len(sent), 1)


class RequestBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TorrentSearchService([], settings_service())

    def test_episode_request(self) -> None:
        self.assertEqual(
            self.service.episode_request(SEVERANCE, 2, 1),
            SearchRequest(
                kind=SearchKind.EPISODE,
                title="Severance",
                year=2022,
                season=2,
                episode=1,
                imdb_id="tt11280740",
                queries=("Severance S02E01", "Severance 2022 S02E01"),
            ),
        )
        unknown = Show(id=1, name="Grey's Anatomy")
        request = self.service.episode_request(unknown, 19, 5)
        self.assertEqual((request.year, request.imdb_id), (None, None))
        self.assertEqual(request.queries, ("Grey's Anatomy S19E05", "Greys Anatomy S19E05"))

    def test_season_request(self) -> None:
        request = self.service.season_request(SEVERANCE, 2)
        self.assertEqual(request.kind, SearchKind.SEASON)
        self.assertEqual((request.season, request.episode, request.year), (2, None, 2022))
        self.assertEqual(request.imdb_id, "tt11280740")
        self.assertEqual(
            request.queries,
            build_queries(SearchKind.SEASON, "Severance", year=2022, season=2),
        )

    def test_movie_request(self) -> None:
        movie = Movie(id=278, title="The Shawshank Redemption", release_date=date(1994, 9, 23))
        request = self.service.movie_request(movie)
        self.assertEqual(
            request,
            SearchRequest(
                kind=SearchKind.MOVIE,
                title="The Shawshank Redemption",
                year=1994,
                queries=("The Shawshank Redemption 1994", "The Shawshank Redemption"),
            ),
        )
        details = MovieDetails(id=278, imdb_id="tt0111161")
        self.assertEqual(self.service.movie_request(movie, details).imdb_id, "tt0111161")
        with_imdb = movie.model_copy(update={"imdb_id": "tt0111161"})
        self.assertEqual(self.service.movie_request(with_imdb).imdb_id, "tt0111161")
        no_imdb_details = MovieDetails(id=278)
        self.assertEqual(
            self.service.movie_request(with_imdb, no_imdb_details).imdb_id, "tt0111161"
        )


# --- the whole pipeline, with the real indexers on recorded payloads ----------------------------


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    """The built-in indexers against tests/fixtures/indexers through httpx.MockTransport."""

    async def asyncSetUp(self) -> None:
        self.hosts: list[str] = []
        eztv = load("eztv_show.json")
        # The recorded EZTV page has no S02E01; add the release APIBay and Bitsearch also list.
        eztv["torrents"].append({
            "id": 1, "hash": "A762217EA6A1D6E64FA297259DC14AB53F75D6C7",
            "filename": "Severance.S02E01.1080p.WEB.H264-SuccessfulCrab[EZTVx.to].mkv",
            "title": "Severance S02E01 1080p WEB H264-SuccessfulCrab EZTV",
            "magnet_url": "magnet:?xt=urn:btih:a762217ea6a1d6e64fa297259dc14ab53f75d6c7"
                          "&tr=udp%3A%2F%2Ftracker.example%3A1337",
            "torrent_url": "https://zoink.ch/torrent/Severance.S02E01.1080p.torrent",
            "seeds": 400, "peers": 2000, "size_bytes": "3963487862",
            "season": "2", "episode": "1", "date_released_unix": 1737081301,
        })
        payloads = {
            "apibay.org": load("apibay_episode.json"),
            "eztvx.to": eztv,
            "bitsearch.eu": load("bitsearch_search.json"),
        }

        async def serve(request: httpx.Request) -> httpx.Response:
            self.hosts.append(request.url.host)
            return httpx.Response(200, json=payloads[request.url.host])

        http = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        self.addAsyncCleanup(http.aclose)
        indexers = default_indexers(http, prowlarr_url=lambda: "", prowlarr_api_key=lambda: "")
        self.service = TorrentSearchService(indexers, settings_service())

    async def test_episode(self) -> None:
        request = self.service.episode_request(SEVERANCE, 2, 1)
        updates = [update async for update in self.service.search(request)]
        self.assertEqual(len(updates), 4)  # start + APIBay, EZTV, Bitsearch
        final = updates[-1]
        self.assertTrue(final.done)
        self.assertEqual(
            summary(final),
            [
                ("apibay", IndexerState.FOUND, 15, None),
                ("eztv", IndexerState.FOUND, 1, None),
                ("bitsearch", IndexerState.FOUND, 15, None),
                ("yts", IndexerState.SKIPPED, 0, "movies only"),
                ("prowlarr", IndexerState.SKIPPED, 0, "not configured"),
            ],
        )
        self.assertEqual(sorted(self.hosts), ["apibay.org", "bitsearch.eu", "eztvx.to"])
        releases = final.releases
        # 31 raw hits; 5 releases are on both APIBay and Bitsearch, one on all three.
        self.assertEqual(len(releases), 25)
        self.assertEqual(final.hidden, ())
        self.assertEqual(len({r.info_hash for r in releases}), 25)
        self.assertTrue(all("s02e01" in r.title.lower() for r in releases))
        self.assertEqual(
            [r.seeders for r in releases], sorted((r.seeders for r in releases), reverse=True)
        )
        top = releases[0]
        self.assertEqual(top.info_hash, "a762217ea6a1d6e64fa297259dc14ab53f75d6c7")
        self.assertEqual(top.title, "Severance S02E01 1080p WEB H264-SuccessfulCrab")  # APIBay's
        self.assertEqual(top.source, "apibay+eztv+bitsearch")
        self.assertEqual((top.seeders, top.leechers), (2980, 2000))
        self.assertEqual(
            top.download_url, "https://zoink.ch/torrent/Severance.S02E01.1080p.torrent"
        )
        assert top.magnet_uri is not None
        self.assertIn("tracker.example", top.magnet_uri)
        self.assertEqual(top.info_url, "https://thepiratebay.org/description.php?id=77879319")
        both = [r for r in releases if r.source == "apibay+bitsearch"]
        self.assertEqual(len(both), 4)
        self.assertEqual(updates[0].statuses[0].web_url,
                         "https://thepiratebay.org/search.php?q=Severance+S02E01")

    async def test_season_pack(self) -> None:
        request = self.service.season_request(SEVERANCE, 2)
        updates = [update async for update in self.service.search(request)]
        final = updates[-1]
        self.assertEqual(
            [(s.name, s.state, s.count) for s in final.statuses[:3]],
            [("apibay", IndexerState.FOUND, 15), ("eztv", IndexerState.FOUND, 2),
             ("bitsearch", IndexerState.FOUND, 15)],
        )
        # The recorded APIBay and Bitsearch answers are single episodes: none is a season pack.
        self.assertEqual(
            [r.title for r in final.releases],
            ["Severance S02 1080p x265-ELiTE EZTV", "Severance S02 720p x265-TiPEX EZTV"],
        )
        # The single episodes are hidden, not lost: merged (30 hits, 25 releases), seeders desc.
        self.assertEqual(len(final.hidden), 25)
        self.assertTrue(all("s02e01" in r.title.lower() for r in final.hidden))
        self.assertEqual(
            [r.seeders for r in final.hidden],
            sorted((r.seeders for r in final.hidden), reverse=True),
        )
        self.assertEqual(final.hidden[0].source, "apibay+bitsearch")


class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_search_reads_the_prowlarr_settings(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        asyncio.get_running_loop().slow_callback_duration = 1.0  # real CA bundle load
        services = await Services.create(AppPaths(Path(tmp.name)))
        self.addAsyncCleanup(services.aclose)
        self.assertIsInstance(services.search, TorrentSearchService)
        with self.assertRaises(IndexerError):
            await services.search.test_prowlarr()  # not set up: no request is made

        await services.settings.update(
            prowlarr_url="http://nas.local:9696", prowlarr_api_key="KEY123"
        )
        sent: list[httpx.Request] = []

        async def send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=STATUS)

        with mock.patch.object(httpx.AsyncClient, "send", side_effect=send):
            self.assertEqual(await services.search.test_prowlarr(), "Prowlarr 1.24.3.4754")
        [request] = sent
        self.assertEqual(str(request.url), "http://nas.local:9696/api/v1/system/status")
        self.assertEqual(request.headers["X-Api-Key"], "KEY123")


class StatusModelTests(unittest.TestCase):
    def test_frozen(self) -> None:
        status = IndexerStatus(name="a", label="A", state=IndexerState.WAITING)
        self.assertEqual((status.count, status.message, status.web_url), (0, None, None))
        with self.assertRaises(ValueError):
            status.count = 3  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
