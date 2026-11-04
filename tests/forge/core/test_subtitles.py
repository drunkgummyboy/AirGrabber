"""Forge's subtitles (app/forge/core/subtitles.py) against a temporary folder and a fake
OpenSubtitles (one request through the real client on a MockTransport): what is searched, what is
saved where, never overwriting, what is on disk spends nothing, the day's allowance, problems in
plain words, the journal; and the one-time carry of AirGrabber's automatic subtitles."""

from __future__ import annotations

import logging
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from app.forge.clients.opensubtitles import OpenSubtitlesClient
from app.forge.core.presets_service import PresetService
from app.forge.core.settings import ForgeSettingsService
from app.forge.core.subtitles import (
    ForgeSubtitles,
    carried_types,
    carry_subtitles,
    combine_fetches,
    file_identity,
    release_marks,
    has_subtitle,
    pick,
    quota_text,
    subtitle_path,
    write_new_file,
)
from app.forge.db.history import HistoryRepo
from app.forge.db.presets import PresetRepo
from app.forge.db.settings import SettingsRepo
from app.forge.models.history import OpKind, OpState, RunSource
from app.forge.models.media import MediaKind, TypeHint
from app.forge.models.moves import MoveRecord
from app.forge.models.opensubtitles import SubtitleCandidate, SubtitleLink
from app.forge.models.subtitles import (
    LoginRejected,
    QuotaExceeded,
    SubtitleAccount,
    SubtitleFetch,
    SubtitleFetchOutcome as Outcome,
    SubtitleIdentity,
    Unreachable,
)
from app.models import HearingImpaired, Settings, SubtitlesFor, SubtitlesMode
from app.services.settings import SettingsService
from tests.forge.base import ForgeTestCase
from tests.forge.db.base import ForgeDbTestCase

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
RESET = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
EPISODE = SubtitleIdentity(
    kind=MediaKind.EPISODE, title="The Ark", year=2023, tmdb_id=4242, season=2, episode=1
)
FILM = SubtitleIdentity(kind=MediaKind.MOVIE, title="Dune", year=2021)
VIDEO_SIZE = 200_000  # big enough for a moviehash


def candidate(file_id: int, language: str, **fields: Any) -> SubtitleCandidate:
    return SubtitleCandidate(file_id=file_id, language=language, **fields)


class FakeClient:
    """OpenSubtitlesClient's surface: `answers` per search; downloads count down."""

    def __init__(self) -> None:
        self.searches: list[dict[str, str | int]] = []
        self.downloads: list[int] = []
        self.sessions: list[tuple[str, str]] = []
        self.remaining = 10
        self.configured = True
        self.quota_error = False
        self.search_error: Exception | None = None
        self.login_error = False
        self.found = [candidate(2, "en", download_count=500), candidate(4, "nl")]

    async def sign_in(self, username: str, password: str) -> SubtitleAccount:
        return SubtitleAccount(allowed_downloads=20, remaining_downloads=18)

    async def check_key(self, key: str) -> None:
        pass

    async def ensure_session(self, username: str, password: str) -> bool:
        if not username:
            return False
        self.sessions.append((username, password))
        if self.login_error:
            raise LoginRejected("Wrong OpenSubtitles user name or password")
        return True

    async def search(self, params: Mapping[str, str | int]) -> list[SubtitleCandidate]:
        self.searches.append(dict(params))
        if self.search_error is not None:
            raise self.search_error
        languages = str(params["languages"]).split(",")
        return [c for c in self.found if c.language in languages]

    async def download_link(self, file_id: int) -> SubtitleLink:
        if self.quota_error:
            raise QuotaExceeded("used up", RESET)
        self.downloads.append(file_id)
        self.remaining -= 1
        return SubtitleLink(
            link=f"https://www.opensubtitles.com/x/{file_id}",
            remaining=self.remaining,
            reset_at=RESET,
        )

    async def fetch(self, link: str) -> bytes:
        return f"subtitle {link.rsplit('/', 1)[1]}".encode()


class FakeSettings:
    def __init__(self, **fields: Any) -> None:
        self.current = Settings(subtitle_languages=("en", "nl"), **fields)


