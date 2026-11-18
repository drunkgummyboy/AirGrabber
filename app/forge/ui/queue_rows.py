"""The queue's rows: the pure, display-free part of the queue (ported from MediaForge's
ui/queue_view.py; the view itself is app/forge/ui/queue_view.py).

Files become `Row`s for the fast list (app/forge/ui/widgets/virtual_list.py): shows and films in
one list, one band per show with its episodes as short lines under it, one band per film with
its file. Every band has a state (`Row.state`: needs, ready or done, its colour) and short words
saying why (`Row.state_text`): "Already in your library", "Not sure which film", "Waiting for
you", "Couldn't forge", "Ready to forge" or "Forged 14:32" (renamed today, faded, with its Undo).
Its preset chip has the caption "Name style" and, as its tip, the name this band's first file
gets ("Dune (2021)\\Dune (2021).mkv").

A band that needs the owner asks in a sentence what it needs to know (`Row.ask`, the owner's
choice "B", mockups/forge-band-buttons.html) and its buttons are the answers, the main one last
(filled):
- already in the library (`held_in_library`): "You already have this film." with the two copies'
  quality and size after it (app/forge/core/replace_facts.py, `facts`); "Keep the one I have ▾"
  (the Leave-it-alone menu) and "Use this one instead" (`REPLACE_ACTION`);
- not sure what it is (TMDB isn't sure, or a watched download held because its match is wrong
  or missing, `is_unsure`): "Is this Dune (2021)?"; "No, pick the right film…" (the match picker)
  and "Yes, forge it" (`ACCEPT_ACTION`); without a guess "Which film is this?" and "Pick the
  film…";
- held back for another reason, or failed: the reason as its subtitle; "Leave it alone ▾" and
  "Forge this" (held) or "Try again" (failed).
A ready band has "Forge this" (only under the pointer). Small extras are quiet links (`Row.quiet`):
"📂 Show file" on every band, "Wrong film?" / "Wrong show?" (the picker), and a ready or unsure
band's "Leave it alone ▾". A done band keeps CC ▾ and Undo.

A watched download's band (`WatchedLook`, from its rule's route) says where its rule sends it
("📦 Moves to Movies 4K", or for a rule that sorts its files where they are "📁 Sorted in
Downloads") and which rule took it (`Row.rule`, "Rule: 4K films": a click opens that rule in
Settings; a done one "📁 Was sorted in …"). A ready one that waits says why instead of "Ready to
forge": its rule is Manual (or it was taken as Manual once), its rule's preset is gone, its rule was
changed since it took the file, or no rule takes it any more (then it names no destination).

The filter (`show`) is All (needs you first, then the rest by name, then today's done ones under
their heading), Needs you or Ready, and every count is worked out whichever is shown.

No Tk here: the rows' texts are translated when they are built (the language is set once at
startup).
"""

from __future__ import annotations

import itertools
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Literal, NamedTuple

from app.forge.core.auto import held_in_library
from app.forge.core.formatter import name_file
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.core.replace_facts import ReplaceFacts, facts_text
from app.forge.core.rules import NO_RULE_TAKES, NOT_WATCHED
from app.forge.models.media import MatchState, MediaKind
from app.forge.ui.name_diff import name_changes
from app.forge.ui.subtitle_control import CC_ACTION, CC_LABEL, CcLook
from app.forge.ui.widgets.virtual_list import Row
from app.i18n import N_, number, t, tn

_SE = re.compile(r"S\d{2,}E\d{2,}(?:-E\d{2,})?")

type Kind = Literal["series", "movies"]
type Show = Literal["all", "needs", "ready"]
type State = Literal["needs", "ready", "done"]

FINISHED = (ItemStatus.DONE, ItemStatus.SKIPPED)
NEEDS_OWNER = (ItemStatus.NEEDS_CHOICE, ItemStatus.HELD, ItemStatus.ERROR)
_WAITING = (ItemStatus.HELD, ItemStatus.ERROR)  # held back or failed, with a reason

