"""Playing the folder library (app/services/folder_library/playing.py) and ▶ Play's services by
source (app/services/playback.py, app/services/plex_library.py): a title's files from
`library_files`, the best copy (Plex's `play_rank` rule), per-person places in `watch_places`,
Continue watching per person, the next episode from the library, and Plex as before when it is
connected. A real temporary database and temporary files; nothing reaches the network, no
player starts."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from app.database import Database
from app.i18n_nl import NL
from app.models import Settings
from app.models.folder_library import LibraryFile
from app.models.plex import PlexScan, PlexTitle, PlexVideo
from app.services.folder_library import playing as playing_module
from app.services.folder_library.match import title_key
from app.services.folder_library.playing import (
    MIN_PLACE_MS,
    WATCHED_SHARE,
    FolderPlaying,
    from_folders,
    item_key,
)
from app.services.local_files import MIN_PLACE_MS as LOCAL_MIN_PLACE_MS
from app.services.playback import (
    LibraryFolders,
    Located,
    PlaybackService,
    PlayOutcome,
    PlayProblem,
    next_episode,
)
from app.services.plex_library import PlexLibraryService
from app.services.settings import SettingsService
from app.ui import media_player
from app.ui.media_player import next_in_show
from tests.test_plex_playing import PlayingTestCase

DUNE_FILE = "/volume1/video/Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv"  # Plex's

NOW = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)
DUNE_ID, ARRIVAL_ID, SEVERANCE_ID = 438631, 329865, 95396
DUNE = PlexTitle(
    kind="movie", key=title_key("movie", DUNE_ID), title="Dune", year=2021, thumb="/dune.jpg",
    tmdb_id=DUNE_ID, resolution="4K",
)
ARRIVAL = PlexTitle(
    kind="movie", key=title_key("movie", ARRIVAL_ID), title="Arrival", year=2016,
    thumb="/arr.jpg", tmdb_id=ARRIVAL_ID, resolution="720p",
)
SEVERANCE = PlexTitle(
    kind="show", key=title_key("show", SEVERANCE_ID), title="Severance", year=2022,
    thumb="/sev.jpg", tmdb_id=SEVERANCE_ID, show_id=44933, seasons=2, episodes=5,
)


async def nothing() -> LibraryFolders | None:
    return None  # MediaForge's folders: the folder library never needs them


class FolderTestCase(unittest.IsolatedAsyncioTestCase):
    """Films in two folders (Dune as 1080p and 4K, Arrival 720p) and a series folder
    (Severance: S01E01, S01E02, the double S01E03-E04, S02E01), read and stored as a check
    would; the folders are the library (no Plex token)."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = await Database.open(self.root / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.films, self.films_4k = self.root / "Films", self.root / "UHD"  # 4K: by path, last
        self.series = self.root / "Series"
        self.dune_1080 = self.films / "Dune (2021)" / "Dune.2021.1080p.mkv"
        self.dune_4k = self.films_4k / "Dune (2021)" / "Dune.2021.2160p.mkv"
        self.arrival = self.films / "Arrival (2016)" / "Arrival.2016.720p.mkv"
        season = self.series / "Severance (2022)" / "Season 01"
        self.episodes = {
            (1, 1): season / "Severance.S01E01.mkv",
            (1, 2): season / "Severance.S01E02.mkv",
            (1, 3): season / "Severance.S01E03-E04.mkv",
            (2, 1): self.series / "Severance (2022)" / "Season 02" / "Severance.S02E01.mkv",
        }
        rows = [
            self.film_row(self.dune_1080, DUNE_ID, "1080p", 2_000),
            self.film_row(self.dune_4k, DUNE_ID, "4K", 5_000),
            self.film_row(self.arrival, ARRIVAL_ID, "720p", 1_000),
            *(
                LibraryFile(
                    path=str(path), size=900, mtime=1.0, kind="episode", tmdb_id=SEVERANCE_ID,
                    season=season_number, episode=number,
                    episode_end=4 if number == 3 else None, quality="1080p", recognised=True,
                )
                for (season_number, number), path in self.episodes.items()
            ),
            LibraryFile(path=str(self.films / "home video.mkv"), size=10, mtime=1.0),
        ]
        for row in rows:
            Path(row.path).parent.mkdir(parents=True, exist_ok=True)
            Path(row.path).write_bytes(b"video")
        await self.db.library_files.replace_folder(str(self.root), rows)
        await self.db.plex.replace(PlexScan(titles=(DUNE, ARRIVAL, SEVERANCE)), NOW)
        self.settings = SettingsService(
            self.db.settings,
            Settings(
                folder_library_on=True,
                film_folders=(str(self.films), str(self.films_4k)),
                series_folders=(str(self.series),),
            ),
        )
        self.now = NOW
        self.playing = FolderPlaying(
            self.settings, self.db.library_files, self.db.watch_places, self.db.plex,
            now=lambda: self.now,
        )
        self.person = 1

    @staticmethod
    def film_row(path: Path, tmdb_id: int, quality: str, size: int) -> LibraryFile:
        return LibraryFile(
            path=str(path), size=size, mtime=1.0, kind="movie", tmdb_id=tmdb_id,
            quality=quality, recognised=True,
        )

    def playback(self) -> PlaybackService:
        return PlaybackService(
            cast(Any, _NoPlex()), nothing, folder_library=self.playing, person=lambda: self.person
        )

    def later(self, minutes: int = 1) -> None:
        self.now += timedelta(minutes=minutes)


