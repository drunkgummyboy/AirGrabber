"""The phone connection's folder library routes (app/services/phone/library.py): the list of films
and shows, a title's details and a show's episodes, with the phone's own person's places. A real
temporary database; a fake Discovery and TMDB. Nothing here reaches a network or reads a file."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import ssl
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

from app.config import AppPaths
from app.database import Database
from app.i18n import t
from app.models import PairedPhone, Settings
from app.models.domain import CastMember, TitleExtras, TitleInfo, Video
from app.models.folder_library import LibraryFile, WatchPlace
from app.models.plex import PlexScan, PlexTitle
from app.models.tmdb import TMDBSeason, TMDBSeasonEpisode
from app.services.folder_library.match import title_key
from app.services.folder_library.playing import FolderPlaying
from app.services.phone import library as library_module
from app.services.phone.certificate import ensure_certificate
from app.services.phone.devices import DeviceStore
from app.services.phone.library import PLEX_LIBRARY_TEXT, PhoneLibrary, video_json
from app.services.phone.server import DataReply, FileReply, PhoneServer, Reply, Request
from app.services.settings import SettingsService
from app.services.tmdb import TMDBError
from tests.plex_link import TOKEN

NOW = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)
DUNE_ID, ARRIVAL_ID, SEVERANCE_ID = 438631, 329865, 95396
DUNE = PlexTitle(
    kind="movie", key=title_key("movie", DUNE_ID), title="Dune", year=2021, thumb="/dune.jpg",
    tmdb_id=DUNE_ID, resolution="4K", added_at=NOW - timedelta(days=3),
)
ARRIVAL = PlexTitle(
    kind="movie", key=title_key("movie", ARRIVAL_ID), title="Arrival", year=2016,
    tmdb_id=ARRIVAL_ID, resolution="720p",
)
SEVERANCE = PlexTitle(
    kind="show", key=title_key("show", SEVERANCE_ID), title="Severance", year=2022,
    thumb="/sev.jpg", tmdb_id=SEVERANCE_ID, seasons=2, episodes=5,
)
LIST = ("GET", "/v1/library")
DETAILS = ("GET", "/v1/library/{key}")
EPISODES = ("GET", "/v1/library/{key}/episodes")
FILE = ("GET", "/v1/library/{key}/file")
SUBTITLES = ("GET", "/v1/library/{key}/subtitles")
SUBTITLE = ("GET", "/v1/library/{key}/subtitles/{id}")
PLACE = ("PUT", "/v1/library/{key}/place")
WATCHED = ("PUT", "/v1/library/{key}/watched")
OFF_CARRY_ON = ("DELETE", "/v1/library/{key}/place")
PHONES_PERSON, PCS_PERSON = 2, 1
GONE = "That isn't in your folders any more, so refresh the list."


def film(path: str, tmdb_id: int, quality: str | None, size: int) -> LibraryFile:
    return LibraryFile(
        path=path, size=size, mtime=1.0, kind="movie", tmdb_id=tmdb_id, quality=quality,
        recognised=True,
    )


def episode(
    path: str, season: int, number: int, end: int | None = None, size: int = 900
) -> LibraryFile:
    return LibraryFile(
        path=path, size=size, mtime=1.0, kind="episode", tmdb_id=SEVERANCE_ID, season=season,
        episode=number, episode_end=end, quality="1080p", recognised=True,
    )


class FakeDiscovery:
    def __init__(self) -> None:
        self.extras: dict[tuple[str, int], TitleExtras | Exception] = {}

    async def movie_extras(self, movie_id: int) -> TitleExtras:
        return self._answer("movie", movie_id)

    async def tv_extras(self, tv_id: int) -> TitleExtras:
        return self._answer("tv", tv_id)

    def _answer(self, kind: str, id: int) -> TitleExtras:
        found = self.extras.get((kind, id), TMDBError("not known"))
        if isinstance(found, Exception):
            raise found
        return found


class FakeTMDB:
    def __init__(self) -> None:
        self.english: dict[tuple[str, int], TitleInfo | Exception] = {}
        self.seasons: dict[int, TMDBSeason | None | Exception] = {}
        self.languages: list[str | None] = []
        self.season_calls: list[int] = []

    async def movie_info(self, movie_id: int, *, language: str | None = None) -> TitleInfo:
        return self._info("movie", movie_id, language)

    async def tv_info(self, tv_id: int, *, language: str | None = None) -> TitleInfo:
        return self._info("tv", tv_id, language)

    def _info(self, kind: str, id: int, language: str | None) -> TitleInfo:
        self.languages.append(language)
        found = self.english.get((kind, id), TMDBError("not known"))
        if isinstance(found, Exception):
            raise found
        return found

    async def tv_season(
        self, tv_id: int, number: int, *, language: str | None = None
    ) -> TMDBSeason | None:
        self.season_calls.append(number)
        found = self.seasons.get(number)
        if isinstance(found, Exception):
            raise found
        return found


class LibraryCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = await Database.open(self.root / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = SettingsService(
            self.db.settings,
            Settings(folder_library_on=True, film_folders=(r"\\nas\films",)),
        )
        self.playing = FolderPlaying(
            self.settings, self.db.library_files, self.db.watch_places, self.db.folders,
            now=lambda: NOW,
        )
        self.discovery = FakeDiscovery()
        self.tmdb = FakeTMDB()
        self.library = PhoneLibrary(
            self.settings,
            self.playing,
            self.db.library_files,
            self.db.watch_places,
            cast(Any, self.discovery),
            cast(Any, self.tmdb),
        )
        self.phone = PairedPhone(
            id="phone-1", name="Pixel", token_hash="0" * 64, profile_id=PHONES_PERSON,
            paired_at=NOW,
        )

    async def store(self, titles: tuple[PlexTitle, ...], files: list[LibraryFile]) -> None:
        await self.db.library_files.replace_folder(r"\\nas", files)
        await self.db.folders.replace(PlexScan(titles=titles), NOW)

    async def place(
        self, person: int, item: str, position: int = 0, *, watched: bool = False,
        when: datetime = NOW, duration: int | None = 7_200_000,
    ) -> None:
        await self.db.watch_places.put(
            WatchPlace(
                profile_id=person, item=item, position_ms=position, duration_ms=duration,
                watched=watched, updated_at=when,
            )
        )

    async def ask(self, route: tuple[str, str], **query: str) -> Reply:
        reply = await self.call(route, query)
        assert isinstance(reply, Reply)
        json.dumps(reply.body)  # what the server sends
        return reply

    async def call(
        self,
        route: tuple[str, str],
        query: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> Reply | FileReply | DataReply:
        request = Request(route[0], route[1], query, {}, self.phone, headers or {})
        return await self.library.routes()[route](request)


class ListTests(LibraryCase):
    async def test_films_list_with_the_phones_persons_place(self) -> None:
        await self.store(
            (DUNE, ARRIVAL),
            [
                film(r"\\nas\films\Dune (2021)\Dune.2021.1080p.mkv", DUNE_ID, "1080p", 2_000),
                film(r"\\nas\films\Dune (2021)\Dune.2021.2160p.MKV", DUNE_ID, "4K", 5_000),
                film(r"\\nas\films\Arrival (2016)\Arrival.mp4", ARRIVAL_ID, "720p", 1_000),
            ],
        )
        await self.place(PHONES_PERSON, "movie:438631", 600_000, when=NOW - timedelta(hours=1))
        await self.place(PCS_PERSON, "movie:438631", 900_000)
        await self.place(PCS_PERSON, "movie:329865", 300_000)

        reply = await self.ask(LIST, kind="movie")

        self.assertEqual(reply.status, 200, reply.body)
        arrival, dune = reply.body["items"]  # by title
        self.assertEqual(
            dune,
            {
                "key": "folder:movie:438631",
                "kind": "movie",
                "title": "Dune",
                "year": 2021,
                "poster_url": "https://image.tmdb.org/t/p/w342/dune.jpg",
                "added_at": (NOW - timedelta(days=3)).isoformat(),
                "tmdb_id": DUNE_ID,
                "quality": "4K",
                "video": {
                    "key": "movie:438631",
                    "season": None,
                    "episode": None,
                    "episode_end": None,
                    "title": "Dune",
                    "quality": "4K",
                    "size": 5_000,
                    "ext": "mkv",
                    "duration_ms": 7_200_000,
                    "position_ms": 600_000,
                    "watched": False,
                    "last_watched_at": (NOW - timedelta(hours=1)).isoformat(),
                },
                "seasons": None,
                "episodes": None,
                "watched_episodes": None,
                "last_watched_at": (NOW - timedelta(hours=1)).isoformat(),
            },
        )
        self.assertEqual(arrival["title"], "Arrival")
        self.assertIsNone(arrival["poster_url"])  # no thumb
        self.assertEqual(arrival["quality"], "720p")
        self.assertEqual(arrival["video"]["ext"], "mp4")
        self.assertEqual(arrival["video"]["position_ms"], 0)  # the PC's person's place isn't hers
        self.assertIsNone(arrival["video"]["duration_ms"])
        self.assertIsNone(arrival["last_watched_at"])

    async def test_shows_list_counts_episodes_and_watched(self) -> None:
        await self.store(
            (DUNE, SEVERANCE),
            [
                film(r"\\nas\films\Dune.mkv", DUNE_ID, "4K", 5_000),
                episode(r"\\nas\series\Severance\S01E01.mkv", 1, 1),
                episode(r"\\nas\series\Severance\S01E02.mkv", 1, 2),
                episode(r"\\nas\series\Severance\S02E01.mkv", 2, 1),
            ],
        )
        await self.place(PHONES_PERSON, "episode:95396:1:1", watched=True, when=NOW)
        await self.place(
            PHONES_PERSON, "episode:95396:1:2", 120_000, when=NOW - timedelta(days=1)
        )
        await self.place(PCS_PERSON, "episode:95396:2:1", watched=True)

        reply = await self.ask(LIST, kind="show")

        self.assertEqual(reply.status, 200, reply.body)
        [show] = reply.body["items"]
        self.assertEqual(show["key"], "folder:show:95396")
        self.assertEqual(show["kind"], "show")
        self.assertEqual(show["poster_url"], "https://image.tmdb.org/t/p/w342/sev.jpg")
        self.assertEqual(show["episodes"], 3)
        self.assertEqual(show["seasons"], 2)
        self.assertEqual(show["watched_episodes"], 1)
        self.assertEqual(show["last_watched_at"], NOW.isoformat())
        self.assertIsNone(show["video"])
        self.assertIsNone(show["quality"])

    async def test_kind_must_be_movie_or_show(self) -> None:
        for query in ({"kind": "x"}, {}):
            reply = await self.ask(LIST, **query)
            self.assertEqual(reply.status, 400)
            self.assertEqual(reply.body["error"], "bad_request")

    async def test_a_double_episode_is_one_video_with_its_end(self) -> None:
        file = episode(r"\\nas\series\Severance\S01E03-E04.mkv", 1, 3, end=4)
        video = video_json("episode:95396:1:3", file, None, "Who Is Alive?")
        self.assertEqual((video["season"], video["episode"], video["episode_end"]), (1, 3, 4))
        self.assertEqual(video["title"], "Who Is Alive?")


class PhoneVideoTests(LibraryCase):
    async def test_a_video_key_gives_its_best_copy_at_the_persons_place(self) -> None:
        await self.store(
            (DUNE, SEVERANCE),
            [
                film(r"\\nas\films\Dune.1080p.mkv", DUNE_ID, "1080p", 2_000),
                film(r"\\nas\films\Dune.2160p.mkv", DUNE_ID, "4K", 5_000),
                episode(r"\\nas\series\S01E03-E04.mkv", 1, 3, end=4),
            ],
        )
        await self.place(PHONES_PERSON, "movie:438631", 600_000)
        await self.place(PCS_PERSON, "episode:95396:1:3", 60_000)

        dune = await self.library._phone_video("movie:438631", PHONES_PERSON)
        assert dune is not None
        self.assertEqual(
            (dune["title"], dune["size"], dune["position_ms"]), ("Dune", 5_000, 600_000)
        )
        double = await self.library._phone_video("episode:95396:1:3", PHONES_PERSON)
        assert double is not None
        self.assertEqual(
            (double["episode"], double["episode_end"], double["position_ms"]), (3, 4, 0)
        )
        for unknown in ("episode:95396:1:4", "movie:1", "folder:movie:438631", "plex:1", ""):
            self.assertIsNone(await self.library._phone_video(unknown, PHONES_PERSON), unknown)


class DetailsTests(LibraryCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.store(
            (DUNE, SEVERANCE),
            [
                film(r"\\nas\films\Dune.mkv", DUNE_ID, "4K", 5_000),
                episode(r"\\nas\series\Severance\S01E01.mkv", 1, 1),
            ],
        )

    async def test_details_from_tmdb(self) -> None:
        cast_members = tuple(
            CastMember(name=f"Actor {n}", character=f"Part {n}", profile_path=f"/{n}.jpg")
            for n in range(20)
        )
        self.discovery.extras[("movie", DUNE_ID)] = TitleExtras(
            info=TitleInfo(
                id=DUNE_ID, overview="Paul Atreides…", genres=("Sciencefiction", "Avontuur"),
                backdrop_path="/back.jpg", runtime=155, vote_average=7.8, vote_count=900,
            ),
            trailer=Video(key="abc123", site="YouTube", type="Trailer"),
            cast=cast_members,
        )
        self.tmdb.english[("movie", DUNE_ID)] = TitleInfo(
            id=DUNE_ID, genres=("Science Fiction", "Adventure")
        )

        reply = await self.ask(DETAILS, key="folder:movie:438631")

        self.assertEqual(reply.status, 200, reply.body)
        body = reply.body
        self.assertEqual(body["key"], "folder:movie:438631")
        self.assertEqual(body["title"], "Dune")
        self.assertEqual(body["video"]["key"], "movie:438631")
        self.assertEqual(body["overview"], "Paul Atreides…")
        self.assertEqual(body["genres"], ["Science Fiction", "Adventure"])
        self.assertEqual(self.tmdb.languages, ["en-US"])
        self.assertEqual(body["rating"], 7.8)
        self.assertEqual(body["backdrop_url"], "https://image.tmdb.org/t/p/w1280/back.jpg")
        self.assertEqual(body["runtime_min"], 155)
        self.assertEqual(body["trailer_url"], "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(len(body["cast"]), 15)
        self.assertEqual(
            body["cast"][0],
            {
                "name": "Actor 0",
                "character": "Part 0",
                "photo_url": "https://image.tmdb.org/t/p/w185/0.jpg",
            },
        )

    async def test_a_shows_details_and_split_genres(self) -> None:
        self.discovery.extras[("tv", SEVERANCE_ID)] = TitleExtras(
            info=TitleInfo(id=SEVERANCE_ID, overview="Mark leads a team…")
        )
        self.tmdb.english[("tv", SEVERANCE_ID)] = TitleInfo(
            id=SEVERANCE_ID, genres=("Drama", "Sci-Fi & Fantasy")
        )
        reply = await self.ask(DETAILS, key="folder:show:95396")
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.body["kind"], "show")
        self.assertEqual(reply.body["episodes"], 1)
        self.assertEqual(reply.body["overview"], "Mark leads a team…")
        self.assertEqual(reply.body["genres"], ["Drama", "Science Fiction", "Fantasy"])
        self.assertIsNone(reply.body["trailer_url"])
        self.assertIsNone(reply.body["rating"])  # nobody voted

    async def test_details_without_tmdb_are_empty_not_an_error(self) -> None:
        self.discovery.extras[("movie", DUNE_ID)] = TMDBError("down")
        self.tmdb.english[("movie", DUNE_ID)] = TMDBError("down")

        reply = await self.ask(DETAILS, key="folder:movie:438631")

        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.body["title"], "Dune")
        self.assertEqual(reply.body["overview"], "")
        self.assertEqual(reply.body["genres"], [])
        self.assertEqual(reply.body["cast"], [])
        for empty in ("rating", "backdrop_url", "runtime_min", "trailer_url"):
            self.assertIsNone(reply.body[empty], empty)

    async def test_unknown_title_is_no_longer_there(self) -> None:
        for key in ("folder:movie:1", "folder:show:438631", "movie:438631", "folder:x:1", "abc"):
            for route in (DETAILS, EPISODES):
                reply = await self.ask(route, key=key)
                self.assertEqual(reply.status, 404, (key, route))
                self.assertEqual(reply.body["error"], "no_longer_there")
                self.assertEqual(reply.body["message"], t(GONE))


class EpisodesTests(LibraryCase):
    async def test_episodes_in_order_with_tmdb_names(self) -> None:
        await self.store(
            (SEVERANCE,),
            [
                episode(r"\\nas\series\Severance\S01E02.mkv", 1, 2),
                episode(r"\\nas\series\Severance\S01E01.720p.mkv", 1, 1, size=500),
                episode(r"\\nas\series\Severance\S01E01.mkv", 1, 1, size=900),
                episode(r"\\nas\series\Severance\S00E01.mp4", 0, 1),
                episode(r"\\nas\series\Severance\S01E03-E04.mkv", 1, 3, end=4),
            ],
        )
        self.tmdb.seasons[1] = TMDBSeason(
            season_number=1,
            episodes=tuple(
                TMDBSeasonEpisode(episode_number=n, name=name)
                for n, name in ((1, "Good News About Hell"), (2, "Half Loop"), (3, "In Perpetuity"),
                                (4, "The You You Are"))
            ),
        )
        self.tmdb.seasons[0] = None  # TMDB doesn't know it
        await self.place(PHONES_PERSON, "episode:95396:1:2", 60_000)
        await self.place(PCS_PERSON, "episode:95396:1:1", watched=True)

        reply = await self.ask(EPISODES, key="folder:show:95396")

        self.assertEqual(reply.status, 200, reply.body)
        episodes = reply.body["episodes"]
        self.assertEqual(
            [(one["season"], one["episode"], one["episode_end"]) for one in episodes],
            [(1, 1, None), (1, 2, None), (1, 3, 4), (0, 1, None)],
        )
        self.assertEqual(
            [one["title"] for one in episodes],
            ["Good News About Hell", "Half Loop", "In Perpetuity", ""],
        )
        self.assertEqual(
            [one["key"] for one in episodes],
            ["episode:95396:1:1", "episode:95396:1:2", "episode:95396:1:3", "episode:95396:0:1"],
        )
        self.assertEqual(episodes[0]["size"], 900)  # the best copy
        self.assertFalse(episodes[0]["watched"])  # the PC's person watched it, not hers
        self.assertEqual(episodes[1]["position_ms"], 60_000)
        self.assertEqual(episodes[3]["ext"], "mp4")
        self.assertEqual(sorted(self.tmdb.season_calls), [0, 1])  # one per season present

    async def test_a_season_tmdb_cant_be_asked_about_gives_no_names(self) -> None:
        await self.store((SEVERANCE,), [episode(r"\\nas\series\S01E01.mkv", 1, 1)])
        self.tmdb.seasons[1] = TMDBError("down")
        reply = await self.ask(EPISODES, key="folder:show:95396")
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual([one["title"] for one in reply.body["episodes"]], [""])

    async def test_a_films_key_has_no_episodes(self) -> None:
        await self.store((DUNE,), [film(r"\\nas\films\Dune.mkv", DUNE_ID, "4K", 5_000)])
        reply = await self.ask(EPISODES, key="folder:movie:438631")
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.body["error"], "no_longer_there")


class FileTests(LibraryCase):
    """`file`: the best copy that is there now, from a byte on (Range); a NAS asleep is 504."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.best = self.root / "Dune.2160p.mkv"
        self.other = self.root / "Dune.1080p.mp4"
        self.data = bytes(range(100))
        self.best.write_bytes(self.data * 2)
        self.other.write_bytes(self.data)
        await self.store(
            (DUNE,),
            [
                film(str(self.best), DUNE_ID, "4K", 5_000),
                film(str(self.other), DUNE_ID, "1080p", 2_000),
            ],
        )

    async def file(self, headers: dict[str, str] | None = None) -> Reply | FileReply | DataReply:
        return await self.call(FILE, {"key": "movie:438631"}, headers)

    async def test_the_best_copy_is_sent(self) -> None:
        reply = await self.file()
        self.assertEqual(reply, FileReply(self.best, "video/x-matroska", len(self.data) * 2))

    async def test_the_copy_that_is_there_now_is_sent(self) -> None:
        self.best.unlink()  # MediaForge moved it after the list was read
        reply = await self.file()
        assert isinstance(reply, FileReply), reply
        self.assertEqual((reply.path, reply.partial), (self.other, False))  # a 200
        self.assertEqual(reply.content_type, "video/mp4")
        self.assertEqual((reply.size, reply.start, reply.length), (100, 0, 100))
        self.assertEqual(reply.path.read_bytes(), self.data)
        self.other.unlink()
        reply = await self.file()
        assert isinstance(reply, Reply), reply
        self.assertEqual(
            (reply.status, reply.body), (404, {"error": "no_longer_there", "message": t(GONE)})
        )

    async def test_a_range_resumes(self) -> None:
        self.best.unlink()
        reply = await self.file({"range": "bytes=5-"})
        assert isinstance(reply, FileReply), reply
        self.assertEqual((reply.start, reply.length, reply.partial), (5, 95, True))
        reply = await self.file({"range": "bytes=100-"})
        assert isinstance(reply, Reply), reply
        self.assertEqual((reply.status, reply.body["error"]), (416, "bad_request"))

    async def test_keys_that_are_no_video_are_no_longer_there(self) -> None:
        for key in ("movie:1", "folder:movie:438631", "movie:" + "9" * 30, "movie:²", ""):
            for route in (FILE, SUBTITLES):
                reply = await self.call(route, {"key": key})
                assert isinstance(reply, Reply), (key, reply)
                self.assertEqual((reply.status, reply.body["error"]), (404, "no_longer_there"))

    async def test_a_share_that_says_it_timed_out_is_folder_timeout(self) -> None:
        def timed_out(_path: Path) -> int:
            error = OSError(None, "The semaphore timeout period has expired")
            error.winerror = 121  # Windows' ERROR_SEM_TIMEOUT
            raise error

        with mock.patch.object(library_module, "_size_of", timed_out):
            reply = await self.call(FILE, {"key": "movie:438631"})
        assert isinstance(reply, Reply), reply
        self.assertEqual((reply.status, reply.body["error"]), (504, "folder_timeout"))

    async def test_a_subtitle_that_doesnt_answer_in_time_is_folder_timeout(self) -> None:
        def asleep(_path: Path, *, convert: bool) -> bytes:
            time.sleep(1.0)
            return b""

        subtitle = self.best.with_name(self.best.stem + ".nl.srt")
        subtitle.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nHoi\n")
        with (
            mock.patch.object(library_module, "READ_TIMEOUT_S", 0.2),
            mock.patch.object(library_module, "_subtitle_bytes", asleep),
        ):
            reply = await self.call(SUBTITLE, {"key": "movie:438631", "id": "0"})
        assert isinstance(reply, Reply), reply
        self.assertEqual(
            (reply.status, reply.body),
            (504, {"error": "folder_timeout",
                   "message": t("The folder didn't answer in time. Try again.")}),
        )  # fmt: skip

    async def test_an_unreachable_share_answers_folder_timeout(self) -> None:
        def asleep(_path: Path) -> int:
            time.sleep(1.0)  # a NAS waking up, in the worker thread
            return 0

        ticks = 0

        async def ticking() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.create_task(ticking())
        self.addCleanup(ticker.cancel)
        with (
            mock.patch.object(library_module, "READ_TIMEOUT_S", 0.2),
            mock.patch.object(library_module, "_size_of", asleep),
        ):
            for route, query in (
                (FILE, {"key": "movie:438631"}),
                (SUBTITLES, {"key": "movie:438631"}),
                (SUBTITLE, {"key": "movie:438631", "id": "0"}),
            ):
                started = time.perf_counter()
                ticks_before = ticks
                reply = await self.call(route, query)
                took = time.perf_counter() - started
                assert isinstance(reply, Reply), reply
                # The share didn't answer in time: the file may well be there (not 404).
                self.assertEqual((reply.status, reply.body["error"]), (504, "folder_timeout"))
                self.assertLess(took, 0.6, route)  # within the limit, not the stat's second
                self.assertGreater(ticks - ticks_before, 5, route)  # the loop stayed free

    async def test_a_folder_that_cant_be_listed_has_no_subtitles(self) -> None:
        def asleep(_folder: Path) -> list[str]:
            time.sleep(1.0)
            return []

        with (
            mock.patch.object(library_module, "READ_TIMEOUT_S", 0.2),
            mock.patch.object(library_module, "_names_in", asleep),
        ):
            started = time.perf_counter()
            self.assertEqual(await self.library._sidecars_of(self.best), [])
            self.assertLess(time.perf_counter() - started, 0.6)
        self.assertEqual(await self.library._sidecars_of(self.root / "missing" / "x.mkv"), [])