CURRENT_NAME = N_("Current name:")  # before a line's current file name, so it isn't taken for the new one
DONE_TODAY = N_("Done today")

# A band's status: a dot and these words, in its state's colour.
IN_LIBRARY_STATUS = N_("Already in your library")
UNSURE_FILM_STATUS = N_("Not sure which film")
UNSURE_SHOW_STATUS = N_("Not sure which show")
WAITING_STATUS = N_("Waiting for you")
FAILED_STATUS = N_("Couldn't forge")
READY_STATUS = N_("Ready to forge")
DONE_STATUS = N_("Forged")
DONE_AT_STATUS = N_("Forged {time}")
NAME_STYLE = N_("Name style")  # the caption before a band's preset chip

# A band's question (`Row.ask`).
HAVE_FILM = N_("You already have this film.")
HAVE_EPISODE = N_("You already have this episode.")
HAVE_EPISODES = N_("You already have {n} of these episodes.")
IS_THIS = N_("Is this {title}?")
UNSURE_FILM = N_("The file name and the film database don't fully agree.")
UNSURE_SHOW = N_("The file name and the show database don't fully agree.")
WHICH_FILM = N_("Which film is this?")
WHICH_SHOW = N_("Which show is this?")

# A band's buttons: (id, words). The ids are QueueView._on_band_action's.
EXPLORER_ACTION = "explorer"  # every band's first link
SHOW_FILE = N_("📂 Show file")
OPEN_IN_EXPLORER = N_("Open in Explorer")
FORGE_ACTION = "rename"  # forge only this band
FORGE_THIS = N_("Forge this")
TRY_AGAIN = N_("Try again")
CHOOSE_ACTION = "choose"  # the match picker
WRONG_FILM = N_("Wrong film?")
WRONG_SHOW = N_("Wrong show?")
PICK_OTHER_FILM = N_("No, pick the right film…")
PICK_OTHER_SHOW = N_("No, pick the right show…")
PICK_FILM = N_("Pick the film…")
PICK_SHOW = N_("Pick the show…")
# Yes to the guess the band asks about: taken as the match picker would, then forged
# (QueueView.accept_band).
ACCEPT_ACTION = "accept"
YES_FORGE = N_("Yes, forge it")
IGNORE_ACTION = "ignore"  # a menu: Auto-rename leaves the file (or its folder) alone
LEAVE_ALONE = N_("Leave it alone ▾")
KEEP_MINE = N_("Keep the one I have ▾")
LEAVE_TIP = N_("Takes it off the list. Auto-rename won't touch it again.")
# A band held as already in the library: the download takes the library copy's place, which
# moves aside (QueueView.replace_band).
REPLACE_ACTION = "replace"
REPLACE_LABEL = N_("Use this one instead")
REPLACE_TIP = N_(
    "The one in your library moves to the Duplicates folder. Nothing is deleted; Undo puts "
    "both back."
)

# Where a band's files go, after its subtitle. A glyph and a colour apart, to see it at a glance.
MOVES_TO = N_("📦 Moves to {folder}")
MOVED_TO = N_("📦 Moved to {folder}")
STAYS = N_("📁 Stays in its folder")
STAYS_IN = N_("📁 Stays here, in {folder}")
STAYED = N_("📁 Stayed in its folder")
STAYED_IN = N_("📁 Stayed here, in {folder}")
NO_SERIES_FOLDER = N_("⚠ Choose your Series folder in Settings › Auto-rename")
NO_MOVIES_FOLDER = N_("⚠ Choose your Movies folder in Settings › Auto-rename")
SORTED_IN = N_("📁 Sorted in {folder}")  # a rule that sorts its files inside its watched folder
SORTED_DONE = N_("📁 Was sorted in {folder}")

