"""Forge's move records (app/forge/core/moves.py, app/forge/db/moves.py, schema v3): a rename into
a library folder recorded in its journal step and taken out by undo, the readers AirGrabber uses
(`moves_from`, `subscribe`), and a MediaForge 1.3 database migrating.

Every file lives in the test's own temporary folder; TMDB is a fake."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import time
from pathlib import Path
from typing import cast
from unittest import mock

import aiosqlite
import httpx

from app.config import AppPaths
from app.forge.core.executor import Executor, ProgressEvent, RunResult
from app.forge.core.moves import ForgeMoveService, library_kinds, library_roots
from app.forge.core.undo import UndoService
from app.forge.db import moves as moves_db
from app.forge.db.database import Database
from app.forge.db.history import HistoryRepo
from app.forge.db.moves import MoveRepository
from app.forge.models.history import OpKind, RunSource
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.forge.models.moves import MOVES_KEPT_S, MoveNote
from app.forge.models.plan import Plan, PlannedFile
from app.forge.models.settings import ForgeSettings
from app.forge.services import ForgeServices
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.core.journal_base import FakePosters
from tests.forge.core.test_auto_renamer import Clock, RenamerTestCase
from tests.forge.db.base import ForgeDbTestCase
from tests.forge.db.test_database import MEDIAFORGE_1_3_SCHEMA

GOLD_RUSH = 34634  # made-up TMDB ids
DUNE = 438631


def episode(title: str, season: int, number: int, tmdb_id: int | None) -> ResolvedMedia:
    parsed = ParsedMedia(
        source_name="e.mkv", kind=MediaKind.EPISODE, title=title, season=season, episode=number
    )
    return ResolvedMedia(
        parsed=parsed,
        title=title,
        year=2010,
        episode_title=None,
        match_state=MatchState.CONFIDENT,
        tmdb_id=tmdb_id,
    )


def film(title: str, tmdb_id: int | None) -> ResolvedMedia:
    parsed = ParsedMedia(source_name="m.mkv", kind=MediaKind.MOVIE, title=title, year=2021)
    return ResolvedMedia(
        parsed=parsed,
        title=title,
        year=2021,
        episode_title=None,
        match_state=MatchState.CONFIDENT,
        tmdb_id=tmdb_id,
    )


def write(path: Path, data: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class MovesTestCase(ForgeDbTestCase):
    """A forge.db with the move records, a journal that reports to them, and three folders:
    Downloads, Series and Movies (the library)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.downloads = self.tmp / "Downloads"
        self.series = self.tmp / "Series"
        self.movies = self.tmp / "Movies"
        for folder in (self.downloads, self.series, self.movies):
            folder.mkdir()
        self.settings = ForgeSettings(
            watch_folders=[str(self.downloads)],
            series_folder=str(self.series),
            movies_folder=str(self.movies),
        )
        self.media: dict[int, ResolvedMedia] = {}
        self.moves = ForgeMoveService(MoveRepository(self.db), settings=lambda: self.settings)
        self.heard = 0
        self.moves.subscribe(self._heard)
        self.history = HistoryRepo(self.db, on_moves_change=self.moves.changed)

    def _heard(self) -> None:
        self.heard += 1

    async def rename(self, *moves: tuple[Path, Path]) -> RunResult:
        """Run a plan that renames each (source, target), through an executor with the move
        records; `self.media` is what the caller's queue knows of each (by item id, from 1)."""
        files = tuple(
            PlannedFile(item_id=i, source=src, target=dst)
            for i, (src, dst) in enumerate(moves, start=1)
        )
        executor = Executor(self.history, FakePosters(), None, moves=self.moves)
        events: list[ProgressEvent] = []
        return await executor.run(
            Plan(files=files),
            source=RunSource.AUTOMATIC,
            preset_name="Series (Folder)",
            on_progress=events.append,
            cancel=asyncio.Event(),
            media=self.media,
        )

    async def count(self) -> int:
        return cast(int, await self.scalar("SELECT COUNT(*) FROM moves"))


# --- the library folders -------------------------------------------------------------------------


