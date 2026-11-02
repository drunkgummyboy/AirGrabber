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
from app.forge.models.media import MediaKind
from app.forge.models.moves import MoveRecord
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
from app.models.downloads import MovedTo
from app.services import download_status as ds
from app.services.download_status import (
    DiskUsage,
    DownloadStatusService,
    ForgeMoves,
    Located,
    entry_state,
    episode_title,
    expected_in_program,
    grab_outcome,
    is_network_path,
    match_moves,
    match_tasks,
    move_folders,
    moved_state,
    movie_title,
    progress_program,
    shown_folder,
    space_folders,
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
        finished = settings_of(
            subtitles_folder=r"\\nas\video", download_dir="D:/watch", movie_download_dir="E:/films"
        )
        self.assertEqual(space_folders(finished), (r"\\nas\video",))
        folder = settings_of(download_mode=DownloadMode.FOLDER, download_dir="D:/watch")
        self.assertEqual(space_folders(folder), (str(Path("D:/watch")),))
        self.assertEqual(space_folders(settings_of()), ())

    def test_a_films_folder_of_its_own_counts_too(self) -> None:
        films = settings_of(
            download_mode=DownloadMode.FOLDER, download_dir="D:/watch", movie_download_dir="E:/films"
        )
        self.assertEqual(space_folders(films), (str(Path("D:/watch")), str(Path("E:/films"))))
        same = films.model_copy(update={"movie_download_dir": Path("D:/watch")})
        self.assertEqual(space_folders(same), (str(Path("D:/watch")),))
        # An empty films folder is none: the watch folder alone.
        empty = settings_of(
            download_mode=DownloadMode.FOLDER, download_dir="D:/watch", movie_download_dir=" "
        )
        self.assertEqual(space_folders(empty), (str(Path("D:/watch")),))

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

    async def add_magnet(self, magnet: str, *, movie: bool = False) -> None:
        pass

    async def add_torrent(self, content: bytes, filename: str, *, movie: bool = False) -> None:
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

    async def test_folder_mode_counts_the_fuller_of_the_watch_and_films_folders(self) -> None:
        await self.settings.update(
            download_mode=DownloadMode.FOLDER,
            download_dir=r"\\nas\watch",
            movie_download_dir="E:/films",
        )
        watch, films = str(Path(r"\\nas\watch")), str(Path("E:/films"))
        self.usage[watch] = DiskUsage(4000 * GB, 1000 * GB, 3000 * GB)
        self.usage[films] = DiskUsage(500 * GB, 490 * GB, 10 * GB)
        overview = await self.service.check()
        self.assertEqual(self.usage_calls, [watch, films])
        self.assertEqual(
            overview.space,
            FreeSpace(free_bytes=10 * GB, total_bytes=500 * GB, on_nas=False, low=True),
        )

    async def test_an_unreachable_films_folder_leaves_the_watch_folder(self) -> None:
        await self.settings.update(
            download_mode=DownloadMode.FOLDER,
            download_dir=r"\\nas\watch",
            movie_download_dir=r"\\nas\gone",
        )
        self.usage[str(Path(r"\\nas\watch"))] = DiskUsage(4000 * GB, 1000 * GB, 3000 * GB)
        overview = await self.service.check()
        self.assertEqual(
            overview.space,
            FreeSpace(free_bytes=3000 * GB, total_bytes=4000 * GB, on_nas=True, low=False),
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


# -- where MediaForge moved a grab ----------------------------------------------------------------

FINISHED = r"D:\Downloads\Finished"
SERIES = r"\\nas\video\Series"
MOVIES = r"\\nas\video\Movies"
GOLD_RUSH_TMDB = 34634
GLADIATOR_TMDB = 558449


def local(moment: datetime) -> datetime:
    """Forge's records keep local time without a zone."""
    return moment.astimezone().replace(tzinfo=None)


def move(
    source: str,
    target: str,
    *,
    kind: MediaKind = MediaKind.EPISODE,
    tmdb_id: int | None = GOLD_RUSH_TMDB,
    when: datetime = NOW - timedelta(minutes=30),
) -> MoveRecord:
    return MoveRecord(
        source=Path(FINISHED) / source,
        target=Path(target),
        moved_at=local(when),
        kind=kind,
        tmdb_id=tmdb_id,
        run_id=1,
    )


def film(
    source: str,
    target: str,
    *,
    tmdb_id: int | None = GLADIATOR_TMDB,
    when: datetime = NOW - timedelta(minutes=30),
) -> MoveRecord:
    return move(source, target, kind=MediaKind.MOVIE, tmdb_id=tmdb_id, when=when)


def season_17(episode: int) -> str:
    return rf"{SERIES}\Gold Rush\Season 17\Gold Rush - S17E{episode:02d}.mkv"


class MoveRuleTests(unittest.TestCase):
    def match(
        self, records: list[DownloadRecord], moves: list[MoveRecord], **values: Any
    ) -> dict[int, MoveRecord]:
        options: dict[str, Any] = {
            "show_names": {FALLOUT.id: "Gold Rush"},
            "folders": (Path(FINISHED),),
        }
        options.update(values)
        return match_moves(records, moves, **options)

    def test_folders_each_once_nested_ones_left_out(self) -> None:
        self.assertEqual(
            move_folders(FINISHED, [FINISHED.upper() + "\\", r"D:\Downloads", r"E:\Other", ""]),
            (Path(r"D:\Downloads"), Path(r"E:\Other")),
        )
        self.assertEqual(move_folders("", []), ())
        self.assertEqual(move_folders(" ", [r"E:\Other"]), (Path(r"E:\Other"),))

    def test_shown_from_the_library_folders_name_on(self) -> None:
        libraries = (SERIES, MOVIES)
        folder = Path(SERIES) / "Gold Rush" / "Season 17"
        self.assertEqual(shown_folder(folder, libraries), ("Series", "Gold Rush", "Season 17"))
        self.assertEqual(
            shown_folder(Path(r"\\NAS\Video\movies\Dune (2021)"), libraries),
            ("Movies", "Dune (2021)"),
        )
        # The deepest library folder wins; a share itself shows its own name.
        self.assertEqual(
            shown_folder(Path(r"\\nas\video\Series\Kids\Bluey"), (SERIES, SERIES + r"\Kids")),
            ("Kids", "Bluey"),
        )
        self.assertEqual(
            shown_folder(Path(r"\\nas\video\Gold Rush"), (r"\\nas\video",)),
            ("video", "Gold Rush"),
        )
        # Outside every library folder (they changed since): its parent's and its own name only.
        self.assertEqual(
            shown_folder(Path(r"C:\Users\Alex\Videos\Gold Rush\Season 17"), libraries),
            ("Gold Rush", "Season 17"),
        )

    def test_a_film_by_its_tmdb_id_episodes_by_their_release_names(self) -> None:
        records = [
            record("Gladiator.II.2024.1080p", media_type=MediaType.MOVIE, media_id=GLADIATOR_TMDB),
            record("Gold.Rush.S02E03.720p.WEB-GRP", season=2, episode=3),
            record("Gold.Rush.S02E05.720p", season=2, episode=5),
        ]
        moves = [
            film(r"x\a.mkv", rf"{MOVIES}\Gladiator II (2024)\Gladiator II (2024).mkv"),
            move(r"Gold.Rush.S02E03.720p.WEB-GRP\gr.mkv", season_17(3)),  # the release's folder
            move(r"Gold.Rush.S02E05.720p.mkv", season_17(5)),  # a single file: its own name
        ]
        self.assertEqual(self.match(records, moves), {0: moves[0], 1: moves[1], 2: moves[2]})

    def test_an_episodes_move_without_its_name_isnt_taken(self) -> None:
        # A move whose source tells nothing of the grab isn't taken, whatever its ids say
        # (AirGrabber knows a show by its TVMaze id, Forge by its TMDB id).
        grab = record("Gold.Rush.S02E03.720p")
        self.assertEqual(self.match([grab], [move(r"x\a.mkv", season_17(3))]), {})

    def test_a_move_from_before_the_grab_or_of_another_kind_doesnt_count(self) -> None:
        grab = record("Gold.Rush.S02E03.720p")  # an hour ago
        source = r"Gold.Rush.S02E03.720p\gr.mkv"
        early = move(source, season_17(3), when=NOW - timedelta(hours=2))
        self.assertEqual(self.match([grab], [early]), {})
        slack = move(source, season_17(3), when=NOW - timedelta(hours=1, minutes=2))
        self.assertEqual(self.match([grab], [slack]), {0: slack})
        other_kind = film(source, rf"{MOVIES}\X\X.mkv", tmdb_id=GOLD_RUSH_TMDB)
        self.assertEqual(self.match([grab], [other_kind]), {})
        other_episode = move(r"Gold Rush - S02E04.mkv", season_17(4))
        self.assertEqual(self.match([grab], [other_episode]), {})
        film_grab = record(
            "Gladiator.II.2024.1080p", media_type=MediaType.MOVIE, media_id=GLADIATOR_TMDB
        )
        before = film(r"x\a.mkv", rf"{MOVIES}\G\G.mkv", when=NOW - timedelta(hours=2))
        self.assertEqual(self.match([film_grab], [before]), {})

    def test_each_move_once_newest_grab_first(self) -> None:
        newer = record("Gold.Rush.S02E03.1080p", when=NOW - timedelta(hours=1))
        older = record("Gold.Rush.S02E03.720p", when=NOW - timedelta(days=3))
        moves = [  # both named by the show: either grab could own them
            move(r"Gold Rush - S02E03.mkv", season_17(3), when=NOW - timedelta(minutes=10)),
            move(r"old\Gold Rush - S02E03.mkv", season_17(3), when=NOW - timedelta(days=2)),
        ]
        self.assertEqual(self.match([newer, older], moves), {0: moves[0], 1: moves[1]})

    def test_a_season_pack_takes_its_seasons_moves(self) -> None:
        pack = record("Gold.Rush.S02.1080p", season=2, episode=None)
        single = record("Gold.Rush.S02E02.1080p", episode=2, when=NOW - timedelta(hours=2))
        moves = [
            move(rf"Gold.Rush.S02.1080p\Gold.Rush.S02E0{n}.1080p.mkv", season_17(n))
            for n in (1, 2, 3)
        ]
        self.assertEqual(self.match([pack, single], moves), {0: moves[0]})  # the pack took all

    def test_by_release_name_when_the_ids_dont_tell(self) -> None:
        records = [
            record("Gold.Rush.S02E03.720p.WEB-GRP"),
            record("Dune.2021.2160p.UHD", media_type=MediaType.MOVIE, media_id=438631),
            record("Other.Show.S01E01.720p", media_id=999, season=1, episode=1),
        ]
        moves = [
            move(r"Gold.Rush.S02E03.720p.WEB-GRP\gr.mkv", season_17(3), tmdb_id=None),
            film(r"Dune.2021.2160p.UHD.mkv", rf"{MOVIES}\Dune (2021)\Dune (2021).mkv", tmdb_id=None),
            move(r"Unrelated.S01E01.mkv", season_17(1), tmdb_id=None),
        ]
        self.assertEqual(self.match(records, moves), {0: moves[0], 1: moves[1]})
        # A file named "Show S02E03" outside a release folder: by the show's name.
        loose = move(r"gold rush s02e03.mkv", season_17(3), tmdb_id=None)
        self.assertEqual(self.match(records[:1], [loose]), {0: loose})
        # A film's release name never takes an episode's move.
        self.assertEqual(self.match(records[1:2], moves[:1]), {})


class FakeMoveRecords:
    """ForgeMoveService's surface the Downloads page reads."""

    def __init__(self) -> None:
        self.moves: list[MoveRecord] = []
        self.asked: list[Path] = []
        self.listeners: list[Any] = []
        self.error: Exception | None = None

    async def moves_from(self, folder: Path) -> list[MoveRecord]:
        self.asked.append(folder)
        if self.error is not None:
            raise self.error
        prefix = str(folder).lower() + "\\"
        return [m for m in self.moves if str(m.source).lower().startswith(prefix)]

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class MovedTests(ServiceTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.library = FakeMoveRecords()
        self.forge: ForgeMoves | None = None
        self.service._forge = lambda: self.forge
        await self.settings.update(subtitles_folder=FINISHED)

    def forge_runs(self, *moves: MoveRecord) -> None:
        self.library.moves = list(moves)
        self.forge = ForgeMoves(self.library, (r"E:\Watched",), (SERIES, MOVIES))

    async def test_without_forge_the_page_is_as_before(self) -> None:
        await self.add(record("Gold.Rush.S02E03.720p"))
        overview = await self.service.check()
        self.assertIsNone(overview.entries[0].moved_to)
        self.assertEqual(self.library.asked, [])

    async def test_a_moved_grab_says_where_and_its_folder_opens_there(self) -> None:
        await self.add(record("Gold.Rush.S02E03.720p", when=NOW - timedelta(hours=2)))
        await self.add(record("Fallout.S02E04.720p", episode=4))
        self.forge_runs(move(r"Gold.Rush.S02E03.720p\gr.mkv", season_17(3)))
        overview = await self.service.check()
        self.assertEqual(
            [entry.record.episode for entry in overview.entries], [4, 3]  # newest first
        )
        self.assertIsNone(overview.entries[0].moved_to)
        self.assertEqual(
            overview.entries[1].moved_to,
            MovedTo(
                folder=rf"{SERIES}\Gold Rush\Season 17", shown=("Series", "Gold Rush", "Season 17")
            ),
        )
        self.assertEqual(self.library.asked, [Path(FINISHED), Path(r"E:\Watched")])
        # "Open the folder" opens the new place (even with no finished-downloads folder set).
        await self.settings.update(subtitles_folder="")
        located = await self.service.locate(overview.entries[1])
        self.assertEqual(located, Located(rf"{SERIES}\Gold Rush\Season 17", ""))

    async def test_moves_are_read_again_only_after_a_change(self) -> None:
        await self.add(record("Gold.Rush.S02E03.720p"))
        self.forge_runs()
        await self.service.check()
        await self.service.check()
        self.assertEqual(len(self.library.asked), 2)  # one read: two folders
        self.assertEqual(len(self.library.listeners), 1)
        self.library.moves = [move(r"Gold.Rush.S02E03.720p.mkv", season_17(3))]
        self.service._wake.clear()
        self.library.listeners[0]()  # hidden: the next check reads them again
        self.assertFalse(self.service._wake.is_set())
        self.service.set_visible(True)
        self.service._wake.clear()
        self.library.listeners[0]()
        self.assertTrue(self.service._wake.is_set())  # the page shows: look again now
        overview = await self.service.check()
        self.assertIsNotNone(overview.entries[0].moved_to)
        self.assertEqual(len(self.library.asked), 4)
        self.clock += ds.MOVES_TTL_S
        await self.service.check()
        self.assertEqual(len(self.library.asked), 6)  # at the latest after MOVES_TTL_S

    async def test_a_forge_failure_leaves_the_page_as_it_was(self) -> None:
        await self.add(record("Gold.Rush.S02E03.720p"))
        self.forge_runs(move(r"a.mkv", season_17(3)))
        self.library.error = ValueError(r"D:\Downloads\Finished: forge.db is closed")
        with self.assertLogs(ds.log, "INFO") as logs:
            overview = await self.service.check()
        self.assertIsNone(overview.entries[0].moved_to)
        self.assertNotIn("forge.db", "\n".join(logs.output))
        self.assertNotIn(FINISHED, "\n".join(logs.output))

    async def test_a_moved_grab_is_finished_whatever_its_program_says(self) -> None:
        # Forge moved the seeding file away: qBittorrent says "missingFiles", Transmission a
        # local error, Download Station "error" (all PROBLEM); a task removed since: not found.
        await self.add(record("Gold.Rush.S02E03.720p", info_hash=HASH_A))
        await self.add(record("Gold.Rush.S02E05.720p", episode=5, when=NOW - timedelta(hours=2)))
        await self.add(record("Fallout.S02E06.720p", episode=6, info_hash=HASH_B))
        await self.add(record("Fallout.S02E07.720p", episode=7, when=NOW - timedelta(hours=3)))
        self.station.tasks = (
            task("Gold.Rush.S02E03.720p", DownloadState.PROBLEM, 1.0, info_hash=HASH_A),
            task("Fallout.S02E06.720p", DownloadState.PROBLEM, 0.5, info_hash=HASH_B),
        )
        self.forge_runs(
            move(r"Gold.Rush.S02E03.720p\gr.mkv", season_17(3)),
            move(r"Gold.Rush.S02E05.720p\gr.mkv", season_17(5)),
        )
        overview = await self.service.check()
        rows = {
            entry.record.episode: (entry.state, entry.progress, entry.moved_to is not None)
            for entry in overview.entries
        }
        self.assertEqual(
            rows,
            {
                3: (DownloadState.READY, 1.0, True),  # a problem in the program: finished
                5: (DownloadState.READY, 1.0, True),  # not found in the program: finished
                6: (DownloadState.PROBLEM, 0.5, False),  # not moved: a real problem
                7: (DownloadState.NOT_FOUND, None, False),
            },
        )

    def test_moved_state(self) -> None:
        moved = MovedTo(folder=rf"{SERIES}\Gold Rush\Season 17", shown=("Series",))
        for state in (DownloadState.PROBLEM, DownloadState.NOT_FOUND):
            with self.subTest(state=state):
                self.assertEqual(moved_state(state, None, moved), (DownloadState.READY, 1.0))
                self.assertEqual(moved_state(state, 0.3, None), (state, 0.3))
        for state in (DownloadState.READY, DownloadState.DOWNLOADING, DownloadState.UNKNOWN):
            with self.subTest(state=state):
                self.assertEqual(moved_state(state, 0.4, moved), (state, 0.4))


class RealForgeMovesTests(ServiceTestCase):
    """Forge's own move records (forge.db, written by its executor) behind the page: ordinary
    downloads (a release folder, a single file named like the release, a film by its TMDB id)
    show where they ended up, with no TVMaze ↔ TMDB link for the show."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.forge.core.executor import Executor
        from app.forge.core.moves import ForgeMoveService
        from app.forge.db.database import Database as ForgeDatabase
        from app.forge.db.history import HistoryRepo
        from app.forge.db.moves import MoveRepository
        from app.forge.models.settings import ForgeSettings

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.finished = self.root / "Finished"
        self.series = self.root / "Series"
        self.movies = self.root / "Movies"
        forge_db = await ForgeDatabase.open(self.root / "forge.db")
        self.addAsyncCleanup(forge_db.close)
        forge_settings = ForgeSettings(
            watch_folders=[str(self.finished)],
            series_folder=str(self.series),
            movies_folder=str(self.movies),
        )
        self.moves = ForgeMoveService(MoveRepository(forge_db), settings=lambda: forge_settings)
        self.executor = Executor(
            HistoryRepo(forge_db, on_moves_change=self.moves.changed), None, None, moves=self.moves
        )
        await self.settings.update(subtitles_folder=str(self.finished))
        forge = ForgeMoves(self.moves, (str(self.finished),), (str(self.series), str(self.movies)))
        self.service._forge = lambda: forge

    def video(self, *parts: str) -> Path:
        path = self.finished.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return path

    async def rename(self, *files: tuple[Path, Path, Any]) -> None:
        from app.forge.models.history import RunSource
        from app.forge.models.plan import Plan, PlannedFile

        plan = Plan(
            files=tuple(
                PlannedFile(item_id=i, source=src, target=dst)
                for i, (src, dst, _) in enumerate(files, start=1)
            )
        )
        result = await self.executor.run(
            plan,
            source=RunSource.AUTOMATIC,
            preset_name=None,
            on_progress=lambda event: None,
            cancel=asyncio.Event(),
            media={i: media for i, (_, _, media) in enumerate(files, start=1)},
        )
        self.assertEqual(result.renamed, len(files))

    async def test_ordinary_downloads_say_where_they_ended_up(self) -> None:
        from app.forge.models.media import MatchState, ParsedMedia, ResolvedMedia

        def resolved(kind: MediaKind, title: str, tmdb_id: int) -> ResolvedMedia:
            parsed = ParsedMedia(source_name="x.mkv", kind=kind, title=title)
            return ResolvedMedia(
                parsed=parsed,
                title=title,
                year=None,
                episode_title=None,
                match_state=MatchState.CONFIDENT,
                tmdb_id=tmdb_id,
            )

        await self.add(record("Fallout.S02E03.720p.WEB-GRP", episode=3))
        await self.add(record("Fallout.S02E04.1080p.WEB", episode=4))
        await self.add(
            record("Gladiator.II.2024.1080p.WEB", media_type=MediaType.MOVIE, media_id=GLADIATOR_TMDB)
        )
        await self.add(record("Fallout.S02E05.720p", episode=5))  # not renamed (yet)
        show = resolved(MediaKind.EPISODE, "Fallout", 106379)
        season = self.series / "Fallout" / "Season 02"
        gladiator = self.movies / "Gladiator II (2024)" / "Gladiator II (2024).mkv"
        await self.rename(
            (
                self.video("Fallout.S02E03.720p.WEB-GRP", "fallout.s02e03.720p.web-grp.mkv"),
                season / "Fallout - S02E03.mkv",
                show,
            ),
            (self.video("Fallout.S02E04.1080p.WEB.mkv"), season / "Fallout - S02E04.mkv", show),
            (
                self.video("glad2-1080p", "g2.mkv"),  # a name that tells nothing: the TMDB id does
                gladiator,
                resolved(MediaKind.MOVIE, "Gladiator II", GLADIATOR_TMDB),
            ),
        )
        overview = await self.service.check()
        shown = {entry.record.release_title: entry.moved_to for entry in overview.entries}
        series_line = ("Series", "Fallout", "Season 02")
        self.assertEqual(
            {title: moved.shown if moved else None for title, moved in shown.items()},
            {
                "Fallout.S02E03.720p.WEB-GRP": series_line,
                "Fallout.S02E04.1080p.WEB": series_line,
                "Gladiator.II.2024.1080p.WEB": ("Movies", "Gladiator II (2024)"),
                "Fallout.S02E05.720p": None,
            },
        )
        # "Open the folder" opens the new place.
        [episode] = [e for e in overview.entries if e.record.episode == 3]
        self.assertEqual(await self.service.locate(episode), Located(str(season), ""))


if __name__ == "__main__":
    unittest.main()
