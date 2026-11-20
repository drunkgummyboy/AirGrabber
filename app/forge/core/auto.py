"""Renaming on its own: when a finished download is sure enough, where it goes, and the
`AutoRenamer` that looks at the watched download folders every few minutes.

A download is only renamed without a click when its file name and TMDB agree on what it is
(`decide`); everything else waits in the Inbox with one plain reason. Where a file goes and how
it is named is its rule's (the rules for watched folders, app/forge/core/rules.py's
`route_for_item`): one run per rule, kind and folder (`library_runs`): its library folder or its
own folder with the rule's preset and extras, or for a Stays rule sorted inside the watched folder
(the folders it builds are never cleaned away). Callers that pass no `rules` get today's setup
(`rules_from_settings`). A file without a rule, whose library folder isn't chosen or (without the
owner's naming) whose rule's preset is gone waits; a file whose name is already taken there waits
instead of becoming a "(1)" copy.
Such a download ("Already in the library", `held_in_library`) can take that copy's place: the
queue band's "Use this one instead" (`AutoRenamer.replace`, `replace_runs`) moves the copy there
aside first, to the Duplicates folder by app/forge/core/duplicates.py's rule with the folder its
rule sends it to as the root (a share or drive root: the place the owner chose), with its
subtitles, in the same journaled run; so Undo puts both back. Nothing is deleted.
Everything runs on the one asyncio loop; only the disk work of a look happens in worker threads.

A look goes by the rules (`RenamerParts.rules`; without it today's setup, `rules_from_settings`):
each distinct folder is listed once (`listing_roots`), whatever the number of rules; files in a
destination, and past runs' outputs (the move records, `RenamerParts.outputs`: a Stays rule's
sorted files, after a restart too), are never taken; the rest is offered to the rules top-down
(`rule_for`) and queued with the rule and its own folder, or left alone and counted (`unfit`); a
Stays rule never takes a file that already has the name it would give it. A fresh rule's first
look of each of its folders, and every rule's after Off or the old MediaForge's guard
(`reconfigure` marks every on rule fresh, stored: `RulesLike.mark_fresh`), queues what is already
there as Manual once (`_intake`, per rule and folder: a
folder away keeps only its own first look pending, and nothing of it moves meanwhile; the files
are stored in `ForgeSettings.manual_once`, so they still wait after a restart). A rule edited or
deleted since holds its files (`rule_changed`; stored in `manual_once` too, for a restart). Only
an Automatic rule whose preset is there moves a sure file on its own; a gone preset acts as Manual. Logs give ids, counts and kinds, never a
path, file name, folder name or rule name.

A NAS can take the connection and then never answer (2026-11-14). So each folder step of a look
(listing a watched folder, the finished check of its files, asking whether a library folder is
there, and forgetting ignored entries that are gone) gets LOOK_STEP_TIMEOUT_S: past it the folder
counts as can't be reached, as after an OSError. Its thread can't be stopped and is left to end on
its own; until it has, the next looks skip that folder (`_busy`). Each watched folder has its own
`Watcher` (its `seen` sizes), touched by one thread at a time: looks never overlap (only `_loop`
runs them, one after the other; each holds `_one_look`), and a folder whose thread still runs is
skipped. Nothing waits for a look's listing, TMDB or moves (a change in Settings waits at most for one
of its short commits): a look notes the rules'
generation (`RulesLike.generation`) before it reads them, lists and checks the folders without a
lock, and commits what it decided from the rules (its take-in: the Manual-once memory, the fresh
flags, the queue; then, before its moves, the rule-changed marks) only in short `_look_lock`
blocks where the generation, the watch list and the destinations are still as read
(`_still_as_read`); else it commits nothing and the next look comes at once. `_run` checks the
generation again under `run_lock` before an automatic move, and `first_look_done` clears no
fresh flag once it moved. `hold_looks` (AirGrabber's folder sync, a change in Settings) takes the same
short lock, so neither lands inside a look's commit.
The owner's runs wait for `run_lock` through `taking`, which tells them when they have to wait
(their dock or status says WAITING_FOR_AUTO meanwhile).

MediaForge's renamer took the whole `Services`; this one takes `RenamerParts` (the queue and a few
narrow protocols). `automatic` is the master switch: `start()` does nothing while it is "off" (the
default, so a fresh Forge never watches a folder by itself: the old MediaForge may still be
watching the same folders, INTEGRATION.md 3.10's guard); "trial" / "on" watch, each rule's mode
deciding.

`blocked_by_old_install` (set by ForgeRuntime before the first start, from
app/forge/migration.py's `blocked_by_old_install()`, and by the import): while the old MediaForge
is still installed it starts with Windows and watches the same folders, so the stored Manual / On
is kept but the renamer stays idle as if Off (its status says why). app/forge/old_install.py's
`OldInstallRemover.lift_guard` clears it once the old install is gone and calls `reconfigure()`. The owner's own renames are never blocked.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import logging
import math
import os
import time
from collections import Counter
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

from app.forge.core import fsops
from app.forge.core.activity import ActivityFeed, say_to
from app.forge.core.duplicates import free_aside, place_problem, quarantine_target
from app.forge.core.executor import ProgressEvent, RunResult
from app.forge.core.formatter import name_file
from app.forge.core.library import inside
from app.forge.core.matcher import normalize_title
from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.core.planner import build_plan
from app.forge.core.queue import ItemStatus, Queue, QueueItem, WatchedFile, parse_names
from app.forge.core.rules import NOT_WATCHED as NOT_WATCHED  # re-exported: it lived here
from app.forge.core.rules import (
    Route,
    WatchedRule,
    destinations,
    holds,
    in_watched_folder,
    listing_roots,
    route_for_item,
    route_problem,
    rule_for,
    rules_from_settings,
    watch_list,
    watched_folders,
)
from app.forge.core.run_progress import AUTO, RunProgress, item_name
from app.forge.core.watch import (
    LOOK_INTERVAL_S,
    SECOND_LOOK_S,
    Ignored,
    Settling,
    Watcher,
    all_settling,
    candidates,
)
from app.forge.models.history import RunSource, RunSummary
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.forge.models.plan import Plan, PlanInput, PlannedAside, PlannedLeftovers
from app.forge.models.preset import DEFAULT_PRESETS, Preset, PresetOptions, with_options
from app.forge.models.rules import Rule
from app.forge.models.settings import ForgeSettings
from app.i18n import N_, t, tn

log = logging.getLogger(__name__)

# Reasons a watched download waits (translated where they are handed to the queue).
ALREADY_THERE = N_("Already in the library.")
SAME_NAME = N_("Two downloads would get the same name.")
# Held when a run has neither the owner's naming choice nor its rule's preset (no fallback).
PRESET_GONE_HOLD = N_("Its rule's preset is gone: choose another in Settings › Sorting.")
# Replace found nothing in the library under a download's name (the band's preset names it
# otherwise): renamed as usual, one Activity line.
NOTHING_TO_REPLACE = N_("Nothing in the library to replace for {n} file: renamed as usual.")
NOTHING_TO_REPLACE_MORE = N_("Nothing in the library to replace for {n} files: renamed as usual.")
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
    in_library: frozenset[int] = frozenset()  # those of `held` already there (where it goes)
    unreplaced: tuple[int, ...] = ()  # replace mode: nothing there under their name
    rule_id: int = 0  # the rule it runs for (0: a rule not stored yet, or none)


class NamingChoice(NamedTuple):
    """How the owner chose to name one watched download (the queue's band): its preset (for
    History), the naming rule (the preset's or a Custom one) and the options. Where its rule
    sends it all the same; clean-up stays the global one (`clean_downloads`)."""

    preset: Preset
    rule: str
    options: PresetOptions


def _preset(name: str, fallback: str, presets: Sequence[Preset]) -> Preset:
    for source in (presets, DEFAULT_PRESETS):
        for preset in source:
            if preset.name == name:
                return preset
    return next(p for p in DEFAULT_PRESETS if p.name == fallback)


def held_in_library(item: QueueItem) -> bool:
    """Held because the film or episode is in the library already (`QueueItem.in_library`,
    set by the hold): its band offers "Use this one instead"."""
    return item.status is ItemStatus.HELD and item.in_library


def library_copies(target: Path, exists: Callable[[Path], bool]) -> list[Path]:
    """What a download named `target` would replace in the library: a file with that name, or
    the same name as another kind of video (`_other_copy`'s)."""
    others = [target.with_suffix(ext) for ext in sorted(VIDEO_EXTENSIONS) if ext != target.suffix.lower()]
    return [path for path in (target, *others) if exists(path)]


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
    items: Sequence[QueueItem],
    plan: Plan,
    held: dict[int, str],
    keep: Callable[[Path], bool] | None = None,
) -> tuple[PlannedLeftovers, ...]:
    """The download folders to clear after the move: below (never equal to) the watched folder,
    and only those every file of which is moving now. Never a folder `keep` names, nor one a
    planned file goes into or below (the folders a Stays rule built are the result)."""
    moving = {f.item_id for f in plan.files}
    folders: dict[str, tuple[Path, list[int]]] = {}
    kept: set[str] = set()
    for item in items:
        folder = item.path.parent
        key = os.path.normcase(str(folder))
        if item.id not in moving or item.id in held:
            kept.add(key)
        elif (keep is not None and keep(folder)) or any(
            inside(f.target.parent, folder) for f in plan.files
        ):
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


def _held_preset(kind: MediaKind) -> Preset:
    """What a run whose files all wait carries as its preset (nothing is named with it)."""
    name = "Series (Folder)" if kind is MediaKind.EPISODE else "Movie (Folder)"
    return next(p for p in DEFAULT_PRESETS if p.name == name)


def _group_root(group: Sequence[QueueItem], routes: Mapping[int, Route | None]) -> Path | None:
    """The folder the names of a group start in (one per group)."""
    route = routes.get(group[0].id)
    return route.root if route is not None else None


def _runs(
    items: Sequence[QueueItem],
    routes: Mapping[int, Route | None],
    exists: Callable[[Path], bool],
    *,
    roots: Sequence[Path],
    hold_taken: bool,
    clean_downloads: bool,
    naming: Mapping[int, NamingChoice] | None = None,
    preferred: int | None = None,
    replace: bool = False,
    place: Path | None = None,
    keep: Callable[[Path], bool] | None = None,
    watched: Sequence[Path] = (),
) -> list[LibraryRun]:
    """One run per rule, kind and folder the names start in (`routes`, per item id), and
    inside that one per naming: the route's preset with the rule's extras, or the owner's
    `naming` per item. A file outside its watched folder, without a route, with a
    `route_problem` or (without a naming choice) whose rule's preset is gone waits first.
    Downloads wanting one name all wait, unless one of them is `preferred`: that one goes.
    `replace`: what is there under a download's name moves aside first (`_asides`); of
    downloads wanting one name (whatever its extension) the first goes, the others wait.
    `roots`: every folder files go to (the destinations and the Stays rules' watched folders):
    never a place for the copies moved aside. `watched`: the folders the rules watch (a file
    in one that no rule takes says so, `route_problem`)."""
    chosen = naming or {}
    held: dict[int, str] = {}
    how: dict[int, NamingChoice] = {}
    for item in items:
        route = routes.get(item.id)
        in_one = any(inside(item.path, folder) for folder in watched)
        problem = route_problem(route, item.media.kind, watched=in_one)
        if item.watched is None or not inside(item.path, item.watched):
            held[item.id] = t(NOT_WATCHED)
        elif problem is not None:
            held[item.id] = problem
        if route is None:
            continue
        if item.id in chosen:
            how[item.id] = chosen[item.id]
        elif route.preset is not None:
            preset = route.preset
            how[item.id] = NamingChoice(
                preset, with_options(preset.format, route.options), route.options
            )
        elif item.id not in held:
            held[item.id] = t(PRESET_GONE_HOLD)
    wanted: dict[int, Path] = {}
    for item in items:
        route = routes.get(item.id)
        if item.id in held or route is None or route.root is None:
            continue
        name = name_file(how[item.id].rule, item.media, item.path.suffix)
        wanted[item.id] = route.root.joinpath(*name.parts)
    if hold_taken:
        claims = Counter(os.path.normcase(str(w)) for w in wanted.values())
        for item_id, target in wanted.items():
            if item_id != preferred and claims[os.path.normcase(str(target))] > 1:
                held[item_id] = t(SAME_NAME)

    # The groups: (rule, kind, the folder names start in), shows first; by naming within each.
    groups: dict[tuple[int, MediaKind, str], list[QueueItem]] = {}
    for kind in (MediaKind.EPISODE, MediaKind.MOVIE):
        for item in items:
            if item.media.kind is not kind:
                continue
            route = routes.get(item.id)
            root = route.root if route is not None else None
            key = (route.rule.id if route is not None else 0, kind, _key(root) if root else "")
            groups.setdefault(key, []).append(item)

    there: set[int] = set()  # held as already there
    asides: dict[int, list[tuple[Path, Path]]] = {}
    unreplaced: list[int] = []
    aside_names: set[str] = set()  # names given to copies moved aside in this plan
    if replace:
        # Never a "(1)" copy where it goes: one download per name (and extension-less).
        claimed: set[str] = set()
        for item_id, target in wanted.items():
            if item_id in held:
                continue
            stem = os.path.normcase(str(target.with_suffix("")))
            if stem in claimed:
                held[item_id] = t(SAME_NAME)
            claimed.add(stem)
        for group in groups.values():
            group_root = _group_root(group, routes)
            if group_root is None:
                continue
            mine = {i.id: wanted[i.id] for i in group if i.id in wanted}
            found, none_there = _asides(
                mine, held, there, group_root, roots, place, exists, aside_names
            )
            asides.update(found)
            unreplaced += none_there
    moving_aside = {_key(old) for moves in asides.values() for old, _ in moves}

    def in_use(path: Path) -> bool:
        return _key(path) not in moving_aside and exists(path)

    runs: list[LibraryRun] = []
    for (rule_id, kind, _folder), group in groups.items():
        root = _group_root(group, routes)
        by_naming: dict[tuple[str, str, str], list[QueueItem]] = {}
        for item in group:
            chosen_how = how.get(item.id)
            key_of = (
                (chosen_how.preset.name, chosen_how.rule, chosen_how.options.model_dump_json())
                if chosen_how is not None
                else ("", "", "")
            )
            by_naming.setdefault(key_of, []).append(item)
        for part in by_naming.values():
            part_how = how.get(part[0].id)
            ids = {i.id for i in part}
            plan = Plan(files=())
            if part_how is not None and root is not None:
                options = part_how.options.model_copy(
                    update={"clean_junk": False, "prune_empty": False}
                )
                inputs = [
                    PlanInput(
                        item_id=i.id, source=i.path, root=root, dropped_folder=None, media=i.media
                    )
                    for i in part
                    if i.id not in held
                ]
                plan = build_plan(inputs, part_how.rule, options, in_use)
            if asides:  # only for a file that does get the name it replaces
                plan = plan.model_copy(
                    update={
                        "aside": tuple(
                            PlannedAside(item_id=f.item_id, source=old, target=new)
                            for f in plan.files
                            if not f.skip_reason and _same(f.target, wanted[f.item_id])
                            for old, new in asides.get(f.item_id, ())
                        )
                    }
                )
            if hold_taken:
                for planned in plan.files:
                    target = wanted[planned.item_id]
                    if not _same(planned.target, target) or _other_copy(target, exists):
                        held[planned.item_id] = t(ALREADY_THERE)
                        there.add(planned.item_id)
                plan = plan.model_copy(
                    update={"files": tuple(f for f in plan.files if f.item_id not in held)}
                )
            if clean_downloads:
                plan = plan.model_copy(update={"leftovers": _leftovers(part, plan, held, keep)})
            for planned in plan.files:
                if root is None or not inside(planned.target, root):
                    # Never the path in the message: it can hold the user name.
                    raise ValueError(f"a planned file is outside its {kind.value} folder")
            mine_held = {item_id: why for item_id, why in held.items() if item_id in ids}
            planned_ids = {f.item_id for f in plan.files}
            runs.append(
                LibraryRun(
                    kind,
                    part_how.preset if part_how is not None else _held_preset(kind),
                    plan,
                    mine_held,
                    frozenset(there & mine_held.keys()),
                    tuple(n for n in unreplaced if n in planned_ids),
                    rule_id,
                )
            )
    return runs


def _asides(
    wanted: Mapping[int, Path],
    held: dict[int, str],
    there: set[int],
    root: Path,
    roots: Sequence[Path],
    place: Path | None,
    exists: Callable[[Path], bool],
    names: set[str],
) -> tuple[dict[int, list[tuple[Path, Path]]], list[int]]:
    """Per download whose name starts in `root`, the copies there under its name and where
    each moves aside (`quarantine_target` with `root` as the root, a free name there), and the
    downloads with nothing there under their name (renamed as usual). A copy with nowhere to go
    (a share or drive root without a chosen place, or a place inside any of `roots`) holds the
    download as it was (`there`)."""
    if place is not None and place_problem(place, roots) is not None:
        place = None  # never inside where files go
    asides: dict[int, list[tuple[Path, Path]]] = {}
    unreplaced: list[int] = []
    for item_id, target in wanted.items():
        if item_id in held:
            continue
        moves: list[tuple[Path, Path]] = []
        for old in library_copies(target, exists):
            new = quarantine_target(old, [root], place)
            if new is None or any(inside(new, r) for r in roots):
                held[item_id] = t(ALREADY_THERE)
                there.add(item_id)
                break
            moves.append((old, free_aside(new, exists, names)))
        else:
            if moves:
                asides[item_id] = moves
            else:
                unreplaced.append(item_id)
    return asides, unreplaced


def _legacy(settings: ForgeSettings, presets: Sequence[Preset]) -> tuple[list[Rule], list[Preset]]:
    """Today's setup as rules, for a caller that passes none (`rules_from_settings`), with the
    Series / Movies presets as `_preset` finds them (a gone one: the built-in of that name,
    else the kind's), so such a caller plans as before."""
    series = _preset(settings.series_preset, "Series (Folder)", presets)
    movies = _preset(settings.movies_preset, "Movie (Folder)", presets)
    named = settings.model_copy(update={"series_preset": series.name, "movies_preset": movies.name})
    known = {p.name for p in presets}
    extra = {p.name: p for p in (series, movies) if p.name not in known}
    return rules_from_settings(named), [*presets, *extra.values()]


def _planned(
    items: Sequence[QueueItem],
    settings: ForgeSettings,
    presets: Sequence[Preset],
    rules: Sequence[Rule] | None,
) -> tuple[dict[int, Route | None], list[Path], list[Path]]:
    """Each item's route, every folder files go to (`_runs`' `roots`, distinct: the
    destinations, and the folders the routes start in, a Stays rule's watched folder), and the
    folders the rules watch (`_runs`' `watched`)."""
    if rules is None:
        rules, presets = _legacy(settings, presets)
    routes = {i.id: route_for_item(i, rules, settings, presets) for i in items}
    roots: dict[str, Path] = {}
    for root in destinations(rules, settings):
        roots.setdefault(_key(root), root)
    for route in routes.values():
        if route is not None and route.root is not None:
            roots.setdefault(_key(route.root), route.root)
    return routes, list(roots.values()), [Path(f) for f in watched_folders(rules, settings)]


def library_runs(
    items: Sequence[QueueItem],
    settings: ForgeSettings,
    presets: Sequence[Preset],
    exists: Callable[[Path], bool] = fsops.exists,
    *,
    preferred: int | None = None,
    rules: Sequence[Rule] | None = None,
    keep: Callable[[Path], bool] | None = None,
) -> list[LibraryRun]:
    """One run per rule and kind for renaming on its own; a taken name holds the file instead.
    `rules`: the rules for watched folders (None: today's setup, `rules_from_settings`);
    `keep(folder)`: a download folder never cleaned away."""
    routes, roots, watched = _planned(items, settings, presets, rules)
    return _runs(
        items,
        routes,
        exists,
        roots=roots,
        hold_taken=True,
        clean_downloads=settings.clean_downloads,
        preferred=preferred,
        keep=keep,
        watched=watched,
    )


def owner_runs(
    items: Sequence[QueueItem],
    settings: ForgeSettings,
    presets: Sequence[Preset],
    exists: Callable[[Path], bool] = fsops.exists,
    naming: Mapping[int, NamingChoice] | None = None,
    *,
    rules: Sequence[Rule] | None = None,
    keep: Callable[[Path], bool] | None = None,
) -> list[LibraryRun]:
    """The same for the owner's Rename click: a taken name gets a numbered copy, as in 1.x;
    `naming`: how the owner chose to name each download (its queue band), else its rule's
    preset."""
    routes, roots, watched = _planned(items, settings, presets, rules)
    return _runs(
        items,
        routes,
        exists,
        roots=roots,
        hold_taken=False,
        clean_downloads=settings.clean_downloads,
        naming=naming,
        keep=keep,
        watched=watched,
    )


def replace_runs(
    items: Sequence[QueueItem],
    settings: ForgeSettings,
    presets: Sequence[Preset],
    exists: Callable[[Path], bool] = fsops.exists,
    naming: Mapping[int, NamingChoice] | None = None,
    *,
    place: Path | None = None,
    rules: Sequence[Rule] | None = None,
    keep: Callable[[Path], bool] | None = None,
) -> list[LibraryRun]:
    """The owner's "Use this one instead": as `owner_runs`, but each download takes the name it
    wants and what is there under it moves aside first (`Plan.aside`), to the Duplicates folder
    beside the folder its rule sends it to (a Stays rule's: its watched folder); `place`: where
    the copies of a share or drive root go (the owner's choice)."""
    routes, roots, watched = _planned(items, settings, presets, rules)
    return _runs(
        items,
        routes,
        exists,
        roots=roots,
        hold_taken=False,
        clean_downloads=settings.clean_downloads,
        naming=naming,
        replace=True,
        place=place,
        keep=keep,
        watched=watched,
    )


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


class RulesLike(Protocol):
    """What the renamer needs of RuleService (app/forge/core/rules_service.py)."""

    @property
    def current(self) -> tuple[Rule, ...]: ...

    @property
    def generation(self) -> int | None: ...  # changes with every stored change; None meanwhile

    async def first_look_done(
        self, rule_ids: Collection[int], *, generation: int | None = None
    ) -> None: ...  # clears nothing when the generation moved since the look noted it

    async def mark_fresh(self, rule_id: int) -> None: ...


class OutputsLike(Protocol):
    """What the renamer needs of ForgeMoveService: where past runs put files (its memory)."""

    async def targets_in(self, folders: Sequence[Path]) -> set[str]: ...


class FinishedCheck(Protocol):
    """What a look needs of a watched folder's `Watcher` (app/forge/core/watch.py)."""

    settling: Settling  # what its last `finished` left on its way

    def finished(self, files: Sequence[Path]) -> list[Path]: ...


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
    # The rules for watched folders (None: today's setup, `rules_from_settings`, for tests and
    # stand-ins) and the move records (None: no memory of sorted files).
    rules: RulesLike | None = None
    outputs: OutputsLike | None = None


# --- the renamer ---------------------------------------------------------------------------------

SUMMARY_HOUR = 20  # the evening summary comes from 8 pm
UNDONE = N_("You undid this once; click Forge this to move it again.")
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
    settling: Settling = Settling()  # downloads seen but not quiet yet (`settling_message`)


TOOLS_NOT_THERE = N_("No trailer: the trailer tools aren't downloaded yet.")
SCAN_SECOND_LOOK_S = 3.0  # "look now": a file counts as finished from its second look on
LOOK_STEP_TIMEOUT_S = 15  # a folder step of a look that takes longer: can't reach that folder
# What an owner's run shows while it waits for `run_lock` (`taking`).
WAITING_FOR_AUTO = N_("Waiting for Sorting to finish…")
_IGNORED_STEP = "\0ignored"  # `_busy`'s key for forgetting gone ignored entries (never a path)
_MEMORY_STEP = "\0memory"  # `_busy`'s key for asking whether remembered downloads are there


@asynccontextmanager
async def taking(
    lock: asyncio.Lock, on_wait: Callable[[bool], None] | None = None
) -> AsyncIterator[None]:
    """`async with lock`, and when another run holds it, `on_wait(True)` before waiting and
    `on_wait(False)` once it is ours (or the wait was given up)."""
    waits = on_wait is not None and lock.locked()
    if waits and on_wait is not None:
        on_wait(True)
    try:
        await lock.acquire()
    finally:
        if waits and on_wait is not None:
            on_wait(False)
    try:
        yield
    finally:
        lock.release()


def _retrieve(task: asyncio.Future[Any]) -> None:
    """An abandoned step's end: its error (if any) is read, so nothing logs it as never read."""
    if not task.cancelled():
        task.exception()


def needs_message(new: int) -> tuple[str, str]:
    """The notification's title and text when `new` watched downloads start to wait."""
    title = tn("{n} file needs you", "{n} files need you", new)
    return title, t("Open MediaForge to pick or confirm them.")


def settling_message(settling: Settling, now: float) -> str | None:
    """"1 download finishing, ready in about 8 min" (the minutes rounded up; "in under a minute"
    below 60 s), or None when nothing is on its way. `now`: `time.time()`."""
    count, ready_at = settling
    if not count or ready_at is None:
        return None
    left = ready_at - now
    if left < 60:
        return tn(
            "{n} download finishing, ready in under a minute",
            "{n} downloads finishing, ready in under a minute",
            count,
        )
    return tn(
        "{n} download finishing, ready in about {m} min",
        "{n} downloads finishing, ready in about {m} min",
        count,
        m=math.ceil(left / 60),
    )


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
        replaced=a.replaced + b.replaced,
        cleaned=a.cleaned + b.cleaned,
        pruned=a.pruned + b.pruned,
        cancelled=a.cancelled or b.cancelled,
        errors=[*a.errors, *b.errors],
    )


