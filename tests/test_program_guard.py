"""The magnet guard (app/services/program_guard.py): a magnet sent to the torrent program is
watched until its files are known; one holding a program is removed with its files, refused for
good, taken out of the history and told, and its episode fetched again without it.

A real DownloadService and history (temp SQLite), a fake torrent program (no network), a fake
clock; the guard's rounds are run by hand (`check_due`) or by `run` with a fake sleep."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.database import Database
from app.models import (
    DownloadMode,
    DownloadRecord,
    DownloadState,
    GrabTarget,
    MediaType,
    RemoteKind,
    RemoteStatus,
    RemoteTask,
    Settings,
    Show,
    TorrentApp,
)
from app.services.downloads import DownloadService, ProgramInTorrent
from app.services.program_guard import (
    NOT_LISTED_GRACE,
    WATCH_FOR,
    CaughtProgram,
    ProgramGuard,
    caught_notice,
    check_interval,
)
from app.services.remote import FilesUnknown, RemoteError, RemoteStatusClient, RemoteTaskFiles
from app.services.remote_downloads import RemoteDownloads
from app.services.settings import SettingsService
from tests.test_downloads import (
    DIRECT_URL,
    EPISODE,
    HASH,
    REMOTE_SETTINGS,
    TORRENT,
    FakeWeb,
    release,
)

START = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)
GOLD_RUSH = Show(id=EPISODE.media_id, name="Gold Rush")
PROGRAM_FILE = "Gold Rush S17E08 Carrying Parkers Water [1080p] [WEB-DL] [MeGusta].exe"
TASK_NAME = "TaskNameFromTheProgram"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeProgram:
    """A torrent program with tasks, their files (None: no metadata yet) and the guard's calls."""

    def __init__(self, kind: RemoteKind) -> None:
        self.kind = kind
        self.magnets: list[str] = []
        self.torrents: list[str] = []
        self.tasks: dict[str, RemoteTask] = {}
        self.files: dict[str, tuple[str, ...] | None] = {}
        self.status_calls = 0
        self.file_calls = 0
        self.removed: list[list[str]] = []
        self.down = False
        self.fail_remove = False
        self.files_unknown = False

    def has(
        self,
        info_hash: str,
        files: tuple[str, ...] | None = None,
        *,
        state: DownloadState = DownloadState.WAITING,
        progress: float = 0.0,
    ) -> None:
        task_id = "dbid_7" if self.kind is RemoteKind.SYNOLOGY else info_hash
        self.tasks[info_hash] = RemoteTask(
            name=TASK_NAME, info_hash=info_hash, task_id=task_id, state=state, progress=progress
        )
        self.files[info_hash] = files

    async def test(self) -> str:
        return self.kind.program

    async def add_magnet(self, magnet: str, *, movie: bool = False) -> None:
        self.magnets.append(magnet)

    async def add_torrent(self, content: bytes, filename: str, *, movie: bool = False) -> None:
        self.torrents.append(filename)

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        self.status_calls += 1
        if self.down:
            raise RemoteError("Couldn't reach 192.168.1.20:8080.")
        return RemoteStatus(program=self.kind.program, tasks=tuple(self.tasks.values()))

    async def free_space(self) -> None:
        return None

    async def file_names(self, task: RemoteTask) -> tuple[str, ...] | None:
        self.file_calls += 1
        if self.files_unknown:
            raise FilesUnknown("Download Station on 192.168.1.20:5000 doesn't say.")
        return self.files.get(task.info_hash or "")

    async def remove_with_files(self, task_ids: Collection[str]) -> bool:
        if self.fail_remove:
            raise RemoteError("qBittorrent at 192.168.1.20:8080 didn't answer in time.")
        self.removed.append(list(task_ids))
        for info_hash, task in list(self.tasks.items()):
            if task.task_id in task_ids:
                del self.tasks[info_hash]
        return self.kind is not RemoteKind.SYNOLOGY  # Download Station keeps a finished file


