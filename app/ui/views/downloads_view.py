"""The Downloads page: the grabs of the last 30 days, newest first, with where each went, what
the torrent program says about it (Downloading 42 % / ✓ Finished / Waiting / Paused at 63 % /
Problem in <program>); a hint when no program can be asked, one plain
line when it can't be reached, and an amber banner when the downloads' disk is almost full.

In sync with the torrent program (3.0.2, `DownloadsOverview.synced`: its task list could be
read): the page lists every task the program has, newest first. A grab's row says "sent by
AirGrabber"; a task added in the program itself (`TaskEntry`) shows its name (a clean title and
the poster when AirGrabber recognised the show or film, else a file glyph), "Added in
<program>" beside it and its size. A grab the program hasn't picked up yet waits in amber
("Waiting for Download Station to pick it up…"), after an hour it is a problem ("Download
Station never picked this up"). Next to the filter: "Live from Download Station · 9 downloads".
The ⋯ menu pauses / resumes and removes tasks ("🗑 Remove from Download Station…", after a
question: `ConfirmDialog`, never deleting files; for Download Station an unfinished task's
partial download goes, and the question says so); the change shows at once and a failure puts
the row back with the reason in the problem line. "✓ Clear finished…" asks, then removes every
finished task (grabs and the program's own) from the program and the page.

The data comes from DownloadStatusService, which checks every 10 s while this page is on screen
(`<Map>` / `<Unmap>`) and tells the page about every new overview. Rows are kept by what they
show (`row_key`: the grab, or the program's task) and updated in place, each widget only when
its text or colour changed; a new task makes one new row and moves the others, so a NAS with
hundreds of finished tasks stays smooth. A row opens the show's window (an episode, also a
recognised task) or the film's info window (`on_open_film`; on IMDb when TMDB can't describe
the film, or without it).

At the top a filter (All / Downloading / Finished / Problems, `entry_group`, over every row;
kept for the session) and "✓ Clear finished" (without a program to read: it takes the finished
grabs off the list, never out of the history, never their files: DownloadStatusService.clear).
A row whose video MediaForge moved says where ("Renamed → Series › Gold Rush › Season 17",
`moved_text`, from the library folder's name on).
Each row has a ⋯ menu (`row_menu`): Download again… (⬇ Auto / ☰ Choose…), ⏸ Pause / ▶ Resume
(a task in the program), Open the folder (where MediaForge moved its video, else once it is
found in "Where finished downloads end up", else greyed with the reason as tip), then "🗑 Remove
from <program>…" (a task in the program) or "Remove from list" (a row without one).

Above the grabs (3.0.1), while one-click downloads wait in the download queue
(app/services/download_queue.py), a small "Waiting (3)" group lists them, each with a ✕ that
takes it out of the queue: closing the window a download was asked for in leaves it here.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import tkinter as tk
import webbrowser
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import customtkinter as ctk

from app.i18n import N_, month_short, number, t, tn
from app.models import (
    DownloadEntry,
    DownloadsOverview,
    DownloadState,
    FreeSpace,
    GrabOutcome,
    ListedDownload,
    MediaType,
    QueuedItem,
    QueueEvent,
    QueueKey,
    Show,
    TaskEntry,
)
from app.models.downloads import MovedTo
from app.services import DiscoveryService, DownloadStatusService, ImageCache, SettingsService
from app.services.download_queue import DownloadQueue
from app.services.download_status import Located
from app.services.release_groups import block_group
from app.services.search import release_group
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.missing_control import MENU_SEPARATOR, MenuEntry, popup_menu
from app.ui.row_actions import again_items
from app.ui.scrollable import ScrollableFrame
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip
from app.ui.views.base import BaseView
from app.ui.widgets import LoadingBar, MessagePanel, PosterImage

log = logging.getLogger(__name__)

# Shown through t().
TITLE = N_("Downloads")
EMPTY_TITLE = N_("No downloads yet")
EMPTY_TEXT = N_("Episodes and films you download show up here for 30 days.")
PROGRESS_HINT = N_(
    "Progress shows when AirGrabber can reach your torrent program — set it up in "
    "Settings → Where downloads go"
)
CLEAR_TEXT = N_("✓ Clear finished")
CLEAR_TIP = N_(
    "Takes finished downloads, and ones your torrent program no longer has, off this list. Your "
    "files and AirGrabber's history stay."
)
CLEAR_SYNC_TEXT = N_("✓ Clear finished…")
CLEAR_SYNC_TIP = N_(
    "Removes finished downloads from {program} and from this list. Their files stay where they "
    "are."
)
LIVE_ONE = N_("Live from {program} · {n} download")
LIVE_MORE = N_("Live from {program} · {n} downloads")
SENT_BY_TEXT = N_("sent by AirGrabber")
ADDED_TAG = N_("Added in {program}")
RECOGNISED_TEXT = N_("Recognised as {name}")
MENU_TIP = N_("More options")
NO_MATCH_TEXT = N_("Nothing here right now.")
OPEN_FOLDER_TEXT = N_("Open the folder")
AGAIN_TEXT = N_("Download again…")
REMOVE_TEXT = N_("Remove from list")
REMOVE_TIP = N_("Only takes it off this list. Your files stay where they are.")
PAUSE_TEXT = N_("⏸ Pause")
RESUME_TEXT = N_("▶ Resume")
REMOVE_FROM_TEXT = N_("🗑 Remove from {program}…")
REMOVE_TITLE = N_("Remove from {program}?")
REMOVE_NAS = N_(
    "{name} is removed from {program} and from this list. What it downloaded so far stays on "
    "the NAS."
)
REMOVE_PC = N_(
    "{name} is removed from {program} and from this list. What it downloaded so far stays on "
    "this PC."
)
REMOVE_PARTIAL = N_(
    "{name} is removed from {program} and from this list. Its unfinished download is removed "
    "too; finished files stay."
)
REMOVE_BUTTON = N_("Remove")
BLOCK_TEXT = N_("🚫 Wrong or fake? Block {group}…")
BLOCK_TITLE = N_("Block {group}?")
BLOCK_QUESTION = N_(
    "AirGrabber never shows or picks releases from {group} again. You can undo this in "
    "Settings → Advanced."
)
BLOCK_BUTTON = N_("Block")
CLEAR_TITLE = N_("Clear finished downloads?")
CLEAR_ONE = N_(
    "{n} finished download is removed from {program} and from this list. Its files stay where "
    "they are."
)
CLEAR_MORE = N_(
    "{n} finished downloads are removed from {program} and from this list. Their files stay "
    "where they are."
)
CLEAR_BUTTON = N_("Clear {n}")
NO_FOLDER_TIP = N_("Choose where finished downloads end up first (Settings → Where downloads go)")
NOT_FOUND_TIP = N_("Not found in the folder where finished downloads end up")
UNREACHABLE_TIP = N_("The folder where finished downloads end up can't be reached")
NO_SHOW_TIP = N_("AirGrabber doesn't know this show any more")
WAITING_TEXT = N_("Waiting ({n})")
DROP_TIP = N_("Remove from the queue")
IMDB_TITLE_URL = "https://www.imdb.com/title/{}/"
IMDB_FIND_URL = "https://www.imdb.com/find/?q="
FILE_GLYPH = "📄"  # a task AirGrabber doesn't recognise: in the poster's place, theme-coloured
TITLE_MAX = 64  # characters of a task's own name a row shows (then "…")
MESSAGE_MS = 12_000  # a failed action's reason stays in the problem line this long
# A long list's first showing: this many rows at once (a screenful), the rest BUILD_BATCH at a
# time with BUILD_PAUSE_S between (the loop's turn: Tk handles input and drawing meanwhile), so
# the page answers while hundreds of rows are made.
FIRST_ROWS = 10
BUILD_BATCH = 1
BUILD_PAUSE_S = 0.01
_POSTER = (40, 60)
_STATE_WIDTH = 210
_WAITING_ROW_HEIGHT = 36
_DETAILS_WAIT_S = 5.0

_STATE_COLORS: dict[DownloadState, theme.Color] = {
    DownloadState.DOWNLOADING: theme.TEXT,
    DownloadState.WAITING: theme.TEXT_MUTED,
    DownloadState.STOPPED: theme.TEXT_DIM,
    DownloadState.READY: theme.SUCCESS,
    DownloadState.PROBLEM: theme.DANGER,
    DownloadState.PENDING: theme.WARNING,
    DownloadState.NOT_FOUND: theme.DANGER,
    DownloadState.UNKNOWN: theme.TEXT_DIM,
}


# -- the filter ---------------------------------------------------------------------------------


class EntryFilter(StrEnum):
    """Which rows the page lists (the bar at the top; kept for the session)."""

    ALL = "all"
    DOWNLOADING = "downloading"  # on its way: downloading, waiting (also for pick-up), paused
    FINISHED = "finished"  # ready
    PROBLEMS = "problems"  # a problem in the program, or never picked up there


FILTER_NAMES: dict[EntryFilter, str] = {
    EntryFilter.ALL: N_("All"),
    EntryFilter.DOWNLOADING: N_("Downloading"),
    EntryFilter.FINISHED: N_("Finished"),
    EntryFilter.PROBLEMS: N_("Problems"),
}

_GROUPS: dict[DownloadState, EntryFilter] = {
    DownloadState.DOWNLOADING: EntryFilter.DOWNLOADING,
    DownloadState.WAITING: EntryFilter.DOWNLOADING,
    DownloadState.STOPPED: EntryFilter.DOWNLOADING,
    DownloadState.PENDING: EntryFilter.DOWNLOADING,
    DownloadState.READY: EntryFilter.FINISHED,
    DownloadState.PROBLEM: EntryFilter.PROBLEMS,
    DownloadState.NOT_FOUND: EntryFilter.PROBLEMS,
}


def entry_group(state: DownloadState) -> EntryFilter | None:
    """The filter a row's state belongs to; None for an unknown state (only under All)."""
    return _GROUPS.get(state)