class Unfit(NamedTuple):
    """The last look's finished downloads in one listed folder that no rule took."""

    folder: Path  # a listing root (`listing_roots`)
    files: tuple[Path, ...]


def _under(key: str, folders: Collection[str]) -> bool:
    """Whether the path `key` (`_key`) lies in one of `folders` (`_key`s) or is one."""
    return any(key == f or key.startswith(f.rstrip("\\/") + "\\") for f in folders)


def _keeper(outputs: Collection[str]) -> Callable[[Path], bool]:
    """`keep` for the runs: a download folder holding a past run's output (a Stays rule's
    sorted files: the result) is never cleaned away."""
    return lambda folder: any(_under(output, (_key(folder),)) for output in outputs)


def _rule_now(item: QueueItem, rules: Sequence[Rule], watch: Sequence[WatchedRule]) -> Rule | None:
    """The rule that has `item` now: its rule by id among `rules`; for a rule never stored (id 0:
    today's setup as rules) the one that takes the file now. None: gone (or `item` has none)."""
    snapshot = item.rule
    if snapshot is None:
        return None
    if snapshot.id:
        return next((rule for rule in rules if rule.id == snapshot.id), None)
    found = rule_for(item.path, item.parsed, watch)
    return found.rule if found is not None else None


def _rule_changed(item: QueueItem, rules: Sequence[Rule], watch: Sequence[WatchedRule]) -> bool:
    """Whether the rule that took `item` was deleted or does something else now."""
    if item.rule is None:
        return False
    now = _rule_now(item, rules, watch)
    return now is None or not now.same_routing(item.rule)


