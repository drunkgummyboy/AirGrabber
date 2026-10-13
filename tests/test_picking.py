"""pick_best / rank_releases: fake filters, dead torrents, pack fallback and quality profiles."""

from __future__ import annotations

import unittest
from typing import Any

from app.models import (
    QualityProfile,
    SearchKind,
    SearchRequest,
    Settings,
    TorrentRelease,
)
from app.services.picking import (
    is_likely_fake,
    is_multi_episode,
    is_pre_release,
    pick_best,
    quality_rank,
    rank_releases,
)

MIB = 1024 * 1024
GIB = 1024 * MIB

EPISODE = SearchRequest(kind=SearchKind.EPISODE, title="Severance", year=2022, season=2, episode=1)
MOVIE = SearchRequest(kind=SearchKind.MOVIE, title="Dune Part Two", year=2024)
DEFAULTS = Settings()  # 1080p, excluded: cam, telesync, hdcam, sample

_counter = iter(range(1, 1_000_000))


def rel(title: str, *, seeders: int = 10, size: int = 2 * GIB, **fields: Any) -> TorrentRelease:
    """A release with its own info hash."""
    return TorrentRelease(
        title=title,
        source=fields.pop("source", "apibay"),
        seeders=seeders,
        size_bytes=size,
        info_hash=f"{next(_counter):040x}",
        **fields,
    )


def titles(releases: list[TorrentRelease]) -> list[str]:
    return [release.title for release in releases]


def best_title(
    releases: list[TorrentRelease],
    request: SearchRequest = EPISODE,
    settings: Settings = DEFAULTS,
) -> str | None:
    best = pick_best(releases, request, settings)
    return best.title if best else None


class FakeWordTests(unittest.TestCase):
    def test_fake_words_as_extensions_or_words(self) -> None:
        for title in (
            "Severance.S02E01.1080p.WEB.H264.mkv.exe",
            "Severance S02E01 1080p WEB x264 [scr]",
            "Severance.S02E01.1080p.bat",
            "Severance S02E01 1080p Setup.msi",
            "Severance.S02E01.1080p.mp4.lnk",
            "Severance.S02E01.1080p.WEB.zip",
            "Severance S02E01 1080p WEB RAR",
            "Severance S02E01 1080p WEB Passworded",
            "Severance S02E01 1080p WEB (password in txt)",
            "Severance S02E01 1080p WEB Codec Pack Required",
        ):
            with self.subTest(title):
                self.assertTrue(is_likely_fake(rel(title), EPISODE))

    def test_words_inside_words_are_fine(self) -> None:
        for title in (
            "Severance.S02E01.1080p.WEB.H264-RARBG",  # "rarbg" is not "rar"
            "Severance.S02E01.Zipper.1080p.WEB.H264-GRP",
            "Severance.S02E01.1080p.WEB.H264-EXEcutive",
            "Severance.S02E01.1080p.WEB.H264-NTb",
        ):
            with self.subTest(title):
                self.assertFalse(is_likely_fake(rel(title), EPISODE))

    def test_a_fake_word_in_the_title_itself_is_fine(self) -> None:
        password = SearchRequest(kind=SearchKind.EPISODE, title="Password", season=1, episode=3)
        self.assertFalse(is_likely_fake(rel("Password.2022.S01E03.1080p.WEB.h264"), password))
        # ...but another fake word still counts.
        self.assertTrue(is_likely_fake(rel("Password.2022.S01E03.1080p.WEB.exe"), password))

    def test_fakes_are_never_picked(self) -> None:
        fake = rel("Severance.S02E01.1080p.WEB.mkv.exe", seeders=5000)
        real = rel("Severance.S02E01.1080p.WEB.H264-NTb", seeders=3)
        self.assertEqual(best_title([fake, real]), real.title)
        self.assertIsNone(best_title([fake]))