class GuardTestCase(unittest.IsolatedAsyncioTestCase):
    kind = RemoteKind.QBITTORRENT

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.db = await Database.open(root / "test.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = SettingsService(
            self.db.settings,
            Settings(
                download_dir=root / "watch",
                download_mode=DownloadMode.REMOTE,
                remote_kind=self.kind,
                **REMOTE_SETTINGS[self.kind],
            ),
        )
        self.web = FakeWeb()  # no .torrent anywhere: magnets go
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.web))
        self.addAsyncCleanup(self.http.aclose)
        self.programs = {kind: FakeProgram(kind) for kind in RemoteKind}
        self.program = self.programs[self.kind]
        self.remote = RemoteDownloads(self.settings, clients=self.programs)
        self.downloads = DownloadService(
            self.http, self.db.history, self.settings, self.remote, root / "torrents"
        )
        self.clock = Clock()
        self.shows: dict[int, Show] = {GOLD_RUSH.id: GOLD_RUSH}
        self.guard = ProgramGuard(
            self.remote,
            self.settings,
            self.db.history,
            self.downloads,
            show=self.show,
            now=self.clock,
        )
        self.downloads.subscribe_magnets(self.guard.watch)
        self.caught: list[CaughtProgram] = []
        self.guard.subscribe(self.caught.append)

    async def show(self, show_id: int) -> Show | None:
        return self.shows.get(show_id)

    async def tick(self, seconds: float) -> None:
        self.clock.advance(seconds)
        await self.guard.check_due()

    async def grab_magnet(self, **changes: Any) -> str:
        await self.downloads.grab(release(**changes), EPISODE)
        self.assertEqual(len(self.program.magnets), 1)
        return HASH


class ProgramCaughtTests(GuardTestCase):
    async def test_a_program_is_removed_refused_forgotten_and_told(self) -> None:
        info_hash = await self.grab_magnet()
        self.assertEqual(self.guard.watching, {info_hash})
        self.program.has(info_hash)  # no metadata yet
        await self.tick(2)
        self.assertEqual(self.program.file_calls, 1)
        self.assertEqual(self.guard.watching, {info_hash})
        self.program.files[info_hash] = ("readme.txt", PROGRAM_FILE)
        await self.tick(2)
        # Removed with its files.
        self.assertEqual(self.program.removed, [[info_hash]])
        self.assertEqual(self.guard.watching, frozenset())
        # Never sent again.
        self.assertIn(info_hash, self.settings.current.refused_hashes)
        with self.assertRaises(ProgramInTorrent):
            await self.downloads.grab(release(), EPISODE)
        self.assertEqual(len(self.program.magnets), 1)
        # No longer counts as fetched.
        self.assertEqual(await self.db.history.every_grab(), [])
        self.assertFalse(await self.downloads.has_grabbed(EPISODE))
        # Told, with what to fetch again and what to leave out.
        [caught] = self.caught
        self.assertEqual(caught.title, "Gold Rush S01E02")
        self.assertEqual(caught.file_name, PROGRAM_FILE)
        self.assertFalse(caught.file_may_remain)  # qBittorrent deleted it
        assert caught.entry is not None
        self.assertEqual(caught.entry.show, GOLD_RUSH)
        self.assertEqual(
            (caught.entry.record.media_type, caught.entry.record.episode),
            (MediaType.TV, 2),
        )
        self.assertIn(info_hash, caught.avoid)
        self.assertIn(release().title.casefold(), caught.avoid)
        notice = caught_notice(caught)
        self.assertEqual(notice.title, "Removed Gold Rush S01E02")
        self.assertEqual(
            notice.body, "It was a program, not a video. AirGrabber is fetching another release."
        )
        self.assertNotIn("downloads folder", notice.status)

    async def test_a_film_is_told_by_its_title(self) -> None:
        film = GrabTarget(media_type=MediaType.MOVIE, media_id=558449)
        await self.downloads.grab(release(title="Gladiator.II.2024.1080p.WEB-DL"), film)
        self.program.has(HASH, ("Gladiator.II.2024.1080p.WEB-DL.mkv.scr",))
        await self.tick(2)
        [caught] = self.caught
        self.assertEqual(caught.title, "Gladiator II (2024)")
        assert caught.entry is not None
        self.assertEqual(caught.entry.record.media_id, 558449)
        self.assertFalse(await self.downloads.has_grabbed(film))

    async def test_an_unknown_show_is_told_but_not_fetched_again(self) -> None:
        self.shows.clear()
        info_hash = await self.grab_magnet()
        self.program.has(info_hash, (PROGRAM_FILE,))
        await self.tick(2)
        [caught] = self.caught
        self.assertIsNone(caught.entry)
        self.assertEqual(caught.title, release().title)
        self.assertNotIn("fetching", caught_notice(caught).body)

    async def test_a_removal_that_fails_is_tried_again_and_refused_meanwhile(self) -> None:
        info_hash = await self.grab_magnet()
        self.program.has(info_hash, (PROGRAM_FILE,))
        self.program.fail_remove = True
        await self.tick(2)
        self.assertIn(info_hash, self.settings.current.refused_hashes)
        self.assertEqual((self.caught, self.guard.watching), ([], {info_hash}))
        self.program.fail_remove = False
        await self.tick(2)
        self.assertEqual(self.program.removed, [[info_hash]])
        self.assertEqual(len(self.caught), 1)

    async def test_nothing_private_in_the_log(self) -> None:
        with self.assertLogs("app", level=logging.DEBUG) as logs:
            logging.getLogger("app").debug("start")
            info_hash = await self.grab_magnet()
            self.program.has(info_hash)
            await self.tick(2)
            self.program.down = True
            await self.tick(2)
            self.program.down = False
            self.program.files[info_hash] = (PROGRAM_FILE,)
            await self.tick(2)
        text = "\n".join(logs.output)
        self.assertEqual(len(self.caught), 1)
        private = (info_hash, info_hash.upper(), "magnet:", PROGRAM_FILE, "MeGusta", TASK_NAME)
        for secret in (*private, "192.168.1.20"):
            self.assertNotIn(secret, text)
        removed = f"Removed {release().title!r} from qBittorrent: its torrent holds a .exe file"
        self.assertIn(removed, text)