class _NoPlex:
    """PlexService without a token: what PlaybackService and PlexLibraryService ask of it."""

    connected = False

    async def titles(self) -> list[PlexTitle]:
        return []

    async def checked_at(self) -> datetime | None:
        return NOW

    async def continue_watching(self) -> list[Any]:
        raise AssertionError("Plex isn't asked without a token")


class FolderPlayingTests(FolderTestCase):
    async def test_a_film_plays_from_its_file_at_the_persons_place(self) -> None:
        playback = self.playback()
        self.assertTrue(self.playing.on)
        self.assertTrue(playback.available)
        title = await playback.film(ARRIVAL_ID)
        self.assertEqual(title, ARRIVAL)
        video = await playback.film_video(ARRIVAL)
        assert isinstance(video, PlexVideo)
        self.assertEqual((video.key, video.file), (f"movie:{ARRIVAL_ID}", str(self.arrival)))
        self.assertEqual((video.view_offset_ms, video.watched), (0, False))
        self.assertTrue(video.markers_known)  # no Plex to ask for markers
        # A place under MIN_PLACE_MS isn't stored, as for a file from Explorer.
        self.assertEqual(MIN_PLACE_MS, LOCAL_MIN_PLACE_MS)
        await self.playing.report(video, MIN_PLACE_MS - 1, 7_000_000, profile_id=1)
        self.assertEqual(await self.db.watch_places.places(1), {})
        await self.playing.report(video, 600_000, 7_000_000, profile_id=1)
        again = await playback.film_video(ARRIVAL)
        assert isinstance(again, PlexVideo)
        self.assertEqual((again.view_offset_ms, again.duration_ms), (600_000, 7_000_000))
        # ▶ Play hands the player the file as it is.
        found = await playback.locate(ARRIVAL, again)
        self.assertEqual(found, Located("Arrival", "movie", str(self.arrival)))
        # Back near the start: the place is forgotten (nothing to resume).
        await self.playing.report(again, 2_000, 7_000_000, profile_id=1)
        latest = await self.playing.film_video(ARRIVAL, profile_id=1)
        assert latest is not None
        self.assertEqual(latest.view_offset_ms, 0)

    async def test_two_people_have_their_own_places(self) -> None:
        video = await self.playing.film_video(DUNE, profile_id=1)
        assert video is not None
        await self.playing.report(video, 600_000, 9_000_000, profile_id=1)
        await self.playing.report(video, 1_200_000, 9_000_000, profile_id=2)
        await self.playing.mark_watched(video, profile_id=3)
        places = {
            person: await self.playing.film_video(DUNE, profile_id=person)
            for person in (1, 2, 3, 4)
        }
        self.assertEqual(
            {person: (v.view_offset_ms, v.watched) for person, v in places.items() if v},
            {1: (600_000, False), 2: (1_200_000, False), 3: (0, True), 4: (0, False)},
        )
        # Through ▶ Play: the person selected on this PC.
        playback = self.playback()
        self.person = 2
        chosen = await playback.film_video(DUNE)
        assert isinstance(chosen, PlexVideo)
        self.assertEqual(chosen.view_offset_ms, 1_200_000)

    async def test_watched_at_ninety_percent(self) -> None:
        self.assertEqual(WATCHED_SHARE, 0.9)
        self.assertIs(media_player.WATCHED_SHARE, WATCHED_SHARE)  # the player's own mark
        video = await self.playing.film_video(ARRIVAL, profile_id=1)
        assert video is not None
        await self.playing.report(video, 6_299_000, 7_000_000, profile_id=1)
        below = await self.playing.film_video(ARRIVAL, profile_id=1)
        assert below is not None
        self.assertEqual((below.view_offset_ms, below.watched), (6_299_000, False))
        await self.playing.report(video, 6_300_000, 7_000_000, profile_id=1)
        done = await self.playing.film_video(ARRIVAL, profile_id=1)
        assert done is not None
        self.assertEqual((done.view_offset_ms, done.watched), (0, True))  # nothing to resume
        # The player's own mark (WantNext, or 90 % reached): watched, the length kept.
        episode = (await self.playing.show_videos(SEVERANCE, profile_id=1))[0]
        await self.playing.report(episode, 1_000_000, 3_000_000, profile_id=1)
        await self.playing.mark_watched(episode, profile_id=1)
        [first, *_rest] = await self.playing.show_videos(SEVERANCE, profile_id=1)
        self.assertEqual(
            (first.watched, first.view_offset_ms, first.duration_ms), (True, 0, 3_000_000)
        )

    async def test_continue_watching_is_per_person_newest_first(self) -> None:
        arrival = await self.playing.film_video(ARRIVAL, profile_id=1)
        dune = await self.playing.film_video(DUNE, profile_id=2)
        assert arrival is not None and dune is not None
        episodes = await self.playing.show_videos(SEVERANCE, profile_id=1)
        await self.playing.report(arrival, 600_000, 7_000_000, profile_id=1)
        self.later()
        await self.playing.report(episodes[1], 300_000, 3_000_000, profile_id=1)
        self.later()
        await self.playing.report(dune, 900_000, 9_000_000, profile_id=2)
        self.later()
        await self.playing.mark_watched(episodes[0], profile_id=1)  # watched: no place
        first = await self.playing.continue_watching(profile_id=1)
        self.assertEqual(
            [(item.title.key, item.video.key, item.video.view_offset_ms) for item in first],
            [
                (SEVERANCE.key, f"episode:{SEVERANCE_ID}:1:2", 300_000),
                (ARRIVAL.key, f"movie:{ARRIVAL_ID}", 600_000),
            ],
        )
        self.assertEqual(first[0].video.file, str(self.episodes[1, 2]))
        self.assertEqual((first[0].video.season, first[0].video.episode), (1, 2))
        self.assertEqual(first[0].title, SEVERANCE)
        second = await self.playing.continue_watching(profile_id=2)
        self.assertEqual([item.title for item in second], [DUNE])
        self.assertEqual(second[0].video.file, str(self.dune_4k))
        # The Library page's row: the person selected on this PC.
        page = PlexLibraryService(
            cast(Any, _NoPlex()),
            cast(Any, _NoLibrary()),
            folders=self.playing,
            person=lambda: self.person,
        )
        self.assertEqual(
            [item.title for item in await page.continue_watching()], [SEVERANCE, ARRIVAL]
        )
        self.person = 2
        self.assertEqual([item.title for item in await page.continue_watching()], [DUNE])
        # A title no longer in the library drops out of the row.
        await self.db.plex.replace(PlexScan(titles=(DUNE, SEVERANCE)), NOW)
        self.assertEqual(
            [item.title for item in await self.playing.continue_watching(profile_id=1)],
            [SEVERANCE],
        )

    async def test_two_copies_are_one_title_and_the_best_plays(self) -> None:
        titles = await self.playing.titles()
        self.assertEqual([t.title for t in titles if t.kind == "movie"], ["Arrival", "Dune"])
        playback = self.playback()
        video = await playback.film_video(DUNE)
        assert isinstance(video, PlexVideo)
        self.assertEqual(video.file, str(self.dune_4k))  # the taller picture
        self.assertEqual(
            await self.playing.copies(video), [str(self.dune_4k), str(self.dune_1080)]
        )
        # One place per person, whichever copy plays.
        await self.playing.report(video, 600_000, 9_000_000, profile_id=1)
        self.assertEqual(list(await self.db.watch_places.places(1)), [f"movie:{DUNE_ID}"])
        # The best copy gone from disk: the best remaining one plays.
        self.dune_4k.unlink()
        with self.assertLogs("app.services.playback", "INFO") as logs:
            found = await playback.locate(DUNE, video)
        self.assertEqual(found, Located("Dune", "movie", str(self.dune_1080)))
        self.assertFalse([line for line in logs.output if "Dune" in line or self.root.name in line])
        # None left: the same "can't find the file" as a Plex title, in the folders' words.
        self.dune_1080.unlink()
        missing = await playback.locate(DUNE, video)
        assert isinstance(missing, PlayOutcome)
        self.assertEqual(missing.problem, PlayProblem.NOT_FOUND)
        self.assertNotIn("Settings", missing.text)
        self.assertIn("Dune", missing.text)
        self.assertIn(playing_module.NOT_IN_FOLDERS_TEXT, NL)

    async def test_next_episode_comes_from_the_library(self) -> None:
        videos = await self.playback().episodes(SEVERANCE)
        self.assertEqual(
            [(v.season, v.episode, v.key) for v in videos],
            [
                (1, 1, f"episode:{SEVERANCE_ID}:1:1"),
                (1, 2, f"episode:{SEVERANCE_ID}:1:2"),
                (1, 3, f"episode:{SEVERANCE_ID}:1:3"),  # the double episode: one video
                (2, 1, f"episode:{SEVERANCE_ID}:2:1"),
            ],
        )
        self.assertEqual(videos[2].file, str(self.episodes[1, 3]))
        self.assertEqual(next_episode(videos), videos[0])
        self.assertEqual(next_in_show(videos[1], videos), videos[2])
        self.assertEqual(next_in_show(videos[2], videos), videos[3])
        await self.playing.mark_watched(videos[0], profile_id=1)
        await self.playing.mark_watched(videos[1], profile_id=1)
        self.assertEqual(next_episode(await self.playback().episodes(SEVERANCE)), videos[2])
        # Another person: from the first.
        self.person = 2
        self.assertEqual(next_episode(await self.playback().episodes(SEVERANCE)), videos[0])

    async def test_a_moved_file_keeps_its_place(self) -> None:
        video = await self.playing.film_video(ARRIVAL, profile_id=1)
        assert video is not None
        self.assertEqual(item_key(video), f"movie:{ARRIVAL_ID}")
        await self.playing.report(video, 600_000, 7_000_000, profile_id=1)
        # MediaForge renamed and moved it: the next check stores it under its new path.
        moved = self.films / "Arrival (2016) {tmdb-329865}" / "Arrival (2016).mkv"
        moved.parent.mkdir(parents=True)
        self.arrival.rename(moved)
        await self.db.library_files.remove([str(self.arrival)])
        await self.db.library_files.replace_folder(
            str(moved.parent), [self.film_row(moved, ARRIVAL_ID, "720p", 1_000)]
        )
        after = await self.playing.film_video(ARRIVAL, profile_id=1)
        assert after is not None
        self.assertEqual((after.file, after.key), (str(moved), video.key))
        self.assertEqual(after.view_offset_ms, 600_000)
        # A Plex video has no folder library place.
        with self.assertRaises(ValueError):
            item_key(PlexVideo(key="3001", file=DUNE_FILE))
        self.assertTrue(from_folders(ARRIVAL))
        self.assertFalse(from_folders(PlexTitle(kind="movie", key="3001", title="Dune")))

    async def test_nothing_while_the_folders_arent_the_library(self) -> None:
        await self.settings.update(folder_library_on=False)
        playback = self.playback()
        self.assertFalse(self.playing.on)
        self.assertFalse(playback.available)
        self.assertIsNone(await playback.film(ARRIVAL_ID))
        self.assertEqual(await playback.episodes(SEVERANCE), [])
        page = PlexLibraryService(
            cast(Any, _NoPlex()), cast(Any, _NoLibrary()), folders=self.playing
        )
        self.assertFalse(page.available)
        self.assertEqual(await page.continue_watching(), [])
        self.assertEqual((await page.contents(profile_id=1)).films, ())