class FakeSizeTests(unittest.TestCase):
    def assert_floor(
        self, request: SearchRequest, title: str, too_small_mib: float, fine_mib: float
    ) -> None:
        with self.subTest(title):
            small = rel(title, size=round(too_small_mib * MIB))
            fine = rel(title, size=round(fine_mib * MIB))
            self.assertTrue(is_likely_fake(small, request), f"{too_small_mib} MiB")
            self.assertFalse(is_likely_fake(fine, request), f"{fine_mib} MiB")

    def test_episode_floors(self) -> None:
        self.assert_floor(EPISODE, "Severance S02E01 720p WEB", 99, 101)
        self.assert_floor(EPISODE, "Severance S02E01 1080p WEB", 249, 251)
        self.assert_floor(EPISODE, "Severance S02E01 2160p WEB", 699, 701)
        self.assert_floor(EPISODE, "Severance S02E01 WEB", 79, 81)  # no resolution
        self.assert_floor(EPISODE, "Severance S02E01 480p WEB", 79, 81)  # unknown floor too

    def test_season_requests_use_the_episode_floors(self) -> None:
        season = SearchRequest(kind=SearchKind.SEASON, title="Severance", season=2)
        self.assert_floor(season, "Severance S02 1080p WEB", 249, 251)

    def test_movie_floors(self) -> None:
        self.assert_floor(MOVIE, "Dune Part Two 2024 720p WEB", 399, 401)
        self.assert_floor(MOVIE, "Dune Part Two 2024 1080p WEB", 699, 701)
        self.assert_floor(MOVIE, "Dune Part Two 2024 2160p WEB", 2047, 2049)
        self.assert_floor(MOVIE, "Dune Part Two 2024 WEB", 299, 301)

    def test_hevc_episode_floors(self) -> None:
        self.assert_floor(EPISODE, "Severance S02E01 720p WEB x265-A", 59, 61)
        self.assert_floor(EPISODE, "Severance S02E01 1080p WEB HEVC-A", 119, 121)
        self.assert_floor(EPISODE, "Severance S02E01 2160p WEB H.265-A", 349, 351)
        self.assert_floor(EPISODE, "Severance S02E01 WEB x265-A", 49, 51)  # no resolution
        self.assert_floor(EPISODE, "Severance S02E01 480p WEB x265-A", 49, 51)

    def test_hevc_movie_floors(self) -> None:
        self.assert_floor(MOVIE, "Dune Part Two 2024 720p WEB x265", 249, 251)
        self.assert_floor(MOVIE, "Dune Part Two 2024 1080p WEB HEVC", 399, 401)
        self.assert_floor(MOVIE, "Dune Part Two 2024 2160p WEB H265", 1023, 1025)
        self.assert_floor(MOVIE, "Dune Part Two 2024 WEB x265", 199, 201)

    def test_a_small_1080p_x265_episode_is_picked(self) -> None:
        # The real MeGusta release the x264 floors used to skip.
        request = SearchRequest(
            kind=SearchKind.EPISODE,
            title="Georgie & Mandy's First Marriage",
            season=3,
            episode=1,
        )
        megusta = rel(
            "Georgie and Mandys First Marriage S03E01 1080p HEVC x265-MeGusta",
            seeders=220,
            size=206 * MIB,
        )
        tiny_x264 = rel(
            "Georgie and Mandys First Marriage S03E01 1080p WEB H264-FAKE",
            seeders=900,
            size=206 * MIB,
        )
        self.assertFalse(is_likely_fake(megusta, request))
        self.assertTrue(is_likely_fake(tiny_x264, request))
        self.assertEqual(best_title([tiny_x264, megusta], request), megusta.title)

    def test_unknown_size_is_allowed(self) -> None:
        self.assertFalse(is_likely_fake(rel("Severance S02E01 1080p WEB", size=0), EPISODE))
        self.assertFalse(is_likely_fake(rel("Dune Part Two 2024 2160p", size=0), MOVIE))

    def test_unknown_size_ranks_below_a_known_one(self) -> None:
        unknown = rel("Severance S02E01 1080p WEB-A", size=0, seeders=50)
        known = rel("Severance S02E01 1080p WEB-B", size=900 * MIB, seeders=50)
        self.assertEqual(titles(rank_releases([unknown, known], EPISODE, DEFAULTS)), [
            known.title,
            unknown.title,
        ])
        # Seeders still come first, and an unknown size alone is picked.
        busier = rel("Severance S02E01 1080p WEB-C", size=0, seeders=51)
        self.assertEqual(best_title([known, busier]), busier.title)
        self.assertEqual(best_title([unknown]), unknown.title)

    def test_too_small_is_never_picked(self) -> None:
        tiny = rel("Severance S02E01 1080p WEB-A", size=40 * MIB, seeders=900)
        real = rel("Severance S02E01 720p WEB-B", size=600 * MIB, seeders=2)
        self.assertEqual(best_title([tiny, real]), real.title)


