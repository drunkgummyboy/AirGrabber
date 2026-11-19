"""The rules for watched folders, pure (docs/forge/2026-11-18-rules-design.md): no I/O, no Tk.

- `rules_from_settings`: today's setup (the watched folders, the Series / Movies presets and Off /
  Manual / On) as rules, for the one-time carry-over (app/forge/core/rules_service.py's
  `carry_rules`, and the old MediaForge's import: stored fresh, so the first look queues what is
  already there as Manual once), so nothing behaves differently until the owner edits them.
- Matching: `watch_list` (what is watched, rule by rule, in order: an on rule without a refusal;
  AirGrabber's rule once per folder of AirGrabber's in use, `airgrabber_uses`), `holds` (the
  subfolder option), `fits` (take by the parsed kind, then quality: `is_4k`, read from the name),
  `rule_for` (the first entry that holds and fits a file: top-down, nested folders allowed),
  `listing_roots` (one listing per distinct folder; a folder inside another listed one comes with
  its listing), `watched_folders`; `newly_watched` (the folders AirGrabber's settings brought
  AirGrabber's rule: app/forge/core/airgrabber_folders.py marks it fresh for them); `uncovered`
  (the rules a Save has watch what a look skipped before: a destination or library folder gone
  from under their folder, a folder newly watched; `RuleService.save` marks them fresh).
- Where files go: `destinations` (the library folders and the own folder destination of every
  on rule without a refusal: never watched) and `record_roots` (what move records are about:
  those, plus such Stays rules' watched folders).
- The checks on each rule's row: `rule_notes` (refusals, then warnings, then info, translated)
  and `refusal` (why Save can't go on). Of the refusals AirGrabber's rule gets only "choose the
  folder to move them to": its clashing folders are "overlap", not watched.
- A queued file's way: `route_for_item` → `Route` (its rule as it is now, else as it was when it
  took the file, else the one that fits it now; the preset by name, the rule's extras applied;
  the folder its name starts in: the rule's own folder, the library folder, or for a Stays rule
  the watched folder) and `route_problem` (why it can't go: no rule takes it, in a watched
  folder or not any more (`in_watched_folder`), its library folder not chosen). app/forge/core/auto.py plans its runs from these.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from app.forge.core.airgrabber_folders import airgrabber_uses
from app.forge.core.library import inside, library_folder, library_folders
from app.forge.models.media import MediaKind, ParsedMedia
from app.forge.models.preset import Preset, PresetOptions
from app.forge.models.rules import Rule, RuleMode, RuleTarget
from app.forge.models.settings import ForgeSettings
from app.i18n import N_, t

if TYPE_CHECKING:  # the queue loads the parser and matcher, which the rule rows don't need
    from app.forge.core.queue import QueueItem

# -- texts (the rule's row in Settings › Sorting) --

REFUSE_IN_DESTINATION = N_(
    "✕ Can't save: the folder it watches is inside the folder it moves to, so files would be "
    "found again and again."
)
REFUSE_HOLDS_DESTINATION = N_(
    "✕ Can't save: the folder it moves to is inside the folder it watches, so files would be "
    "found again and again."
)
REFUSE_IN_LIBRARY = N_(
    "✕ Can't save: the folder it watches is inside a library folder, so files would be found "
    "again and again."
)
REFUSE_NO_FOLDER = N_("✕ Can't save: choose the folder this rule watches.")
REFUSE_NO_DESTINATION = N_("✕ Can't save: choose the folder to move them to.")
UNREACHABLE_FOLDER = N_("⚠ Can't reach {folder} right now.")
UNREACHABLE_FILMS = N_(
    "⚠ Can't reach {folder} right now. Films wait in the queue until it's back."
)
UNREACHABLE_SHOWS = N_(
    "⚠ Can't reach {folder} right now. Shows wait in the queue until it's back."
)
UNREACHABLE_FILES = N_(
    "⚠ Can't reach {folder} right now. Files wait in the queue until it's back."
)
NEVER_REACHED = N_(
    "⚠ This rule never gets a file: rule {n} above takes the same files first. Drag it higher, "
    "or change one of them."
)
PRESET_GONE = N_("⚠ Its preset is gone: choose another.")
NO_SERIES_FOLDER = N_("⚠ Choose your Series folder under Folders.")
NO_MOVIES_FOLDER = N_("⚠ Choose your Movies folder under Folders.")
MOVED_INTO = N_(
    "Rule {n} moves files into this folder. MediaForge won't pick them up again here."
)
CANT_SAVE = N_("Rule {n} can't be saved yet. Its row says why.")

# -- texts (why a queued file can't go: translated where they are handed to the queue) --

NOT_WATCHED = N_("It isn't in a watched folder any more.")
NO_RULE_TAKES = N_("No rule takes this file.")  # in a watched folder, but no rule fits it
NO_SERIES_HOLD = N_("Choose the Series library folder.")
NO_MOVIES_HOLD = N_("Choose the Movies library folder.")

_4K = re.compile(r"(?<![a-z0-9])(2160p|4k|uhd)(?![a-z0-9])", re.I)


def rules_from_settings(settings: ForgeSettings) -> list[Rule]:
    """One rule per watched folder, in order, then AirGrabber's rule: each takes anything at any
    quality, looks in subfolders and moves shows / films with the Series / Movies preset to
    their library folder; Automatic only while renaming on its own is On, else Manual. Every
    rule is on (AirGrabber's follows `use_airgrabber_downloads` anyway) and not fresh (the
    carry-over stores them fresh: `carry_rules`)."""
    mode: RuleMode = "auto" if settings.automatic == "on" else "manual"
    shows = RuleTarget(preset=settings.series_preset)
    films = RuleTarget(preset=settings.movies_preset)

    def rule(folder: str, *, airgrabber: bool = False) -> Rule:
        return Rule(folder=folder, airgrabber=airgrabber, shows=shows, films=films, mode=mode)

    return [*(rule(folder) for folder in settings.watch_folders), rule("", airgrabber=True)]


# -- matching ---------------------------------------------------------------------------------------


class WatchedRule(NamedTuple):
    """One folder a rule watches: AirGrabber's rule has one entry per folder of AirGrabber's."""

    rule: Rule
    number: int  # its place in the list, from 1
    folder: Path


def _key(folder: str | Path) -> str:
    """A folder as Windows compares it (case, slashes, a trailing separator)."""
    return os.path.normcase(str(folder)).rstrip("\\/")


def is_4k(parsed: ParsedMedia) -> bool:
    """Whether the file's name says 2160p / 4K / UHD; a name silent on resolution is not 4K."""
    return _4K.search(parsed.source_name) is not None


