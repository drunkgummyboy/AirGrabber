"""The folder library's reading and recognising (app/services/folder_library/scan.py) and its
TMDB / TVMaze matching (match.py): temporary folders with empty files, a fake clock, a fake TMDB
and a fake LibraryService; never a live TMDB, TVMaze or NAS."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest import mock

from app.models import MediaType, Show
from app.models.folder_library import LibraryFile
from app.models.plex import PlexTitle
from app.models.tmdb import TMDBTitle, TMDBTitleIds
from app.services.folder_library import match, scan
from app.services.folder_library.match import FolderMatch, match_files
from app.services.folder_library.scan import FolderRead, FoundFile, read_folder
from app.services.library import LibraryService
from app.services.tmdb import TMDBClient, TMDBError

FIXTURES = Path(__file__).parent / "fixtures" / "folder_library"
NOW = 1_800_000_000.0  # the fake clock
OLD = NOW - 3600  # written an hour ago: settled
QUIET_S = 600.0


class FolderTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def folder(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(exist_ok=True)
        return path

    def touch(self, folder: Path, relative: str, *, mtime: float = OLD, data: bytes = b"") -> Path:
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
        return path

    async def read(
        self,
        folder: Path | str,
        kind: scan.FolderKind,
        known: dict[str, LibraryFile] | None = None,
        timeout_s: float = 30.0,
    ) -> FolderRead:
        return await read_folder(
            str(folder), kind, known=known or {}, quiet_s=QUIET_S, now=NOW, timeout_s=timeout_s
        )

    def by_name(self, found: FolderRead) -> dict[str, FoundFile]:
        return {Path(file.path).name: file for file in found.files}


class ReadFolderTests(FolderTestCase):
    async def test_tidy_and_raw_names_are_recognised(self) -> None:
        films = self.folder("Films")
        tidy = self.touch(films, "Dune (2021)/Dune (2021).mkv", data=b"12345")
        raw = self.touch(films, "Dune.Part.Two.2024.2160p.WEB-DL.mkv")
        series = self.folder("Series")
        self.touch(series, "Severance/Season 02/Severance - S02E01 - Hello.mkv")

        found = await self.read(films, "movie")
        self.assertTrue(found.reached)
        self.assertEqual(found.folder, str(films))
        files = self.by_name(found)
        self.assertEqual(set(files), {tidy.name, raw.name})
        dune = files[tidy.name]
        self.assertEqual(
            (dune.path, dune.size, dune.mtime, dune.kind, dune.title, dune.year, dune.tmdb_id),
            (str(tidy), 5, OLD, "movie", "Dune", 2021, None),
        )
        self.assertIsNone(dune.quality)
        two = files[raw.name]
        self.assertEqual(
            (two.kind, two.title, two.year, two.quality, two.season),
            ("movie", "Dune Part Two", 2024, "4K", None),
        )

        [episode] = (await self.read(series, "show")).files
        self.assertEqual(
            (episode.kind, episode.title, episode.season, episode.episode, episode.episode_end),
            ("episode", "Severance", 2, 1, None),
        )

    async def test_names_are_read_without_guessit(self) -> None:
        films = self.folder("Films")
        self.touch(films, "Dune.Part.Two.2024.2160p.WEB-DL.mkv")
        series = self.folder("Series")
        self.touch(series, "Severance/Severance.S01E02-E03.1080p.mkv")
        with mock.patch("app.forge.core.parser.parser_ready", return_value=False):
            [film] = (await self.read(films, "movie")).files
            [episode] = (await self.read(series, "show")).files
        self.assertEqual((film.title, film.year, film.quality), ("Dune Part Two", 2024, "4K"))
        self.assertEqual(
            (episode.title, episode.season, episode.episode, episode.episode_end, episode.quality),
            ("Severance", 1, 2, 3, "1080p"),
        )

    async def test_an_episode_without_a_show_folder_goes_by_its_name(self) -> None:
        series = self.folder("Series")
        self.touch(series, "The.Bear.S03E04.720p.WEB.mkv")
        [episode] = (await self.read(series, "show")).files
        self.assertEqual(
            (episode.kind, episode.title, episode.season, episode.episode, episode.quality),
            ("episode", "The Bear", 3, 4, "720p"),
        )

    async def test_a_flat_folder_of_several_shows(self) -> None:
        # MediaForge's "Series (flat)": every show's episodes in one folder, no sub-folders,
        # right in the series folder or one level below it.
        names = (
            "Show A (2022) - S01E02 - Pilot.mkv", "Show B - S03E01 - x.mkv",
            "The.Bear.S03E04.720p.mkv",
        )
        expected = {
            ("episode", "Show A", 2022, 1, 2),
            ("episode", "Show B", None, 3, 1),
            ("episode", "The Bear", None, 3, 4),
        }
        flat = self.folder("Downloads")
        nested = self.folder("Series")
        for name in names:
            self.touch(flat, name)
            self.touch(nested, f"Series (flat)/{name}")
        for guessit in (True, False):
            for folder in (flat, nested):
                with self.subTest(guessit=guessit, folder=folder.name), mock.patch(
                    "app.forge.core.parser.parser_ready", return_value=guessit
                ):
                    found = (await self.read(folder, "show")).files
                    self.assertEqual(
                        {(f.kind, f.title, f.year, f.season, f.episode) for f in found}, expected
                    )
        # Read again unchanged (their stored rows, no parsing): each keeps its own show.
        first = (await self.read(nested, "show")).files
        rows = {
            f.path: LibraryFile(path=f.path, size=f.size, mtime=f.mtime, kind="episode",
                                tmdb_id=1, season=f.season, episode=f.episode, recognised=True)
            for f in first
        }
        with mock.patch.object(scan, "_parse_name", wraps=scan._parse_name) as parse:
            again = (await self.read(nested, "show", rows)).files
        parse.assert_not_called()
        self.assertEqual(
            {(f.title, f.year) for f in again},
            {("Show A", 2022), ("Show B", None), ("The Bear", None)},
        )

    async def test_one_show_named_two_ways_is_still_one_show_folder(self) -> None:
        # A show folder whose files are named by different release groups stays one show.
        series = self.folder("Series")
        self.touch(series, "Doctor Who (2005)/Doctor.Who.2005.S01E01.mkv")
        self.touch(series, "Doctor Who (2005)/Doctor Who (2005) - S01E02 - Rose.mkv")
        self.touch(series, "Doctor Who (2005)/Doctor Who - S01E03.mkv")
        self.touch(series, "Severance (2022)/Season 01/Good News About Hell.mkv")
        self.touch(series, "Severance (2022)/Season 01/Half Loop.mkv")
        # Episode titles without a code name no show: the folder still does.
        for guessit in (True, False):
            with self.subTest(guessit=guessit), mock.patch(
                "app.forge.core.parser.parser_ready", return_value=guessit
            ):
                found = (await self.read(series, "show")).files
                self.assertEqual(len(found), 5)
                self.assertEqual(
                    {(f.title, f.year) for f in found}, {("Doctor Who", 2005), ("Severance", 2022)}
                )

    async def test_a_raw_named_show_folder_is_recognised(self) -> None:
        # The final review's probe: a release's own folder in the series folder.
        series = self.folder("Series")
        raw = "Severance.S02.2160p.ATVP.WEB-DL.DDP5.1.H.265-NTb"
        self.touch(series, f"{raw}/Severance.S02E01.2160p.ATVP.WEB-DL.DDP5.1.H.265-NTb.mkv")
        self.touch(series, "Doctor.Who.2005.S01.1080p.BluRay.x264/Doctor.Who.2005.S01E02.mkv")
        self.touch(series, "The.Bear.2160p.WEB/The.Bear.S03E04.mkv")  # tags, no season code
        self.touch(series, "Season 4 Complete 1080p/Fargo.S04E01.720p.mkv")  # names no show
        tidy = self.folder("Tidy")
        self.touch(tidy, "Severance (2022)/Season 02/Severance - S02E01.mkv")
        self.touch(tidy, "Mr. Robot/Season 01/Mr. Robot - S01E01.mkv")
        for guessit in (True, False):
            with self.subTest(guessit=guessit), mock.patch(
                "app.forge.core.parser.parser_ready", return_value=guessit
            ):
                files = self.by_name(await self.read(series, "show"))
                self.assertEqual(
                    {(f.title, f.year, f.season, f.episode, f.kind) for f in files.values()},
                    {
                        ("Severance", None, 2, 1, "episode"),
                        ("Doctor Who", 2005, 1, 2, "episode"),
                        ("The Bear", None, 3, 4, "episode"),
                        ("Fargo", None, 4, 1, "episode"),
                    },
                )
                # A tidy show folder is read as before.
                found = {
                    (f.title, f.year, f.season, f.episode)
                    for f in (await self.read(tidy, "show")).files
                }
                self.assertEqual(found, {("Severance", 2022, 2, 1), ("Mr Robot", None, 1, 1)})
        # The same title for a file read before (its stored row, no parsing).
        [first] = [f for f in files.values() if f.title == "Severance"]
        row = LibraryFile(
            path=first.path, size=first.size, mtime=first.mtime, kind="episode", tmdb_id=95396,
            season=2, episode=1, recognised=True,
        )
        with mock.patch.object(scan, "_parse_name", wraps=scan._parse_name) as parse:
            again = self.by_name(await self.read(series, "show", {first.path: row}))
        self.assertNotIn(Path(first.path).name, [call.args[0] for call in parse.call_args_list])
        self.assertEqual(again[Path(first.path).name].title, "Severance")

    async def test_a_single_shows_folder_set_as_a_series_folder(self) -> None:
        show = self.folder("Severance (2022)")
        self.touch(show, "Season 01/Severance - S01E01.mkv")
        self.touch(show, "Specials/Severance - S00E01.mkv")
        shutil.copy(FIXTURES / "tvshow.nfo", show / "tvshow.nfo")
        dutch = self.folder("Fargo")
        self.touch(dutch, "Seizoen 2/Fargo - S02E03.mkv")
        found = {
            (f.title, f.year, f.season, f.episode, f.tmdb_id)
            for f in (await self.read(show, "show")).files
        }
        self.assertEqual(
            found, {("Severance", 2022, 1, 1, 95396), ("Severance", 2022, 0, 1, 95396)}
        )
        [fargo] = (await self.read(dutch, "show")).files
        self.assertEqual((fargo.title, fargo.season, fargo.episode), ("Fargo", 2, 3))

    async def test_an_nfo_tmdb_id_wins(self) -> None:
        films = self.folder("Films")
        self.touch(films, "Arrival (2016)/Arrival (2016).mkv")
        shutil.copy(FIXTURES / "movie.nfo", films / "Arrival (2016)" / "Arrival (2016).nfo")
        self.touch(films, "Heat (1995)/Heat (1995).mkv")  # no .nfo: no id
        self.touch(films, "Alien (1979)/Alien.1979.1080p.mkv")
        shutil.copy(FIXTURES / "movie.nfo", films / "Alien (1979)" / "movie.nfo")
        # A movie.nfo right in the films folder is no film's.
        self.touch(films, "Loose.Film.2001.mkv")
        shutil.copy(FIXTURES / "movie.nfo", films / "movie.nfo")
        series = self.folder("Series")
        self.touch(series, "Severance/Season 01/Severance - S01E01.mkv")
        shutil.copy(FIXTURES / "tvshow.nfo", series / "Severance" / "tvshow.nfo")
        # An episode's own .nfo holds the episode's TMDB id, not the show's: never read.
        season = series / "Severance" / "Season 01"
        shutil.copy(FIXTURES / "episode.nfo", season / "Severance - S01E01.nfo")

        files = self.by_name(await self.read(films, "movie"))
        self.assertEqual(files["Arrival (2016).mkv"].tmdb_id, 329865)
        self.assertEqual(files["Arrival (2016).mkv"].title, "Arrival")
        self.assertIsNone(files["Heat (1995).mkv"].tmdb_id)
        self.assertEqual(files["Alien.1979.1080p.mkv"].tmdb_id, 329865)
        self.assertEqual(files["Alien.1979.1080p.mkv"].title, "Alien")
        self.assertIsNone(files["Loose.Film.2001.mkv"].tmdb_id)
        [episode] = (await self.read(series, "show")).files
        self.assertEqual((episode.tmdb_id, episode.season, episode.episode), (95396, 1, 1))

    async def test_files_still_arriving_dont_count(self) -> None:
        films = self.folder("Downloads")
        for name in (
            "Film.2020.1080p.mkv.!qB",
            "Other.2020.mkv.part",
            "Third.2021.mkv.crdownload",
            "Fourth.2021.mkv.!ut",
            "Fifth.2021.mkv.bc!",
            "Sixth.2021.mkv.tmp",
            "Loud.2022.MKV.!QB",
        ):
            self.touch(films, name)
        self.touch(films, "Fresh (2023)/Fresh (2023).mkv", mtime=NOW - 60)  # still being written
        self.touch(films, "Edge (2023)/Edge (2023).mkv", mtime=NOW - QUIET_S + 1)
        done = self.touch(films, "Done (2020)/Done (2020).mkv", mtime=NOW - QUIET_S - 1)
        found = await self.read(films, "movie")
        self.assertEqual([file.path for file in found.files], [str(done)])
        self.assertEqual(found.arriving, 2)  # Fresh and Edge; part-downloads never count

    async def test_a_change_time_in_the_future(self) -> None:
        films = self.folder("Films")
        self.touch(films, "Soon (2023)/Soon (2023).mkv", mtime=NOW + 5)  # just written, clock skew
        far = self.touch(films, "Far (2020)/Far (2020).mkv", mtime=NOW + QUIET_S + 1)
        found = await self.read(films, "movie")
        # Further ahead than QUIET_S counts as settled (else it would be "arriving" forever).
        self.assertEqual([file.path for file in found.files], [str(far)])
        self.assertEqual(found.arriving, 1)

    async def test_unchanged_files_arent_parsed_again(self) -> None:
        films = self.folder("Films")
        raw = self.touch(films, "Dune.Part.Two.2024.2160p.WEB-DL.mkv")
        with mock.patch.object(scan, "_parse_name", wraps=scan._parse_name) as parse:
            [first] = (await self.read(films, "movie")).files
            self.assertEqual(parse.call_count, 1)
            row = LibraryFile(
                path=first.path, size=first.size, mtime=first.mtime, kind="movie",
                tmdb_id=693134, quality="4K", recognised=True,
            )
            [again] = (await self.read(films, "movie", {first.path: row})).files
            self.assertEqual(parse.call_count, 1)  # its row was used
            self.assertEqual(
                (again.path, again.kind, again.tmdb_id, again.quality, again.year),
                (str(raw), "movie", 693134, "4K", 2024),
            )
            self.assertTrue(again.title)
            # A changed file (another size) is read again, its old row not used.
            self.touch(films, raw.name, data=b"more")
            [changed] = (await self.read(films, "movie", {first.path: row})).files
            self.assertEqual(parse.call_count, 2)
            self.assertIsNone(changed.tmdb_id)
            self.assertEqual(changed.size, 4)

    async def test_an_unrecognised_row_is_used_too(self) -> None:
        films = self.folder("Films")
        odd = self.touch(films, "zzz.mkv")
        row = LibraryFile(path=str(odd), size=0, mtime=OLD, recognised=False)
        with mock.patch.object(scan, "_parse_name", wraps=scan._parse_name) as parse:
            [file] = (await self.read(films, "movie", {str(odd): row})).files
        parse.assert_not_called()
        self.assertIsNone(file.tmdb_id)

    async def test_extras_and_samples_are_skipped(self) -> None:
        films = self.folder("Films")
        kept = self.touch(films, "Dune (2021)/Dune (2021).mkv")
        for name in (
            "Dune (2021)/Dune (2021)-sample.mkv",
            "Dune (2021)/Sample/dune.mkv",
            "Dune (2021)/Dune (2021)-trailer.mp4",
            "Dune (2021)/Extras/Making of.mkv",
            "Dune (2021)/Featurettes/Sandworms.mkv",
            "Dune (2021)/Behind The Scenes/Set.mkv",
            "Dune (2021)/poster.jpg",
            "Dune (2021)/Dune (2021).en.srt",
            "@eaDir/Dune (2021).mkv/thumb.mkv",
            "#recycle/Old (1990)/Old (1990).mkv",
            "Duplicates/Dune (2021)/Dune (2021).mkv",
        ):
            self.touch(films, name)
        found = await self.read(films, "movie")
        self.assertEqual([file.path for file in found.files], [str(kept)])

    async def test_an_unreachable_folder_keeps_its_last_result(self) -> None:
        gone = self.root / "Asleep" / "Films"
        row = LibraryFile(
            path=str(gone / "Dune (2021)" / "Dune (2021).mkv"), size=1, mtime=OLD, kind="movie",
            tmdb_id=438631, recognised=True,
        )
        with self.assertLogs("app.services.folder_library.scan", logging.INFO) as logs:
            found = await self.read(gone, "movie", {row.path: row})
        # Not reached: nothing found, and the caller keeps the stored rows.
        self.assertEqual(found, FolderRead(folder=str(gone), reached=False, files=()))
        self.assertNotIn("Asleep", "\n".join(logs.output))
        # A NAS dropping out half way through counts the same.
        films = self.folder("Films")
        self.touch(films, "Dune (2021)/Dune (2021).mkv")
        with mock.patch.object(scan, "_walk", side_effect=OSError(64, "gone", str(films))):
            dropped = await self.read(films, "movie")
        self.assertEqual((dropped.reached, dropped.files), (False, ()))

    async def test_a_films_nfo_wins_over_a_name_that_cant_be_read(self) -> None:
        films = self.folder("Films")
        self.touch(films, "Odd/xyz.mkv")
        shutil.copy(FIXTURES / "movie.nfo", films / "Odd" / "xyz.nfo")
        self.touch(films, "Odder/abc.mkv")  # no .nfo either
        with mock.patch.object(scan, "_parse_name", return_value=None):
            files = self.by_name(await self.read(films, "movie"))
        self.assertEqual(
            (files["xyz.mkv"].kind, files["xyz.mkv"].tmdb_id, files["xyz.mkv"].title),
            ("movie", 329865, ""),
        )
        self.assertEqual((files["abc.mkv"].kind, files["abc.mkv"].tmdb_id), (None, None))

    def failing_folder(self, bad: str, error: BaseException) -> Any:
        """The walk's listing, failing for the folder named `bad` (the walk seam)."""
        real = os.scandir

        def listing(path: str) -> Any:
            if os.path.basename(path) == bad:
                raise error
            return real(path)

        return mock.patch.object(scan, "_scandir", listing)

    async def test_a_bad_folder_below_is_left_out(self) -> None:
        films = self.folder("Films")
        kept = self.touch(films, "Dune (2021)/Dune (2021).mkv")
        self.touch(films, "Broken (2000)/Broken (2000).mkv")
        for error in (
            PermissionError(13, "denied"),
            FileNotFoundError(2, "deleted meanwhile"),
            NotADirectoryError(20, "not a folder"),
            OSError(None, "path too long", None, 206),  # ERROR_FILENAME_EXCED_RANGE
            OSError(None, "can't reach the junction's target", None, 1920),
        ):
            with self.subTest(error=error):
                with (
                    self.failing_folder("Broken (2000)", error),
                    self.assertLogs("app.services.folder_library.scan", logging.INFO) as logs,
                ):
                    found = await self.read(films, "movie")
                self.assertTrue(found.reached)
                self.assertEqual([file.path for file in found.files], [str(kept)])
                said = "\n".join(logs.output)
                self.assertIn("1 entries below it couldn't be read", said)
                self.assertNotIn("Broken", said)

    async def test_the_share_going_away_is_not_reached(self) -> None:
        films = self.folder("Films")
        self.touch(films, "Dune (2021)/Dune (2021).mkv")
        self.touch(films, "Heat (1995)/Heat (1995).mkv")
        for error in (
            OSError(None, "network name no longer available", None, 64),
            OSError(None, "bad network path", None, 53),
            OSError(None, "semaphore timeout", None, 121),
            TimeoutError("no answer"),
        ):
            with self.subTest(error=error), self.failing_folder("Heat (1995)", error):
                found = await self.read(films, "movie")
                self.assertEqual((found.reached, found.files), (False, ()))
        # Every network error Windows gives for a share that went away (the final review's
        # list too): the whole folder isn't reached, never half of it.
        for code in (21, 51, 53, 54, 55, 58, 59, 64, 65, 67, 121, 240, 1167, 1222, 1225, 1231,
                     1232, 1236, 2250):
            error = OSError(None, "the share went away", None, code)
            with self.subTest(code=code), self.failing_folder("Heat (1995)", error):
                found = await self.read(films, "movie")
                self.assertEqual((found.reached, found.files), (False, ()))
        # The folder itself gone half way through, whatever the error says.
        real = os.scandir

        def vanishing(path: str) -> Any:
            if os.path.basename(path) == "Heat (1995)":
                shutil.rmtree(films)
                raise FileNotFoundError(2, "gone")
            return real(path)

        with mock.patch.object(scan, "_scandir", vanishing):
            found = await self.read(films, "movie")
        self.assertEqual((found.reached, found.files), (False, ()))

    async def test_a_broken_junction_is_left_out(self) -> None:
        try:
            import _winapi
        except ImportError:
            self.skipTest("junctions are Windows'")
        films = self.folder("Films")
        kept = self.touch(films, "Dune (2021)/Dune (2021).mkv")
        target = self.folder("Elsewhere")
        _winapi.CreateJunction(str(target), str(films / "Gone (1999)"))
        target.rmdir()  # the junction now points nowhere
        found = await self.read(films, "movie")
        self.assertTrue(found.reached)
        self.assertEqual([file.path for file in found.files], [str(kept)])

    async def test_an_empty_folder_is_reached(self) -> None:
        found = await self.read(self.folder("Empty"), "movie")
        self.assertEqual((found.reached, found.files), (True, ()))

    async def test_a_slow_folder_gives_up_after_its_limit(self) -> None:
        films = self.folder("Films")
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(root: str, progress: Any = None) -> list[tuple[str, int, float]]:
            release.wait(30)  # a NAS that took the connection and never answers
            return []

        started = time.monotonic()
        with mock.patch.object(scan, "_walk", stuck):
            found = await self.read(films, "movie", timeout_s=0.1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((found.reached, found.files, found.slow), (False, (), False))


class LongReadTests(FolderTestCase):
    """The final review's finding 1: a read's limit is time without a step forward (a listing,
    an entry, an .nfo), with an overall cap; a read past it is used once it ends."""

    def test_the_limit_is_mediaforges(self) -> None:
        from app.forge.core import auto

        self.assertEqual(scan.READ_TIMEOUT_S, auto.LOOK_STEP_TIMEOUT_S)

    def walking(
        self, steps: int, *, pause: float = 0.02, stall: threading.Event | None = None
    ) -> Any:
        """A walk taking `steps` steps `pause` apart (a large or cold NAS), then, with `stall`,
        stopping until it is set; then the real walk."""
        real = scan._walk

        def walk(root: str, progress: Any = None) -> list[tuple[str, int, float]]:
            for _ in range(steps):
                time.sleep(pause)
                if progress is not None:
                    progress.steps += 1
            if stall is not None:
                stall.wait(30)
            return real(root, progress)

        return mock.patch.object(scan, "_walk", walk)

    async def test_a_read_that_keeps_going_is_reached(self) -> None:
        films = self.folder("Films")
        self.touch(films, "Dune (2021)/Dune (2021).mkv")
        with self.walking(40):  # 0.8 s in all, far past the limit, never 0.15 s without a step
            found = await self.read(films, "movie", timeout_s=0.15)
        self.assertEqual((found.reached, found.slow, len(found.files)), (True, False, 1))

    async def test_a_read_that_stops_going_gives_up(self) -> None:
        films = self.folder("Films")
        stall = threading.Event()
        self.addCleanup(stall.set)
        started = time.monotonic()
        with self.walking(3, stall=stall):
            found = await self.read(films, "movie", timeout_s=0.15)
        self.assertLess(time.monotonic() - started, 5)
        # It answered: slow, not "can't be reached".
        self.assertEqual((found.reached, found.files, found.slow), (False, (), True))

    async def test_a_read_past_its_cap_gives_up(self) -> None:
        films = self.folder("Films")
        stop = threading.Event()
        self.addCleanup(stop.set)
        with self.walking(60, pause=0.01, stall=stop):  # 0.6 s of steps, past the cap
            found = await read_folder(
                str(films), "movie", known={}, quiet_s=QUIET_S, now=NOW, timeout_s=0.2, max_s=0.3
            )
        self.assertEqual((found.reached, found.slow), (False, True))

    async def test_a_long_read_that_ends_is_used(self) -> None:
        films = self.folder("Films")
        self.touch(films, "Dune (2021)/Dune (2021).mkv")
        stall = threading.Event()
        self.addCleanup(stall.set)
        ended: list[int] = []
        long_reads = scan.LongReads(on_ended=lambda: ended.append(1))
        walks: list[str] = []
        real = scan._walk

        def walk(root: str, progress: Any = None) -> list[tuple[str, int, float]]:
            walks.append(root)
            if progress is not None:
                progress.steps += 1  # the share answered …
            stall.wait(30)  # … then stopped a while (a NAS spinning up)
            return real(root, progress)

        async def read() -> FolderRead:
            return await read_folder(
                str(films), "movie", known={}, quiet_s=QUIET_S, now=NOW, timeout_s=0.1,
                busy=long_reads,
            )

        with mock.patch.object(scan, "_walk", walk):
            first = await read()
            self.assertEqual((first.reached, first.slow), (False, True))
            again = await read()  # its read still runs: not read twice
            self.assertEqual((again.reached, again.slow, len(walks)), (False, True, 1))
            stall.set()
            deadline = time.monotonic() + 10
            while not ended and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(ended, [1])  # the caller hears it (a check soon)
            late = await read()  # its result, not a read of its own
            self.assertEqual((late.reached, len(late.files), len(walks)), (True, 1, 1))
            fresh = await read()  # used once: the next one reads
            self.assertTrue(fresh.reached)
            self.assertEqual(len(walks), 2)


# -- matching ----------------------------------------------------------------------------------

DUNE = TMDBTitle(id=438631, title="Dune", date="2021-09-15", poster_path="/dune.jpg")
ARRIVAL = "F:\\Arrival (2016)\\Arrival (2016).mkv"
SEVERANCE = TMDBTitle(id=95396, title="Severance", date="2022-02-17", poster_path="/sev.jpg")


class FakeTMDB:
    """find_titles / title_ids from tables; records every call."""

    def __init__(
        self,
        searches: dict[tuple[MediaType, str, int | None], list[TMDBTitle]] | None = None,
        details: dict[tuple[MediaType, int], TMDBTitleIds] | None = None,
        fail: Iterable[str] = (),
    ) -> None:
        self.searches = searches or {}
        self.details = details or {}
        self.fail = set(fail)
        self.searched: list[tuple[MediaType, str, int | None]] = []
        self.detailed: list[tuple[MediaType, int]] = []

    async def find_titles(
        self, kind: MediaType, query: str, *, year: int | None = None, language: str | None = None
    ) -> list[TMDBTitle]:
        self.searched.append((kind, query, year))
        if query in self.fail:
            raise TMDBError("TMDB can't be reached")
        return self.searches.get((kind, query, year), [])

    async def title_ids(self, kind: MediaType, tmdb_id: int) -> TMDBTitleIds | None:
        self.detailed.append((kind, tmdb_id))
        return self.details.get((kind, tmdb_id))


class FakeLibrary:
    """find_show by TheTVDB id or name; `cached`: the shows cache's ids."""

    def __init__(self, shows: dict[int | str, Show], cached: Iterable[int]) -> None:
        self.shows = shows
        self.cached = set(cached)
        self.asked: list[tuple[int | None, str | None, str]] = []

    async def find_show(
        self,
        *,
        show_id: int | None = None,
        tvdb_id: int | None = None,
        imdb_id: str | None = None,
        name: str = "",
    ) -> Show | None:
        self.asked.append((tvdb_id, imdb_id, name))
        if tvdb_id is not None and tvdb_id in self.shows:
            return self.shows[tvdb_id]
        return self.shows.get(name)

    async def cached_shows(self, show_ids: Iterable[int]) -> dict[int, Show]:
        return {i: Show(id=i, name=str(i)) for i in show_ids if i in self.cached}


def film(path: str, title: str = "Dune", year: int | None = 2021, **values: Any) -> FoundFile:
    fields: dict[str, Any] = {"size": 10, "mtime": OLD, "kind": "movie", "season": None,
                              "episode": None, "episode_end": None, "quality": None,
                              "tmdb_id": None}
    fields.update(values)
    return FoundFile(path=path, title=title, year=year, **fields)


def episode(
    path: str, season: int | None, number: int | None, title: str = "Severance", **values: Any
) -> FoundFile:
    fields: dict[str, Any] = {"size": 10, "mtime": OLD, "episode_end": None, "quality": None,
                              "tmdb_id": None, "year": None}
    fields.update(values)
    return FoundFile(path=path, kind="episode", title=title, season=season, episode=number, **fields)


def dune_ids() -> TMDBTitleIds:
    return TMDBTitleIds.model_validate(
        {"id": 438631, "title": "Dune", "release_date": "2021-09-15", "poster_path": "/dune.jpg",
         "imdb_id": "tt1160419"}
    )


def severance_ids() -> TMDBTitleIds:
    return TMDBTitleIds.model_validate(
        {"id": 95396, "name": "Severance", "first_air_date": "2022-02-17", "poster_path": "/sev.jpg",
         "external_ids": {"imdb_id": "tt11280740", "tvdb_id": 371980}}
    )


class MatchTests(unittest.IsolatedAsyncioTestCase):
    def tmdb(self, **values: Any) -> FakeTMDB:
        values.setdefault(
            "searches",
            {
                (MediaType.MOVIE, "Dune", 2021): [DUNE],
                (MediaType.TV, "Severance", None): [SEVERANCE],
            },
        )
        values.setdefault(
            "details",
            {(MediaType.MOVIE, 438631): dune_ids(), (MediaType.TV, 95396): severance_ids()},
        )
        return FakeTMDB(**values)

    async def match(
        self,
        files: list[FoundFile],
        tmdb: FakeTMDB,
        library: FakeLibrary,
        cached: dict[str, LibraryFile] | None = None,
        titles: dict[str, PlexTitle] | None = None,
    ) -> FolderMatch:
        return await match_files(
            files,
            tmdb=cast(TMDBClient, tmdb),
            library=cast(LibraryService, library),
            cached=cached or {},
            titles=titles,
        )

    async def test_titles_become_library_titles(self) -> None:
        files = [
            film("F:\\Films\\Dune (2021)\\Dune (2021).mkv", quality="1080p", mtime=OLD),
            film("G:\\Films\\Dune.2021.2160p.mkv", quality="4K", mtime=OLD - 100),
            episode("S:\\Severance\\Season 01\\Severance - S01E01.mkv", 1, 1, mtime=OLD - 50),
            episode("S:\\Severance\\Season 01\\Severance - S01E02-E03.mkv", 1, 2, episode_end=3,
                    quality="1080p"),
            episode("S:\\Severance\\Season 02\\Severance - S02E01.mkv", 2, 1, quality="720p"),
        ]
        library = FakeLibrary({371980: Show(id=44933, name="Severance")}, cached={44933})
        found = await self.match(files, self.tmdb(), library)
        titles = {title.key: title for title in found.scan.titles}
        self.assertEqual(
            titles["folder:movie:438631"],
            PlexTitle(
                kind="movie", key="folder:movie:438631", title="Dune", year=2021, thumb="/dune.jpg",
                added_at=datetime.fromtimestamp(OLD - 100, UTC), resolution="4K",
                imdb_id="tt1160419", tmdb_id=438631,
            ),
        )
        self.assertEqual(
            titles["folder:show:95396"],
            PlexTitle(
                kind="show", key="folder:show:95396", title="Severance", year=2022,
                thumb="/sev.jpg", added_at=datetime.fromtimestamp(OLD - 50, UTC), seasons=2,
                episodes=4, resolution="1080p", show_id=44933, tvdb_id=371980,
                imdb_id="tt11280740", tmdb_id=95396,
            ),
        )
        self.assertEqual(library.asked, [(371980, "tt11280740", "Severance")])
        self.assertEqual(found.scan.films, frozenset({(438631, "tt1160419")}))
        self.assertEqual(
            found.scan.episodes,
            frozenset({(44933, 1, 1), (44933, 1, 2), (44933, 1, 3), (44933, 2, 1)}),
        )
        self.assertEqual((found.scan.shows, found.scan.unmatched_shows), (1, 0))
        self.assertEqual(found.scan.unmatched_films, 0)
        self.assertEqual(found.unrecognised, ())
        rows = {row.path: row for row in found.rows}
        self.assertEqual(set(rows), {file.path for file in files})
        self.assertEqual(
            rows["S:\\Severance\\Season 01\\Severance - S01E02-E03.mkv"],
            LibraryFile(
                path="S:\\Severance\\Season 01\\Severance - S01E02-E03.mkv", size=10, mtime=OLD,
                kind="episode", tmdb_id=95396, season=1, episode=2, episode_end=3,
                quality="1080p", recognised=True,
            ),
        )
        self.assertTrue(all(row.recognised for row in found.rows))

    async def test_episodes_count_only_for_known_shows(self) -> None:
        other = TMDBTitle(id=1, title="Unknown Show")
        nobody = TMDBTitle(id=2, title="Nobody Knows")
        tmdb = self.tmdb()
        tmdb.searches[(MediaType.TV, "Unknown Show", None)] = [other]
        tmdb.searches[(MediaType.TV, "Nobody Knows", None)] = [nobody]
        files = [
            episode("S:\\Severance\\S01E01.mkv", 1, 1),
            episode("S:\\Unknown Show\\S01E01.mkv", 1, 1, title="Unknown Show"),
            episode("S:\\Nobody Knows\\S01E01.mkv", 1, 1, title="Nobody Knows"),
        ]
        library = FakeLibrary(
            {371980: Show(id=44933, name="Severance"), "Unknown Show": Show(id=7, name="Unknown")},
            cached={44933},
        )
        found = await self.match(files, tmdb, library)
        self.assertEqual(found.scan.episodes, frozenset({(44933, 1, 1)}))
        self.assertEqual((found.scan.shows, found.scan.unmatched_shows), (3, 2))
        show_ids = {title.tmdb_id: title.show_id for title in found.scan.titles}
        self.assertEqual(show_ids, {95396: 44933, 1: 7, 2: None})
        # Without details the title is TMDB's search hit's.
        self.assertEqual(
            {title.tmdb_id: title.title for title in found.scan.titles},
            {95396: "Severance", 1: "Unknown Show", 2: "Nobody Knows"},
        )

    async def test_one_search_per_new_title(self) -> None:
        tmdb = self.tmdb()
        library = FakeLibrary({371980: Show(id=44933, name="Severance")}, cached={44933})
        cached_row = LibraryFile(
            path="F:\\Heat (1995)\\Heat (1995).mkv", size=10, mtime=OLD, kind="movie",
            tmdb_id=949, recognised=True,
        )
        unknown_row = LibraryFile(
            path="F:\\zzz.mkv", size=10, mtime=OLD, kind="movie", recognised=False
        )
        files = [
            episode("S:\\Severance\\S01E01.mkv", 1, 1),
            episode("S:\\Severance\\S01E02.mkv", 1, 2),
            episode("S:\\Severance\\S02E01.mkv", 2, 1),
            film("F:\\Dune (2021)\\Dune (2021).mkv"),
            film("G:\\Dune (2021)\\Dune (2021).mkv"),
            film("F:\\Heat (1995)\\Heat (1995).mkv", title="Heat", year=1995),  # cached
            film("F:\\zzz.mkv", title="zzz", year=None),  # cached as not recognised
            film(ARRIVAL, title="Arrival", year=2016, tmdb_id=329865),
        ]
        cached = {cached_row.path: cached_row, unknown_row.path: unknown_row}
        found = await self.match(files, tmdb, library, cached)
        self.assertCountEqual(
            tmdb.searched, [(MediaType.TV, "Severance", None), (MediaType.MOVIE, "Dune", 2021)]
        )
        rows = {row.path: row for row in found.rows}
        self.assertEqual(rows["F:\\Heat (1995)\\Heat (1995).mkv"].tmdb_id, 949)
        self.assertEqual(rows[ARRIVAL].tmdb_id, 329865)
        self.assertFalse(rows["F:\\zzz.mkv"].recognised)
        # A changed file (another size) is a new one.
        changed = film("F:\\Heat (1995)\\Heat (1995).mkv", title="Heat", year=1995, size=11)
        tmdb.searched.clear()
        await self.match([changed], tmdb, library, cached)
        self.assertEqual(tmdb.searched, [(MediaType.MOVIE, "Heat", 1995)])

        # The next check: everything cached, the titles known: no search, no TVMaze; only the
        # two titles TMDB gave no details for are asked about again.
        tmdb.searched.clear()
        tmdb.detailed.clear()
        library.asked.clear()
        again = await self.match(
            files, tmdb, library, {row.path: row for row in found.rows},
            {title.key: title for title in found.scan.titles},
        )
        self.assertEqual((tmdb.searched, library.asked), ([], []))
        self.assertCountEqual(
            tmdb.detailed, [(MediaType.MOVIE, 949), (MediaType.MOVIE, 329865)]
        )
        self.assertEqual(again.scan, found.scan)
        self.assertEqual(again.rows, found.rows)

    async def test_an_unreached_folders_rows_match_as_before(self) -> None:
        tmdb = self.tmdb()
        library = FakeLibrary({371980: Show(id=44933, name="Severance")}, cached={44933})
        files = [
            film("F:\\Dune (2021)\\Dune (2021).mkv", quality="4K"),
            episode("S:\\Severance\\S01E01.mkv", 1, 1),
        ]
        first = await self.match(files, tmdb, library)
        tmdb.searched.clear()
        tmdb.detailed.clear()
        library.asked.clear()
        # The NAS asleep: the stored rows stand in for what would have been read.
        stored = {row.path: row for row in first.rows}
        again = await self.match(
            [scan.from_row(row) for row in first.rows], tmdb, library, stored,
            {title.key: title for title in first.scan.titles},
        )
        self.assertEqual((tmdb.searched, tmdb.detailed, library.asked), ([], [], []))
        self.assertEqual(again.scan, first.scan)
        self.assertEqual(again.rows, first.rows)

    async def test_a_title_made_without_details_gets_them_later(self) -> None:
        arrival = "F:\\Odd\\xyz.mkv"
        files = [
            film(arrival, title="", year=None, tmdb_id=329865),  # .nfo id, name unreadable
            episode("S:\\Severance\\S01E01.mkv", 1, 1, tmdb_id=95396),  # tvshow.nfo id
        ]
        tmdb = self.tmdb(details={})  # TMDB offline, or no key
        library = FakeLibrary({371980: Show(id=44933, name="Severance")}, cached={44933})
        first = await self.match(files, tmdb, library)
        self.assertEqual(
            {(t.tmdb_id, t.thumb, t.imdb_id, t.tvdb_id, t.show_id) for t in first.scan.titles},
            {(329865, None, None, None, None), (95396, None, None, None, None)},
        )
        self.assertEqual(first.scan.episodes, frozenset())
        # The next check, TMDB back: the details are asked for and the title is made whole.
        tmdb.details = {
            (MediaType.MOVIE, 329865): TMDBTitleIds.model_validate(
                {"id": 329865, "title": "Arrival", "release_date": "2016-11-10",
                 "poster_path": "/arr.jpg", "imdb_id": "tt2543164"}
            ),
            (MediaType.TV, 95396): severance_ids(),
        }
        prior = {title.key: title for title in first.scan.titles}
        stored = {row.path: row for row in first.rows}
        second = await self.match(files, tmdb, library, stored, prior)
        titles = {title.tmdb_id: title for title in second.scan.titles}
        self.assertEqual(
            (titles[329865].title, titles[329865].year, titles[329865].thumb,
             titles[329865].imdb_id),
            ("Arrival", 2016, "/arr.jpg", "tt2543164"),
        )
        self.assertEqual(
            (titles[95396].thumb, titles[95396].tvdb_id, titles[95396].show_id),
            ("/sev.jpg", 371980, 44933),
        )
        self.assertEqual(second.scan.episodes, frozenset({(44933, 1, 1)}))
        self.assertEqual(second.scan.films, frozenset({(329865, "tt2543164")}))
        # Whole now: the check after asks nothing more.
        tmdb.detailed.clear()
        library.asked.clear()
        third = await self.match(
            files, tmdb, library, stored, {title.key: title for title in second.scan.titles}
        )
        self.assertEqual((tmdb.detailed, library.asked), ([], []))
        self.assertEqual(third.scan, second.scan)

    async def test_every_file_is_in_a_title_or_listed(self) -> None:
        files = [
            film("F:\\Odd\\xyz.mkv", title="", year=None, tmdb_id=329865),  # .nfo, no name
            film("F:\\x\\kindless.mkv", title="", year=None, kind=None, tmdb_id=5),
            episode("S:\\Severance\\Making of.mkv", None, None, tmdb_id=95396),
            episode("S:\\Severance\\S01E01.mkv", 1, 1, tmdb_id=95396),
        ]
        found = await self.match(files, self.tmdb(), FakeLibrary({}, cached=()))
        self.assertEqual(
            found.unrecognised, ("F:\\x\\kindless.mkv", "S:\\Severance\\Making of.mkv")
        )
        self.assertEqual(
            {title.key for title in found.scan.titles},
            {"folder:movie:329865", "folder:show:95396"},
        )
        self.assertEqual(found.scan.titles[0].episodes, None)  # the film
        [show] = [title for title in found.scan.titles if title.kind == "show"]
        self.assertEqual(show.episodes, 1)  # "Making of" isn't counted as an episode
        rows = {row.path: row for row in found.rows}
        self.assertEqual(set(rows), {file.path for file in files})
        self.assertEqual(
            {path for path, row in rows.items() if not row.recognised}, set(found.unrecognised)
        )

    async def test_unrecognised_files_are_listed(self) -> None:
        tmdb = self.tmdb()
        files = [
            film("F:\\Dune (2021)\\Dune (2021).mkv"),
            film("F:\\Nothing (1999)\\Nothing (1999).mkv", title="Nothing", year=1999),
            film("F:\\12345.mkv", title="", year=None, kind=None),
        ]
        found = await self.match(files, tmdb, FakeLibrary({}, cached=()))
        self.assertEqual(
            found.unrecognised, ("F:\\12345.mkv", "F:\\Nothing (1999)\\Nothing (1999).mkv")
        )
        self.assertEqual(found.scan.unmatched_films, 1)  # the film TMDB doesn't know
        rows = {row.path: row for row in found.rows}
        self.assertFalse(rows["F:\\Nothing (1999)\\Nothing (1999).mkv"].recognised)
        self.assertFalse(rows["F:\\12345.mkv"].recognised)
        self.assertTrue(rows["F:\\Dune (2021)\\Dune (2021).mkv"].recognised)
        self.assertEqual([title.tmdb_id for title in found.scan.titles], [438631])

    async def test_a_tmdb_failure_is_tried_again_next_time(self) -> None:
        tmdb = self.tmdb(fail={"Dune"})
        files = [film("F:\\Dune (2021)\\Dune (2021).mkv")]
        with self.assertLogs("app.services.folder_library.match", logging.INFO) as logs:
            found = await self.match(files, tmdb, FakeLibrary({}, cached=()))
        # Neither stored nor called unrecognised: the next check asks again.
        self.assertEqual((found.rows, found.unrecognised, found.scan.titles), ((), (), ()))
        self.assertNotIn("Dune", "\n".join(logs.output))

    async def test_without_details_a_known_id_still_counts(self) -> None:
        tmdb = self.tmdb(details={})
        files = [film(ARRIVAL, title="Arrival", year=2016, tmdb_id=329865)]
        found = await self.match(files, tmdb, FakeLibrary({}, cached=()))
        [title] = found.scan.titles
        self.assertEqual(
            (title.key, title.title, title.year, title.thumb, title.tmdb_id),
            ("folder:movie:329865", "Arrival", 2016, None, 329865),
        )
        self.assertEqual(found.scan.films, frozenset({(329865, None)}))


class ModuleTests(unittest.TestCase):
    def test_title_keys(self) -> None:
        self.assertEqual(match.title_key("movie", 438631), "folder:movie:438631")
        self.assertEqual(match.title_key("show", 95396), "folder:show:95396")

    def test_reading_loads_no_forge_code_at_import(self) -> None:
        from tests.forge.test_layering import module_level_imports

        for name in ("scan.py", "match.py", "__init__.py"):
            path = Path(scan.__file__).parent / name
            imported = module_level_imports(path)
            self.assertFalse(
                [module for module in imported if module.startswith("app.forge")], name
            )


if __name__ == "__main__":
    unittest.main()
