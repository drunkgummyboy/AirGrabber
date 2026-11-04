"""The queue's rows: the pure, display-free part of the queue (ported from MediaForge's
ui/queue_view.py; the view itself is app/forge/ui/queue_view.py).

Files become `Row`s for the fast list (app/forge/ui/widgets/virtual_list.py): shows and films in
one list, one band per show with its episodes as short lines under it, one band per film with
its file. Every band has a status (`Row.state` and its word): "Needs you" (TMDB isn't sure, or a
watched download is held back or failed), "Ready", "Would rename" (a watched download while
renaming on its own is a trial) or "Done" (renamed today, faded, with its Undo). The filter
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

from app.forge.core.formatter import render
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.models.media import MediaKind
from app.forge.ui.subtitle_control import CC_ACTION, CC_LABEL, CcLook
from app.forge.ui.widgets.virtual_list import Row
from app.i18n import N_, number, t, tn

_SE = re.compile(r"S\d{2,}E\d{2,}(?:-E\d{2,})?")

type Kind = Literal["series", "movies"]
type Show = Literal["all", "needs", "ready"]
type State = Literal["needs", "ready", "would", "done"]

FINISHED = (ItemStatus.DONE, ItemStatus.SKIPPED)
NEEDS_OWNER = (ItemStatus.NEEDS_CHOICE, ItemStatus.HELD, ItemStatus.ERROR)
_WAITING = (ItemStatus.HELD, ItemStatus.ERROR)  # held back or failed, with a reason

NOW = N_("Now:")  # before a line's current file name, so it isn't taken for the new one
NEEDS_YOU = N_("Needs you")
READY = N_("Ready")
WOULD_RENAME = N_("Would rename")
DONE = N_("Renamed")
DONE_TODAY = N_("Done today")
IGNORE = N_("Ignore")  # a band's menu: Auto-rename leaves the file (or its folder) alone
EXPLORER_ACTION = "explorer"  # every band's first button, a glyph with this tip
EXPLORER_LABEL = "📂"
OPEN_IN_EXPLORER = N_("Open in Explorer")
STATE_WORDS: dict[State, str] = {
    "needs": NEEDS_YOU,
    "ready": READY,
    "would": WOULD_RENAME,
    "done": DONE,
}

EMPTY_TITLE = N_("Drop video files or folders here")
EMPTY_HINT = N_("or add them with the buttons above. Nothing is renamed until you click Forge.")
# The queue isn't empty but nothing matches the filter: (title, hint) per filter.
_EMPTY_FILTER: dict[Show, tuple[str, str]] = {
    "needs": (N_("Nothing needs you"), N_("Everything in the queue can be renamed.")),
    "ready": (N_("Nothing is ready yet"), N_("The files in the queue need you first.")),
}


def _files(n: int) -> str:
    return tn("{n} file", "{n} files", n)


def build_rows(items: Sequence[QueueItem], fmt: str) -> tuple[list[Row], dict[int, Path]]:
    """Rows grouped by destination folder, plus each item's full target path (the right-click
    confirm window's list)."""
    groups: dict[str, list[Row]] = {}
    labels: dict[str, str] = {}
    counts: dict[str, tuple[int, bool]] = {}
    targets: dict[int, Path] = {}
    for item in items:
        media = item.media
        rendered = render(fmt, media, item.path.suffix)
        targets[item.id] = item.root.joinpath(*rendered.parts)
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


def item_state(item: QueueItem, *, trial: bool) -> State:
    """A file's status: renamed, waiting for the owner, a watched download that only would be
    moved (a trial), or ready to rename."""
    if item.status in FINISHED:
        return "done"
    if item.status in NEEDS_OWNER:
        return "needs"
    if trial and item.watched is not None:
        return "would"
    return "ready"


def needs_you(item: QueueItem) -> bool:
    """The "needs you" rule (the Queue tab's badge, the Needs you filter): TMDB isn't sure, or a
    watched download was held back or failed."""
    return item.status in NEEDS_OWNER