class PickTests(unittest.TestCase):
    FOUND = (
        candidate(1, "en", hearing_impaired=True, download_count=900),
        candidate(2, "en", download_count=500),
        candidate(3, "nl", download_count=10),
    )

    def test_avoid_is_the_default(self) -> None:
        choice = pick(self.FOUND, "en")
        assert choice is not None
        self.assertEqual(choice.file_id, 2)

    def test_prefer_and_either(self) -> None:
        for wanted, file_id in ((HearingImpaired.PREFER, 1), (HearingImpaired.EITHER, 1)):
            choice = pick(self.FOUND, "en", wanted)
            assert choice is not None
            self.assertEqual(choice.file_id, file_id)

    def test_a_hash_match_wins(self) -> None:
        found = (*self.FOUND, candidate(9, "en", moviehash_match=True, hearing_impaired=True))
        choice = pick(found, "en")
        assert choice is not None
        self.assertEqual(choice.file_id, 9)

    def test_the_same_release_beats_more_downloads(self) -> None:
        found = (
            candidate(1, "en", download_count=900, release="The.Ark.S02E01.1080p.BluRay.x264-OTHER"),
            candidate(2, "en", download_count=5, release="The Ark S02E01 720p WEB H264-NTb"),
        )
        choice = pick(found, "en", release="The.Ark.S02E01.1080p.WEB-DL.x265-NTb.mkv")
        assert choice is not None
        self.assertEqual(choice.file_id, 2)

    def test_the_group_counts_before_the_source(self) -> None:
        found = (
            candidate(1, "en", download_count=900, release="The.Ark.S02E01.WEBRip-OTHER"),
            candidate(2, "en", download_count=5, release="The.Ark.S02E01.HDTV-NTb"),
        )
        choice = pick(found, "en", release="The.Ark.S02E01.1080p.WEB-DL-NTb.mkv")
        assert choice is not None
        self.assertEqual(choice.file_id, 2)

    def test_the_same_source_beats_more_downloads(self) -> None:
        found = (
            candidate(1, "en", download_count=900, release="The.Ark.S02E01.BluRay-AAA"),
            candidate(2, "en", download_count=5, release="The.Ark.S02E01.WEBRip-BBB"),
        )
        choice = pick(found, "en", release="The.Ark.S02E01.1080p.WEB-DL-NTb.mkv")
        assert choice is not None
        self.assertEqual(choice.file_id, 2)

    def test_without_a_release_the_most_downloads_decide(self) -> None:
        found = (
            candidate(1, "en", download_count=900, release="The.Ark.S02E01.BluRay-AAA"),
            candidate(2, "en", download_count=5, release="The.Ark.S02E01.WEB-NTb"),
        )
        for name in ("", "The Ark - S02E01 - The Sun.mkv"):
            choice = pick(found, "en", release=name)
            assert choice is not None
            self.assertEqual(choice.file_id, 1)

    def test_the_hearing_impaired_choice_stays_before_the_release(self) -> None:
        found = (
            candidate(1, "en", hearing_impaired=True, release="The.Ark.S02E01.WEB-NTb"),
            candidate(2, "en", release="The.Ark.S02E01.BluRay-AAA"),
        )
        choice = pick(found, "en", release="The.Ark.S02E01.WEB-NTb.mkv")
        assert choice is not None
        self.assertEqual(choice.file_id, 2)

    def test_a_hash_match_beats_the_same_release(self) -> None:
        found = (
            candidate(1, "en", release="The.Ark.S02E01.WEB-NTb"),
            candidate(2, "en", moviehash_match=True, release="The.Ark.S02E01.BluRay-AAA"),
        )
        choice = pick(found, "en", release="The.Ark.S02E01.WEB-NTb.mkv")
        assert choice is not None
        self.assertEqual(choice.file_id, 2)

    def test_what_a_name_says(self) -> None:
        for name, marks in (
            ("The.Ark.S02E01.1080p.WEB-DL.x265-NTb.mkv", ("ntb", "web")),
            ("The.Ark.S02E01.1080p.WEB-DL.mkv", (None, "web")),
            ("Dune.2021.Blu-ray.x264-AAA[rartv].mkv", ("aaa", "bluray")),
            ("Dune.2021.1080p.x265-AAA", ("aaa", None)),
            ("The.Ark.S02E01.HDTV.x264", (None, "tv")),
            ("Spider-Man.mkv", (None, None)),
            ("Dune (2021).mkv", (None, None)),
            ("The Ark - S02E01 - The Sun.mkv", (None, None)),
            ("", (None, None)),
        ):
            with self.subTest(name=name):
                self.assertEqual(release_marks(name), marks)

    def test_a_language_without_any(self) -> None:
        self.assertIsNone(pick(self.FOUND, "de"))


