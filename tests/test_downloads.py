"""DownloadService against a temp SQLite file, a temp watch folder and httpx.MockTransport."""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from pydantic import ValidationError

from app.config import AppPaths
from app.database import Database
from app.models import (
    DownloadRecord,
    GrabOutcome,
    GrabTarget,
    DownloadMode,
    MediaType,
    RemoteKind,
    Settings,
    TorrentApp,
    TorrentRelease,
)
from app.services import Services
from app.services.downloads import (
    NO_REMOTE_FALLBACK,
    NO_TORRENT_APP,
    DownloadService,
    GrabError,
    prune_torrent_cache,
    safe_filename,
)
from app.services.remote import RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.torrent_app import TorrentAppService
from app.services.settings import SettingsService

HASH = "0123456789abcdef0123456789abcdef01234567"
OTHER_HASH = "89abcdef0123456789abcdef0123456789abcdef"
API_KEY = "PROWLARRKEY42"
DIRECT_URL = f"https://prowlarr.local/api/v1/indexer/3/download?apikey={API_KEY}&file=x"
ITORRENTS = f"https://itorrents.org/torrent/{HASH.upper()}.torrent"
EPISODE = GrabTarget(media_type=MediaType.TV, media_id=42846, season=1, episode=2)


def torrent_bytes(name: str = "Some.Show.S01E02") -> bytes:
    encoded = name.encode()
    return (
        b"d8:announce30:udp://tracker.example.org:1337"
        b"4:infod6:lengthi1048576e4:name" + str(len(encoded)).encode() + b":" + encoded
        + b"12:piece lengthi262144e6:pieces20:" + b"\x01" * 20 + b"ee"
    )


TORRENT = torrent_bytes()
CACHED = torrent_bytes("cached copy")


def release(**changes: Any) -> TorrentRelease:
    values: dict[str, Any] = {
        "title": "Some.Show.S01E02.1080p.WEB.h264-GRP",
        "source": "prowlarr",
        "size_bytes": 1_500_000_000,
        "info_hash": HASH,
    }
    values.update(changes)
    return TorrentRelease(**values)


class FakeRemoteClient:
    """A remote torrent program that records what it was sent."""

    def __init__(self, kind: RemoteKind) -> None:
        self.kind = kind
        self.magnets: list[str] = []
        self.torrents: list[tuple[bytes, str]] = []
        self.error: RemoteError | None = None

    async def test(self) -> str:
        return f"{self.kind.program} 1.0"

    async def add_magnet(self, magnet: str) -> None:
        if self.error is not None:
            raise self.error
        self.magnets.append(magnet)

    async def add_torrent(self, content: bytes, filename: str) -> None:
        if self.error is not None:
            raise self.error
        self.torrents.append((content, filename))

    @property
    def calls(self) -> int:
        return len(self.magnets) + len(self.torrents)


def kind_hash(kind: RemoteKind) -> str:
    """A different info hash per kind (the history keeps one grab per hash)."""
    return str(list(RemoteKind).index(kind) + 1) * 40


# What makes each kind of program configured.
REMOTE_SETTINGS: dict[RemoteKind, dict[str, Any]] = {
    RemoteKind.SYNOLOGY: {
        "remote_url": "http://192.168.1.20:5000",
        "remote_username": "beau",
        "remote_password": "secret",
    },
    RemoteKind.QBITTORRENT: {"remote_url": "http://192.168.1.20:8080"},
    RemoteKind.TRANSMISSION: {"remote_url": "http://192.168.1.30:9091"},
}


