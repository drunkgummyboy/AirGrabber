"""The release search panel: search every torrent indexer for one episode, season pack or film,
filter and sort the releases they found, and grab one. A frame that lives inside a host window
(the film / series info window, app/ui/title_info.py), which shows the poster, the title and
the rest itself.

Public API
----------
``ReleasePanel(master, *, target, search, downloads, library, discovery, settings, status,
on_target_change=None, on_count=None, on_fix_match=None)``
    A card (SURFACE, with a border) holding: for a series the SEASON and EPISODE chips (with
    "Pack", and "✎ Fix Match" at the right of the season row), the VERSION chips with their
    counts, the indexer chips, the progress bar, the results table and the selection bar with
    its Download button. Building it only builds widgets: nothing is loaded or searched (and no
    task is started) before ``start()``.
``panel.start()``
    Loads the grabbed releases, a show's episodes (for the chips) or a film's TMDB details (its
    IMDb id, for the search), and searches. Later calls do nothing. Call it once the asyncio
    loop runs (it spawns tasks).
``panel.set_target(target)``
    Another episode, season pack, show or film: the chips follow and, once started, the panel
    searches again (replacing the running search). The season, episode and Pack chips go
    through here. An equal target does nothing.
``panel.target``
    The current target (read-only).
``panel.show_message(text, color=theme.TEXT_MUTED)``
    A host's own message on the panel's message line ("" hides it).
``panel.focus_table()``
    Keyboard focus to the results table (Up / Down to select, Enter to grab).

Callbacks (all optional):
``on_target_change(target)``
    After every change of target: from the chips or from ``set_target()``.
``on_count(n)``
    The number of releases shown for the current version (the results title's number), each
    time it changes, from ``start()`` on: for a tab title such as "Releases (18)".
``on_fix_match(target)``
    Series only: "✎ Fix Match" asked for the right TVMaze id and tracked that show (in place of
    the old one when that was tracked); `target` is that show at the same season and episode.
    Without this callback the link isn't shown.

``destroy()`` cancels every task the panel started.

``PanelServices`` bundles what the panel works with, for a host to pass on.

The results table is a ttk.Treeview rather than CTk widgets: a search can return hundreds of
rows and re-renders on every indexer update, which must stay instant. Rows are keyed by a
stable id (`release_id`), so updates are applied as a diff and a row's selection and grab state
survive re-renders. The mouse wheel over the table scrolls just the table (not a scrolling
window around the panel as well).

Log hygiene: release download links may embed an API key (Prowlarr), so nothing here logs a
release or a URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import tkinter as tk
import webbrowser
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from tkinter import ttk
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import customtkinter as ctk

from app.i18n import N_, number, t, tn
from app.models import (
    Episode,
    GrabOutcome,
    GrabTarget,
    IndexerState,
    IndexerStatus,
    MediaType,
    Movie,
    MovieDetails,
    SearchRequest,
    SearchUpdate,
    Settings,
    Show,
    TorrentRelease,
)
from app.services.downloads import GrabError
from app.services.remote import RemoteError
from app.services.search import (
    QUALITY_KEYS,
    is_preferred,
    matches_quality,
    quality_counts,
    quality_key,
)
from app.services.tmdb import TMDBError
from app.services.tvmaze import TVMazeError, TVMazeNotFound
from app.ui import theme
from app.ui.download_control import outcome_text
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, unscaled_width
from app.ui.tooltip import Tooltip, add_tooltip
from app.ui.widgets import LoadingBar

if TYPE_CHECKING:
    from app.services import (
        DownloadService,
        LibraryService,
        SettingsService,
        TorrentSearchService,
    )
    from app.services.discovery import DiscoveryService
    from app.ui.views.base import StatusReporter

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class TvTarget:
    show: Show
    season: int
    episode: int | None  # None = season pack


@dataclass(frozen=True)
class MovieTarget:
    movie: Movie


IndexerTarget = TvTarget | MovieTarget


@dataclass(frozen=True, slots=True)
class PanelServices:
    """What a ReleasePanel works with, as a host window passes it on."""

    search: TorrentSearchService
    downloads: DownloadService
    library: LibraryService
    discovery: DiscoveryService
    settings: SettingsService
    status: StatusReporter

SortColumn = Literal["name", "size", "seeds", "leech", "source", "age"]
ColumnId = SortColumn | Literal["action"]
RowState = Literal["grabbing", "opened", "saved", "sent", "failed"]
_Anchor = Literal["w", "e", "center"]

QUALITY_LABELS = {
    "2160p": "4K",
    "1080p": "1080P",
    "720p": "720P",
    "480p": "480P",
    "x265": "x265",
    "any": N_("ANY"),  # shown through t(), as the other labels (which stay as they are)
}
# Columns whose first click sorts biggest first; the others start A→Z / newest first.
_DESCENDING_FIRST: frozenset[str] = frozenset({"size", "seeds", "leech"})
# The table's narrow action cell; the selection bar under the table spells things out.
_ACTION_TEXT: dict[RowState, str] = {
    "grabbing": "⏳",
    "opened": "✓",
    "saved": "✓",
    "sent": "✓",
    "failed": "✗",
}
_DONE_STATES: frozenset[RowState] = frozenset({"opened", "saved", "sent"})
_OUTCOME_STATES: dict[GrabOutcome, RowState] = {
    GrabOutcome.OPENED: "opened",
    GrabOutcome.SAVED: "saved",
    GrabOutcome.SENT: "sent",
}
# Shown through t().
DOWNLOAD_TEXT = N_("⬇ Download")
DOWNLOADING_TEXT = N_("Downloading…")
NO_SELECTION_TEXT = N_("Click a release above to select it")
CHIP_TIP = N_("Open this site's search in your browser")
_SIZE_UNITS = ("KB", "MB", "GB", "TB", "PB")
_UNKNOWN = "—"
_DETAILS_WAIT = 5.0  # seconds a movie search waits for TMDB details (for the IMDb id)

# (id, heading (shown through t()), width in CTk units, anchor, stretches)
_COLUMNS: tuple[tuple[ColumnId, str, int, _Anchor, bool], ...] = (
    ("name", N_("Name"), 320, "w", True),
    ("size", N_("Size"), 90, "e", False),
    ("seeds", N_("Seeds"), 70, "center", False),
    ("leech", N_("Leech"), 70, "center", False),
    ("source", N_("Source"), 120, "center", False),
    ("age", N_("Age"), 70, "center", False),
    ("action", "", 50, "center", False),
)
_COLUMN_IDS = tuple(column[0] for column in _COLUMNS)
_ACTION_COLUMN = f"#{_COLUMN_IDS.index('action') + 1}"
_ROW_HEIGHT = 26
_TABLE_FONT_SIZE = 11
_STYLE = "AG.Treeview"
_SELECTOR_HEIGHT = 30  # the tallest selector chip (the VERSION buttons)

# The panel's grid rows (column 0); only the table's row stretches.
_SELECTORS_ROW = 0
_TOP_ROW = 1  # the results title and the indexer chips
_LOADING_ROW = 2
_TABLE_ROW = 3
_HINT_ROW = 4
_MESSAGE_ROW = 5
_BAR_ROW = 6


# -- text --------------------------------------------------------------------------------------


def format_size(size_bytes: int) -> str:
    """"3.69 GB" (binary units, as in 1.x); "—" when the size is unknown (0)."""
    if size_bytes <= 0:
        return _UNKNOWN
    if size_bytes < 1024:
        return f"{size_bytes} B"
    value = size_bytes / 1024
    unit = 0
    while value >= 1024 and unit < len(_SIZE_UNITS) - 1:
        value /= 1024
        unit += 1
    return f"{number(value, '.2f')} {_SIZE_UNITS[unit]}"


def format_age(published: datetime | None, now: datetime) -> str:
    """"45m", "5h", "3d", "2y" since `published`; "—" when unknown."""
    if published is None:
        return _UNKNOWN
    seconds = max(0.0, (now - published).total_seconds())
    if seconds < 3600:
        return t("{n}m", n=int(seconds // 60))
    if seconds < 86400:
        return t("{n}h", n=int(seconds // 3600))
    days = int(seconds // 86400)
    return t("{n}d", n=days) if days < 365 else t("{n}y", n=days // 365)


def release_id(release: TorrentRelease) -> str:
    """The table row id of a release: stable across search updates."""
    return release.info_hash or f"{release.title}|{release.size_bytes}"


def _sort_value(release: TorrentRelease, column: SortColumn) -> tuple[float, str] | None:
    """The key `column` sorts on; None when the release has no value for it."""
    if column == "name":
        return 0.0, release.title.casefold()
    if column == "size":
        return (float(release.size_bytes), "") if release.size_bytes else None
    if column == "seeds":
        return float(release.seeders), ""
    if column == "leech":
        return float(release.leechers), ""
    if column == "source":
        return 0.0, release.source.casefold()
    if release.published_at is None:
        return None
    return -release.published_at.timestamp(), ""  # by age: newest first when ascending


def sort_releases(
    releases: Iterable[TorrentRelease], column: SortColumn, *, descending: bool
) -> list[TorrentRelease]:
    """Sorted by `column` (stable); releases of unknown size or age always come last."""
    known: list[tuple[tuple[float, str], TorrentRelease]] = []
    unknown: list[TorrentRelease] = []
    for release in releases:
        value = _sort_value(release, column)
        if value is None:
            unknown.append(release)
        else:
            known.append((value, release))
    known.sort(key=lambda pair: pair[0], reverse=descending)
    return [release for _, release in known] + unknown


def next_sort(
    current: SortColumn, descending: bool, clicked: SortColumn
) -> tuple[SortColumn, bool]:
    """The sort after a heading click: the same heading flips the direction; another one sorts
    by it, biggest first for Size / Seeds / Leech, A→Z or newest first for the rest."""
    if clicked == current:
        return current, not descending
    return clicked, clicked in _DESCENDING_FIRST


def quality_button_text(key: str, count: int) -> str:
    """"1080P (12)", or just "1080P" when nothing matches."""
    label = t(QUALITY_LABELS[key])
    return f"{label} ({count})" if count > 0 else label


def chip_style(status: IndexerStatus) -> tuple[str, theme.Color]:
    """An indexer chip's text and colour."""
    label = status.label
    if status.state is IndexerState.FOUND:
        return f"{label} ({status.count})", theme.SUCCESS
    if status.state is IndexerState.EMPTY:
        return label, theme.DANGER_BUTTON
    if status.state is IndexerState.FAILED:
        return f"{label} ✗", theme.DANGER
    if status.state is IndexerState.SKIPPED:
        return label, theme.TEXT_FAINT
    return label, theme.PENDING


