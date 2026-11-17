"""Renaming on its own: finished downloads are looked up, decided on, and moved into the library
(MediaForge's tests/core/test_auto_renamer.py).

MediaForge built its whole `Services`; here the renamer's parts are put together by hand: a
temporary forge.db, the real queue, resolver, executor and undo, a fake TMDB and a fake clock.
Every folder is a temporary one: nothing real is watched or moved.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import threading
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from app.forge.clients.tmdb import TmdbError
from app.forge.core import auto as auto_module
from app.forge.core import fsops, watch
from app.forge.core.watch import QUIET_S, Settling
from app.forge.core.auto import (
    TOOLS_NOT_THERE,
    AutoRenamer,
    LookResult,
    NamingChoice,
    RenamerParts,
    WatchStatus,
    needs_message,
    settling_message,
    summary_due,
    summary_message,
    taking,
)
from app.forge.core.executor import Executor
from app.forge.core.matcher import Resolver
from app.forge.core.queue import ItemStatus, Queue
from app.forge.core.run_progress import AUTO, RunProgress, RunState
from app.forge.core.settings import ForgeSettingsService
from app.forge.core.undo import UndoService
from app.forge.db.history import HistoryRepo
from app.forge.db.matches import MatchRepo
from app.forge.db.presets import PresetRepo
from app.forge.db.settings import SettingsRepo
from app.forge.models.history import RunSource
from app.forge.models.media import MediaKind
from app.forge.models.plan import PlannedTrailer
from app.i18n import Language, set_language, t
from app.models.tmdb import TMDBSeason, TMDBSeasonEpisode, TMDBTitle
from tests.forge.core.journal_base import FakePosters
from tests.forge.db.base import ForgeDbTestCase

# Where the fake clock starts, and what the downloads' file times count from (the watcher compares
# the two): today at noon, once for the whole run. Never close to midnight, where a look minutes
# later is tomorrow and clears today's Done items. What the queue and History stamp stays real
# time, from this day's midnight on, so only a test that moves the clock past it purges anything.
NOON = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


class Tmdb:
    """TMDB as far as the matcher needs it."""

    def __init__(self) -> None:
        self.down = False

    def configured(self) -> bool:
        return True

    async def search_tv(self, query: str, year: int | None) -> list[TMDBTitle]:
        return [TMDBTitle(id=7, title="VisionQuest", date="2025-01-10", poster_path="/7.jpg")]

    async def search_movie(self, query: str, year: int | None) -> list[TMDBTitle]:
        if self.down:
            raise TmdbError("the router is restarting")
        return [TMDBTitle(id=8, title=query, date="2021-09-15", poster_path="/8.jpg")]

    async def details(self, kind: MediaKind, tmdb_id: int) -> TMDBTitle | None:
        return None

    async def season(self, tv_id: int, number: int) -> TMDBSeason | None:
        names = {1: "No Strings", 2: "Theseus"}
        episodes = tuple(TMDBSeasonEpisode(episode_number=n, name=name) for n, name in names.items())
        return TMDBSeason(season_number=number, poster_path="/s1.jpg", episodes=episodes)


class NoTrailerTools:
    def tools_present(self) -> bool:
        return False


class Clock:
    def __init__(self) -> None:
        self.now = NOON

    def __call__(self) -> float:
        return self.now


class Folders:
    def __init__(self, root: Path) -> None:
        self.downloads = root / "Downloads"
        self.series = root / "Library" / "Series"
        self.movies = root / "Library" / "Movies"
        for folder in (self.downloads, self.series, self.movies):
            folder.mkdir(parents=True)

    def download(self, name: str, age_s: float = 3600) -> Path:
        release = self.downloads / Path(name).stem
        release.mkdir(exist_ok=True)
        file = release / name
        file.write_bytes(b"video")
        when = NOON - age_s
        os.utime(file, (when, when))
        return file


class RenamerTestCase(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.settings_repo = SettingsRepo(self.db)
        self.settings = await ForgeSettingsService.load(self.settings_repo)
        self.history = HistoryRepo(self.db)
        presets = PresetRepo(self.db)
        await presets.seed_defaults_if_empty()
        self.tmdb = Tmdb()
        self.queue = Queue(Resolver(self.tmdb, MatchRepo(self.db)), lambda event: None)
        self.addAsyncCleanup(self._settle_queue)
        self.undo = UndoService(self.history)
        self.run_lock = asyncio.Lock()
        self.parts = RenamerParts(
            queue=self.queue,
            history=self.history,
            presets=presets,
            executor=Executor(self.history, FakePosters(), None),
            trailers=NoTrailerTools(),
            tmdb=self.tmdb,
            settings=self.settings,
            run_lock=self.run_lock,
        )
        self.folders = Folders(self.tmp)

    async def _settle_queue(self) -> None:
        self.queue.clear()
        await self.queue.wait_idle()

    async def use(self, automatic: str = "on") -> None:
        await self.settings.update(
            watch_folders=[str(self.folders.downloads)],
            series_folder=str(self.folders.series),
            movies_folder=str(self.folders.movies),
            automatic=automatic,
        )

    def renamer(self, clock: Clock, notes: list[tuple[str, str]] | None = None) -> AutoRenamer:
        auto = AutoRenamer(self.parts, now=clock)
        if notes is not None:
            auto.notify = lambda title, text: notes.append((title, text))
        self.addAsyncCleanup(auto.stop)
        return auto

    @staticmethod
    async def two_looks(auto: AutoRenamer, clock: Clock) -> None:
        await auto.look()  # first sight
        clock.now += 300
        await auto.look()  # unchanged since: finished


class OffByDefaultTest(RenamerTestCase):
    async def test_a_fresh_forge_watches_nothing(self) -> None:
        # The old MediaForge may still watch these folders: off until it is chosen.
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        await self.settings.update(
            watch_folders=[str(self.folders.downloads)],
            series_folder=str(self.folders.series),
            movies_folder=str(self.folders.movies),
        )
        assert self.settings.current.automatic == "off"
        clock = Clock()
        auto = self.renamer(clock)
        statuses: list[WatchStatus] = []
        auto.on_status = statuses.append
        auto.start()
        assert not auto.running
        auto.reconfigure()
        assert not auto.running
        await self.two_looks(auto, clock)
        assert file.exists() and self.queue.items == []
        assert statuses[-1] == ("off", "Not watching")

    async def test_choosing_off_stops_watching(self) -> None:
        await self.use()
        auto = self.renamer(Clock())
        auto.start()
        assert auto.running
        await self.use(automatic="off")
        auto.reconfigure()
        await auto.stop()
        assert not auto.running


class RenamerTest(RenamerTestCase):
    async def test_a_sure_episode_is_moved_into_the_series_folder(self) -> None:
        await self.use()
        self.folders.download("VisionQuest.S01E02.Theseus.1080p.WEB.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        season = self.folders.series / "VisionQuest (2025)" / "VisionQuest (2025) - Season 01"
        assert (season / "VisionQuest (2025) - S01E02 - Theseus.mkv").exists()
        assert not any(self.folders.downloads.iterdir())  # moved, and its release folder cleared
        [item] = self.queue.items
        assert item.status is ItemStatus.DONE and item.run_id is not None

    async def test_the_setting_takes_a_moved_download_out_of_the_queue(self) -> None:
        await self.use()
        await self.settings.update(remove_after_forge=True)
        self.folders.download("VisionQuest.S01E02.Theseus.1080p.WEB.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        season = self.folders.series / "VisionQuest (2025)" / "VisionQuest (2025) - Season 01"
        assert (season / "VisionQuest (2025) - S01E02 - Theseus.mkv").exists()
        assert self.queue.items == []
        clock.now += 300
        await auto.look()  # never taken back in
        assert self.queue.items == []
        assert len(await self.history.list_runs()) == 1

    async def test_the_owners_rename_names_each_download_as_its_band_chose(self) -> None:
        await self.use(automatic="off")
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        ids = await self.queue.add_watched([(file, self.folders.downloads)])
        await self.queue.wait_idle()
        flat = next(p for p in await PresetRepo(self.db).list() if p.name == "Movie (Flat)")
        auto = self.renamer(Clock())
        await auto.rename(
            ids,
            source=RunSource.APP,
            naming={ids[0]: NamingChoice(flat, flat.format, flat.options)},
        )
        assert (self.folders.movies / "Dune (2021).mkv").exists()  # flat, still in the library
        [run] = await self.history.list_runs()
        assert run.preset_name == "Movie (Flat)"

    async def test_a_sure_movie_is_moved_into_the_movies_folder(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert (self.folders.movies / "Dune (2021)" / "Dune (2021).mkv").exists()

    async def test_without_trailer_tools_activity_says_there_is_no_trailer(self) -> None:
        presets = PresetRepo(self.db)
        [movie] = [p for p in await presets.list() if p.name == "Movie (Folder)"]
        options = movie.options.model_copy(update={"trailer": True})
        await presets.save(movie.model_copy(update={"options": options}), old_name=movie.name)
        await self.use()
        said: list[str] = []
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        auto._say = lambda text, **_kw: said.append(text)
        await self.two_looks(auto, clock)
        self.assertIn(t(TOOLS_NOT_THERE), said)

    async def test_without_trailer_tools_no_trailer_is_tried(self) -> None:
        presets = PresetRepo(self.db)
        [movie] = [p for p in await presets.list() if p.name == "Movie (Folder)"]
        options = movie.options.model_copy(update={"trailer": True})
        await presets.save(movie.model_copy(update={"options": options}), old_name=movie.name)
        tried: list[PlannedTrailer] = []

        class Downloader:
            async def download(self, trailer: PlannedTrailer) -> Path | None:
                tried.append(trailer)
                raise RuntimeError("yt-dlp is missing")

        executor = Executor(self.history, FakePosters(), Downloader())
        self.parts = dataclasses.replace(self.parts, executor=executor)
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        with self.assertNoLogs("app.forge.core.executor", logging.WARNING):
            await self.two_looks(self.renamer(clock), clock)
        assert (self.folders.movies / "Dune (2021)" / "Dune (2021).mkv").exists()
        assert tried == []

    async def test_trial_moves_nothing(self) -> None:
        await self.use(automatic="trial")
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert file.exists()
        [item] = self.queue.items
        assert item.status is ItemStatus.READY and item.watched == self.folders.downloads

    async def test_an_unsure_match_waits_with_its_reason(self) -> None:
        await self.use()
        file = self.folders.download("Dune.mkv")  # no year: is it a movie?
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert file.exists()
        [item] = self.queue.items
        assert item.status is ItemStatus.HELD
        assert item.message == "No year in the name: is it a movie?"

    async def test_a_file_still_coming_in_waits(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv", age_s=60)  # written a minute ago
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert self.queue.items == []

    async def test_an_unreachable_library_holds_and_retries(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        self.folders.movies.rmdir()  # the NAS share is away
        clock = Clock()
        auto = self.renamer(clock)
        statuses: list[str] = []
        auto.on_status = lambda status: statuses.append(status.state)
        await self.two_looks(auto, clock)
        [item] = self.queue.items
        assert item.status is ItemStatus.HELD and "Can't reach" in item.message
        assert statuses[-1] == "unreachable"
        self.folders.movies.mkdir()  # back again
        clock.now += 300
        await auto.look()
        assert (self.folders.movies / "Dune (2021)" / "Dune (2021).mkv").exists()
        assert statuses[-1] == "watching"

    async def test_an_unreachable_download_folder_changes_the_status(self) -> None:
        await self.use()
        self.folders.downloads.rmdir()
        auto = self.renamer(Clock())
        statuses: list[tuple[str, str]] = []
        auto.on_status = lambda status: statuses.append((status.state, status.text))
        result = await auto.look()
        assert result.unreachable == [self.folders.downloads]
        assert statuses[-1] == ("unreachable", f"Can't reach {self.folders.downloads}")

    async def test_status_texts(self) -> None:
        auto = self.renamer(Clock())
        assert auto.status([]) == ("off", "Not watching")
        await self.settings.update(automatic="on")
        assert auto.status([]) == ("setup", "Set up watching in Settings")
        await self.use(automatic="trial")
        assert auto.status([]) == ("trial", "Manual: watching 1 folder")
        await self.use()
        assert auto.status([]) == ("watching", "Watching 1 folder")
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        assert auto.status([]) == ("watching", "Bewaakt 1 map")

    async def test_an_undone_file_is_not_moved_again(self) -> None:
        await self.use()
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        [item] = self.queue.items
        assert item.run_id is not None
        await self.undo.undo_run(item.run_id)
        assert file.exists()  # back in the download folder
        clock.now += 300
        await self.two_looks(auto, clock)
        assert file.exists()
        held = [i for i in self.queue.items if i.status is ItemStatus.HELD]
        assert [i.message for i in held] == ["You undid this once; click Rename to move it again."]
        # Remembered in the settings, saved in forge.db.
        stored = await self.settings_repo.load()
        assert stored.kept_back == [os.path.normcase(str(file))]

    async def test_files_in_a_library_folder_are_ignored(self) -> None:
        await self.use()
        overlapping = str(self.folders.series.parent)  # watching the library itself
        await self.settings.update(watch_folders=[overlapping])
        done = self.folders.series / "Show (2020)" / "Show (2020) - S01E01.mkv"
        done.parent.mkdir(parents=True)
        done.write_bytes(b"video")
        os.utime(done, (NOON - 3600, NOON - 3600))
        clock = Clock()
        auto = self.renamer(clock)
        statuses: list[str] = []
        auto.on_status = lambda status: statuses.append(status.state)
        await self.two_looks(auto, clock)
        assert self.queue.items == [] and done.exists()
        assert statuses[-1] == "setup"

    async def test_one_automatic_run_per_kind_per_look(self) -> None:
        await self.use()
        self.folders.download("VisionQuest.S01E01.No.Strings.1080p.WEB.mkv")
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        runs = await self.history.list_runs()
        assert sorted(tuple(r.kinds) for r in runs) == [(MediaKind.EPISODE,), (MediaKind.MOVIE,)]
        assert {r.source for r in runs} == {RunSource.AUTOMATIC}

    async def test_the_number_counts_what_waits_the_notice_only_what_needs_you(self) -> None:
        await self.use(automatic="trial")
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        notes: list[tuple[str, str]] = []
        auto = self.renamer(clock, notes)
        told: list[int] = []
        auto.on_needs = told.append
        await self.two_looks(auto, clock)
        assert told[-1] == 1  # ready, waiting for Forge
        assert notes == []  # nothing to decide: no notification

    async def test_the_needs_notification_comes_once_per_batch(self) -> None:
        await self.use()
        self.folders.download("Dune.mkv")
        self.folders.download("Heat.mkv")
        clock = Clock()
        notes: list[tuple[str, str]] = []
        auto = self.renamer(clock, notes)
        await self.two_looks(auto, clock)
        clock.now += 300
        await auto.look()  # nothing new
        assert notes == [("2 files need you", "Open MediaForge to pick or confirm them.")]

    async def test_the_evening_summary_counts_todays_automatic_renames(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        notes: list[tuple[str, str]] = []
        auto = self.renamer(clock, notes)
        await self.two_looks(auto, clock)
        assert notes == []  # renamed at noon: too early for the summary
        [run] = await self.history.list_runs()  # started in real time: the evening of that day
        clock.now = run.started_at.replace(hour=20, minute=30).timestamp()
        await auto.look()
        assert notes == [("MediaForge", "Today: 1 movie renamed")]
        today = run.started_at.date().isoformat()
        assert self.settings.current.summary_sent_on == today
        assert (await self.settings_repo.load()).summary_sent_on == today
        clock.now += 300
        await auto.look()
        assert len(notes) == 1  # once a day

    async def test_unreachable_is_logged_once_without_the_full_path(self) -> None:
        await self.use()
        self.folders.downloads.rmdir()
        clock = Clock()
        auto = self.renamer(clock)
        with self.assertLogs("app.forge.core.auto", logging.INFO) as logs:
            for _ in range(3):
                await auto.look()
                clock.now += 300
            self.folders.downloads.mkdir()
            await auto.look()
            await auto.look()
        lost = [line for line in logs.output if "Can't reach the folder Downloads" in line]
        back = [line for line in logs.output if "Can reach the folder Downloads again" in line]
        assert len(lost) == 1 and len(back) == 1
        assert all(str(self.tmp) not in line for line in logs.output)

    async def test_done_today_rolls_over_at_midnight(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        [item] = self.queue.items
        assert item.status is ItemStatus.DONE and item.finished_at is not None
        finished = datetime.fromtimestamp(item.finished_at)  # real time: that day's midnight counts
        clock.now = finished.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()
        await auto.look()
        assert self.queue.items == [item]  # a look just before midnight keeps it
        next_day = finished + timedelta(days=1)
        clock.now = next_day.replace(hour=0, minute=0, second=1, microsecond=0).timestamp()
        await auto.look()
        assert self.queue.items == []

    async def test_rename_by_the_owner_moves_a_held_file(self) -> None:
        await self.use()
        self.folders.download("Dune.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        [item] = self.queue.items
        assert item.status is ItemStatus.HELD
        result = await auto.rename([item.id], source=RunSource.APP)
        assert result.renamed == 1
        assert (self.folders.movies / "Dune (2021)" / "Dune (2021).mkv").exists()
        moved = self.queue.get(item.id)  # the same item, read again (mypy narrowed `item`)
        assert moved is item and moved.status is ItemStatus.DONE

    async def test_rename_after_off_still_moves(self) -> None:
        await self.use()
        self.folders.download("Dune.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        auto.start()
        await asyncio.sleep(0.2)
        clock.now += 300
        auto.look_soon()
        await asyncio.sleep(0.5)
        [item] = self.queue.items
        await self.use(automatic="off")
        auto.reconfigure()
        await auto.stop()
        result = await auto.rename([item.id], source=RunSource.APP)
        assert result.renamed == 1 and item.status is ItemStatus.DONE

    async def test_a_scan_finds_what_just_finished_without_waiting(self) -> None:
        await self.use(automatic="trial")
        clock = Clock()
        auto = AutoRenamer(self.parts, now=clock, interval_s=3600, second_look_s=3600, scan_look_s=0.01)
        self.addAsyncCleanup(auto.stop)
        auto.start()
        await asyncio.sleep(0.1)
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock.now += 300
        result = await auto.scan_now()
        assert result is not None and result.added == 1
        assert len(self.queue.items) == 1
        result = await auto.scan_now()  # nothing new since
        assert result is not None and result.added == 0

    async def test_no_scan_while_nothing_is_watched(self) -> None:
        await self.use(automatic="off")
        auto = self.renamer(Clock())
        auto.start()
        assert await auto.scan_now() is None

    async def test_two_scans_at_once_share_one_and_a_stop_ends_the_wait(self) -> None:
        await self.use(automatic="trial")
        auto = AutoRenamer(self.parts, now=Clock(), interval_s=3600, second_look_s=3600, scan_look_s=3600)
        self.addAsyncCleanup(auto.stop)
        auto.start()
        await asyncio.sleep(0.1)
        waiting = [asyncio.ensure_future(auto.scan_now()) for _ in range(2)]
        await asyncio.sleep(0.1)
        await auto.stop()
        assert await asyncio.gather(*waiting) == [None, None]

    async def test_after_a_start_the_second_look_comes_soon_then_the_interval(self) -> None:
        await self.use()
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        auto = AutoRenamer(self.parts, now=Clock(), interval_s=3600, second_look_s=0.01)
        self.addAsyncCleanup(auto.stop)
        looks = 0
        real_look = auto.look

        async def counted() -> LookResult:
            nonlocal looks
            looks += 1
            return await real_look()

        auto.look = counted  # type: ignore[method-assign]
        auto.start()
        for _ in range(500):
            if not file.exists():
                break
            await asyncio.sleep(0.01)
        assert not file.exists()  # what was already there is renamed right after opening
        assert looks == 2
        await asyncio.sleep(0.1)
        assert looks == 2  # from then on, the interval

    async def test_the_second_look_never_waits_longer_than_the_interval(self) -> None:
        auto = AutoRenamer(self.parts, now=Clock(), interval_s=5, second_look_s=30)
        assert auto._second_look == 5

    async def test_off_then_on_during_a_look_keeps_watching(self) -> None:
        await self.use()
        auto = self.renamer(Clock())
        auto.start()
        await asyncio.sleep(0)
        await self.use(automatic="off")
        auto.reconfigure()
        await self.use(automatic="trial")
        auto.reconfigure()
        # The look under way ends first (its folder reads run on a thread: slow on a busy
        # machine), then it starts again: waited for, not a fixed time.
        for _ in range(200):
            if auto.running and not auto._stopping:
                break
            await asyncio.sleep(0.05)
        assert auto.running and not auto._stopping
        await auto.stop()

    async def test_a_mode_change_before_the_lock_moves_nothing(self) -> None:
        await self.use()
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await auto.look()
        clock.now += 300
        await self.run_lock.acquire()  # the owner's Forge is busy
        look = asyncio.get_running_loop().create_task(auto.look())
        await asyncio.sleep(0.3)
        await self.use(automatic="trial")  # meanwhile the owner chose Manual
        self.run_lock.release()
        await look
        assert file.exists()

    async def test_kept_back_survives_history_trimming(self) -> None:
        await self.use()
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        [item] = self.queue.items
        assert item.run_id is not None
        await self.undo.undo_run(item.run_id)
        clock.now += 300
        await self.two_looks(auto, clock)  # it comes back, and is held as undone
        async with self.db.transaction() as conn:  # later trimmed after 50 newer runs
            await conn.execute("DELETE FROM runs")
        self.queue.clear()
        fresh = self.renamer(clock)  # as after a restart
        clock.now += 300
        await self.two_looks(fresh, clock)
        assert file.exists()
        [held] = self.queue.items
        assert held.status is ItemStatus.HELD

    async def test_offline_files_are_looked_up_again(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        self.tmdb.down = True
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        [item] = self.queue.items
        assert item.status is ItemStatus.HELD
        self.tmdb.down = False
        clock.now += 300
        await auto.look()
        assert (self.folders.movies / "Dune (2021)" / "Dune (2021).mkv").exists()


class SettlingTest(RenamerTestCase):
    """Downloads seen but not quiet yet: how many, and when the first should be ready (item 29)."""

    async def test_a_look_counts_the_downloads_still_settling(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv", age_s=120)  # written 2 minutes ago
        self.folders.download("Heat.1995.1080p.mkv", age_s=60)
        clock = Clock()
        auto = self.renamer(clock)
        assert (await auto.look()).settling == Settling()  # first seen: may still be growing
        clock.now += 60
        result = await auto.look()
        assert result.settling == Settling(2, NOON - 120 + QUIET_S)
        assert auto.settling == result.settling  # not running: no look planned
        clock.now += QUIET_S
        await auto.look()
        assert auto.settling == Settling()  # both renamed
        assert len(self.queue.items) == 2

    async def test_the_time_counts_to_the_look_that_takes_it_in(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv", age_s=120)  # quiet at NOON + 480
        clock = Clock()  # stands still: the looks come by the real clock, the times by this one
        auto = AutoRenamer(
            self.parts, now=clock, interval_s=300, second_look_s=300, scan_look_s=0.05
        )
        self.addAsyncCleanup(auto.stop)
        auto.start()
        result = await auto.scan_now()  # two looks; the next timed one at NOON + 300
        assert result is not None
        # Quiet between two looks: it comes in at the one after, NOON + 600.
        assert result.settling == Settling(1, NOON + 600) and auto.settling == result.settling
        self.assertEqual(
            settling_message(result.settling, NOON), "1 download finishing, ready in about 10 min"
        )
        auto._settling_seen = Settling(1, NOON + 200)  # as if one were quiet at NOON + 200
        # Quiet before the next look: that look takes it in, not "under a minute" for 5.
        self.assertEqual(auto.settling, Settling(1, NOON + 300))
        self.assertEqual(
            settling_message(auto.settling, NOON + 199),
            "1 download finishing, ready in about 2 min",
        )
        await auto.stop()
        assert auto.settling == Settling()

    async def test_nothing_settles_while_off_or_unreachable(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv", age_s=120)
        auto = self.renamer(Clock())
        await auto.look()
        await auto.look()  # unchanged since: settling
        assert auto.settling.files == 1
        await self.settings.update(automatic="off")
        assert (await auto.look()).settling == Settling() and auto.settling == Settling()
        await self.settings.update(automatic="on", watch_folders=[str(self.tmp / "gone")])
        assert (await auto.look()).settling == Settling()

    def test_the_message_rounds_up_to_whole_minutes(self) -> None:
        set_language(Language.EN)
        self.assertIsNone(settling_message(Settling(), NOON))
        self.assertEqual(
            settling_message(Settling(1, NOON + 480), NOON),
            "1 download finishing, ready in about 8 min",
        )
        self.assertEqual(
            settling_message(Settling(2, NOON + 481), NOON),
            "2 downloads finishing, ready in about 9 min",
        )
        self.assertEqual(
            settling_message(Settling(1, NOON + 59), NOON),
            "1 download finishing, ready in under a minute",
        )
        self.assertEqual(  # due already: the next look takes it
            settling_message(Settling(3, NOON - 30), NOON),
            "3 downloads finishing, ready in under a minute",
        )
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        self.assertEqual(
            settling_message(Settling(1, NOON + 480), NOON),
            "1 download bijna binnen, klaar over ongeveer 8 min",
        )


class ThreadWatcher:
    """A watcher that notes which thread asks it, and can be made to hang until `free` is set."""

    settling = Settling()

    def __init__(self, hang: threading.Event | None = None) -> None:
        self.threads: list[int] = []
        self.hang = hang
        self.at_once = 0  # how many of its checks run right now
        self.most_at_once = 0

    def finished(self, files: Sequence[Path]) -> list[Path]:
        self.threads.append(threading.get_ident())
        self.at_once += 1
        self.most_at_once = max(self.most_at_once, self.at_once)
        try:
            if self.hang is not None:
                self.hang.wait(10)
            return []
        finally:
            self.at_once -= 1


class SilentFolderTest(RenamerTestCase):
    """A NAS that takes the connection but never answers (2026-11-14): a look still ends."""

    LIMIT_S = 0.3

    def hanging(self) -> threading.Event:
        free = threading.Event()
        self.addCleanup(free.set)  # the abandoned thread ends with the test
        return free

    async def test_the_finished_check_runs_off_the_loop(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        watcher = ThreadWatcher()
        auto = AutoRenamer(self.parts, now=Clock(), new_watcher=lambda: watcher)
        self.addAsyncCleanup(auto.stop)
        await auto.look()
        assert watcher.threads and threading.get_ident() not in watcher.threads

    async def test_a_silent_folder_counts_as_unreachable_after_the_limit(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        free = self.hanging()
        calls: list[Path] = []
        real = watch.candidates

        def silent(root: Path, ignored: object = None) -> list[Path]:
            calls.append(root)
            if not free.is_set():
                free.wait(10)  # the share took the connection, and says nothing
            return real(root, None)

        clock = Clock()
        auto = AutoRenamer(self.parts, now=clock, step_timeout_s=self.LIMIT_S)
        self.addAsyncCleanup(auto.stop)
        statuses: list[tuple[str, str]] = []
        auto.on_status = lambda status: statuses.append((status.state, status.text))
        with mock.patch.object(auto_module, "candidates", silent), self.assertLogs(
            "app.forge.core.auto", logging.INFO
        ) as logs:
            began = time.monotonic()
            result = await auto.look()
            assert time.monotonic() - began < self.LIMIT_S + 2
            assert result.unreachable == [self.folders.downloads]
            assert statuses[-1] == ("unreachable", f"Can't reach {self.folders.downloads}")
            began = time.monotonic()
            result = await auto.look()  # its thread still hangs: the folder is skipped at once
            assert time.monotonic() - began < self.LIMIT_S + 2  # slack for a slow disk; hang: 10 s
            assert result.unreachable == [self.folders.downloads] and len(calls) == 1
            free.set()  # the NAS answers again
            for _ in range(200):
                await asyncio.sleep(0.01)
                result = await auto.look()
                if not result.unreachable:
                    break
            assert result.unreachable == [] and statuses[-1][0] == "watching"
        assert sum("Can't reach the folder Downloads" in line for line in logs.output) == 1
        assert sum("Can reach the folder Downloads again" in line for line in logs.output) == 1

    async def test_a_silent_finished_check_never_runs_twice_at_once(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        free = self.hanging()
        watcher = ThreadWatcher(hang=free)
        auto = AutoRenamer(
            self.parts, now=Clock(), new_watcher=lambda: watcher, step_timeout_s=self.LIMIT_S
        )
        self.addAsyncCleanup(auto.stop)
        for _ in range(3):
            result = await auto.look()
            assert result.unreachable == [self.folders.downloads]
        assert len(watcher.threads) == 1 and watcher.most_at_once == 1

    async def test_a_silent_forget_gone_is_limited_and_skipped_while_it_hangs(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        free = asyncio.Event()
        self.addCleanup(free.set)
        calls: list[list[Path]] = []

        class NothingIgnored:
            def ignored(self, path: Path | str) -> bool:
                return False

            def folder_ignored(self, folder: Path | str) -> bool:
                return False

        class SilentIgnores:
            def snapshot(self) -> NothingIgnored:
                return NothingIgnored()

            async def forget_gone(self, watched: Iterable[Path]) -> int:
                calls.append(list(watched))
                await free.wait()  # it asks the share, which says nothing
                return 0

        parts = dataclasses.replace(self.parts, ignored=SilentIgnores())
        auto = AutoRenamer(parts, now=Clock(), step_timeout_s=self.LIMIT_S)
        self.addAsyncCleanup(auto.stop)
        # Slack for a slow disk or guessit's first load; the hang itself never ends, so a look
        # that waited for it (the limit ignored) runs into this bound and fails.
        bound = self.LIMIT_S + 2.0
        result = await asyncio.wait_for(auto.look(), bound)
        assert result.unreachable == []  # the folders themselves answered
        await asyncio.wait_for(auto.look(), bound)  # the earlier one still hangs: not asked again
        assert len(calls) == 1  # (asked again, it would have waited the limit once more)
        free.set()
        await asyncio.sleep(0.05)
        await auto.look()  # it ended: asked again
        assert len(calls) == 2

    async def test_a_silent_library_folder_counts_as_unreachable(self) -> None:
        await self.use()
        free = self.hanging()
        real = fsops.is_dir
        movies = str(self.folders.movies)

        def silent(path: Path) -> bool:
            if str(path) == movies:
                free.wait(10)
            return real(path)

        auto = AutoRenamer(self.parts, now=Clock(), step_timeout_s=self.LIMIT_S)
        self.addAsyncCleanup(auto.stop)
        statuses: list[tuple[str, str]] = []
        auto.on_status = lambda status: statuses.append((status.state, status.text))
        with mock.patch.object(fsops, "is_dir", silent):
            await auto.look()
        assert statuses[-1] == ("unreachable", f"Can't reach {self.folders.movies}")

    async def test_the_owners_rename_says_when_it_waits_for_the_lock(self) -> None:
        await self.use(automatic="off")
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        ids = await self.queue.add_watched([(file, self.folders.downloads)])
        await self.queue.wait_idle()
        auto = self.renamer(Clock())
        heard: list[bool] = []
        await self.run_lock.acquire()  # an automatic run is busy
        try:
            task = asyncio.get_running_loop().create_task(
                auto.rename(ids, source=RunSource.APP, on_wait=heard.append)
            )
            await asyncio.sleep(0.1)
            assert heard == [True] and file.exists()
        finally:
            self.run_lock.release()
        result = await task
        assert heard == [True, False] and result.renamed == 1

    async def test_a_rename_that_never_waits_says_nothing(self) -> None:
        heard: list[bool] = []
        async with taking(self.run_lock, heard.append):
            pass
        assert heard == [] and not self.run_lock.locked()


class MessagesTest(RenamerTestCase):
    def test_summary_message_texts(self) -> None:
        assert summary_message(5, 1) == "Today: 5 episodes and 1 movie renamed"
        assert summary_message(1, 0) == "Today: 1 episode renamed"
        assert summary_message(0, 3) == "Today: 3 movies renamed"
        assert needs_message(1) == ("1 file needs you", "Open MediaForge to pick or confirm them.")

    def test_messages_in_dutch(self) -> None:
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        assert summary_message(5, 1) == "Vandaag: 5 afleveringen en 1 film hernoemd"
        assert needs_message(2)[0] == "2 bestanden hebben je nodig"

    def test_summary_due_once_a_day_after_eight(self) -> None:
        evening = datetime(2026, 11, 2, 20, 5)
        assert not summary_due(datetime(2026, 11, 2, 19, 59), "")
        assert summary_due(evening, "")
        assert summary_due(evening, "2026-11-01")
        assert not summary_due(evening, "2026-11-02")  # sent today already


class RunProgressTest(RenamerTestCase):
    """An automatic run reports to MediaForge's run progress (the dock on every page of the main
    window) and goes idle at its end, whatever happened; a run whose caller shows its own
    progress (the page) leaves that to the caller."""

    def follow(self, clock: Clock) -> tuple[AutoRenamer, list[RunState]]:
        progress = RunProgress()
        heard: list[RunState] = []
        progress.subscribe(heard.append)
        auto = AutoRenamer(self.parts, now=clock, progress=progress)
        self.addAsyncCleanup(auto.stop)
        return auto, heard

    async def test_an_automatic_run_reports_its_steps_then_goes_idle(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto, heard = self.follow(clock)
        await self.two_looks(auto, clock)
        assert (self.folders.movies / "Dune (2021)" / "Dune (2021).mkv").exists()
        busy = [state for state in heard if state.running]
        assert busy and all(state.source == AUTO and state.cancel is None for state in busy)
        assert busy[0].text == "Renaming 1 of 1"
        assert any(state.text == "Renaming 1 of 1 · Dune" for state in busy)
        assert not heard[-1].running

    async def test_a_failed_run_goes_idle_too(self) -> None:
        await self.use()
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto, heard = self.follow(clock)
        await auto.look()
        clock.now += 300
        broken = mock.AsyncMock(side_effect=RuntimeError("disk"))
        with mock.patch.object(self.parts.executor, "run", broken), self.assertRaises(RuntimeError):
            await auto.look()
        assert broken.called
        assert any(state.running for state in heard)
        assert not heard[-1].running

    async def test_a_run_whose_caller_shows_its_progress_isnt_reported(self) -> None:
        await self.use(automatic="off")
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        ids = await self.queue.add_watched([(file, self.folders.downloads)])
        await self.queue.wait_idle()
        auto, heard = self.follow(Clock())
        events: list[object] = []
        result = await auto.rename(ids, source=RunSource.APP, on_progress=events.append)
        assert result.renamed == 1 and events
        assert heard == []

    async def test_the_owners_rename_without_its_own_progress_is_reported(self) -> None:
        # The phone's tap: nobody else shows that run.
        await self.use(automatic="off")
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        ids = await self.queue.add_watched([(file, self.folders.downloads)])
        await self.queue.wait_idle()
        auto, heard = self.follow(Clock())
        result = await auto.rename(ids, source=RunSource.APP)
        assert result.renamed == 1
        assert any(state.running for state in heard) and not heard[-1].running