class FileTests(ForgeTestCase):
    def test_named_to_match(self) -> None:
        video = Path("D:/Series/The Ark - S02E01.mkv")
        self.assertEqual(subtitle_path(video, "en").name, "The Ark - S02E01.en.srt")
        self.assertEqual(subtitle_path(video, "pt-BR").name, "The Ark - S02E01.pt-BR.srt")

    def test_an_existing_subtitle_counts_in_any_variant(self) -> None:
        video = Path("D:/Series/Show.mkv")
        for name in ("Show.en.srt", "show.EN.srt", "Show.en.forced.srt", "Show.en.ass"):
            self.assertTrue(has_subtitle(video, "en", [name]), name)
        for name in ("Show.nl.srt", "Show.en.txt", "Other.en.srt", "Show.srt"):
            self.assertFalse(has_subtitle(video, "en", [name]), name)

    def test_written_once_never_over_an_existing_file(self) -> None:
        target = self.tmp / "x.en.srt"
        stamp = write_new_file(target, b"first")
        assert stamp is not None
        self.assertEqual(stamp[0], 5)
        self.assertIsNone(write_new_file(target, b"second"))
        self.assertEqual(target.read_bytes(), b"first")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["x.en.srt"])  # no temporary left

    def test_an_unwritable_folder_raises(self) -> None:
        with self.assertRaises(OSError):
            write_new_file(self.tmp / "missing" / "x.en.srt", b"x")


class TextTests(unittest.TestCase):
    def test_quota_text(self) -> None:
        self.assertEqual(
            quota_text(None), "Today's subtitle downloads are used up; more tomorrow"
        )
        self.assertIn("more after", quota_text(RESET))


class IdentityTests(ForgeTestCase):
    def test_from_the_name_alone(self) -> None:
        found = file_identity("The Ark (2023) - S02E01 - The Sun.mkv")
        self.assertEqual(found.kind, MediaKind.EPISODE)
        self.assertEqual((found.season, found.episode), (2, 1))
        self.assertEqual(found.title, "The Ark")
        self.assertIsNone(found.tmdb_id)

    def test_a_move_record_adds_the_kind_and_the_tmdb_id(self) -> None:
        record = MoveRecord(
            source=Path("D:/in/a.mkv"),
            target=Path("D:/lib/Dune (2021).mkv"),
            moved_at=datetime(2026, 1, 1),
            kind=MediaKind.MOVIE,
            tmdb_id=438631,
            run_id=1,
        )
        found = file_identity("Dune (2021).mkv", record)
        self.assertEqual((found.kind, found.tmdb_id, found.year), (MediaKind.MOVIE, 438631, 2021))

    def test_the_original_name_comes_along(self) -> None:
        found = file_identity("Dune (2021).mkv", None, "Dune.2021.WEB-NTb.mkv")
        self.assertEqual(found.release, "Dune.2021.WEB-NTb.mkv")


