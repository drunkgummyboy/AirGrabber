"""The words for a rule (docs/forge/2026-11-18-rules-design.md), pure: no Tk.

- `made_name(rule)`: the name a rule left without one gets from its choices ("4K films from
  Downloads"; without a folder yet "4K films"; AirGrabber's rule: "AirGrabber's downloads"),
  `last_name(folder)` (the folder's last name, as rules and the editor show it), and
  `name_of(rule)`: its own name, else that one (the rule's tooltip, the editor's hint, the
  queue's label).
- `check_lines(groups)`: the rule editor's live check (app/forge/core/rule_check.py's
  CheckGroup) as sentences, the files that fit first ("2 films fit this rule.", "4 episodes
  don't: they're shows. Rule 3 takes them.").
- `row_look(rule, settings)`: a rule's row in Settings › Auto-rename's list (look A, compact
  rows) as words: 🔒 or not, the folder's last name, the filter's chips, the preset(s),
  where the files go, Automatic / Manual, the tooltip (`name_of`) and whether it is greyed (off).
- `unfit_line(unfit)`: the quiet line under the list for a folder's files no rule takes (the
  renamer's `unfit()`).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, NamedTuple

from app.forge.models.media import MediaKind
from app.forge.models.rules import Rule
from app.forge.models.settings import ForgeSettings
from app.i18n import N_, t, tn

if TYPE_CHECKING:  # rule_check / auto load the queue and the renamer; these texts don't need them
    from app.forge.core.auto import Unfit
    from app.forge.core.rule_check import CheckGroup, CheckReason

AIRGRABBERS = N_("AirGrabber's downloads")

# A row's chips and words (Settings › Auto-rename's list, look A).
CHIP_TAKES: dict[str, str] = {
    "anything": N_("Anything"),
    "shows": N_("Shows"),
    "films": N_("Films"),
}
CHIP_QUALITIES: dict[str, str] = {"4k": N_("4K"), "not_4k": N_("Not 4K")}
TOP_FOLDER_ONLY = N_("Top folder only")
SORTED_HERE = N_("sorted in this folder")
NO_LIBRARY: dict[MediaKind, str] = {
    MediaKind.EPISODE: N_("your Series folder"),
    MediaKind.MOVIE: N_("your Movies folder"),
}
AUTOMATIC = N_("Automatic")
MANUAL = N_("Manual")
UNFIT: tuple[str, str] = (
    N_("{n} file in {folder} fits no rule and is left alone."),
    N_("{n} files in {folder} fit no rule and are left alone."),
)

# (take, quality) → the made name; {folder} = the watched folder's last name.
_MADE: dict[tuple[str, str], str] = {
    ("anything", "any"): N_("Everything from {folder}"),
    ("shows", "any"): N_("Shows from {folder}"),
    ("films", "any"): N_("Films from {folder}"),
    ("anything", "4k"): N_("Everything in 4K from {folder}"),
    ("shows", "4k"): N_("4K shows from {folder}"),
    ("films", "4k"): N_("4K films from {folder}"),
    ("anything", "not_4k"): N_("Everything not in 4K from {folder}"),
    ("shows", "not_4k"): N_("Shows not in 4K from {folder}"),
    ("films", "not_4k"): N_("Films not in 4K from {folder}"),
}
# The same without a folder (a new rule before its folder is chosen).
_MADE_BARE: dict[tuple[str, str], str] = {
    ("anything", "any"): N_("Everything"),
    ("shows", "any"): N_("Shows"),
    ("films", "any"): N_("Films"),
    ("anything", "4k"): N_("Everything in 4K"),
    ("shows", "4k"): N_("4K shows"),
    ("films", "4k"): N_("4K films"),
    ("anything", "not_4k"): N_("Everything not in 4K"),
    ("shows", "not_4k"): N_("Shows not in 4K"),
    ("films", "not_4k"): N_("Films not in 4K"),
}

# reason (and kind) → (one, more); {n} the count, {rule} the other rule's number.
type _Pair = tuple[str, str]
_FITS: dict[MediaKind, _Pair] = {
    MediaKind.MOVIE: (N_("{n} film fits this rule."), N_("{n} films fit this rule.")),
    MediaKind.EPISODE: (N_("{n} episode fits this rule."), N_("{n} episodes fit this rule.")),
}
_TAKEN_ABOVE: _Pair = (
    N_("{n} file doesn't: rule {rule} above takes it."),
    N_("{n} files don't: rule {rule} above takes them."),
)
_IN_SUBFOLDER: _Pair = (
    N_("{n} file doesn't: it's in a folder inside this one."),
    N_("{n} files don't: they're in folders inside this one."),
)
_A_SHOW: _Pair = (N_("{n} episode doesn't: it's a show."), N_("{n} episodes don't: they're shows."))
_A_FILM: _Pair = (N_("{n} film doesn't: it's a film."), N_("{n} films don't: they're films."))
_NOT_4K: dict[MediaKind, _Pair] = {
    MediaKind.MOVIE: (N_("{n} film doesn't: it's not 4K."), N_("{n} films don't: they're not 4K.")),
    MediaKind.EPISODE: (
        N_("{n} episode doesn't: it's not 4K."),
        N_("{n} episodes don't: they're not 4K."),
    ),
}
_IS_4K: dict[MediaKind, _Pair] = {
    MediaKind.MOVIE: (N_("{n} film doesn't: it's 4K."), N_("{n} films don't: they're 4K.")),
    MediaKind.EPISODE: (N_("{n} episode doesn't: it's 4K."), N_("{n} episodes don't: they're 4K.")),
}
_TAKES: _Pair = (N_("Rule {rule} takes it."), N_("Rule {rule} takes them."))
_NO_RULE: _Pair = (N_("No rule takes it."), N_("No rule takes them."))
# Said of files whatever their kind: one line for both kinds.
_ANY_KIND: frozenset[str] = frozenset({"taken_above", "in_subfolder"})


def last_name(folder: str) -> str:
    """A folder's last name ("Downloads"); a drive or share root as it is ("E:\\")."""
    return PureWindowsPath(folder).name or folder