type EntryKey = tuple[str, str]  # a rule (its id; "" for one never stored) and a folder it watches


def _entry_key(rule: Rule, folder: Path) -> EntryKey:
    return (str(rule.id) if rule.id else "", _key(folder))


def _automatic_route(
    item: QueueItem,
    rules: Sequence[Rule],
    watch: Sequence[WatchedRule],
    settings: ForgeSettings,
    presets: Sequence[Preset],
    *,
    pending: Collection[EntryKey] = (),
    manual_once: Collection[str] = (),
) -> Route | None:
    """The route a watched download goes by on its own; None when it waits for the owner: not
    READY, without a rule, taken as Manual (`manual`, or its path in `manual_once`: the stored
    memory), its rule's first look of its folder still to come (`pending`), its rule changed
    since, that rule Manual or its preset gone (acting as Manual, never another preset), or not
    sure (`decide`)."""
    if item.watched is None or item.rule is None or item.status is not ItemStatus.READY:
        return None
    if item.manual or _key(item.path) in manual_once:
        return None
    if _entry_key(item.rule, item.watched) in pending:
        return None
    if item.rule_changed or _rule_changed(item, rules, watch):
        return None
    route = route_for_item(item, rules, settings, presets)
    if route is None or route.rule.mode != "auto" or route.preset is None:
        return None
    return route if decide(item).sure else None