def fits(rule: Rule, parsed: ParsedMedia) -> bool:
    """Whether `rule` takes the file: its kind as the name reads (an SxxEyy is a show), then
    its quality."""
    if parsed.kind not in rule.kinds():
        return False
    if rule.quality == "any":
        return True
    return is_4k(parsed) == (rule.quality == "4k")


def _destination(rule: Rule, kind: MediaKind, settings: ForgeSettings) -> str:
    """Where `rule` moves `kind`: its library folder or its own folder; "" for a Stays rule or
    one not chosen."""
    if rule.stays:
        return ""
    target = rule.target(kind)
    if target.to == "folder":
        return target.folder
    return settings.series_folder if kind is MediaKind.EPISODE else settings.movies_folder


def _own_destinations(rule: Rule) -> list[tuple[MediaKind, str]]:
    """The folder destinations (`to="folder"`, chosen) of the kinds `rule` takes."""
    if rule.stays:
        return []
    return [
        (kind, rule.target(kind).folder)
        for kind in rule.kinds()
        if rule.target(kind).to == "folder" and rule.target(kind).folder
    ]


def _refusals(rule: Rule, settings: ForgeSettings) -> list[str]:
    """Why `rule` can't be saved (untranslated texts, in their order). AirGrabber's rule only
    without its folder to move to: its folders are AirGrabber's, a clash makes them "overlap"."""
    no_destination = not rule.stays and any(
        rule.target(kind).to == "folder" and not rule.target(kind).folder for kind in rule.kinds()
    )
    if rule.airgrabber:
        return [REFUSE_NO_DESTINATION] if no_destination else []
    found: list[str] = []
    if not rule.folder:
        found.append(REFUSE_NO_FOLDER)
    if no_destination:
        found.append(REFUSE_NO_DESTINATION)
    if not rule.folder:
        return found
    watched = Path(rule.folder)
    if any(inside(watched, library) for library in library_folders(settings)):
        found.append(REFUSE_IN_LIBRARY)
    if any(inside(watched, Path(folder)) for _kind, folder in _own_destinations(rule)):
        found.append(REFUSE_IN_DESTINATION)
    goes_to = [Path(f) for f in (_destination(rule, k, settings) for k in rule.kinds()) if f]
    if any(inside(dest, watched) and not inside(watched, dest) for dest in goes_to):
        found.append(REFUSE_HOLDS_DESTINATION)
    return found