def filter_entries[T: (DownloadEntry, ListedDownload)](
    entries: Sequence[T], chosen: EntryFilter
) -> tuple[T, ...]:
    """The rows the chosen filter lists, in their order."""
    if chosen is EntryFilter.ALL:
        return tuple(entries)
    return tuple(entry for entry in entries if entry_group(entry.state) is chosen)


_CLEARABLE = frozenset({DownloadState.READY, DownloadState.NOT_FOUND})


def finished_ids(entries: Iterable[DownloadEntry]) -> list[int]:
    """The history ids "✓ Clear finished" takes off the list without a program to read: the
    ready grabs, and those the program never had."""
    return [
        entry.record.id
        for entry in entries
        if entry.state in _CLEARABLE and entry.record.id is not None
    ]


def finished_rows(overview: DownloadsOverview) -> list[ListedDownload]:
    """What "✓ Clear finished…" takes in sync: every finished row (also seeding)."""
    return [item for item in overview.rows if item.state is DownloadState.READY]


def filter_from_name(name: str) -> EntryFilter:
    for kind, text in FILTER_NAMES.items():
        if t(text) == name:
            return kind
    return EntryFilter.ALL


RowKey = tuple[str, object]


def row_key(item: ListedDownload) -> RowKey:
    """What a row shows, across checks: a grab by its history id, a task added in the program
    by the program's id for it (else its name)."""
    if isinstance(item, DownloadEntry):
        return ("grab", item.record.id)
    return ("task", item.task.task_id or item.task.name)


# -- the ⋯ menu --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageActions:
    """What the rows' ⋯ menus do besides the folder and the list (the main window provides
    them): a grab's title downloaded again (`auto`: the one-click download; else the search
    window)."""

    again: Callable[[DownloadEntry, bool], None]


def folder_tip(located: Located) -> str:
    """Why "Open the folder" is greyed out ("" when it isn't)."""
    if located.folder is not None:
        return ""
    if located.problem == "no_folder":
        return t(NO_FOLDER_TIP)
    if located.problem == "unreachable":
        return t(UNREACHABLE_TIP)
    return t(NOT_FOUND_TIP)


def pause_or_resume(state: DownloadState) -> str | None:
    """"pause" for a task on its way, "resume" for a paused one or one with a problem (try
    again); None for a finished one."""
    if state in (DownloadState.DOWNLOADING, DownloadState.WAITING):
        return "pause"
    if state in (DownloadState.STOPPED, DownloadState.PROBLEM):
        return "resume"
    return None