def _in_place(
    item: QueueItem, rules: Sequence[Rule], settings: ForgeSettings, presets: Sequence[Preset]
) -> bool:
    """A Stays rule's file that already has the name its rule gives it, where it is (sorted
    before; its move record may have gone after a year): never taken again."""
    route = route_for_item(item, rules, settings, presets)
    if route is None or route.moves or route.preset is None or route.root is None:
        return False
    rule = with_options(route.preset.format, route.options)
    name = name_file(rule, item.media, item.path.suffix)
    return _key(route.root.joinpath(*name.parts)) == _key(item.path)


def _listing(
    root: Path, ignored: Ignored | None, skip: Collection[str], dests: Sequence[Path]
) -> tuple[list[Path], list[Path]]:
    """`root`'s candidates (the destinations in it not walked) and those of them the finished
    check is for: neither in `skip` nor in a destination in it (a destination holding `root`
    is no matter: a look never goes up) (blocking: a worker thread)."""
    within = _within(root, dests)
    files = candidates(root, ignored, within)
    return files, [
        file
        for file in files
        if _key(file) not in skip and not any(inside(file, dest) for dest in within)
    ]


def _within(root: Path | None, dests: Sequence[Path]) -> list[Path]:
    """The destinations inside `root` (none without one): what a look of it skips."""
    return [] if root is None else [dest for dest in dests if inside(dest, root)]


