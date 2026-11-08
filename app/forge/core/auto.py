"""Renaming on its own: when a finished download is sure enough, where it goes, and the
`AutoRenamer` that looks at the watched download folders every few minutes.

A download is only renamed without a click when its file name and TMDB agree on what it is
(`decide`); everything else waits in the Inbox with one plain reason. Shows go to the Series
library folder with the Series preset, movies to the Movies folder with the Movies preset
(`library_runs`); a file whose name is already taken there waits instead of becoming a "(1)" copy.
Everything runs on the one asyncio loop; only listing folders happens in a worker thread.

MediaForge's renamer took the whole `Services`; this one takes `RenamerParts` (the queue and a few
narrow protocols). It is off unless the settings say Manual or On: `start()` does nothing while
`automatic` is "off", the default, so a fresh Forge never watches a folder by itself (the old
MediaForge may still be watching the same folders: INTEGRATION.md 3.10's guard).

`blocked_by_old_install` (set by ForgeRuntime before the first start, from
app/forge/migration.py's `blocked_by_old_install()`, and by the import): while the old MediaForge
is still installed it starts with Windows and watches the same folders, so the stored Manual / On
is kept but the renamer stays idle as if Off (its status says why). app/forge/old_install.py's
`OldInstallRemover.lift_guard` clears it once the old install is gone and calls `reconfigure()`. The owner's own renames are never blocked.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

from app.forge.core import fsops
from app.forge.core.activity import ActivityFeed, say_to
from app.forge.core.airgrabber_folders import watched_folders
from app.forge.core.executor import ProgressEvent, RunResult
from app.forge.core.formatter import render
from app.forge.core.library import inside, library_problems
from app.forge.core.matcher import normalize_title
from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.core.planner import build_plan
from app.forge.core.queue import ItemStatus, Queue, QueueItem
from app.forge.core.watch import LOOK_INTERVAL_S, SECOND_LOOK_S, Ignored, Watcher, candidates
from app.forge.models.history import RunSource, RunSummary
from app.forge.models.media import MatchState, MediaKind, ResolvedMedia
from app.forge.models.plan import Plan, PlanInput, PlannedLeftovers
from app.forge.models.preset import DEFAULT_PRESETS, Preset, PresetOptions
from app.forge.models.settings import ForgeSettings
from app.i18n import N_, t, tn

log = logging.getLogger(__name__)

# Reasons a watched download waits (translated where they are handed to the queue).
ALREADY_THERE = N_("Already in the library.")
SAME_NAME = N_("Two downloads would get the same name.")
NOT_WATCHED = N_("It isn't in a watched folder any more.")
# The status while the old MediaForge is still installed (`blocked_by_old_install`).
OLD_INSTALL_STATUS = N_("Not watching until the old MediaForge is removed")
_NOT_SURE = {
    MatchState.UNCERTAIN: N_("TMDB isn't sure which one it is."),
    MatchState.NOT_FOUND: N_("Not found at TMDB."),
    MatchState.OFFLINE: N_("No TMDB key, or TMDB can't be reached."),
    MatchState.SKIPPED: N_("Skipped before."),
}
_DISAGREE = N_("The name and TMDB don't agree.")
_NO_NUMBERS = N_("No season or episode number in the name.")
_UNKNOWN_EPISODE = N_("TMDB doesn't know this episode.")
_NO_YEAR = N_("No year in the name: is it a movie?")


class Decision(NamedTuple):
    sure: bool
    reason: str  # empty when sure; translated
    match_problem: bool = False  # not sure because the match is missing or wrong


def decide(item: QueueItem) -> Decision:
    """Whether `item` may be renamed and moved without the owner, and if not, why."""
    media, parsed = item.media, item.parsed
    remembered = media.match_state is MatchState.REMEMBERED
    if not remembered and media.match_state is not MatchState.CONFIDENT:
        missing = media.match_state is MatchState.NOT_FOUND
        return Decision(False, t(_NOT_SURE[media.match_state]), match_problem=missing)
    if media.kind is MediaKind.EPISODE:  # a remembered show still needs a known episode
        if parsed.season is None or parsed.episode is None:
            return Decision(False, t(_NO_NUMBERS))
        if not media.episode_known:
            return Decision(False, t(_UNKNOWN_EPISODE))
    if remembered:
        return Decision(True, "")  # the owner confirmed this title before
    if media.kind is MediaKind.MOVIE:
        if parsed.year is None:
            return Decision(False, t(_NO_YEAR))
        if media.year != parsed.year:
            return Decision(False, t(_DISAGREE), match_problem=True)
    names = {normalize_title(media.title), normalize_title(media.original_title or "")}
    if normalize_title(parsed.title) not in names:
        return Decision(False, t(_DISAGREE), match_problem=True)
    return Decision(True, "")


class LibraryRun(NamedTuple):
    kind: MediaKind
    preset: Preset
    plan: Plan
    held: dict[int, str]  # item id → why it was left out of the plan (translated)


class NamingChoice(NamedTuple):
    """How the owner chose to name one watched download (the queue's band): its preset (for
    History), the naming rule (the preset's or a Custom one) and the options. Into the library
    folder of its kind all the same; clean-up stays the library's (`clean_downloads`)."""

    preset: Preset
    rule: str
    options: PresetOptions


def _preset(name: str, fallback: str, presets: Sequence[Preset]) -> Preset:
    for source in (presets, DEFAULT_PRESETS):
        for preset in source:
            if preset.name == name:
                return preset
    return next(p for p in DEFAULT_PRESETS if p.name == fallback)


def library_naming(
    item: QueueItem, settings: ForgeSettings, presets: Sequence[Preset]
) -> tuple[str, Path]:
    """The naming rule and library folder a watched download goes to (by its kind)."""
    if item.media.kind is MediaKind.EPISODE:
        preset = _preset(settings.series_preset, "Series (Folder)", presets)
        return preset.rule, Path(settings.series_folder)
    preset = _preset(settings.movies_preset, "Movie (Folder)", presets)
    return preset.rule, Path(settings.movies_folder)


def _other_copy(target: Path, exists: Callable[[Path], bool]) -> bool:
    """The same title is in the library already as another kind of video file."""
    return any(
        exists(target.with_suffix(ext))
        for ext in sorted(VIDEO_EXTENSIONS)
        if ext != target.suffix.lower()
    )


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _leftovers(
    items: Sequence[QueueItem], plan: Plan, held: dict[int, str]
) -> tuple[PlannedLeftovers, ...]:
    """The download folders to clear after the move: below (never equal to) the watched folder,
    and only those every file of which is moving now."""
    moving = {f.item_id for f in plan.files}
    folders: dict[str, tuple[Path, list[int]]] = {}
    kept: set[str] = set()
    for item in items:
        folder = item.path.parent
        key = os.path.normcase(str(folder))
        if item.id not in moving or item.id in held:
            kept.add(key)
        elif item.watched is not None and inside(folder, item.watched):
            title = normalize_title(item.parsed.title)
            named_for_it = f"{normalize_title(folder.name)} ".startswith(f"{title} ")
            if named_for_it and not _same(folder, item.watched):
                folders.setdefault(key, (folder, []))[1].append(item.id)
            else:
                kept.add(key)  # a downloader's category folder, or the watched folder itself
    return tuple(
        PlannedLeftovers(folder=folder, item_ids=tuple(ids))
        for key, (folder, ids) in folders.items()
        if key not in kept
    )


def _runs(
    items: Sequence[QueueItem],
    settings: ForgeSettings,
    presets: Sequence[Preset],
    exists: Callable[[Path], bool],
    *,
    hold_taken: bool,
    naming: Mapping[int, NamingChoice] | None = None,
    preferred: int | None = None,
) -> list[LibraryRun]:
    """One run per kind, or (with the owner's `naming` per item) one per kind and naming.
    Downloads wanting one name all wait, unless one of them is `preferred`: that one goes."""
    runs: list[LibraryRun] = []
    chosen = naming or {}
    for kind, folder, preset_name, fallback in (
        (MediaKind.EPISODE, settings.series_folder, settings.series_preset, "Series (Folder)"),
        (MediaKind.MOVIE, settings.movies_folder, settings.movies_preset, "Movie (Folder)"),
    ):
        mine = [i for i in items if i.media.kind is kind]
        if not mine:
            continue
        library = Path(folder)
        preset = _preset(preset_name, fallback, presets)
        default = NamingChoice(preset, preset.rule, preset.options)

        def naming_of(item: QueueItem, default: NamingChoice = default) -> NamingChoice:
            return chosen.get(item.id, default)

        held: dict[int, str] = {
            i.id: t(NOT_WATCHED) for i in mine if i.watched is None or not inside(i.path, i.watched)
        }
        wanted = {
            i.id: library.joinpath(*render(naming_of(i).rule, i.media, i.path.suffix).parts)
            for i in mine
        }
        if hold_taken:
            claims = Counter(os.path.normcase(str(w)) for n, w in wanted.items() if n not in held)
            for item_id, target in wanted.items():
                if item_id in held or item_id == preferred:
                    continue
                if claims[os.path.normcase(str(target))] > 1:
                    held[item_id] = t(SAME_NAME)
        groups: dict[tuple[str, str, str], list[QueueItem]] = {}
        for item in mine:
            how = naming_of(item)
            key = (how.preset.name, how.rule, how.options.model_dump_json())
            groups.setdefault(key, []).append(item)
        for group in groups.values():
            how = naming_of(group[0])
            options = how.options.model_copy(update={"clean_junk": False, "prune_empty": False})
            ids = {i.id for i in group}
            inputs = [
                PlanInput(
                    item_id=i.id, source=i.path, root=library, dropped_folder=None, media=i.media
                )
                for i in group
                if i.id not in held
            ]
            plan = build_plan(inputs, how.rule, options, exists)
            if hold_taken:
                for planned in plan.files:
                    target = wanted[planned.item_id]
                    if not _same(planned.target, target) or _other_copy(target, exists):
                        held[planned.item_id] = t(ALREADY_THERE)
                plan = plan.model_copy(
                    update={"files": tuple(f for f in plan.files if f.item_id not in held)}
                )
            if settings.clean_downloads:
                plan = plan.model_copy(update={"leftovers": _leftovers(group, plan, held)})
            for planned in plan.files:
                if not inside(planned.target, library):
                    # Never the path in the message: it can hold the user name.
                    raise ValueError(f"a planned file is outside the {kind.value} library folder")
            mine_held = {item_id: why for item_id, why in held.items() if item_id in ids}
            runs.append(LibraryRun(kind, how.preset, plan, mine_held))
    return runs


def library_runs(
    items: Sequence[QueueItem],
    settings: ForgeSettings,
    presets: Sequence[Preset],
    exists: Callable[[Path], bool] = fsops.exists,
    *,
    preferred: int | None = None,
) -> list[LibraryRun]:
    """One run per kind for renaming on its own; a taken name holds the file instead."""
    return _runs(items, settings, presets, exists, hold_taken=True, preferred=preferred)


def owner_runs(
    items: Sequence[QueueItem],
    settings: ForgeSettings,
    presets: Sequence[Preset],
    exists: Callable[[Path], bool] = fsops.exists,
    naming: Mapping[int, NamingChoice] | None = None,
) -> list[LibraryRun]:
    """The same for the owner's Rename click: a taken name gets a numbered copy, as in 1.x;
    `naming`: how the owner chose to name each download (its queue band), else the library's
    preset."""
    return _runs(items, settings, presets, exists, hold_taken=False, naming=naming)


# --- what the renamer needs ----------------------------------------------------------------------


class HistoryLike(Protocol):
    async def was_undone(self, src: Path) -> bool: ...
    async def list_runs(self) -> list[RunSummary]: ...


class PresetsLike(Protocol):
    async def list(self) -> builtins.list[Preset]: ...


class ExecutorLike(Protocol):
    async def run(
        self,
        plan: Plan,
        *,
        source: RunSource,
        preset_name: str | None,
        on_progress: Callable[[ProgressEvent], None],
        cancel: asyncio.Event,
        kinds: Collection[MediaKind] = ...,
        media: Mapping[int, ResolvedMedia] | None = ...,
    ) -> RunResult: ...


class TrailerToolsLike(Protocol):
    def tools_present(self) -> bool: ...


class TmdbStateLike(Protocol):
    def configured(self) -> bool: ...


class SettingsLike(Protocol):
    """ForgeSettingsService as far as the renamer needs it."""

    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...


class IgnoreSource(Protocol):
    """What the renamer needs of IgnoreService (app/forge/core/ignored.py)."""

    def snapshot(self) -> Ignored: ...

    async def forget_gone(self, watched: Iterable[Path]) -> int: ...


@dataclass(frozen=True)
class RenamerParts:
    """Everything the renamer works with (MediaForge passed its whole `Services`)."""

    queue: Queue
    history: HistoryLike
    presets: PresetsLike
    executor: ExecutorLike
    trailers: TrailerToolsLike | None  # None: no trailer tools, a preset's trailers are left out
    tmdb: TmdbStateLike
    settings: SettingsLike
    run_lock: asyncio.Lock  # shared with the owner's runs: one run at a time
    ignored: IgnoreSource | None = None  # what the owner ignored: never looked at


# --- the renamer ---------------------------------------------------------------------------------

SUMMARY_HOUR = 20  # the evening summary comes from 8 pm
UNDONE = N_("You undid this once; click Rename to move it again.")
SUMMARY_TITLE = "MediaForge"  # the notification's title: a name, not translated
_NEEDS_OWNER = frozenset({ItemStatus.NEEDS_CHOICE, ItemStatus.HELD, ItemStatus.ERROR})
_DONE = frozenset({ItemStatus.DONE, ItemStatus.SKIPPED})

type WatchState = Literal["off", "trial", "watching", "unreachable", "setup"]


class WatchStatus(NamedTuple):
    state: WatchState
    text: str  # translated


class LookResult(NamedTuple):
    added: int
    renamed: int
    held: int
    unreachable: list[Path]


TOOLS_NOT_THERE = N_("No trailer: the trailer tools aren't downloaded yet.")
SCAN_SECOND_LOOK_S = 3.0  # "look now": a file counts as finished from its second look on


def needs_message(new: int) -> tuple[str, str]:
    """The notification's title and text when `new` watched downloads start to wait."""
    title = tn("{n} file needs you", "{n} files need you", new)
    return title, t("Open MediaForge to pick or confirm them.")


def summary_message(episodes: int, movies: int) -> str:
    shows = tn("{n} episode", "{n} episodes", episodes)
    films = tn("{n} movie", "{n} movies", movies)
    if episodes and movies:
        return t("Today: {episodes} and {movies} renamed", episodes=shows, movies=films)
    return t("Today: {what} renamed", what=shows if episodes else films)


def summary_due(now: datetime, sent_on: str) -> bool:
    """From SUMMARY_HOUR, once a day (the caller also checks something was renamed)."""
    return now.hour >= SUMMARY_HOUR and sent_on != now.date().isoformat()


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _logged(path: Path) -> str:
    """A folder as the log names it: its last name only (full paths can hold the user name)."""
    return path.name or path.anchor


def _add(a: RunResult, b: RunResult) -> RunResult:
    return RunResult(
        run_id=b.run_id or a.run_id,
        renamed=a.renamed + b.renamed,
        skipped=a.skipped + b.skipped,
        failed=a.failed + b.failed,
        posters=a.posters + b.posters,
        backdrops=a.backdrops + b.backdrops,
        nfos=a.nfos + b.nfos,
        trailers=a.trailers + b.trailers,
        subtitles_fetched=a.subtitles_fetched + b.subtitles_fetched,
        cleaned=a.cleaned + b.cleaned,
        pruned=a.pruned + b.pruned,
        cancelled=a.cancelled or b.cancelled,
        errors=[*a.errors, *b.errors],
    )


class AutoRenamer:
    """Looks at the watched download folders and renames what it is sure about."""

    def __init__(
        self,
        parts: RenamerParts,
        *,
        now: Callable[[], float] = time.time,
        interval_s: float = LOOK_INTERVAL_S,
        second_look_s: float = SECOND_LOOK_S,
        scan_look_s: float = SCAN_SECOND_LOOK_S,
        activity: ActivityFeed | None = None,
    ) -> None:
        self._parts = parts
        self._say = say_to(activity)  # the Activity strip, next to each English log line
        self._closed = False  # `close()` was called: never starts again
        self._now = now
        self._interval = interval_s
        self._second_look = min(second_look_s, interval_s)
        self._scan_look = min(scan_look_s, interval_s)
        self._scan_looks = 0  # looks still to come for `scan_now`
        self._scan_found = LookResult(0, 0, 0, [])  # what they found so far
        self._scan_waiting: list[asyncio.Future[LookResult | None]] = []
        self._watcher = Watcher(now=now)
        self._retry: set[int] = set()  # held for a passing reason (the NAS was away)
        self._undone: set[int] = set()  # put back by an undo: never moved on its own again
        self._lost: dict[str, Path] = {}  # folders that couldn't be reached at the last look
        self._task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._cancel = asyncio.Event()
        self.on_status: Callable[[WatchStatus], None] = lambda status: None
        self.last_status: WatchStatus | None = None  # what `on_status` heard last
        self._status_listeners: list[Callable[[WatchStatus], None]] = []
        self.on_needs: Callable[[int], None] = lambda count: None
        self.notify: Callable[[str, str], None] = lambda title, text: None
        self.blocked_by_old_install = False  # see the module's notes

    def watch_status(self, listener: Callable[[WatchStatus], None]) -> Callable[[], None]:
        """Hear every status too, as `on_status` (Settings › Auto-rename while it is open); the
        result stops it."""
        self._status_listeners.append(listener)
        return lambda: self._status_listeners.remove(listener)

    def _tell_status(self, status: WatchStatus) -> None:
        self.last_status = status
        self.on_status(status)
        for listener in list(self._status_listeners):
            listener(status)

    @property
    def _settings(self) -> ForgeSettings:
        return self._parts.settings.current

    @property
    def _mode(self) -> Literal["off", "trial", "on"]:
        """The mode in effect: the stored one, or Off while the old MediaForge is installed."""
        return "off" if self.blocked_by_old_install else self._settings.automatic

    # --- running on a timer ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start looking, unless renaming on its own is off (the default) or it runs already."""
        if self.running or self._closed:
            return
        if self._mode == "off":
            self._tell_status(self.status([]))
            return
        self._stopping = False
        self._cancel = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        """Stop looking; a rename in progress stops after its current file."""
        self._stopping = True
        self._cancel.set()
        self._wake.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def close(self) -> None:
        """Stop for good (Forge is shutting down): after a stop or restart `reconfigure` left
        pending (Off then Manual just before quitting), so nothing starts looking afterwards. A
        rename in progress stops after its current file."""
        self._closed = True
        pending = [task for task in (self._stop_task, self._restart_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.stop()

    def look_soon(self) -> None:
        self._wake.set()

    async def scan_now(self) -> LookResult | None:
        """Look now and once more a moment later, so that what just finished comes in without
        waiting for the next timed look (a file counts as finished from its second look on).
        What both found; None when nothing is being watched. A call during a scan shares it."""
        if not self.running or self._stopping:
            return None
        if not self._scan_waiting:
            self._scan_looks = 2
            self._scan_found = LookResult(0, 0, 0, [])
        future: asyncio.Future[LookResult | None] = asyncio.get_running_loop().create_future()
        self._scan_waiting.append(future)
        self._wake.set()
        return await future

    def _scan_look_done(self, result: LookResult | None) -> None:
        """A look ended: count it for `scan_now`, and answer once both have."""
        if self._scan_looks == 0:
            return
        self._scan_looks -= 1
        found = self._scan_found
        if result is not None:
            found = LookResult(
                found.added + result.added,
                found.renamed + result.renamed,
                result.held,
                result.unreachable,
            )
        self._scan_found = found
        if self._scan_looks == 0:
            self._answer_scan(found)

    def _answer_scan(self, result: LookResult | None) -> None:
        self._scan_looks = 0
        waiting, self._scan_waiting = self._scan_waiting, []
        for future in waiting:
            if not future.done():
                future.set_result(result)

    def reconfigure(self) -> None:
        """The settings changed: start, stop, or look again at once."""
        if self._closed:
            return
        if self._mode == "off":
            if self.running and not self._stopping:
                self._stopping = True  # known at once, before the stop task gets its turn
                self._stop_task = asyncio.get_running_loop().create_task(self.stop())
            self._tell_status(self.status([]))
        elif self._stopping and self.running:  # Off was chosen a moment ago: start again after
            self._restart_task = asyncio.get_running_loop().create_task(self._start_after_stop())
        elif self.running:
            self.look_soon()
        else:
            self.start()

    async def _start_after_stop(self) -> None:
        if self._stop_task is not None:
            await asyncio.gather(self._stop_task, return_exceptions=True)
        if self._mode != "off":
            self.start()

    async def _loop(self) -> None:
        wait = self._second_look  # a file counts as finished from its second look on
        try:
            while not self._stopping:
                result: LookResult | None = None
                try:
                    result = await self.look()
                except Exception:
                    log.exception("Looking at the download folders failed")
                    self._say(t("Looking at the download folders failed"), level=logging.ERROR)
                self._scan_look_done(result)
                self._wake.clear()
                if self._stopping:
                    break
                try:
                    pause = self._scan_look if self._scan_looks else wait
                    await asyncio.wait_for(self._wake.wait(), pause)
                except TimeoutError:
                    pass
                wait = self._interval
        finally:
            self._answer_scan(None)  # stopped in between: nobody keeps waiting

    # --- one look ------------------------------------------------------------------------------

    def status(self, unreachable: Sequence[Path]) -> WatchStatus:
        settings = self._settings
        if self.blocked_by_old_install and settings.automatic != "off":
            return WatchStatus("off", t(OLD_INSTALL_STATUS))
        if settings.automatic == "off":
            return WatchStatus("off", t("Not watching"))
        if library_problems(settings):
            return WatchStatus("setup", t("Set up watching in Settings"))
        if unreachable:
            return WatchStatus("unreachable", t("Can't reach {folder}", folder=unreachable[0]))
        count = len(watched_folders(settings))
        if settings.automatic == "trial":
            text = tn("Manual: watching {n} folder", "Manual: watching {n} folders", count)
            return WatchStatus("trial", text)
        return WatchStatus("watching", tn("Watching {n} folder", "Watching {n} folders", count))

    async def look(self) -> LookResult:
        parts, settings = self._parts, self._settings
        if self._mode == "off" or library_problems(settings):
            self._tell_status(self.status([]))
            return LookResult(0, 0, 0, [])
        queue = parts.queue
        libraries = [Path(settings.series_folder), Path(settings.movies_folder)]
        lost_roots: list[Path] = []
        listed: list[tuple[Path, Path]] = []
        roots = [Path(folder) for folder in watched_folders(settings)]
        if parts.ignored is not None:
            await parts.ignored.forget_gone(roots)
        ignored = parts.ignored.snapshot() if parts.ignored is not None else None
        for root in roots:
            try:
                files = await asyncio.to_thread(candidates, root, ignored)
            except OSError:
                lost_roots.append(root)
                continue
            listed.extend((f, root) for f in files)
        lost_libraries = [
            lib for lib in libraries if not await asyncio.to_thread(fsops.is_dir, lib)
        ]
        self._note_reachability([*lost_roots, *lost_libraries])

        finished = set(map(_key, self._watcher.finished([f for f, _ in listed])))
        known = {_key(i.path) for i in queue.items}
        fresh = [
            (file, root)
            for file, root in listed
            if _key(file) in finished
            and _key(file) not in known
            and not any(inside(file, lib) for lib in libraries)  # never taken back out
        ]
        kept = set(settings.kept_back)
        undone = {
            _key(f) for f, _ in fresh if _key(f) in kept or await parts.history.was_undone(f)
        }
        new_ids = await queue.add_watched(fresh)
        offline: list[int] = []
        if parts.tmdb.configured():  # TMDB couldn't be reached before: ask again
            offline = [
                i.id
                for i in queue.items
                if i.watched is not None
                and i.status in (ItemStatus.READY, ItemStatus.HELD)
                and i.media.match_state is MatchState.OFFLINE
                and i.id not in self._undone
            ]
            queue.lookup_again(offline)
        await queue.wait_idle()

        held_before = self._needing_owner()
        for item_id in [*new_ids, *self._retry, *offline]:
            item = queue.get(item_id)
            if item is None or item.status in _DONE:
                continue
            if item_id in self._retry:
                item.status, item.message = ItemStatus.READY, ""
            if item_id in self._undone or _key(item.path) in undone:
                self._undone.add(item_id)
                queue.hold(item_id, t(UNDONE))
                continue
            verdict = decide(item)
            if not verdict.sure and item.status is not ItemStatus.NEEDS_CHOICE:
                queue.hold(item_id, verdict.reason, match_problem=verdict.match_problem)
        self._retry.clear()
        newly_kept = {_key(i.path) for i in queue.items if i.id in self._undone} - kept
        present_keys = {_key(f) for f, _ in listed}
        lost_keys = {_key(r) for r in lost_roots}
        still_kept = {
            k
            for k in kept | newly_kept
            if k in present_keys
            or any(k.startswith(r) for r in lost_keys)
            or await asyncio.to_thread(fsops.exists, Path(k))
        }  # forgotten once the file has left the download folder
        if still_kept != kept:
            await self._save_kept(still_kept)

        renamed = 0
        if settings.automatic == "on":
            movers = [
                i
                for i in queue.items
                if i.watched is not None
                and i.status is ItemStatus.READY
                and i.id not in self._undone
                and decide(i).sure
            ]
            if movers and lost_libraries:
                for item in movers:
                    queue.hold(item.id, t("Can't reach {folder}.", folder=lost_libraries[0]))
                    self._retry.add(item.id)
            elif movers:
                renamed = (await self._run(movers, RunSource.AUTOMATIC, owner=False)).renamed

        present = present_keys | {
            _key(i.path)
            for i in queue.items
            if i.watched is not None and _key(i.watched) in lost_keys
        }
        queue.forget_watched(present)
        midnight = datetime.fromtimestamp(self._now()).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        queue.purge_finished(midnight.timestamp())

        needing = self._needing_owner()
        new_needs = len(needing - held_before)
        if new_needs and settings.notify_needs:
            self.notify(*needs_message(new_needs))
        self.on_needs(self._waiting())
        await self._maybe_summary()
        self._tell_status(self.status([*lost_roots, *lost_libraries]))
        return LookResult(len(new_ids), renamed, len(needing), lost_roots)

    def _waiting(self) -> int:
        """MediaForge's number before its page is built: the watched files not renamed yet."""
        return sum(
            1
            for i in self._parts.queue.items
            if i.watched is not None and i.status not in _DONE
        )

    def _needing_owner(self) -> set[int]:
        return {
            i.id
            for i in self._parts.queue.items
            if i.watched is not None and i.status in _NEEDS_OWNER
        }

    def _note_reachability(self, lost: Sequence[Path]) -> None:
        """Log a folder going away once, and coming back once: never at every look."""
        now_lost = {_key(p): p for p in lost}
        for key, path in now_lost.items():
            if key not in self._lost:
                log.warning("Can't reach the folder %s", _logged(path))
                self._say(t("Can't reach {folder}", folder=path), level=logging.WARNING)
        for key, path in self._lost.items():
            if key not in now_lost:
                log.info("Can reach the folder %s again", _logged(path))
                self._say(t("Can reach {folder} again", folder=path))
        self._lost = now_lost

    # --- renaming --------------------------------------------------------------------------

    async def rename(
        self,
        ids: Sequence[int],
        *,
        source: RunSource,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        cancel: asyncio.Event | None = None,
        naming: Mapping[int, NamingChoice] | None = None,
        hold_taken: bool = False,
        preferred: int | None = None,
    ) -> RunResult:
        """The owner's Rename on watched downloads: into the library, whatever decide says.
        `hold_taken` (the phone's tap, where no preview shows the name): a taken name holds the
        file instead of making a numbered copy, and downloads wanting one name all wait, except
        `preferred`, the one the owner tapped.
        `cancel` is the Inbox's (its Cancel button, or closing), not the renamer's own.
        `naming`: the preset, rule and options the owner chose per download (the queue's band);
        a download without one goes by its library's preset."""
        queue = self._parts.queue
        items = [
            i
            for i in (queue.get(item_id) for item_id in ids)
            if i is not None and i.watched is not None and i.status not in _DONE
        ]
        problems = library_problems(self._settings)
        if problems:
            for item in items:
                queue.hold(item.id, problems[0])
            return RunResult()
        kept = set(self._settings.kept_back)
        for item in items:
            self._undone.discard(item.id)
            kept.discard(_key(item.path))
            if item.status is ItemStatus.HELD:
                item.status, item.message = ItemStatus.READY, ""
        if kept != set(self._settings.kept_back):
            await self._save_kept(kept)
        return await self._run(
            items,
            source,
            owner=True,
            on_progress=on_progress,
            cancel=cancel or asyncio.Event(),
            naming=naming,
            hold_taken=hold_taken,
            preferred=preferred,
        )

    async def keep_back(self, paths: Sequence[Path]) -> None:
        """Put back by an undo: these downloads are never moved on their own again."""
        await self._save_kept(set(self._settings.kept_back) | {_key(p) for p in paths})

    async def _save_kept(self, kept: set[str]) -> None:
        await self._parts.settings.update(kept_back=sorted(kept))

    async def _run(
        self,
        items: Sequence[QueueItem],
        source: RunSource,
        *,
        owner: bool,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        cancel: asyncio.Event | None = None,
        naming: Mapping[int, NamingChoice] | None = None,
        hold_taken: bool = False,
        preferred: int | None = None,
    ) -> RunResult:
        parts = self._parts
        queue = parts.queue
        total = RunResult()
        stop = cancel if cancel is not None else self._cancel
        async with parts.run_lock:
            # Things may have changed while waiting for the lock: the mode, or the files.
            if not owner and self._mode != "on":
                return total
            items = [i for i in items if queue.get(i.id) is i and i.status is ItemStatus.READY]
            if not items:
                return total
            presets = await parts.presets.list()
            if hold_taken:
                runs = await asyncio.to_thread(
                    library_runs, items, self._settings, presets, preferred=preferred
                )
            elif owner:
                runs = await asyncio.to_thread(
                    owner_runs, items, self._settings, presets, fsops.exists, naming
                )
            else:
                runs = await asyncio.to_thread(library_runs, items, self._settings, presets)
            for run in runs:
                for item_id, reason in run.held.items():
                    queue.hold(item_id, reason)
                if not run.plan.files:
                    continue
                plan = run.plan
                tools = parts.trailers is not None and parts.trailers.tools_present()
                if not tools and (plan.options.trailer or plan.trailers):
                    # Without the tools no trailer can be fetched: drop the planned ones too,
                    # or the executor would still try (and report a failure) for each.
                    if plan.trailers:
                        log.info("No trailer: the trailer tools aren't downloaded yet")
                        self._say(t(TOOLS_NOT_THERE))
                    options = plan.options.model_copy(update={"trailer": False})
                    plan = plan.model_copy(update={"options": options, "trailers": ()})
                targets = {f.item_id: f.target for f in plan.files}
                finished: list[int] = []

                def progress(
                    event: ProgressEvent,
                    targets: dict[int, Path] = targets,
                    finished: list[int] = finished,
                ) -> None:
                    if event.item_id is not None:
                        if event.status == "done":
                            queue.mark_finished(event.item_id, targets.get(event.item_id))
                            finished.append(event.item_id)
                        elif event.status == "error":
                            reason = t("Couldn't move it: {reason}", reason=event.message)
                            queue.hold(event.item_id, reason)
                            if not owner:
                                self._retry.add(event.item_id)
                    if on_progress is not None:
                        on_progress(event)

                result = await parts.executor.run(
                    plan,
                    source=source,
                    preset_name=run.preset.name,
                    on_progress=progress,
                    cancel=stop,
                    kinds={run.kind},
                    media={i.id: i.media for i in items},  # for the move records
                )
                for item_id in finished:
                    if (item := queue.get(item_id)) is not None:
                        item.run_id = result.run_id
                total = _add(total, result)
            if self._settings.remove_after_forge:
                queue.forget_finished()
        return total

    async def _maybe_summary(self) -> None:
        settings = self._settings
        now = datetime.fromtimestamp(self._now())
        if not settings.daily_summary or not summary_due(now, settings.summary_sent_on):
            return
        episodes = movies = 0
        for run in await self._parts.history.list_runs():
            if run.source is not RunSource.AUTOMATIC or run.started_at.date() != now.date():
                continue
            if MediaKind.MOVIE in run.kinds:
                movies += run.file_count
            else:
                episodes += run.file_count
        if not episodes and not movies:
            return
        self.notify(SUMMARY_TITLE, summary_message(episodes, movies))
        await self._parts.settings.update(summary_sent_on=now.date().isoformat())