class _NoLibrary:
    """LibraryService's part the Library page's contents use: nobody follows anything."""

    async def tracked_ids(self, *, profile_id: int) -> frozenset[int]:
        return frozenset()

    async def gaps(self, *, profile_id: int) -> list[Any]:
        return []

    async def cached_shows(self, show_ids: Any) -> dict[int, Any]:
        return {}


class PlexAsBeforeTests(PlayingTestCase):
    async def test_plex_still_plays_as_before(self) -> None:
        # Plex connected while folders are set up and their rows are still stored.
        await self.settings.update(
            folder_library_on=True, film_folders=(r"D:\Films",), series_folders=(r"D:\Series",)
        )
        await self.db.library_files.replace_folder(
            r"D:\Films",
            [LibraryFile(path=r"D:\Films\Dune.mkv", size=1, mtime=1.0, kind="movie",
                         tmdb_id=693134, recognised=True)],
        )
        playing = FolderPlaying(
            self.settings, self.db.library_files, self.db.watch_places, self.db.plex
        )
        self.assertFalse(playing.on)  # Plex wins
        playback = PlaybackService(self.plex, nothing, folder_library=playing, person=lambda: 1)
        self.assertTrue(playback.available)
        plex_dune = PlexTitle(kind="movie", key="3001", title="Dune: Part Two", tmdb_id=693134)
        video = await playback.film_video(plex_dune)
        assert isinstance(video, PlexVideo)
        self.assertEqual((video.key, video.file), ("3001", DUNE_FILE))  # Plex's file
        self.assertEqual(self.server.requests[-1].url.path, "/library/metadata/3001")
        page = PlexLibraryService(
            self.plex, cast(Any, _NoLibrary()), folders=playing, person=lambda: 1
        )
        self.assertTrue(page.available)


if __name__ == "__main__":
    unittest.main()
