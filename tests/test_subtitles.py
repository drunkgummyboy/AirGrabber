"""Subtitle runs against a scratch library, a temporary downloads folder and a fake
OpenSubtitles (one run also through the real client and a fake server): what is searched, what
is saved where, never overwriting, retries and giving up, the daily quota, problems in plain
words; plus the v6 schema and its repository."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.database import MIGRATIONS, SUBTITLES_MIGRATION, Database
from app.models import (
    DownloadRecord,
    MediaType,
    Settings,
    Show,
    SubtitleAccount,
    SubtitleEvent,
    SubtitleProblem,
    SubtitlesSaved,
)
from app.models.opensubtitles import SubtitleCandidate, SubtitleLink
from app.services import SettingsService
from app.services.opensubtitles import (
    LOGIN_REJECTED_TEXT,
    LoginRejected,
    OpenSubtitlesClient,
    QuotaExceeded,
)
from app.services.subtitles import (
    GIVE_UP_AFTER,
    NO_FOLDER_TEXT,
    RETRY_AFTER,
    SubtitleService,
    folder_unreachable_text,
    pick,
    quota_text,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
RESET = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
SEVERANCE = Show(id=7, name="Severance", imdb_id="tt11280740")
RELEASE = "Severance.S02E04.1080p.WEB.h264-ETHEL[eztv.re]"
FOLDER_NAME = "Severance.S02E04.1080p.WEB.h264-ETHEL"
PACK = "Severance.S02.1080p.WEB.h264-ETHEL"
FILM = "Dune.Part.Two.2024.1080p.WEB-DL.x264-GRP"
VIDEO_SIZE = 200_000  # big enough for a moviehash


def candidate(file_id: int, language: str, **fields: Any) -> SubtitleCandidate:
    return SubtitleCandidate(file_id=file_id, language=language, **fields)


class FakeClient:
    """OpenSubtitlesClient's surface: `found(params)` answers searches; downloads count down."""

    def __init__(self) -> None:
        self.searches: list[dict[str, str | int]] = []
        self.downloads: list[int] = []
        self.sessions: list[tuple[str, str]] = []
        self.remaining = 10
        self.quota_error = False
        self.login_error = False
        self.answers: list[list[SubtitleCandidate]] | None = None
        self.configured = True  # AirGrabber's key is there

    def found(self, params: Mapping[str, str | int]) -> list[SubtitleCandidate]:
        languages = str(params["languages"]).split(",")
        if "moviehash" in params:
            return [candidate(1, "en", moviehash_match=True, hearing_impaired=True)] if "en" in languages else []
        result = []
        if "en" in languages:
            result += [candidate(2, "en", download_count=500), candidate(3, "en", download_count=900, hearing_impaired=True)]
        if "nl" in languages:
            result += [candidate(4, "nl", download_count=50)]
        return result

    async def sign_in(self, username: str, password: str, *, retry: bool = True) -> SubtitleAccount:
        return SubtitleAccount(level="Sub leecher", allowed_downloads=20, remaining_downloads=18)

    async def ensure_session(self, username: str, password: str) -> bool:
        if not username:
            return False
        self.sessions.append((username, password))
        if self.login_error:
            raise LoginRejected(LOGIN_REJECTED_TEXT)
        return True

    async def search(self, params: Mapping[str, str | int]) -> list[SubtitleCandidate]:
        self.searches.append(dict(params))
        if self.answers is not None:
            return self.answers.pop(0) if self.answers else []
        return self.found(params)

    async def download_link(self, file_id: int) -> SubtitleLink:
        if self.quota_error:
            raise QuotaExceeded("used up", RESET)
        self.downloads.append(file_id)
        self.remaining -= 1
        return SubtitleLink(link=f"https://www.opensubtitles.com/download/x/{file_id}", remaining=self.remaining, reset_at=RESET)

    async def fetch(self, link: str) -> bytes:
        return f"subtitle {link.rsplit('/', 1)[1]}".encode()


class SubtitleRunTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        logging.disable(logging.WARNING)  # the runs' problem warnings (asserted as events)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.downloads = self.tmp / "downloads"
        self.downloads.mkdir()
        self.db = await Database.open(self.tmp / "airgrabber.db")
        self.addAsyncCleanup(self.db.close)
        await self.db.library.save_show(SEVERANCE)
        self.settings = await SettingsService.load(self.db.settings)
        await self.settings.update(
            subtitles_enabled=True,
            subtitle_languages=("en", "nl"),
            subtitles_folder=str(self.downloads),
            opensubtitles_username="beau",
            opensubtitles_password="pw",
        )
        self.now = NOW
        self.client = FakeClient()
        self.events: list[SubtitleEvent] = []
        self.movie_ids: dict[int, str | None] = {9: "tt15239678"}
        self.service = self.make_service(self.client)

    def make_service(self, client: Any) -> SubtitleService:
        async def movie_imdb_id(movie_id: int) -> str | None:
            return self.movie_ids.get(movie_id)

        service = SubtitleService(
            self.db.subtitles,
            client,
            self.settings,
            show=self.db.library.get_show,
            movie_imdb_id=movie_imdb_id,
            clock=lambda: self.now,
        )
        service.subscribe(self.events.append)
        return service

    async def grab(self, release: str = RELEASE, *, season: int | None = 2, episode: int | None = 4,
                   media_type: MediaType = MediaType.TV, media_id: int = 7, days_ago: float = 1) -> DownloadRecord:
        await self.db.history.add(
            DownloadRecord(
                media_type=media_type, media_id=media_id, season=season if media_type is MediaType.TV else None,
                episode=episode if media_type is MediaType.TV else None, release_title=release,
                downloaded_at=self.now - timedelta(days=days_ago),
            )
        )
        rows = await self.db.history.recent(1)
        return rows[0]

    def video(self, relative: str, *, recent: bool = False, size: int = VIDEO_SIZE) -> Path:
        path = self.downloads / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(size))
        if not recent:
            old = time.time() - 3600
            os.utime(path, (old, old))
        return path

    def problems(self) -> list[str]:
        return [e.message for e in self.events if isinstance(e, SubtitleProblem)]