def _folders(rule: Rule, settings: ForgeSettings, destinations: Sequence[Path]) -> list[str]:
    """The folders `rule` watches as given (AirGrabber's: those of AirGrabber's in use)."""
    if rule.airgrabber:
        return [
            use.folder.path
            for use in airgrabber_uses(settings, destinations)
            if use.use == "watched"
        ]
    return [rule.folder] if rule.folder else []


def _watched(rules: Sequence[Rule], settings: ForgeSettings) -> list[tuple[Rule, int, str]]:
    dests = destinations(rules, settings)
    return [
        (rule, number, folder)
        for number, rule in enumerate(rules, 1)
        if _moving(rule, settings)
        for folder in _folders(rule, settings, dests)
    ]


def watch_list(rules: Sequence[Rule], settings: ForgeSettings) -> list[WatchedRule]:
    """What is watched, in the rule order: each on rule without a refusal with its folder;
    AirGrabber's rule once per folder of AirGrabber's that `airgrabber_uses` marks watched."""
    return [
        WatchedRule(rule, number, Path(folder))
        for rule, number, folder in _watched(rules, settings)
    ]


def holds(watched: WatchedRule, file: Path) -> bool:
    """Whether `file` is in the entry's folder: anywhere below it, or only directly in it for a
    rule that doesn't look in subfolders (Windows' comparison)."""
    if not inside(file, watched.folder):
        return False
    return watched.rule.subfolders or _key(file.parent) == _key(watched.folder)


def rule_for(file: Path, parsed: ParsedMedia, watch: Sequence[WatchedRule]) -> WatchedRule | None:
    """The first entry that holds `file` and whose rule fits it; None when no rule takes it."""
    return next((w for w in watch if holds(w, file) and fits(w.rule, parsed)), None)


def listing_roots(watch: Sequence[WatchedRule]) -> list[Path]:
    """The distinct folders to list, in order, without those inside another listed one (they
    come with its listing)."""
    distinct: dict[str, Path] = {}
    for entry in watch:
        distinct.setdefault(_key(entry.folder), entry.folder)
    folders = list(distinct.values())
    return [f for f in folders if not any(o is not f and inside(f, o) for o in folders)]


def watched_folders(rules: Sequence[Rule], settings: ForgeSettings) -> list[str]:
    """The distinct folders `watch_list` watches, in order, as given."""
    distinct: dict[str, str] = {}
    for _rule, _number, folder in _watched(rules, settings):
        distinct.setdefault(_key(folder), folder)
    return list(distinct.values())