class FoldersTest(ForgeTestCase):
    def test_roots_and_the_kinds_they_hold(self) -> None:
        settings = ForgeSettings(series_folder=r"D:\Lib", movies_folder=r"D:\Lib\Movies")
        roots = library_roots(settings)
        self.assertEqual(
            roots,
            [
                (Path(r"D:\Lib"), frozenset({MediaKind.EPISODE})),
                (Path(r"D:\Lib\Movies"), frozenset({MediaKind.MOVIE})),
            ],
        )
        # the deepest library folder decides; Windows paths compare without case
        place = library_kinds(Path(r"d:\lib\MOVIES\Dune (2021)\Dune (2021).mkv"), roots)
        self.assertEqual(place, roots[1])
        self.assertEqual(library_kinds(Path(r"D:\Lib\Gold Rush\x.mkv"), roots), roots[0])
        self.assertIsNone(library_kinds(Path(r"D:\Library2\x.mkv"), roots))  # whole names only

    def test_one_folder_for_both_holds_both_kinds(self) -> None:
        settings = ForgeSettings(series_folder=r"\\nas\video", movies_folder="\\\\NAS\\Video\\")
        [(_root, kinds)] = library_roots(settings)
        self.assertEqual(kinds, {MediaKind.EPISODE, MediaKind.MOVIE})
        self.assertEqual(library_roots(ForgeSettings()), [])


# --- recorded in the journal step, taken out by undo ---------------------------------------------