def failure_text(statuses: Iterable[IndexerStatus]) -> str:
    """"EZTV: timed out · Bitsearch: …" for the indexers that failed ("" for none)."""
    parts: list[str] = []
    for status in statuses:
        if status.state is not IndexerState.FAILED:
            continue
        message = status.message or t("failed")
        if not message.startswith(f"{status.label}:"):  # IndexerError texts already do
            message = f"{status.label}: {message}"
        parts.append(message)
    return " · ".join(parts)


def action_text(state: RowState | None, *, grabbed: bool) -> str:
    """The action cell: "⬇", or a mark for a release being / already grabbed."""
    if state is not None:
        return _ACTION_TEXT[state]
    return "✓" if grabbed else "⬇"


def selection_detail(release: TorrentRelease) -> str:
    """The selection bar's detail line: "1080p · 2.10 GB · 412 seeders", whichever parts are
    known (the quality from the release name, the size as the table shows it)."""
    quality = [release.resolution.value] if release.resolution is not None else []
    if release.is_hevc:
        quality.append("x265")
    parts = [" ".join(quality)] if quality else []
    if release.size_bytes > 0:
        parts.append(format_size(release.size_bytes))
    parts.append(tn("{n} seeder", "{n} seeders", release.seeders))
    return " · ".join(parts)


def download_button_text(state: RowState | None) -> str:
    """The selection bar's button: "⬇ Download", "Downloading…" during the grab, then the
    outcome ("✓ Opened", "✓ Saved" or "✓ Sent"). A failed grab offers the download again."""
    if state == "grabbing":
        return t(DOWNLOADING_TEXT)
    if state in _DONE_STATES:
        return outcome_text(GrabOutcome(state))
    return t(DOWNLOAD_TEXT)