class DownloadStationTests(GuardTestCase):
    kind = RemoteKind.SYNOLOGY

    async def test_a_finished_file_may_stay_and_the_notice_says_so(self) -> None:
        info_hash = await self.grab_magnet()
        self.program.has(info_hash, (PROGRAM_FILE,), state=DownloadState.READY, progress=1.0)
        await self.tick(2)
        self.assertEqual(self.program.removed, [["dbid_7"]])  # by Download Station's own id
        [caught] = self.caught
        self.assertTrue(caught.file_may_remain)
        self.assertIn(
            f"The file {PROGRAM_FILE} may still be in your downloads folder: don't open it, "
            "delete it.",
            caught_notice(caught).body,
        )

    async def test_an_unstarted_download_goes_with_its_task(self) -> None:
        info_hash = await self.grab_magnet()
        self.program.has(info_hash, (PROGRAM_FILE,), state=DownloadState.WAITING, progress=0.0)
        await self.tick(2)
        [caught] = self.caught
        self.assertFalse(caught.file_may_remain)

    async def test_a_program_that_cant_tell_files_is_no_longer_watched(self) -> None:
        info_hash = await self.grab_magnet()
        self.program.has(info_hash)
        self.program.files_unknown = True
        await self.tick(2)
        self.assertEqual((self.guard.watching, self.program.removed), (frozenset(), []))

    async def test_the_folder_modes_magnet_fallback_is_watched(self) -> None:
        await self.settings.update(download_mode=DownloadMode.FOLDER)
        info_hash = await self.grab_magnet()
        self.assertEqual(self.guard.watching, {info_hash})
        self.program.has(info_hash, (PROGRAM_FILE,))
        await self.tick(2)
        self.assertEqual(len(self.caught), 1)


