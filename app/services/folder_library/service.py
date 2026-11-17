"""The folder library's service (docs/folder-library/): when the user's film and series folders
are read, what a check stores, switching on and off, and the status Settings and the Library
page show.

- **When:** at the start (`run`, once the loop runs; the start never waits for a folder), every
  CHECK_EVERY_S, MOVE_CHECK_S after MediaForge moved a file into one of the folders (`attach`,
  as app/services/plex_scans.py hears Forge's moves), QUIET_S after a check found files still
  being written, at once when a folder's read past its limit ends at last (its result used),
  at once when a show is newly followed (`LibraryService.on_followed`: its episodes count only
  for a show in the cache, so its files count from that check on, not 6 hours later) and when
  asked (`check_soon`, "Check now"). Only while the folders are on (`folders_on`: switched on
  with a folder set, with or without Plex); otherwise nothing is read.
- **A check** reads every folder at once, each on its own and limited in time (scan.py). A
  folder that can't be reached, that answers but whose read went past its limit (`slow`), or
  whose last read still runs in its thread, keeps its stored
  rows: they go into the matching as they are (`from_row`), since `FolderRepository.replace`
  replaces all the folders' rows. The same folder set twice, or one inside another of its kind,
  is read once (the outer one). Then `match_files` (with the last check's titles, so TMDB and
  TVMaze aren't asked again) and the store: `replace_folder` per folder read, the rows below no
  folder set dropped, `FolderRepository.replace` with the whole scan. The pages reload
  (`LibraryService.notify_changed`) when what is had or the titles changed.
- **Beside Plex** (two shelves, docs/folder-library/2026-11-16-alongside-plex-design.md): the
  folders' result has tables of its own (`folder_*`, `db.folders`); Plex's check and the folder
  check each replace only their own rows, so Plex connecting or going changes nothing here (its
  status listeners only tell Settings' box). Right before storing, the switch is checked again:
  a check under way when the folders got switched off, or switched on with other folders, stores
  nothing.
- **Switching on** stores the first read's result before `folder_library_on` is saved, so the
  marks never stand over empty tables; **switching off** clears `library_files` and the
  folders' tables, never Plex's. Never a file on disk is touched.

Logs give counts only, never a path, a file name or a title.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.database import FolderRepository, LibraryFileRepository
from app.models.folder_library import FolderLibraryStatus, LibraryFile, SwitchOutcome
from app.models.plex import PlexTitle
from app.services.folder_library.match import FolderMatch, match_files
from app.services.folder_library.scan import (
    READ_TIMEOUT_S,
    FolderKind,
    FolderRead,
    FoundFile,
    LongReads,
    from_row,
    read_folder,
)
from app.services.library import LibraryService
from app.services.library_source import folders_on
from app.services.plex import plex_connected
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient

if TYPE_CHECKING:
    from app.forge.models.moves import MoveRecord

log = logging.getLogger(__name__)

CHECK_EVERY_S = 6 * 3600
# MediaForge's QUIET_S (app/forge/core/watch.py), not imported: AirGrabber's start loads no Forge
# code. A file changed less than this long ago is still arriving and doesn't count yet.
QUIET_S = 600.0
ARRIVING_MARGIN_S = 30.0  # files still arriving: the check after they settled, with this to spare
MOVE_CHECK_S = 60.0  # after MediaForge moved something into a folder: its run's other files too

Listener = Callable[[], None]


class PlexListeners(Protocol):
    """PlexService's status listeners: called after a connect and after a disconnect, among
    other times."""

    def subscribe(self, listener: Listener) -> Callable[[], None]: ...


class MoveRecords(Protocol):
    """What this reads of Forge's move records (ForgeMoveService)."""

    async def moved_after(self, when: float) -> list[MoveRecord]: ...

    def subscribe(self, listener: Listener) -> Callable[[], None]: ...


@dataclass(frozen=True, slots=True)
class _Folder:
    path: str  # as set in Settings (stripped): what the status names
    kind: FolderKind
    key: str  # how it compares: Windows paths ignore case and the separator's direction

    @property
    def read_path(self) -> str:
        return os.path.normpath(self.path)