class FakeWeb:
    """Serves `routes` (URL → response factory); anything else is a 404. Counts requests."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: Counter[str] = Counter()
        self.user_agents: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests[url] += 1
        self.user_agents.append(request.headers.get("User-Agent", ""))
        if self.gate is not None:
            await self.gate.wait()
        factory = self.routes.get(url)
        if factory is None:
            return httpx.Response(404, text="<html>Not found</html>")
        response: httpx.Response = factory()
        return response


class DownloadTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.watch = root / "nas" / "watch"  # created on the first grab
        self.db = await Database.open(root / "test.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = SettingsService(self.db.settings, Settings(download_dir=self.watch))
        self.web = FakeWeb()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.web))
        self.addAsyncCleanup(self.http.aclose)
        self.clients = {kind: FakeRemoteClient(kind) for kind in RemoteKind}
        self.remote = RemoteDownloads(self.settings, clients=self.clients)
        self.station = self.clients[RemoteKind.SYNOLOGY]
        self.cache = root / "data" / "torrents"
        self.opened: list[str] = []
        self.app: TorrentApp | None = TorrentApp(name="qBittorrent")
        self.service = DownloadService(
            self.http,
            self.db.history,
            self.settings,
            self.remote,
            self.cache,
            find_app=lambda: self.app,
            opener=self.opened.append,  # never the real os.startfile
        )
        self.notifications = 0
        self.service.subscribe(self.on_grab)

    async def set_up_remote(self, kind: RemoteKind = RemoteKind.SYNOLOGY) -> FakeRemoteClient:
        await self.settings.update(remote_kind=kind, **REMOTE_SETTINGS[kind])
        return self.clients[kind]

    def remote_calls(self) -> int:
        return sum(client.calls for client in self.clients.values())

    def on_grab(self) -> None:
        self.notifications += 1

    def serve(
        self, url: str, content: bytes, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.web.routes[url] = lambda: httpx.Response(status, content=content, headers=headers)

    def watch_files(self) -> list[str]:
        return sorted(path.name for path in self.watch.iterdir()) if self.watch.exists() else []

    async def history(self) -> list[DownloadRecord]:
        return await self.db.history.recent()


class TorrentFileTests(DownloadTestCase):
    async def test_direct_link_torrent_is_saved(self) -> None:
        self.serve(DIRECT_URL, TORRENT)
        result = await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertIs(result.outcome, GrabOutcome.SAVED)
        expected = self.watch / "Some.Show.S01E02.1080p.WEB.h264-GRP.torrent"
        self.assertEqual(result.path, str(expected))
        self.assertEqual(expected.read_bytes(), TORRENT)
        self.assertEqual(self.watch_files(), [expected.name])  # no temp file left behind
        self.assertEqual(self.web.requests[ITORRENTS], 0)
        self.assertIn("Mozilla/5.0", self.web.user_agents[0])
        self.assertIn(expected.name, result.message)
        self.assertEqual(self.remote_calls(), 0)

    async def test_grab_listeners_hear_the_target_first(self) -> None:
        heard: list[object] = []
        self.service.subscribe(lambda: heard.append("grabbed"))
        unsubscribe = self.service.subscribe_grabs(heard.append)
        self.serve(DIRECT_URL, TORRENT)
        await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertEqual(heard, [EPISODE, "grabbed"])
        unsubscribe()
        unsubscribe()
        with self.assertRaises(GrabError):  # a failed grab: nobody hears of it
            await self.service.grab(release(), EPISODE)
        self.assertEqual(heard, [EPISODE, "grabbed"])

    async def test_html_instead_of_a_torrent_falls_back_to_itorrents(self) -> None:
        self.serve(DIRECT_URL, b"<!DOCTYPE html><html>Login required</html>")
        self.serve(ITORRENTS, CACHED)
        result = await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertIs(result.outcome, GrabOutcome.SAVED)
        self.assertEqual(Path(result.path or "").read_bytes(), CACHED)
        self.assertEqual(self.web.requests[ITORRENTS], 1)

    async def test_failing_direct_link_falls_back_to_itorrents(self) -> None:
        self.serve(DIRECT_URL, b"", status=500)
        self.serve(ITORRENTS, CACHED)
        result = await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertEqual(Path(result.path or "").read_bytes(), CACHED)

    async def test_itorrents_by_uppercase_hash(self) -> None:
        self.serve(ITORRENTS, CACHED)
        result = await self.service.grab(release(), EPISODE)
        self.assertIs(result.outcome, GrabOutcome.SAVED)
        self.assertEqual(list(self.web.requests), [ITORRENTS])

    async def test_gzipped_cache_reply_is_unpacked(self) -> None:
        self.serve(ITORRENTS, gzip.compress(CACHED))
        result = await self.service.grab(release(), EPISODE)
        self.assertEqual(Path(result.path or "").read_bytes(), CACHED)

    async def test_oversized_reply_is_rejected(self) -> None:
        self.serve(DIRECT_URL, TORRENT, headers={"Content-Length": str(11 * 1024 * 1024)})
        self.serve(ITORRENTS, CACHED)
        result = await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertEqual(Path(result.path or "").read_bytes(), CACHED)

    async def test_redirect_to_a_magnet_supplies_the_hash(self) -> None:
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=x"
        self.web.routes[DIRECT_URL] = lambda: httpx.Response(302, headers={"Location": magnet})
        self.serve(ITORRENTS, CACHED)
        hashless = release(info_hash=None, download_url=DIRECT_URL)
        result = await self.service.grab(hashless, EPISODE)
        self.assertIs(result.outcome, GrabOutcome.SAVED)
        self.assertEqual((await self.history())[0].info_hash, HASH)

    async def test_http_redirects_are_followed(self) -> None:
        mirror = "https://mirror.example/file.torrent"
        self.web.routes[DIRECT_URL] = lambda: httpx.Response(301, headers={"Location": mirror})
        self.serve(mirror, TORRENT)
        result = await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertEqual(Path(result.path or "").read_bytes(), TORRENT)

    async def test_the_direct_link_never_reaches_the_logs(self) -> None:
        self.serve(DIRECT_URL, b"<html></html>")
        with self.assertLogs(level=logging.DEBUG) as logs:
            with self.assertRaises(GrabError) as caught:
                await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        # httpx's own request log is kept at WARNING by app.config, as for the TMDB key.
        own = "\n".join(line for line in logs.output if ":httpx:" not in line)
        self.assertIn("No .torrent file from prowlarr", own)
        self.assertNotIn(API_KEY, own)
        self.assertNotIn("prowlarr.local", own)
        self.assertNotIn(API_KEY, str(caught.exception))


class RemoteFallbackTests(DownloadTestCase):
    """FOLDER mode: a release without a .torrent file goes to the remote program, if any."""

    async def test_nothing_cached_sends_the_magnet(self) -> None:
        await self.set_up_remote()
        item = release()
        result = await self.service.grab(item, EPISODE)
        self.assertIs(result.outcome, GrabOutcome.SENT)
        self.assertEqual(result.message, "Sent to Download Station")
        self.assertIsNone(result.path)
        self.assertEqual(self.station.magnets, [item.magnet])
        self.assertEqual(self.watch_files(), [])
        record = (await self.history())[0]
        self.assertEqual(record.saved_path, "Download Station")
        self.assertEqual(self.notifications, 1)

    async def test_each_kind_of_program_gets_the_magnet(self) -> None:
        for kind in RemoteKind:
            with self.subTest(kind=kind):
                client = await self.set_up_remote(kind)
                item = release(title=f"Show {kind}", info_hash=kind_hash(kind))
                result = await self.service.grab(item, EPISODE)
                self.assertEqual(result.message, f"Sent to {kind.program}")
                self.assertEqual(client.magnets, [item.magnet])
                self.assertEqual(client.torrents, [])
        self.assertEqual(self.remote_calls(), 3)
        saved = {record.saved_path for record in await self.history()}
        self.assertEqual(saved, {kind.program for kind in RemoteKind})

    async def test_release_magnet_uri_is_sent_as_is(self) -> None:
        await self.set_up_remote()
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=Original&tr=udp://tracker"
        await self.service.grab(release(info_hash=None, magnet_uri=magnet), EPISODE)
        self.assertEqual(self.station.magnets, [magnet])

    async def test_nothing_cached_and_no_remote_program(self) -> None:
        # Download Station without an account doesn't count as set up.
        await self.settings.update(remote_url="http://192.168.1.20:5000")
        with self.assertRaises(GrabError) as caught:
            await self.service.grab(release(), EPISODE)
        self.assertEqual(str(caught.exception), NO_REMOTE_FALLBACK)
        self.assertEqual(await self.history(), [])
        self.assertEqual(self.notifications, 0)
        self.assertEqual(self.remote_calls(), 0)

    async def test_no_magnet_to_send(self) -> None:
        await self.set_up_remote(RemoteKind.QBITTORRENT)
        self.serve(DIRECT_URL, b"<html></html>")
        with self.assertRaises(GrabError) as caught:
            await self.service.grab(release(info_hash=None, download_url=DIRECT_URL), EPISODE)
        self.assertIn("no magnet link to send to qBittorrent", str(caught.exception))
        self.assertEqual(self.remote_calls(), 0)

    async def test_remote_errors_propagate_and_record_nothing(self) -> None:
        await self.set_up_remote()
        self.station.error = RemoteError("DSM rejected the password.")
        with self.assertRaises(RemoteError):
            await self.service.grab(release(), EPISODE)
        self.assertEqual(await self.history(), [])
        self.assertEqual(self.notifications, 0)

    async def test_cancellation_propagates(self) -> None:
        self.web.gate = asyncio.Event()
        task = asyncio.create_task(self.service.grab(release(), EPISODE))
        while not self.web.requests:
            await asyncio.sleep(0.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.watch_files(), [])


class FileNameTests(DownloadTestCase):
    def test_safe_filename(self) -> None:
        cases = {
            'Bad: Name/With*Chars? "Quoted" <x>|y\\z': "Bad NameWithChars Quoted xyz",
            "Tabs\tand\nnew\x00lines   collapse": "Tabs and new lines collapse",
            "  ..Leading and trailing..  ": "Leading and trailing",
            '<>:"/\\|?*': "torrent",
            "": "torrent",
            "CON": "_CON",
            "nul.S01E01": "_nul.S01E01",
            "Consortium.S01": "Consortium.S01",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(safe_filename(title), expected)

    def test_long_titles_are_cut_to_120_characters(self) -> None:
        name = safe_filename("A" * 100 + " " + "B" * 100)
        self.assertEqual(len(name), 120)
        self.assertEqual(safe_filename("A" * 119 + " B"), "A" * 119)  # no trailing space

    async def test_collisions_get_a_number(self) -> None:
        self.watch.mkdir(parents=True)
        (self.watch / "Same Title.torrent").write_bytes(b"existing")
        other = f"https://itorrents.org/torrent/{OTHER_HASH.upper()}.torrent"
        self.serve(ITORRENTS, CACHED)
        self.serve(other, TORRENT)
        first = await self.service.grab(release(title="Same Title"), EPISODE)
        renamed = release(title="Same: Title", info_hash=OTHER_HASH)  # same name once cleaned
        second = await self.service.grab(renamed, EPISODE)
        self.assertEqual(Path(first.path or "").name, "Same Title (2).torrent")
        self.assertEqual(Path(second.path or "").name, "Same Title (3).torrent")
        self.assertEqual((self.watch / "Same Title.torrent").read_bytes(), b"existing")

    async def test_unwritable_folder(self) -> None:
        blocker = Path(self.tmp.name) / "not-a-folder"
        blocker.write_text("a file where the folder should be")
        await self.settings.update(download_dir=blocker)
        self.serve(ITORRENTS, CACHED)
        with self.assertRaises(GrabError) as caught:
            await self.service.grab(release(), EPISODE)
        self.assertIn(f"Can't write to the download folder {blocker}", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(await self.history(), [])
        self.assertEqual(self.notifications, 0)


class HistoryTests(DownloadTestCase):
    async def test_episode_grab_is_recorded(self) -> None:
        self.serve(ITORRENTS, CACHED)
        result = await self.service.grab(release(), EPISODE)
        [record] = await self.history()
        self.assertEqual(
            (record.media_type, record.media_id, record.season, record.episode),
            (MediaType.TV, 42846, 1, 2),
        )
        self.assertEqual(record.release_title, "Some.Show.S01E02.1080p.WEB.h264-GRP")
        self.assertEqual(record.info_hash, HASH)
        self.assertEqual(record.source, "prowlarr")
        self.assertEqual(record.size_bytes, 1_500_000_000)
        self.assertEqual(record.saved_path, result.path)
        self.assertEqual(self.notifications, 1)
        self.assertTrue(await self.db.history.has_episode(42846, 1, 2))

    async def test_season_pack_records_no_episode(self) -> None:
        self.serve(ITORRENTS, CACHED)
        pack = GrabTarget(media_type=MediaType.TV, media_id=42846, season=3)
        await self.service.grab(release(title="Some.Show.S03.1080p"), pack)
        [record] = await self.history()
        self.assertEqual((record.season, record.episode), (3, None))
        self.assertTrue(await self.db.history.has_episode(42846, 3, 7))
        self.assertEqual(self.notifications, 1)

    async def test_movie_grab(self) -> None:
        self.serve(ITORRENTS, CACHED)
        movie = GrabTarget(media_type=MediaType.MOVIE, media_id=557)
        await self.service.grab(release(title="Spider-Man.2002.1080p", size_bytes=0), movie)
        [record] = await self.history()
        self.assertEqual(
            (record.media_type, record.media_id, record.season), (MediaType.MOVIE, 557, None)
        )
        self.assertIsNone(record.size_bytes)  # 0 = unknown
        self.assertTrue(await self.db.history.has_movie(557))

    async def test_has_grabbed(self) -> None:
        movie = GrabTarget(media_type=MediaType.MOVIE, media_id=557)
        pack = GrabTarget(media_type=MediaType.TV, media_id=42846, season=3)
        in_pack = GrabTarget(media_type=MediaType.TV, media_id=42846, season=3, episode=7)
        for target in (EPISODE, movie, pack, in_pack):
            self.assertFalse(await self.service.has_grabbed(target))
        self.serve(ITORRENTS, CACHED)
        self.serve(f"https://itorrents.org/torrent/{OTHER_HASH.upper()}.torrent", TORRENT)
        await self.service.grab(release(), EPISODE)
        await self.service.grab(release(title="Spider-Man.2002", info_hash=OTHER_HASH), movie)
        self.assertTrue(await self.service.has_grabbed(EPISODE))
        self.assertTrue(await self.service.has_grabbed(movie))
        self.assertFalse(await self.service.has_grabbed(in_pack))
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV, media_id=42846, season=3, release_title="Pack S03"
            )
        )
        self.assertTrue(await self.service.has_grabbed(pack))
        self.assertTrue(await self.service.has_grabbed(in_pack))

    async def test_grabbed_hashes(self) -> None:
        self.assertEqual(await self.service.grabbed_hashes(), frozenset())
        self.serve(ITORRENTS, CACHED)
        self.serve(f"https://itorrents.org/torrent/{OTHER_HASH.upper()}.torrent", TORRENT)
        await self.service.grab(release(), EPISODE)
        await self.service.grab(release(title="Other", info_hash=OTHER_HASH), EPISODE)
        self.assertEqual(await self.service.grabbed_hashes(), {HASH, OTHER_HASH})

    async def test_failing_listener_is_logged_and_unsubscribe_works(self) -> None:
        def broken() -> None:
            raise RuntimeError("boom")

        unsubscribe = self.service.subscribe(broken)
        self.serve(ITORRENTS, CACHED)
        with self.assertLogs("app.services.downloads", "ERROR"):
            await self.service.grab(release(), EPISODE)
        self.assertEqual(self.notifications, 1)
        unsubscribe()
        unsubscribe()
        await self.service.grab(release(title="Again"), EPISODE)
        self.assertEqual(self.notifications, 2)

    async def test_history_failure_does_not_fail_the_grab(self) -> None:
        self.serve(ITORRENTS, CACHED)
        with (
            mock.patch.object(self.db.history, "add", side_effect=RuntimeError("disk full")),
            self.assertLogs("app.services.downloads", "ERROR"),
        ):
            result = await self.service.grab(release(), EPISODE)
        self.assertIs(result.outcome, GrabOutcome.SAVED)

    def test_tv_targets_need_a_season(self) -> None:
        with self.assertRaises(ValidationError):
            GrabTarget(media_type=MediaType.TV, media_id=1)
        with self.assertRaises(ValidationError):
            GrabTarget(media_type=MediaType.MOVIE, media_id=1, episode=3)


class AppModeTests(DownloadTestCase):
    """DownloadMode.APP: open in the torrent program (a fake opener; nothing really opens)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.set_up_remote()  # must still not be used in APP mode
        await self.settings.update(download_mode=DownloadMode.APP)

    def cache_files(self) -> list[str]:
        return sorted(path.name for path in self.cache.iterdir()) if self.cache.exists() else []

    async def test_torrent_is_saved_in_the_cache_and_opened(self) -> None:
        self.serve(DIRECT_URL, TORRENT)
        result = await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertIs(result.outcome, GrabOutcome.OPENED)
        self.assertEqual(result.message, "Opened in your torrent program")
        expected = self.cache / "Some.Show.S01E02.1080p.WEB.h264-GRP.torrent"
        self.assertEqual(result.path, str(expected))
        self.assertEqual(expected.read_bytes(), TORRENT)
        self.assertEqual(self.opened, [str(expected)])
        self.assertEqual(self.cache_files(), [expected.name])
        self.assertEqual(self.watch_files(), [])  # the watch folder is not used
        self.assertEqual(self.remote_calls(), 0)

    async def test_itorrents_copy_is_opened(self) -> None:
        self.serve(ITORRENTS, CACHED)
        result = await self.service.grab(release(), EPISODE)
        self.assertEqual(Path(result.path or "").read_bytes(), CACHED)
        self.assertEqual(self.opened, [result.path])

    async def test_magnet_only_release_opens_its_magnet(self) -> None:
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=Original&tr=udp://tracker"
        result = await self.service.grab(release(info_hash=None, magnet_uri=magnet), EPISODE)
        self.assertIs(result.outcome, GrabOutcome.OPENED)
        self.assertIsNone(result.path)
        self.assertEqual(self.opened, [magnet])
        self.assertEqual(self.web.requests[ITORRENTS], 1)  # a .torrent is still preferred
        self.assertEqual(self.cache_files(), [])
        self.assertEqual(self.remote_calls(), 0)

    async def test_prowlarr_redirect_to_a_magnet_opens_it(self) -> None:
        magnet = f"magnet:?xt=urn:btih:{OTHER_HASH}&dn=Redirected"
        self.web.routes[DIRECT_URL] = lambda: httpx.Response(302, headers={"Location": magnet})
        result = await self.service.grab(
            release(info_hash=None, download_url=DIRECT_URL), EPISODE
        )
        self.assertIs(result.outcome, GrabOutcome.OPENED)
        self.assertEqual(self.opened, [magnet])
        self.assertEqual((await self.history())[0].info_hash, OTHER_HASH)

    async def test_hash_only_release_opens_a_built_magnet(self) -> None:
        title = "Show & Tell / S01E02 [1080p] #1?"
        await self.service.grab(release(title=title), EPISODE)
        self.assertEqual(
            self.opened,
            [
                f"magnet:?xt=urn:btih:{HASH}"
                "&dn=Show%20%26%20Tell%20%2F%20S01E02%20%5B1080p%5D%20%231%3F"
            ],
        )

    async def test_no_torrent_program_fails_before_any_request(self) -> None:
        self.app = None
        self.serve(DIRECT_URL, TORRENT)
        with self.assertRaises(GrabError) as caught:
            await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertEqual(str(caught.exception), NO_TORRENT_APP)
        self.assertIn('under "When I download" in Settings.', NO_TORRENT_APP)
        self.assertEqual(sum(self.web.requests.values()), 0)
        self.assertEqual(self.opened, [])
        self.assertEqual(await self.history(), [])
        self.assertEqual(self.notifications, 0)

    async def test_history_records_the_opened_grab(self) -> None:
        self.serve(ITORRENTS, CACHED)
        await self.service.grab(release(), EPISODE)
        record = (await self.history())[0]
        self.assertEqual(record.saved_path, "qBittorrent")
        self.assertEqual(record.info_hash, HASH)
        self.assertEqual(record.season, 1)
        self.assertEqual(self.notifications, 1)
        self.assertEqual(await self.service.grabbed_hashes(), frozenset({HASH}))

    async def test_open_failure_is_a_grab_error_without_the_magnet(self) -> None:
        def refuse(target: str) -> None:
            raise OSError(2, "No application is associated")

        self.service._opener = refuse
        with self.assertRaises(GrabError) as caught:
            await self.service.grab(release(), EPISODE)
        self.assertNotIn("magnet", str(caught.exception))
        self.assertIn("qBittorrent", str(caught.exception))
        self.assertEqual(await self.history(), [])

    async def test_magnets_are_not_logged(self) -> None:
        with self.assertLogs("app.services.downloads", "INFO") as logs:
            await self.service.grab(release(), EPISODE)
        self.assertNotIn("magnet:", "\n".join(logs.output))
        self.assertNotIn(HASH, "\n".join(logs.output))

    async def test_cache_is_pruned_to_the_newest_files(self) -> None:
        self.cache.mkdir(parents=True)
        for number in range(55):
            old = self.cache / f"old {number}.torrent"
            old.write_bytes(CACHED)
            os.utime(old, (1_000_000 + number, 1_000_000 + number))
        self.serve(ITORRENTS, CACHED)
        result = await self.service.grab(release(), EPISODE)
        files = self.cache_files()
        self.assertEqual(len(files), 50)
        self.assertIn(Path(result.path or "").name, files)
        self.assertNotIn("old 0.torrent", files)  # the oldest went first
        self.assertIn("old 54.torrent", files)

    def test_prune_keeps_other_files_and_tolerates_a_missing_folder(self) -> None:
        prune_torrent_cache(self.cache, 1)  # no folder yet
        self.cache.mkdir(parents=True)
        (self.cache / "a.torrent").write_bytes(b"x")
        (self.cache / "notes.txt").write_bytes(b"x")
        prune_torrent_cache(self.cache, 0)
        self.assertEqual(self.cache_files(), ["notes.txt"])