class WatchingTests(GuardTestCase):
    async def test_a_clean_download_stops_being_watched(self) -> None:
        info_hash = await self.grab_magnet()
        self.program.has(info_hash, ("Gold.Rush.S17E08.mkv", "Sample.mkv", "Info.url", "x.nfo"))
        await self.tick(2)
        self.assertEqual(self.guard.watching, frozenset())
        self.assertEqual((self.program.removed, self.caught), ([], []))
        self.assertEqual(len(await self.db.history.every_grab()), 1)
        await self.tick(60)
        self.assertEqual(self.program.status_calls, 1)  # nothing asked any more

    async def test_no_metadata_keeps_watching_on_the_schedule(self) -> None:
        info_hash = await self.grab_magnet()
        self.program.has(info_hash)
        await self.tick(1)
        self.assertEqual(self.program.status_calls, 0)  # not due yet
        await self.tick(1)
        self.assertEqual(self.program.status_calls, 1)
        for _ in range(10):
            await self.tick(2)
        self.assertEqual(self.program.status_calls, 11)  # every 2 s at first
        self.clock.now = START + timedelta(minutes=3)
        await self.guard.check_due()
        calls = self.program.status_calls
        await self.tick(5)
        self.assertEqual(self.program.status_calls, calls)
        await self.tick(5)
        self.assertEqual(self.program.status_calls, calls + 1)  # then every 10 s
        self.clock.now = START + timedelta(hours=2)
        await self.guard.check_due()
        calls = self.program.status_calls
        await self.tick(30)
        self.assertEqual(self.program.status_calls, calls)
        await self.tick(30)
        self.assertEqual(self.program.status_calls, calls + 1)  # then every minute
        self.assertEqual(self.guard.watching, {info_hash})
        self.clock.now = START + WATCH_FOR
        await self.guard.check_due()
        self.assertEqual(self.guard.watching, frozenset())  # then no more
        self.assertEqual(self.caught, [])

    def test_the_schedule(self) -> None:
        self.assertEqual(check_interval(timedelta(0)), 2.0)
        self.assertEqual(check_interval(timedelta(minutes=1, seconds=59)), 2.0)
        self.assertEqual(check_interval(timedelta(minutes=2)), 10.0)
        self.assertEqual(check_interval(timedelta(minutes=29)), 10.0)
        self.assertEqual(check_interval(timedelta(minutes=30)), 60.0)
        self.assertEqual(check_interval(timedelta(hours=23, minutes=59)), 60.0)
        self.assertIsNone(check_interval(timedelta(hours=24)))

    async def test_a_task_removed_in_the_program_stops_the_watch(self) -> None:
        info_hash = await self.grab_magnet()
        self.program.has(info_hash)
        await self.tick(2)
        del self.program.tasks[info_hash]  # the user removed it
        await self.tick(2)
        self.assertEqual(self.guard.watching, frozenset())
        self.assertEqual(self.caught, [])

    async def test_a_task_not_listed_yet_is_waited_for_a_while(self) -> None:
        info_hash = await self.grab_magnet()
        await self.tick(2)
        await self.tick(10)
        self.assertEqual(self.guard.watching, {info_hash})
        self.clock.now = START + NOT_LISTED_GRACE
        await self.guard.check_due()
        self.assertEqual(self.guard.watching, frozenset())

    async def test_a_program_that_cant_be_reached_is_tried_again(self) -> None:
        info_hash = await self.grab_magnet()
        self.program.has(info_hash, (PROGRAM_FILE,))
        self.program.down = True
        for _ in range(3):
            await self.tick(2)
        self.assertEqual(self.program.status_calls, 3)
        self.assertEqual(self.guard.watching, {info_hash})
        self.clock.now = START + NOT_LISTED_GRACE + timedelta(minutes=1)  # long down: no matter
        await self.guard.check_due()
        self.assertEqual(self.guard.watching, {info_hash})
        self.program.down = False
        await self.tick(10)
        self.assertEqual(self.program.removed, [[info_hash]])

    async def test_several_at_once_in_one_task_list(self) -> None:
        other = "c" * 40
        info_hash = await self.grab_magnet()
        self.guard.watch(other)
        self.program.has(info_hash, ("fine.mkv",))
        self.program.has(other, (PROGRAM_FILE,))
        await self.tick(2)
        self.assertEqual(self.program.status_calls, 1)
        self.assertEqual(self.program.removed, [[other]])
        self.assertEqual(self.guard.watching, frozenset())

    async def test_a_torrent_file_send_is_not_watched(self) -> None:
        self.web.routes[DIRECT_URL] = lambda: httpx.Response(200, content=TORRENT)
        await self.downloads.grab(release(download_url=DIRECT_URL), EPISODE)
        self.assertEqual(self.program.torrents, ["Some.Show.S01E02.1080p.WEB.h264-GRP.torrent"])
        self.assertEqual(self.guard.watching, frozenset())

    async def test_app_mode_is_not_watched(self) -> None:
        await self.settings.update(download_mode=DownloadMode.APP)
        opened: list[str] = []
        downloads = DownloadService(
            self.http,
            self.db.history,
            self.settings,
            self.remote,
            Path(tempfile.gettempdir()) / "never",
            find_app=lambda: TorrentApp(name="qBittorrent"),
            opener=opened.append,  # never the real os.startfile
        )
        downloads.subscribe_magnets(self.guard.watch)
        await downloads.grab(release(), EPISODE)
        self.assertEqual(len(opened), 1)  # the magnet, opened in Windows' torrent program
        self.assertEqual(self.guard.watching, frozenset())

    async def test_run_checks_on_its_own(self) -> None:
        waits: list[float | None] = []

        async def sleep(seconds: float | None) -> None:
            waits.append(seconds)
            if len(waits) >= 3:
                raise asyncio.CancelledError
            self.clock.advance(seconds or 0.0)

        guard = ProgramGuard(
            self.remote,
            self.settings,
            self.db.history,
            self.downloads,
            show=self.show,
            now=self.clock,
            sleep=sleep,
        )
        guard.watch(HASH)
        self.program.has(HASH)
        with self.assertRaises(asyncio.CancelledError):
            await guard.run()
        self.assertEqual(waits, [2.0, 2.0, 2.0])
        self.assertEqual(self.program.status_calls, 2)


