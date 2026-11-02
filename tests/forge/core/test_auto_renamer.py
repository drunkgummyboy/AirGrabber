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
import time
from datetime import datetime
from pathlib import Path

from app.forge.clients.tmdb import TmdbError
from app.forge.core.auto import (
    AutoRenamer,
    NamingChoice,
    RenamerParts,
    WatchStatus,
    needs_message,
    summary_due,
    summary_message,
)
from app.forge.core.executor import Executor
from app.forge.core.matcher import Resolver
from app.forge.core.queue import ItemStatus, Queue
from app.forge.core.settings import ForgeSettingsService
from app.forge.core.undo import UndoService
from app.forge.db.history import HistoryRepo
from app.forge.db.matches import MatchRepo
from app.forge.db.presets import PresetRepo
from app.forge.db.settings import SettingsRepo
from app.forge.models.history import RunSource
from app.forge.models.media import MediaKind
from app.forge.models.plan import PlannedTrailer
from app.i18n import Language, set_language
from app.models.tmdb import TMDBSeason, TMDBSeasonEpisode, TMDBTitle
from tests.forge.core.journal_base import FakePosters
from tests.forge.db.base import ForgeDbTestCase


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
        self.now = time.time()

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
        when = time.time() - age_s
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
        assert auto.status([]) == ("trial", "Trial: watching 1 folder")
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
        os.utime(done, (time.time() - 3600, time.time() - 3600))
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
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        clock.now = datetime.now().replace(hour=20, minute=30).timestamp()
        os.utime(file, (clock.now - 3600, clock.now - 3600))  # finished an hour before "now"
        notes: list[tuple[str, str]] = []
        auto = self.renamer(clock, notes)
        await self.two_looks(auto, clock)
        clock.now += 300
        await auto.look()
        assert notes == [("MediaForge", "Today: 1 movie renamed")]
        today = datetime.now().date().isoformat()
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
        assert [i.status for i in self.queue.items] == [ItemStatus.DONE]
        clock.now += 24 * 3600
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

    async def test_off_then_on_during_a_look_keeps_watching(self) -> None:
        await self.use()
        auto = self.renamer(Clock())
        auto.start()
        await asyncio.sleep(0)
        await self.use(automatic="off")
        auto.reconfigure()
        await self.use(automatic="trial")
        auto.reconfigure()
        await asyncio.sleep(0.5)
        assert auto.running
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
        await self.use(automatic="trial")  # meanwhile the owner chose Trial
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