class PreReleaseTests(unittest.TestCase):
    NO_KEYWORDS = Settings(excluded_keywords="")  # the filter doesn't depend on them

    def test_pre_release_tags_in_every_spelling(self) -> None:
        for tag in (
            "CAM", "CAMRip", "CAM-Rip", "HDCAM", "HDCAMRip",
            "TS", "HDTS", "HD-TS", "TSRip", "TELESYNC", "PDVD", "PreDVD",
            "TC", "HDTC", "TELECINE",
            "SCR", "DVDScr", "DVD-SCR", "SCREENER", "BDSCR", "WEBSCR",
        ):
            title = f"Dune.Part.Two.2024.1080p.{tag}.x264-GRP"
            with self.subTest(tag):
                self.assertTrue(is_pre_release(rel(title), MOVIE))
                self.assertIsNone(best_title([rel(title)], MOVIE, self.NO_KEYWORDS))

    def test_dvdscr_and_hdts_are_skipped_for_a_web_dl(self) -> None:
        request = SearchRequest(kind=SearchKind.MOVIE, title="Bethlehem Kudumba Unit", year=2026)
        dvdscr = rel(
            "www.5MovieRulz.company - Bethlehem Kudumba Unit (2026) 1080p Telugu DVDScr - x264 "
            "- AAC - 2.8GB.mkv",
            seeders=1447,
            size=2800 * MIB,
        )
        hdts = rel("Bethlehem Kudumba Unit 2026 1080p HDTS x264-GRP", seeders=900)
        web = rel(
            "Bethlehem Kudumba Unit (2026) [1080p] [web] [x264] [YTS]", seeders=91, size=2 * GIB
        )
        ranked = rank_releases([dvdscr, hdts, web], request, self.NO_KEYWORDS)
        self.assertEqual(titles(ranked), [web.title])

    def test_words_that_merely_contain_a_tag_are_fine(self) -> None:
        for title in (
            "Scream.2022.1080p.WEB-DL.DDP5.1.H.264-FLUX",
            "Scrubs.S02E01.1080p.WEB",
            "Camden.S02E01.1080p.WEB",
            "Ghosts.S02E01.1080p.WEB",
            "Severance.S02E01.Description.1080p.WEB",
            "Severance.S02E01.1080p.WEB.H264-TCPGRP",
            "Severance.S02E01.1080p.BluRay.M2TS",
        ):
            with self.subTest(title):
                self.assertFalse(is_pre_release(rel(title), EPISODE))

    def test_a_film_named_after_a_tag(self) -> None:
        cam = SearchRequest(kind=SearchKind.MOVIE, title="Cam", year=2018)
        web = rel("Cam.2018.1080p.NF.WEB-DL.DDP5.1.x264-NTG")
        self.assertFalse(is_pre_release(web, cam))
        self.assertEqual(best_title([web], cam, self.NO_KEYWORDS), web.title)
        self.assertTrue(is_pre_release(rel("Cam.2018.HDTS.x264"), cam))


class SeederTests(unittest.TestCase):
    def test_dead_torrents_are_skipped_when_any_candidate_has_seeders(self) -> None:
        dead = rel("Severance S02E01 1080p WEB-A", seeders=0)
        alive = rel("Severance S02E01 480p WEB-B", seeders=1, size=300 * MIB)
        self.assertEqual(titles(rank_releases([dead, alive], EPISODE, DEFAULTS)), [alive.title])

    def test_all_dead_still_picks(self) -> None:
        a = rel("Severance S02E01 720p WEB-A", seeders=0)
        b = rel("Severance S02E01 1080p WEB-B", seeders=0)
        self.assertEqual(best_title([a, b]), b.title)

    def test_seeders_only_count_among_real_candidates(self) -> None:
        # The fake's seeders don't make the dead real release go away.
        fake = rel("Severance S02E01 1080p WEB.exe", seeders=999)
        dead = rel("Severance S02E01 1080p WEB-A", seeders=0)
        self.assertEqual(best_title([fake, dead]), dead.title)

    def test_more_seeders_first(self) -> None:
        releases = [
            rel("Severance S02E01 1080p WEB-A", seeders=5),
            rel("Severance S02E01 1080p WEB-B", seeders=50),
            rel("Severance S02E01 1080p WEB-C", seeders=20),
        ]
        self.assertEqual(
            titles(rank_releases(releases, EPISODE, DEFAULTS)),
            ["Severance S02E01 1080p WEB-B", "Severance S02E01 1080p WEB-C",
             "Severance S02E01 1080p WEB-A"],
        )