class RestartTests(GuardTestCase):
    async def add(self, ago: timedelta, **changes: Any) -> None:
        values: dict[str, Any] = {
            "media_type": MediaType.TV,
            "media_id": GOLD_RUSH.id,
            "season": 17,
            "episode": 8,
            "release_title": "Gold.Rush.S17E08",
            "info_hash": "a" * 40,
            "source": "prowlarr",
            "saved_path": "qBittorrent",
            "downloaded_at": START - ago,
        }
        values.update(changes)
        await self.db.history.add(DownloadRecord(**values))

    async def test_the_last_days_magnets_are_watched_again(self) -> None:
        await self.add(timedelta(hours=1))
        await self.add(timedelta(minutes=1), info_hash="b" * 40, episode=9)
        await self.add(timedelta(hours=25), info_hash="c" * 40, episode=7)  # too old
        saved = r"C:\watch\Gold.Rush.S17E06.torrent"  # a .torrent saved to the watch folder
        await self.add(timedelta(hours=2), info_hash="d" * 40, episode=6, saved_path=saved)
        await self.add(timedelta(hours=2), info_hash=None, episode=5)  # no hash to look for
        await self.add(timedelta(hours=2), info_hash="e" * 40, episode=4, saved_path="Transmission")
        await self.add(timedelta(hours=3), info_hash="f" * 40, episode=3)
        await self.settings.update(refused_hashes=("f" * 40,))
        self.assertEqual(await self.guard.pick_up(), 2)
        self.assertEqual(self.guard.watching, {"a" * 40, "b" * 40})
        # Checked at once, each on its own schedule from when it was sent.
        self.program.has("a" * 40)
        self.program.has("b" * 40, (PROGRAM_FILE,))
        await self.guard.check_due()
        self.assertEqual(self.program.removed, [["b" * 40]])
        self.assertEqual(self.guard.watching, {"a" * 40})
        [caught] = self.caught
        self.assertEqual(caught.title, "Gold Rush S17E09")
        await self.tick(10)
        self.assertEqual(self.program.status_calls, 1)  # an hour old: every minute
        await self.tick(50)
        self.assertEqual(self.program.status_calls, 2)

    async def test_nothing_in_app_mode_or_without_a_program(self) -> None:
        await self.add(timedelta(hours=1))
        await self.settings.update(download_mode=DownloadMode.APP)
        self.assertEqual(await self.guard.pick_up(), 0)
        await self.settings.update(download_mode=DownloadMode.REMOTE, remote_url="")
        self.assertEqual(await self.guard.pick_up(), 0)
        self.assertEqual(self.guard.watching, frozenset())


class ProtocolTests(unittest.TestCase):
    def test_the_fake_and_the_real_clients_have_the_file_calls(self) -> None:
        from unittest import mock

        from app.database import SettingsRepository
        from app.services.qbittorrent import QBittorrentClient
        from app.services.qbittorrent_local import LocalQBittorrent
        from app.services.synology import DownloadStationClient
        from app.services.transmission import TransmissionClient

        settings = SettingsService(mock.Mock(spec=SettingsRepository), Settings())
        for client in (
            FakeProgram(RemoteKind.QBITTORRENT),
            DownloadStationClient(settings),
            QBittorrentClient(settings),
            TransmissionClient(settings),
            LocalQBittorrent(QBittorrentClient(settings), mock.Mock(), 8085),
        ):
            self.assertIsInstance(client, RemoteTaskFiles)
            self.assertIsInstance(client, RemoteStatusClient)


if __name__ == "__main__":
    unittest.main()