def searching_text(season: int | None, episode: int | None) -> str:
    """The empty table's text while searching; `season` None for a film."""
    if season is None:
        return t("Searching indexers…")
    if episode is None:
        return t("Searching indexers for Season {season} pack…", season=f"{season:02d}")
    return t("Searching indexers for {code}…", code=f"S{season:02d}E{episode:02d}")


def hidden_text(count: int, *, showing: bool) -> str:
    if showing:
        return t("{n} hidden by name/episode matching or your filters — Hide", n=count)
    return t("{n} hidden by name/episode matching or your filters — Show", n=count)


def default_quality(target: IndexerTarget, settings: Settings) -> str:
    """The VERSION chip a target starts on: the user's quality for films or for series."""
    profile = settings.quality_movie if isinstance(target, MovieTarget) else settings.quality_tv
    return quality_key(profile)


# -- the panel ---------------------------------------------------------------------------------


class _AnswerDialog(ctk.CTkInputDialog):
    """CTkInputDialog that hands its answer to `on_answer` when it closes.

    Its own get_input() waits in a nested Tk event loop, which would stall the asyncio loop
    (and every running search) for as long as the dialog is open.
    """

    def __init__(self, *, on_answer: Callable[[str | None], None], **kwargs: Any) -> None:
        self._on_answer: Callable[[str | None], None] | None = on_answer
        super().__init__(**kwargs)

    def destroy(self) -> None:
        callback, self._on_answer = self._on_answer, None
        super().destroy()
        if callback is not None:
            callback(self._user_input)