class SubtitleTests(LibraryCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.video = self.root / "Film.mkv"
        self.video.write_bytes(b"video")
        self.srt = "1\r\n00:00:01,500 --> 00:00:03,000\r\nHallo.\r\n".encode("utf-8-sig")
        (self.root / "Film.nl.srt").write_bytes(self.srt)
        (self.root / "Film.eng.ass").write_bytes(b"[Script Info]\r\n")
        (self.root / "Film 2.nl.srt").write_bytes(b"not this one's")
        await self.store((DUNE,), [film(str(self.video), DUNE_ID, "1080p", 5)])

    async def subtitle(self, id: str, **query: str) -> Reply | FileReply | DataReply:
        return await self.call(SUBTITLE, {"key": "movie:438631", "id": id} | query)

    async def srt_id(self) -> str:
        reply = await self.ask(SUBTITLES, key="movie:438631")
        return str(next(one["id"] for one in reply.body["subtitles"] if one["format"] == "srt"))

    async def test_subtitles_list_and_file(self) -> None:
        order = [name for name in os.listdir(self.root) if name in ("Film.nl.srt", "Film.eng.ass")]
        about = {
            "Film.nl.srt": {"language": "nl", "forced": False, "format": "srt"},
            "Film.eng.ass": {"language": "eng", "forced": False, "format": "ass"},
        }

        reply = await self.ask(SUBTITLES, key="movie:438631")

        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(
            reply.body["subtitles"],
            [{"id": str(n)} | about[name] for n, name in enumerate(order)],  # as found
        )
        srt_id = str(order.index("Film.nl.srt"))
        ass_id = str(order.index("Film.eng.ass"))
        self.assertEqual(await self.subtitle(srt_id), DataReply("application/x-subrip", self.srt))
        vtt = await self.subtitle(srt_id, **{"as": "vtt"})
        assert isinstance(vtt, DataReply), vtt
        self.assertTrue(vtt.content_type.startswith("text/vtt"))
        self.assertEqual(vtt.data, b"WEBVTT\n\n1\n00:00:01.500 --> 00:00:03.000\nHallo.\n")
        not_srt = await self.subtitle(ass_id, **{"as": "vtt"})
        assert isinstance(not_srt, Reply), not_srt
        self.assertEqual((not_srt.status, not_srt.body["error"]), (400, "bad_request"))
        ass = await self.subtitle(ass_id)
        assert isinstance(ass, DataReply), ass
        self.assertEqual(ass.data, b"[Script Info]\r\n")
        odd_as = await self.subtitle(srt_id, **{"as": "pdf"})
        assert isinstance(odd_as, Reply), odd_as
        self.assertEqual(odd_as.status, 400)
        for unknown in ("2", "x", "-1", "01", "²"):
            gone = await self.subtitle(unknown)
            assert isinstance(gone, Reply), (unknown, gone)
            self.assertEqual((gone.status, gone.body["error"]), (404, "no_longer_there"), unknown)

    async def test_a_subtitle_over_the_limit_is_refused(self) -> None:
        srt_id = await self.srt_id()
        with mock.patch.object(library_module, "MAX_SUBTITLE_BYTES", 10):
            too_big = await self.subtitle(srt_id)
        assert isinstance(too_big, Reply), too_big
        self.assertEqual((too_big.status, too_big.body["error"]), (400, "bad_request"))

    async def test_a_latin_1_srt_still_converts(self) -> None:
        (self.root / "Film.nl.srt").write_bytes(
            "1\n00:00:01,000 --> 00:00:02,000\nCafé\n".encode("cp1252")
        )
        vtt = await self.subtitle(await self.srt_id(), **{"as": "vtt"})
        assert isinstance(vtt, DataReply), vtt
        self.assertIn("Café".encode(), vtt.data)

    async def test_the_log_holds_no_name_or_path(self) -> None:
        with self.assertLogs("app.services.phone.library", "DEBUG") as logs:
            await self.ask(SUBTITLES, key="movie:438631")
            await self.subtitle(await self.srt_id(), **{"as": "vtt"})
            self.video.unlink()
            await self.call(FILE, {"key": "movie:438631"})
            library_module.log.debug("(something logged)")
        text = "\n".join(logs.output)
        for secret in ("Film", "nl.srt", self.root.name, "438631"):
            self.assertNotIn(secret, text)


class PlaceTests(LibraryCase):
    """`place`, `watched` and Carry on's removal: the phone's own person's, through playing.py."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.store(
            (DUNE, SEVERANCE),
            [
                film(r"\\nas\films\Dune.mkv", DUNE_ID, "4K", 5_000),
                episode(r"\\nas\series\Severance\S01E01.mkv", 1, 1),
            ],
        )
        await self.settings.update(current_profile=PCS_PERSON)

    async def send(
        self, route: tuple[str, str], key: str, body: dict[str, Any] | None = None
    ) -> Reply:
        request = Request(route[0], route[1], {"key": key}, body or {}, self.phone)
        reply = await self.library.routes()[route](request)
        assert isinstance(reply, Reply), reply
        json.dumps(reply.body)
        return reply

    async def mine(self, item: str = "movie:438631") -> WatchPlace | None:
        return await self.db.watch_places.get(PHONES_PERSON, item)

    async def test_a_place_from_the_phone_is_its_persons(self) -> None:
        reply = await self.send(
            PLACE, "movie:438631", {"position_ms": 120_000, "duration_ms": 3_600_000}
        )
        self.assertEqual(reply, Reply(204, {}))
        place = await self.mine()
        assert place is not None
        self.assertEqual(
            (place.position_ms, place.duration_ms, place.watched), (120_000, 3_600_000, False)
        )
        self.assertEqual(place.updated_at, NOW)
        self.assertIsNone(await self.db.watch_places.get(PCS_PERSON, "movie:438631"))
        # An episode, its length not known yet; a short place isn't kept.
        reply = await self.send(
            PLACE, "episode:95396:1:1", {"position_ms": 60_000, "duration_ms": None}
        )
        self.assertEqual(reply.status, 204)
        episode_place = await self.mine("episode:95396:1:1")
        assert episode_place is not None
        self.assertEqual((episode_place.position_ms, episode_place.duration_ms), (60_000, None))
        await self.send(PLACE, "movie:438631", {"position_ms": 5_000, "duration_ms": 3_600_000})
        back = await self.mine()
        assert back is not None
        self.assertEqual(back.position_ms, 0)  # under 10 s: back to the start

    async def test_a_key_with_a_leading_zero_is_the_same_video(self) -> None:
        reply = await self.send(
            PLACE, "movie:0438631", {"position_ms": 120_000, "duration_ms": 3_600_000}
        )
        self.assertEqual(reply.status, 204)
        place = await self.mine()  # stored under the key the list reads
        assert place is not None
        self.assertEqual(place.position_ms, 120_000)
        self.assertIsNone(await self.db.watch_places.get(PHONES_PERSON, "movie:0438631"))
        reply = await self.send(WATCHED, "episode:095396:01:001", {"watched": True})
        self.assertEqual(reply.status, 204)
        watched = await self.mine("episode:95396:1:1")
        assert watched is not None
        self.assertTrue(watched.watched)

    async def test_past_ninety_percent_is_watched(self) -> None:
        reply = await self.send(
            PLACE, "movie:438631", {"position_ms": 3_300_000, "duration_ms": 3_600_000}
        )
        self.assertEqual(reply, Reply(204, {}))
        place = await self.mine()
        assert place is not None
        self.assertEqual((place.watched, place.position_ms), (True, 0))

    async def test_watched_false_clears_the_place(self) -> None:
        self.assertEqual(
            await self.send(WATCHED, "movie:438631", {"watched": True}), Reply(204, {})
        )
        watched = await self.mine()
        assert watched is not None
        self.assertEqual((watched.watched, watched.position_ms), (True, 0))
        await self.place(PHONES_PERSON, "movie:438631", 900_000, watched=True)
        await self.place(PCS_PERSON, "movie:438631", 900_000, watched=True)
        self.assertEqual(
            await self.send(WATCHED, "movie:438631", {"watched": False}), Reply(204, {})
        )
        place = await self.mine()
        assert place is not None
        self.assertEqual((place.watched, place.position_ms), (False, 0))
        pcs = await self.db.watch_places.get(PCS_PERSON, "movie:438631")
        assert pcs is not None
        self.assertEqual((pcs.watched, pcs.position_ms), (True, 900_000))  # not the phone's

    async def test_off_carry_on_keeps_watched(self) -> None:
        await self.place(PHONES_PERSON, "episode:95396:1:1", 600_000, watched=True)
        reply = await self.send(OFF_CARRY_ON, "episode:95396:1:1")
        self.assertEqual(reply, Reply(204, {}))
        place = await self.mine("episode:95396:1:1")
        assert place is not None
        self.assertEqual((place.watched, place.position_ms), (True, 0))
        # Never watched, nothing stored: still 204, nothing made.
        self.assertEqual((await self.send(OFF_CARRY_ON, "movie:438631")).status, 204)
        self.assertIsNone(await self.mine())

    async def test_bad_bodies(self) -> None:
        places: tuple[dict[str, Any], ...] = (
            {"position_ms": -1, "duration_ms": 3_600_000},
            {"position_ms": "120000", "duration_ms": 3_600_000},
            {"position_ms": 120_000.0, "duration_ms": 3_600_000},
            {"position_ms": True, "duration_ms": 3_600_000},
            {"position_ms": 10**30, "duration_ms": 3_600_000},
            {"position_ms": 120_000, "duration_ms": -1},
            {"position_ms": 120_000, "duration_ms": "long"},
            {"position_ms": 120_000, "duration_ms": 10**30},
            {"duration_ms": 3_600_000},
            {"position_ms": 120_000},
            {},
        )
        for body in places:
            reply = await self.send(PLACE, "movie:438631", body)
            self.assertEqual((reply.status, reply.body["error"]), (400, "bad_request"), body)
        marks: tuple[dict[str, Any], ...] = (
            {"watched": "yes"}, {"watched": 1}, {"watched": None}, {},
        )
        for mark in marks:
            reply = await self.send(WATCHED, "movie:438631", mark)
            self.assertEqual((reply.status, reply.body["error"]), (400, "bad_request"), mark)
        self.assertIsNone(await self.mine())
        good = {"position_ms": 120_000, "duration_ms": 3_600_000, "watched": True}
        for key in ("folder:movie:438631", "folder:show:95396"):  # a title, not a video
            for route in (PLACE, WATCHED, OFF_CARRY_ON):
                reply = await self.send(route, key, good)
                self.assertEqual((reply.status, reply.body["error"]), (400, "bad_request"), key)
        for key in ("movie:1", "episode:95396:1:2", "12345", "movie:" + "9" * 30, "movie:²", ""):
            for route in (PLACE, WATCHED, OFF_CARRY_ON):
                reply = await self.send(route, key, good)
                self.assertEqual(
                    (reply.status, reply.body),
                    (404, {"error": "no_longer_there", "message": t(GONE)}),
                    (key, route),
                )
        self.assertEqual(await self.db.watch_places.places(PHONES_PERSON), {})

    async def test_the_log_holds_no_key(self) -> None:
        with self.assertLogs("app.services.phone.library", "DEBUG") as logs:
            await self.send(PLACE, "movie:438631", {"position_ms": 120_000, "duration_ms": None})
            await self.send(WATCHED, "episode:95396:1:1", {"watched": True})
            await self.send(OFF_CARRY_ON, "movie:438631")
            await self.send(PLACE, "movie:1", {"position_ms": 120_000, "duration_ms": None})
            library_module.log.debug("(something logged)")
        text = "\n".join(logs.output)
        for secret in ("438631", "95396", "movie:", "Dune"):
            self.assertNotIn(secret, text)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class OverTheWireTests(LibraryCase):
    """A real request through the phone server (TLS on loopback), its key percent-encoded."""

    async def test_a_percent_encoded_key_reaches_the_file_route(self) -> None:
        matrix = self.root / "The Matrix (1999).mkv"
        data = bytes(range(256)) * 50
        matrix.write_bytes(data)
        await self.store((), [film(str(matrix), 603, "1080p", len(data))])
        devices = DeviceStore(self.settings)
        _, token = await devices.add("Pixel", PHONES_PERSON)
        cert, key, _ = await asyncio.to_thread(ensure_certificate, AppPaths(root=self.root))
        port = _free_port()
        server = PhoneServer(self.library.routes(), devices, cert, key)
        await server.start(port)
        self.addAsyncCleanup(server.stop)
        client = ssl.create_default_context()
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client)
        try:
            writer.write(
                (
                    "GET /v1/library/movie%3A603/file HTTP/1.1\r\nHost: pc\r\n"
                    f"Authorization: Bearer {token}\r\n"
                    "X-Companion-Can: folder-library\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(-1), 10)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {n.lower(): v.strip() for n, _, v in (one.partition(":") for one in lines[1:])}
        self.assertEqual(lines[0], "HTTP/1.1 200 OK")
        self.assertEqual(headers["content-type"], "video/x-matroska")
        self.assertEqual(int(headers["content-length"]), len(data))
        self.assertEqual(body, data)


class PlexTests(LibraryCase):
    async def test_library_routes_answer_beside_plex(self) -> None:
        """Two shelves: Plex connected and the folders on, the routes answer for the folders
        (the phone's Downloaded section); folders off with Plex, 409 `plex_library`."""
        dune = self.root / "Dune.mkv"
        dune.write_bytes(b"d" * 5_000)
        await self.store((DUNE,), [film(str(dune), DUNE_ID, "4K", 5_000)])
        await self.settings.update(plex_token=TOKEN)  # the folders stay on
        listed = await self.ask(LIST, kind="movie")
        self.assertEqual(listed.status, 200)
        self.assertEqual([item["title"] for item in listed.body["items"]], ["Dune"])
        details = await self.ask(DETAILS, key="folder:movie:438631")
        self.assertEqual(details.status, 200)
        whole = await self.call(FILE, {"key": "movie:438631"})
        self.assertIsInstance(whole, FileReply)
        placed = await self.call(PLACE, {"key": "movie:438631"})
        self.assertNotEqual(getattr(placed, "status", 200), 409)
        # Stop using folders, Plex stays: back to Plex.
        await self.settings.update(folder_library_on=False)
        reply = await self.ask(LIST, kind="movie")
        self.assertEqual(
            (reply.status, reply.body),
            (409, {"error": "plex_library", "message": t(PLEX_LIBRARY_TEXT)}),
        )

    async def test_plex_as_the_library_answers_plex_library(self) -> None:
        await self.store((DUNE,), [film(r"\\nas\films\Dune.mkv", DUNE_ID, "4K", 5_000)])
        await self.settings.update(plex_token=TOKEN, folder_library_on=False)
        for route, query in (
            (LIST, {"kind": "movie"}),
            (DETAILS, {"key": "folder:movie:438631"}),
            (EPISODES, {"key": "folder:show:95396"}),
            (FILE, {"key": "movie:438631"}),
            (SUBTITLES, {"key": "movie:438631"}),
            (SUBTITLE, {"key": "movie:438631", "id": "0"}),
            (PLACE, {"key": "movie:438631"}),
            (WATCHED, {"key": "movie:438631"}),
            (OFF_CARRY_ON, {"key": "movie:438631"}),
        ):
            reply = await self.ask(route, **query)
            self.assertEqual(reply.status, 409, route)
            self.assertEqual(
                reply.body, {"error": "plex_library", "message": t(PLEX_LIBRARY_TEXT)}
            )


class LogTests(LibraryCase):
    async def test_the_log_gives_counts_only(self) -> None:
        await self.store(
            (DUNE, SEVERANCE),
            [
                film(r"\\nas\films\Dune.mkv", DUNE_ID, "4K", 5_000),
                episode(r"\\nas\series\Severance\S01E01.mkv", 1, 1),
            ],
        )
        with self.assertLogs("app.services.phone.library", "INFO") as logs:
            await self.ask(LIST, kind="movie")
            await self.ask(EPISODES, key="folder:show:95396")
        text = "\n".join(logs.output)
        self.assertIn("Phone library: 1 title(s)", text)
        self.assertIn("Phone library: 1 episode(s)", text)
        for secret in ("Dune", "Severance", "nas", "438631", "95396"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