class PackTests(unittest.TestCase):
    def test_multi_episode_titles(self) -> None:
        for title in (
            "Severance.S02E01E02.1080p.WEB",
            "Severance.S02E01-E10.1080p.WEB",
            "Severance S02E01 - E10 1080p",
            "Severance.S02E01-10.1080p.WEB",
            "Severance.S02E01-S02E10.1080p",
            "Severance 2x01-2x02 720p",
        ):
            with self.subTest(title):
                self.assertTrue(is_multi_episode(rel(title)))

    def test_single_episode_titles(self) -> None:
        for title in (
            "Severance.S02E01.1080p.WEB.H264-NTb",
            "Severance.S02E01.720p.WEB",
            "Severance.S02E01.2160p.10bit.HDR.DV",
            "Severance.S02E01.2025.1080p",
            "Severance S02E01 Hello Ms Cobel 1080p ATVP WEB-DL DDP5 1 Atmos H 264-FLUX",
            "Severance.2x01.720p.HDTV.x264",
            "Severance.S02E01.1080p.x265-ELiTE",
        ):
            with self.subTest(title):
                self.assertFalse(is_multi_episode(rel(title)))

    def test_a_single_episode_beats_a_pack(self) -> None:
        pack = rel("Severance.S02E01-E10.1080p.WEB-GRP", seeders=900, size=20 * GIB)
        single = rel("Severance.S02E01.720p.WEB-GRP", seeders=3, size=500 * MIB)
        self.assertEqual(best_title([pack, single]), single.title)

    def test_a_pack_when_no_single_episode_survives(self) -> None:
        pack = rel("Severance.S02E01-E10.1080p.WEB-GRP", seeders=900, size=20 * GIB)
        for singles in (
            [],
            [rel("Severance.S02E01.1080p.WEB.exe")],  # fake
            [rel("Severance.S02E01.1080p.WEB-A", size=10 * MIB)],  # too small
            [rel("Severance.S02E01.1080p.WEB-A", seeders=0)],  # dead while the pack is alive
            [rel("Severance.S02E01.1080p.CAM-A")],  # excluded keyword
        ):
            with self.subTest(singles=titles(singles)):
                self.assertEqual(best_title([*singles, pack]), pack.title)

    def test_a_pack_must_hold_the_episode(self) -> None:
        other = rel("Severance.S02E03-E10.1080p.WEB-GRP", size=20 * GIB)
        whole_season = rel("Severance.S02.1080p.WEB-GRP", size=20 * GIB)
        self.assertIsNone(best_title([other, whole_season]))

    def test_season_requests_take_packs(self) -> None:
        season = SearchRequest(kind=SearchKind.SEASON, title="Severance", season=2)
        pack = rel("Severance.S02.1080p.WEB-GRP", size=20 * GIB)
        self.assertEqual(best_title([pack], season), pack.title)