def newly_watched(
    before: ForgeSettings, after: ForgeSettings, rules: Sequence[Rule]
) -> list[str]:
    """The folders AirGrabber's rule watches with `after` that it didn't with `before` (its
    on / off each side's `airgrabber_on`): the ones AirGrabber's settings brought in."""

    def airgrabbers(settings: ForgeSettings) -> list[str]:
        on = settings.airgrabber_on
        shown = [
            rule.model_copy(update={"on": on}) if rule.airgrabber and rule.on != on else rule
            for rule in rules
        ]
        return [folder for rule, _n, folder in _watched(shown, settings) if rule.airgrabber]

    old = {_key(folder) for folder in airgrabbers(before)}
    found: dict[str, str] = {}
    for folder in airgrabbers(after):
        if _key(folder) not in old:
            found.setdefault(_key(folder), folder)
    return list(found.values())


def uncovered(
    before_rules: Sequence[Rule],
    before: ForgeSettings,
    after_rules: Sequence[Rule],
    after: ForgeSettings,
) -> list[int]:
    """The rules (ids, in order) that watch with `after` something a look skipped with `before`,
    so their next look queues what is there as Manual once (nothing unseen moves): a folder an
    on rule didn't watch (AirGrabber's from "overlap", a refusal gone), or a destination (a
    library folder, a rule's own folder) that stopped being one, inside a folder the rule
    watches or holding it (a rule deleted, switched off or re-pointed, a library folder
    changed). Rules
    without an id (new ones) are left out: they are fresh anyway."""
    new_dests = destinations(after_rules, after)
    gone = [
        old
        for old in destinations(before_rules, before)
        if not any(inside(old, dest) for dest in new_dests)
    ]
    seen = {(rule.id, _key(folder)) for rule, _n, folder in _watched(before_rules, before)}
    found: dict[int, None] = {}
    for rule, _n, folder in _watched(after_rules, after):
        if not rule.id:
            continue
        watched = Path(folder)
        if (rule.id, _key(folder)) not in seen or any(
            inside(watched, dest)  # its whole folder was skipped
            or (inside(dest, watched) and (rule.subfolders or _key(dest) == _key(watched)))
            for dest in gone
        ):
            found.setdefault(rule.id)
    return list(found)


# -- where files go ---------------------------------------------------------------------------------


def _moving(rule: Rule, settings: ForgeSettings) -> bool:
    """Whether `rule` runs: on and without a refusal."""
    return rule.on and not _refusals(rule, settings)


def destinations(rules: Sequence[Rule], settings: ForgeSettings) -> list[Path]:
    """The folders files are moved to, never watched: the chosen library folders, plus the own
    folder of the kinds it takes of every rule that runs (on, without a refusal: a refused rule
    moves nothing); distinct. A Stays rule adds nothing."""
    found: dict[str, Path] = {}
    for library in library_folders(settings):
        found.setdefault(_key(library), library)
    for rule in rules:
        if _moving(rule, settings):
            for _kind, folder in _own_destinations(rule):
                found.setdefault(_key(folder), Path(folder))
    return list(found.values())


def record_roots(
    rules: Sequence[Rule], settings: ForgeSettings
) -> list[tuple[Path, frozenset[MediaKind]]]:
    """The folders move records are about, with the kind(s) each one gets: the library folders
    (`moves.library_roots`), and of every rule that runs (on, without a refusal) its own folder
    destinations (its target's kind) or, for a Stays rule, its watched folders (the kinds the
    rule takes); merged per folder."""
    # Here, not at the top: moves.py loads forge.db's package, which the UI importing this
    # module (the rule rows) doesn't need.
    from app.forge.core.moves import library_roots

    roots: dict[str, tuple[Path, set[MediaKind]]] = {}

    def add(folder: Path, kinds: Collection[MediaKind]) -> None:
        roots.setdefault(_key(folder), (folder, set()))[1].update(kinds)

    for folder, kinds in library_roots(settings):
        add(folder, kinds)
    dests = destinations(rules, settings)
    for rule in rules:
        if not _moving(rule, settings):
            continue
        for kind, dest in _own_destinations(rule):
            add(Path(dest), (kind,))
        if rule.stays:
            for watched in _folders(rule, settings, dests):
                add(Path(watched), rule.kinds())
    return [(folder, frozenset(kinds)) for folder, kinds in roots.values()]


# -- the checks on a rule's row ---------------------------------------------------------------------