class RunTests(SubtitleRunTestCase):
    async def test_saves_each_language_next_to_the_video(self) -> None:
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.1080p.mkv")
        report = await self.service.run_once()
        en = video.with_name("severance.s02e04.1080p.en.srt")
        nl = video.with_name("severance.s02e04.1080p.nl.srt")
        self.assertEqual(en.read_bytes(), b"subtitle 1")  # the moviehash match, though HI
        self.assertEqual(nl.read_bytes(), b"subtitle 4")
        self.assertEqual(report.saved, ("Severance S02E04 (EN)", "Severance S02E04 (NL)"))
        self.assertEqual(report.problem, None)
        hashed, by_id = self.client.searches
        self.assertEqual(set(hashed), {"moviehash", "languages", "ai_translated"})
        self.assertEqual(hashed["languages"], "en,nl")
        self.assertEqual(hashed["ai_translated"], "exclude")
        self.assertEqual(
            by_id,
            {"languages": "nl", "ai_translated": "exclude", "parent_imdb_id": 11280740,
             "season_number": 2, "episode_number": 4, "type": "episode"},
        )
        self.assertEqual(
            await self.service.show_languages(7), {(2, 4): frozenset({"en", "nl"})}
        )
        self.assertEqual(self.events, [SubtitlesSaved(label="Severance S02E04", languages=("en", "nl"))])
        self.assertEqual(self.service.quota.remaining, 8)

        # The grab is complete: the next run doesn't even look.
        self.client.searches.clear()
        await self.service.run_once()
        self.assertEqual(self.client.searches, [])

    async def test_without_a_hash_match_the_best_by_downloads_not_hearing_impaired(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", size=1000)  # too small to hash
        await self.service.run_once()
        self.assertEqual(video.with_name("severance.s02e04.en.srt").read_bytes(), b"subtitle 2")
        self.assertNotIn("moviehash", self.client.searches[0])

    async def test_a_subtitle_already_there_is_kept(self) -> None:
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        existing = video.with_name("severance.s02e04.en.srt")
        existing.write_bytes(b"mine")
        await self.service.run_once()
        self.assertEqual(existing.read_bytes(), b"mine")
        self.assertEqual([s["languages"] for s in self.client.searches], ["nl", "nl"])
        self.assertEqual(await self.service.show_languages(7), {(2, 4): frozenset({"en", "nl"})})

    async def test_a_subtitle_appearing_meanwhile_is_not_overwritten(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        target = video.with_name("severance.s02e04.en.srt")
        original_fetch = self.client.fetch

        async def fetch(link: str) -> bytes:
            target.write_bytes(b"written by someone else")
            return await original_fetch(link)

        self.client.fetch = fetch  # type: ignore[method-assign]
        await self.service.run_once()
        self.assertEqual(target.read_bytes(), b"written by someone else")

    async def test_a_season_pack_gives_each_episode_its_own(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        await self.grab(PACK, episode=None)
        self.video(f"{PACK}/Severance.S02E01.mkv")
        self.video(f"{PACK}/Severance.S02E02.mkv")
        self.video(f"{PACK}/Sample/Severance.S02E01.sample.mkv")
        await self.service.run_once()
        self.assertTrue((self.downloads / PACK / "Severance.S02E01.en.srt").exists())
        self.assertTrue((self.downloads / PACK / "Severance.S02E02.en.srt").exists())
        self.assertFalse((self.downloads / PACK / "Sample" / "Severance.S02E01.sample.en.srt").exists())
        self.assertEqual(
            await self.service.show_languages(7),
            {(2, 1): frozenset({"en"}), (2, 2): frozenset({"en"})},
        )

    async def test_a_film(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        await self.grab(FILM, media_type=MediaType.MOVIE, media_id=9)
        video = self.video("Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv", size=1000)
        report = await self.service.run_once()
        self.assertTrue(video.with_name("Dune Part Two (2024).en.srt").exists())
        self.assertEqual(self.client.searches[0]["imdb_id"], 15239678)
        self.assertEqual(self.client.searches[0]["type"], "movie")
        self.assertEqual(report.saved, ("Dune Part Two (2024) (EN)",))

    async def test_a_film_without_an_imdb_id_is_searched_by_title_and_year(self) -> None:
        self.movie_ids = {}
        await self.settings.update(subtitle_languages=("en",))
        await self.grab(FILM, media_type=MediaType.MOVIE, media_id=9)
        self.video(f"{FILM}/dune.mkv", size=1000)
        await self.service.run_once()
        self.assertEqual(self.client.searches[0]["query"], "dune part two")
        self.assertEqual(self.client.searches[0]["year"], "2024")

    async def test_a_growing_file_waits(self) -> None:
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", recent=True)
        report = await self.service.run_once()
        self.assertEqual(self.client.searches, [])
        self.assertEqual(report.videos, 0)

    async def test_old_grabs_are_left_alone(self) -> None:
        await self.grab(days_ago=31)
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        await self.service.run_once()
        self.assertEqual(self.client.searches, [])


class RetryTests(SubtitleRunTestCase):
    async def test_nothing_found_is_tried_again_later_then_given_up(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        self.client.answers = []
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", size=1000)
        report = await self.service.run_once()
        self.assertEqual(report.not_found, ("Severance S02E04 (EN)",))
        self.assertEqual(len(self.client.searches), 1)

        await self.service.run_once()  # too soon
        self.assertEqual(len(self.client.searches), 1)

        self.now += RETRY_AFTER + timedelta(minutes=1)
        await self.service.run_once()
        self.assertEqual(len(self.client.searches), 2)

        self.now = NOW + GIVE_UP_AFTER
        report = await self.service.run_once()
        self.assertEqual(report.gave_up, ("Severance S02E04 (EN)",))
        self.now += RETRY_AFTER * 2
        await self.service.run_once()
        self.assertEqual(len(self.client.searches), 3)  # given up: never again
        self.assertFalse(video.with_name("severance.s02e04.en.srt").exists())

    async def test_changed_languages_open_a_complete_grab_again(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", size=1000)
        await self.service.run_once()
        await self.settings.update(subtitle_languages=("en", "nl"))
        self.client.searches.clear()
        await self.service.run_once()
        self.assertEqual([s["languages"] for s in self.client.searches], ["nl"])
        self.assertTrue(video.with_name("severance.s02e04.nl.srt").exists())


class QuotaTests(SubtitleRunTestCase):
    async def test_the_last_download_of_the_day_stops_the_run(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        self.client.remaining = 1
        await self.grab(PACK, episode=None)
        self.video(f"{PACK}/Severance.S02E01.mkv", size=1000)
        self.video(f"{PACK}/Severance.S02E02.mkv", size=1000)
        report = await self.service.run_once()
        self.assertEqual(len(self.client.downloads), 1)
        self.assertEqual(report.problem, quota_text(RESET))
        self.assertEqual(self.problems(), [quota_text(RESET)])
        self.assertIn("more after", quota_text(RESET))

        searches = len(self.client.searches)
        await self.service.run_once()  # before the reset: nothing
        self.assertEqual(len(self.client.searches), searches)

        self.now = RESET + timedelta(minutes=5)
        self.client.remaining = 20
        await self.service.run_once()
        self.assertEqual(len(self.client.downloads), 2)

    async def test_used_up_already(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        self.client.quota_error = True
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", size=1000)
        report = await self.service.run_once()
        self.assertEqual(report.problem, quota_text(RESET))
        self.assertEqual(self.service.quota.remaining, 0)
        self.assertEqual(self.service.quota.reset_at, RESET)


class ProblemTests(SubtitleRunTestCase):
    async def test_off_does_nothing(self) -> None:
        await self.settings.update(subtitles_enabled=False)
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        await self.service.run_once()
        self.assertEqual((self.client.searches, self.events), ([], []))

    async def test_without_the_app_key_nothing_happens(self) -> None:
        self.client.configured = False
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        report = await self.service.run_once()
        self.assertFalse(self.service.available)
        self.assertIsNone(report.problem)
        self.assertEqual((self.client.searches, self.client.sessions, self.events), ([], [], []))

    async def test_nothing_to_do_needs_no_folder(self) -> None:
        await self.settings.update(subtitles_folder="")
        report = await self.service.run_once()
        self.assertIsNone(report.problem)

    async def test_no_account_still_gets_subtitles(self) -> None:
        await self.settings.update(opensubtitles_username="", opensubtitles_password="")
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        report = await self.service.run_once()
        self.assertIsNone(report.problem)
        self.assertNotEqual(self.client.searches, [])

    async def test_every_run_signs_in_with_the_account(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        await self.service.run_once()
        self.assertEqual(self.client.sessions, [("beau", "pw")])

    async def test_no_folder(self) -> None:
        await self.settings.update(subtitles_folder="")
        await self.grab()
        self.assertEqual((await self.service.run_once()).problem, NO_FOLDER_TEXT)

    async def test_folder_out_of_reach(self) -> None:
        missing = str(self.tmp / "nas-offline")
        await self.settings.update(subtitles_folder=missing)
        await self.grab()
        report = await self.service.run_once()
        self.assertEqual(report.problem, folder_unreachable_text(missing))

    async def test_wrong_password_stops_the_run_and_is_said_once_in_a_while(self) -> None:
        self.client.login_error = True
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        await self.service.run_once()
        self.assertEqual(self.client.sessions, [("beau", "pw")])
        self.assertEqual(self.client.downloads, [])
        await self.service.run_once()
        self.assertEqual(self.problems(), [LOGIN_REJECTED_TEXT])  # not repeated
        self.now += timedelta(hours=4)
        await self.service.run_once()
        self.assertEqual(self.problems(), [LOGIN_REJECTED_TEXT] * 2)

    async def test_a_settings_change_wakes_the_runs(self) -> None:
        self.assertFalse(self.service._wake.is_set())
        await self.settings.update(first_day="Sunday")
        self.assertFalse(self.service._wake.is_set())
        await self.settings.update(subtitle_languages=("nl",))
        self.assertTrue(self.service._wake.is_set())

    async def test_sign_in_makes_the_quota_known(self) -> None:
        account = await self.service.sign_in("beau", "pw")
        self.assertEqual(account.remaining_downloads, 18)
        self.assertEqual(self.service.quota.remaining, 18)


class PickTests(unittest.TestCase):
    def test_hash_match_then_not_hearing_impaired_then_downloads(self) -> None:
        found = [
            candidate(1, "en", download_count=10, hearing_impaired=True),
            candidate(2, "en", download_count=5),
            candidate(3, "EN", download_count=7),
            candidate(4, "nl", download_count=99),
        ]
        self.assertEqual(pick(found, "en"), found[2])
        self.assertEqual(pick([*found, candidate(5, "en", moviehash_match=True, hearing_impaired=True)], "en"), candidate(5, "en", moviehash_match=True, hearing_impaired=True))
        self.assertIsNone(pick(found, "de"))


FIXTURES = Path(__file__).parent / "fixtures" / "opensubtitles"


class RealClientRunTests(SubtitleRunTestCase):
    """The run through the real client and a fake OpenSubtitles server."""

    async def test_end_to_end(self) -> None:
        await self.settings.update(subtitle_languages=("en",))
        seen: list[httpx.Request] = []

        def server(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "www.opensubtitles.com":
                return httpx.Response(200, content=b"1\r\n00:00:01,000 --> 00:00:02,000\r\nHi\r\n")
            assert request.headers["Api-Key"] == "APPKEY123"
            name = {
                "/api/v1/login": "login.json",
                "/api/v1/infos/user": "infos_user.json",
                "/api/v1/subtitles": "search_episode.json",
                "/api/v1/download": "download.json",
            }[request.url.path]
            return httpx.Response(200, json=json.loads((FIXTURES / name).read_text(encoding="utf-8")))

        http = httpx.AsyncClient(transport=httpx.MockTransport(server))
        self.addAsyncCleanup(http.aclose)

        async def no_sleep(_seconds: float) -> None:
            return None

        client = OpenSubtitlesClient(http, api_key=lambda: "APPKEY123", sleep=no_sleep)
        service = self.make_service(client)
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        await service.run_once()
        self.assertTrue(video.with_name("severance.s02e04.en.srt").read_bytes().startswith(b"1\r\n"))
        self.assertEqual(
            [(r.method, r.url.path) for r in seen[:4]],
            [("POST", "/api/v1/login"), ("GET", "/api/v1/infos/user"),
             ("GET", "/api/v1/subtitles"), ("POST", "/api/v1/download")],
        )
        self.assertEqual(json.loads(seen[0].content), {"username": "beau", "password": "pw"})
        self.assertTrue(seen[3].headers["Authorization"].startswith("Bearer "))
        self.assertEqual(json.loads(seen[3].content), {"file_id": 10061})  # the hash match
        self.assertEqual(seen[4].url.host, "www.opensubtitles.com")
        self.assertEqual(service.quota.remaining, 17)


class SchemaTests(unittest.IsolatedAsyncioTestCase):
    def test_the_subtitle_migration_is_v6_and_self_contained(self) -> None:
        self.assertEqual(MIGRATIONS[5], SUBTITLES_MIGRATION)
        self.assertIn("CREATE TABLE subtitle_files", SUBTITLES_MIGRATION)
        self.assertNotIn("ALTER TABLE", SUBTITLES_MIGRATION)

    async def test_v5_database_is_upgraded_and_keeps_its_data(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "v5.db"
        with contextlib.closing(sqlite3.connect(path)) as raw:
            script = "\n".join(MIGRATIONS[:5])
            raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 5;\nCOMMIT;")
            with raw:
                raw.execute("INSERT INTO shows (id, name) VALUES (1, 'Kept')")
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], len(MIGRATIONS))
        self.assertIsNotNone(await db.library.get_show(1))
        tables = {row["name"]: row["strict"] for row in await db.fetch_all("PRAGMA table_list")}
        self.assertEqual((tables["subtitle_files"], tables["subtitle_grabs"]), (1, 1))

    async def test_rows_go_with_their_grab(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = await Database.open(Path(tmp.name) / "x.db")
        self.addAsyncCleanup(db.close)
        await db.history.add(DownloadRecord(media_type=MediaType.TV, media_id=7, season=1, episode=2, release_title="x", info_hash="a" * 40))
        record = (await db.history.recent(1))[0]
        assert record.id is not None
        await db.subtitles.record_saved("v.mkv", "en", record=record, season=1, episode=2, subtitle_path="v.en.srt", when=NOW)
        await db.subtitles.mark_grab_done(record.id, ["en"], NOW)
        grabs = await db.subtitles.grabs_since(NOW - timedelta(days=400))
        self.assertEqual(grabs[0].done_languages, ("en",))
        files = await db.subtitles.files(["v.mkv"])
        self.assertEqual(files[("v.mkv", "en")].subtitle_path, "v.en.srt")
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM download_history")
        self.assertEqual(await db.fetch_all("SELECT * FROM subtitle_grabs"), [])
        self.assertEqual(await db.subtitles.show_languages(7), {(1, 2): frozenset({"en"})})



class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_service_reads_the_shipped_key(self) -> None:
        from unittest import mock

        from app.config import AppPaths
        from app.services import Services, opensubtitles

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        services = await Services.create(AppPaths(Path(tmp.name)).ensure())
        self.addAsyncCleanup(services.aclose)
        with mock.patch.object(opensubtitles, "APP_API_KEY", ""):
            self.assertFalse(services.subtitles.available)
        with mock.patch.object(opensubtitles, "APP_API_KEY", "SHIPPED"):
            self.assertTrue(services.subtitles.available)

    async def test_the_users_own_key_overrides_the_shipped_one(self) -> None:
        from unittest import mock

        from app.config import AppPaths
        from app.services import Services, opensubtitles
        from app.services.subtitles import SETTINGS_FIELDS

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        services = await Services.create(AppPaths(Path(tmp.name)).ensure())
        self.addAsyncCleanup(services.aclose)
        client = services.subtitles._client
        with mock.patch.object(opensubtitles, "APP_API_KEY", "SHIPPED"):
            self.assertEqual(client._api_key(), "SHIPPED")
            self.assertFalse(client._own_key())
            await services.settings.update(opensubtitles_own_key="  MINE  ")
            self.assertEqual(client._api_key(), "MINE")  # read on every request
            self.assertTrue(client._own_key())
            await services.settings.update(opensubtitles_own_key="")
            self.assertEqual(client._api_key(), "SHIPPED")
        with mock.patch.object(opensubtitles, "APP_API_KEY", ""):
            await services.settings.update(opensubtitles_own_key="MINE")
            self.assertTrue(services.subtitles.available)
        self.assertIn("opensubtitles_own_key", SETTINGS_FIELDS)  # a change wakes a run


if __name__ == "__main__":
    unittest.main()
