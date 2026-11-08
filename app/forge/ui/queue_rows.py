"""The queue's rows: the pure, display-free part of the queue (ported from MediaForge's
ui/queue_view.py; the view itself is app/forge/ui/queue_view.py).

Files become `Row`s for the fast list (app/forge/ui/widgets/virtual_list.py): shows and films in
one list, one band per show with its episodes as short lines under it, one band per film with
its file. Every band has a status (`Row.state` and its word): "Needs you" (TMDB isn't sure, or a
watched download is held back or failed), "Ready" or "Done" (renamed today, faded, with its
Undo). The filter
(`show`) is All (needs you first, then the rest by name, then today's done ones under their
heading), Needs you or Ready, and every count is worked out whichever is shown.

No Tk here: the rows' texts are translated when they are built (the language is set once at
startup).
"""

from __future__ import annotations

import itertools
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from app.forge.core.formatter import name_file
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.models.media import MediaKind
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
NEEDS_YOU = N_("Needs you")
READY = N_("Ready")
DONE = N_("Renamed")
DONE_TODAY = N_("Done today")
IGNORE = N_("Ignore")  # a band's menu: Auto-rename leaves the file (or its folder) alone
EXPLORER_ACTION = "explorer"  # every band's first button, a glyph with this tip
EXPLORER_LABEL = "📂"
OPEN_IN_EXPLORER = N_("Open in Explorer")
STATE_WORDS: dict[State, str] = {
    "needs": NEEDS_YOU,
    "ready": READY,
    "done": DONE,
}

# Where a band's files go, after its subtitle. A glyph and a colour apart, to see it at a glance.
MOVES_TO = N_("📦 Moves to {folder}")
MOVED_TO = N_("📦 Moved to {folder}")
STAYS = N_("📁 Stays in its folder")
STAYS_IN = N_("📁 Stays here, in {folder}")
STAYED = N_("📁 Stayed in its folder")
STAYED_IN = N_("📁 Stayed here, in {folder}")
NO_SERIES_FOLDER = N_("⚠ Choose your Series folder in Settings › Auto-rename")
NO_MOVIES_FOLDER = N_("⚠ Choose your Movies folder in Settings › Auto-rename")

EMPTY_TITLE = N_("Drop video files or folders here")
EMPTY_HINT = N_("or add them with the buttons above. Nothing is renamed until you click Forge.")
# The queue isn't empty but nothing matches the filter: (title, hint) per filter.
_EMPTY_FILTER: dict[Show, tuple[str, str]] = {
    "needs": (N_("Nothing needs you"), N_("Everything in the queue can be renamed.")),
    "ready": (N_("Nothing is ready yet"), N_("The files in the queue need you first.")),
}


def _files(n: int) -> str:
    return tn("{n} file", "{n} files", n)


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


def _subtitle(state: State, group: Sequence[QueueItem], episodes: bool) -> str:
    if state == "needs":
        if any(i.status is ItemStatus.NEEDS_CHOICE for i in group):
            return tn("TMDB isn't sure · {n} file", "TMDB isn't sure · {n} files", len(group))
        waiting = [i for i in group if i.status in _WAITING]
        reason = waiting[0].message if waiting and waiting[0].message else t("Waiting for you")
        return f"{reason} · {_files(len(group))}"
    if episodes:
        count = tn("{n} episode", "{n} episodes", len(group))
        return " · ".join(x for x in (_seasons(group), count) if x)
    return _film_line(group)


def build_queue_rows(
    items: Sequence[QueueItem],
    rule: Callable[[QueueItem], str],
    *,
    naming: Callable[[QueueItem], tuple[str, Path]] | None = None,
    moving: Callable[[QueueItem], tuple[bool, Path | None]] | None = None,
    show: Show = "all",
    today: date | None = None,
    chip: Callable[[QueueItem], str] | None = None,
    cc: Callable[[Sequence[QueueItem]], CcLook | None] | None = None,
) -> QueueRows:
    """The queue's rows for the filter `show`, with every filter's count.

    `rule` gives a dropped file its naming rule (its band's preset, or the band's Custom rule).
    `moving` says whether its band moves it to a library folder, and that folder (None: not chosen
    yet); otherwise it is renamed where it is. `naming` gives a watched download its library folder
    and naming rule: those always move.
    Only today's finished files are shown when `today` is set. `chip`: a band's preset chip
    (from its first file; none on a done band). `cc`: how a done band's CC ▾ shows (None: not).
    """
    out = QueueRows()
    rendered = {}
    roots: dict[int, Path] = {}
    moves: dict[int, bool] = {}  # going to a library folder: whether that folder is known
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
            item_fmt, root = naming(item)
            moves[item.id] = True
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
        library = (root.name or str(root),) if moves.get(item.id) else ()
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
        out.bands[band_key] = [i.id for i in group]
        greyed: tuple[tuple[str, str], ...] = ()
        if state == "done":
            times = [i.finished_at for i in group if i.finished_at]
            note = time.strftime("%H:%M", time.localtime(max(times))) if times else ""
            actions: tuple[tuple[str, str], ...] = (("undo", t("Undo")),)
            look = cc(group) if cc is not None else None
            if look is not None:
                actions = ((CC_ACTION, CC_LABEL), *actions)
                note = look.note or note
                greyed = ((CC_ACTION, look.tip),) if look.greyed else ()
        else:
            note = ""
            # Ignore (a menu: this file / this whole folder) first, the main button last; 📂 goes
            # before them all, below. Choose… on every band: any match can turn out wrong, also a
            # remembered one that looks settled.
            actions = (("ignore", t(IGNORE)), ("choose", t("Choose…")), ("rename", t("Rename")))
        actions = ((EXPLORER_ACTION, EXPLORER_LABEL), *actions)
        dest, dest_look = _destination(
            state,
            first.kind,
            group[0].id in moves,
            moves.get(group[0].id, False),
            _shared_folder([folders(i) for i in group]),
        )
        out.rows.append(
            Row(
                key=band_key,
                kind="band",
                title=first.title if episodes or state != "needs" else first.ny,
                subtitle=_subtitle(state, group, episodes),
                dest=dest,
                dest_look=dest_look,
                badge=first.title,
                art=first.poster_path,
                backdrop=first.backdrop_path,
                actions=actions,
                greyed=greyed,
                tips=((EXPLORER_ACTION, t(OPEN_IN_EXPLORER)),),
                note=note,
                chip=chip(group[0]) if chip is not None and state != "done" else "",
                state=state,
                state_text=t(STATE_WORDS[state]),
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