class CombineTests(unittest.TestCase):
    def test_what_stopped_them_counts_first(self) -> None:
        quota = SubtitleFetch(outcome=Outcome.QUOTA, not_found=("nl",), message="m", reason="r")
        fine = SubtitleFetch(outcome=Outcome.SAVED, saved=("en",))
        combined = combine_fetches([fine, quota])
        self.assertEqual(combined.outcome, Outcome.QUOTA)
        self.assertEqual((combined.saved, combined.message), (("en",), "m"))

    def test_languages_add_up(self) -> None:
        one = SubtitleFetch(outcome=Outcome.SAVED, saved=("en",))
        two = SubtitleFetch(outcome=Outcome.NOT_FOUND, not_found=("en",))
        self.assertEqual(combine_fetches([one, two]).outcome, Outcome.PARTLY)
        self.assertEqual(combine_fetches([one, one]).outcome, Outcome.SAVED)
        self.assertEqual(combine_fetches([two]).outcome, Outcome.NOT_FOUND)
        there = SubtitleFetch(outcome=Outcome.ALREADY, already=("en",))
        self.assertEqual(combine_fetches([there]).outcome, Outcome.ALREADY)
        gone = SubtitleFetch(outcome=Outcome.NO_VIDEO)
        self.assertEqual(combine_fetches([gone, gone]).outcome, Outcome.NO_VIDEO)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.video = self.tmp / "The Ark - S02E01.mkv"
        self.video.write_bytes(b"v" * VIDEO_SIZE)
        self.client = FakeClient()
        self.settings = FakeSettings()
        self.service = self.make()

    def make(self) -> ForgeSubtitles:
        return ForgeSubtitles(
            cast(OpenSubtitlesClient, self.client), self.settings, clock=lambda: NOW
        )

    def names(self) -> list[str]:
        return sorted(p.name for p in self.tmp.iterdir() if p != self.video)

    async def test_every_chosen_language_named_to_match(self) -> None:
        result = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(result.outcome, Outcome.SAVED)
        self.assertEqual(result.saved, ("en", "nl"))
        self.assertEqual(self.names(), ["The Ark - S02E01.en.srt", "The Ark - S02E01.nl.srt"])
        self.assertEqual(self.client.downloads, [2, 4])

    async def test_by_hash_first_then_by_what_forge_knows(self) -> None:
        self.client.found = []
        await self.service.fetch(self.video, EPISODE)
        by_hash, by_identity = self.client.searches
        self.assertIn("moviehash", by_hash)
        self.assertEqual(by_hash["ai_translated"], "exclude")
        self.assertEqual(by_identity["parent_tmdb_id"], 4242)
        self.assertEqual((by_identity["season_number"], by_identity["episode_number"]), (2, 1))
        self.assertEqual(by_identity["type"], "episode")
        self.assertNotIn("query", by_identity)

    async def test_a_film_by_id_or_else_title_and_year(self) -> None:
        self.client.found = []
        await self.service.fetch(self.video, FILM.model_copy(update={"tmdb_id": 9}))
        self.assertEqual(self.client.searches[-1]["tmdb_id"], 9)
        self.assertEqual(self.client.searches[-1]["type"], "movie")
        await self.service.fetch(self.video, FILM)
        last = self.client.searches[-1]
        self.assertEqual((last["query"], last["year"]), ("Dune", 2021))
        self.assertNotIn("tmdb_id", last)

    async def test_a_show_without_an_id_by_its_name(self) -> None:
        self.client.found = []
        await self.service.fetch(self.video, EPISODE.model_copy(update={"tmdb_id": None}))
        self.assertEqual(self.client.searches[-1]["query"], "The Ark")

    async def test_the_original_name_chooses_among_what_was_found(self) -> None:
        self.client.found = [
            candidate(1, "en", download_count=900, release="The.Ark.S02E01.BluRay-AAA"),
            candidate(2, "en", download_count=5, release="The.Ark.S02E01.WEB-NTb"),
        ]
        identity = EPISODE.model_copy(update={"release": "The.Ark.S02E01.1080p.WEB-DL-NTb.mkv"})
        await self.service.fetch(self.video, identity, ("en",))
        self.assertEqual(self.client.downloads, [2])

    async def test_hash_matches_spare_the_second_search(self) -> None:
        self.client.found = [candidate(1, "en"), candidate(3, "nl")]
        await self.service.fetch(self.video, EPISODE)
        self.assertEqual(len(self.client.searches), 1)

    async def test_what_is_on_disk_spends_nothing(self) -> None:
        for language in ("en", "nl"):
            subtitle_path(self.video, language).write_bytes(b"mine")
        result = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(result.outcome, Outcome.ALREADY)
        self.assertEqual(result.already, ("en", "nl"))
        self.assertEqual(self.client.searches, [])
        self.assertEqual(self.client.downloads, [])
        self.assertEqual(subtitle_path(self.video, "en").read_bytes(), b"mine")

    async def test_only_the_missing_language_is_asked_for(self) -> None:
        subtitle_path(self.video, "en").write_bytes(b"mine")
        result = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(result.outcome, Outcome.SAVED)
        self.assertEqual((result.saved, result.already), (("nl",), ("en",)))
        self.assertEqual(self.client.downloads, [4])
        self.assertEqual(self.client.searches[0]["languages"], "nl")

    async def test_one_language_by_hand(self) -> None:
        result = await self.service.fetch(self.video, EPISODE, ("nl",))
        self.assertEqual(result.saved, ("nl",))
        self.assertEqual(self.names(), ["The Ark - S02E01.nl.srt"])

    async def test_partly_and_none_found(self) -> None:
        self.client.found = [candidate(2, "en")]
        partly = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(partly.outcome, Outcome.PARTLY)
        self.assertEqual((partly.saved, partly.not_found), (("en",), ("nl",)))
        self.client.found = []
        subtitle_path(self.video, "en").unlink()
        none = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(none.outcome, Outcome.NOT_FOUND)
        self.assertEqual(none.not_found, ("en", "nl"))

    async def test_the_quota_refused_on_download(self) -> None:
        self.client.quota_error = True
        result = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(result.outcome, Outcome.QUOTA)
        self.assertEqual(result.not_found, ("en", "nl"))
        self.assertIn("used up", result.message)
        self.assertEqual(result.reason, "Today's subtitle downloads are used up")
        self.assertTrue(self.service.used_up)
        self.assertEqual(self.names(), [])
        # Until the count starts again nothing is asked at all.
        searches = len(self.client.searches)
        again = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(again.outcome, Outcome.QUOTA)
        self.assertEqual(len(self.client.searches), searches)

    async def test_the_last_download_of_the_day_stops_the_next_language(self) -> None:
        self.client.remaining = 1
        result = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(result.outcome, Outcome.QUOTA)
        self.assertEqual((result.saved, result.not_found), (("en",), ("nl",)))
        self.assertEqual(self.client.downloads, [2])

    async def test_the_quota_starts_again(self) -> None:
        self.client.quota_error = True
        await self.service.fetch(self.video, EPISODE)
        self.client.quota_error = False
        self.service._clock = lambda: RESET
        self.assertFalse(self.service.used_up)
        self.assertEqual((await self.service.fetch(self.video, EPISODE)).outcome, Outcome.SAVED)

    async def test_problems_in_plain_words(self) -> None:
        self.client.search_error = Unreachable("Couldn't reach OpenSubtitles")
        result = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(result.outcome, Outcome.PROBLEM)
        self.assertEqual(result.message, "Couldn't reach OpenSubtitles")
        self.assertEqual(
            result.reason, "Couldn't reach OpenSubtitles. Check your internet connection"
        )

    async def test_a_wrong_saved_password(self) -> None:
        self.settings = FakeSettings(opensubtitles_username="alex", opensubtitles_password="pw")
        self.client.login_error = True
        result = await self.make().fetch(self.video, EPISODE)
        self.assertEqual(result.outcome, Outcome.PROBLEM)
        self.assertEqual(self.client.sessions, [("alex", "pw")])
        self.assertEqual(self.client.searches, [])

    async def test_the_video_is_gone(self) -> None:
        result = await self.service.fetch(self.tmp / "gone.mkv", EPISODE)
        self.assertEqual(result.outcome, Outcome.NO_VIDEO)
        self.assertEqual(self.client.searches, [])

    async def test_no_key_or_no_language(self) -> None:
        self.client.configured = False
        self.assertFalse(self.service.available)
        self.assertEqual((await self.service.fetch(self.video, EPISODE)).outcome, Outcome.UNAVAILABLE)
        self.client.configured = True
        result = await self.service.fetch(self.video, EPISODE, ())
        self.assertEqual(result.outcome, Outcome.UNAVAILABLE)

    async def test_a_folder_that_cant_be_written(self) -> None:
        from unittest import mock

        with mock.patch(
            "app.forge.core.subtitles.write_new_file", side_effect=PermissionError("denied")
        ):
            result = await self.service.fetch(self.video, EPISODE)
        self.assertEqual(result.outcome, Outcome.PROBLEM)
        self.assertIn("writable", result.message)

    async def test_the_real_client_keeps_the_key_out_of_the_address(self) -> None:
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/subtitles"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "attributes": {
                                    "language": "en",
                                    "files": [{"file_id": 7}],
                                }
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/download"):
                return httpx.Response(
                    200,
                    json={
                        "link": "https://www.opensubtitles.com/download/abc/s.srt",
                        "remaining": 4,
                    },
                )
            return httpx.Response(200, content=b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            client = OpenSubtitlesClient(http, api_key=lambda: "SECRETKEY", own_key=lambda: False)
            service = ForgeSubtitles(client, self.settings)
            result = await service.fetch(self.video, EPISODE, ("en",))
        self.assertEqual(result.outcome, Outcome.SAVED)
        self.assertEqual(service.quota.remaining, 4)
        self.assertTrue(subtitle_path(self.video, "en").exists())
        for request in seen:
            self.assertNotIn("SECRETKEY", str(request.url))
        self.assertEqual(seen[0].headers["Api-Key"], "SECRETKEY")


class JournalTests(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.history = HistoryRepo(self.db)
        self.video = self.tmp / "The Ark - S02E01.mkv"
        self.video.write_bytes(b"v")
        self.service = ForgeSubtitles(
            cast(OpenSubtitlesClient, FakeClient()), FakeSettings(), self.history
        )

    async def test_journaled_in_the_videos_run(self) -> None:
        run = await self.history.start_run(RunSource.APP, None)
        op = await self.history.record_pending(run, OpKind.RENAME, self.tmp / "a.mkv", self.video)
        await self.history.mark_done(op)
        await self.service.fetch(self.video, EPISODE, ("en",), run_id=run)
        ops = await self.history.operations(run)
        self.assertEqual([o.kind for o in ops], [OpKind.RENAME, OpKind.CREATE_FILE])
        self.assertEqual(ops[1].state, OpState.DONE)
        self.assertEqual(ops[1].dst, str(subtitle_path(self.video, "en")))
        self.assertTrue(ops[1].size)
        self.assertTrue(ops[1].mtime_ns)

    async def test_without_a_run_nothing_is_journaled(self) -> None:
        result = await self.service.fetch(self.video, EPISODE, ("en",))
        self.assertEqual(result.outcome, Outcome.SAVED)
        self.assertEqual(await self.history.list_runs(), [])

    async def test_a_run_that_left_the_history_still_gets_its_subtitle(self) -> None:
        result = await self.service.fetch(self.video, EPISODE, ("en",), run_id=999)
        self.assertEqual(result.outcome, Outcome.SAVED)
        self.assertTrue(subtitle_path(self.video, "en").exists())


class FakeAirGrabber:
    def __init__(self, **fields: Any) -> None:
        self.current = Settings(**fields)


class CarryTests(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        repo = PresetRepo(self.db)
        await repo.seed_defaults_if_empty()
        self.presets = PresetService(repo)
        self.settings = await ForgeSettingsService.load(SettingsRepo(self.db))

    async def carry(self, **fields: Any) -> None:
        await carry_subtitles(
            self.settings, self.presets, cast(SettingsService, FakeAirGrabber(**fields))
        )

    async def on(self) -> set[str]:
        return {p.name for p in await self.presets.list() if p.options.subtitles}

    async def names(self, *types: TypeHint) -> set[str]:
        return {p.name for p in await self.presets.list() if p.type in types}

    async def test_a_new_install_gets_nothing(self) -> None:
        await self.carry()
        self.assertEqual(await self.on(), set())
        self.assertTrue(self.settings.current.subtitles_carried)

    async def test_only_when_i_ask_gets_nothing(self) -> None:
        await self.carry(subtitles_enabled=True, subtitles_mode=SubtitlesMode.ASK)
        self.assertEqual(await self.on(), set())
        self.assertTrue(self.settings.current.subtitles_carried)

    async def test_both_turns_on_every_preset(self) -> None:
        await self.carry(subtitles_enabled=True, subtitles_for=SubtitlesFor.BOTH)
        self.assertEqual(await self.on(), {p.name for p in await self.presets.list()})

    async def test_series_turns_on_tv_and_auto_presets(self) -> None:
        await self.carry(subtitles_enabled=True, subtitles_for=SubtitlesFor.SERIES)
        self.assertEqual(await self.on(), await self.names(TypeHint.TV, TypeHint.AUTO))

    async def test_films_turns_on_movie_and_auto_presets(self) -> None:
        await self.carry(subtitles_enabled=True, subtitles_for=SubtitlesFor.FILMS)
        self.assertEqual(await self.on(), await self.names(TypeHint.MOVIE, TypeHint.AUTO))

    async def test_it_never_runs_twice(self) -> None:
        await self.carry(subtitles_enabled=True)
        first = (await self.presets.list())[0]
        options = first.options.model_copy(update={"subtitles": False})
        await self.presets.save(first.model_copy(update={"options": options}))
        await self.carry(subtitles_enabled=True)
        self.assertFalse((await self.presets.list())[0].options.subtitles)  # turned off stays off

    async def test_the_other_extras_stay(self) -> None:
        before = {p.name: p.options for p in await self.presets.list()}
        await self.carry(subtitles_enabled=True)
        for preset in await self.presets.list():
            expected = before[preset.name].model_copy(update={"subtitles": True})
            self.assertEqual(preset.options, expected)

    def test_which_presets_each_choice_means(self) -> None:
        self.assertEqual(carried_types(SubtitlesFor.BOTH), frozenset(TypeHint))
        self.assertNotIn(TypeHint.MOVIE, carried_types(SubtitlesFor.SERIES))
        self.assertNotIn(TypeHint.TV, carried_types(SubtitlesFor.FILMS))


if __name__ == "__main__":
    unittest.main()