def row_menu(
    entry: ListedDownload,
    located: Located,
    *,
    open_folder: Callable[[str], object],
    again: Callable[[], None] | None,
    remove: Callable[[], None],
    program: str = "",
    pause: Callable[[], None] | None = None,
    resume: Callable[[], None] | None = None,
    remove_from_program: Callable[[], None] | None = None,
    block: tuple[str, Callable[[], None]] | None = None,
) -> list[MenuEntry]:
    """A row's ⋯ menu, as the mockup has it: Download again…, ⏸ Pause / ▶ Resume, Open the
    folder, "🚫 Wrong or fake? Block <group>…" (`block`: the grab's release group and what
    blocks it), then "🗑 Remove from <program>…" (red; a task in the program, with
    `remove_from_program`) or Remove from list. A grab's items are always there, greyed with
    their reason as tip when they can't be done now; a task added in the program has no
    Download again."""
    folder = located.folder
    grab = isinstance(entry, DownloadEntry)
    items: list[MenuEntry] = []
    if grab:
        items.append(MenuEntry(t(AGAIN_TEXT), again, "" if again is not None else t(NO_SHOW_TIP)))
    if pause is not None:
        items.append(MenuEntry(t(PAUSE_TEXT), pause))
    elif resume is not None:
        items.append(MenuEntry(t(RESUME_TEXT), resume))
    items.append(
        MenuEntry(
            t(OPEN_FOLDER_TEXT),
            (lambda: _call(open_folder, folder)) if folder is not None else None,
            folder_tip(located),
        )
    )
    if block is not None:
        group, on_block = block
        items.append(MenuEntry(t(BLOCK_TEXT, group=group), on_block))
    if remove_from_program is not None:
        items.append(MENU_SEPARATOR)
        items.append(
            MenuEntry(t(REMOVE_FROM_TEXT, program=program), remove_from_program, danger=True)
        )
    elif grab:
        items.append(MENU_SEPARATOR)
        items.append(MenuEntry(t(REMOVE_TEXT), remove, t(REMOVE_TIP)))
    return items


def _call(action: Callable[[str], object], value: str) -> None:
    action(value)


# -- texts -------------------------------------------------------------------------------------


def percent(progress: float | None) -> int:
    """Whole percent, rounded down (99.9 % is not done yet)."""
    if progress is None:
        return 0
    return max(0, min(100, int(progress * 100)))


def state_text(state: DownloadState, progress: float | None, program: str) -> str:
    """"Downloading 42 %", "✓ Finished", "Paused at 63 %", "Waiting for Download Station to
    pick it up…", …"""
    if state is DownloadState.DOWNLOADING:
        return t("Downloading {percent} %", percent=percent(progress))
    if state is DownloadState.WAITING:
        return t("Waiting")
    if state is DownloadState.STOPPED:
        return t("Paused at {percent} %", percent=percent(progress))
    if state is DownloadState.READY:
        return t("✓ Finished")
    if state is DownloadState.PROBLEM:
        return t("Problem in {program}", program=program) if program else t("Problem")
    if state is DownloadState.PENDING:
        if program:
            return t("Waiting for {program} to pick it up…", program=program)
        return t("Waiting")
    if state is DownloadState.NOT_FOUND:
        return t("{program} never picked this up", program=program) if program else t("Not found")
    return t("Status unknown")


def state_color(state: DownloadState) -> theme.Color:
    return _STATE_COLORS[state]


def destination_text(outcome: GrabOutcome | None, program: str) -> str:
    """Where a grab went: "Sent to Download Station", "Saved to folder", "Opened in
    qBittorrent"."""
    if outcome is GrabOutcome.SENT:
        if program:
            return t("Sent to {program}", program=program)
        return t("Sent to your torrent program")
    if outcome is GrabOutcome.SAVED:
        return t("Saved to folder")
    if outcome is GrabOutcome.OPENED:
        if program:
            return t("Opened in {program}", program=program)
        return t("Opened in your torrent program")
    return ""


def when_text(moment: datetime, now: datetime) -> str:
    """"Today 14:32", "Yesterday 09:10", "12 Oct 14:32" (with the year when not this year),
    in local time."""
    local, today = moment.astimezone(), now.astimezone()
    clock = f"{local:%H:%M}"
    days = (today.date() - local.date()).days
    if days == 0:
        return t("Today {time}", time=clock)
    if days == 1:
        return t("Yesterday {time}", time=clock)
    if local.year == today.year:
        return f"{local.day} {month_short(local)} {clock}"
    return f"{local.day} {month_short(local)} {local:%Y}"


def moved_text(moved: MovedTo | None) -> str:
    """Where MediaForge moved it: "Renamed → Series › Gold Rush › Season 17" (from the library
    folder's name on); "" when it didn't."""
    if moved is None or not moved.shown:
        return ""
    return t("Renamed → {folder}", folder=" › ".join(moved.shown))


_VIA_PROGRAM = frozenset({DownloadState.PENDING, DownloadState.NOT_FOUND})


def meta_text(entry: DownloadEntry, now: datetime, *, synced: bool = False) -> str:
    """"Today 14:32 · Sent to Download Station" (in sync, a grab in its program or waiting
    for it: "· sent by AirGrabber"), then where MediaForge moved it when it did
    (" · Renamed → Series › Gold Rush › Season 17")."""
    parts = [when_text(entry.record.downloaded_at, now)]
    if synced and (entry.task is not None or entry.state in _VIA_PROGRAM):
        parts.append(t(SENT_BY_TEXT))
    elif where := destination_text(entry.outcome, entry.program):
        parts.append(where)
    if moved := moved_text(entry.moved_to):
        parts.append(moved)
    return " · ".join(parts)


def task_meta_text(item: TaskEntry) -> str:
    """A task added in the program: "Recognised as Severance · 2.1 GB", or just "5.7 GB"."""
    parts = []
    if item.recognised is not None:
        parts.append(t(RECOGNISED_TEXT, name=item.recognised.name))
    if item.task.size:
        parts.append(size_text(item.task.size))
    return " · ".join(parts)


def task_title(item: TaskEntry) -> str:
    """Its clean title, or its own name cut at TITLE_MAX characters."""
    title = item.title
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1].rstrip() + "…"


def size_text(size: int) -> str:
    """"12 GB", "4.5 GB", "1.2 TB" (binary units, as Windows shows them; a decimal comma in
    Dutch)."""
    gb = size / 1024**3
    if gb >= 1000:
        return f"{number(gb / 1024, '.1f')} TB"
    if gb >= 10:
        return f"{gb:.0f} GB"
    return f"{number(gb, '.1f')} GB"


def space_text(space: FreeSpace) -> str:
    """"Your NAS is almost full: 12 GB free"."""
    free = size_text(space.free_bytes)
    if space.on_nas:
        return t("Your NAS is almost full: {size} free", size=free)
    return t("Your download drive is almost full: {size} free", size=free)


def space_status_text(space: FreeSpace) -> str:
    """The once-a-day status-bar notice."""
    return f"⚠ {space_text(space)}"


def badge_text(count: int) -> str:
    """The header button's badge: "" (hidden) for none, "99+" above 99."""
    if count <= 0:
        return ""
    return "99+" if count > 99 else str(count)