class RuleNote(NamedTuple):
    """One line under a rule's row."""

    level: Literal["refuse", "warn", "info"]
    text: str  # translated


def _covers(above: Rule, above_folders: Sequence[str], rule: Rule, folder: str) -> bool:
    """Whether `above` takes every file `rule` would get from `folder` first."""
    if above.take != "anything" and above.take != rule.take:
        return False
    if above.quality != "any" and above.quality != rule.quality:
        return False
    for other in above_folders:
        same = _key(other) == _key(folder)
        if same and (above.subfolders or not rule.subfolders):
            return True
        if not same and above.subfolders and inside(Path(folder), Path(other)):
            return True
    return False


def _unreachable_destinations(
    rule: Rule, settings: ForgeSettings, unreachable: set[str]
) -> list[str]:
    """The texts for the rule's destinations the last look couldn't reach, one per folder."""
    away: dict[str, tuple[str, set[MediaKind]]] = {}
    for kind in rule.kinds():
        folder = _destination(rule, kind, settings)
        if folder and _key(folder) in unreachable:
            away.setdefault(_key(folder), (folder, set()))[1].add(kind)
    texts: list[str] = []
    for folder, kinds in away.values():
        if len(kinds) > 1:
            text = UNREACHABLE_FILES
        elif MediaKind.EPISODE in kinds:
            text = UNREACHABLE_SHOWS
        else:
            text = UNREACHABLE_FILMS
        texts.append(t(text, folder=folder))
    return texts


def _moves_into(other: Rule, watched: Sequence[str], settings: ForgeSettings) -> bool:
    """Whether `other` moves files where a rule watching `watched` would find them, or where a
    look skips them: one of its own folder destinations is in a watched folder, or a watched
    folder is in one of its destinations (its own folders and the library folders it moves to).
    A Stays rule moves nothing (its watched folder is its destination, kept by the memory)."""
    own = [Path(dest) for _kind, dest in _own_destinations(other)]
    goes_to = [Path(f) for f in (_destination(other, k, settings) for k in other.kinds()) if f]
    return any(
        any(inside(dest, Path(folder)) for dest in own)
        or any(inside(Path(folder), dest) for dest in goes_to)
        for folder in watched
    )


def rule_notes(
    rules: Sequence[Rule],
    settings: ForgeSettings,
    *,
    presets: Collection[str],
    unreachable: Collection[str] = (),
) -> list[list[RuleNote]]:
    """Each rule's notes, in the rule order: refusals (any rule), then for an on rule the
    warnings (its watched folder or a destination the last look couldn't reach (`unreachable`:
    normcased folders), never reached because of a rule above, its preset gone, its library
    folder not chosen) and the info (another rule that runs moves files into its folder, or its
    folder lies in what another rule moves to: a look skips destinations, `_moves_into`)."""
    away = {_key(folder) for folder in unreachable}
    dests = destinations(rules, settings)
    refused = [_refusals(rule, settings) for rule in rules]
    folders = [_folders(rule, settings, dests) for rule in rules]
    active = [rule.on and not refused[i] for i, rule in enumerate(rules)]
    notes: list[list[RuleNote]] = []
    for i, rule in enumerate(rules):
        mine = [RuleNote("refuse", t(text)) for text in refused[i]]
        if not rule.on:
            notes.append(mine)
            continue
        warn = [
            t(UNREACHABLE_FOLDER, folder=folder) for folder in folders[i] if _key(folder) in away
        ]
        warn += _unreachable_destinations(rule, settings, away)
        coverers: list[int] = []
        for folder in folders[i]:
            by = next(
                (
                    j
                    for j in range(i)
                    if active[j] and _covers(rules[j], folders[j], rule, folder)
                ),
                None,
            )
            if by is None:
                break
            coverers.append(by)
        if folders[i] and len(coverers) == len(folders[i]):
            warn.append(t(NEVER_REACHED, n=coverers[0] + 1))
        if any(rule.target(kind).preset not in presets for kind in rule.kinds()):
            warn.append(t(PRESET_GONE))
        if not rule.stays:
            for kind in rule.kinds():
                if rule.target(kind).to == "library" and library_folder(kind, settings) is None:
                    warn.append(
                        t(NO_SERIES_FOLDER if kind is MediaKind.EPISODE else NO_MOVIES_FOLDER)
                    )
        mine += [RuleNote("warn", text) for text in warn]
        mine += [
            RuleNote("info", t(MOVED_INTO, n=j + 1))
            for j, other in enumerate(rules)
            if j != i and active[j] and _moves_into(other, folders[i], settings)
        ]
        notes.append(mine)
    return notes


