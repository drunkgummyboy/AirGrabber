"""DownloadStatusService and its pure rules: where a grab went, matching grabs to the torrent
program's tasks, states, titles, the free-space rule and the check timing. The torrent program
is a fake RemoteStatusClient; the history and subtitles live in a real database."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from app.database import Database, SettingsRepository
from app.models import (
    DownloadMode,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    FreeSpace,
    GrabOutcome,
    MediaType,
    RemoteKind,
    RemoteStatus,
    RemoteTask,
    Settings,
    Show,
)
from app.services import download_status as ds
from app.services.download_status import (
    DiskUsage,
    DownloadStatusService,
    entry_state,
    episode_title,
    expected_in_program,
    grab_outcome,
    is_network_path,
    match_tasks,
    movie_title,
    progress_program,
    space_folder,
    space_is_low,
    torrent_file_names,
)
from app.services.remote import DiskSpace, RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.settings import SettingsService

GB = 1024**3
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
FALLOUT = Show(id=101, name="Fallout", poster_url="https://static.tvmaze.com/fallout.jpg")
HASH_A = "a" * 40
HASH_B = "b" * 40


def record(
    title: str,
    *,
    media_type: MediaType = MediaType.TV,
    media_id: int = FALLOUT.id,
    season: int | None = 2,
    episode: int | None = 3,
    info_hash: str | None = None,
    saved_path: str | None = "Download Station",
    when: datetime = NOW - timedelta(hours=1),
    record_id: int | None = None,
) -> DownloadRecord:
    return DownloadRecord(
        id=record_id,
        media_type=media_type,
        media_id=media_id,
        season=season if media_type is MediaType.TV else None,
        episode=episode if media_type is MediaType.TV else None,
        release_title=title,
        info_hash=info_hash,
        saved_path=saved_path,
        downloaded_at=when,
    )


def task(
    name: str,
    state: DownloadState = DownloadState.DOWNLOADING,
    progress: float = 0.42,
    *,
    info_hash: str | None = None,
    file_name: str | None = None,
) -> RemoteTask:
    return RemoteTask(
        name=name, info_hash=info_hash, file_name=file_name, progress=progress, state=state
    )


def settings_of(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "download_mode": DownloadMode.REMOTE,
        "remote_kind": RemoteKind.SYNOLOGY,
        "remote_url": "http://192.168.1.20:5000",
        "remote_username": "alex",
        "remote_password": "secret",
    }
    base.update(values)
    return Settings(**base)


class OutcomeTests(unittest.TestCase):
    def test_where_a_grab_went(self) -> None:
        remote, app = DownloadMode.REMOTE, DownloadMode.APP
        self.assertEqual(
            grab_outcome(r"\\nas\video\watch\Fallout S02E03.torrent", DownloadMode.FOLDER),
            (GrabOutcome.SAVED, ""),
        )
        self.assertEqual(grab_outcome("Download Station", remote), (GrabOutcome.SENT, "Download Station"))
        self.assertEqual(grab_outcome("Download Station", app), (GrabOutcome.SENT, "Download Station"))
        self.assertEqual(grab_outcome("qBittorrent", remote), (GrabOutcome.SENT, "qBittorrent"))
        # The torrent program on this PC can have the same name.
        self.assertEqual(grab_outcome("qBittorrent", app), (GrabOutcome.OPENED, "qBittorrent"))
        self.assertEqual(grab_outcome("uTorrent", remote), (GrabOutcome.OPENED, "uTorrent"))
        self.assertEqual(grab_outcome(None, remote), (None, ""))

    def test_which_grabs_the_program_should_have(self) -> None:
        remote = settings_of()
        folder = settings_of(download_mode=DownloadMode.FOLDER)
        self.assertEqual(progress_program(remote), "Download Station")
        self.assertTrue(expected_in_program(GrabOutcome.SENT, "Download Station", remote))
        self.assertFalse(expected_in_program(GrabOutcome.SENT, "qBittorrent", remote))
        self.assertFalse(expected_in_program(GrabOutcome.OPENED, "qBittorrent", remote))
        self.assertTrue(expected_in_program(GrabOutcome.SAVED, "", folder))  # the watch folder
        self.assertFalse(expected_in_program(GrabOutcome.SAVED, "", remote))
        for nothing_to_ask in (
            settings_of(download_mode=DownloadMode.APP),
            settings_of(remote_password=""),
            settings_of(download_mode=DownloadMode.FOLDER, remote_url=""),
        ):
            self.assertEqual(progress_program(nothing_to_ask), "")
            self.assertFalse(
                expected_in_program(GrabOutcome.SENT, "Download Station", nothing_to_ask)
            )


class MatchTests(unittest.TestCase):
    def test_by_hash_then_file_name_then_name(self) -> None:
        records = [
            record("Fallout.S02E03.1080p.WEB.h264", info_hash=HASH_A),
            record("The Bear S04E01 720p", info_hash=HASH_B),  # sent as a .torrent file
            record("Severance.S02E01.1080p", saved_path=r"\\nas\watch\Severance S02E01 (2).torrent"),
            record("Andor.S02E01.1080p"),
            record("Not.There.S01E01"),
        ]
        tasks = [
            task("Andor S02E01 1080p"),  # by name (normalised)
            task("Severance.S02E01.1080p", file_name="Severance S02E01 (2).torrent"),
            task("The.Bear.S04E01.720p-GRP", file_name="The Bear S04E01 720p.torrent"),
            task("Fallout renamed", info_hash=HASH_A),
        ]
        found = match_tasks(records, tasks)
        self.assertEqual(
            {index: matched.name for index, matched in found.items()},
            {
                0: "Fallout renamed",
                1: "The.Bear.S04E01.720p-GRP",
                2: "Severance.S02E01.1080p",
                3: "Andor S02E01 1080p",
            },
        )

    def test_each_task_once_newest_grab_first(self) -> None:
        records = [record("Fallout.S02E03.1080p"), record("Fallout.S02E03.1080p")]
        found = match_tasks(records, [task("Fallout.S02E03.1080p")])
        self.assertEqual(list(found), [0])

    def test_torrent_file_names(self) -> None:
        saved = record("A: B/C", saved_path=r"\\nas\watch\A B C (3).torrent")
        self.assertEqual(torrent_file_names(saved), {"a bc.torrent", "a b c (3).torrent"})

    def test_entry_state(self) -> None:
        found = task("x", DownloadState.READY, 1.0)
        self.assertEqual(
            entry_state(found, expected=True, asked=True), (DownloadState.READY, 1.0)
        )
        self.assertEqual(
            entry_state(None, expected=True, asked=True), (DownloadState.NOT_FOUND, None)
        )
        self.assertEqual(
            entry_state(None, expected=True, asked=False), (DownloadState.UNKNOWN, None)
        )
        self.assertEqual(
            entry_state(None, expected=False, asked=True), (DownloadState.UNKNOWN, None)
        )


class TitleTests(unittest.TestCase):
    def test_episode_and_season_pack(self) -> None:
        self.assertEqual(episode_title("Fallout", 2, 3), "Fallout S02E03")
        self.assertEqual(episode_title("Fallout", 2, None), "Fallout Season 2")

    def test_film_from_its_release_title(self) -> None:
        cases = {
            "Gladiator.II.2024.1080p.WEB-DL.DDP5.1": "Gladiator II (2024)",
            "Blade.Runner.2049.2017.2160p.UHD": "Blade Runner 2049 (2017)",
            "1917.2019.1080p.BluRay": "1917 (2019)",
            "Gladiator II (2024) [1080p] [YTS.MX]": "Gladiator II (2024)",
            "Some_Film_Without_Year_720p": "Some Film Without Year 720p",
        }
        for release, title in cases.items():
            with self.subTest(release=release):
                self.assertEqual(movie_title(release), title)


class SpaceRuleTests(unittest.TestCase):
    def test_low_below_20_gb_or_5_percent_under_100_gb(self) -> None:
        self.assertTrue(space_is_low(19 * GB, 1000 * GB))  # below 20 GB
        self.assertTrue(space_is_low(19 * GB, None))
        self.assertTrue(space_is_low(0, None))
        self.assertFalse(space_is_low(30 * GB, 100 * GB))  # 30 %
        self.assertTrue(space_is_low(90 * GB, 4000 * GB))  # 2.25 % and below 100 GB
        self.assertTrue(space_is_low(25 * GB, 1000 * GB))  # 2.5 %
        # A big disk with plenty of room never warns, however small the share.
        self.assertFalse(space_is_low(100 * GB, 4000 * GB))
        self.assertFalse(space_is_low(800 * GB, 16 * 1024 * GB))  # 16 TB NAS, 4.9 %
        self.assertFalse(space_is_low(250 * GB, 4000 * GB))
        self.assertFalse(space_is_low(25 * GB, None))  # no total: only the 20 GB rule

    def test_which_folder_counts(self) -> None:
        self.assertEqual(
            space_folder(settings_of(subtitles_folder=r"\\nas\video", download_dir="D:/watch")),
            r"\\nas\video",
        )
        folder = settings_of(download_mode=DownloadMode.FOLDER, download_dir="D:/watch")
        self.assertEqual(space_folder(folder), str(Path("D:/watch")))
        self.assertIsNone(space_folder(settings_of()))

    def test_network_paths(self) -> None:
        self.assertTrue(is_network_path(r"\\nas\video"))
        self.assertTrue(is_network_path("//nas/video"))
        self.assertFalse(is_network_path("relative/folder"))


# -- the service -------------------------------------------------------------------------------


class FakeStatusClient:
    def __init__(self, kind: RemoteKind) -> None:
        self.kind = kind
        self.tasks: tuple[RemoteTask, ...] = ()
        self.error: RemoteError | None = None
        self.asked: list[frozenset[str]] = []
        self.space: DiskSpace | None = None

    async def test(self) -> str:
        return self.kind.program

    async def add_magnet(self, magnet: str) -> None:
        pass

    async def add_torrent(self, content: bytes, filename: str) -> None:
        pass

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        self.asked.append(frozenset(hashes))
        if self.error is not None:
            raise self.error
        return RemoteStatus(program=self.kind.program, tasks=self.tasks)

    async def free_space(self) -> DiskSpace | None:
        return self.space


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "airgrabber.db")
        self.addAsyncCleanup(self.db.close)
        await self.db.library.save_show(FALLOUT)
        self.settings = SettingsService(mock.Mock(spec=SettingsRepository), settings_of())
        self.clients = {kind: FakeStatusClient(kind) for kind in RemoteKind}
        self.remote = RemoteDownloads(self.settings, clients=self.clients)
        self.usage: dict[str, DiskUsage | Exception] = {}
        self.usage_calls: list[str] = []
        self.clock = 1000.0
        self.service = DownloadStatusService(
            self.db.history,
            self.db.subtitles,
            self.settings,
            self.remote,
            show=self.db.library.get_show,
            disk_usage=self.disk_usage,
            network_path=lambda path: path.startswith("\\\\"),
            clock=lambda: self.clock,
            now=lambda: NOW,
        )
        self.overviews: list[DownloadsOverview] = []
        self.notices: list[FreeSpace] = []
        self.service.subscribe(self.overviews.append)
        self.service.subscribe_space(self.notices.append)

    def disk_usage(self, path: str) -> DiskUsage:
        self.usage_calls.append(path)
        found = self.usage.get(path, OSError(53, "The network path was not found"))
        if isinstance(found, Exception):
            raise found
        return found

    @property
    def station(self) -> FakeStatusClient:
        return self.clients[RemoteKind.SYNOLOGY]

    async def add(self, grab: DownloadRecord) -> DownloadRecord:
        await self.db.history.add(grab)
        recent = await self.db.history.recent(200)
        return next(item for item in recent if item.release_title == grab.release_title)


class OverviewTests(ServiceTestCase):
    async def test_states_titles_subtitles_and_listeners(self) -> None:
        fallout = await self.add(record("Fallout.S02E03.1080p.WEB", info_hash=HASH_A))
        await self.add(
            record(
                "Gladiator.II.2024.1080p",
                media_type=MediaType.MOVIE,
                media_id=558449,
                info_hash=HASH_B,
                when=NOW - timedelta(days=2),
            )
        )
        await self.add(record("Gone.S01E01", episode=4, when=NOW - timedelta(days=3)))
        await self.add(
            record("Opened.S01E05", episode=5, saved_path="uTorrent", when=NOW - timedelta(days=4))
        )
        await self.add(record("Too.Old.S01E01", episode=6, when=NOW - timedelta(days=31)))
        for language in ("nl", "en"):
            await self.db.subtitles.record_saved(
                f"/video/{fallout.release_title}.mkv",
                language,
                record=fallout,
                season=2,
                episode=3,
                subtitle_path=f"/video/x.{language}.srt",
                when=NOW,
            )
        self.station.tasks = (
            task("whatever", info_hash=HASH_A, progress=0.4216),
            task("Gladiator", DownloadState.READY, 1.0, info_hash=HASH_B),
        )

        overview = await self.service.check()

        self.assertEqual(self.overviews, [overview])
        self.assertIs(self.service.latest, overview)
        self.assertEqual(overview.program, "Download Station")
        self.assertIsNone(overview.problem)
        rows = [
            (entry.title, entry.outcome, entry.program, entry.state, entry.progress, entry.subtitles)
            for entry in overview.entries
        ]
        self.assertEqual(
            rows,
            [
                ("Fallout S02E03", GrabOutcome.SENT, "Download Station", DownloadState.DOWNLOADING,
                 0.4216, ("en", "nl")),
                ("Gladiator II (2024)", GrabOutcome.SENT, "Download Station", DownloadState.READY,
                 1.0, ()),
                ("Fallout S02E04", GrabOutcome.SENT, "Download Station", DownloadState.NOT_FOUND,
                 None, ()),
                ("Fallout S02E05", GrabOutcome.OPENED, "uTorrent", DownloadState.UNKNOWN, None,
                 ()),
            ],
        )
        self.assertEqual(overview.entries[0].show, FALLOUT)
        self.assertIsNone(overview.entries[1].show)
        self.assertEqual(overview.in_progress, 1)
        self.assertEqual(self.station.asked, [frozenset()])  # one grab has no hash: ask for all

    async def test_asks_only_for_the_hashes_when_every_grab_has_one(self) -> None:
        await self.add(record("A.S01E01", episode=1, info_hash=HASH_A))
        await self.add(record("B.S01E02", episode=2, info_hash=HASH_B))
        await self.add(record("C.S01E03", episode=3, saved_path="uTorrent"))  # not asked about
        await self.service.check()
        self.assertEqual(self.station.asked, [frozenset({HASH_A, HASH_B})])

    async def test_the_torrent_program_on_this_pc_gives_no_progress(self) -> None:
        await self.settings.update(download_mode=DownloadMode.APP)
        await self.add(record("A.S01E01", saved_path="qBittorrent"))
        overview = await self.service.check()
        self.assertEqual(overview.program, "")
        self.assertFalse(overview.progress_available)
        self.assertEqual(overview.entries[0].state, DownloadState.UNKNOWN)
        self.assertEqual(overview.entries[0].outcome, GrabOutcome.OPENED)
        self.assertEqual(self.station.asked, [])

    async def test_an_unreachable_program_is_one_plain_line(self) -> None:
        await self.add(record("A.S01E01"))
        self.station.error = RemoteError("Can't reach the NAS at 192.168.1.20:5000.")
        overview = await self.service.check()
        self.assertEqual(overview.problem, "Can't reach the NAS at 192.168.1.20:5000.")
        self.assertEqual(overview.entries[0].state, DownloadState.UNKNOWN)

    async def test_nothing_to_ask_about_asks_nothing(self) -> None:
        overview = await self.service.check()
        self.assertEqual(overview.entries, ())
        self.assertEqual(self.station.asked, [])
        self.assertEqual(overview.program, "Download Station")

    async def test_folder_mode_matches_the_watch_folders_files(self) -> None:
        await self.settings.update(download_mode=DownloadMode.FOLDER, download_dir="D:/watch")
        await self.add(record("Fallout.S02E03", saved_path=r"D:\watch\Fallout.S02E03.torrent"))
        self.station.tasks = (task("Fallout", file_name="Fallout.S02E03.torrent", progress=0.5),)
        overview = await self.service.check()
        entry = overview.entries[0]
        self.assertEqual((entry.outcome, entry.state, entry.progress),
                         (GrabOutcome.SAVED, DownloadState.DOWNLOADING, 0.5))

    async def test_concurrent_checks_share_one(self) -> None:
        await self.add(record("A.S01E01"))
        first, second = await asyncio.gather(self.service.check(), self.service.check())
        self.assertIs(first, second)
        self.assertEqual(len(self.station.asked), 1)


class FreeSpaceTests(ServiceTestCase):
    async def test_a_nearly_full_share_warns_once_a_day(self) -> None:
        await self.settings.update(subtitles_folder=r"\\nas\video")
        self.usage[r"\\nas\video"] = DiskUsage(4000 * GB, 3988 * GB, 12 * GB)
        overview = await self.service.check()
        self.assertEqual(
            overview.space,
            FreeSpace(free_bytes=12 * GB, total_bytes=4000 * GB, on_nas=True, low=True),
        )
        self.assertEqual(len(self.notices), 1)
        # Not measured again before it is due; no second notice the same day.
        await self.service.check()
        self.assertEqual(len(self.usage_calls), 1)
        self.clock += ds.SPACE_INTERVAL_S
        await self.service.check()
        self.assertEqual(len(self.usage_calls), 2)
        self.assertEqual(len(self.notices), 1)
        self.clock += ds.NOTICE_INTERVAL_S
        await self.service.check()
        self.assertEqual(len(self.notices), 2)

    async def test_while_the_page_shows_it_is_measured_more_often(self) -> None:
        await self.settings.update(subtitles_folder=r"\\nas\video")
        self.usage[r"\\nas\video"] = DiskUsage(4000 * GB, 1000 * GB, 3000 * GB)
        self.service.set_visible(True)
        overview = await self.service.check()
        assert overview.space is not None
        self.assertFalse(overview.space.low)
        self.clock += ds.SPACE_VISIBLE_INTERVAL_S
        await self.service.check()
        self.assertEqual(len(self.usage_calls), 2)
        self.assertEqual(self.notices, [])

    async def test_an_unreachable_share_falls_back_to_the_programs_report(self) -> None:
        await self.settings.update(
            subtitles_folder=r"\\nas\gone", remote_kind=RemoteKind.QBITTORRENT
        )
        self.clients[RemoteKind.QBITTORRENT].space = DiskSpace(5 * GB)
        overview = await self.service.check()
        self.assertEqual(
            overview.space, FreeSpace(free_bytes=5 * GB, total_bytes=None, on_nas=True, low=True)
        )

    async def test_no_folder_and_no_report_is_no_banner(self) -> None:
        overview = await self.service.check()  # REMOTE mode, Download Station reports none
        self.assertIsNone(overview.space)
        self.assertEqual(self.usage_calls, [])

    async def test_a_hanging_share_never_blocks_and_isnt_asked_twice(self) -> None:
        release = threading.Event()
        calls: list[str] = []

        def hang(path: str) -> DiskUsage:
            calls.append(path)
            release.wait(5)
            return DiskUsage(100 * GB, 0, 100 * GB)

        self.service._disk_usage = hang
        await self.settings.update(subtitles_folder=r"\\nas\slow")
        with mock.patch.object(ds, "SPACE_TIMEOUT_S", 0.05):
            overview = await self.service.check()
            self.assertIsNone(overview.space)
            self.clock += ds.SPACE_INTERVAL_S
            await self.service.check()  # the first one still hangs: no second thread
        self.assertEqual(calls, [r"\\nas\slow"])
        release.set()


class TimingTests(ServiceTestCase):
    async def test_intervals(self) -> None:
        self.assertEqual(self.service.interval(), ds.IDLE_INTERVAL_S)
        await self.add(record("A.S01E01"))
        self.station.tasks = (task("A.S01E01"),)
        await self.service.check()
        self.assertEqual(self.service.interval(), ds.BUSY_INTERVAL_S)
        self.service.set_visible(True)
        self.assertEqual(self.service.interval(), ds.VISIBLE_INTERVAL_S)

    async def test_showing_the_page_and_a_grab_wake_the_loop(self) -> None:
        loop_task = asyncio.create_task(self.service.run_periodic(start_delay_s=3600))
        self.addCleanup(loop_task.cancel)
        await asyncio.sleep(0)
        self.service.set_visible(True)
        await self.wait_for_overviews(1)
        self.service.set_visible(False)
        with mock.patch.object(ds, "AFTER_GRAB_S", 0.01):
            self.service.after_grab()
        await self.wait_for_overviews(2)
        loop_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await loop_task

    async def wait_for_overviews(self, count: int) -> None:
        async with asyncio.timeout(2):
            while len(self.overviews) < count:
                await asyncio.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
