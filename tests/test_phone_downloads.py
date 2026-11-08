"""The phone's downloads: a tap (AirGrabber chooses), and holding it to choose by hand. Fake
sources and a fake DownloadService: nothing is searched for real or sent to a torrent program."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any, cast
from unittest import mock

from app.config import AppPaths
from app.indexers.base import BaseIndexer
from app.models import (
    DownloadRecord,
    DownloadState,
    GrabOutcome,
    GrabResult,
    GrabTarget,
    MediaType,
    PairedPhone,
    SearchKind,
    SearchRequest,
    TorrentRelease,
)
from app.services import Services
from app.i18n import t
from app.services.downloads import PROGRAM_IN_TORRENT, GrabError, ProgramInTorrent
from app.services.phone.downloads import RELEASE_TTL_S, PhoneDownloads
from app.services.phone.server import Reply, Request
from app.services.phone.suggestions import Suggestions
from app.services.quick_download import SEARCH_TIMEOUT, QuickDownloadService
from app.services.remote import RemoteError
from app.services.search import TorrentSearchService
from tests.test_phone_coming_up import DUNE, NOW, SEVERANCE, TODAY, PhoneCase, episode, film
from tests.test_search import MIB, fake, release

SENT = GrabResult(outcome=GrabOutcome.SENT, message="Sent to qBittorrent")
EPISODE_BODY = {"kind": "episode", "id": SEVERANCE, "season": 2, "episode": 1}
MOVIE_BODY = {"kind": "movie", "id": DUNE, "season": None, "episode": None}
EPISODE_TARGET = GrabTarget(media_type=MediaType.TV, media_id=SEVERANCE, season=2, episode=1)
MOVIE_TARGET = GrabTarget(media_type=MediaType.MOVIE, media_id=DUNE)
GOOD = "Severance.S02E01.1080p.WEB.x264-GROUP"
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=https://tracker.example/SECRET-PASSKEY"
LINK = "https://prowlarr.example/download?apikey=SECRET-KEY"


class DownloadsCase(PhoneCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.grabs.grab = mock.AsyncMock(return_value=SENT)
        self.suggestions: Any = mock.Mock(spec=Suggestions)  # nothing was suggested
        self.suggestions.film = mock.AsyncMock(return_value=None)
        self.suggestions.season_one = mock.AsyncMock(return_value=None)
        self.use_sources(self.good())
        await self.follow(episode(1, TODAY))

    @staticmethod
    def good(title: str = GOOD, **fields: Any) -> TorrentRelease:
        fields.setdefault("size", 1000 * MIB)
        return release(title, **fields)

    def use_sources(self, *results: TorrentRelease) -> None:
        self.use_indexers(fake("one", result=results))

    def use_indexers(self, *indexers: BaseIndexer, timeout: float = SEARCH_TIMEOUT) -> None:
        self.search = TorrentSearchService(indexers, self.settings)
        self.quick = QuickDownloadService(self.search, self.grabs, self.settings)
        self.service = PhoneDownloads(
            self.library,
            self.watchlist,
            self.search,
            self.grabs,
            self.quick,
            self.settings,
            self.profiles,
            self.films,
            self.suggestions,
            timeout=timeout,
            clock=lambda: self.seconds,
        )

    async def post(
        self, path: str, body: dict[str, Any], phone: PairedPhone | None = None
    ) -> Reply:
        request = Request("POST", path, {}, body, phone or self.phone)
        reply = await self.service.routes()[("POST", path)](request)
        json.dumps(reply.body)  # what the server sends
        return reply


class TapTests(DownloadsCase):
    async def test_a_tap_grabs_the_best_one_for_that_episode(self) -> None:
        reply = await self.post("/v1/download", EPISODE_BODY)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.body["outcome"], "grabbed")
        self.assertEqual(reply.body["message"], "Severance S02E01 · Sent to qBittorrent")
        self.grabs.grab.assert_awaited_once()
        self.assertEqual(self.grabs.grab.await_args.args[1], EPISODE_TARGET)

    async def test_a_tap_grabs_a_film_on_the_list(self) -> None:
        self.use_sources(self.good("Dune.2021.1080p.WEB.x264-GROUP"))
        await self.want(film(release_date=date(2021, 10, 22)), digital=date(2021, 10, 22))
        reply = await self.post("/v1/download", MOVIE_BODY)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(self.grabs.grab.await_args.args[1], MOVIE_TARGET)

    async def test_tap_returns_already_when_grabbed_before(self) -> None:
        self.grabs.has_grabbed.return_value = True
        reply = await self.post("/v1/download", EPISODE_BODY)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["outcome"], "already")
        self.assertEqual(reply.body["message"], "Severance S02E01 was already downloaded")
        self.grabs.grab.assert_not_awaited()

    async def grabbed_film(self) -> None:
        """Dune was on the list and a phone asked about it; a grab really took it off, and the
        Downloads page has it."""
        await self.want(film(release_date=date(2021, 10, 22)), digital=date(2021, 10, 22))
        self.grabs.has_grabbed.return_value = True
        self.assertEqual((await self.post("/v1/download", MOVIE_BODY)).body["outcome"], "already")
        grab = DownloadRecord(media_type=MediaType.MOVIE, media_id=DUNE, release_title="Dune.2021")
        await self.db.history.add(grab)
        self.downloads_page((grab, DownloadState.DOWNLOADING), title="Dune (2021)")

    async def test_a_tap_on_a_film_just_grabbed_says_already_not_gone(self) -> None:
        await self.grabbed_film()
        reply = await self.post("/v1/download", MOVIE_BODY)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.body["outcome"], "already")
        self.assertEqual(reply.body["message"], "Dune (2021) was already downloaded")
        self.grabs.grab.assert_not_awaited()

    async def test_a_tap_before_the_downloads_page_has_it_says_already_too(self) -> None:
        await self.grabbed_film()
        self.status.latest = None
        reply = await self.post("/v1/download", MOVIE_BODY)
        self.assertEqual(reply.body["outcome"], "already")

    async def test_a_film_on_the_page_only_is_named_by_its_row(self) -> None:
        grab = DownloadRecord(media_type=MediaType.MOVIE, media_id=DUNE, release_title="Dune.2021")
        self.grabs.has_grabbed.return_value = True
        self.downloads_page((grab, DownloadState.READY), title="Dune (2021)")
        reply = await self.post("/v1/download", MOVIE_BODY)
        self.assertEqual(reply.body["message"], "Dune (2021) was already downloaded")

    async def test_again_works_on_a_film_just_grabbed(self) -> None:
        self.use_sources(self.good("Dune.2021.1080p.WEB.x264-GROUP"))
        await self.grabbed_film()
        reply = await self.post("/v1/download", {**MOVIE_BODY, "again": True})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(self.grabs.grab.await_args.args[1], MOVIE_TARGET)

    async def test_a_film_grabbed_long_ago_and_gone_from_the_page_is_no_longer_there(self) -> None:
        self.grabs.has_grabbed.return_value = True
        reply = await self.post("/v1/download", MOVIE_BODY)
        self.assertEqual(reply.status, 410)

    async def test_again_downloads_it_anyway(self) -> None:
        self.grabs.has_grabbed.return_value = True
        reply = await self.post("/v1/download", {**EPISODE_BODY, "again": True})
        self.assertEqual(reply.body["outcome"], "grabbed")
        self.grabs.grab.assert_awaited_once()

    async def test_nothing_found_has_a_plain_message(self) -> None:
        self.use_sources()
        reply = await self.post("/v1/download", EPISODE_BODY)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["outcome"], "nothing_found")
        self.assertEqual(
            reply.body["message"],
            "No good download found for Severance S02E01. Choose one yourself.",
        )

    async def test_grab_error_returns_its_user_text(self) -> None:
        for error in (GrabError("No torrent program is set up."), RemoteError("Can't reach it.")):
            with self.subTest(type(error).__name__):
                self.grabs.grab.side_effect = error
                reply = await self.post("/v1/download", EPISODE_BODY)
                self.assertEqual(reply.status, 502)
                self.assertEqual(reply.body, {"error": "failed", "message": str(error)})

    async def test_a_tap_goes_on_past_a_torrent_holding_a_program(self) -> None:
        self.use_sources(self.good(GOOD, seeders=900), self.good(GOOD + "2", h=2, seeders=40))
        tried: list[str] = []

        async def grab(chosen: TorrentRelease, target: GrabTarget) -> GrabResult:
            tried.append(chosen.title)
            if len(tried) == 1:
                raise ProgramInTorrent(t(PROGRAM_IN_TORRENT, suffix=".exe"))
            return SENT

        self.grabs.grab.side_effect = grab
        reply = await self.post("/v1/download", EPISODE_BODY)
        self.assertEqual(len(tried), 2)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_a_tap_where_every_torrent_holds_a_program_starts_nothing(self) -> None:
        # The tap's own answer, as on the PC's button (QuickDownloadService keeps the reason to
        # itself); choosing by hand then tells which one and why.
        self.grabs.grab.side_effect = ProgramInTorrent(t(PROGRAM_IN_TORRENT, suffix=".exe"))
        reply = await self.post("/v1/download", EPISODE_BODY)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["outcome"], "nothing_found")
        self.assertEqual(
            reply.body["message"],
            "No good download found for Severance S02E01. Choose one yourself.",
        )

    async def test_what_isnt_out_yet_is_not_searched_for(self) -> None:
        await self.follow(episode(1, TODAY), episode(2, date(2099, 1, 1), number=2))
        await self.want(film(), digital=date(2099, 1, 1))
        for body, label in (
            ({**EPISODE_BODY, "episode": 2}, "Severance S02E02"),
            (MOVIE_BODY, "Dune"),
        ):
            with self.subTest(label):
                reply = await self.post("/v1/download", {**body, "again": True})
                self.assertEqual(reply.body["outcome"], "nothing_found")
                self.assertEqual(
                    reply.body["message"], f"{label} isn't out yet, so try again once it is."
                )
        self.grabs.grab.assert_not_awaited()

    async def test_what_isnt_on_the_persons_lists_is_no_longer_there(self) -> None:
        other = await self.profiles.add("Sam", self.profiles.current.colour)
        for body, phone in (
            ({**EPISODE_BODY, "episode": 9}, None),  # no such episode
            ({**EPISODE_BODY, "id": 1}, None),  # a show nobody follows
            (EPISODE_BODY, self.phone_of(other.id)),  # someone else's show
            (MOVIE_BODY, None),  # a film that left the list
        ):
            with self.subTest(body=body, phone=phone):
                reply = await self.post("/v1/download", body, phone)
                self.assertEqual(reply.status, 410)
                self.assertEqual(reply.body["error"], "no_longer_there")
        self.grabs.grab.assert_not_awaited()

    async def test_what_cant_be_read_is_a_bad_request(self) -> None:
        for body in (
            {},
            {"kind": "song", "id": 1},
            {"kind": "movie", "id": "1"},
            {"kind": "movie", "id": True},
            {"kind": "episode", "id": SEVERANCE, "season": 2},
            {"kind": "episode", "id": SEVERANCE, "season": "2", "episode": 1},
            {**EPISODE_BODY, "again": "yes"},
        ):
            with self.subTest(body):
                reply = await self.post("/v1/download", body)
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.body["error"], "bad_request")

    async def test_a_deleted_person_gets_a_plain_answer_everywhere(self) -> None:
        gone = self.phone_of(12345)
        for path, body in (
            ("/v1/download", EPISODE_BODY),
            ("/v1/releases", EPISODE_BODY),
            ("/v1/grab", {"ref": "abc"}),
        ):
            with self.subTest(path):
                reply = await self.post(path, body, gone)
                self.assertEqual(reply.status, 401)
                self.assertEqual(reply.body["error"], "not_paired")
                self.assertIn("connect the phone again", reply.body["message"])


class ChoosingTests(DownloadsCase):
    async def choose(self) -> list[dict[str, Any]]:
        reply = await self.post("/v1/releases", EPISODE_BODY)
        self.assertEqual(reply.status, 200, reply.body)
        return cast(list[dict[str, Any]], reply.body["releases"])

    async def test_releases_never_carry_magnet_or_download_url(self) -> None:
        self.use_sources(
            self.good(h=None, download_url=LINK, source="prowlarr", info_url=LINK),
            self.good(GOOD + "2", h=2, magnet_uri=MAGNET),
        )
        reply = await self.post("/v1/releases", EPISODE_BODY)
        self.assertEqual(len(reply.body["releases"]), 2)
        text = json.dumps(reply.body)
        for secret in ("SECRET", "magnet", "http", "btih", "a" * 40):
            self.assertNotIn(secret, text)
        for item in reply.body["releases"]:
            self.assertEqual(set(item), {"ref", "title", "source", "size_bytes", "seeders"})

    async def test_they_come_in_pick_best_order_best_first(self) -> None:
        best = self.good(h=1, seeders=5)
        most_seeders_but_720p = self.good("Severance.S02E01.720p.WEB.x264-GROUP", h=2, seeders=900)
        better_seeded = self.good("Severance.S02E01.1080p.BluRay.x264-OTHER", h=3, seeders=50)
        self.use_sources(best, most_seeders_but_720p, better_seeded)
        items = await self.choose()
        self.assertEqual(
            [item["title"] for item in items],
            [better_seeded.title, best.title, most_seeders_but_720p.title],
        )
        self.assertEqual(items[0]["seeders"], 50)
        self.assertEqual(items[0]["size_bytes"], 1000 * MIB)
        self.assertEqual(items[0]["source"], "apibay")

    async def test_what_a_tap_wouldnt_take_is_listed_after_the_rest_but_never_a_fake(self) -> None:
        # "Choose one yourself" must lead somewhere, also when nothing was good enough.
        self.use_sources(
            self.good(h=1, seeders=5),
            self.good("Severance.S02E01-E02.1080p.WEB.x264-PACK", h=3, seeders=99, size=4000 * MIB),
            self.good("Severance.S02E01.1080p.exe", h=2, seeders=500),
        )
        self.assertEqual(
            [item["title"] for item in await self.choose()],
            [GOOD, "Severance.S02E01-E02.1080p.WEB.x264-PACK"],
        )

    async def test_nothing_found_says_so(self) -> None:
        self.use_sources()
        reply = await self.post("/v1/releases", EPISODE_BODY)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["releases"], [])
        self.assertEqual(
            reply.body["message"],
            "No downloads found for Severance S02E01, so try again later.",
        )

    async def test_it_collects_until_the_time_limit_and_answers_with_what_arrived(self) -> None:
        arrived = self.good(h=1)
        self.use_indexers(
            fake("quick", result=[arrived]), fake("slow", gate=asyncio.Event()), timeout=0.05
        )
        items = await self.choose()
        self.assertEqual([item["title"] for item in items], [GOOD])

    async def test_what_isnt_out_yet_has_nothing_to_choose(self) -> None:
        await self.follow(episode(1, TODAY), episode(2, date(2099, 1, 1), number=2))
        reply = await self.post("/v1/releases", {**EPISODE_BODY, "episode": 2})
        self.assertEqual(reply.body["releases"], [])
        self.assertIn("isn't out yet", reply.body["message"])

    async def test_what_isnt_on_the_list_is_no_longer_there(self) -> None:
        reply = await self.post("/v1/releases", MOVIE_BODY)
        self.assertEqual(reply.status, 410)
        self.assertEqual(reply.body["error"], "no_longer_there")

    async def test_grab_takes_exactly_the_one_chosen_for_its_title(self) -> None:
        self.use_sources(self.good(h=1, seeders=5), self.good(GOOD + "-B", h=2, seeders=50))
        items = await self.choose()
        chosen = next(item for item in items if item["title"] == GOOD)
        reply = await self.post("/v1/grab", {"ref": chosen["ref"]})
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["outcome"], "grabbed")
        self.assertEqual(reply.body["message"], "Severance S02E01 · Sent to qBittorrent")
        grabbed, target = self.grabs.grab.await_args.args
        self.assertEqual(grabbed.title, GOOD)
        self.assertEqual(target, EPISODE_TARGET)

    async def test_refs_are_short_random_and_different(self) -> None:
        self.use_sources(self.good(h=1), self.good(GOOD + "-B", h=2))
        refs = [item["ref"] for item in await self.choose()]
        self.assertEqual(len(set(refs)), 2)
        self.assertTrue(all(8 <= len(ref) <= 16 for ref in refs))
        self.assertNotIn(GOOD, refs)

    async def test_unknown_ref_returns_no_longer_there(self) -> None:
        reply = await self.post("/v1/grab", {"ref": "nope"})
        self.assertEqual(reply.status, 410)
        self.assertEqual(reply.body["error"], "no_longer_there")
        self.assertEqual(
            reply.body["message"],
            "That download isn't available any more, so look for downloads again.",
        )
        self.grabs.grab.assert_not_awaited()

    async def test_a_ref_that_isnt_text_is_a_bad_request(self) -> None:
        for body in ({}, {"ref": 1}):
            reply = await self.post("/v1/grab", body)
            self.assertEqual(reply.status, 400)

    async def test_a_ref_lasts_ten_minutes(self) -> None:
        self.assertEqual(RELEASE_TTL_S, 600.0)
        ref = (await self.choose())[0]["ref"]
        self.seconds += RELEASE_TTL_S + 1
        reply = await self.post("/v1/grab", {"ref": ref})
        self.assertEqual(reply.body["error"], "no_longer_there")
        self.grabs.grab.assert_not_awaited()

    async def test_expired_releases_are_let_go_of(self) -> None:
        first = (await self.choose())[0]["ref"]
        self.assertEqual(list(self.service._kept), [first])
        self.seconds += RELEASE_TTL_S + 1
        second = (await self.choose())[0]["ref"]
        self.assertEqual(list(self.service._kept), [second])

    async def test_a_double_tap_grabs_once(self) -> None:
        ref = (await self.choose())[0]["ref"]
        first, second = await asyncio.gather(
            self.post("/v1/grab", {"ref": ref}), self.post("/v1/grab", {"ref": ref})
        )
        self.assertEqual(first.body["outcome"], "grabbed")
        self.assertEqual(second.body["error"], "no_longer_there")
        self.grabs.grab.assert_awaited_once()

    async def test_a_failed_grab_says_why_and_can_be_tried_again(self) -> None:
        ref = (await self.choose())[0]["ref"]
        self.grabs.grab.side_effect = GrabError("The folder is full.")
        reply = await self.post("/v1/grab", {"ref": ref})
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body, {"error": "failed", "message": "The folder is full."})
        self.grabs.grab.side_effect = None
        again = await self.post("/v1/grab", {"ref": ref})
        self.assertEqual(again.body["outcome"], "grabbed")

    async def test_a_chosen_torrent_holding_a_program_says_so(self) -> None:
        ref = (await self.choose())[0]["ref"]
        message = t(PROGRAM_IN_TORRENT, suffix=".exe")
        self.grabs.grab.side_effect = ProgramInTorrent(message)
        reply = await self.post("/v1/grab", {"ref": ref})
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body, {"error": "failed", "message": message})
        self.assertIn(".exe", reply.body["message"])

    async def test_the_log_never_holds_a_release_or_its_link(self) -> None:
        self.use_sources(self.good(h=None, download_url=LINK))
        with self.assertLogs("app.services.phone.downloads", "DEBUG") as logs:
            ref = (await self.choose())[0]["ref"]
            await self.post("/v1/grab", {"ref": ref})
        text = "\n".join(logs.output)
        self.assertNotIn("SECRET", text)
        self.assertNotIn(GOOD, text)
        self.assertIn("Severance S02E01", text)


class RequestShapeTests(DownloadsCase):
    async def test_it_searches_for_the_right_title(self) -> None:
        requests: list[SearchRequest] = []
        real = self.search.search

        def spy(request: SearchRequest) -> Any:
            requests.append(request)
            return real(request)

        with mock.patch.object(self.search, "search", side_effect=spy):
            await self.post("/v1/releases", EPISODE_BODY)
        self.assertEqual(requests[0].kind, SearchKind.EPISODE)
        self.assertEqual((requests[0].season, requests[0].episode), (2, 1))
        self.assertEqual(requests[0].title, "Severance")

    async def test_a_film_searches_with_its_imdb_id_from_the_list(self) -> None:
        self.use_sources()
        await self.want(film(), digital=date(2021, 10, 22))
        await self.db.watchlist.set_release_dates(
            DUNE, imdb_id="tt1160419", digital=date(2021, 10, 22), theatrical=None, when=NOW
        )
        requests: list[SearchRequest] = []
        real = self.search.search

        def spy(request: SearchRequest) -> Any:
            requests.append(request)
            return real(request)

        with mock.patch.object(self.search, "search", side_effect=spy):
            await self.post("/v1/releases", MOVIE_BODY)
        self.assertEqual(requests[0].kind, SearchKind.MOVIE)
        self.assertEqual(requests[0].imdb_id, "tt1160419")


class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_phone_connection_offers_these_routes(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 1.0  # httpx loads its CA bundle
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            services = await Services.create(AppPaths(Path(folder)))
            try:
                routes = services.phone._routes_factory()
            finally:
                await services.aclose()
        self.assertLessEqual(
            {
                ("GET", "/v1/coming-up"),
                ("POST", "/v1/download"),
                ("POST", "/v1/releases"),
                ("POST", "/v1/grab"),
                ("GET", "/v1/suggestions"),
                ("GET", "/v1/shortlist"),
                ("PUT", "/v1/shortlist/{key}"),
                ("DELETE", "/v1/shortlist/{key}"),
                ("DELETE", "/v1/shortlist"),
            },
            set(routes),
        )
