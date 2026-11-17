"""The folder library's service (app/services/folder_library/service.py): when the folders are
read, switching on and off, the folders beside Plex, Forge's moves and the status. A real temporary
database and temporary folders with empty files, a fake clock and sleep, a fake TMDB / TVMaze
side and a fake Plex link; never a live TMDB, TVMaze, NAS or Plex."""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_args
from unittest import mock

from app.database import Database
from app.forge.core import watch
from app.forge.models.media import MediaKind
from app.forge.models.moves import MoveRecord
from app.models import MediaType, Show
from app.models.folder_library import SwitchOutcome
from app.models.plex import PlexLibrary, PlexScan, PlexTitle
from app.models.tmdb import TMDBTitle, TMDBTitleIds
from app.services.folder_library import scan
from app.services.folder_library import service as folder_service
from app.services.folder_library.service import (
    ARRIVING_MARGIN_S,
    CHECK_EVERY_S,
    MOVE_CHECK_S,
    QUIET_S,
    FolderLibraryService,
)
from app.services.library import LibraryService
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from tests.test_folder_scan import FakeTMDB

NOW = 1_800_000_000.0  # the fake clock's start
OLD = NOW - 3600  # written an hour before: settled

DUNE = TMDBTitle(id=438631, title="Dune", date="2021-09-15", poster_path="/dune.jpg")
ARRIVAL = TMDBTitle(id=329865, title="Arrival", date="2016-11-10", poster_path="/arr.jpg")
HEAT = TMDBTitle(id=949, title="Heat", date="1995-12-15", poster_path="/heat.jpg")
SEVERANCE = TMDBTitle(id=95396, title="Severance", date="2022-02-17", poster_path="/sev.jpg")


def film_ids(title: TMDBTitle, imdb: str) -> TMDBTitleIds:
    return TMDBTitleIds.model_validate(
        {"id": title.id, "title": title.title, "release_date": f"{title.year}-01-01",
         "poster_path": title.poster_path, "imdb_id": imdb}
    )


def make_tmdb() -> FakeTMDB:
    return FakeTMDB(
        searches={
            (MediaType.MOVIE, "Dune", 2021): [DUNE],
            (MediaType.MOVIE, "Arrival", 2016): [ARRIVAL],
            (MediaType.MOVIE, "Heat", 1995): [HEAT],
            (MediaType.TV, "Severance", 2022): [SEVERANCE],
        },
        details={
            (MediaType.MOVIE, DUNE.id): film_ids(DUNE, "tt1160419"),
            (MediaType.MOVIE, ARRIVAL.id): film_ids(ARRIVAL, "tt2543164"),
            (MediaType.MOVIE, HEAT.id): film_ids(HEAT, "tt0113277"),
            (MediaType.TV, SEVERANCE.id): TMDBTitleIds.model_validate(
                {"id": SEVERANCE.id, "name": "Severance", "first_air_date": "2022-02-17",
                 "poster_path": "/sev.jpg",
                 "external_ids": {"imdb_id": "tt11280740", "tvdb_id": 371980}}
            ),
        },
    )