# Which rule took a watched download (`Row.rule`), and why a ready one waits.
RULE_LABEL = N_("Rule: {name}")
MANUAL_WAITS = N_("Waits for you: rule “{name}” is Manual")
RULE_CHANGED = N_("Its rule was changed: check it")
PRESET_GONE = N_("⚠ The preset of rule “{name}” is gone: choose another")

EMPTY_TITLE = N_("Drop video files or folders here")
EMPTY_HINT = N_("or add them with the buttons above. Nothing is renamed until you click Forge.")
# The queue isn't empty but nothing matches the filter: (title, hint) per filter.
_EMPTY_FILTER: dict[Show, tuple[str, str]] = {
    "needs": (N_("Nothing needs you"), N_("Everything in the queue can be renamed.")),
    "ready": (N_("Nothing is ready yet"), N_("The files in the queue need you first.")),
}


def _files(n: int) -> str:
    return tn("{n} file", "{n} files", n)


type Waits = Literal["", "manual", "changed", "no_preset", "unwatched", "no_rule"]


class WatchedLook(NamedTuple):
    """How a watched download's band shows, by its rule's route: its naming rule, the folder its
    name starts in (None: its library folder isn't chosen), whether it moves (False: sorted inside
    its watched folder, `root`), the rule's name ("" without a rule) and why a ready one waits
    ("manual": its rule is Manual or it was taken as Manual once; "no_preset": its rule's preset
    is gone; "changed": its rule was changed since it took the file; "unwatched": it isn't in
    a watched folder any more, "no_rule": it is, but no rule takes it; either way its band names
    no destination)."""

    fmt: str
    root: Path | None
    moves: bool
    rule_name: str
    waits: Waits


def _folder_name(folder: Path) -> str:
    """A folder as a band names it: its own name, a share root's share ("Movies 4K" for
    \\\\NAS\\Movies 4K), else as written (a drive root)."""
    if folder.name:
        return folder.name
    share = folder.drive.replace("\\", "/").rstrip("/")
    if share.startswith("//"):
        return share.rsplit("/", 1)[-1]
    return str(folder)


def build_rows(
    items: Sequence[QueueItem], fmt: str, root_of: Callable[[QueueItem], Path] | None = None
) -> tuple[list[Row], dict[int, Path]]:
    """Rows grouped by destination folder, plus each item's full target path (the right-click
    confirm window's list). `root_of`: where its naming rule starts (default: beside the file)."""
    groups: dict[str, list[Row]] = {}
    labels: dict[str, str] = {}
    counts: dict[str, tuple[int, bool]] = {}
    targets: dict[int, Path] = {}
    for item in items:
        media = item.media
        rendered = name_file(fmt, media, item.path.suffix)
        root = item.root if root_of is None else root_of(item)
        targets[item.id] = root.joinpath(*rendered.parts)
        if len(rendered.parts) > 1:
            group, label = "\\".join(rendered.parts[:-1]), rendered.parts[-2]
        elif media.kind is MediaKind.EPISODE:
            group = label = media.ny
        else:
            group, label = "", t("Same folder")
        labels.setdefault(group, label)
        title = rendered.parts[-1]
        match = _SE.search(title)
        groups.setdefault(group, []).append(
            Row(
                key=item.id,
                kind="item",
                title=title,
                subtitle=item.path.name,
                badge=media.title,
                status=item.status.value,
                highlight=(match.start(), match.end()) if match else None,
            )
        )
        n, episodes = counts.get(group, (0, True))
        counts[group] = (n + 1, episodes and media.kind is MediaKind.EPISODE)
    rows: list[Row] = []
    for index, (group, members) in enumerate(groups.items(), start=1):
        n, episodes = counts[group]
        icon = "📺" if episodes else "📁"
        rows.append(
            Row(
                key=-index,
                kind="group",
                title=f"{icon} {labels[group]}",
                subtitle=tn("{n} episode", "{n} episodes", n) if episodes else _files(n),
            )
        )
        rows.extend(members)
    return rows, targets