class ReleasePanel(ctk.CTkFrame):
    """Searches the torrent indexers for a target and grabs releases (see the module docstring
    for the API)."""

    def __init__(
        self,
        master: Any,
        *,
        target: IndexerTarget,
        search: TorrentSearchService,
        downloads: DownloadService,
        library: LibraryService,
        discovery: DiscoveryService,
        settings: SettingsService,
        status: StatusReporter,
        on_target_change: Callable[[IndexerTarget], None] | None = None,
        on_count: Callable[[int], None] | None = None,
        on_fix_match: Callable[[TvTarget], None] | None = None,
    ) -> None:
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_width=1,
            border_color=theme.EDGE,
            corner_radius=8,
        )
        self._target: IndexerTarget = target
        self._search = search
        self._downloads = downloads
        self._library = library
        self._discovery = discovery
        self._settings = settings
        self._status = status
        self._on_target_change = on_target_change
        self._on_count = on_count
        self._on_fix_match = on_fix_match

        self._started = False
        self._episodes: list[Episode] = []
        self._quality = default_quality(target, settings.current)
        self._update: SearchUpdate | None = None
        self._searching = False
        self._sort_column: SortColumn = "size"
        self._sort_descending = True
        self._show_hidden = False
        self._grabbed: frozenset[str] = frozenset()
        self._row_states: dict[str, RowState] = {}
        self._rows: dict[str, tuple[TorrentRelease, bool]] = {}  # id -> (release, hidden)
        self._rendered: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._failures = ""
        self._count: int | None = None  # what on_count was last told
        self._fixing = False
        self._chips: dict[str, ctk.CTkLabel] = {}
        self._chip_urls: dict[str, str | None] = {}
        self._chip_tips: dict[str, Tooltip] = {}
        self._quality_buttons: dict[str, ctk.CTkButton] = {}
        self._quality_painted: dict[str, tuple[str, bool]] = {}
        self._season_buttons: dict[int, ctk.CTkButton] = {}
        self._episode_buttons: dict[int | None, ctk.CTkButton] = {}  # None = the Pack button
        self._loading: LoadingBar | None = None
        self._empty_look: tuple[str, str] | None = None
        self._selected_name = ""  # the selection bar's release name, before eliding
        self._name_room = 0.0  # the room for it, in CTk units (0 until laid out)

        self._search_task: asyncio.Task[None] | None = None
        self._grab_tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._details_task: asyncio.Task[MovieDetails | None] | None = None
        self._episodes_task: asyncio.Task[None] | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(_TABLE_ROW, weight=1)
        self._build_selectors()
        self._build_results()
        self._layout_selectors()
        if isinstance(target, TvTarget):
            self._render_seasons()
            self._render_episodes()

    # -- public ------------------------------------------------------------------------------

    @property
    def target(self) -> IndexerTarget:
        return self._target

    def start(self) -> None:
        """Load what the chips and the search need, and search (once)."""
        if self._started:
            return
        self._started = True
        self._start(self._load_grabbed(), "grabbed")
        self._load_target_data()
        self._start_search()

    def set_target(self, target: IndexerTarget) -> None:
        """Switch to `target`: the chips follow and, once started, the panel searches again."""
        if target == self._target:
            return
        old, self._target = self._target, target
        if type(old) is not type(target):
            self._quality = default_quality(target, self._settings.current)
            self._layout_selectors()
        new_subject = not (
            isinstance(old, TvTarget)
            and isinstance(target, TvTarget)
            and old.show.id == target.show.id
        )
        if isinstance(target, TvTarget):
            if new_subject:
                self._episodes = []
            same_season = (
                not new_subject and isinstance(old, TvTarget) and old.season == target.season
            )
            if new_subject or target.season not in self._season_buttons:
                self._render_seasons()
            if not same_season or target.episode not in self._episode_buttons:
                self._render_episodes()
            self._paint_selectors()
        else:
            self._clear_tv_selectors()
        if new_subject and self._started:
            self._load_target_data()
        if self._started:
            self._start_search()
        if self._on_target_change is not None:
            self._on_target_change(target)

    def show_message(self, text: str, color: theme.Color = theme.TEXT_MUTED) -> None:
        """Show `text` on the message line under the table ("" hides it)."""
        self._set_message(text, color)

    def focus_table(self) -> None:
        self._tree.focus_set()

    # -- layout ------------------------------------------------------------------------------

    def _build_selectors(self) -> None:
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=_SELECTORS_ROW, column=0, sticky="ew", padx=10, pady=(10, 0))
        frame.grid_columnconfigure(0, weight=1)
        self._season_row, self._season_chips = self._selector_row(
            frame, 0, t("SEASON"), fix_match=self._on_fix_match is not None
        )
        self._episode_row, self._episode_chips = self._selector_row(frame, 1, t("EPISODE"))
        versions = self._selector_row(frame, 2, t("VERSION"))[1]
        for key in QUALITY_KEYS:
            button = ctk.CTkButton(
                versions,
                text=t(QUALITY_LABELS[key]),
                width=80,
                height=30,
                corner_radius=4,
                font=theme.font(11, bold=True),
                command=lambda k=key: self._select_quality(k),
            )
            button.pack(side="left", padx=3)
            self._quality_buttons[key] = button
        self._paint_quality({})

    def _layout_selectors(self) -> None:
        """The SEASON and EPISODE rows are there for a series only."""
        for row in (self._season_row, self._episode_row):
            if isinstance(self._target, TvTarget):
                row.grid()
            else:
                row.grid_remove()

    def _selector_row(
        self, master: ctk.CTkFrame, row: int, label: str, *, fix_match: bool = False
    ) -> tuple[ctk.CTkFrame, ctk.CTkScrollableFrame]:
        """A labelled row of chips (grid `row` of `master`); the "✎ Fix Match" link at its
        right when `fix_match`. Returns the row and the frame the chips go in."""
        frame = ctk.CTkFrame(master, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", pady=2)
        ctk.CTkLabel(
            frame,
            text=label,
            width=70,
            anchor="w",
            font=theme.font(12, bold=True),
            text_color=theme.TEXT_DIM,
        ).pack(side="left")
        if fix_match:
            ctk.CTkButton(
                frame,
                text=t("✎ Fix Match"),
                width=60,
                height=20,
                fg_color="transparent",
                hover_color=theme.SUGGESTION_BORDER,
                text_color=theme.BUTTON_TEXT,
                font=theme.font(10),
                command=self._fix_match,
            ).pack(side="right", padx=(10, 0))
        # Most rows fit, but long-running daily shows can have hundreds of episodes in a
        # season: the scrollbar appears only then (it blends into the card until hovered), so
        # the rows stay chip-high. No corner radius: it would pad the chips.
        scroll = ctk.CTkScrollableFrame(
            frame,
            orientation="horizontal",
            height=_SELECTOR_HEIGHT,
            corner_radius=0,
            fg_color="transparent",
            scrollbar_button_color=theme.SURFACE,
            scrollbar_button_hover_color=theme.EDGE,
        )
        scroll.pack(side="left", fill="x", expand=True)
        fit = functools.partial(ReleasePanel._fit_scrollbar, scroll)
        scroll.bind("<Configure>", lambda _event: fit(), add="+")
        scroll._parent_canvas.bind("<Configure>", lambda _event: fit(), add="+")
        scroll._scrollbar.grid_remove()
        return frame, scroll

    @staticmethod
    def _fit_scrollbar(scroll: ctk.CTkScrollableFrame) -> None:
        """Show a selector row's scrollbar only while its chips overflow."""
        canvas_width = scroll._parent_canvas.winfo_width()
        overflow = canvas_width > 1 and scroll.winfo_reqwidth() > canvas_width
        bar = scroll._scrollbar
        if overflow and not bar.winfo_manager():
            bar.grid()
        elif not overflow and bar.winfo_manager():
            bar.grid_remove()

    def _build_results(self) -> None:
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=_TOP_ROW, column=0, sticky="ew", padx=10, pady=10)
        self._results_title = ctk.CTkLabel(
            top, text=t("Releases"), font=theme.font(16, bold=True), text_color=theme.TEXT
        )
        self._results_title.pack(side="left")
        # 1 x 1 while it holds no chip (a CTkFrame is 200 x 200 by default: the title's row
        # would stand that tall, gaps above and under it); the chips make it as big as they are.
        self._chip_bar = ctk.CTkFrame(top, fg_color="transparent", width=1, height=1)
        self._chip_bar.pack(side="right")

        table = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        table.grid(row=_TABLE_ROW, column=0, sticky="nsew", padx=10, pady=(0, 6))
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(0, weight=1)
        scaling = float(ctk.ScalingTracker.get_widget_scaling(self))
        self._style_table(scaling)
        tree = ttk.Treeview(
            table, columns=_COLUMN_IDS, show="headings", selectmode="browse", style=_STYLE
        )
        for column, heading, width, anchor, stretch in _COLUMNS:
            tree.column(
                column,
                width=round(width * scaling),
                minwidth=round(min(width, 60) * scaling),
                anchor=anchor,
                stretch=stretch,
            )
            if column == "action":
                tree.heading(column, text=heading, anchor=anchor)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ctk.CTkScrollbar(table, command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        tree.bind("<ButtonRelease-1>", self._on_click)
        tree.bind("<Double-1>", self._on_double_click)
        tree.bind("<Return>", self._on_return)
        tree.bind("<MouseWheel>", self._on_wheel)
        tree.bind("<KP_Enter>", self._on_return)
        # Mouse and keyboard (Up/Down) selection both fill the selection bar.
        tree.bind("<<TreeviewSelect>>", lambda _event: self._paint_selection())
        self._tree = tree
        self._paint_rows()
        self._update_headings()
        self._build_selection_bar()

        self._empty = ctk.CTkLabel(table, text="", fg_color=theme.SURFACE, anchor="w")
        self._hint = ctk.CTkLabel(
            self,
            text="",
            font=theme.font(11),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            cursor="hand2",
            height=20,
        )
        self._hint.bind("<Button-1>", lambda _event: self._toggle_hidden())
        self._message = ctk.CTkLabel(self, text="", font=theme.font(11), anchor="w", height=20)

    def _build_selection_bar(self) -> None:
        """The bar under the table: the selected release and a big Download button. It sits in
        a row without weight, so on a short screen the table shrinks instead of the bar."""
        bar = ctk.CTkFrame(
            self,
            fg_color=theme.ROW_SELECTED,
            border_width=1,
            border_color=theme.EDGE,
            corner_radius=12,
        )
        bar.grid(row=_BAR_ROW, column=0, sticky="ew", padx=10, pady=(14, 10))
        self._download_button = ctk.CTkButton(
            bar,
            text=DOWNLOAD_TEXT,
            width=160,
            height=44,
            font=theme.font(15, bold=True),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            state="disabled",
            command=self._grab_selected,
        )
        self._download_button.pack(side="right", padx=16, pady=12)
        info = ctk.CTkFrame(bar, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=(16, 0), pady=12)  # centred
        info.bind("<Configure>", self._on_bar_resize)
        self._bar_info = info
        self._bar_caption = ctk.CTkLabel(
            info, text="", font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w", height=18
        )
        self._bar_caption.pack(fill="x")
        self._bar_name_font = theme.font(14, bold=True)
        self._bar_name = ctk.CTkLabel(
            info, text="", font=self._bar_name_font, text_color=theme.TEXT, anchor="w", height=22
        )
        self._bar_detail = ctk.CTkLabel(
            info, text="", font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w", height=18
        )
        self._paint_selection()

    def _on_bar_resize(self, event: tk.Event[Any]) -> None:
        room = unscaled_width(self._bar_info, event.width) - SLACK
        if room != self._name_room:
            self._name_room = room
            self._paint_name()

    def _paint_name(self) -> None:
        text = self._selected_name
        if text and self._name_room > 0:
            text = elide_to_width(text, self._name_room, self._bar_name_font.measure)
        if self._bar_name.cget("text") != text:
            self._bar_name.configure(text=text)

    def _selected_iid(self) -> str | None:
        """The selected row's id, if it is still in the table."""
        selection = self._tree.selection()
        return selection[0] if selection and selection[0] in self._rows else None

    def _paint_selection(self) -> None:
        """Fill the selection bar from the table's selection and that row's grab state."""
        iid = self._selected_iid()
        if iid is None:
            self._selected_name = ""
            self._bar_caption.configure(text=t(NO_SELECTION_TEXT))
            self._bar_name.pack_forget()
            self._bar_detail.pack_forget()
            self._download_button.configure(text=t(DOWNLOAD_TEXT), state="disabled")
            return
        release = self._rows[iid][0]
        state = self._row_states.get(iid)
        self._selected_name = release.title
        self._bar_caption.configure(text=t("Selected"))
        self._paint_name()
        self._bar_detail.configure(text=selection_detail(release))
        if not self._bar_name.winfo_manager():
            self._bar_name.pack(fill="x")
            self._bar_detail.pack(fill="x")
        self._download_button.configure(
            text=download_button_text(state),
            state="disabled" if state == "grabbing" else "normal",
        )

    def _style_table(self, scaling: float) -> None:
        """The "AG." ttk styles in the current mode's colours (Tk fonts and row heights are
        physical pixels)."""
        self._table_scaling = scaling
        pick = theme.pick
        surface, tab_bg = pick(theme.SURFACE), pick(theme.TAB_BG)
        style = ttk.Style(self)
        if style.theme_use() != "clam":
            style.theme_use("clam")  # the native Windows themes ignore heading colours
        size = -round(_TABLE_FONT_SIZE * scaling)  # negative = pixels, like CTk's own fonts
        style.configure(
            _STYLE,
            background=surface,
            fieldbackground=surface,
            foreground=pick(theme.TEXT_MUTED),
            rowheight=round(_ROW_HEIGHT * scaling),
            font=("Consolas", size),
            borderwidth=0,
            relief="flat",
            bordercolor=surface,
            lightcolor=surface,
            darkcolor=surface,
        )
        # Just the rows: no field border around them.
        style.layout(_STYLE, [("Treeview.treearea", {"sticky": "nswe"})])
        # The chosen row: clam's own white text only suits the dark row colour.
        style.map(
            _STYLE,
            background=[("selected", pick(theme.ROW_SELECTED))],
            foreground=[("selected", pick(theme.TEXT))],
        )
        style.configure(
            f"{_STYLE}.Heading",
            background=tab_bg,
            foreground=pick(theme.ACCENT),
            font=("Consolas", size, "bold"),
            relief="flat",
            borderwidth=0,
            bordercolor=tab_bg,
            lightcolor=tab_bg,
            darkcolor=tab_bg,
        )
        style.map(
            f"{_STYLE}.Heading",
            background=[("active", tab_bg)],
            relief=[("active", "flat"), ("pressed", "flat")],
        )

    def _paint_rows(self) -> None:
        """The rows' text colours by tag, in the current mode."""
        tree, pick = self._tree, theme.pick
        tree.tag_configure("preferred", foreground=pick(theme.SECTION_HOT))
        tree.tag_configure("grabbed", foreground=pick(theme.SUCCESS))
        tree.tag_configure("hidden", foreground=pick(theme.TEXT_FAINT))

    def _set_appearance_mode(self, mode_string: str) -> None:
        """Light <-> dark: CTk repaints its widgets; the table's ttk styles follow here."""
        super()._set_appearance_mode(mode_string)
        if hasattr(self, "_tree"):
            self._style_table(self._table_scaling)
            self._paint_rows()

    def _start(self, coro: Coroutine[Any, Any, T], name: str) -> asyncio.Task[T]:
        """Spawn a helper task that destroy() cancels."""
        task = spawn(coro, name=f"ReleasePanel.{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _report(self, text: str, clear_after_ms: int) -> None:
        # The status bar belongs to the main window, which may outlive this panel (or not).
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass

    # -- the target's data: episodes, film details -----------------------------------------------

    def _load_target_data(self) -> None:
        """Start loading the current target's episodes (a series) or details (a film),
        replacing a load for an earlier target."""
        for task in (self._episodes_task, self._details_task):
            if task is not None and not task.done():
                task.cancel()
        self._episodes_task = self._details_task = None
        target = self._target
        if isinstance(target, TvTarget):
            self._episodes_task = self._start(self._load_episodes(target.show), "episodes")
        else:
            self._details_task = self._start(self._load_details(target.movie), "details")

    async def _load_details(self, movie: Movie) -> MovieDetails | None:
        try:
            return await self._discovery.movie_details(movie.id)
        except TMDBError as exc:
            log.info("No TMDB details for %s (TMDB id %d): %s", movie.title, movie.id, exc)
            return None

    async def _load_episodes(self, show: Show) -> None:
        episodes: list[Episode] = []
        try:
            episodes = await self._library.episodes(show.id)
        finally:
            # Also on failure: the selectors then offer the target's own season and episode.
            target = self._target
            if (
                self.winfo_exists()
                and isinstance(target, TvTarget)
                and target.show.id == show.id
            ):
                self._episodes = episodes
                self._render_seasons()
                self._render_episodes()

    # -- fix match -----------------------------------------------------------------------------

    def _fix_match(self) -> None:
        if not self._fixing and isinstance(self._target, TvTarget):
            self._start(self._run_fix_match(self._target), "fix_match")

    async def _run_fix_match(self, target: TvTarget) -> None:
        self._fixing = True
        try:
            show = await self._track_correct_show(target.show)
        finally:
            self._fixing = False
        if show is None or not self.winfo_exists():
            return
        self._report(t("Now tracking {name} (TVMaze {id})", name=show.name, id=show.id), 5000)
        current = self._target
        if self._on_fix_match is not None and isinstance(current, TvTarget):
            self._on_fix_match(TvTarget(show, current.season, current.episode))

    async def _track_correct_show(self, old: Show) -> Show | None:
        """Ask for the right TVMaze id and track that show in place of `old` (untracking `old`
        if it was tracked). None when the user cancels or the show can't be fetched."""
        answer = (await self._ask_tvmaze_id() or "").strip()
        if not answer or not self.winfo_exists():
            return None
        if not (answer.isascii() and answer.isdigit()):
            self._set_message(
                t("“{answer}” is not a TVMaze ID (a number).", answer=answer), theme.DANGER
            )
            return None
        new_id = int(answer)
        if new_id == old.id:
            return None
        self._set_message(t("Fetching TVMaze show {id}…", id=new_id), theme.TEXT_MUTED)
        try:
            was_tracked = any(entry.show.id == old.id for entry in await self._library.tracked())
            show = await self._library.track(new_id)
            if was_tracked:
                await self._library.untrack(old.id)
        except TVMazeNotFound:
            message = t("TVMaze has no show with ID {id}.", id=new_id)
        except TVMazeError as exc:
            message = t("Couldn't fetch TVMaze show {id}: {error}", id=new_id, error=exc)
        else:
            return show
        if self.winfo_exists():
            self._set_message(message, theme.DANGER)
        return None

    async def _ask_tvmaze_id(self) -> str | None:
        answer: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

        def on_answer(value: str | None) -> None:
            if not answer.done():
                answer.set_result(value)

        dialog = _AnswerDialog(
            on_answer=on_answer,
            text=t("Enter the correct TVMaze ID for this show:"),
            title=t("Fix Match"),
        )
        window = self.winfo_toplevel()
        scaling = ctk.ScalingTracker.get_window_scaling(window)
        x = window.winfo_rootx() + window.winfo_width() // 2 - round(170 * scaling)
        y = window.winfo_rooty() + window.winfo_height() // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            return await answer
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

    # -- selectors -----------------------------------------------------------------------------

    def _render_seasons(self) -> None:
        target = self._target
        if not isinstance(target, TvTarget):
            return
        for button in self._season_buttons.values():
            button.destroy()
        seasons = sorted({episode.season for episode in self._episodes} | {target.season})
        self._season_buttons = {
            season: self._selector_button(
                self._season_chips, str(season), functools.partial(self._select_season, season)
            )
            for season in seasons
        }
        self._paint_selectors()

    def _render_episodes(self) -> None:
        target = self._target
        if not isinstance(target, TvTarget):
            return
        for button in self._episode_buttons.values():
            button.destroy()
        numbers = [
            episode.number
            for episode in self._episodes
            if episode.season == target.season and episode.number is not None
        ]
        last = max([*numbers, target.episode or 0])
        pack = self._selector_button(
            self._episode_chips,
            t("Pack"),
            self._select_pack,
            width=45,
            padx=(3, 8),
            font=theme.font(11, bold=True),
        )
        buttons: dict[int | None, ctk.CTkButton] = {None: pack}
        for number in range(1, last + 1):
            buttons[number] = self._selector_button(
                self._episode_chips, str(number), functools.partial(self._select_episode, number)
            )
        self._episode_buttons = buttons
        self._paint_selectors()

    def _clear_tv_selectors(self) -> None:
        """A film: no season or episode chips."""
        for button in [*self._season_buttons.values(), *self._episode_buttons.values()]:
            button.destroy()
        self._season_buttons = {}
        self._episode_buttons = {}
        self._episodes = []

    @staticmethod
    def _selector_button(
        master: Any,
        text: str,
        command: Callable[[], None],
        *,
        width: int = 35,
        padx: int | tuple[int, int] = 3,
        **options: Any,
    ) -> ctk.CTkButton:
        button = ctk.CTkButton(
            master,
            text=text,
            width=width,
            height=25,
            fg_color=theme.CHIP,
            hover_color=theme.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            command=command,
            **options,
        )
        button.pack(side="left", padx=padx)
        return button

    @staticmethod
    def _paint(button: ctk.CTkButton, selected: bool) -> None:
        button.configure(**theme.choice_colors(selected))

    def _paint_selectors(self) -> None:
        target = self._target
        if not isinstance(target, TvTarget):
            return
        for season, button in self._season_buttons.items():
            self._paint(button, season == target.season)
        for number, button in self._episode_buttons.items():
            self._paint(button, number == target.episode)

    def _select_season(self, season: int) -> None:
        target = self._target
        if isinstance(target, TvTarget) and season != target.season:
            episode = None if target.episode is None else 1
            self.set_target(TvTarget(target.show, season, episode))

    def _select_pack(self) -> None:
        target = self._target
        if isinstance(target, TvTarget) and target.episode is not None:
            self.set_target(TvTarget(target.show, target.season, None))

    def _select_episode(self, number: int) -> None:
        target = self._target
        if isinstance(target, TvTarget) and number != target.episode:
            self.set_target(TvTarget(target.show, target.season, number))

    def _select_quality(self, key: str) -> None:
        if key != self._quality:
            self._quality = key
            self._render()

    def _paint_quality(self, counts: dict[str, int]) -> None:
        for key, button in self._quality_buttons.items():
            look = (quality_button_text(key, counts.get(key, 0)), key == self._quality)
            if self._quality_painted.get(key) == look:
                continue
            self._quality_painted[key] = look
            text, selected = look
            button.configure(text=text, **theme.choice_colors(selected))

    # -- searching -----------------------------------------------------------------------------

    def _start_search(self) -> None:
        """Search for the current target, replacing any search still running."""
        if self._search_task is not None and not self._search_task.done():
            self._search_task.cancel()
        self._update = None
        self._searching = True
        self._failures = ""
        self._set_message("")
        self._show_loading(True)
        self._render()
        self._search_task = spawn(self._run_search(), name="ReleasePanel.search")

    async def _run_search(self) -> None:
        try:
            request = await self._request()
            async with contextlib.aclosing(self._search.search(request)) as updates:
                async for update in updates:
                    if not self.winfo_exists():
                        return
                    self._apply(update)
        finally:
            # A search that failed (not one replaced by a newer search) stops looking busy.
            if (
                self._search_task is asyncio.current_task()
                and self._searching
                and self.winfo_exists()
            ):
                self._searching = False
                self._show_loading(False)
                self._render()

    async def _request(self) -> SearchRequest:
        target = self._target
        if isinstance(target, MovieTarget):
            return self._search.movie_request(target.movie, await self._details_within())
        if target.episode is None:
            return self._search.season_request(target.show, target.season)
        return self._search.episode_request(target.show, target.season, target.episode)

    async def _details_within(self) -> MovieDetails | None:
        """The film's TMDB details (for its IMDb id) if they arrive within _DETAILS_WAIT."""
        task = self._details_task
        if task is None:
            return None
        await asyncio.wait([task], timeout=_DETAILS_WAIT)
        if task.done() and not task.cancelled() and task.exception() is None:
            return task.result()
        return None

    def _apply(self, update: SearchUpdate) -> None:
        self._update = update
        self._searching = not update.done
        if update.done:
            self._show_loading(False)
        self._render_chips(update.statuses)
        failures = failure_text(update.statuses)
        if failures != self._failures:
            self._failures = failures
            self._set_message(failures, theme.DANGER)
        self._render()

    def _show_loading(self, visible: bool) -> None:
        if visible and self._loading is None:
            self._loading = LoadingBar(self)
            self._loading.grid(row=_LOADING_ROW, column=0, sticky="ew", padx=10, pady=(0, 6))
        elif not visible and self._loading is not None:
            self._loading.destroy()
            self._loading = None

    def _render_chips(self, statuses: Sequence[IndexerStatus]) -> None:
        for status in statuses:
            text, color = chip_style(status)
            chip = self._chips.get(status.name)
            if chip is None:
                chip = ctk.CTkLabel(
                    self._chip_bar,
                    text=text,
                    text_color=color,
                    font=("Consolas", 11, "bold"),
                    cursor="hand2",
                )
                chip.pack(side="left", padx=(10, 0))
                chip.bind("<Button-1>", lambda _event, name=status.name: self._open_indexer(name))
                self._chips[status.name] = chip
                self._chip_tips[status.name] = add_tooltip(chip, "")
            elif chip.cget("text") != text or chip.cget("text_color") != color:
                chip.configure(text=text, text_color=color)
            self._chip_urls[status.name] = status.web_url
            self._chip_tips[status.name].set_text(t(CHIP_TIP) if status.web_url else "")

    def _open_indexer(self, name: str) -> None:
        url = self._chip_urls.get(name)
        if url:
            webbrowser.open(url)

    # -- the table -----------------------------------------------------------------------------

    def _render(self) -> None:
        """Quality counts, results title, rows, hidden hint and empty state, from the latest
        update and the current filters."""
        update = self._update
        counts = quality_counts(update.releases if update else ())
        self._paint_quality(counts)
        found = counts[self._quality]
        self._results_title.configure(
            text=(
                t("Searching… {n} found", n=found)
                if self._searching
                else t("Releases ({n})", n=found)
            )
        )
        self._render_rows()
        hidden_releases = update.hidden if update else ()
        hidden = sum(matches_quality(release, self._quality) for release in hidden_releases)
        if hidden:
            self._hint.configure(text=hidden_text(hidden, showing=self._show_hidden))
            self._hint.grid(row=_HINT_ROW, column=0, sticky="ew", padx=12, pady=(0, 2))
        else:
            self._hint.grid_remove()
        self._render_empty_state()
        if found != self._count:
            self._count = found
            if self._on_count is not None:
                self._on_count(found)

    def _visible_releases(self) -> list[tuple[TorrentRelease, bool]]:
        """(release, hidden) in table order: the matching releases, then (when shown) the
        hidden ones, each part sorted on its own."""
        update = self._update
        if update is None:
            return []
        column, descending = self._sort_column, self._sort_descending
        shown = [release for release in update.releases if matches_quality(release, self._quality)]
        rows = [(release, False) for release in sort_releases(shown, column, descending=descending)]
        if self._show_hidden:
            hidden = [r for r in update.hidden if matches_quality(r, self._quality)]
            rows += [(r, True) for r in sort_releases(hidden, column, descending=descending)]
        return rows

    def _render_rows(self) -> None:
        """Diff the table against the wanted rows: rows keep their id, selection and scroll."""
        wanted: dict[str, tuple[TorrentRelease, bool]] = {}
        for release, hidden in self._visible_releases():
            wanted.setdefault(release_id(release), (release, hidden))
        tree = self._tree
        gone = [iid for iid in self._rendered if iid not in wanted]
        if gone:
            tree.delete(*gone)
            for iid in gone:
                del self._rendered[iid]
        self._rows = wanted
        settings = self._settings.current
        now = datetime.now(UTC)
        for iid in wanted:
            self._render_row(iid, settings, now)
        order = tuple(wanted)
        if tree.get_children() != order:
            tree.set_children("", *order)
        self._paint_selection()

    def _render_row(self, iid: str, settings: Settings, now: datetime) -> None:
        release, hidden = self._rows[iid]
        grabbed = release.info_hash is not None and release.info_hash in self._grabbed
        state = self._row_states.get(iid)
        values = (
            release.title,
            format_size(release.size_bytes),
            str(release.seeders),
            str(release.leechers),
            release.source.upper(),
            format_age(release.published_at, now),
            action_text(state, grabbed=grabbed),
        )
        if grabbed or state in _DONE_STATES:
            tags: tuple[str, ...] = ("grabbed",)
        elif hidden:
            tags = ("hidden",)
        elif is_preferred(release, settings):
            tags = ("preferred",)
        else:
            tags = ()
        rendered = self._rendered.get(iid)
        if rendered is None:
            self._tree.insert("", "end", iid=iid, values=values, tags=tags)
        elif rendered != (values, tags):
            self._tree.item(iid, values=values, tags=tags)
        self._rendered[iid] = (values, tags)

    def _refresh_row(self, iid: str) -> None:
        if iid in self._rows:
            self._render_row(iid, self._settings.current, datetime.now(UTC))
            if iid == self._selected_iid():
                self._paint_selection()

    def _render_empty_state(self) -> None:
        if self._rows:
            if self._empty_look is not None:
                self._empty_look = None
                self._empty.place_forget()
            return
        if self._searching:
            target = self._target
            if isinstance(target, TvTarget):
                text = searching_text(target.season, target.episode)
            else:
                text = searching_text(None, None)
            look = (text, "bold")
        else:
            look = (t("No matching releases found."), "normal")
        if look == self._empty_look:
            return
        self._empty_look = look
        text, weight = look
        self._empty.configure(
            text=text,
            text_color=theme.ACCENT if weight == "bold" else theme.TEXT_QUIET,
            font=("Consolas", 12, weight),
        )
        self._empty.place(x=12, y=_ROW_HEIGHT + 12, anchor="nw")
        self._empty.lift()

    def _update_headings(self) -> None:
        for column, heading, _width, anchor, _stretch in _COLUMNS:
            if column == "action":
                continue
            arrow = ""
            if column == self._sort_column:
                arrow = " ▼" if self._sort_descending else " ▲"
            self._tree.heading(
                column,
                text=t(heading) + arrow,
                anchor=anchor,
                command=functools.partial(self._sort_by, column),
            )

    def _sort_by(self, column: SortColumn) -> None:
        self._sort_column, self._sort_descending = next_sort(
            self._sort_column, self._sort_descending, column
        )
        self._update_headings()
        self._render_rows()

    def _toggle_hidden(self) -> None:
        self._show_hidden = not self._show_hidden
        self._render()

    def _set_message(self, text: str, color: theme.Color = theme.TEXT_MUTED) -> None:
        if text:
            self._message.configure(text=text, text_color=color)
            self._message.grid(row=_MESSAGE_ROW, column=0, sticky="ew", padx=12, pady=(0, 8))
        else:
            self._message.grid_remove()

    # -- grabbing ------------------------------------------------------------------------------

    def _on_click(self, event: tk.Event[Any]) -> None:
        tree = self._tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        if tree.identify_column(event.x) == _ACTION_COLUMN:
            self._grab(tree.identify_row(event.y))

    def _on_double_click(self, event: tk.Event[Any]) -> None:
        tree = self._tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        if tree.identify_column(event.x) != _ACTION_COLUMN:  # the click already grabbed there
            self._grab(tree.identify_row(event.y))

    def _on_wheel(self, event: tk.Event[Any]) -> str:
        """Scroll the table only: "break" keeps a scrolling window around the panel still."""
        steps = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
        self._tree.yview_scroll(steps, "units")
        return "break"

    def _on_return(self, _event: tk.Event[Any]) -> None:
        for iid in self._tree.selection():
            self._grab(iid)

    def _grab_selected(self) -> None:
        iid = self._selected_iid()
        if iid is not None:
            self._grab(iid)

    def _grab_target(self) -> GrabTarget:
        target = self._target
        if isinstance(target, TvTarget):
            return GrabTarget(
                media_type=MediaType.TV,
                media_id=target.show.id,
                season=target.season,
                episode=target.episode,
            )
        return GrabTarget(media_type=MediaType.MOVIE, media_id=target.movie.id)

    def _grab(self, iid: str) -> None:
        row = self._rows.get(iid)
        running = self._grab_tasks.get(iid)
        if row is None or (running is not None and not running.done()):
            return
        self._row_states[iid] = "grabbing"
        self._refresh_row(iid)
        task = spawn(self._run_grab(iid, row[0], self._grab_target()), name="ReleasePanel.grab")
        self._grab_tasks[iid] = task
        task.add_done_callback(functools.partial(self._forget_grab, iid))

    def _forget_grab(self, iid: str, task: asyncio.Task[None]) -> None:
        if self._grab_tasks.get(iid) is task:
            del self._grab_tasks[iid]

    async def _run_grab(self, iid: str, release: TorrentRelease, target: GrabTarget) -> None:
        try:
            result = await self._downloads.grab(release, target)
        except (GrabError, RemoteError) as exc:
            self._finish_grab(iid, "failed", str(exc), theme.DANGER, 10_000)
            return
        except Exception:
            if self.winfo_exists():
                self._row_states[iid] = "failed"
                self._refresh_row(iid)
            raise  # logged and reported by the task runner
        state = _OUTCOME_STATES[result.outcome]
        self._finish_grab(iid, state, result.message, theme.SUCCESS, 6000)
        await self._load_grabbed()

    def _finish_grab(
        self, iid: str, state: RowState, message: str, color: theme.Color, clear_after_ms: int
    ) -> None:
        self._report(message, clear_after_ms)
        if not self.winfo_exists():
            return
        self._row_states[iid] = state
        self._refresh_row(iid)
        self._set_message(message, color)

    async def _load_grabbed(self) -> None:
        grabbed = await self._downloads.grabbed_hashes()
        if self.winfo_exists() and grabbed != self._grabbed:
            self._grabbed = grabbed
            self._render_rows()

    # -- teardown ------------------------------------------------------------------------------

    def destroy(self) -> None:
        tasks = [self._search_task, *self._grab_tasks.values(), *self._tasks]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        self._search_task = None
        self._grab_tasks = {}
        self._tasks = set()
        self._episodes_task = self._details_task = None
        if self._loading is not None:
            self._loading.destroy()
            self._loading = None
        super().destroy()