def _existing(keys: Sequence[str]) -> set[str]:
    """Those of the path keys that are on the disk (blocking: a worker thread)."""
    return {key for key in keys if fsops.exists(Path(key))}


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
        new_watcher: Callable[[], FinishedCheck] | None = None,
        step_timeout_s: float = LOOK_STEP_TIMEOUT_S,
        progress: RunProgress | None = None,
    ) -> None:
        self._parts = parts
        # The dock on every page: this renamer's runs that nobody else shows (`_run`).
        self._progress = progress
        self._say = say_to(activity)  # the Activity strip, next to each English log line
        self._closed = False  # `close()` was called: never starts again
        self._now = now
        self._interval = interval_s
        self._second_look = min(second_look_s, interval_s)
        self._scan_look = min(scan_look_s, interval_s)
        self._scan_looks = 0  # looks still to come for `scan_now`
        self._scan_found = LookResult(0, 0, 0, [])  # what they found so far
        self._scan_waiting: list[asyncio.Future[LookResult | None]] = []
        self._new_watcher: Callable[[], FinishedCheck] = new_watcher or (lambda: Watcher(now=now))
        self._watchers: dict[str, FinishedCheck] = {}  # per watched folder (see the module's notes)
        self._step_timeout = step_timeout_s
        self._busy: dict[str, asyncio.Future[Any]] = {}  # steps past the limit, still going
        self._retry: set[int] = set()  # held for a passing reason (the NAS was away)
        self._undone: set[int] = set()  # put back by an undo: never moved on its own again
        self._lost: dict[str, Path] = {}  # folders that couldn't be reached at the last look
        self._unreachable: list[Path] = []  # the last look's lost listed folders, destinations
        self._parsed: dict[str, ParsedMedia] = {}  # listed files' parsed names, by `_key`
        self._unfit: dict[str, Unfit] = {}  # per listing root: what no rule took (`unfit`)
        # Each rule's first look of each of its folders (a fresh rule's, every rule's after an
        # un-pause), per (rule, folder): due until a look reaches that folder; what is there then
        # waits as Manual once (`ForgeSettings.manual_once`). Nothing of a pending one moves.
        self._pending: set[EntryKey] = set()
        self._fresh_done: set[EntryKey] = set()  # a fresh rule's folders already taken in
        # Watching again after Off: every on rule is marked fresh (stored: a restart before a
        # folder away comes back keeps its first look due), the next look waits for it. Without
        # a rule service, or when marking failed, the next look makes them pending (`_unpaused`).
        self._marking: asyncio.Task[None] | None = None
        self._unpaused = False
        self._one_look = asyncio.Lock()  # held by each whole look: they never overlap
        # Held by a look's two short commits (its take-in; the check before its moves) and by
        # `hold_looks`: never across a disk step, TMDB or a move.
        self._look_lock = asyncio.Lock()
        self._redo = False  # the rules changed during a look: the next one comes at once
        self._sorted: set[str] = set()  # listed files a Stays rule has named so already
        self._last_mode: str | None = None  # the mode `reconfigure` saw last (None: not started)
        # The last look's downloads on their way, as the watchers saw them (`settling` adds when
        # the look that takes them in comes); the ↻ and the empty queue say when they're ready.
        self._settling_seen = Settling()
        self._next_look_at: float | None = None  # `now()` of the timed look to come (None: idle)
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
        """Hear every status too, as `on_status` (Settings › Sorting while it is open); the
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
        """The master switch in effect: Off (paused) or watching ("trial" / "on": each rule's
        own mode decides), and Off while the old MediaForge is installed."""
        return "off" if self.blocked_by_old_install else self._settings.automatic

    def _paused(self) -> bool:
        return self._mode == "off"

    def _rules(self) -> tuple[Rule, ...]:
        """The rules for watched folders now (without a rule service: today's setup)."""
        rules = self._parts.rules
        if rules is not None:
            return rules.current
        return tuple(rules_from_settings(self._settings))

    def unfit(self) -> list[Unfit]:
        """The last look's finished downloads no rule took, per listed folder that has any."""
        return list(self._unfit.values())

    def unreachable_folders(self) -> list[str]:
        """The watched folders and destinations the last look couldn't reach."""
        return [str(folder) for folder in self._unreachable]

    # --- running on a timer ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start looking, unless renaming on its own is off (the default) or it runs already."""
        if self._last_mode is None:
            self._last_mode = self._mode
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
        self._settling_seen = Settling()

    async def close(self) -> None:
        """Stop for good (Forge is shutting down): after a stop or restart `reconfigure` left
        pending (Off then Manual just before quitting), so nothing starts looking afterwards. A
        rename in progress stops after its current file."""
        self._closed = True
        tasks = (self._stop_task, self._restart_task, self._marking)
        pending = [task for task in tasks if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.stop()

    def look_soon(self) -> None:
        self._wake.set()

    @property
    def settling(self) -> Settling:
        """The downloads on their way, ready at the first look at or after the time they are quiet
        (the next timed look, then every interval): a look takes them in, not the clock."""
        seen = self._settling_seen
        first = self._next_look_at
        if seen.ready_at is None or first is None:
            return seen
        if seen.ready_at <= first:
            return Settling(seen.files, first)
        later = math.ceil((seen.ready_at - first) / self._interval)
        return Settling(seen.files, first + later * self._interval)

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
                self.settling,  # with the look to come (`_loop` has set it)
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
        """The settings changed: start, stop, or look again at once. Watching again after Off
        makes the next look every rule's first (what came in meanwhile waits as Manual once)."""
        if self._closed:
            return
        if self._last_mode == "off" and self._mode != "off":
            self._watching_again()
        self._last_mode = self._mode
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

    def _watching_again(self) -> None:
        """Off (or the old MediaForge's guard) ended: every rule's first look of each of its
        folders is due again, what came in meanwhile waits as Manual once."""
        self._fresh_done.clear()  # a fresh rule's folders taken in before count again
        rules = self._parts.rules
        if rules is None:
            self._unpaused = True
            return
        ids = [rule.id for rule in self._rules() if rule.on and rule.id]
        unstored = any(rule.on and not rule.id for rule in self._rules())
        if unstored:
            self._unpaused = True
        if ids:
            previous = self._marking
            self._marking = asyncio.get_running_loop().create_task(
                self._mark_fresh(rules, ids, previous)
            )

    async def _mark_fresh(
        self, rules: RulesLike, ids: Sequence[int], previous: asyncio.Task[None] | None
    ) -> None:
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        try:
            for rule_id in ids:
                await rules.mark_fresh(rule_id)
        except Exception as exc:  # noqa: BLE001 - this session's looks still take them in
            log.warning("Couldn't mark %d rule(s) fresh (%s)", len(ids), type(exc).__name__)
            self._unpaused = True

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
                # A scan's first look is followed by its second one soon; else the timed wait.
                pause = self._scan_look if self._scan_looks > 1 else wait
                self._next_look_at = self._now() + pause  # before a scan is answered
                self._scan_look_done(result)
                self._wake.clear()
                if self._redo:  # the rules changed during that look: the next one at once
                    self._redo = False
                    self._wake.set()
                if self._stopping:
                    break
                try:
                    await asyncio.wait_for(self._wake.wait(), pause)
                except TimeoutError:
                    pass
                wait = self._interval
        finally:
            self._next_look_at = None
            self._answer_scan(None)  # stopped in between: nobody keeps waiting

    # --- one look ------------------------------------------------------------------------------

    def status(self, unreachable: Sequence[Path]) -> WatchStatus:
        settings = self._settings
        if self.blocked_by_old_install and settings.automatic != "off":
            return WatchStatus("off", t(OLD_INSTALL_STATUS))
        if settings.automatic == "off":
            return WatchStatus("off", t("Not watching"))
        rules = self._rules()
        watch = watch_list(rules, settings)
        if not watch:
            return WatchStatus("setup", t("Set up watching in Settings"))
        if unreachable:
            return WatchStatus("unreachable", t("Can't reach {folder}", folder=unreachable[0]))
        count = len(watched_folders(rules, settings))
        if all(entry.rule.mode != "auto" for entry in watch):
            text = tn("Manual: watching {n} folder", "Manual: watching {n} folders", count)
            return WatchStatus("trial", text)
        return WatchStatus("watching", tn("Watching {n} folder", "Watching {n} folders", count))

    @asynccontextmanager
    async def hold_looks(self) -> AsyncIterator[None]:
        """No look commits meanwhile (AirGrabber's folders are stored and AirGrabber's rule
        marked fresh in one step: app/forge/core/airgrabber_folders.py; a change in Settings,
        `RuleService.save`). A look holds this lock
        only for its two short commits (a few DB writes), never while it lists a folder, waits
        for TMDB or moves; a look that read the rules or the watched folders before the block
        commits nothing of it (`_still_as_read`) and looks again at once."""
        async with self._look_lock:
            yield

    async def look(self) -> LookResult:
        async with self._one_look:  # looks never overlap
            return await self._look()

    async def _look(self) -> LookResult:
        if self._marking is not None:  # watching again: the rules' fresh flags first
            await asyncio.gather(self._marking, return_exceptions=True)
            self._marking = None
        parts, settings = self._parts, self._settings
        # Noted before the rules are read: a change stored meanwhile (or under way now) makes
        # this look commit nothing it decided from them (`_still_as_read`).
        generation = self._generation()
        rules = self._rules()
        watch = watch_list(rules, settings)
        if self._mode == "off" or not watch:
            self._settling_seen = Settling()
            self._unfit, self._unreachable = {}, []
            self._tell_status(self.status([]))
            return LookResult(0, 0, 0, [])
        queue = parts.queue
        roots = listing_roots(watch)  # one listing per folder, whatever the number of rules
        dests = destinations(rules, settings)  # never watched
        lost_roots: list[Path] = []
        listed: list[tuple[Path, Path]] = []
        # The listing goes by the snapshot as it is now; what forgetting gone entries changes
        # counts from the next look. All at once, so a silent NAS costs about two limits.
        ignored = parts.ignored.snapshot() if parts.ignored is not None else None
        # What past runs put in the listed folders (a Stays rule's sorted files) and what a
        # Stays rule named so already: never taken again, so never checked either (nor the
        # destinations' files: their folders aren't walked).
        outputs = await parts.outputs.targets_in(roots) if parts.outputs is not None else set()
        skip = frozenset(outputs | self._sorted)
        looked, reached, _ = await asyncio.gather(
            asyncio.gather(*(self._look_in(root, ignored, skip, dests) for root in roots)),
            asyncio.gather(*(self._reachable(dest) for dest in dests)),
            self._forget_gone_ignored(roots),
        )
        finished: set[str] = set()
        settling: list[Settling] = []
        for root, seen in zip(roots, looked, strict=True):
            if seen is None:
                lost_roots.append(root)
                continue
            files, done, on_its_way = seen
            listed.extend((f, root) for f in files)
            finished.update(map(_key, done))
            settling.append(on_its_way)
        self._settling_seen = all_settling(settling)
        watching = {_key(root) for root in roots}
        self._watchers = {k: w for k, w in self._watchers.items() if k in watching}
        lost_dests = [dest for dest, there in zip(dests, reached, strict=True) if not there]
        self._note_reachability([*lost_roots, *lost_dests])
        self._unreachable = [*lost_roots, *lost_dests]
        lost_keys = {_key(r) for r in lost_roots}
        present_keys = {_key(f) for f, _ in listed}
        self._sorted &= present_keys
        known = {_key(i.path) for i in queue.items}
        # Never a destination's file (never taken back out) nor a past run's output.
        open_ = [
            (file, root)
            for file, root in listed
            if _key(file) not in outputs
            and _key(file) not in self._sorted
            and not any(inside(file, dest) for dest in _within(root, dests))
        ]
        fresh = [(f, r) for f, r in open_ if _key(f) in finished and _key(f) not in known]
        taken = await self._offer(fresh, watch, present_keys, lost_keys)
        kept = set(settings.kept_back)
        undone = {
            _key(f)
            for f, _parsed, _entry in taken
            if _key(f) in kept or await parts.history.was_undone(f)
        }
        # The take-in, committed only while the rules and the watched folders are still as
        # this look read them: a short block (DB writes and the queue, never the disk or TMDB).
        async with self._look_lock:
            if not self._still_as_read(generation, watch, dests):
                return self._redo_soon(lost_roots)
            # Stored before a fresh rule's flag is cleared: after a restart they still wait.
            manual_once, first_looks = await self._intake(watch, lost_roots, open_)
            if first_looks and parts.rules is not None:
                await parts.rules.first_look_done(first_looks, generation=generation)
            new_ids = await queue.add_watched(
                [
                    WatchedFile(f, entry.folder, parsed, entry.rule, _key(f) in manual_once)
                    for f, parsed, entry in taken
                ]
            )
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
        presets = await parts.presets.list()
        waiting = [
            i.id
            for i in queue.items
            if i.watched is not None and i.status in (ItemStatus.READY, ItemStatus.HELD)
        ]
        new_ids = self._drop_sorted(new_ids, rules, settings, presets, waiting)

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
        await self._forget_gone_memory(kept | newly_kept, present_keys, lost_keys)

        # Before anything moves: the rules still as read (else the next look, soon, does it).
        renamed = 0
        async with self._look_lock:
            current = self._still_as_read(generation, watch, dests)
            if current:
                changed = [
                    i
                    for i in queue.items
                    if i.watched is not None
                    and i.status not in _DONE
                    and not i.rule_changed
                    and _rule_changed(i, rules, watch)
                ]
                queue.mark_rule_changed([i.id for i in changed])
                # Stored as Manual once too: after a restart they still wait for the owner.
                once = set(self._settings.manual_once)
                if any(_key(i.path) not in once for i in changed):
                    once.update(_key(i.path) for i in changed)
                    await parts.settings.update(manual_once=sorted(once))
            else:
                self._redo = True
        # The moves go on without `_look_lock` (a copy can take minutes); `_run` reads the rules
        # and settings again under `run_lock` and moves nothing once they changed (`generation`).
        if current and not self._paused():  # it may have been paused meanwhile
            lost = [*lost_dests, *lost_roots]
            renamed = await self._move_sure(rules, watch, presets, lost, generation=generation)

        present = present_keys | {
            _key(i.path)
            for i in queue.items
            if i.watched is not None
            and (
                _under(_key(i.watched), lost_keys)
                or any(inside(i.path, d) for d in _within(i.watched, dests))
            )
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
        log.debug(
            "Look: %d new, %d fit no rule, %d rule(s) watching",
            len(new_ids),
            sum(len(u.files) for u in self._unfit.values()),
            len({entry.number for entry in watch}),
        )
        self._tell_status(self.status(self._unreachable))
        return LookResult(len(new_ids), renamed, len(needing), lost_roots, self._settling_seen)

    def _generation(self) -> int | None:
        """The rules' generation (`RulesLike.generation`: None while a change is being stored);
        0 without a rule service."""
        rules = self._parts.rules
        return rules.generation if rules is not None else 0

    def _still_as_read(
        self, generation: int | None, watch: Sequence[WatchedRule], dests: Sequence[Path]
    ) -> bool:
        """Whether the rules and what is watched are still as a look read them: no rules change
        stored or under way since (`generation`), and the same watch list and destinations (the
        settings: AirGrabber's folders, the library folders)."""
        now = self._generation()
        if generation is None or now != generation:
            return False
        rules, settings = self._rules(), self._settings
        return watch_list(rules, settings) == list(watch) and destinations(
            rules, settings
        ) == list(dests)

    def _redo_soon(self, lost_roots: list[Path]) -> LookResult:
        """The rules changed while this look read the folders: it took nothing in; the next
        look comes at once (`_loop`)."""
        log.debug("The rules changed during a look: looking again")
        self._redo = True
        self._tell_status(self.status(self._unreachable))
        return LookResult(0, 0, len(self._needing_owner()), lost_roots, self._settling_seen)

    # --- a look's rules ------------------------------------------------------------------------

    async def _offer(
        self,
        fresh: Sequence[tuple[Path, Path]],
        watch: Sequence[WatchedRule],
        present_keys: Collection[str],
        lost_keys: Collection[str],
    ) -> list[tuple[Path, ParsedMedia, WatchedRule]]:
        """Each new finished download offered to the rules top-down (`rule_for`): those a rule
        takes, with their parsed name and the entry that took them. What none takes goes to
        `_unfit` (per listing root; a root not reached keeps what it had). Names are parsed once
        while their file stays listed (`_parsed`)."""
        self._parsed = {k: p for k, p in self._parsed.items() if k in present_keys}
        todo = [file for file, _root in fresh if _key(file) not in self._parsed]
        for file, parsed in zip(todo, await parse_names([f.name for f in todo]), strict=True):
            self._parsed[_key(file)] = parsed
        taken: list[tuple[Path, ParsedMedia, WatchedRule]] = []
        unfit: dict[str, tuple[Path, list[Path]]] = {}
        for file, root in fresh:
            parsed = self._parsed[_key(file)]
            entry = rule_for(file, parsed, watch)
            if entry is None:
                unfit.setdefault(_key(root), (root, []))[1].append(file)
            else:
                taken.append((file, parsed, entry))
        kept = {k: u for k, u in self._unfit.items() if k in lost_keys}
        self._unfit = kept | {k: Unfit(root, tuple(files)) for k, (root, files) in unfit.items()}
        return taken

    async def _intake(
        self,
        watch: Sequence[WatchedRule],
        lost_roots: Sequence[Path],
        listed: Sequence[tuple[Path, Path]],
    ) -> tuple[set[str], list[int]]:
        """Each rule's first look of each of its folders: a fresh rule's (an un-pause marks every
        on rule fresh; `_unpaused` without a rule service), per (rule, folder). Due at the first
        look that reaches that folder: what is there then (finished or not; never a
        destination's file nor a past run's output: `listed` comes without) waits as Manual once,
        stored in
        `ForgeSettings.manual_once` before anything else happens; a folder not reached stays
        pending (`_pending`: nothing of it moves on its own), the rule's other folders work as
        usual. Returns the stored set and the fresh rules (by id) whose every folder had its
        first look now (their flag can go)."""
        if self._unpaused:
            self._unpaused = False
            self._pending.update(_entry_key(e.rule, e.folder) for e in watch)
        self._pending &= {_entry_key(e.rule, e.folder) for e in watch}  # no longer watched: done
        fresh = {_entry_key(e.rule, e.folder)[0] for e in watch if e.rule.fresh}
        self._fresh_done = {k for k in self._fresh_done if k[0] in fresh}
        once = set(self._settings.manual_once)
        new: set[str] = set()
        for entry in watch:
            key = _entry_key(entry.rule, entry.folder)
            due = key in self._pending or (entry.rule.fresh and key not in self._fresh_done)
            if not due:
                continue
            if any(inside(entry.folder, lost) for lost in lost_roots):
                self._pending.add(key)  # its first look comes when the folder is back
                continue
            new.update(_key(f) for f, _root in listed if holds(entry, f))
            self._pending.discard(key)
            if entry.rule.fresh:
                self._fresh_done.add(key)
        if new - once:
            once |= new
            await self._parts.settings.update(manual_once=sorted(once))
        done: dict[int, bool] = {}
        for entry in watch:
            if entry.rule.fresh and entry.rule.id:
                key = _entry_key(entry.rule, entry.folder)
                done[entry.rule.id] = done.get(entry.rule.id, True) and key in self._fresh_done
        return once, [rule_id for rule_id, all_done in done.items() if all_done]

    def _drop_sorted(
        self,
        ids: Sequence[int],
        rules: Sequence[Rule],
        settings: ForgeSettings,
        presets: Sequence[Preset],
        waiting: Collection[int] = (),
    ) -> list[int]:
        """Out of the queue again: the items a Stays rule named so already (`_in_place`): the
        new ones and those `waiting` (READY or held: TMDB may have answered only now, so the
        name their rule gives is known only now), remembered while they stay listed
        (`_sorted`). The other new ids."""
        queue = self._parts.queue
        found = [
            item
            for item in (queue.get(i) for i in dict.fromkeys([*ids, *waiting]))
            if item is not None
            and item.watched is not None
            and item.status not in _DONE
            and _in_place(item, rules, settings, presets)
        ]
        if not found:
            return list(ids)
        self._sorted.update(_key(item.path) for item in found)
        queue.remove_many([item.id for item in found])
        log.info("%d file(s) already sorted by their rule: left alone", len(found))
        gone = {item.id for item in found}
        return [i for i in ids if i not in gone]

    async def _forget_gone_memory(
        self, kept: set[str], present_keys: Collection[str], lost_keys: Collection[str]
    ) -> None:
        """Keep `kept_back` (`kept`) and `manual_once` only for the downloads still there: listed,
        under a folder the look couldn't reach, or on the disk (a release still coming in isn't
        listed). The disk is asked in one worker-thread step under the limit; past it (or while
        an earlier one still goes) nothing is forgotten this time."""
        settings = self._settings
        once = set(settings.manual_once)
        everything = kept | once
        sure = {k for k in everything if k in present_keys or _under(k, lost_keys)}
        ask = sorted(everything - sure)
        there = set(everything)  # unless the disk says otherwise
        if ask and not self._still_busy(_MEMORY_STEP):
            with contextlib.suppress(OSError, TimeoutError):
                there = sure | await self._limited(_MEMORY_STEP, asyncio.to_thread(_existing, ask))
        changes: dict[str, list[str]] = {}
        if kept & there != set(settings.kept_back):
            changes["kept_back"] = sorted(kept & there)
        if once & there != once:
            changes["manual_once"] = sorted(once & there)
        if changes:
            await self._parts.settings.update(**changes)

    async def _move_sure(
        self,
        rules: Sequence[Rule],
        watch: Sequence[WatchedRule],
        presets: Sequence[Preset],
        lost: Sequence[Path],
        *,
        generation: int | None = None,
    ) -> int:
        """Rename the downloads that go on their own (`_automatic_route`); one whose route can't
        go (its library folder not chosen) or goes into a folder the look couldn't reach (`lost`)
        is held and tried again at the next look. `generation`: the rules' as the look read them
        (`_run` moves nothing once it changed). How many were renamed."""
        queue, settings = self._parts.queue, self._settings
        once = set(settings.manual_once)
        movers: list[QueueItem] = []
        for item in list(queue.items):
            if item.id in self._undone:
                continue
            route = _automatic_route(
                item, rules, watch, settings, presets, pending=self._pending, manual_once=once
            )
            if route is None:
                continue
            problem = route_problem(route, item.media.kind)
            root = route.root
            away = next((f for f in lost if root is not None and inside(root, f)), None)
            if problem is None and away is not None:
                problem = t("Can't reach {folder}.", folder=away)
            if problem is not None:
                queue.hold(item.id, problem)
                self._retry.add(item.id)
                continue
            movers.append(item)
        if not movers:
            return 0
        run = await self._run(movers, RunSource.AUTOMATIC, owner=False, generation=generation)
        return run.renamed

    # --- a look's folder steps: each in a worker thread, each with a time limit ----------------

    def _still_busy(self, key: str) -> bool:
        """A step for `key` outran the limit at an earlier look and its thread still goes."""
        step = self._busy.get(key)
        if step is not None and step.done():
            del self._busy[key]
            return False
        return step is not None

    async def _limited[T](self, key: str, work: Awaitable[T]) -> T:
        """`work`, for at most the step limit (TimeoutError past it). A step past the limit goes
        on by itself (a thread can't be stopped) and is kept in `_busy` until it ends."""
        step = asyncio.ensure_future(work)
        try:
            return await asyncio.wait_for(asyncio.shield(step), self._step_timeout)
        finally:
            if not step.done():  # past the limit, or this look was cancelled meanwhile
                self._busy[key] = step
                step.add_done_callback(_retrieve)

    async def _look_in(
        self,
        root: Path,
        ignored: Ignored | None,
        skip: Collection[str] = frozenset(),
        dests: Sequence[Path] = (),
    ) -> tuple[list[Path], list[Path], Settling] | None:
        """`root`'s candidate videos, those of them that are finished, and those on their way;
        None when it can't be reached (an OSError, a step past the limit, or a thread of an
        earlier look still going). The destinations `dests` aren't walked, and the finished
        check (a stat and an exclusive open per file: slow over SMB) passes over their files
        and those in `skip` (`_key`s: past runs' outputs, files a Stays rule named so already),
        so a sorted collection never costs a look its time limit."""
        key = _key(root)
        if self._still_busy(key):
            return None
        watcher = self._watchers.get(key)
        if watcher is None:
            watcher = self._new_watcher()
        try:
            files, open_files = await self._limited(
                key, asyncio.to_thread(_listing, root, ignored, skip, dests)
            )
            done = await self._limited(key, asyncio.to_thread(watcher.finished, open_files))
        except (OSError, TimeoutError):
            self._watchers.pop(key, None)  # what its files looked like is forgotten, as before
            return None
        self._watchers[key] = watcher
        return files, done, watcher.settling  # read once its thread has ended

    async def _reachable(self, library: Path) -> bool:
        key = _key(library)
        if self._still_busy(key):
            return False
        try:
            return await self._limited(key, asyncio.to_thread(fsops.is_dir, library))
        except (OSError, TimeoutError):
            return False

    async def _forget_gone_ignored(self, roots: Sequence[Path]) -> None:
        """Stop ignoring what left the watched folders: it asks the disk, so it is limited too
        (past the limit nothing is forgotten this time)."""
        ignored = self._parts.ignored
        if ignored is None or self._still_busy(_IGNORED_STEP):
            return
        try:
            await self._limited(_IGNORED_STEP, ignored.forget_gone(roots))
        except TimeoutError:
            log.info("Forgetting gone ignored files took too long; trying again at the next look")

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
        """Log a folder going away once, and coming back once: never at every look. The log
        counts them (never a folder's name: it may hold a title); Activity names them."""
        now_lost = {_key(p): p for p in lost}
        went = [path for key, path in now_lost.items() if key not in self._lost]
        back = [path for key, path in self._lost.items() if key not in now_lost]
        if went:
            log.warning("Can't reach %d folder(s) (%d away now)", len(went), len(now_lost))
        if back:
            log.info("Can reach %d folder(s) again (%d still away)", len(back), len(now_lost))
        for path in went:
            self._say(t("Can't reach {folder}", folder=path), level=logging.WARNING)
        for path in back:
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
        on_wait: Callable[[bool], None] | None = None,
    ) -> RunResult:
        """The owner's Rename on watched downloads: where each one's rule sends it, whatever
        decide says (Manual, a changed rule); a file whose route can't go is held (`_routable`).
        `on_wait`: told True while it waits for another run (`taking`), False once it goes.
        `hold_taken` (the phone's tap, where no preview shows the name): a taken name holds the
        file instead of making a numbered copy, and downloads wanting one name all wait, except
        `preferred`, the one the owner tapped.
        `cancel` is the Inbox's (its Cancel button, or closing), not the renamer's own.
        `naming`: the preset, rule and options the owner chose per download (the queue's band);
        a download without one goes by its rule's preset."""
        return await self._owner_run(
            ids,
            source=source,
            on_progress=on_progress,
            cancel=cancel,
            naming=naming,
            hold_taken=hold_taken,
            preferred=preferred,
            on_wait=on_wait,
        )

    async def replace(
        self,
        ids: Sequence[int],
        *,
        source: RunSource,
        place: Path | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        cancel: asyncio.Event | None = None,
        naming: Mapping[int, NamingChoice] | None = None,
        on_wait: Callable[[bool], None] | None = None,
    ) -> RunResult:
        """The queue band's "Use this one instead", for those of `ids` held as already
        in the library (`held_in_library`; the others are left as they are): the copy there moves
        aside (to the Duplicates folder beside the library folder; for a share or drive root to
        `place`, the owner's choice), then the download is renamed into its place, in one run.
        Otherwise as `rename`."""
        queue = self._parts.queue
        held = [
            item_id
            for item_id in ids
            if (item := queue.get(item_id)) is not None
            and item.watched is not None
            and held_in_library(item)
        ]
        if not held:
            return RunResult()
        return await self._owner_run(
            held,
            source=source,
            on_progress=on_progress,
            cancel=cancel,
            naming=naming,
            on_wait=on_wait,
            replace=True,
            place=place,
        )

    async def _owner_run(
        self,
        ids: Sequence[int],
        *,
        source: RunSource,
        on_progress: Callable[[ProgressEvent], None] | None,
        cancel: asyncio.Event | None,
        naming: Mapping[int, NamingChoice] | None,
        on_wait: Callable[[bool], None] | None,
        hold_taken: bool = False,
        preferred: int | None = None,
        replace: bool = False,
        place: Path | None = None,
    ) -> RunResult:
        queue = self._parts.queue
        items = [
            i
            for i in (queue.get(item_id) for item_id in ids)
            if i is not None and i.watched is not None and i.status not in _DONE
        ]
        items = await self._routable(items)
        if not items:
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
            on_wait=on_wait,
            replace=replace,
            place=place,
        )

    async def _routable(self, items: Sequence[QueueItem]) -> list[QueueItem]:
        """Those of the owner's `items` whose route can go; each of the others is held with why
        (no rule takes it, its library folder isn't chosen): only the rules it touches wait."""
        rules, settings = self._rules(), self._settings
        presets = await self._parts.presets.list()
        going: list[QueueItem] = []
        for item in items:
            route = route_for_item(item, rules, settings, presets)
            watched = route is None and in_watched_folder(item.path, rules, settings)
            problem = route_problem(route, item.media.kind, watched=watched)
            if problem is None:
                going.append(item)
            else:
                self._parts.queue.hold(item.id, problem)
        return going

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
        on_wait: Callable[[bool], None] | None = None,
        replace: bool = False,
        place: Path | None = None,
        generation: int | None = None,
    ) -> RunResult:
        parts = self._parts
        queue = parts.queue
        total = RunResult()
        stop = cancel if cancel is not None else self._cancel
        async with taking(parts.run_lock, on_wait):
            # Things may have changed while waiting for the lock: the switch, the rules, the files.
            if not owner and self._mode == "off":
                return total
            settings, rules = self._settings, self._rules()
            if generation is not None and self._generation() != generation:
                # The rules changed (or a Save is under way) since the look read them: nothing
                # moves on what it decided; the next look, at once, decides again.
                self._redo = True
                return total
            presets = await parts.presets.list()
            items = [i for i in items if queue.get(i.id) is i and i.status is ItemStatus.READY]
            if not owner:
                watch = watch_list(rules, settings)
                items = [
                    i
                    for i in items
                    if _automatic_route(
                        i,
                        rules,
                        watch,
                        settings,
                        presets,
                        pending=self._pending,
                        manual_once=set(settings.manual_once),
                    )
                    is not None
                ]
            if not items:
                return total
            # Always the rules (never the `rules=None` fallback to today's presets), and the
            # folders holding past runs' output are kept (the move records, after a restart too).
            watched = list({_key(i.watched): i.watched for i in items if i.watched}.values())
            outputs = await parts.outputs.targets_in(watched) if parts.outputs else set()
            keep = _keeper(outputs)
            if replace:
                runs = await asyncio.to_thread(
                    replace_runs,
                    items,
                    settings,
                    presets,
                    fsops.exists,
                    naming,
                    place=place,
                    rules=rules,
                    keep=keep,
                )
            elif hold_taken:
                runs = await asyncio.to_thread(
                    library_runs,
                    items,
                    settings,
                    presets,
                    preferred=preferred,
                    rules=rules,
                    keep=keep,
                )
            elif owner:
                runs = await asyncio.to_thread(
                    owner_runs, items, settings, presets, fsops.exists, naming, rules=rules, keep=keep
                )
            else:
                runs = await asyncio.to_thread(
                    library_runs, items, settings, presets, rules=rules, keep=keep
                )
            # Reported for the dock on every page when the caller shows no progress of its own
            # (an automatic run, the phone's tap; the page reports its own run itself).
            follow: Callable[[ProgressEvent], None] | None = None
            files = sum(len(run.plan.files) for run in runs)
            if self._progress is not None and on_progress is None and files:
                follow = self._progress.follow(AUTO, files, self._name_of)
            try:
                total = await self._run_plans(
                    runs,
                    items,
                    source,
                    owner=owner,
                    on_progress=on_progress,
                    follow=follow,
                    stop=stop,
                )
            finally:
                if follow is not None and self._progress is not None:
                    self._progress.idle(AUTO)
            if self._settings.remove_after_forge:
                queue.forget_finished()
        return total

    def _name_of(self, item_id: int) -> str | None:
        item = self._parts.queue.get(item_id)
        return item_name(item) if item is not None else None

    async def _run_plans(
        self,
        runs: Sequence[LibraryRun],
        items: Sequence[QueueItem],
        source: RunSource,
        *,
        owner: bool,
        on_progress: Callable[[ProgressEvent], None] | None,
        follow: Callable[[ProgressEvent], None] | None,
        stop: asyncio.Event,
    ) -> RunResult:
        """`_run`'s part under the lock, once the runs are planned: each run's plan through the
        executor, its steps to the queue (and to `on_progress` / `follow`)."""
        parts = self._parts
        queue = parts.queue
        total = RunResult()
        for run in runs:
            for item_id, reason in run.held.items():
                queue.hold(item_id, reason, in_library=item_id in run.in_library)
            if run.unreplaced:
                count = len(run.unreplaced)
                log.info("Replace: nothing in the library under %d file(s)' name", count)
                self._say(tn(NOTHING_TO_REPLACE, NOTHING_TO_REPLACE_MORE, count))
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
                    elif event.status == "error" and event.in_library:
                        # The copy in the library couldn't move aside: still in the library.
                        queue.hold(event.item_id, event.message, in_library=True)
                    elif event.status == "error":
                        reason = t("Couldn't move it: {reason}", reason=event.message)
                        queue.hold(event.item_id, reason)
                        if not owner:
                            self._retry.add(event.item_id)
                if on_progress is not None:
                    on_progress(event)
                if follow is not None:
                    follow(event)

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