def item_kind(item: QueueItem) -> Kind:
    """Shows or films: which preset of the preset card names a file."""
    return "series" if item.media.kind is MediaKind.EPISODE else "movies"


def item_state(item: QueueItem) -> State:
    """A file's status: renamed, waiting for the owner, or ready to rename."""
    if item.status in FINISHED:
        return "done"
    if item.status in NEEDS_OWNER:
        return "needs"
    return "ready"


@dataclass
class QueueRows:
    rows: list[Row] = field(default_factory=list[Row])
    targets: dict[int, Path] = field(default_factory=dict[int, Path])
    bands: dict[int, list[int]] = field(default_factory=dict[int, list[int]])  # band → items
    counts: dict[Show, int] = field(default_factory=dict[Show, int])  # files per filter
    kinds: dict[Kind, int] = field(default_factory=dict[Kind, int])  # files not renamed, per kind
    # A band shown that asks about a download already in the library, with one such file: its
    # item and the name it would get there (item id → both), for "Use this one instead"'s facts.
    held: dict[int, tuple[QueueItem, Path]] = field(
        default_factory=dict[int, tuple[QueueItem, Path]]
    )
    # Band → the files "Keep the one I have ▾" leaves alone: only those in the library.
    keeps: dict[int, list[int]] = field(default_factory=dict[int, list[int]])
    # Band → the TMDB id its "Is this …?" names: "Yes, forge it" takes exactly that one.
    guesses: dict[int, int] = field(default_factory=dict[int, int])

    @property
    def needs(self) -> int:
        """Files that need the owner (the "Needs you" filter)."""
        return self.counts.get("needs", 0)

    @property
    def waiting(self) -> int:
        """Files not renamed yet, whether they need a decision or just Forge: MediaForge's number."""
        return self.needs + self.counts.get("ready", 0)


def empty_texts(show: Show, *, others: bool) -> tuple[str, str]:
    """The empty list's title and hint, translated: nothing matches the filter while the queue
    has other files (`others`), or the queue is empty."""
    title, hint = EMPTY_TITLE, EMPTY_HINT
    if others and show in _EMPTY_FILTER:
        title, hint = _EMPTY_FILTER[show]
    return t(title), t(hint)


def _seasons(group: Sequence[QueueItem]) -> str:
    numbers = sorted({i.media.parsed.season for i in group if i.media.parsed.season is not None})
    if not numbers:
        return ""
    if len(numbers) == 1:
        return t("Season {n}", n=numbers[0])
    if numbers[-1] - numbers[0] == len(numbers) - 1:
        return t("Seasons {first}–{last}", first=numbers[0], last=numbers[-1])
    return t("Seasons {numbers}", numbers=", ".join(map(str, numbers)))


def _shared_folder(folders: Sequence[tuple[str, ...]]) -> str:
    """Where a band's files go: their folder ("" for none), or the part their folders share
    ("Show › …")."""
    if all(f == folders[0] for f in folders):
        return " › ".join(folders[0])
    shared: list[str] = []
    for parts in zip(*folders, strict=False):
        if any(part != parts[0] for part in parts):
            break
        shared.append(parts[0])
    return " › ".join([*shared, "…"]) if shared else t("several folders")


def _destination(
    state: State, kind: MediaKind, moves: bool, library_known: bool, folder: str
) -> tuple[str, Literal["moves", "stays", "warn"]]:
    """A band's destination text and how it shows. `folder`: where in its library, or beside
    its files, its files go."""
    if state == "needs":
        return "", "moves"
    if not moves:
        if not folder:
            return t(STAYED if state == "done" else STAYS), "stays"
        return t(STAYED_IN if state == "done" else STAYS_IN, folder=folder), "stays"
    if not library_known:
        return t(NO_SERIES_FOLDER if kind is MediaKind.EPISODE else NO_MOVIES_FOLDER), "warn"
    return t(MOVED_TO if state == "done" else MOVES_TO, folder=folder), "moves"