def live_text(overview: DownloadsOverview) -> str:
    """"Live from Download Station · 9 downloads" while the page mirrors the program."""
    if not overview.synced or overview.problem:
        return ""
    count = len(overview.entries) + len(overview.tasks)
    return tn(LIVE_ONE, LIVE_MORE, count, program=overview.program)


def remove_question(item: ListedDownload, overview: DownloadsOverview) -> str:
    """The text of "Remove from <program>?": what stays (on the NAS / this PC), or for an
    unfinished Download Station task that its partial download goes too."""
    name = item.title if isinstance(item, DownloadEntry) else task_title(item)
    program = overview.program
    if overview.removes_partial and item.state is not DownloadState.READY:
        return t(REMOVE_PARTIAL, name=name, program=program)
    if overview.on_this_pc:
        return t(REMOVE_PC, name=name, program=program)
    return t(REMOVE_NAS, name=name, program=program)


def clear_question(count: int, program: str) -> str:
    return tn(CLEAR_ONE, CLEAR_MORE, count, program=program)


def imdb_find_url(title: str) -> str:
    return IMDB_FIND_URL + quote(title, safe="")


def poster_url(item: ListedDownload) -> str | None:
    """An episode's show poster (TVMaze's medium one, cached); a recognised task's show or
    watchlist film poster; films grabbed have none to hand."""
    if isinstance(item, DownloadEntry):
        show = item.show
    else:
        recognised = item.recognised
        if recognised is None:
            return None
        if recognised.show is None:
            return recognised.poster_url
        show = recognised.show
    return (show.poster_url or show.poster_url_original) if show is not None else None


@dataclass(frozen=True, slots=True)
class RowLook:
    """Everything a row shows, worked out once per check: a row repaints only what changed."""

    title: str
    tag: str  # "Added in Download Station" ("" for a grab)
    meta: str
    state: str
    state_color: theme.Color
    bar: float | None  # the progress bar's value; None hides it
    bar_paused: bool
    poster: str | None
    glyph: bool  # the file glyph instead of a poster
    clickable: bool


def row_look(item: ListedDownload, overview: DownloadsOverview, now: datetime) -> RowLook:
    state = item.state
    if isinstance(item, DownloadEntry):
        program = overview.program or item.program
        title, tag = item.title, ""
        meta = meta_text(item, now, synced=overview.synced)
        clickable, glyph = True, False
        progress = item.progress
    else:
        program = overview.program
        title, tag = task_title(item), t(ADDED_TAG, program=program)
        meta = task_meta_text(item)
        clickable = glyph = False
        if item.recognised is not None:
            clickable = item.recognised.show is not None or item.recognised.movie_id is not None
        else:
            glyph = True
        progress = item.progress
    bar = None
    if state in (DownloadState.DOWNLOADING, DownloadState.STOPPED):
        bar = progress or 0.0
    return RowLook(
        title=title,
        tag=tag,
        meta=meta,
        state=state_text(state, progress, program),
        state_color=state_color(state),
        bar=bar,
        bar_paused=state is DownloadState.STOPPED,
        poster=poster_url(item),
        glyph=glyph,
        clickable=clickable,
    )


@dataclass(frozen=True, slots=True)
class _Unbuilt:
    """A row not made yet: what it will show, and where."""

    key: RowKey
    grid_row: int
    item: ListedDownload
    look: RowLook


# -- the page ----------------------------------------------------------------------------------