@dataclass(frozen=True, slots=True)
class _Outcome:
    """One read of the folders, matched; not stored yet."""

    folders: tuple[_Folder, ...]  # every folder read (`_owner` places each file in one)
    reached: tuple[_Folder, ...]
    unreachable: tuple[_Folder, ...]
    slow: tuple[_Folder, ...]  # not reached either, but they answered
    stored: dict[str, LibraryFile]  # the rows before this check
    match: FolderMatch
    arriving: int
    now: float


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.strip()))


def _below(path_key: str, folder_key: str) -> bool:
    """Whether `path_key` lies below `folder_key` (both `_key`s)."""
    return path_key.startswith(folder_key.rstrip("\\/") + os.sep)


def _deepest(path_key: str, folders: Iterable[_Folder]) -> _Folder | None:
    """The deepest of `folders` that `path_key` lies below (None: below none)."""
    found: _Folder | None = None
    for folder in folders:
        if _below(path_key, folder.key) and (found is None or len(folder.key) > len(found.key)):
            found = folder
    return found


def _folders(films: Iterable[str], series: Iterable[str]) -> tuple[_Folder, ...]:
    """The folders to read: blanks and repeats left out (the same folder set twice, of either
    kind: the first one), and a folder whose nearest enclosing folder set is of its own kind
    (that one's read covers it). A folder inside one of the other kind is read on its own: a
    path belongs to the deepest folder set above it, whatever its kind (`_owner`)."""
    every: list[_Folder] = []
    kinds: tuple[tuple[FolderKind, Iterable[str]], ...] = (("movie", films), ("show", series))
    for kind, folders in kinds:
        for folder in folders:
            if not folder.strip():
                continue
            key = _key(folder)
            if any(key == other.key for other in every):
                continue
            every.append(_Folder(folder.strip(), kind, key))
    kept: list[_Folder] = []
    for one in every:
        nearest = _deepest(one.key, every)
        if nearest is None or nearest.kind != one.kind:
            kept.append(one)
    return tuple(kept)


def _owner(path: str, folders: Iterable[_Folder]) -> _Folder | None:
    """The folder a file belongs to: the deepest of the folders read that holds it."""
    return _deepest(_key(path), folders)


def _retrieved(task: asyncio.Task[Any]) -> None:
    """A task of the service's own ended: its error is taken (never "Task exception was never
    retrieved" once its caller has gone) and logged by its type only (its text may hold a path).
    A caller still waiting gets it too."""
    if not task.cancelled() and (error := task.exception()) is not None:
        what = task.get_name().removeprefix("folder-library-")  # "check", "switch-on", …
        log.info("Folder library %s failed (%s)", what, type(error).__name__)


def _cleaned(folders: Sequence[str]) -> tuple[str, ...]:
    return tuple(folder.strip() for folder in folders if folder.strip())


def _counted(
    titles: Iterable[PlexTitle],
    *,
    unrecognised: Iterable[str],
    unreachable: Iterable[str],
    slow: Iterable[str] = (),
    checked_at: datetime | None,
) -> FolderLibraryStatus:
    titles = list(titles)
    shows = [title for title in titles if title.kind == "show"]
    return FolderLibraryStatus(
        films=sum(1 for title in titles if title.kind == "movie"),
        shows=len(shows),
        episodes=sum(title.episodes or 0 for title in shows),
        unrecognised=tuple(sorted(unrecognised)),
        unreachable=tuple(unreachable),
        slow=tuple(slow),
        checked_at=checked_at,
    )