def episode_code(item: QueueItem) -> str:
    """S04E03 (or S01E02-E03), from the file itself rather than the naming rule."""
    parsed = item.media.parsed
    if parsed.season is None or parsed.episode is None:
        return ""
    code = f"S{parsed.season:02d}E{parsed.episode:02d}"
    if parsed.episode_end is not None and parsed.episode_end != parsed.episode:
        code += f"-E{parsed.episode_end:02d}"
    return code


def _done_on(item: QueueItem, today: date | None) -> bool:
    if item.status not in FINISHED:
        return False
    if today is None or item.finished_at is None:
        return True
    return date.fromtimestamp(item.finished_at) == today


def _film_line(group: Sequence[QueueItem]) -> str:
    year = group[0].media.year
    text = t("Film · {year}", year=year) if year else t("Film")
    return text + (f" · {_files(len(group))}" if len(group) > 1 else "")


def is_unsure(item: QueueItem) -> bool:
    """Not sure what it is: TMDB wasn't, or a watched download was held because its match is
    wrong or missing (not one already in the library)."""
    if item.status is ItemStatus.NEEDS_CHOICE:
        return True
    return (
        item.status is ItemStatus.HELD
        and not item.in_library
        and (item.match_problem or item.media.match_state is MatchState.UNCERTAIN)
    )


def guess_of(item: QueueItem) -> bool:
    """Whether an unsure file has a guess to ask about (TMDB's top result)."""
    return item.media.tmdb_id is not None


def _subtitle(state: State, group: Sequence[QueueItem], episodes: bool) -> str:
    if state == "needs":
        waiting = [i for i in group if i.status in _WAITING and i.message]
        reason = waiting[0].message if waiting else t(WAITING_STATUS)
        return f"{reason} · {_files(len(group))}"
    if episodes:
        count = tn("{n} episode", "{n} episodes", len(group))
        return " · ".join(x for x in (_seasons(group), count) if x)
    return _film_line(group)


@dataclass(frozen=True, slots=True)
class _Band:
    """What a band says and offers: its status words, its question and its buttons."""

    state_text: str
    actions: tuple[tuple[str, str], ...]
    quiet: tuple[str, ...] = ()
    tips: tuple[tuple[str, str], ...] = ()
    ask: str = ""
    ask_bold: tuple[int, int] | None = None
    ask_more: str = ""
    names_file: bool = False  # the question names the guess: the title is what the file says
    guess: int | None = None  # the TMDB id "Is this …?" names


def _have(group: Sequence[QueueItem], episodes: bool, facts: ReplaceFacts | None) -> _Band:
    """A band held as already in the library: do you want this download instead?"""
    held = [i for i in group if held_in_library(i)]
    more = ""
    if episodes and len(held) > 1:
        ask = t(HAVE_EPISODES, n=number(len(held)))
    else:
        ask = t(HAVE_EPISODE if episodes else HAVE_FILM)
        if len(held) == 1 and facts is not None:
            more = facts_text(facts)
    return _Band(
        state_text=t(IN_LIBRARY_STATUS),
        actions=(
            (CHOOSE_ACTION, t(WRONG_SHOW if episodes else WRONG_FILM)),
            (IGNORE_ACTION, t(KEEP_MINE)),
            (REPLACE_ACTION, t(REPLACE_LABEL)),
        ),
        quiet=(CHOOSE_ACTION,),
        tips=((IGNORE_ACTION, t(LEAVE_TIP)), (REPLACE_ACTION, t(REPLACE_TIP))),
        ask=ask,
        ask_more=more,
    )