class DownloadsView(BaseView):
    def __init__(
        self,
        master: Any,
        *,
        downloads: DownloadStatusService,
        settings: SettingsService,
        images: ImageCache,
        discovery: DiscoveryService,
        on_open_show: Callable[[Show], None],
        on_open_film: Callable[[int], Awaitable[bool]] | None = None,
        actions: PageActions | None = None,
        queue: DownloadQueue | None = None,
        **kwargs: Any,
    ) -> None:
        """`on_open_film(tmdb_id)`: opens a film's info window; False when it couldn't (the
        row then opens the film on IMDb). `actions`: the ⋯ menus' Download again… (greyed out
        without it). `queue`: the download queue, whose waiting
        downloads show at the top (none without it)."""
        super().__init__(master, **kwargs)
        self._on_open_film = on_open_film
        self._actions = actions
        self._filter = EntryFilter.ALL
        self._overview: DownloadsOverview | None = None
        self._menu_task: asyncio.Task[None] | None = None
        self._downloads = downloads
        self._settings = settings
        self._images = images
        self._discovery = discovery
        self._on_open_show = on_open_show
        self._rows: list[_Row] = []  # in the order they show
        self._by_key: dict[RowKey, _Row] = {}
        self._unbuilt: list[_Unbuilt] = []  # rows still to make (`_build_rest`)
        self._build_task: asyncio.Task[None] | None = None
        self._open_task: asyncio.Task[None] | None = None
        self._action_tasks: set[asyncio.Task[None]] = set()
        self._message = ""  # a failed action's reason, in the problem line for a while
        self._message_job: str | None = None
        self.dialog: ConfirmDialog | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=5)
        ctk.CTkLabel(
            header, text=t(TITLE), font=theme.font(22, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        self._filter_var = ctk.StringVar(value=t(FILTER_NAMES[EntryFilter.ALL]))
        self.filter_bar = ctk.CTkSegmentedButton(
            header,
            values=[t(name) for name in FILTER_NAMES.values()],
            variable=self._filter_var,
            command=self._filter_chosen,
            height=32,
            corner_radius=8,
            font=theme.font(13, bold=True),
            fg_color=theme.SURFACE,
            selected_color=theme.SELECTED,
            selected_hover_color=theme.SELECTED_HOVER,
            unselected_color=theme.SURFACE,
            unselected_hover_color=theme.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
        )
        self.filter_bar.pack(side="left", padx=(24, 0))
        # "Live from Download Station · 9 downloads" while the page mirrors the program.
        self.live = ctk.CTkLabel(
            header, text="", font=theme.font(13), text_color=theme.TEXT_MUTED, anchor="w"
        )
        self.clear_button = ctk.CTkButton(
            header,
            text=t(CLEAR_TEXT),
            width=0,
            height=32,
            corner_radius=8,
            font=theme.font(13, bold=True),
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            text_color_disabled=theme.TEXT_FAINT,
            border_width=1,
            border_color=theme.EDGE,
            command=self._clear_finished,
        )
        self.clear_button.pack(side="right")
        self._clear_tip = add_tooltip(self.clear_button, t(CLEAR_TIP))
        self._no_match: ctk.CTkLabel | None = None  # the filter lists nothing
        # One plain line: the hint (no program to ask) or why the program can't be reached.
        self._note = ctk.CTkLabel(
            self, text="", font=theme.font(13), text_color=theme.TEXT_MUTED, anchor="w",
            justify="left", wraplength=1100,
        )
        self._banner = ctk.CTkFrame(self, fg_color=theme.WARNING, corner_radius=8)
        self._banner_label = ctk.CTkLabel(
            self._banner, text="", font=theme.font(14, bold=True), text_color=theme.BG
        )
        self._banner_label.pack(side="left", padx=14, pady=8)
        self._body = ScrollableFrame(self, fg_color="transparent")
        self._body.grid_columnconfigure(0, weight=1)
        self._body.grid(row=3, column=0, sticky="nsew", padx=5, pady=(0, 5))
        self._empty: MessagePanel | None = None
        self._loading: LoadingBar | None = None
        # The downloads waiting in the queue, above the grabs (the body's first row).
        self._queue = queue
        self.waiting: WaitingGroup | None = None
        self._unsubscribe_queue: Callable[[], None] = lambda: None
        if queue is not None:
            self.waiting = WaitingGroup(self._body, on_drop=queue.remove)
            self._unsubscribe_queue = queue.subscribe(self._on_queue)
            self._paint_waiting()

        self._unsubscribe = downloads.subscribe(self._on_overview)
        # Checks every 10 s only while the page is on screen.
        self.bind("<Map>", lambda _event: self._downloads.set_visible(True), add="+")
        self.bind("<Unmap>", lambda _event: self._downloads.set_visible(False), add="+")

    async def load(self) -> None:
        latest = self._downloads.latest
        if latest is not None:
            self._render(latest)
        elif not self._rows and self._loading is None:
            self._loading = LoadingBar(self._body)
            self._loading.grid(row=1, column=0, sticky="ew", padx=10, pady=10)
        await self._downloads.check()  # the listener renders it

    def destroy(self) -> None:
        self._unsubscribe()
        self._unsubscribe_queue()
        self._downloads.set_visible(False)
        for task in (self._open_task, self._menu_task, self._build_task, *self._action_tasks):
            if task is not None:
                task.cancel()
        if self._message_job is not None:
            self.after_cancel(self._message_job)
            self._message_job = None
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.close()
        super().destroy()

    # -- the downloads waiting in the queue ----------------------------------------------------

    def _on_queue(self, _event: QueueEvent) -> None:
        try:
            if self.winfo_exists():
                self._paint_waiting()
        except tk.TclError:
            pass  # the window is closing

    def _paint_waiting(self) -> None:
        """"Waiting (n)" and its downloads while any wait; nothing while none does."""
        group, queue = self.waiting, self._queue
        if group is None or queue is None:
            return
        items = queue.waiting
        if items:
            group.show_items(items)
            group.grid(row=0, column=0, sticky="ew", padx=10, pady=(4, 8))
        else:
            group.grid_remove()

    # -- the filter and the list ---------------------------------------------------------------

    def set_filter(self, chosen: EntryFilter) -> None:
        self._filter_var.set(t(FILTER_NAMES[chosen]))
        self._filter_chosen(t(FILTER_NAMES[chosen]))

    def _filter_chosen(self, name: str) -> None:
        chosen = filter_from_name(name)
        if chosen is self._filter:
            return
        self._filter = chosen
        if self._overview is not None:
            self._render(self._overview)

    def _clear_finished(self) -> None:
        overview = self._overview
        if overview is None:
            return
        if not overview.synced:
            ids = finished_ids(overview.entries)
            if ids:
                spawn(self._downloads.clear(ids), name="DownloadsView.clear")
            return
        count = len(finished_rows(overview))
        if count:
            self._ask(
                title=t(CLEAR_TITLE),
                text=clear_question(count, overview.program),
                confirm=t(CLEAR_BUTTON, n=count),
                on_confirm=lambda: self._run(self._downloads.clear_finished(), "clear_finished"),
            )

    def _remove(self, entry: DownloadEntry) -> None:
        """Remove from list (a row without a task in the program)."""
        if entry.record.id is not None:
            spawn(self._downloads.clear([entry.record.id]), name="DownloadsView.remove")

    def _ask_remove(self, item: ListedDownload) -> None:
        """"Remove from <program>?", then the row goes at once (back when it failed)."""
        overview = self._overview
        if overview is None:
            return
        self._ask(
            title=t(REMOVE_TITLE, program=overview.program),
            text=remove_question(item, overview),
            confirm=t(REMOVE_BUTTON),
            on_confirm=lambda: self._run(self._downloads.remove(item), "remove_task"),
        )

    def _ask(
        self, *, title: str, text: str, confirm: str, on_confirm: Callable[[], object]
    ) -> None:
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.close()
        self.dialog = ConfirmDialog(
            self.winfo_toplevel(), title=title, text=text, confirm=confirm, on_confirm=on_confirm
        )

    def _run(self, call: Awaitable[str | None], name: str) -> None:
        """An action on the program: the page shows it at once (the service tells it); a
        failure's plain reason shows in the problem line."""

        async def run() -> None:
            message = await call
            if message and self.winfo_exists():
                self.show_failure(message)

        task: asyncio.Task[None] = spawn(run(), name=f"DownloadsView.{name}")
        self._action_tasks.add(task)
        task.add_done_callback(self._action_tasks.discard)

    def show_failure(self, message: str) -> None:
        """A failed action's reason, in the problem line for MESSAGE_MS."""
        self._message = message
        if self._message_job is not None:
            self.after_cancel(self._message_job)
        self._message_job = self.after(MESSAGE_MS, self._drop_message)
        if self._overview is not None:
            self._show_note(self._overview)

    def _drop_message(self) -> None:
        self._message_job = None
        self._message = ""
        if self._overview is not None and self.winfo_exists():
            self._show_note(self._overview)

    # -- a row's ⋯ menu ------------------------------------------------------------------------

    def open_menu(self, row: _Row) -> None:
        """Find the row's video (for "Open the folder"), then show its menu."""
        entry = row.entry
        if entry is None:
            return
        if self._menu_task is not None and not self._menu_task.done():
            self._menu_task.cancel()
        self._menu_task = spawn(self._show_menu(row, entry), name="DownloadsView.menu")

    async def _show_menu(self, row: _Row, entry: ListedDownload) -> None:
        row.set_busy(True)
        try:
            located = await self._downloads.locate(entry)
        finally:
            if row.winfo_exists():
                row.set_busy(False)
        if not row.winfo_exists():
            return
        popup_menu(row.more, self.menu_items(row, entry, located))

    def menu_items(
        self, row: _Row, entry: ListedDownload, located: Located
    ) -> list[MenuEntry]:
        overview = self._overview
        synced = overview is not None and overview.synced
        program = overview.program if overview is not None else ""
        task = entry.task if synced else None
        on_program = task is not None and task.task_id is not None
        action = pause_or_resume(entry.state) if on_program else None
        pause = functools.partial(self._pause, entry) if action == "pause" else None
        resume = functools.partial(self._resume, entry) if action == "resume" else None
        remove_from_program = functools.partial(self._ask_remove, entry) if on_program else None
        if isinstance(entry, TaskEntry):
            return row_menu(
                entry,
                located,
                open_folder=self._downloads.open_folder,
                again=None,
                remove=lambda: None,
                program=program,
                pause=pause,
                resume=resume,
                remove_from_program=remove_from_program,
            )
        actions = self._actions
        again: Callable[[], None] | None = None
        if actions is not None and (entry.record.media_type is MediaType.MOVIE or entry.show):

            def again_menu() -> None:
                assert actions is not None
                items = again_items(
                    lambda: actions.again(entry, True), lambda: actions.again(entry, False)
                )
                popup_menu(row.more, items)

            again = again_menu
        group = release_group(entry.record.release_title)
        block = (group, functools.partial(self._ask_block, group)) if group else None
        return row_menu(
            entry,
            located,
            open_folder=self._downloads.open_folder,
            again=again,
            remove=functools.partial(self._remove, entry),
            program=program,
            pause=pause,
            resume=resume,
            remove_from_program=remove_from_program,
            block=block,
        )

    def _ask_block(self, group: str) -> None:
        """"Block <group>?", then its releases are never shown or picked again."""
        self._ask(
            title=t(BLOCK_TITLE, group=group),
            text=t(BLOCK_QUESTION, group=group),
            confirm=t(BLOCK_BUTTON),
            on_confirm=lambda: spawn(
                block_group(self._settings, group), name="DownloadsView.block"
            ),
        )

    def _pause(self, item: ListedDownload) -> None:
        self._run(self._downloads.pause(item), "pause")

    def _resume(self, item: ListedDownload) -> None:
        self._run(self._downloads.resume(item), "resume")

    # -- rendering -----------------------------------------------------------------------------

    def _on_overview(self, overview: DownloadsOverview) -> None:
        try:
            if self.winfo_exists():
                self._render(overview)
        except tk.TclError:
            pass  # the window is closing

    def _render(self, overview: DownloadsOverview) -> None:
        self._overview = overview
        if self._loading is not None:
            self._loading.destroy()
            self._loading = None
        self._show_note(overview)
        self._show_banner(overview.space)
        self._show_header(overview)
        rows = overview.rows
        if not rows:
            self._clear_rows()
            self._show_no_match(False)
            if self._empty is None:
                self._empty = MessagePanel(
                    self._body, t(EMPTY_TITLE), t(EMPTY_TEXT), height=300
                )
                self._empty.grid(row=1, column=0, sticky="nsew")
            return
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        shown = filter_entries(rows, self._filter)
        self._show_no_match(not shown)
        self._place_rows(shown, overview)

    def _place_rows(self, items: Sequence[ListedDownload], overview: DownloadsOverview) -> None:
        """The rows for `items` (what the filter lists), in their order: a row of the same
        grab or task is kept and updated in place (and moved when its place changed), a new
        one made (the first FIRST_ROWS at once, the rest by `_build_rest`). A row the filter
        leaves out is hidden and kept for when it lists it again; a row of something the
        overview no longer has goes."""
        now = overview.checked_at or datetime.now().astimezone()
        listed = {row_key(item) for item in overview.rows}
        old = dict(self._by_key)
        by_key: dict[RowKey, _Row] = {}
        rows: list[_Row] = []
        unbuilt: list[_Unbuilt] = []
        used: set[RowKey] = set()
        made = 0
        for index, item in enumerate(items):
            key = row_key(item)
            if key in used:
                key = (key[0], (key[1], index))  # the same task twice (no id, one name)
            used.add(key)
            look = row_look(item, overview, now)
            row = old.pop(key, None)
            if row is None:
                if made >= FIRST_ROWS:  # the rest a few at a time, the page answering meanwhile
                    unbuilt.append(_Unbuilt(key, index + 1, item, look))
                    continue
                row = self._new_row()
                made += 1
            row.show_item(item, look)
            row.place_at(index + 1)
            by_key[key] = row
            rows.append(row)
        for key, row in old.items():
            if key in listed:  # filtered out: kept, out of sight
                row.hide()
                by_key[key] = row
            else:
                row.destroy()
        self._by_key, self._rows = by_key, rows
        self._unbuilt = unbuilt
        if unbuilt and (self._build_task is None or self._build_task.done()):
            self._build_task = spawn(self._build_rest(), name="DownloadsView.build_rows")

    def _new_row(self) -> _Row:
        return _Row(
            self._body,
            images=self._images,
            on_click=self._open,
            on_menu=self.open_menu,
        )

    async def _build_rest(self) -> None:
        """The rows not made yet, BUILD_BATCH at a time with the loop's turn in between."""
        while self._unbuilt:
            await asyncio.sleep(BUILD_PAUSE_S)
            if not self.winfo_exists():
                return
            self._build_batch()

    def _build_batch(self) -> None:
        batch, self._unbuilt = self._unbuilt[:BUILD_BATCH], self._unbuilt[BUILD_BATCH:]
        for pending in batch:
            row = self._new_row()
            row.show_item(pending.item, pending.look)
            row.place_at(pending.grid_row)
            self._by_key[pending.key] = row
        if batch:
            shown = (row for row in self._by_key.values() if row.grid_row)
            self._rows = sorted(shown, key=lambda row: row.grid_row)

    @property
    def building(self) -> bool:
        """Rows are still being made (a long list's first showing)."""
        return bool(self._unbuilt)

    def _show_header(self, overview: DownloadsOverview) -> None:
        live = live_text(overview)
        if live != str(self.live.cget("text")):
            self.live.configure(text=live)
        if live and not self.live.winfo_manager():
            self.live.pack(side="left", padx=(16, 0))
        elif not live and self.live.winfo_manager():
            self.live.pack_forget()
        if overview.synced:
            text, tip = t(CLEAR_SYNC_TEXT), t(CLEAR_SYNC_TIP, program=overview.program)
            has_finished = bool(finished_rows(overview))
        else:
            text, tip = t(CLEAR_TEXT), t(CLEAR_TIP)
            has_finished = bool(finished_ids(overview.entries))
        if text != str(self.clear_button.cget("text")):
            self.clear_button.configure(text=text)
        self._clear_tip.set_text(tip)
        state = "normal" if has_finished else "disabled"
        if state != str(self.clear_button.cget("state")):
            self.clear_button.configure(state=state)

    def _show_no_match(self, show: bool) -> None:
        if show and self._no_match is None:
            self._no_match = ctk.CTkLabel(
                self._body,
                text=t(NO_MATCH_TEXT),
                font=theme.font(14),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            )
            self._no_match.grid(row=1, column=0, sticky="w", padx=15, pady=15)
        elif not show and self._no_match is not None:
            self._no_match.destroy()
            self._no_match = None

    def _clear_rows(self) -> None:
        for row in self._by_key.values():
            row.destroy()
        self._rows = []
        self._by_key = {}
        self._unbuilt = []

    def _show_note(self, overview: DownloadsOverview) -> None:
        if self._message:
            text, color = f"⚠ {self._message}", theme.WARNING
        elif overview.problem:
            text, color = f"⚠ {overview.problem}", theme.WARNING
        elif not overview.progress_available and overview.entries:
            text, color = t(PROGRESS_HINT), theme.TEXT_MUTED
        else:
            text, color = "", theme.TEXT_MUTED
        if text != str(self._note.cget("text")):
            self._note.configure(text=text, text_color=color)
        if text:
            if not self._note.winfo_manager():
                self._note.grid(row=1, column=0, sticky="ew", padx=15, pady=(0, 6))
        elif self._note.winfo_manager():
            self._note.grid_remove()

    def _show_banner(self, space: FreeSpace | None) -> None:
        if space is not None and space.low:
            self._banner_label.configure(text=f"⚠ {space_text(space)}")
            self._banner.grid(row=2, column=0, sticky="ew", padx=15, pady=(0, 8))
        else:
            self._banner.grid_remove()

    # -- opening a row -------------------------------------------------------------------------

    def _open(self, item: ListedDownload) -> None:
        if isinstance(item, TaskEntry):
            recognised = item.recognised
            if recognised is None:
                return
            if recognised.show is not None:
                self._on_open_show(recognised.show)
            elif recognised.movie_id is not None:
                self._open_movie(recognised.movie_id, item.title)
            return
        if item.record.media_type is MediaType.TV:
            if item.show is not None:
                self._on_open_show(item.show)
            return
        self._open_movie(item.record.media_id, item.title)

    def _open_movie(self, movie_id: int, title: str) -> None:
        if self._open_task is not None and not self._open_task.done():
            return
        self._open_task = spawn(self._open_film(movie_id, title), name="DownloadsView.open_film")

    async def _open_film(self, movie_id: int, title: str) -> None:
        """The film's info window; else the film on IMDb: its page when TMDB knows its IMDb
        id, else IMDb's search."""
        if self._on_open_film is not None and await self._on_open_film(movie_id):
            return
        url = imdb_find_url(title)
        try:
            async with asyncio.timeout(_DETAILS_WAIT_S):
                details = await self._discovery.movie_details(movie_id)
            if details.imdb_id:
                url = IMDB_TITLE_URL.format(details.imdb_id)
        except (TMDBError, TimeoutError):
            log.info("No IMDb id for TMDB film %d; opening IMDb's search", movie_id)
        webbrowser.open(url)


class WaitingGroup(ctk.CTkFrame):
    """The downloads waiting in the queue: "Waiting (3)", then a slim row each (its label and
    a ✕ that takes it out of the queue: `on_drop`)."""

    def __init__(self, master: Any, *, on_drop: Callable[[QueueKey], object]) -> None:
        super().__init__(master, fg_color="transparent")
        self._on_drop = on_drop
        self._shown: tuple[QueuedItem, ...] = ()
        self.rows: list[ctk.CTkFrame] = []
        self.drops: list[ctk.CTkButton] = []  # each row's ✕
        self.grid_columnconfigure(0, weight=1)
        self.title = ctk.CTkLabel(
            self, text="", font=theme.font(13, bold=True), text_color=theme.TEXT_MUTED, anchor="w"
        )
        self.title.grid(row=0, column=0, sticky="w", padx=4, pady=(0, 2))

    def show_items(self, items: Sequence[QueuedItem]) -> None:
        shown = tuple(items)
        if shown == self._shown:
            return
        self._shown = shown
        self.title.configure(text=t(WAITING_TEXT, n=len(shown)))
        for row in self.rows:
            row.destroy()
        self.rows, self.drops = [], []
        for index, item in enumerate(shown, start=1):
            row = ctk.CTkFrame(
                self, fg_color=theme.SURFACE, corner_radius=8, height=_WAITING_ROW_HEIGHT
            )
            row.grid(row=index, column=0, sticky="ew", pady=2)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row, text=item.label, font=theme.font(13), text_color=theme.TEXT, anchor="w"
            ).grid(row=0, column=0, sticky="w", padx=(14, 8), pady=4)
            drop = ctk.CTkButton(
                row,
                text="✕",
                width=28,
                height=28,
                corner_radius=6,
                font=theme.font(13, bold=True),
                fg_color="transparent",
                hover_color=theme.CHIP_HOVER,
                text_color=theme.TEXT_MUTED,
                command=functools.partial(self._on_drop, item.key),
            )
            drop.grid(row=0, column=1, padx=(0, 8), pady=4)
            add_tooltip(drop, t(DROP_TIP))
            self.rows.append(row)
            self.drops.append(drop)

    @property
    def labels(self) -> tuple[str, ...]:
        """The waiting downloads it lists, in order."""
        return tuple(item.label for item in self._shown)


class _Row(ctk.CTkFrame):
    """One row: poster (or a file glyph), name (with "Added in …" for a task added in the
    program), when · where or the task's size,
    state and a thin progress bar, and its ⋯ menu (`on_menu`). `show_item` repaints only what
    changed since the last check; the tag, the glyph and the bar are made when first needed."""

    def __init__(
        self,
        master: Any,
        *,
        images: ImageCache,
        on_click: Callable[[ListedDownload], None],
        on_menu: Callable[[_Row], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=theme.SURFACE, corner_radius=8, cursor="hand2")
        self._images = images
        self._on_click = on_click
        self._entry: ListedDownload | None = None
        self._look: RowLook | None = None
        self._poster_url: str | None = None
        self._grid_row: int | None = None
        self.grid_columnconfigure(1, weight=1)

        self._poster = PosterImage(self, width=_POSTER[0], height=_POSTER[1])
        self._poster.grid(row=0, column=0, rowspan=2, padx=(8, 12), pady=8)
        self._poster.set_on_click(self._clicked)
        self._glyph: ctk.CTkLabel | None = None
        # Plain Tk frames for the three groups inside the row (a CTk one costs ~70 times as
        # much to make: hundreds of rows), in the row's colour, repainted with it
        # (`_set_appearance_mode`); the CTk widgets in them take that colour as theirs.
        surface = theme.SURFACE
        title_line = tk.Frame(self, bg=theme.pick(surface), highlightthickness=0, bd=0)
        title_line.grid(row=0, column=1, sticky="sw", pady=(10, 0))
        self._title = ctk.CTkLabel(
            title_line, text="", font=theme.font(14, bold=True), text_color=theme.TEXT,
            anchor="w", bg_color=surface,
        )
        self._title.pack(side="left")
        self._tag: ctk.CTkLabel | None = None
        self._title_line = title_line
        line = tk.Frame(self, bg=theme.pick(surface), highlightthickness=0, bd=0)
        line.grid(row=1, column=1, sticky="nw", pady=(0, 10))
        self._meta = ctk.CTkLabel(
            line, text="", font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w",
            bg_color=surface,
        )
        self._meta.pack(side="left")
        state_box = tk.Frame(self, bg=theme.pick(surface), highlightthickness=0, bd=0)
        state_box.grid(row=0, column=2, rowspan=2, sticky="e", padx=(12, 16))
        self._state_box = state_box
        self._plain = (title_line, line, state_box)
        self._state = ctk.CTkLabel(
            state_box, text="", font=theme.font(13), anchor="e", justify="right",
            wraplength=_STATE_WIDTH, bg_color=surface,
        )
        self._state.pack(anchor="e")
        self._bar: ctk.CTkProgressBar | None = None
        self.more = ctk.CTkButton(
            self,
            text="⋯",
            width=36,
            height=32,
            corner_radius=8,
            font=theme.font(14, bold=True),
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            text_color_disabled=theme.TEXT_FAINT,
            border_width=1,
            border_color=theme.EDGE,
            command=lambda: on_menu(self) if on_menu is not None else None,
        )
        if on_menu is not None:
            self.more.grid(row=0, column=3, rowspan=2, padx=(0, 14))
            add_tooltip(self.more, t(MENU_TIP))
        for widget in (self, self._title, title_line, line, self._meta,
                       state_box, self._state):
            widget.bind("<Button-1>", lambda _event: self._clicked(), add="+")

    @property
    def entry(self) -> ListedDownload | None:
        return self._entry

    @property
    def look(self) -> RowLook | None:
        return self._look

    @property
    def grid_row(self) -> int:
        return self._grid_row if self._grid_row is not None else 0

    def place_at(self, grid_row: int) -> None:
        """Lay the row out at `grid_row` of the list (only when that changed)."""
        if grid_row != self._grid_row:
            self._grid_row = grid_row
            self.grid(row=grid_row, column=0, sticky="ew", padx=10, pady=4)

    def hide(self) -> None:
        """Out of the list (the filter leaves it out); `place_at` brings it back."""
        if self._grid_row is not None:
            self._grid_row = None
            self.grid_remove()

    def _set_appearance_mode(self, mode_string: str) -> None:
        """Light ↔ dark: CTk repaints its own widgets; the plain frames take the row's colour."""
        super()._set_appearance_mode(mode_string)
        color = theme.pick(theme.SURFACE, dark=mode_string.lower() == "dark")
        for frame in self._plain:
            frame.configure(bg=color)

    def set_busy(self, busy: bool) -> None:
        """The ⋯ while its menu is being put together (the video looked for)."""
        self.more.configure(text="…" if busy else "⋯", state="disabled" if busy else "normal")

    def _clicked(self) -> None:
        if self._entry is not None:
            self._on_click(self._entry)

    def show_item(self, item: ListedDownload, look: RowLook) -> None:
        """Show `item` as `look` says, configuring only the widgets whose look changed."""
        self._entry = item
        before = self._look
        self._look = look
        if before is None or before.title != look.title:
            self._title.configure(text=look.title)
        if before is None or before.tag != look.tag:
            self._show_tag(look.tag)
        if before is None or before.meta != look.meta:
            self._meta.configure(text=look.meta)
        if before is None or (before.state, before.state_color) != (look.state, look.state_color):
            self._state.configure(text=look.state, text_color=look.state_color)
        if before is None or (before.bar, before.bar_paused) != (look.bar, look.bar_paused):
            self._show_bar(look.bar, look.bar_paused)
        if before is None or before.glyph != look.glyph:
            self._show_glyph(look.glyph)
        if before is None or before.clickable != look.clickable:
            cursor = "hand2" if look.clickable else ""
            self.configure(cursor=cursor)
            self._poster.set_on_click(self._clicked if look.clickable else None)
        if look.poster != self._poster_url:
            self._poster_url = look.poster
            self._poster.show(self._images, look.poster)

    def _show_tag(self, text: str) -> None:
        if text and self._tag is None:
            self._tag = ctk.CTkLabel(
                self._title_line,
                text=text,
                font=theme.font(11),
                fg_color=theme.PLACEHOLDER,
                bg_color=theme.SURFACE,
                text_color=theme.TEXT_MUTED,
                corner_radius=9,
                height=20,
            )
            self._tag.bind("<Button-1>", lambda _event: self._clicked(), add="+")
        if self._tag is None:
            return
        if text:
            self._tag.configure(text=f" {text} ")
            self._tag.pack(side="left", padx=(8, 0))
        else:
            self._tag.pack_forget()

    def _show_bar(self, value: float | None, paused: bool) -> None:
        if value is None:
            if self._bar is not None:
                self._bar.pack_forget()
            return
        color = theme.TEXT_FAINT if paused else theme.ACCENT
        if self._bar is None:
            self._bar = ctk.CTkProgressBar(
                self._state_box,
                width=_STATE_WIDTH - 30,
                height=4,
                progress_color=color,
                fg_color=theme.PLACEHOLDER,
                bg_color=theme.SURFACE,
            )
        else:
            self._bar.configure(progress_color=color)
        self._bar.set(value)
        if not self._bar.winfo_manager():
            self._bar.pack(anchor="e", pady=(4, 0))

    def _show_glyph(self, show: bool) -> None:
        if show and self._glyph is None:
            self._glyph = ctk.CTkLabel(
                self._poster,
                text=FILE_GLYPH,
                font=theme.font(20),
                text_color=theme.TEXT_FAINT,
                fg_color="transparent",
            )
        if self._glyph is None:
            return
        if show:
            self._glyph.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self._glyph.place_forget()
