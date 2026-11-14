"""What Plex says about playing (app/services/plex.py, for AirGrabber's own player): where a
video was left and how long it is (`film_video` / `show_videos`), its intro and credits markers
(`markers`), the position reported to Plex's timeline (`report`) and the watched mark
(`mark_watched`). A fake Plex Media Server answers through httpx.MockTransport from
tests/fixtures/plex/; nothing reaches the network, the token only ever in its header."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from app.database import Database
from app.models.plex import PlexTitle
from app.services.plex import PlexClient, PlexError, PlexService, PlexUnreachable
from tests.plex_link import TOKEN, plex_settings

FIXTURES = Path(__file__).parent / "fixtures" / "plex"
SERVER = "https://192-168-1-20.a1b2c3d4e5f6.plex.direct:32400"
CLIENT_ID = "00000000-0000-4000-8000-000000000001"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeServer:
    """A Plex Media Server: film 3001 (left at 1:12:40), episode 42 with its markers, The Ark's
    episodes (1001); the timeline and scrobble calls answer 200 with nothing, or `status`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        film = fixture("metadata_movie.json")
        film["MediaContainer"]["Metadata"][0].update(viewOffset=4360000, duration=9000000)
        self.pages: dict[str, Any] = {
            "/library/metadata/3001": film,
            "/library/metadata/42": fixture("metadata_episode_markers.json"),
            "/library/metadata/1001/allLeaves": fixture("all_leaves.json"),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("X-Plex-Token") != TOKEN:
            return httpx.Response(401)
        if self.status != 200:
            return httpx.Response(self.status, headers={"Location": "https://elsewhere.example/x"})
        path = request.url.path
        if path in ("/:/timeline", "/:/scrobble"):
            return httpx.Response(200)
        if path in self.pages:
            return httpx.Response(200, json=self.pages[path])
        return httpx.Response(404)


class PlayingTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = plex_settings(self.db, plex_client_id=CLIENT_ID, plex_server_uri=SERVER)
        self.server = FakeServer()
        # follow_redirects on the shared client: Plex's requests must still never follow one.
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.server), follow_redirects=True
        )
        self.addAsyncCleanup(self.http.aclose)
        self.plex = PlexService(
            PlexClient(self.http, client_id=lambda: CLIENT_ID), self.db.plex, self.settings
        )

    def last(self) -> httpx.Request:
        return self.server.requests[-1]

    def assert_token_in_header_only(self, request: httpx.Request) -> None:
        self.assertEqual(request.headers["X-Plex-Token"], TOKEN)
        self.assertNotIn(TOKEN, str(request.url))


class PositionTests(PlayingTestCase):
    async def test_film_video_reads_position_and_length(self) -> None:
        video = await self.plex.film_video("3001")
        assert video is not None
        self.assertEqual(video.view_offset_ms, 4360000)
        self.assertEqual(video.duration_ms, 9000000)
        self.assertIsNone(video.intro)
        self.assertIsNone(video.credits_ms)

    async def test_episodes_without_a_position_start_at_zero(self) -> None:
        videos = await self.plex.show_videos(PlexTitle(kind="show", key="1001", title="The Ark"))
        self.assertTrue(videos)
        self.assertEqual({v.view_offset_ms for v in videos}, {0})


class MarkerTests(PlayingTestCase):
    async def test_markers_intro_and_credits(self) -> None:
        self.assertEqual(await self.plex.markers("42"), ((61000, 142000), 2550000))
        asked = self.last()
        self.assertEqual(asked.url.path, "/library/metadata/42")
        self.assertEqual(asked.url.params["includeMarkers"], "1")
        self.assert_token_in_header_only(asked)

    async def test_credits_before_half_the_length_are_passed_over(self) -> None:
        # An opening-credits marker (or a wrong one): the countdown would start in the first act.
        item = self.server.pages["/library/metadata/42"]["MediaContainer"]["Metadata"][0]
        early = {"type": "credits", "startTimeOffset": 45000, "endTimeOffset": 90000}
        item["Marker"] = [early, *item["Marker"]]
        self.assertEqual(await self.plex.markers("42"), ((61000, 142000), 2550000))
        item["Marker"] = [early]
        self.assertEqual(await self.plex.markers("42"), (None, None))
        # Without Plex's length a credits start can't be checked: none.
        item["Marker"] = [{"type": "credits", "startTimeOffset": 2550000}]
        del item["duration"]
        self.assertEqual(await self.plex.markers("42"), (None, None))

    async def test_markers_none(self) -> None:
        self.assertEqual(await self.plex.markers("3001"), (None, None))

    async def test_markers_of_an_item_the_server_doesnt_have(self) -> None:
        self.assertEqual(await self.plex.markers("3999"), (None, None))

    async def test_a_key_that_doesnt_read_is_never_asked(self) -> None:
        for call in (
            self.plex.markers("1/../2"),
            self.plex.report("1/../2", "playing", 0, 1),
            self.plex.mark_watched("1/../2"),
        ):
            with self.assertRaises(PlexError):
                await call
        self.assertEqual(self.server.requests, [])


class ReportTests(PlayingTestCase):
    async def test_report_sends_state_and_time(self) -> None:
        await self.plex.report("42", "paused", 120000, 9000000)
        asked = self.last()
        self.assertEqual(asked.method, "GET")
        self.assertEqual(asked.url.path, "/:/timeline")
        params = asked.url.params
        self.assertEqual(params["ratingKey"], "42")
        self.assertEqual(params["key"], "/library/metadata/42")
        self.assertEqual(params["state"], "paused")
        self.assertEqual(params["time"], "120000")
        self.assertEqual(params["duration"], "9000000")
        self.assert_token_in_header_only(asked)

    async def test_a_redirect_is_never_followed(self) -> None:
        self.server.status = 302
        with self.assertRaises(PlexError):
            await self.plex.report("42", "playing", 0, 9000000)
        self.assertEqual(len(self.server.requests), 1)

    async def test_mark_watched(self) -> None:
        await self.plex.mark_watched("42")
        asked = self.last()
        self.assertEqual(asked.url.path, "/:/scrobble")
        self.assertEqual(asked.url.params["key"], "42")
        self.assertEqual(asked.url.params["identifier"], "com.plexapp.plugins.library")
        self.assert_token_in_header_only(asked)

    async def test_report_never_logs_url(self) -> None:
        self.server.status = 500
        with self.assertLogs("app.services.plex", "DEBUG") as logs:
            with self.assertRaises(PlexError) as caught:
                await self.plex.report("42", "stopped", 120000, 9000000)
            with self.assertRaises(PlexError):
                await self.plex.mark_watched("42")
        for line in logs.output + [str(caught.exception)]:
            self.assertNotIn("http", line)
            self.assertNotIn(TOKEN, line)
            self.assertNotIn("plex.direct", line)

    async def test_without_plex_nothing_is_asked(self) -> None:
        await self.settings.update(plex_token="")
        self.assertEqual(await self.plex.markers("42"), (None, None))
        with self.assertRaises(PlexUnreachable):
            await self.plex.report("42", "playing", 0, 9000000)
        with self.assertRaises(PlexUnreachable):
            await self.plex.mark_watched("42")
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