class JournalTest(MovesTestCase):
    async def test_a_move_into_the_library_is_recorded_with_the_queues_ids(self) -> None:
        src = write(self.downloads / "Gold.Rush.S17E01.mkv", b"1234567")
        dst = self.series / "Gold Rush (2010)" / "Gold Rush (2010) - S17E01.mkv"
        self.media[1] = episode("Gold Rush", 17, 1, GOLD_RUSH)
        result = await self.rename((src, dst))
        self.assertEqual(result.renamed, 1)

        [move] = await self.moves.moves_from(self.downloads)
        self.assertEqual((move.source, move.target, move.run_id), (src, dst, result.run_id))
        self.assertEqual((move.kind, move.tmdb_id), (MediaKind.EPISODE, GOLD_RUSH))
        self.assertEqual(self.heard, 1)

    async def test_the_record_shares_the_renames_transaction(self) -> None:
        src = write(self.downloads / "Dune.2021.mkv")
        dst = self.movies / "Dune (2021).mkv"
        op = await self.history.record_pending(
            await self.history.start_run(RunSource.APP, None), OpKind.RENAME, src, dst
        )
        note = self.moves.note(PlannedFile(item_id=1, source=src, target=dst), film("Dune", DUNE))
        self.assertEqual(note, MoveNote(source=src, target=dst, kind=MediaKind.MOVIE, tmdb_id=DUNE))
        with mock.patch(
            "app.forge.db.history.record_move", side_effect=sqlite3.OperationalError("disk")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await self.history.mark_done(op, move=note)
        [pending] = await self.history.pending_operations()  # nothing half written
        self.assertEqual(pending.id, op)
        self.assertEqual(await self.count(), 0)
        self.assertEqual(self.heard, 0)

    async def test_a_move_elsewhere_records_nothing(self) -> None:
        self.media[1] = film("Dune", DUNE)
        await self.rename((write(self.downloads / "Dune.2021.mkv"), self.tmp / "Other" / "D.mkv"))
        self.assertEqual(await self.count(), 0)
        self.assertEqual(self.heard, 0)

    async def test_undo_takes_the_move_record_out(self) -> None:
        src = write(self.downloads / "Gold.Rush.S17E02.mkv")
        dst = self.series / "Gold Rush (2010)" / "Gold Rush (2010) - S17E02.mkv"
        self.media[1] = episode("Gold Rush", 17, 2, GOLD_RUSH)
        result = await self.rename((src, dst))
        assert result.run_id is not None
        self.heard = 0

        undone = await UndoService(self.history).undo_run(result.run_id)
        self.assertEqual(undone.undone, 2)  # the file and the show's folder
        self.assertTrue(src.exists())
        self.assertEqual(await self.moves.moves_from(self.downloads), [])
        self.assertEqual(self.heard, 1)  # once: the folder's undo had no record

    async def test_a_skipped_undo_keeps_the_record(self) -> None:
        src = write(self.downloads / "Dune.2021.mkv")
        self.media[1] = film("Dune", DUNE)
        result = await self.rename((src, self.movies / "Dune (2021).mkv"))
        assert result.run_id is not None
        write(src)  # something else now has the old name
        with self.assertLogs("app.forge.core.undo", "WARNING"):
            undone = await UndoService(self.history).undo_run(result.run_id)
        self.assertEqual(undone.undone, 0)
        self.assertEqual(await self.count(), 1)

    async def test_without_the_queues_media_the_folder_tells_the_kind(self) -> None:
        src = write(self.downloads / "Dune.2021.1080p.mkv")
        dst = self.movies / "Dune (2021)" / "Dune (2021).mkv"
        await self.rename((src, dst))  # e.g. a caller without a queue
        [move] = await self.moves.moves_from(self.downloads)
        self.assertEqual((move.target, move.kind, move.tmdb_id), (dst, MediaKind.MOVIE, None))

    async def test_a_fault_in_the_note_never_stops_a_rename(self) -> None:
        src = write(self.downloads / "Dune.2021.mkv")
        with (
            mock.patch.object(self.moves, "note", side_effect=RuntimeError("bug")),
            self.assertLogs("app.forge.core.executor", "ERROR"),
        ):
            result = await self.rename((src, self.movies / "Dune (2021).mkv"))
        self.assertEqual(result.renamed, 1)
        self.assertEqual(await self.count(), 0)

    async def test_one_folder_for_both_needs_the_queues_kind(self) -> None:
        both = str(self.series)
        self.settings = ForgeSettings(series_folder=both, movies_folder=both)
        await self.rename((write(self.downloads / "a.mkv"), self.series / "a.mkv"))
        self.assertEqual(await self.count(), 0)  # nothing known: no record
        self.media[1] = film("Dune", DUNE)
        await self.rename((write(self.downloads / "b.mkv"), self.series / "Dune (2021).mkv"))
        [move] = await self.moves.moves_from(self.downloads)
        self.assertEqual((move.kind, move.tmdb_id), (MediaKind.MOVIE, DUNE))

    async def test_a_subtitle_moved_along_is_no_record_of_its_own(self) -> None:
        src = write(self.downloads / "Dune.2021.mkv")
        write(self.downloads / "Dune.2021.en.srt", b"sub")
        dst = self.movies / "Dune (2021)" / "Dune (2021).mkv"
        self.media[1] = film("Dune", DUNE)
        result = await self.rename((src, dst))
        self.assertEqual((result.renamed, result.subtitles), (1, 1))
        self.assertEqual((dst.parent / "Dune (2021).en.srt").read_bytes(), b"sub")
        [move] = await self.moves.moves_from(self.downloads)
        self.assertEqual(move.target, dst)


# --- reading -------------------------------------------------------------------------------------


class ReadersTest(MovesTestCase):
    async def test_moves_from_a_folder_and_only_below_it(self) -> None:
        self.media = {1: film("Dune", DUNE), 2: episode("Gold Rush", 1, 1, GOLD_RUSH)}
        other = self.tmp / "Downloads2"
        await self.rename(
            (write(self.downloads / "Sub" / "Dune.mkv"), self.movies / "Dune (2021).mkv"),
            (write(other / "Gold.mkv"), self.series / "Gold Rush - S01E01.mkv"),
        )
        [move] = await self.moves.moves_from(Path(str(self.downloads).upper()))
        self.assertEqual(move.target, self.movies / "Dune (2021).mkv")
        [move] = await self.moves.moves_from(other)
        self.assertEqual(move.tmdb_id, GOLD_RUSH)
        self.assertEqual(await self.moves.moves_from(self.tmp / "Down"), [])  # whole names only

    async def test_newest_first(self) -> None:
        self.media = {1: film("Dune", DUNE)}
        await self.rename((write(self.downloads / "a.mkv"), self.movies / "A.mkv"))
        await self.rename((write(self.downloads / "b.mkv"), self.movies / "B.mkv"))
        moves = await self.moves.moves_from(self.downloads)
        self.assertEqual([m.target.name for m in moves], ["B.mkv", "A.mkv"])

    async def test_listeners_hear_changes_until_they_unsubscribe(self) -> None:
        heard: list[str] = []

        def broken() -> None:
            raise RuntimeError("listener bug")

        self.moves.subscribe(broken)
        unsubscribe = self.moves.subscribe(lambda: heard.append("changed"))
        self.media[1] = film("Dune", DUNE)
        with self.assertLogs("app.forge.core.moves", "ERROR"):
            await self.rename((write(self.downloads / "a.mkv"), self.movies / "Dune.mkv"))
        self.assertEqual(heard, ["changed"])
        unsubscribe()
        unsubscribe()  # twice is harmless
        with self.assertLogs("app.forge.core.moves", "ERROR"):
            await self.rename((write(self.downloads / "b.mkv"), self.movies / "Dune 2.mkv"))
        self.assertEqual(heard, ["changed"])
        self.moves.close()  # Forge closing: nobody hears any more
        await self.rename((write(self.downloads / "c.mkv"), self.movies / "Dune 3.mkv"))
        self.assertEqual(self.heard, 2)

    async def test_move_records_are_kept_for_a_year(self) -> None:
        self.media[1] = film("Dune", DUNE)
        await self.rename((write(self.downloads / "a.mkv"), self.movies / "Dune.mkv"))
        a_year_on = time.time() + MOVES_KEPT_S + 60
        with mock.patch.object(moves_db, "time") as clock:  # only the records' clock
            clock.time.return_value = a_year_on
            await self.rename((write(self.downloads / "b.mkv"), self.movies / "Dune 2.mkv"))
        moves = await self.moves.moves_from(self.downloads)
        self.assertEqual([m.target.name for m in moves], ["Dune 2.mkv"])  # the old one went


# --- an old MediaForge's database ----------------------------------------------------------------


class MediaForgeFileTest(MovesTestCase):
    async def test_a_mediaforge_1_3_database_migrates_and_records_moves(self) -> None:
        path = self.tmp / "mediaforge.db"
        async with aiosqlite.connect(path) as conn:
            await conn.executescript(MEDIAFORGE_1_3_SCHEMA)
            await conn.execute(
                "INSERT INTO runs (id, started_at, finished_at, source, preset_name, kinds) "
                "VALUES (7, ?, ?, 'automatic', 'Series (Folder)', 'episode')",
                (time.time() - 60, time.time() - 50),
            )
            await conn.execute(
                "INSERT INTO operations (run_id, seq, kind, src, dst, state) "
                "VALUES (7, 1, 'rename', ?, ?, 'done')",
                (str(self.downloads / "Old.S01E01.mkv"), str(self.series / "Old - S01E01.mkv")),
            )
            await conn.commit()
        db = await Database.open(path)
        try:
            self.assertEqual((await db.fetch_one("PRAGMA user_version") or [0])[0], 4)
            moves = ForgeMoveService(MoveRepository(db), settings=lambda: self.settings)
            self.assertEqual(await moves.moves_from(self.downloads), [])  # no records before v3
            history = HistoryRepo(db, on_moves_change=moves.changed)
            [run] = await history.list_runs()
            self.assertEqual(run.id, 7)  # the history stays
            executor = Executor(history, FakePosters(), None, moves=moves)
            await executor.run(
                Plan(
                    files=(
                        PlannedFile(
                            item_id=1,
                            source=write(self.downloads / "Dune.2021.mkv"),
                            target=self.movies / "Dune (2021).mkv",
                        ),
                    )
                ),
                source=RunSource.APP,
                preset_name=None,
                on_progress=lambda event: None,
                cancel=asyncio.Event(),
                media={1: film("Dune", DUNE)},
            )
            [move] = await moves.moves_from(self.downloads)
            self.assertEqual((move.tmdb_id, move.target.name), (DUNE, "Dune (2021).mkv"))
        finally:
            await db.close()


# --- the callers hand their media over -----------------------------------------------------------


class RenamerMediaTest(RenamerTestCase):
    async def test_the_renamer_records_each_move_with_the_queues_ids(self) -> None:
        await self.use()
        moves = ForgeMoveService(MoveRepository(self.db), settings=lambda: self.settings.current)
        history = HistoryRepo(self.db, on_moves_change=moves.changed)
        executor = Executor(history, FakePosters(), None, moves=moves)
        self.parts = dataclasses.replace(self.parts, executor=executor)
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        [move] = await moves.moves_from(self.folders.downloads)
        self.assertEqual(move.target, self.folders.movies / "Dune (2021)" / "Dune (2021).mkv")
        self.assertEqual((move.kind, move.tmdb_id), (MediaKind.MOVIE, 8))  # the fake TMDB's id


# --- the wiring in ForgeServices -----------------------------------------------------------------


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


class ServicesWiringTest(ForgeAsyncTestCase):
    async def test_forge_services_record_a_move_with_its_media_and_undo(self) -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.addAsyncCleanup(http.aclose)
        services = await ForgeServices.create(
            AppPaths(self.data_dir),
            http,
            TMDBClient(http, api_key=lambda: ""),
            ImageCache(http, self.data_dir / "posters"),
            cast(SettingsService, mock.Mock(spec=SettingsService)),
        )
        self.addAsyncCleanup(services.aclose)
        movies = self.tmp / "Movies"
        await services.settings.update(movies_folder=str(movies))
        heard: list[str] = []
        services.moves.subscribe(lambda: heard.append("changed"))
        downloads = self.tmp / "Downloads"
        src = write(downloads / "Dune.2021.1080p.mkv")
        dst = movies / "Dune (2021).mkv"
        result = await services.executor.run(
            Plan(files=(PlannedFile(item_id=7, source=src, target=dst),)),
            source=RunSource.RIGHT_CLICK,
            preset_name="Movie (Flat)",
            on_progress=lambda event: None,
            cancel=asyncio.Event(),
            media={7: film("Dune", DUNE)},
        )
        [move] = await services.moves.moves_from(downloads)
        self.assertEqual((move.target, move.tmdb_id), (dst, DUNE))
        assert result.run_id is not None
        await services.undo.undo_run(result.run_id)
        self.assertEqual(await services.moves.moves_from(downloads), [])
        self.assertEqual(heard, ["changed", "changed"])