def _ask_unsure(group: Sequence[QueueItem], episodes: bool) -> _Band:
    """A band not sure what it is: is it TMDB's guess?"""
    first = next(i for i in group if is_unsure(i))
    status = t(UNSURE_SHOW_STATUS if episodes else UNSURE_FILM_STATUS)
    leave = (IGNORE_ACTION, t(LEAVE_ALONE))
    tips = ((IGNORE_ACTION, t(LEAVE_TIP)),)
    if not guess_of(first):
        return _Band(
            state_text=status,
            actions=(leave, (CHOOSE_ACTION, t(PICK_SHOW if episodes else PICK_FILM))),
            quiet=(IGNORE_ACTION,),
            tips=tips,
            ask=t(WHICH_SHOW if episodes else WHICH_FILM),
            ask_more=first.message if first.status is ItemStatus.HELD else "",
            names_file=True,
        )
    name = first.media.title if episodes else first.media.ny
    ask = t(IS_THIS, title=name)
    start = ask.find(name)
    return _Band(
        state_text=status,
        actions=(
            leave,
            (CHOOSE_ACTION, t(PICK_OTHER_SHOW if episodes else PICK_OTHER_FILM)),
            (ACCEPT_ACTION, t(YES_FORGE)),
        ),
        quiet=(IGNORE_ACTION,),
        tips=tips,
        ask=ask,
        ask_bold=(start, start + len(name)) if start >= 0 else None,
        ask_more=t(UNSURE_SHOW if episodes else UNSURE_FILM),
        names_file=True,
        guess=first.media.tmdb_id,
    )


def _band_look(
    state: State,
    group: Sequence[QueueItem],
    episodes: bool,
    facts: Callable[[QueueItem], ReplaceFacts | None] | None,
) -> _Band:
    """A band not done: its status words, question and buttons (📂 goes before them all)."""
    wrong = (CHOOSE_ACTION, t(WRONG_SHOW if episodes else WRONG_FILM))
    leave = (IGNORE_ACTION, t(LEAVE_ALONE))
    leave_tip = (IGNORE_ACTION, t(LEAVE_TIP))
    if state == "ready":
        return _Band(
            state_text=t(READY_STATUS),
            actions=(wrong, leave, (FORGE_ACTION, t(FORGE_THIS))),
            quiet=(CHOOSE_ACTION, IGNORE_ACTION),
            tips=(leave_tip,),
        )
    held = next((i for i in group if held_in_library(i)), None)
    if held is not None:
        return _have(group, episodes, facts(held) if facts is not None else None)
    if any(is_unsure(i) for i in group):
        return _ask_unsure(group, episodes)
    failed = any(i.status is ItemStatus.ERROR for i in group)
    return _Band(
        state_text=t(FAILED_STATUS if failed else WAITING_STATUS),
        actions=(wrong, leave, (FORGE_ACTION, t(TRY_AGAIN if failed else FORGE_THIS))),
        quiet=(CHOOSE_ACTION,),
        tips=(leave_tip,),
    )


def _waiting(said: _Band, state: State, way: WatchedLook | None) -> _Band:
    """A ready watched band that waits says why instead of "Ready to forge"."""
    if state != "ready" or way is None or not way.waits:
        return said
    if way.waits == "changed":
        text = t(RULE_CHANGED)
    elif way.waits == "no_preset":
        text = t(PRESET_GONE, name=way.rule_name)
    elif way.waits == "unwatched":
        text = t(NOT_WATCHED)
    elif way.waits == "no_rule":
        text = t(NO_RULE_TAKES)
    else:
        text = t(MANUAL_WAITS, name=way.rule_name)
    return replace(said, state_text=text)


def _rule_label(way: WatchedLook | None) -> str:
    """A band's "Rule: 4K films" ("" without a rule: a dropped file)."""
    return t(RULE_LABEL, name=way.rule_name) if way is not None and way.rule_name else ""