class RemoteModeTests(DownloadTestCase):
    """DownloadMode.REMOTE: everything goes to the program on the NAS or other computer."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.settings.update(download_mode=DownloadMode.REMOTE)

    async def test_the_torrent_file_is_sent_to_each_kind(self) -> None:
        self.serve(DIRECT_URL, TORRENT)
        for kind in RemoteKind:
            with self.subTest(kind=kind):
                client = await self.set_up_remote(kind)
                item = release(download_url=DIRECT_URL, info_hash=kind_hash(kind))
                result = await self.service.grab(item, EPISODE)
                self.assertIs(result.outcome, GrabOutcome.SENT)
                self.assertEqual(result.message, f"Sent to {kind.program}")
                self.assertIsNone(result.path)
                self.assertEqual(
                    client.torrents, [(TORRENT, "Some.Show.S01E02.1080p.WEB.h264-GRP.torrent")]
                )
                self.assertEqual(client.magnets, [])
        records = await self.history()
        self.assertEqual({r.saved_path for r in records}, {k.program for k in RemoteKind})
        self.assertEqual(self.watch_files(), [])
        self.assertEqual(self.opened, [])
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.notifications, 3)

    async def test_itorrents_copy_is_sent(self) -> None:
        client = await self.set_up_remote(RemoteKind.TRANSMISSION)
        self.serve(ITORRENTS, CACHED)
        await self.service.grab(release(), EPISODE)
        self.assertEqual([content for content, _ in client.torrents], [CACHED])

    async def test_magnet_only_release_sends_its_own_magnet(self) -> None:
        client = await self.set_up_remote(RemoteKind.QBITTORRENT)
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=Original&tr=udp://tracker"
        result = await self.service.grab(release(info_hash=None, magnet_uri=magnet), EPISODE)
        self.assertEqual(result.message, "Sent to qBittorrent")
        self.assertEqual(client.magnets, [magnet])
        self.assertEqual(self.web.requests[ITORRENTS], 1)  # a .torrent is still preferred

    async def test_prowlarr_redirect_to_a_magnet_is_sent(self) -> None:
        client = await self.set_up_remote(RemoteKind.QBITTORRENT)
        magnet = f"magnet:?xt=urn:btih:{OTHER_HASH}&dn=Redirected"
        self.web.routes[DIRECT_URL] = lambda: httpx.Response(302, headers={"Location": magnet})
        await self.service.grab(release(info_hash=None, download_url=DIRECT_URL), EPISODE)
        self.assertEqual(client.magnets, [magnet])
        self.assertEqual((await self.history())[0].info_hash, OTHER_HASH)

    async def test_hash_only_release_sends_a_built_magnet(self) -> None:
        client = await self.set_up_remote()
        await self.service.grab(release(title="Show & Tell"), EPISODE)
        self.assertEqual(client.magnets, [f"magnet:?xt=urn:btih:{HASH}&dn=Show%20%26%20Tell"])

    async def test_not_set_up_fails_before_any_request(self) -> None:
        await self.settings.update(remote_url="http://192.168.1.20:5000")  # no DSM account
        self.serve(DIRECT_URL, TORRENT)
        with self.assertRaises(GrabError) as caught:
            await self.service.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertEqual(
            str(caught.exception),
            "Download Station isn't set up yet: enter its address, user name and password in "
            "Settings.",
        )
        self.assertEqual(sum(self.web.requests.values()), 0)
        self.assertEqual(await self.history(), [])
        await self.settings.update(remote_kind=RemoteKind.QBITTORRENT, remote_url="")
        with self.assertRaises(GrabError) as caught:
            await self.service.grab(release(), EPISODE)
        self.assertEqual(
            str(caught.exception), "qBittorrent isn't set up yet: enter its address in Settings."
        )

    async def test_remote_errors_propagate_and_record_nothing(self) -> None:
        client = await self.set_up_remote(RemoteKind.TRANSMISSION)
        client.error = RemoteError("Transmission: wrong user name or password.")
        self.serve(ITORRENTS, CACHED)
        with self.assertRaises(RemoteError):
            await self.service.grab(release(), EPISODE)
        self.assertEqual(await self.history(), [])
        self.assertEqual(self.notifications, 0)

    async def test_nothing_at_all_to_send(self) -> None:
        await self.set_up_remote()
        self.serve(DIRECT_URL, b"<html></html>")
        with self.assertRaises(GrabError) as caught:
            await self.service.grab(release(info_hash=None, download_url=DIRECT_URL), EPISODE)
        self.assertIn("No .torrent file or magnet link", str(caught.exception))


class FolderModeTests(DownloadTestCase):
    async def test_folder_is_the_default_and_never_opens_anything(self) -> None:
        self.assertIs(Settings().download_mode, DownloadMode.FOLDER)
        self.app = None  # detection is not consulted in FOLDER mode
        self.serve(ITORRENTS, CACHED)
        result = await self.service.grab(release(), EPISODE)
        self.assertIs(result.outcome, GrabOutcome.SAVED)
        self.assertEqual(self.opened, [])
        self.assertFalse(self.cache.exists())


class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_expose_downloads_and_the_station(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        asyncio.get_running_loop().slow_callback_duration = 1.0  # real CA bundle load
        services = await Services.create(AppPaths(Path(tmp.name)))
        try:
            self.assertIsInstance(services.downloads, DownloadService)
            self.assertIsInstance(services.remote, RemoteDownloads)
            self.assertIs(services.downloads._remote, services.remote)
            self.assertIs(services.station, services.remote)  # main_window.py's older name
            self.assertIs(services.downloads._http, services._http)
            self.assertFalse(services.remote.configured)
            self.assertIs(services.remote.kind, RemoteKind.SYNOLOGY)
            self.assertEqual(services.downloads._torrent_cache, Path(tmp.name) / "torrents")
            self.assertIsInstance(services.torrent_app, TorrentAppService)
        finally:
            await services.aclose()


if __name__ == "__main__":
    unittest.main()
