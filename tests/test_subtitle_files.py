"""Finding a grab's videos in the finished-downloads folder: names, episodes, season packs,
films, samples, partial and growing files; and writing a subtitle without overwriting."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from app.models import DownloadRecord, MediaType
from app.services.subtitle_files import (
    SETTLE_S,
    FolderScan,
    VideoFile,
    grab_files,
    has_subtitle,
    movie_identity,
    normalize,
    parse_episode,
    release_name,
    scan_folder,
    subtitle_path,
    write_new_file,
)


def tv(release: str, season: int, episode: int | None) -> DownloadRecord:
    return DownloadRecord(
        id=1, media_type=MediaType.TV, media_id=7, season=season, episode=episode,
        release_title=release,
    )  # fmt: skip


def movie(release: str) -> DownloadRecord:
    return DownloadRecord(id=2, media_type=MediaType.MOVIE, media_id=9, release_title=release)


def video(*parts: str, growing: bool = False) -> VideoFile:
    return VideoFile(path="D:\\dl\\" + "\\".join(parts), parts=parts, size=1, growing=growing)


def scan(*videos: VideoFile, partial: tuple[tuple[str, ...], ...] = ()) -> FolderScan:
    return FolderScan(videos=list(videos), partial=list(partial))


class NameTests(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(
            normalize("Marvel's.Agents.of.S.H.I.E.L.D.S01E02.720p-GRP[eztv.re]"),
            "marvels agents of s h i e l d s01e02 720p grp",
        )
        self.assertEqual(normalize("Law & Order"), "law and order")
        self.assertEqual(normalize("www.Torrenting.com - Show.S01E02"), "show s01e02")

    def test_release_name_drops_video_and_partial_extensions(self) -> None:
        self.assertEqual(release_name("Show.S01E02.1080p.mkv"), "show s01e02 1080p")
        self.assertEqual(release_name("Show.S01E02.1080p.mkv.!qB"), "show s01e02 1080p")
        self.assertEqual(release_name("Show.S01E02.1080p"), "show s01e02 1080p")

    def test_parse_episode(self) -> None:
        self.assertEqual(parse_episode("Show.S02E04.1080p.mkv"), (2, 4))
        self.assertEqual(parse_episode("show 2x04 title.mkv"), (2, 4))
        self.assertEqual(parse_episode("Show.S01E01E02.mkv"), (1, 1))
        self.assertEqual(parse_episode("Show S01 E10"), (1, 10))
        self.assertIsNone(parse_episode("Dune.Part.Two.2024.1080p.mkv"))

    def test_movie_identity(self) -> None:
        self.assertEqual(
            movie_identity("Dune.Part.Two.2024.1080p.WEB-DL.x264"), ("dune part two", "2024")
        )
        self.assertEqual(movie_identity("Blade Runner 2049 (2017) 2160p"), ("blade runner 2049", "2017"))
        self.assertIsNone(movie_identity("Untitled.Release.1080p"))


class EpisodeMatchTests(unittest.TestCase):
    RELEASE = "Severance.S02E04.1080p.WEB.h264-ETHEL[eztv.re]"

    def test_a_folder_named_like_the_release(self) -> None:
        found = grab_files(
            tv(self.RELEASE, 2, 4),
            scan(video("Severance.S02E04.1080p.WEB.h264-ETHEL", "severance.s02e04.1080p.mkv")),
        )
        self.assertEqual([(v.season, v.episode) for v in found.videos], [(2, 4)])
        self.assertFalse(found.incomplete)

    def test_a_single_file_named_like_the_release(self) -> None:
        found = grab_files(tv(self.RELEASE, 2, 4), scan(video("Severance.S02E04.1080p.WEB.h264-ETHEL.mkv")))
        self.assertEqual(len(found.videos), 1)

    def test_a_renamed_file_found_by_show_name_and_episode(self) -> None:
        files = scan(
            video("TV", "Severance - S02E04 - Woe's Hollow.mkv"),
            video("TV", "Severance - S02E05.mkv"),
            video("TV", "Severance Sisters S02E04.mkv"),
            video("Severance 2022 S02E04.mkv"),
        )
        found = grab_files(tv(self.RELEASE, 2, 4), files, show_name="Severance")
        self.assertEqual(
            sorted(v.video.parts[-1] for v in found.videos),
            ["Severance - S02E04 - Woe's Hollow.mkv", "Severance 2022 S02E04.mkv"],
        )

    def test_without_the_show_name_only_the_release_name_counts(self) -> None:
        found = grab_files(tv(self.RELEASE, 2, 4), scan(video("Severance - S02E04.mkv")))
        self.assertEqual(found.videos, ())

    def test_another_episode_in_a_folder_named_like_the_release_is_not_it(self) -> None:
        found = grab_files(
            tv(self.RELEASE, 2, 4),
            scan(video("Severance.S02E04.1080p.WEB.h264-ETHEL", "Severance.S02E05.mkv")),
        )
        self.assertEqual(found.videos, ())

    def test_samples_are_never_videos(self) -> None:
        folder = "Severance.S02E04.1080p.WEB.h264-ETHEL"
        files = scan(video(folder, "Sample", "severance.s02e04.sample.mkv"))
        self.assertEqual(grab_files(tv(self.RELEASE, 2, 4), files).videos, ())

    def test_growing_and_partial_files_make_the_grab_incomplete(self) -> None:
        folder = "Severance.S02E04.1080p.WEB.h264-ETHEL"
        growing = grab_files(tv(self.RELEASE, 2, 4), scan(video(folder, "s02e04.mkv", growing=True)))
        self.assertEqual(growing.videos, ())
        self.assertTrue(growing.incomplete)
        partial = grab_files(
            tv(self.RELEASE, 2, 4), scan(partial=((f"{folder}.mkv.!qB",),))
        )
        self.assertTrue(partial.incomplete)

    def test_nothing_on_disk(self) -> None:
        found = grab_files(tv(self.RELEASE, 2, 4), scan())
        self.assertEqual(found.videos, ())
        self.assertFalse(found.incomplete)


class SeasonPackTests(unittest.TestCase):
    RELEASE = "The.Bear.S03.1080p.WEB.h264-ETHEL"

    def test_every_episode_of_the_season_with_its_own_number(self) -> None:
        files = scan(
            video(self.RELEASE, "The.Bear.S03E01.mkv"),
            video(self.RELEASE, "The.Bear.S03E02.mkv"),
            video(self.RELEASE, "Extras", "Behind the scenes.mkv"),  # no episode number
            video(self.RELEASE, "The.Bear.S02E10.mkv"),  # another season
        )
        found = grab_files(tv(self.RELEASE, 3, None), files)
        self.assertEqual(sorted((v.season, v.episode) for v in found.videos), [(3, 1), (3, 2)])

    def test_a_pack_found_by_show_name(self) -> None:
        files = scan(video("Shows", "The Bear", "The Bear S03E07.mkv"))
        found = grab_files(tv(self.RELEASE, 3, None), files, show_name="The Bear")
        self.assertEqual([(v.season, v.episode) for v in found.videos], [(3, 7)])


class MovieMatchTests(unittest.TestCase):
    RELEASE = "Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.x264-GRP"

    def test_a_folder_named_like_the_release(self) -> None:
        found = grab_files(movie(self.RELEASE), scan(video(self.RELEASE, "dune.mkv")))
        self.assertEqual(len(found.videos), 1)
        self.assertIsNone(found.videos[0].episode)

    def test_a_renamed_film_found_by_title_and_year(self) -> None:
        files = scan(
            video("Movies", "Dune Part Two (2024)", "Dune Part Two (2024).mkv"),
            video("Movies", "Dune (2021).mkv"),
            video("Movies", "Dune Part Two Making Of 2024.mkv"),
        )
        found = grab_files(movie(self.RELEASE), files)
        self.assertEqual([v.video.parts[-1] for v in found.videos], ["Dune Part Two (2024).mkv"])

    def test_an_episode_file_is_never_a_film(self) -> None:
        files = scan(video(self.RELEASE, "Dune.Prophecy.S01E01.mkv"))
        self.assertEqual(grab_files(movie(self.RELEASE), files).videos, ())


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.old = time.time() - 3600

    def make(self, relative: str, *, recent: bool = False) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        if not recent:
            os.utime(path, (self.old, self.old))
        return path

    def test_what_a_scan_finds(self) -> None:
        self.make("Show.S01E01/show.s01e01.mkv")
        self.make("Show.S01E01/Sample/sample.mkv")
        self.make("Show.S01E01/show.s01e01.en.srt")
        self.make("Show.S01E01/show.nfo")
        self.make("Show.S01E02.mkv.!qB")
        self.make("New.S01E03.mkv", recent=True)
        self.make("@eaDir/Show.S01E01/thumb.mkv")
        self.make("#recycle/old.mkv")
        self.make(".hidden/x.mkv")
        found = scan_folder(self.root, now=time.time())
        self.assertEqual(
            sorted(v.parts for v in found.videos),
            [("New.S01E03.mkv",), ("Show.S01E01", "show.s01e01.mkv")],
        )
        growing = {v.parts[-1]: v.growing for v in found.videos}
        self.assertEqual(growing, {"New.S01E03.mkv": True, "show.s01e01.mkv": False})
        self.assertEqual(found.partial, [("Show.S01E02.mkv.!qB",)])
        self.assertEqual(
            found.subtitles, {str(self.root / "Show.S01E01" / "show.s01e01.en.srt").lower()}
        )
        self.assertFalse(found.truncated)

    def test_depth_and_size_are_bounded(self) -> None:
        self.make("a/b/c/d/deep.mkv")
        self.make("a/b/c/d/e/too.deep.mkv")
        found = scan_folder(self.root, now=time.time())
        self.assertEqual([v.parts[-1] for v in found.videos], ["deep.mkv"])
        for index in range(30):
            self.make(f"many/{index}.mkv")
        self.assertTrue(scan_folder(self.root, now=time.time(), max_entries=10).truncated)

    def test_an_unreachable_folder_raises(self) -> None:
        with self.assertRaises(OSError):
            scan_folder(self.root / "missing", now=time.time())

    def test_settle_time(self) -> None:
        self.assertGreaterEqual(SETTLE_S, 120)


class SubtitleFileTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_subtitle_goes_next_to_the_video(self) -> None:
        self.assertEqual(
            subtitle_path(r"\\NAS\video\Show.S01E02.mkv", "pt-BR"), r"\\NAS\video\Show.S01E02.pt-BR.srt"
        )

    def test_an_existing_subtitle_counts_in_any_variant(self) -> None:
        video_path = r"D:\dl\Show.S01E02.mkv"
        existing = {r"d:\dl\show.s01e02.en.forced.srt", r"d:\dl\show.s01e02.nl.ass"}
        self.assertTrue(has_subtitle(video_path, "en", existing))
        self.assertTrue(has_subtitle(video_path, "nl", existing))
        self.assertFalse(has_subtitle(video_path, "de", existing))
        self.assertFalse(has_subtitle(r"D:\dl\Show.S01E03.mkv", "en", existing))

    def test_written_once_never_over_an_existing_file(self) -> None:
        target = str(self.root / "Show.S01E02.en.srt")
        self.assertTrue(write_new_file(target, b"first"))
        self.assertFalse(write_new_file(target, b"second"))
        self.assertEqual(Path(target).read_bytes(), b"first")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["Show.S01E02.en.srt"])

    def test_an_unwritable_folder_raises(self) -> None:
        with self.assertRaises(OSError):
            write_new_file(str(self.root / "missing" / "x.en.srt"), b"x")


if __name__ == "__main__":
    unittest.main()