def build_queue_rows(
    items: Sequence[QueueItem],
    rule: Callable[[QueueItem], str],
    *,
    naming: Callable[[QueueItem], WatchedLook] | None = None,
    moving: Callable[[QueueItem], tuple[bool, Path | None]] | None = None,
    show: Show = "all",
    today: date | None = None,
    chip: Callable[[QueueItem], str] | None = None,
    cc: Callable[[Sequence[QueueItem]], CcLook | None] | None = None,
    facts: Callable[[QueueItem], ReplaceFacts | None] | None = None,
) -> QueueRows:
    """The queue's rows for the filter `show`, with every filter's count.

    `rule` gives a dropped file its naming rule (its band's preset, or the band's Custom rule).
    `moving` says whether its band moves it to a library folder, and that folder (None: not chosen
    yet); otherwise it is renamed where it is. `naming` gives a watched download how its rule's
    route shows (`WatchedLook`): moved to the route's folder, or sorted inside its watched folder;
    its band names the rule (`Row.rule`) and a ready one says why it waits, if it does.
    Only today's finished files are shown when `today` is set. `chip`: a band's preset chip
    (from its first file; none on a done band). `cc`: how a done band's CC ▾ shows (None: not).
    `facts`: the library copy's and the download's quality and size for a download already in
    the library (None: not known yet).
    """
    out = QueueRows()
    rendered = {}
    roots: dict[int, Path] = {}
    moves: dict[int, bool] = {}  # going to a library folder: whether that folder is known
    looks: dict[int, WatchedLook] = {}  # a watched download's, by its rule
    states: dict[int, State] = {}
    counts: dict[Show, int] = {"all": 0, "needs": 0, "ready": 0}
    kinds: dict[Kind, int] = {"series": 0, "movies": 0}
    shown: list[QueueItem] = []
    for item in items:
        state = item_state(item)
        if state == "done" and not _done_on(item, today):
            continue
        states[item.id] = state
        counts["all"] += 1
        if state in ("needs", "ready"):
            counts[state] += 1
        if state != "done":
            kinds[item_kind(item)] += 1
        if show != "all" and state != show:
            continue
        item_fmt, root = rule(item), item.root
        if naming is not None and item.watched is not None:
            way = looks[item.id] = naming(item)
            item_fmt, root = way.fmt, way.root or root
            if way.moves:
                moves[item.id] = way.root is not None
        elif moving is not None:
            wanted, library = moving(item)
            if wanted:
                moves[item.id] = library is not None
                root = library or root
        rendered[item.id] = name_file(item_fmt, item.media, item.path.suffix)
        roots[item.id] = root
        out.targets[item.id] = root.joinpath(*rendered[item.id].parts)
        shown.append(item)
    out.counts, out.kinds = counts, kinds

    def folders(item: QueueItem) -> tuple[str, ...]:
        root = roots[item.id]
        library = (_folder_name(root),) if moves.get(item.id) else ()
        return (*library, *rendered[item.id].parts[:-1])

    # Needs you first, then the ready ones by name, then today's done ones.
    order = {"needs": 0, "ready": 1, "done": 2}
    groups: dict[tuple[int, str, int | None, bool, State], list[QueueItem]] = {}
    for item in shown:
        media, state = item.media, states[item.id]
        episodes = media.kind is MediaKind.EPISODE
        key = (order[state], media.title.casefold(), media.year, episodes, state)
        groups.setdefault(key, []).append(item)
    keys = itertools.count(1)
    done_heading = False
    for (_, _, _, episodes, state), group in sorted(
        groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0, kv[0][4])
    ):
        if state == "done" and not done_heading and show == "all":
            done_heading = True
            done_count = sum(1 for s in states.values() if s == "done")
            out.rows.append(
                Row(
                    key=-next(keys),
                    kind="section",
                    title=t(DONE_TODAY),
                    subtitle=number(done_count),
                )
            )
        group.sort(
            key=lambda i: (i.media.parsed.season or 0, i.media.parsed.episode or 0, i.path.name)
        )
        first = group[0].media
        band_key = -next(keys)
        band_way = looks.get(group[0].id)  # a watched download's: by its rule
        out.bands[band_key] = [i.id for i in group]
        greyed: tuple[tuple[str, str], ...] = ()
        note = ""
        if state == "done":
            times = [i.finished_at for i in group if i.finished_at]
            at = time.strftime("%H:%M", time.localtime(max(times))) if times else ""
            actions: tuple[tuple[str, str], ...] = (("undo", t("Undo")),)
            look = cc(group) if cc is not None else None
            if look is not None:
                actions = ((CC_ACTION, CC_LABEL), *actions)
                note = look.note
                greyed = ((CC_ACTION, look.tip),) if look.greyed else ()
            done = t(DONE_AT_STATUS, time=at) if at else t(DONE_STATUS)
            said = _Band(state_text=done, actions=actions)
        else:
            said = _band_look(state, group, episodes, facts)
            said = _waiting(said, state, band_way)
            in_library = [i for i in group if held_in_library(i)]
            if state == "needs" and in_library:
                out.keeps[band_key] = [i.id for i in in_library]
                if len(in_library) == 1:
                    out.held[in_library[0].id] = (in_library[0], out.targets[in_library[0].id])
            if said.guess is not None:
                out.guesses[band_key] = said.guess
        actions = ((EXPLORER_ACTION, t(SHOW_FILE)), *said.actions)
        title = first.title if episodes or state != "needs" else first.ny
        if said.names_file:  # the question names the guess: the title is what the file says
            title = group[0].parsed.source_name if len(group) == 1 else group[0].parsed.title
        band_chip = chip(group[0]) if chip is not None and state != "done" else ""
        dest, dest_look = _destination(
            state,
            first.kind,
            group[0].id in moves,
            moves.get(group[0].id, False),
            _shared_folder([folders(i) for i in group]),
        )
        if band_way is not None and band_way.waits in ("unwatched", "no_rule"):
            dest, dest_look = "", "stays"  # no rule takes it: nowhere to name
        elif band_way is not None and not band_way.moves and state != "needs":
            folder = _folder_name(roots[group[0].id])  # its watched folder
            dest = t(SORTED_DONE if state == "done" else SORTED_IN, folder=folder)
            dest_look = "stays"
        out.rows.append(
            Row(
                key=band_key,
                kind="band",
                title=title,
                subtitle=_subtitle(state, group, episodes),
                dest=dest,
                dest_look=dest_look,
                badge=first.title,
                art=first.poster_path,
                backdrop=first.backdrop_path,
                actions=actions,
                quiet=(EXPLORER_ACTION, *said.quiet),
                greyed=greyed,
                tips=(
                    (EXPLORER_ACTION, t(OPEN_IN_EXPLORER)),
                    *said.tips,
                    # The chip's tip: the name this band's first file gets ("Dune (2021)\…").
                    *((("preset", "\\".join(rendered[group[0].id].parts)),) if band_chip else ()),
                ),
                note=note,
                rule=_rule_label(band_way),
                chip=band_chip,
                chip_caption=t(NAME_STYLE) if band_chip else "",
                state=state,
                state_text=said.state_text,
                ask=said.ask,
                ask_bold=said.ask_bold,
                ask_more=said.ask_more,
                faded=state == "done",
            )
        )
        seasons = {i.media.parsed.season for i in group} if episodes else set()
        previous = group[0].media.parsed.season
        for item in group:
            season = item.media.parsed.season
            divider = ""
            if len(seasons) > 1 and season != previous and season is not None:
                divider = t("Season {n}", n=season)
            previous = season
            name = rendered[item.id].parts[-1]
            title = item.media.episode_title if episodes and item.media.episode_title else name
            dropped, added = name_changes(item.parsed.source_name, name)
            out.rows.append(
                Row(
                    key=item.id,
                    kind="item",
                    title=title,
                    code=episode_code(item) if episodes else "",
                    faint=item.parsed.source_name,
                    faint_label=t(CURRENT_NAME),
                    faint_dropped=dropped,
                    title_added=added if title == name else (),
                    new=not episodes,
                    status=item.status.value,
                    badge=first.title,
                    band=band_key,
                    faded=state == "done",
                    divider=divider,
                )
            )
    return out