@dataclass
class QueueRows:
    rows: list[Row] = field(default_factory=list[Row])
    targets: dict[int, Path] = field(default_factory=dict[int, Path])
    bands: dict[int, list[int]] = field(default_factory=dict[int, list[int]])  # band → items
    counts: dict[Show, int] = field(default_factory=dict[Show, int])  # files per filter
    kinds: dict[Kind, int] = field(default_factory=dict[Kind, int])  # files not renamed, per kind

    @property
    def needs(self) -> int:
        """Files that need the owner (the Queue tab's badge)."""
        return self.counts.get("needs", 0)


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
    """Where a band's files go: their folder, or the part their folders share ("Show › …")."""
    if all(f == folders[0] for f in folders):
        return " › ".join(folders[0]) or t("the same folder")
    shared: list[str] = []
    for parts in zip(*folders, strict=False):
        if any(part != parts[0] for part in parts):
            break
        shared.append(parts[0])
    return " › ".join([*shared, "…"]) if shared else t("several folders")


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
    show: Show = "all",
    trial: bool = False,
    today: date | None = None,
    chip: Callable[[QueueItem], str] | None = None,
    cc: Callable[[Sequence[QueueItem]], CcLook | None] | None = None,
) -> QueueRows:
    """The queue's rows for the filter `show`, with every filter's count.

    `rule` gives a dropped file its naming rule (its band's preset, or the band's Custom rule):
    it is renamed where it is. `naming` gives a watched download its library folder and naming
    rule. `trial`: renaming on its own is a trial, so watched downloads only "would" be renamed.
    Only today's finished files are shown when `today` is set. `chip`: a band's preset chip
    (from its first file; none on a done band). `cc`: how a done band's CC ▾ shows (None: not).
    """
    out = QueueRows()
    rendered = {}
    roots: dict[int, Path] = {}
    states: dict[int, State] = {}
    counts: dict[Show, int] = {"all": 0, "needs": 0, "ready": 0}
    kinds: dict[Kind, int] = {"series": 0, "movies": 0}
    shown: list[QueueItem] = []
    for item in items:
        state = item_state(item, trial=trial)
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
        rendered[item.id] = render(item_fmt, item.media, item.path.suffix)
        roots[item.id] = root
        out.targets[item.id] = root.joinpath(*rendered[item.id].parts)
        shown.append(item)
    out.counts, out.kinds = counts, kinds

    def folders(item: QueueItem) -> tuple[str, ...]:
        library = (roots[item.id].name,) if item.watched is not None and naming else ()
        return (*library, *rendered[item.id].parts[:-1])

    # Needs you first, then ready and would-rename by name, then today's done ones.
    order = {"needs": 0, "ready": 1, "would": 1, "done": 2}
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
        unsure = any(i.status is ItemStatus.NEEDS_CHOICE for i in group)
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
            # before them all, below.
            actions = (
                (("ignore", t(IGNORE)), ("choose", t("Choose…")), ("rename", t("Rename")))
                if unsure
                else (("ignore", t(IGNORE)), ("rename", t("Rename")))
            )
        actions = ((EXPLORER_ACTION, EXPLORER_LABEL), *actions)
        dest = "" if state == "needs" else "→ " + _shared_folder([folders(i) for i in group])
        out.rows.append(
            Row(
                key=band_key,
                kind="band",
                title=first.title if episodes or state != "needs" else first.ny,
                subtitle=_subtitle(state, group, episodes),
                dest=dest,
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
        for item in group:
            name = rendered[item.id].parts[-1]
            title = item.media.episode_title if episodes and item.media.episode_title else name
            out.rows.append(
                Row(
                    key=item.id,
                    kind="item",
                    title=title,
                    code=episode_code(item) if episodes else "",
                    faint=item.parsed.source_name,
                    faint_label=t(NOW),
                    new=not episodes,
                    status=item.status.value,
                    badge=first.title,
                    band=band_key,
                    faded=state == "done",
                )
            )
    return out