def made_name(rule: Rule) -> str:
    """The name made from `rule`'s choices: what it takes from which folder (its last name);
    only what it takes while it has no folder yet."""
    if rule.airgrabber:
        return t(AIRGRABBERS)
    if not rule.folder.strip():
        return t(_MADE_BARE[rule.take, rule.quality])
    return t(_MADE[rule.take, rule.quality], folder=last_name(rule.folder))


def name_of(rule: Rule) -> str:
    """`rule`'s own name, else the one made from its choices."""
    return rule.name.strip() or made_name(rule)


def _pair(reason: CheckReason, kind: MediaKind) -> _Pair:
    match reason:
        case "fits":
            return _FITS[kind]
        case "taken_above":
            return _TAKEN_ABOVE
        case "in_subfolder":
            return _IN_SUBFOLDER
        case "a_show":
            return _A_SHOW
        case "a_film":
            return _A_FILM
        case "not_4k":
            return _NOT_4K[kind]
        case "is_4k":
            return _IS_4K[kind]


def check_lines(groups: Sequence[CheckGroup]) -> list[str]:
    """One sentence per group, in the groups' order; a miss says which rule takes the files
    instead (a rule above already says so itself). Files said of whatever their kind (a rule
    above takes them, in a folder inside) are one line per other rule, at the first's place."""
    merged: dict[tuple[str, MediaKind | None, int | None], int] = {}
    for group in groups:
        kind = None if group.reason in _ANY_KIND else group.kind
        key = (group.reason, kind, group.other)
        merged[key] = merged.get(key, 0) + group.count
    lines: list[str] = []
    for group in groups:
        kind = None if group.reason in _ANY_KIND else group.kind
        count = merged.pop((group.reason, kind, group.other), None)
        if count is None:
            continue  # merged into an earlier line
        one, more = _pair(group.reason, group.kind)
        line = tn(one, more, count, rule=group.other)
        if group.reason not in ("fits", "taken_above"):
            if group.other is not None:
                then = tn(_TAKES[0], _TAKES[1], count, rule=group.other)
            else:
                then = tn(_NO_RULE[0], _NO_RULE[1], count)
            line = f"{line} {then}"
        lines.append(line)
    return lines


# -- the rules list's rows ---------------------------------------------------------------------


class RowLook(NamedTuple):
    """A rule's row in the list, as words (translated)."""

    locked: bool  # AirGrabber's rule: 🔒 instead of the grip, no Delete
    folder: str  # the watched folder's last name; AirGrabber's: "AirGrabber's downloads"
    chips: tuple[str, ...]  # what it takes, the quality (none for any), "Top folder only"
    preset: str  # the taken kinds' preset names, " / " between them
    destination: str  # drawn after "→ "
    mode: str  # "Automatic" / "Manual"
    tip: str  # the row's tooltip: `name_of`
    dim: bool  # off: greyed


def _distinct(texts: Sequence[str]) -> list[str]:
    """`texts` without repeats, in their order."""
    return list(dict.fromkeys(texts))


def _goes_to(rule: Rule, kind: MediaKind, settings: ForgeSettings) -> str:
    target = rule.target(kind)
    if target.to == "folder":
        return target.folder
    folder = settings.series_folder if kind is MediaKind.EPISODE else settings.movies_folder
    return folder or t(NO_LIBRARY[kind])


def row_look(rule: Rule, settings: ForgeSettings) -> RowLook:
    """`rule`'s row: where a kind goes is its own folder, or its library folder's path ("your
    Series folder" while that isn't chosen); a Stays rule's files are "sorted in this folder"."""
    chips = [t(CHIP_TAKES[rule.take])]
    if rule.quality in CHIP_QUALITIES:
        chips.append(t(CHIP_QUALITIES[rule.quality]))
    if not rule.subfolders:
        chips.append(t(TOP_FOLDER_ONLY))
    kinds = rule.kinds()
    preset = " / ".join(_distinct([rule.target(kind).preset for kind in kinds]))
    if rule.stays:
        destination = t(SORTED_HERE)
    else:
        destination = " / ".join(_distinct([_goes_to(rule, kind, settings) for kind in kinds]))
    return RowLook(
        locked=rule.airgrabber,
        folder=t(AIRGRABBERS) if rule.airgrabber else last_name(rule.folder),
        chips=tuple(chips),
        preset=preset,
        destination=destination,
        mode=t(AUTOMATIC if rule.mode == "auto" else MANUAL),
        tip=name_of(rule),
        dim=not rule.on,
    )


def unfit_line(unfit: Unfit) -> str:
    """The line for a folder's files no rule takes ("2 files in Downloads fit no rule and are
    left alone.")."""
    return tn(UNFIT[0], UNFIT[1], len(unfit.files), folder=last_name(str(unfit.folder)))