def refusal(rules: Sequence[Rule], settings: ForgeSettings) -> str | None:
    """Why Save can't go on (translated): the first rule with a refusal; None when none has."""
    for number, rule in enumerate(rules, 1):
        if _refusals(rule, settings):
            return t(CANT_SAVE, n=number)
    return None


# -- a queued file's way ----------------------------------------------------------------------------


class Route(NamedTuple):
    """Where one queued file goes and how it is named, by its rule."""

    rule: Rule
    number: int  # its place in the list, from 1; 0 when the rule is gone (its snapshot)
    kind: MediaKind
    preset: Preset | None  # None: the rule's preset is gone
    options: PresetOptions  # the preset's, with the rule's extras; PresetOptions() when gone
    # Where the name starts: the rule's own folder, the library folder, or for a Stays rule the
    # watched folder. None: its library folder isn't chosen.
    root: Path | None
    moves: bool  # False for a Stays rule


def _route(
    rule: Rule, number: int, item: QueueItem, settings: ForgeSettings, presets: Sequence[Preset]
) -> Route:
    kind = item.media.kind
    target = rule.target(kind)
    preset = next((p for p in presets if p.name == target.preset), None)
    options = (
        preset.options.model_copy(update=target.extras) if preset is not None else PresetOptions()
    )
    root: Path | None
    if rule.stays:
        root = item.watched
    elif target.to == "folder":
        root = Path(target.folder) if target.folder else None
    else:
        root = library_folder(kind, settings)
    return Route(rule, number, kind, preset, options, root, not rule.stays)


def route_for_item(
    item: QueueItem, rules: Sequence[Rule], settings: ForgeSettings, presets: Sequence[Preset]
) -> Route | None:
    """`item`'s route: by its rule as it is now (`item.rule`'s id among `rules`), else by the
    rule as it was when it took the file (deleted since: number 0), else (no snapshot: added by
    hand or by an old caller) by the rule that fits it now (`rule_for`); None when none does.
    The preset is looked up by name in `presets` only: a gone preset is None, never another."""
    snapshot = item.rule
    if snapshot is not None:
        if snapshot.id:
            for number, rule in enumerate(rules, 1):
                if rule.id == snapshot.id:
                    return _route(rule, number, item, settings, presets)
        return _route(snapshot, 0, item, settings, presets)
    found = rule_for(item.path, item.parsed, watch_list(rules, settings))
    if found is None:
        return None
    return _route(found.rule, found.number, item, settings, presets)


def in_watched_folder(path: Path, rules: Sequence[Rule], settings: ForgeSettings) -> bool:
    """Whether `path` lies in a folder the rules watch now (`watched_folders`)."""
    return any(inside(path, Path(folder)) for folder in watched_folders(rules, settings))


def route_problem(route: Route | None, kind: MediaKind, *, watched: bool = False) -> str | None:
    """Why a file with `route` can't go (translated): no rule takes it ("No rule takes this
    file." when it is in a folder the rules watch, `watched`: `in_watched_folder`; else "It
    isn't in a watched folder any more."), or its library folder isn't chosen; None when it
    can."""
    if route is None:
        return t(NO_RULE_TAKES if watched else NOT_WATCHED)
    if route.root is None:
        return t(NO_SERIES_HOLD if kind is MediaKind.EPISODE else NO_MOVIES_HOLD)
    return None