class FolderLibraryService:
    """The folder library's checks, its switch and its status (see the module docstring)."""

    def __init__(
        self,
        settings: SettingsService,
        files: LibraryFileRepository,
        folders: FolderRepository,
        tmdb: TMDBClient,
        library: LibraryService,
        *,
        clock: Callable[[], float] = time.time,
        quiet_s: float = QUIET_S,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        read_timeout_s: float = READ_TIMEOUT_S,
        plex: PlexListeners | None = None,
    ) -> None:
        """`clock`: POSIX seconds (a file's change time is held against it); `sleep`: the waits
        between checks (tests move both). `folders`: the folders' own tables (`db.folders`).
        `plex`: PlexService, whose connect / disconnect this passes on to its listeners."""
        self._settings = settings
        self._files = files
        self._folders = folders
        self._tmdb = tmdb
        self._library = library
        self._clock = clock
        self._quiet = quiet_s
        self._sleep = sleep
        self._read_timeout = read_timeout_s
        self._listeners: list[Listener] = []
        self._found = FolderLibraryStatus()  # the last check's (`on` / `checking` are live)
        self._working = 0  # checks and switch-ons under way
        self._run: asyncio.Task[None] | None = None
        self._again = False  # asked for during a check: one more after it
        self._soon: asyncio.Task[None] | None = None  # `check_soon`'s
        self._soon_at = 0.0  # the loop's time it is due
        self._generation = 0  # moved on by a switch: older reads store nothing
        self._store_lock = asyncio.Lock()
        # Reads that went past their limit: one ending at last brings a check.
        self._reading = LongReads(on_ended=self._long_read_ended)
        self._closed = False
        self._moves: MoveRecords | None = None
        self._moves_since = 0.0
        self._moves_task: asyncio.Task[None] | None = None
        self._moves_again = False
        # switch_on / switch_off run as tasks of their own: a caller that goes (Settings closed
        # mid-read) doesn't stop them half-way.
        self._switches: set[asyncio.Task[Any]] = set()
        self._unsubscribe: list[Callable[[], None]] = []
        self._plex_was = plex_connected(settings.current)
        self._titles: dict[str, PlexTitle] = {}  # the last stored ones (`prior` for matching)
        if plex is not None:
            self._unsubscribe.append(plex.subscribe(self._plex_changed))
        self._unsubscribe.append(library.on_followed(self._followed))

    # -- status --------------------------------------------------------------------------------

    @property
    def status(self) -> FolderLibraryStatus:
        return self._found.model_copy(
            update={
                "on": folders_on(self._settings.current),
                "checking": self._working > 0,
            }
        )

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call `listener` whenever the status may have changed (a check starts or ends, a
        switch, Plex connected or disconnected). Returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("Folder library listener %r failed", listener)

    def _work(self, step: int) -> None:
        self._working += step
        self._notify()

    def _on(self) -> bool:
        return folders_on(self._settings.current)

    async def load(self) -> None:
        """The stored result as the status, before the first check (the start). Off, what an
        older run left of the folders (cut off between switching off and forgetting) is
        forgotten: it would count as had beside Plex."""
        if not self._on():
            await self._forget_leftovers()
            return
        titles = await self._folders.titles()
        self._titles = {title.key: title for title in titles}
        rows = await self._files.all()
        self._found = _counted(
            titles,
            unrecognised=(path for path, row in rows.items() if not row.recognised),
            unreachable=(),
            checked_at=await self._folders.checked_at(),
        )
        self._notify()

    async def _forget_leftovers(self) -> None:
        if not await self._folders.stored() and not await self._files.all():
            return
        async with self._store_lock:
            if self._on():  # switched on meanwhile: those rows are its
                return
            await self._files.clear()
            await self._folders.clear()
        log.info("Folder library is off: what an earlier run left of it forgotten")
        self._library.notify_changed()

    # -- when ----------------------------------------------------------------------------------

    async def run(self) -> None:
        """At the start (spawned once the loop runs: the start waits for nothing here), then
        every CHECK_EVERY_S, until cancelled."""
        try:
            await self.load()
        except Exception as exc:  # by its type only: a message could hold a path
            log.warning("Reading the folder library's last result failed (%s)", type(exc).__name__)
        while True:
            if self._on():
                with contextlib.suppress(Exception):  # logged by its type (`_retrieved`)
                    await self.check()
            await self._sleep(CHECK_EVERY_S)

    def check_soon(self, delay_s: float = 0) -> None:
        """One check about `delay_s` seconds from now; one waiting at a time (the earliest).
        Nothing while the folders are off (no timer either)."""
        if not self._on():
            return
        loop = asyncio.get_running_loop()
        due = loop.time() + delay_s
        if self._soon is not None and not self._soon.done():
            if self._soon_at <= due:
                return
            self._soon.cancel()
        self._soon_at = due
        self._soon = loop.create_task(self._check_after(delay_s), name="folder-library-soon")

    async def _check_after(self, delay_s: float) -> None:
        await self._sleep(delay_s)
        self._soon = None  # from here on a new `check_soon` waits on its own
        with contextlib.suppress(Exception):  # logged by its type (`_retrieved`)
            await self.check()

    def _cancel_soon(self) -> None:
        if self._soon is not None:
            self._soon.cancel()
            self._soon = None

    async def check(self) -> None:
        """Read the folders and store what they hold (see the module docstring); nothing while
        they are off. One at a time: a call during a check waits for it, and one
        more check follows it."""
        if not self._on():
            return
        run = self._run
        if run is None or run.done():
            run = asyncio.get_running_loop().create_task(
                self._checks(), name="folder-library-check"
            )
            run.add_done_callback(_retrieved)
            self._run = run
        else:
            self._again = True
        await asyncio.wait({run})  # a caller cancelled doesn't cancel the check
        if not run.cancelled() and (error := run.exception()) is not None:
            raise error

    async def _checks(self) -> None:
        self._work(1)
        try:
            while True:
                self._again = False
                await self._check_once()
                if not self._again:
                    return
        finally:
            self._work(-1)

    async def _check_once(self) -> None:
        settings = self._settings.current
        if not folders_on(settings):
            return
        generation = self._generation
        folders = (settings.film_folders, settings.series_folders)
        outcome = await self._read(*folders, generation=generation)
        if outcome is None:
            log.info("Folder library check: the library changed meanwhile, nothing stored")
            return
        async with self._store_lock:
            current = self._settings.current
            if not self._may_store(generation) or not folders_on(current):
                log.info("Folder library check: the library changed meanwhile, nothing stored")
                return
            if (current.film_folders, current.series_folders) != folders:
                log.info("Folder library check: the folders changed meanwhile, read again")
                self._again = True
                return
            changed = await self._store(outcome, generation)
        if changed is None:
            return
        if changed:
            self._library.notify_changed()
        if outcome.arriving:
            self.check_soon(self._quiet + ARRIVING_MARGIN_S)

    # -- reading and storing --------------------------------------------------------------------

    def _may_store(self, generation: int) -> bool:
        """No switch since `generation` was taken."""
        return generation == self._generation

    async def _read(
        self, films: Sequence[str], series: Sequence[str], *, generation: int
    ) -> _Outcome | None:
        """Read and match the folders; None when overtaken (`_may_store`) before matching."""
        folders = _folders(films, series)
        stored = await self._files.all()
        prior = dict(self._titles)
        prior.update((title.key, title) for title in await self._folders.titles())
        now = self._clock()
        owners = {path: _owner(path, folders) for path in stored}
        shares = [
            {path: row for path, row in stored.items() if owners[path] == folder}
            for folder in folders
        ]
        reads: list[FolderRead] = list(
            await asyncio.gather(
                *(
                    read_folder(
                        folder.read_path,
                        folder.kind,
                        known=known,
                        quiet_s=self._quiet,
                        now=now,
                        timeout_s=self._read_timeout,
                        busy=self._reading,
                    )
                    for folder, known in zip(folders, shares, strict=True)
                )
            )
        )
        files: dict[str, FoundFile] = {}
        reached: list[_Folder] = []
        unreachable: list[_Folder] = []
        slow: list[_Folder] = []
        arriving = 0
        for folder, known, read in zip(folders, shares, reads, strict=True):
            if read.reached:
                reached.append(folder)
                arriving += read.arriving
                # What lies in a deeper folder set (of either kind) is that folder's.
                found = [file for file in read.files if _owner(file.path, folders) == folder]
            else:  # its last result stays
                (slow if read.slow else unreachable).append(folder)
                found = [from_row(row) for row in known.values()]
            for file in found:
                files.setdefault(file.path, file)
        if not self._may_store(generation):
            return None  # no TMDB / TVMaze asked for a read that won't be stored
        match = await match_files(
            list(files.values()),
            tmdb=self._tmdb,
            library=self._library,
            cached=stored,
            titles=prior,
        )
        log.info(
            "Folder library check: %d of %d folders read (%d slow), %d titles, %d files not "
            "recognised, %d still arriving",
            len(reached), len(folders), len(slow), len(match.scan.titles), len(match.unrecognised),
            arriving,
        )
        return _Outcome(
            folders=folders,
            reached=tuple(reached),
            unreachable=tuple(unreachable),
            slow=tuple(slow),
            stored=stored,
            match=match,
            arriving=arriving,
            now=now,
        )

    async def _store(self, outcome: _Outcome, generation: int) -> bool | None:
        """Store a read (under the store lock): whether what is had or the titles changed; None
        when overtaken right before the tables (`_may_store`: a switch came first).
        Each file belongs to the deepest folder set above it (`_owner`): a folder read gives
        its own files' rows, an unreached one keeps its stored rows."""
        match = outcome.match
        before = await self._folders.contents()
        before_titles = {title.key: title for title in await self._folders.titles()}
        reached = set(outcome.reached)
        final: dict[str, LibraryFile] = {}
        for row in match.rows:
            if _owner(row.path, outcome.folders) in reached:
                final[row.path] = row
        for path, row in outcome.stored.items():
            owner = _owner(path, outcome.folders)
            if owner is not None and owner not in reached:
                final.setdefault(path, row)
        for folder in outcome.reached:
            # Everything below it, a deeper folder's rows too: replace_folder clears them all.
            rows = [row for path, row in final.items() if _below(_key(path), folder.key)]
            await self._files.replace_folder(folder.read_path, rows)
        stale = [path for path in outcome.stored if path not in final]
        if stale:  # below no folder set (any more), or under a folder's other spelling
            await self._files.remove(stale)
        if not self._may_store(generation):
            log.info("Folder library: a switch came first, the tables left alone")
            return None
        when = datetime.fromtimestamp(outcome.now, UTC)
        await self._folders.replace(match.scan, when)
        self._titles = {title.key: title for title in match.scan.titles}
        self._found = _counted(
            match.scan.titles,
            unrecognised=match.unrecognised,
            unreachable=(folder.path for folder in outcome.unreachable),
            slow=(folder.path for folder in outcome.slow),
            checked_at=when,
        )
        return before != (match.scan.episodes, match.scan.films) or before_titles != {
            title.key: title for title in match.scan.titles
        }

    # -- the switch ----------------------------------------------------------------------------

    def _own[T](self, coro: Coroutine[Any, Any, T], name: str) -> asyncio.Task[T]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._switches.add(task)
        task.add_done_callback(self._switches.discard)
        task.add_done_callback(_retrieved)
        return task

    async def switch_on(self, films: Sequence[str], series: Sequence[str]) -> SwitchOutcome:
        """"Use these folders": read them, store what they hold, then switch on (in that order:
        the marks never stand over empty tables). It runs as the service's own task: a caller
        cancelled meanwhile (Settings closed) doesn't stop it; only `aclose` does. What came of
        it (SwitchOutcome):
        - "on": switched on (some folders may be unreachable: `status.unreachable`);
        - "unreachable": no folder could be reached; nothing stored, nothing saved, it stays as
          it was; `status.unreachable` names the folders;
        - "slow": none was read in time, but some answered (`status.slow`): as "unreachable",
          but asking again soon uses what their reads found meanwhile;
        - "overtaken": a later switch came first; nothing stored;
        - "off": no folder given: switched off (`switch_off`).
        Plex connected or not, the folders are read the same way (they count beside Plex).
        A check under way is waited for first (it stores nothing): never two reads at once."""
        task = self._own(self._switch_on(films, series), "folder-library-switch-on")
        await asyncio.wait({task})  # a caller cancelled doesn't cancel the switch
        return task.result()

    async def _switch_on(self, films: Sequence[str], series: Sequence[str]) -> SwitchOutcome:
        film_folders, series_folders = _cleaned(films), _cleaned(series)
        if not film_folders and not series_folders:
            await self._settings.update(film_folders=(), series_folders=())
            await self._switch_off()
            return "off"
        self._generation += 1  # a check under way stores nothing
        generation = self._generation
        self._cancel_soon()
        self._work(1)
        try:
            await self._wait_for_check()
            outcome = await self._read(film_folders, series_folders, generation=generation)
            if outcome is None:
                log.info("Folder library: switching on was overtaken, nothing stored")
                return "overtaken"
            if not outcome.reached:
                if not self._on():
                    # Off: the folders asked for. On, `unreachable` stays about the folders in
                    # use (the rejected change isn't them).
                    self._found = self._found.model_copy(
                        update={
                            "unreachable": tuple(f.path for f in outcome.unreachable),
                            "slow": tuple(f.path for f in outcome.slow),
                        }
                    )
                log.info(
                    "Folder library not switched on: none of its %d folders was read in time "
                    "(%d slow)",
                    len(outcome.unreachable) + len(outcome.slow), len(outcome.slow),
                )
                return "slow" if outcome.slow else "unreachable"
            async with self._store_lock:
                if not self._may_store(generation):
                    log.info("Folder library: switching on was overtaken, nothing stored")
                    return "overtaken"
                if await self._store(outcome, generation) is None:
                    return "overtaken"
                await self._settings.update(
                    folder_library_on=True,
                    film_folders=film_folders,
                    series_folders=series_folders,
                )
            log.info(
                "Folder library switched on: %d film and %d series folders, %d not reached",
                len(film_folders), len(series_folders),
                len(outcome.unreachable) + len(outcome.slow),
            )
            self._library.notify_changed()  # the pages show what you have and miss from now on
            if outcome.arriving:
                self.check_soon(self._quiet + ARRIVING_MARGIN_S)
            return "on"
        finally:
            self._work(-1)

    async def _wait_for_check(self) -> None:
        """Let a check under way end (overtaken, it stops before matching and stores nothing)."""
        self._again = False
        run = self._run
        if run is not None and not run.done():
            await asyncio.wait({run})

    async def switch_off(self) -> None:
        """"Stop using these folders": what was read is forgotten (`library_files` and the
        folders' own tables; Plex's stay), the folders stay set. Nothing on disk is touched.
        Its own task, as `switch_on`'s."""
        task = self._own(self._switch_off(), "folder-library-switch-off")
        await asyncio.wait({task})
        task.result()

    async def _switch_off(self) -> None:
        self._generation += 1  # a check under way stores nothing
        self._cancel_soon()
        async with self._store_lock:
            # Forgotten first, then the switch saved: rows outliving it would count as had
            # beside Plex (a failed or cut-off clear leaves it on, and the next check refills).
            await self._files.clear()
            await self._folders.clear()
            await self._settings.update(folder_library_on=False)
        self._found = FolderLibraryStatus()
        self._titles = {}  # what was read is forgotten
        log.info("Folder library switched off, what was read forgotten")
        self._notify()
        self._library.notify_changed()

    # -- Plex ----------------------------------------------------------------------------------

    def _plex_changed(self) -> None:
        """Plex connected or disconnected: nothing of the folders' changes (their own tables),
        but the status's listeners hear it (Settings' box words itself by Plex)."""
        connected = plex_connected(self._settings.current)
        was, self._plex_was = self._plex_was, connected
        if connected != was:
            log.info("Plex %s: the folder library goes on as it was",
                     "connected" if connected else "disconnected")
            self._notify()

    def _followed(self) -> None:
        """A show newly followed: a check now, so its files' episodes count (only a show in the
        cache gets them). Cheap: unchanged files aren't read again, titles come from `prior`."""
        if not self._closed and self._on():
            log.info("A show was followed: the folder library is checked")
            self.check_soon()

    def _long_read_ended(self) -> None:
        """A folder's read past its limit ended at last: a check now uses it (not in 6 hours);
        switched off, the next "Use these folders" does."""
        if self._closed:
            return
        self._notify()
        if self._on():
            self.check_soon()

    # -- MediaForge's moves --------------------------------------------------------------------

    def attach(self, moves: MoveRecords) -> None:
        """Hear Forge's moves from now on: one into a library folder brings a check
        MOVE_CHECK_S later."""
        if self._moves is not None:
            return
        self._moves = moves
        self._moves_since = self._clock()
        self._unsubscribe.append(moves.subscribe(self._moved))

    def _moved(self) -> None:
        if self._moves_task is None or self._moves_task.done():
            self._moves_task = asyncio.get_running_loop().create_task(
                self._read_moves(), name="folder-library-moves"
            )
        else:
            self._moves_again = True

    async def _read_moves(self) -> None:
        while True:
            self._moves_again = False
            await self._look_at_moves()
            if not self._moves_again:
                return

    async def _look_at_moves(self) -> None:
        moves = self._moves
        if moves is None:
            return
        try:
            records = await moves.moved_after(self._moves_since)
        except Exception as exc:  # the type only: a message could hold a path
            log.warning("Reading MediaForge's moves failed (%s)", type(exc).__name__)
            return
        if not records:
            return
        self._moves_since = max(record.moved_at.timestamp() for record in records)
        settings = self._settings.current
        if not folders_on(settings):
            return
        folders = _folders(settings.film_folders, settings.series_folders)
        inside = sum(
            1
            for record in records
            if any(_below(_key(str(record.target)), folder.key) for folder in folders)
        )
        if inside:
            log.info("MediaForge moved %d files into the folder library: a check soon", inside)
            self.check_soon(MOVE_CHECK_S)

    async def aclose(self) -> None:
        self._closed = True
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        tasks = [
            task
            for task in (self._soon, self._moves_task, self._run, *self._switches)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