class FakeLibrary:
    """LibraryService's part the folder library uses; counts `notify_changed` and calls the
    `on_followed` listeners when `followed` is. No `hold_marks`: the marks are never held."""

    def __init__(self) -> None:
        self.notified = 0
        self.cached: set[int] = set()
        self.follow_listeners: list[Callable[[], None]] = []

    def notify_changed(self) -> None:
        self.notified += 1

    def on_followed(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.follow_listeners.append(listener)
        return lambda: self.follow_listeners.remove(listener)

    def followed(self, show_id: int) -> None:
        self.cached.add(show_id)
        for listener in list(self.follow_listeners):
            listener()

    async def find_show(
        self,
        *,
        show_id: int | None = None,
        tvdb_id: int | None = None,
        imdb_id: str | None = None,
        name: str = "",
    ) -> Show | None:
        return Show(id=44933, name="Severance") if name == "Severance" else None

    async def cached_shows(self, show_ids: Any) -> dict[int, Show]:
        return {i: Show(id=i, name=str(i)) for i in show_ids if i in self.cached}


class FakePlex:
    """PlexService's status listeners: `notify` as it calls them after a connect / disconnect."""

    def __init__(self) -> None:
        self.listeners: list[Callable[[], None]] = []

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def notify(self) -> None:
        for listener in list(self.listeners):
            listener()


class FakeMoves:
    """Forge's move records (ForgeMoveService's `moved_after` / `subscribe`)."""

    def __init__(self) -> None:
        self.records: list[MoveRecord] = []
        self.listeners: list[Callable[[], None]] = []
        self.asked = 0

    async def moved_after(self, when: float) -> list[MoveRecord]:
        self.asked += 1
        return [record for record in self.records if record.moved_at.timestamp() > when]

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def move(self, target: Path, at: float) -> None:
        self.records.append(
            MoveRecord(source=Path("C:/Downloads/x.mkv"), target=target,
                       moved_at=datetime.fromtimestamp(at), kind=MediaKind.MOVIE, tmdb_id=None,
                       run_id=1)
        )
        for listener in list(self.listeners):
            listener()


class FakeTime:
    """The clock and the sleep: a wait of CHECK_EVERY_S or more holds until `wake`; shorter
    ones (check_soon's) pass at once. Every wait moves the clock on."""

    def __init__(self) -> None:
        self.now = NOW
        self.sleeps: list[float] = []
        self.waking = asyncio.Event()

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        if delay >= CHECK_EVERY_S:
            await self.waking.wait()
            self.waking.clear()
        self.now += delay

    def wake(self) -> None:
        self.waking.set()


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = await Database.open(self.root / "airgrabber.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = await SettingsService.load(self.db.settings)
        self.tmdb = make_tmdb()
        self.library = FakeLibrary()
        self.plex = FakePlex()
        self.time = FakeTime()
        self.films = self.folder("Films")
        self.series = self.folder("Series")
        self.dune = self.touch(self.films, "Dune (2021)/Dune (2021) - 1080p.mkv")

    def make(self, **values: Any) -> FolderLibraryService:
        service = FolderLibraryService(
            self.settings,
            self.db.library_files,
            self.db.folders,
            cast(TMDBClient, self.tmdb),
            cast(LibraryService, self.library),
            clock=self.time.clock,
            sleep=self.time.sleep,
            plex=self.plex,
            **values,
        )
        self.addAsyncCleanup(service.aclose)
        return service

    def folder(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def touch(self, folder: Path, relative: str, *, mtime: float = OLD) -> Path:
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        os.utime(path, (mtime, mtime))
        return path

    async def keys(self) -> set[str]:
        return {title.key for title in await self.db.folders.titles()}

    async def until(self, check: Callable[[], Any], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            outcome = check()
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if outcome:
                return
            if time.monotonic() > deadline:
                self.fail("timed out waiting")
            await asyncio.sleep(0.01)

    async def has(self, key: str) -> bool:
        return key in await self.keys()

    async def lacks(self, key: str) -> bool:
        return key not in await self.keys()


class FolderLibraryServiceTests(ServiceTestCase):
    async def test_nothing_is_read_while_off(self) -> None:
        await self.settings.update(film_folders=(str(self.films),))  # set, never switched on
        service = self.make()
        reads = mock.AsyncMock(wraps=scan.read_folder)
        with mock.patch.object(folder_service, "read_folder", reads):
            run = asyncio.create_task(service.run())
            await self.until(lambda: self.time.sleeps == [CHECK_EVERY_S])
            await service.check()
            service.check_soon()  # no timer either
            await asyncio.sleep(0.05)
            self.assertEqual(self.time.sleeps, [CHECK_EVERY_S])
            run.cancel()
        reads.assert_not_called()
        self.assertEqual(self.tmdb.searched, [])
        self.assertEqual(await self.db.library_files.all(), {})
        self.assertEqual(await self.keys(), set())
        self.assertEqual(self.library.notified, 0)
        self.assertFalse(service.status.on)

    async def test_switch_on_reads_and_stores(self) -> None:
        service = self.make()
        on_when_stored: list[bool] = []
        real_replace = self.db.folders.replace

        async def replace(scan_: PlexScan, when: datetime) -> None:
            on_when_stored.append(self.settings.current.folder_library_on)
            await real_replace(scan_, when)

        with mock.patch.object(self.db.folders, "replace", replace):
            outcome = await service.switch_on([str(self.films)], [str(self.series)])
        self.assertEqual(outcome, "on")

        # What was read is stored before the switch is: never a library over empty tables.
        self.assertEqual(on_when_stored, [False])
        current = self.settings.current
        self.assertTrue(current.folder_library_on)
        self.assertEqual(
            (current.film_folders, current.series_folders), ((str(self.films),), (str(self.series),))
        )
        self.assertEqual(await self.keys(), {"folder:movie:438631"})
        episodes, films = await self.db.folders.contents()
        self.assertEqual(films, frozenset({(438631, "tt1160419")}))
        self.assertEqual(await self.db.plex.contents(), (frozenset(), frozenset()))  # Plex's
        rows = await self.db.library_files.all()
        self.assertEqual(set(rows), {str(self.dune)})
        self.assertTrue(rows[str(self.dune)].recognised)
        self.assertEqual(self.library.notified, 1)
        self.assertTrue(service.status.on)
        self.assertEqual(service.status.films, 1)
        self.assertTrue(self.dune.exists())

    async def test_a_check_every_six_hours(self) -> None:
        service = self.make()
        await service.switch_on([str(self.films)], [])
        searched = len(self.tmdb.searched)
        run = asyncio.create_task(service.run())
        self.addCleanup(run.cancel)
        await self.until(lambda: self.time.sleeps == [CHECK_EVERY_S])
        self.assertEqual(len(self.tmdb.searched), searched)  # the start's check: nothing new
        self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv")
        self.time.wake()  # six hours later
        await self.until(lambda: self.has("folder:movie:329865"))
        await self.until(lambda: self.time.sleeps == [CHECK_EVERY_S, CHECK_EVERY_S])
        self.assertEqual(self.time.now, NOW + CHECK_EVERY_S)
        self.assertEqual(service.status.films, 2)

    async def test_a_move_brings_a_check(self) -> None:
        service = self.make()
        await service.switch_on([str(self.films)], [])
        moves = FakeMoves()
        service.attach(moves)
        elsewhere = self.folder("Elsewhere")
        moves.move(self.touch(elsewhere, "Heat (1995).mkv"), NOW + 1)
        await self.until(lambda: moves.asked == 1)
        await asyncio.sleep(0.05)
        self.assertEqual(self.time.sleeps, [])  # not a library folder: no check
        arrival = self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv")
        moves.move(arrival, NOW + 2)
        await self.until(lambda: self.has("folder:movie:329865"))
        self.assertEqual(self.time.sleeps, [MOVE_CHECK_S])
        # Case and separators don't matter: Windows paths.
        heat = self.touch(self.films, "Heat (1995)/Heat (1995).mkv")
        moves.move(Path(str(heat).upper().replace("\\", "/")), NOW + 3)
        await self.until(lambda: self.has("folder:movie:949"))

    async def test_plex_connecting_during_a_check_stores_both(self) -> None:
        # Was "Plex connected stops the folder library": now a check under way when Plex gets
        # connected (and its check stores) stores the folders' result in their own tables, and
        # Plex's result stays as Plex stored it.
        service = self.make()
        await service.switch_on([str(self.films)], [])
        self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv")
        plex_scan = PlexScan(films=frozenset({(7, "tt0000007")}))
        real_read = scan.read_folder

        async def read_while_plex_connects(*args: Any, **kwargs: Any) -> Any:
            # Plex gets connected and its check stores while the folders are being read.
            await self.settings.update(plex_token="token")
            self.plex.notify()
            await self.db.plex.replace(plex_scan, datetime.now(UTC))
            return await real_read(*args, **kwargs)

        with mock.patch.object(folder_service, "read_folder", read_while_plex_connects):
            await service.check()
        self.assertEqual(await self.db.plex.contents(), (frozenset(), plex_scan.films))  # Plex's
        self.assertEqual(await self.keys(), {"folder:movie:438631", "folder:movie:329865"})
        self.assertIn(
            "Arrival (2016).mkv", {Path(p).name for p in await self.db.library_files.all()}
        )
        self.assertTrue(service.status.on)

    async def test_plex_disconnected_tells_the_box_while_the_folders_are_off(self) -> None:
        # The folders were never switched on: Settings' box still has to leave its "Plex is
        # your library" line when Plex goes (the owner saw it stay, 2026-11-16).
        await self.settings.update(plex_token="token")
        service = self.make()
        heard: list[bool] = []
        service.subscribe(lambda: heard.append(service.status.on))
        await self.settings.update(plex_token="")
        self.plex.notify()
        self.assertEqual(heard, [False])
        await asyncio.sleep(0.05)
        self.assertEqual(await self.db.library_files.all(), {})  # nothing read: still off

    async def test_stop_using_folders_clears_what_was_read(self) -> None:
        service = self.make()
        await service.switch_on([str(self.films)], [])
        notified = self.library.notified
        before = (self.dune.stat().st_size, self.dune.stat().st_mtime)
        await service.switch_off()
        self.assertFalse(self.settings.current.folder_library_on)
        self.assertEqual(await self.db.library_files.all(), {})
        self.assertEqual(await self.keys(), set())
        self.assertEqual(await self.db.folders.contents(), (frozenset(), frozenset()))
        self.assertIsNone(await self.db.folders.checked_at())
        self.assertEqual(self.library.notified, notified + 1)
        # Nothing on disk changed.
        self.assertTrue(self.dune.exists())
        self.assertEqual((self.dune.stat().st_size, self.dune.stat().st_mtime), before)
        self.assertEqual(sorted(p.name for p in self.films.rglob("*")),
                         ["Dune (2021)", "Dune (2021) - 1080p.mkv"])
        status = service.status
        self.assertEqual(
            (status.on, status.films, status.shows, status.episodes, status.unrecognised,
             status.unreachable, status.checked_at),
            (False, 0, 0, 0, (), (), None),
        )

    async def test_status_counts_and_unreachable(self) -> None:
        self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv")
        odd = self.touch(self.films, "zzqx.mkv")
        self.touch(self.series, "Severance (2022)/Season 01/Severance - S01E01.mkv")
        self.touch(self.series, "Severance (2022)/Season 01/Severance - S01E02.mkv")
        gone = str(self.root / "Gone")
        service = self.make()
        seen: list[bool] = []
        service.subscribe(lambda: seen.append(service.status.checking))
        await service.switch_on([str(self.films), gone], [str(self.series)])
        status = service.status
        self.assertEqual(
            (status.on, status.checking, status.films, status.shows, status.episodes),
            (True, False, 2, 1, 2),
        )
        self.assertEqual(status.unrecognised, (str(odd),))
        self.assertEqual(status.unreachable, (gone,))
        self.assertEqual(status.checked_at, datetime.fromtimestamp(NOW, UTC))
        self.assertIn(True, seen)  # checking started …
        self.assertEqual(seen[-1], False)  # … and finished
        # A new service (the next start) shows the stored counts before its first check.
        later = self.make()
        await later.load()
        self.assertEqual(
            (later.status.films, later.status.shows, later.status.episodes,
             later.status.checked_at),
            (2, 1, 2, datetime.fromtimestamp(NOW, UTC)),
        )


class FolderRulesTests(ServiceTestCase):
    async def test_an_unreached_folder_keeps_its_titles(self) -> None:
        nas = self.folder("NAS")
        heat = self.touch(nas, "Heat (1995)/Heat (1995).mkv")
        service = self.make()
        await service.switch_on([str(self.films), str(nas)], [])
        self.assertEqual(await self.keys(), {"folder:movie:438631", "folder:movie:949"})
        searched, detailed = len(self.tmdb.searched), len(self.tmdb.detailed)
        notified = self.library.notified
        asleep = self.root / "NAS asleep"
        nas.rename(asleep)  # the NAS sleeps
        await service.check()
        self.assertEqual(await self.keys(), {"folder:movie:438631", "folder:movie:949"})
        self.assertIn(str(heat), await self.db.library_files.all())
        _episodes, films = await self.db.folders.contents()
        self.assertIn((949, "tt0113277"), films)
        self.assertEqual(service.status.unreachable, (str(nas),))
        self.assertEqual(service.status.films, 2)
        # Nothing asked again, and nothing changed: the pages aren't reloaded.
        self.assertEqual((len(self.tmdb.searched), len(self.tmdb.detailed)), (searched, detailed))
        self.assertEqual(self.library.notified, notified)

    async def test_a_folder_still_being_read_is_not_read_again(self) -> None:
        slow = self.folder("Slow")
        self.touch(slow, "Heat (1995)/Heat (1995).mkv")
        release = threading.Event()
        self.addCleanup(release.set)
        walks: list[str] = []
        real_walk = scan._walk

        def walk(root: str, progress: Any = None) -> list[tuple[str, int, float]]:
            walks.append(root)
            if root == str(slow):
                release.wait(30)  # a NAS that took the connection and doesn't answer
            return real_walk(root, progress)

        service = self.make(read_timeout_s=0.1)
        with mock.patch.object(scan, "_walk", walk):
            await service.switch_on([str(self.films), str(slow)], [])
            self.assertEqual(service.status.unreachable, (str(slow),))
            self.assertEqual(service.status.slow, ())  # it never answered
            await service.check()  # its first read still runs: not read again
            self.assertEqual(walks.count(str(slow)), 1)
            self.assertEqual(service.status.unreachable, (str(slow),))
            release.set()
            # Its read ends at last: a check uses it at once (finding 1), not in 6 hours.
            await self.until(lambda: self.has("folder:movie:949"))
        self.assertEqual(walks.count(str(slow)), 1)
        self.assertEqual((service.status.unreachable, service.status.slow), ((), ()))

    def answering_then_stalling(self, slow: Path, release: threading.Event) -> Any:
        real_walk = scan._walk
        self.walks: list[str] = []

        def walk(root: str, progress: Any = None) -> list[tuple[str, int, float]]:
            self.walks.append(root)
            if root == str(slow):
                if progress is not None:
                    progress.step()  # the share answered …
                release.wait(30)  # … then went quiet a while (a cold NAS spinning up)
            return real_walk(root, progress)

        return mock.patch.object(scan, "_walk", walk)

    async def test_a_slow_folder_is_slow_not_unreachable(self) -> None:
        slow = self.folder("Slow")
        self.touch(slow, "Heat (1995)/Heat (1995).mkv")
        release = threading.Event()
        self.addCleanup(release.set)
        service = self.make(read_timeout_s=0.1)
        with self.answering_then_stalling(slow, release):
            # Only that folder: not switched on, but never "can't be reached" (it answered).
            self.assertEqual(await service.switch_on([str(slow)], []), "slow")
            self.assertFalse(service.status.on)
            self.assertEqual((service.status.unreachable, service.status.slow), ((), (str(slow),)))
            release.set()
            await self.until(lambda: not self.walks or str(slow) not in service._reading)
            await asyncio.sleep(0.05)
            # Asked again: the read that ended meanwhile is used, not read once more.
            self.assertEqual(await service.switch_on([str(slow)], []), "on")
        self.assertEqual(self.walks.count(str(slow)), 1)
        self.assertIn("folder:movie:949", await self.keys())
        self.assertEqual(service.status.slow, ())

    async def test_a_slow_folder_beside_others_is_read_again_once_done(self) -> None:
        slow = self.folder("Slow")
        self.touch(slow, "Heat (1995)/Heat (1995).mkv")
        release = threading.Event()
        self.addCleanup(release.set)
        service = self.make(read_timeout_s=0.1)
        with self.answering_then_stalling(slow, release):
            self.assertEqual(await service.switch_on([str(self.films), str(slow)], []), "on")
            self.assertEqual((service.status.unreachable, service.status.slow), ((), (str(slow),)))
            self.assertNotIn("folder:movie:949", await self.keys())
            release.set()
            await self.until(lambda: self.has("folder:movie:949"))
        self.assertEqual(service.status.slow, ())
        self.assertEqual(self.walks.count(str(slow)), 1)

    async def test_files_still_arriving_bring_a_check_soon(self) -> None:
        self.assertEqual(QUIET_S, watch.QUIET_S)  # MediaForge's
        self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv", mtime=NOW - 10)
        self.touch(self.films, "Heat (1995)/Heat.1995.mkv.!qB")  # never counts, never waited for
        service = self.make()
        await service.switch_on([str(self.films)], [])
        self.assertNotIn("folder:movie:329865", await self.keys())
        await self.until(lambda: self.has("folder:movie:329865"))
        self.assertEqual(self.time.sleeps, [QUIET_S + ARRIVING_MARGIN_S])

    async def test_nested_and_removed_folders(self) -> None:
        inner = self.folder("Films/4K")
        heat = self.touch(inner, "Heat (1995) - 2160p.mkv")
        reads: list[str] = []
        real_read = scan.read_folder

        async def read(folder: str, *args: Any, **kwargs: Any) -> Any:
            reads.append(folder)
            return await real_read(folder, *args, **kwargs)

        service = self.make()
        with mock.patch.object(folder_service, "read_folder", read):
            # The same folder twice and one inside it: the outer one is read, once.
            await service.switch_on(
                [str(self.films), str(inner), str(self.films).upper() + "\\"], []
            )
        self.assertEqual(reads, [str(self.films)])
        self.assertEqual(set(await self.db.library_files.all()), {str(self.dune), str(heat)})
        self.assertEqual(await self.keys(), {"folder:movie:438631", "folder:movie:949"})
        # The outer folder taken out in Settings: its rows go, the inner one's stay.
        await service.switch_on([str(inner)], [])
        self.assertEqual(set(await self.db.library_files.all()), {str(heat)})
        self.assertEqual(await self.keys(), {"folder:movie:949"})
        # The same folder written differently: one row per file, never two.
        await service.switch_on([str(self.films).upper()], [])
        rows = await self.db.library_files.all()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(path.startswith(str(self.films).upper()) for path in rows))


class FixRoundOneTests(ServiceTestCase):
    async def test_a_series_folder_inside_a_film_folder_is_read_as_series(self) -> None:
        videos = self.folder("Videos")
        film = self.touch(videos, "Heat (1995)/Heat (1995).mkv")
        shows = self.folder("Videos/Series")
        one = self.touch(shows, "Severance (2022)/Season 01/Severance - S01E01.mkv")
        two = self.touch(shows, "Severance (2022)/Season 01/Severance - S01E02.mkv")
        service = self.make()
        await service.switch_on([str(videos)], [str(shows)])
        self.assertEqual(await self.keys(), {"folder:movie:949", "folder:show:95396"})
        rows = await self.db.library_files.all()
        self.assertEqual(set(rows), {str(film), str(one), str(two)})
        self.assertEqual((rows[str(one)].kind, rows[str(one)].season), ("episode", 1))
        self.assertEqual(rows[str(film)].kind, "movie")
        status = service.status
        self.assertEqual((status.films, status.shows, status.episodes), (1, 1, 2))
        self.assertEqual(status.unrecognised, ())
        # The inner series folder asleep: the outer read doesn't take its files, and its rows stay.
        searched = len(self.tmdb.searched)
        real_read = scan.read_folder

        async def read(folder: str, *args: Any, **kwargs: Any) -> Any:
            if folder == str(shows):
                return scan.FolderRead(folder=folder, reached=False, files=())
            return await real_read(folder, *args, **kwargs)

        with mock.patch.object(folder_service, "read_folder", read):
            await service.check()
        self.assertEqual(set(await self.db.library_files.all()), {str(film), str(one), str(two)})
        self.assertEqual(await self.keys(), {"folder:movie:949", "folder:show:95396"})
        self.assertEqual(service.status.unreachable, (str(shows),))
        self.assertEqual(len(self.tmdb.searched), searched)

    async def test_a_film_folder_inside_a_series_folder_is_read_as_films(self) -> None:
        tv = self.folder("TV")
        episode = self.touch(tv, "Severance (2022)/Season 01/Severance - S01E01.mkv")
        films = self.folder("TV/Films")
        heat = self.touch(films, "Heat (1995)/Heat (1995).mkv")
        service = self.make()
        await service.switch_on([str(films)], [str(tv)])
        self.assertEqual(await self.keys(), {"folder:movie:949", "folder:show:95396"})
        rows = await self.db.library_files.all()
        self.assertEqual((rows[str(heat)].kind, rows[str(episode)].kind), ("movie", "episode"))

    async def test_no_folder_reached_stays_off(self) -> None:
        gone = [str(self.root / "Gone"), str(self.root / "Away")]
        service = self.make()
        outcome = await service.switch_on([gone[0]], [gone[1]])
        self.assertEqual(outcome, "unreachable")
        current = self.settings.current
        self.assertFalse(current.folder_library_on)
        self.assertEqual((current.film_folders, current.series_folders), ((), ()))
        self.assertEqual(await self.keys(), set())
        self.assertEqual(await self.db.library_files.all(), {})
        self.assertEqual(self.library.notified, 0)
        self.assertFalse(service.status.on)
        self.assertEqual(service.status.unreachable, tuple(gone))
        # One reached is enough.
        self.assertEqual(await service.switch_on([str(self.films), gone[0]], []), "on")
        self.assertEqual(service.status.unreachable, (gone[0],))

    async def test_a_file_from_the_future_is_settled(self) -> None:
        self.touch(self.films, "Heat (1995)/Heat (1995).mkv", mtime=NOW + 3600)  # a NAS clock off
        self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv", mtime=NOW + 5)
        service = self.make()
        await service.switch_on([str(self.films)], [])
        keys = await self.keys()
        self.assertIn("folder:movie:949", keys)
        self.assertNotIn("folder:movie:329865", keys)  # just written: still arriving
        await self.until(lambda: self.has("folder:movie:329865"))
        self.assertEqual(self.time.sleeps, [QUIET_S + ARRIVING_MARGIN_S])  # once, not forever

    async def test_plex_storing_while_the_folders_store_keeps_both(self) -> None:
        # Was "Plex connecting while storing writes nothing over Plex": Plex connects and its
        # check stores between the folders' rows and their tables; each keeps its own.
        service = self.make()
        await service.switch_on([str(self.films)], [])
        self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv")
        real = self.db.library_files.replace_folder

        async def replace_folder(folder: str, rows: Any) -> None:
            await real(folder, rows)
            await self.settings.update(plex_token="token")
            await self.db.plex.replace(PlexScan(films=frozenset({(7, None)})), datetime.now(UTC))

        with mock.patch.object(self.db.library_files, "replace_folder", replace_folder):
            await service.check()
        self.assertEqual(await self.db.plex.contents(), (frozenset(), frozenset({(7, None)})))
        self.assertEqual(await self.keys(), {"folder:movie:438631", "folder:movie:329865"})

    async def test_switch_on_waits_for_a_check_under_way(self) -> None:
        service = self.make()
        await service.switch_on([str(self.films)], [])
        self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv")
        release = threading.Event()
        self.addCleanup(release.set)
        lock = threading.Lock()
        walking = [0, 0]  # now, most at once
        walks: list[str] = []
        real_walk = scan._walk

        def walk(root: str, progress: Any = None) -> list[tuple[str, int, float]]:
            with lock:
                walks.append(root)
                walking[0] += 1
                walking[1] = max(walking)
            try:
                if len(walks) == 1:
                    release.wait(10)  # the check's read, slow
                return real_walk(root, progress)
            finally:
                with lock:
                    walking[0] -= 1

        searched = len(self.tmdb.searched)
        with mock.patch.object(scan, "_walk", walk):
            check = asyncio.create_task(service.check())
            await self.until(lambda: len(walks) == 1)
            switching = asyncio.create_task(service.switch_on([str(self.films)], []))
            await asyncio.sleep(0.2)
            self.assertEqual(len(walks), 1)  # switch_on waits for the check's read
            release.set()
            self.assertEqual(await switching, "on")
            await check
        self.assertEqual((len(walks), walking[1]), (2, 1))  # one after the other, never at once
        self.assertEqual(len(self.tmdb.searched), searched + 1)  # only switch_on matched (Arrival)
        self.assertIn("folder:movie:329865", await self.keys())


class FinalReviewTests(ServiceTestCase):
    """The final review's finding 2 (finding 9's held marks went with two shelves: the
    folders' rows no longer leave when Plex comes or goes, BesidePlexTests)."""

    async def test_a_newly_followed_show_counts_at_once(self) -> None:
        self.touch(self.series, "Severance (2022)/Season 01/Severance - S01E01.mkv")
        service = self.make()
        await service.switch_on([], [str(self.series)])
        self.assertEqual(await self.db.folders.contents(), (frozenset(), frozenset()))  # unfollowed
        searched, detailed = len(self.tmdb.searched), len(self.tmdb.detailed)
        # Followed (My shows, the Library page's Follow, an import): the show in the cache.
        await self.db.library.save_show(Show(id=44933, name="Severance"), [])
        self.library.followed(44933)
        await self.until(lambda: self.has_episode((44933, 1, 1)))
        self.assertEqual(self.time.sleeps, [0])  # a check at once, not in 6 hours
        # Cheap: nothing asked of TMDB again.
        self.assertEqual((len(self.tmdb.searched), len(self.tmdb.detailed)), (searched, detailed))
        # Switched off: a follow reads nothing.
        await service.switch_off()
        self.library.followed(1)
        await asyncio.sleep(0.05)
        self.assertEqual(self.time.sleeps, [0])

    async def has_episode(self, episode: tuple[int, int, int]) -> bool:
        return episode in (await self.db.folders.contents())[0]


class BesidePlexTests(ServiceTestCase):
    """Two shelves (docs/folder-library/2026-11-16-alongside-plex-design.md): the folders count
    beside Plex, in tables of their own (`db.folders`); neither source touches the other's."""

    PLEX_FILM = PlexTitle(kind="movie", key="123", title="Plex film", tmdb_id=7)
    LIBRARY = PlexLibrary(key="1", title="Films", kind="movie")

    async def connect_plex(self) -> PlexScan:
        """Plex connected and its check stored (a title, a film, its libraries)."""
        await self.settings.update(plex_token="token")
        plex_scan = PlexScan(
            films=frozenset({(7, "tt0000007")}), titles=(self.PLEX_FILM,),
            libraries=(self.LIBRARY,),
        )
        await self.db.plex.replace(plex_scan, datetime.fromtimestamp(OLD, UTC))
        self.plex.notify()
        return plex_scan

    async def plex_untouched(self, plex_scan: PlexScan) -> None:
        self.assertEqual(await self.db.plex.contents(), (frozenset(), plex_scan.films))
        self.assertEqual([title.key for title in await self.db.plex.titles()], ["123"])
        self.assertEqual(await self.db.plex.libraries(), (self.LIBRARY,))
        counts = await self.db.plex.counts()
        self.assertEqual((counts.films, counts.checked_at), (1, datetime.fromtimestamp(OLD, UTC)))

    async def test_plex_and_folder_checks_dont_touch_each_other(self) -> None:
        plex_scan = await self.connect_plex()
        service = self.make()
        self.assertEqual(await service.switch_on([str(self.films)], []), "on")
        self.touch(self.films, "Arrival (2016)/Arrival (2016).mkv")
        await service.check()
        both = {"folder:movie:438631", "folder:movie:329865"}
        self.assertEqual(await self.keys(), both)
        _episodes, films = await self.db.folders.contents()
        self.assertEqual(films, frozenset({(438631, "tt1160419"), (329865, "tt2543164")}))
        self.assertEqual(await self.db.folders.checked_at(), datetime.fromtimestamp(NOW, UTC))
        await self.plex_untouched(plex_scan)  # its rows, its libraries list, its check's time
        # Plex's next check replaces Plex's rows only.
        again = PlexScan(films=frozenset({(8, None)}), libraries=(self.LIBRARY,))
        await self.db.plex.replace(again, datetime.now(UTC))
        self.assertEqual(await self.db.plex.contents(), (frozenset(), again.films))
        self.assertEqual(await self.keys(), both)
        self.assertEqual((await self.db.folders.contents())[1], films)
        self.assertEqual(await self.db.folders.checked_at(), datetime.fromtimestamp(NOW, UTC))
        self.assertTrue(service.status.on)
        self.assertEqual(service.status.films, 2)

    async def test_switch_on_with_plex_reads_the_folders(self) -> None:
        self.assertNotIn("plex", get_args(SwitchOutcome))  # no more "saved behind Plex"
        plex_scan = await self.connect_plex()
        service = self.make()
        reads = mock.AsyncMock(wraps=scan.read_folder)
        with mock.patch.object(folder_service, "read_folder", reads):
            outcome = await service.switch_on([str(self.films)], [])
        self.assertEqual(outcome, "on")
        reads.assert_called_once()
        self.assertEqual(await self.keys(), {"folder:movie:438631"})
        rows = await self.db.library_files.all()
        self.assertEqual(set(rows), {str(self.dune)})
        self.assertTrue(rows[str(self.dune)].recognised)
        self.assertTrue(self.settings.current.folder_library_on)
        self.assertEqual((service.status.on, service.status.films), (True, 1))
        self.assertEqual(self.library.notified, 1)  # the pages count the folders from now on
        await self.plex_untouched(plex_scan)

    async def test_plex_connecting_or_going_leaves_the_folders(self) -> None:
        service = self.make()
        await service.switch_on([str(self.films)], [])

        async def folders() -> Any:
            return (
                await self.keys(), await self.db.folders.contents(),
                await self.db.folders.checked_at(), await self.db.library_files.all(),
            )

        stored = await folders()
        self.assertEqual(stored[0], {"folder:movie:438631"})
        notified = self.library.notified
        heard: list[bool] = []
        service.subscribe(lambda: heard.append(service.status.on))
        reads = mock.AsyncMock(wraps=scan.read_folder)
        with mock.patch.object(folder_service, "read_folder", reads):
            plex_scan = await self.connect_plex()
            await asyncio.sleep(0.05)
            self.assertEqual(await folders(), stored)
            await self.plex_untouched(plex_scan)
            self.assertTrue(service.status.on)
            # Disconnect (PlexService.disconnect): the token goes, Plex's tables are cleared,
            # then its listeners hear it.
            await self.settings.update(plex_token="")
            await self.db.plex.clear()
            self.plex.notify()
            await asyncio.sleep(0.05)
        self.assertEqual(await folders(), stored)
        self.assertEqual(await self.db.plex.contents(), (frozenset(), frozenset()))
        self.assertTrue(service.status.on)
        self.assertEqual(heard, [True, True])  # Settings' box hears both
        # Nothing to read again or to hold: the folders' rows never left.
        reads.assert_not_called()
        self.assertEqual(self.time.sleeps, [])
        self.assertEqual(self.library.notified, notified)
        self.assertFalse(hasattr(LibraryService, "hold_marks"))

    async def test_stop_using_folders_clears_only_the_folders(self) -> None:
        service = self.make()
        await service.switch_on([str(self.films)], [])
        plex_scan = await self.connect_plex()
        await service.switch_off()
        self.assertFalse(self.settings.current.folder_library_on)
        self.assertEqual(await self.db.library_files.all(), {})
        self.assertEqual(await self.keys(), set())
        self.assertEqual(await self.db.folders.contents(), (frozenset(), frozenset()))
        self.assertIsNone(await self.db.folders.checked_at())
        self.assertFalse(service.status.on)
        await self.plex_untouched(plex_scan)

    async def test_stop_using_folders_forgets_before_it_saves_the_switch(self) -> None:
        # Saved first, a clear that fails (or the app dying in between) would leave rows that
        # count as had beside Plex while the folders are off, with no "Stop using" to clear them.
        service = self.make()
        await service.switch_on([str(self.films)], [])
        await self.connect_plex()
        with mock.patch.object(self.db.folders, "clear", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                await service.switch_off()
        self.assertTrue(self.settings.current.folder_library_on)  # still on: the next check refills
        self.assertTrue(service.status.on)
        await service.switch_off()
        self.assertFalse(self.settings.current.folder_library_on)
        self.assertEqual(await self.keys(), set())

    async def test_rows_left_while_off_are_forgotten_at_start(self) -> None:
        # An older run switched off and died before the clear: the next start heals it.
        service = self.make()
        await service.switch_on([str(self.films)], [])
        plex_scan = await self.connect_plex()
        await self.settings.update(folder_library_on=False)  # saved, never cleared
        self.assertEqual(await self.keys(), {"folder:movie:438631"})
        notified = self.library.notified
        again = self.make()
        with self.assertLogs("app.services.folder_library.service", "INFO"):
            await again.load()
        self.assertEqual(await self.keys(), set())
        self.assertEqual(await self.db.folders.contents(), (frozenset(), frozenset()))
        self.assertIsNone(await self.db.folders.checked_at())
        self.assertEqual(await self.db.library_files.all(), {})
        self.assertFalse(again.status.on)
        self.assertEqual(self.library.notified, notified + 1)  # the pages lose the leftovers' ✓
        await self.plex_untouched(plex_scan)
        # Nothing left: the next start leaves it be.
        await self.make().load()
        self.assertEqual(self.library.notified, notified + 1)

    async def test_a_user_who_had_it_on_is_read_at_start(self) -> None:
        # 3.17.0's "Use these folders" with Plex connected saved the switch and read nothing
        # (outcome "plex"): after the update the folders count, read at the start.
        plex_scan = await self.connect_plex()
        await self.settings.update(folder_library_on=True, film_folders=(str(self.films),))
        self.assertEqual(await self.db.library_files.all(), {})
        service = self.make()
        run = asyncio.create_task(service.run())
        self.addCleanup(run.cancel)
        await self.until(lambda: self.time.sleeps == [CHECK_EVERY_S])
        self.assertEqual(await self.keys(), {"folder:movie:438631"})
        self.assertIn(str(self.dune), await self.db.library_files.all())
        self.assertEqual((service.status.on, service.status.films), (True, 1))
        self.assertEqual(self.library.notified, 1)
        await self.plex_untouched(plex_scan)


class SettingsClosedTests(ServiceTestCase):
    """Settings closed while a switch runs (Task 5's fix round): the box's task is cancelled, the
    switch isn't."""

    async def test_a_switch_on_survives_its_caller(self) -> None:
        service = self.make()
        storing, go_on = asyncio.Event(), asyncio.Event()
        real_replace = self.db.folders.replace

        async def replace(scan_: PlexScan, when: datetime) -> None:
            storing.set()
            await go_on.wait()
            await real_replace(scan_, when)

        with mock.patch.object(self.db.folders, "replace", replace):
            caller = asyncio.create_task(service.switch_on([str(self.films)], []))
            await self.until(storing.is_set)
            caller.cancel()  # the Settings box went
            await asyncio.wait({caller})
            self.assertTrue(caller.cancelled())
            go_on.set()
            await self.until(lambda: not service.status.checking)
        self.assertTrue(self.settings.current.folder_library_on)
        self.assertEqual(self.settings.current.film_folders, (str(self.films),))
        self.assertEqual(await self.keys(), {"folder:movie:438631"})
        self.assertEqual(self.library.notified, 1)  # the pages heard it

    async def test_a_failed_switch_without_its_caller_is_logged_by_type(self) -> None:
        service = self.make()
        storing = asyncio.Event()

        async def replace(scan_: PlexScan, when: datetime) -> None:
            storing.set()
            await asyncio.sleep(0.05)
            raise RuntimeError(f"disk trouble under {self.films}")

        with mock.patch.object(self.db.folders, "replace", replace):
            with self.assertLogs(folder_service.log, "INFO") as logs:
                caller = asyncio.create_task(service.switch_on([str(self.films)], []))
                await self.until(storing.is_set)
                caller.cancel()  # Settings closed
                await asyncio.wait({caller})
                await self.until(lambda: not service._switches)
        failed = [line for line in logs.output if "failed" in line]
        self.assertEqual(len(failed), 1)
        self.assertIn("RuntimeError", failed[0])
        self.assertFalse(any(str(self.films) in line for line in logs.output))
        self.assertFalse(self.settings.current.folder_library_on)

    async def test_a_failed_check_is_logged_once_by_its_type(self) -> None:
        # The final review's finding 7: never a traceback (its text may hold a path), never twice.
        service = self.make()
        await service.switch_on([str(self.films)], [])

        async def failing() -> None:
            raise RuntimeError(f"disk trouble under {self.films}")

        with mock.patch.object(service, "_check_once", failing):
            for start in ("run", "soon"):
                with self.subTest(start=start):
                    with self.assertLogs(folder_service.log, "INFO") as logs:
                        if start == "run":
                            run = asyncio.create_task(service.run())
                            await self.until(lambda: CHECK_EVERY_S in self.time.sleeps)
                            run.cancel()
                        else:
                            service.check_soon()
                            await self.until(lambda: service._soon is None)
                            await asyncio.sleep(0.05)
                    said = [record for record in logs.records if "failed" in record.getMessage()]
                    self.assertEqual(
                        [record.getMessage() for record in said],
                        ["Folder library check failed (RuntimeError)"],
                    )
                    self.assertIsNone(said[0].exc_info)  # no traceback
                    self.assertFalse(any(str(self.films) in line for line in logs.output))

    async def test_an_unreachable_change_while_on_keeps_the_folders_in_use(self) -> None:
        service = self.make()
        self.assertEqual(await service.switch_on([str(self.films)], []), "on")
        gone = str(self.root / "Gone")
        self.assertEqual(await service.switch_on([gone], []), "unreachable")
        self.assertEqual(service.status.unreachable, ())  # about the folders in use
        self.assertTrue(service.status.on)
        self.assertEqual(self.settings.current.film_folders, (str(self.films),))

    async def test_a_switch_off_survives_its_caller(self) -> None:
        service = self.make()
        await service.switch_on([str(self.films)], [])
        clearing, go_on = asyncio.Event(), asyncio.Event()
        real_clear = self.db.library_files.clear

        async def clear() -> None:
            clearing.set()
            await go_on.wait()
            await real_clear()

        with mock.patch.object(self.db.library_files, "clear", clear):
            caller = asyncio.create_task(service.switch_off())
            await self.until(clearing.is_set)
            caller.cancel()
            await asyncio.wait({caller})
            go_on.set()
            await self.until(lambda: not service._switches)
        self.assertFalse(self.settings.current.folder_library_on)
        self.assertEqual(await self.db.library_files.all(), {})
        self.assertEqual(await self.keys(), set())
        self.assertEqual(self.library.notified, 2)


if __name__ == "__main__":
    unittest.main()