class QualityTests(unittest.TestCase):
    RELEASES = (
        ("2160p", "Severance S02E01 2160p WEB H265-A"),
        ("1080p", "Severance S02E01 1080p WEB H264-B"),
        ("1080p x265", "Severance S02E01 1080p WEB x265-C"),
        ("720p", "Severance S02E01 720p WEB H264-D"),
        ("720p x265", "Severance S02E01 720p WEB HEVC-E"),
        ("480p", "Severance S02E01 480p WEB H264-F"),
        ("unknown", "Severance S02E01 WEB H264-G"),
    )

    def order(self, profile: QualityProfile, wanted: tuple[str, ...] | None = None) -> list[str]:
        names = dict((title, name) for name, title in self.RELEASES)
        releases = [
            rel(title, seeders=10, size=3 * GIB)
            for name, title in self.RELEASES
            if wanted is None or name in wanted
        ]
        ranked = rank_releases(releases, EPISODE, Settings(quality=profile))
        return [names[release.title] for release in ranked]

    def test_1080p(self) -> None:
        # 1080p, then 720p, then 2160p, then the rest (equal: their order stays).
        self.assertEqual(
            self.order(QualityProfile.FULL_HD),
            ["1080p", "1080p x265", "720p", "720p x265", "2160p", "480p", "unknown"],
        )

    def test_720p(self) -> None:
        self.assertEqual(
            self.order(QualityProfile.HD),
            ["720p", "720p x265", "1080p", "1080p x265", "480p", "2160p", "unknown"],
        )

    def test_2160p(self) -> None:
        self.assertEqual(
            self.order(QualityProfile.UHD),
            ["2160p", "1080p", "1080p x265", "720p", "720p x265", "480p", "unknown"],
        )

    def test_x265(self) -> None:
        # 1080p x265 is best, then the other HEVC releases, then the rest in the 1080p order.
        self.assertEqual(
            self.order(QualityProfile.HEVC),
            ["1080p x265", "720p x265", "2160p", "1080p", "720p", "480p", "unknown"],
        )

    def test_any_has_no_quality_preference(self) -> None:
        releases = [
            rel("Severance S02E01 480p WEB-A", seeders=30, size=300 * MIB),
            rel("Severance S02E01 2160p WEB-B", seeders=10, size=5 * GIB),
            rel("Severance S02E01 1080p WEB-C", seeders=20),
        ]
        ranked = rank_releases(releases, EPISODE, Settings(quality=QualityProfile.ANY))
        self.assertEqual(
            titles(ranked),
            ["Severance S02E01 480p WEB-A", "Severance S02E01 1080p WEB-C",
             "Severance S02E01 2160p WEB-B"],
        )

    def test_quality_beats_seeders(self) -> None:
        exact = rel("Severance S02E01 1080p WEB-A", seeders=2)
        popular = rel("Severance S02E01 720p WEB-B", seeders=2000, size=800 * MIB)
        self.assertEqual(best_title([popular, exact]), exact.title)

    def test_quality_rank_values(self) -> None:
        self.assertEqual(quality_rank(rel("Severance S02E01 1080p"), QualityProfile.FULL_HD), 0)
        self.assertEqual(quality_rank(rel("Severance S02E01 4K"), QualityProfile.UHD), 0)
        self.assertEqual(quality_rank(rel("Severance S02E01 4K"), QualityProfile.ANY), 0)
        self.assertEqual(quality_rank(rel("Severance S02E01 1080p HEVC"), QualityProfile.HEVC), 0)


class PreferredGroupTests(unittest.TestCase):
    def test_preferred_group_beats_seeders_but_not_quality(self) -> None:
        settings = Settings(preferred_groups="ntb")
        plain = rel("Severance.S02E01.1080p.WEB.H264-FLUX", seeders=500)
        preferred = rel("Severance.S02E01.1080p.WEB.H264-NTb", seeders=5)
        preferred_720 = rel("Severance.S02E01.720p.WEB.H264-NTb", seeders=900, size=900 * MIB)
        self.assertEqual(
            titles(rank_releases([plain, preferred_720, preferred], EPISODE, settings)),
            [preferred.title, plain.title, preferred_720.title],
        )


class ShownReleasesOnlyTests(unittest.TestCase):
    def test_irrelevant_and_excluded_releases_are_ignored(self) -> None:
        wrong_episode = rel("Severance.S02E02.1080p.WEB-A", seeders=900)
        other_show = rel("Succession.S02E01.1080p.WEB-B", seeders=900)
        cam = rel("Severance.S02E01.1080p.CAM-C", seeders=900)
        sample = rel("Severance.S02E01.1080p.WEB.Sample-D", seeders=900)
        good = rel("Severance.S02E01.1080p.WEB-E", seeders=1)
        releases = [wrong_episode, other_show, cam, sample, good]
        self.assertEqual(titles(rank_releases(releases, EPISODE, DEFAULTS)), [good.title])

    def test_movies(self) -> None:
        wrong_year = rel("Dune Part Two 2019 1080p BluRay", seeders=900, size=8 * GIB)
        episode = rel("Dune Part Two S01E01 1080p", seeders=900, size=8 * GIB)
        good = rel("Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.H.264-FLUX", seeders=40, size=8 * GIB)
        tiny = rel("Dune.Part.Two.2024.1080p.WEB-DL-YIFY", seeders=4000, size=600 * MIB)
        self.assertEqual(best_title([wrong_year, episode, tiny, good], MOVIE), good.title)

    def test_nothing(self) -> None:
        self.assertIsNone(best_title([]))
        self.assertIsNone(best_title([rel("Something Else S02E01 1080p")]))


if __name__ == "__main__":
    unittest.main()
