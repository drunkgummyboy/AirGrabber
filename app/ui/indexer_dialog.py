"""The "Advanced Indexer Interrogation" dialog: search every torrent indexer for one episode,
season pack or film, filter and sort what they found, and grab a release.

The results table is a ttk.Treeview rather than CTk widgets: a search can return hundreds of
rows and re-renders on every indexer update, which must stay instant. Rows are keyed by a
stable id (`release_id`), so updates are applied as a diff and a row's selection and grab state
survive re-renders.

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
from app.ui.branding import set_window_icon
from app.ui.download_control import outcome_text
from app.ui.media_cards import format_runtime
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, unscaled_width
from app.ui.tooltip import Tooltip, add_tooltip
from app.ui.widgets import LoadingBar, PosterImage

if TYPE_CHECKING:
    from app.services import (
        DownloadService,
        ImageCache,
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
    "any": "ANY",
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
DOWNLOAD_TEXT = "⬇ Download"
DOWNLOADING_TEXT = "Downloading…"
NO_SELECTION_TEXT = "Click a release above to select it"
_SIZE_UNITS = ("KB", "MB", "GB", "TB", "PB")
_UNKNOWN = "—"
_DETAILS_WAIT = 5.0  # seconds a movie search waits for TMDB details (for the IMDb id)

# (id, heading, width in CTk units, anchor, stretches)
_COLUMNS: tuple[tuple[ColumnId, str, int, _Anchor, bool], ...] = (
    ("name", "Name", 320, "w", True),
    ("size", "Size", 90, "e", False),
    ("seeds", "Seeds", 70, "center", False),
    ("leech", "Leech", 70, "center", False),
    ("source", "Source", 120, "center", False),
    ("age", "Age", 70, "center", False),
    ("action", "", 50, "center", False),
)
_COLUMN_IDS = tuple(column[0] for column in _COLUMNS)
_ACTION_COLUMN = f"#{_COLUMN_IDS.index('action') + 1}"
_ROW_HEIGHT = 26
_TABLE_FONT_SIZE = 11
_STYLE = "AG.Treeview"
# A dialog shorter than this (CTk units) gets the compact header, so the table keeps its rows.
COMPACT_BELOW = 700
_POSTER_SIZE = {False: (150, 225), True: (100, 150)}  # by compact
_TITLE_SIZE = {False: 24, True: 20}
_HEADER_PAD = {False: 15, True: 8}
_SELECTOR_HEIGHT = 30  # the tallest selector chip (the VERSION buttons)
CHIP_TIP = "Open this site's search in your browser"


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
    return f"{value:.2f} {_SIZE_UNITS[unit]}"


def format_age(published: datetime | None, now: datetime) -> str:
    """"45m", "5h", "3d", "2y" since `published`; "—" when unknown."""
    if published is None:
        return _UNKNOWN
    seconds = max(0.0, (now - published).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    days = int(seconds // 86400)
    return f"{days}d" if days < 365 else f"{days // 365}y"


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
    label = QUALITY_LABELS[key]
    return f"{label} ({count})" if count > 0 else label


def chip_style(status: IndexerStatus) -> tuple[str, str]:
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
        message = status.message or "failed"
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
    parts.append(f"{release.seeders} seeder" + ("" if release.seeders == 1 else "s"))
    return " · ".join(parts)


def download_button_text(state: RowState | None) -> str:
    """The selection bar's button: "⬇ Download", "Downloading…" during the grab, then the
    outcome ("✓ Opened", "✓ Saved" or "✓ Sent"). A failed grab offers the download again."""
    if state == "grabbing":
        return DOWNLOADING_TEXT
    if state in _DONE_STATES:
        return outcome_text(GrabOutcome(state))
    return DOWNLOAD_TEXT


def episode_title(show: Show, season: int, episode: int | None) -> str:
    if episode is None:
        return f"{show.name}: Season {season:02d} (Pack)"
    return f"{show.name}: S{season:02d}E{episode:02d}"


def movie_title(movie: Movie) -> str:
    return f"{movie.title} ({movie.year})" if movie.year else movie.title


def show_meta(show: Show) -> str:
    """"2022 • Drama, Mystery • RUNNING", whichever parts are known."""
    parts: list[str] = []
    if show.year:
        parts.append(str(show.year))
    if show.genres:
        parts.append(", ".join(show.genres))
    if show.status:
        parts.append(show.status.upper())
    return " • ".join(parts)


def movie_meta(movie: Movie, details: MovieDetails | None) -> str:
    """"2024 • ★ 7.3 • Rated PG-13 • 2h 46m", whichever parts are known."""
    parts: list[str] = []
    if movie.year:
        parts.append(str(movie.year))
    if movie.vote_count:
        parts.append(f"★ {movie.vote_average:.1f}")
    if details is not None and details.certification:
        parts.append(f"Rated {details.certification}")
    if details is not None and details.runtime:
        parts.append(format_runtime(details.runtime))
    return " • ".join(parts)


def searching_text(season: int | None, episode: int | None) -> str:
    """The empty table's text while searching; `season` None for a film."""
    if season is None:
        return "Searching indexers…"
    if episode is None:
        return f"Searching indexers for Season {season:02d} pack…"
    return f"Searching indexers for S{season:02d}E{episode:02d}…"


def compact_header(height: float) -> bool:
    """Whether a dialog `height` tall (CTk units) gets the compact header: a smaller poster and
    title and less padding, so the results table keeps room for its rows."""
    return height < COMPACT_BELOW


def hidden_text(count: int, *, showing: bool) -> str:
    action = "Hide" if showing else "Show"
    return f"{count} hidden by name/episode matching or your filters — {action}"


# -- dialog ------------------------------------------------------------------------------------


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


class IndexerDialog(ctk.CTkToplevel):
    """Searches the torrent indexers for `target` and grabs releases (not modal).

    The main window keeps one open at a time; `on_reopen` asks it to replace this dialog with
    one for another target (after "Fix Match").
    """

    WIDTH = 1100
    HEIGHT = 750

    def __init__(
        self,
        master: ctk.CTk,
        *,
        target: IndexerTarget,
        search: TorrentSearchService,
        downloads: DownloadService,
        library: LibraryService,
        discovery: DiscoveryService,
        images: ImageCache,
        settings: SettingsService,
        status: StatusReporter,
        on_reopen: Callable[[IndexerTarget], None],
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._target = target
        self._search = search
        self._downloads = downloads
        self._library = library
        self._discovery = discovery
        self._settings = settings
        self._status = status
        self._on_reopen = on_reopen

        self._season = target.season if isinstance(target, TvTarget) else 0
        self._episode = target.episode if isinstance(target, TvTarget) else None
        self._episodes: list[Episode] = []
        self._quality = quality_key(settings.current.quality)
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
        self._is_tracked: bool | None = None
        self._busy_tracking = False
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
        self._activate_job: str | None = None
        self._images = images
        self._poster: PosterImage | None = None
        self._poster_url: str | None = None
        self._compact: bool | None = None  # None until the header is built
        self._selected_name = ""  # the selection bar's release name, before eliding
        self._name_room = 0.0  # the room for it, in CTk units (0 until laid out)

        self._search_task: asyncio.Task[None] | None = None
        self._grab_tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._details_task: asyncio.Task[MovieDetails | None] | None = None

        self.title("Advanced Indexer Interrogation")
        set_window_icon(self)
        self.transient(master)
        # CTkToplevel only applies its dark Windows title bar reliably from resizable().
        self.resizable(True, True)
        self.minsize(900, 600)
        height = self._place_over(master)

        self._build_header(compact=compact_header(height))
        self._build_results()
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Configure>", self._on_configure)

        self._start(self._load_grabbed(), "grabbed")
        if isinstance(target, TvTarget):
            self._start(self._load_episodes(target.show), "episodes")
            self._start(self._load_tracked(target.show), "tracked")
        else:
            self._details_task = self._start(self._load_details(target.movie), "details")
        self._start_search()
        # CTkToplevel can open behind its parent on Windows; raise it once it is mapped.
        self._activate_job = self.after(150, self._activate)

    # -- layout ------------------------------------------------------------------------------

    def _place_over(self, master: ctk.CTk) -> int:
        """Centre over `master`, clamped to the screen (see MainWindow._place for the DPI math).
        Returns the height (CTk units)."""
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        screen_w, screen_h = self.wm_maxsize()
        width = min(self.WIDTH, int(screen_w / scaling) - 40)
        height = min(self.HEIGHT, int(screen_h / scaling) - 40)
        width_px, height_px = round(width * scaling), round(height * scaling)
        x = master.winfo_rootx() + (master.winfo_width() - width_px) // 2
        y = master.winfo_rooty() + (master.winfo_height() - height_px) // 2
        x = min(max(0, x), max(0, screen_w - width_px))
        y = min(max(0, y), max(0, screen_h - height_px))
        self.geometry(f"{width}x{height}+{x}+{y}")
        return height

    def _on_configure(self, event: tk.Event[Any]) -> None:
        # Bound on the toplevel, so every descendant's <Configure> passes here too.
        if event.widget is not self or event.height < 50:
            return
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        self._set_compact(compact_header(event.height / scaling))

    def _set_compact(self, compact: bool) -> None:
        """Lay the header out full size or compact (see compact_header)."""
        if compact == self._compact:
            return
        self._compact = compact
        pad = _HEADER_PAD[compact]
        self._header_card.pack_configure(padx=15, pady=pad)
        self._header_info.pack_configure(pady=pad, padx=(0, pad))
        self._title.configure(font=theme.font(_TITLE_SIZE[compact], bold=True))
        self._meta.pack_configure(pady=(0, 4) if compact else (2, 10))
        # PosterImage renders at a fixed size: a new one at the new size (the cache has the image).
        if self._poster is not None:
            self._poster.destroy()
        width, height = _POSTER_SIZE[compact]
        self._poster = PosterImage(self._header_card, width=width, height=height)
        self._poster.pack(side="left", padx=pad, pady=pad, before=self._header_info)
        self._poster.show(self._images, self._poster_url)

    def _build_header(self, *, compact: bool) -> None:
        card = ctk.CTkFrame(
            self, fg_color=theme.SURFACE, border_width=1, border_color=theme.EDGE, corner_radius=8
        )
        card.pack(fill="x", padx=15, pady=15)
        self._header_card = card

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, pady=15, padx=(0, 15))
        self._header_info = info
        title_row = ctk.CTkFrame(info, fg_color="transparent")
        title_row.pack(fill="x")
        self._title = ctk.CTkLabel(
            title_row, text="", font=theme.font(24, bold=True), text_color=theme.TEXT, anchor="w"
        )
        self._title.pack(side="left")
        self._meta = ctk.CTkLabel(
            info, text="", font=theme.font(12), text_color=theme.TEXT_DIM, anchor="w", height=20
        )
        self._meta.pack(anchor="w", pady=(2, 10))  # see _set_compact

        target = self._target
        if isinstance(target, TvTarget):
            ctk.CTkButton(
                title_row,
                text="✎ Fix Match",
                width=60,
                height=20,
                fg_color="transparent",
                hover_color=theme.SUGGESTION_BORDER,
                font=theme.font(10),
                command=self._fix_match,
            ).pack(side="left", padx=10)
            self._track_button = ctk.CTkButton(
                title_row,
                text="+ Track",
                width=60,
                height=20,
                font=theme.font(10, bold=True),
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                border_color=theme.EDGE,
                state="disabled",  # until the tracked state is known
                command=self._toggle_track,
            )
            self._track_button.pack(side="left", padx=5)
            self._meta.configure(text=show_meta(target.show))
            self._season_row = self._selector_row(info, "SEASON")
            self._episode_row = self._selector_row(info, "EPISODE")
            self._poster_url = target.show.poster_url_original or target.show.poster_url
        else:
            self._meta.configure(text=movie_meta(target.movie, None))
            self._poster_url = target.movie.poster_url("w342")
        self._update_title()

        versions = self._selector_row(info, "VERSION")
        for key in QUALITY_KEYS:
            button = ctk.CTkButton(
                versions,
                text=QUALITY_LABELS[key],
                width=80,
                height=30,
                corner_radius=4,
                font=theme.font(11, bold=True),
                command=lambda k=key: self._select_quality(k),
            )
            button.pack(side="left", padx=3)
            self._quality_buttons[key] = button
        self._paint_quality({})
        self._set_compact(compact)

    @staticmethod
    def _selector_row(master: Any, label: str) -> ctk.CTkScrollableFrame:
        row = ctk.CTkFrame(master, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(
            row,
            text=label,
            width=70,
            anchor="w",
            font=theme.font(12, bold=True),
            text_color=theme.TEXT_DIM,
        ).pack(side="left")
        # Most rows fit, but long-running daily shows can have hundreds of episodes in a
        # season: the scrollbar appears only then (it blends into the card until hovered), so
        # the rows stay chip-high. No corner radius: it would pad the chips.
        scroll = ctk.CTkScrollableFrame(
            row,
            orientation="horizontal",
            height=_SELECTOR_HEIGHT,
            corner_radius=0,
            fg_color="transparent",
            scrollbar_button_color=theme.SURFACE,
            scrollbar_button_hover_color=theme.EDGE,
        )
        scroll.pack(side="left", fill="x", expand=True)
        fit = functools.partial(IndexerDialog._fit_scrollbar, scroll)
        scroll.bind("<Configure>", lambda _event: fit(), add="+")
        scroll._parent_canvas.bind("<Configure>", lambda _event: fit(), add="+")
        scroll._scrollbar.grid_remove()
        return scroll

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
        card = ctk.CTkFrame(
            self, fg_color=theme.SURFACE, border_width=1, border_color=theme.EDGE, corner_radius=8
        )
        card.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(2, weight=1)
        self._results_card = card

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        self._results_title = ctk.CTkLabel(
            top, text="Torrents", font=theme.font(16, bold=True), text_color=theme.TEXT
        )
        self._results_title.pack(side="left")
        self._chip_bar = ctk.CTkFrame(top, fg_color="transparent")
        self._chip_bar.pack(side="right")

        table = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        table.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 6))
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
        tree.tag_configure("preferred", foreground=theme.SECTION_HOT)
        tree.tag_configure("grabbed", foreground=theme.SUCCESS)
        tree.tag_configure("hidden", foreground=theme.TEXT_FAINT)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ctk.CTkScrollbar(table, command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        tree.bind("<ButtonRelease-1>", self._on_click)
        tree.bind("<Double-1>", self._on_double_click)
        tree.bind("<Return>", self._on_return)
        tree.bind("<KP_Enter>", self._on_return)
        # Mouse and keyboard (Up/Down) selection both fill the selection bar.
        tree.bind("<<TreeviewSelect>>", lambda _event: self._paint_selection())
        self._tree = tree
        self._update_headings()
        self._build_selection_bar(card)

        self._empty = ctk.CTkLabel(table, text="", fg_color=theme.SURFACE, anchor="w")
        self._hint = ctk.CTkLabel(
            card,
            text="",
            font=theme.font(11),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            cursor="hand2",
            height=20,
        )
        self._hint.bind("<Button-1>", lambda _event: self._toggle_hidden())
        self._message = ctk.CTkLabel(card, text="", font=theme.font(11), anchor="w", height=20)

    def _build_selection_bar(self, card: ctk.CTkFrame) -> None:
        """The bar under the table: the selected release and a big Download button. It sits in
        a row without weight, so on a short screen the table shrinks instead of the bar."""
        bar = ctk.CTkFrame(
            card,
            fg_color=theme.ROW_SELECTED,
            border_width=1,
            border_color=theme.EDGE,
            corner_radius=12,
        )
        bar.grid(row=5, column=0, sticky="ew", padx=10, pady=(14, 10))
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
            self._bar_caption.configure(text=NO_SELECTION_TEXT)
            self._bar_name.pack_forget()
            self._bar_detail.pack_forget()
            self._download_button.configure(text=DOWNLOAD_TEXT, state="disabled")
            return
        release = self._rows[iid][0]
        state = self._row_states.get(iid)
        self._selected_name = release.title
        self._bar_caption.configure(text="Selected")
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
        """The dark "AG." ttk styles (Tk fonts and row heights are physical pixels)."""
        style = ttk.Style(self)
        if style.theme_use() != "clam":
            style.theme_use("clam")  # the native Windows themes ignore heading colours
        size = -round(_TABLE_FONT_SIZE * scaling)  # negative = pixels, like CTk's own fonts
        style.configure(
            _STYLE,
            background=theme.SURFACE,
            fieldbackground=theme.SURFACE,
            foreground=theme.TEXT_MUTED,
            rowheight=round(_ROW_HEIGHT * scaling),
            font=("Consolas", size),
            borderwidth=0,
            relief="flat",
            bordercolor=theme.SURFACE,
            lightcolor=theme.SURFACE,
            darkcolor=theme.SURFACE,
        )
        # Just the rows: no field border around them.
        style.layout(_STYLE, [("Treeview.treearea", {"sticky": "nswe"})])
        style.map(_STYLE, background=[("selected", theme.ROW_SELECTED)])
        style.configure(
            f"{_STYLE}.Heading",
            background=theme.TAB_BG,
            foreground=theme.ACCENT,
            font=("Consolas", size, "bold"),
            relief="flat",
            borderwidth=0,
            bordercolor=theme.TAB_BG,
            lightcolor=theme.TAB_BG,
            darkcolor=theme.TAB_BG,
        )
        style.map(
            f"{_STYLE}.Heading",
            background=[("active", theme.TAB_BG)],
            relief=[("active", "flat"), ("pressed", "flat")],
        )

    def _activate(self) -> None:
        self._activate_job = None
        if self.winfo_exists():
            self.lift()
            self.focus_force()
            self._tree.focus_set()

    def _start(self, coro: Coroutine[Any, Any, T], name: str) -> asyncio.Task[T]:
        """Spawn a helper task that destroy() cancels."""
        task = spawn(coro, name=f"IndexerDialog.{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- header: title, tracking, fix match ----------------------------------------------------

    def _update_title(self) -> None:
        target = self._target
        if isinstance(target, TvTarget):
            self._title.configure(text=episode_title(target.show, self._season, self._episode))
        else:
            self._title.configure(text=movie_title(target.movie))

    async def _load_details(self, movie: Movie) -> MovieDetails | None:
        try:
            details = await self._discovery.movie_details(movie.id)
        except TMDBError as exc:
            log.info("No TMDB details for %s (TMDB id %d): %s", movie.title, movie.id, exc)
            return None
        if self.winfo_exists():
            self._meta.configure(text=movie_meta(movie, details))
        return details

    async def _load_tracked(self, show: Show) -> None:
        entries = await self._library.tracked()
        if self.winfo_exists():
            self._is_tracked = any(entry.show.id == show.id for entry in entries)
            self._paint_track_button()

    def _paint_track_button(self) -> None:
        tracked = bool(self._is_tracked)
        self._track_button.configure(
            text="Tracking" if tracked else "+ Track",
            fg_color="transparent" if tracked else theme.ACCENT,
            border_width=1 if tracked else 0,
            state="disabled" if self._busy_tracking or self._is_tracked is None else "normal",
        )

    def _toggle_track(self) -> None:
        if self._busy_tracking or self._is_tracked is None:
            return
        assert isinstance(self._target, TvTarget)
        self._start(self._set_tracked(self._target.show, not self._is_tracked), "track")

    async def _set_tracked(self, show: Show, track: bool) -> None:
        self._busy_tracking = True
        self._paint_track_button()
        try:
            if track:
                await self._library.track(show.id)
            else:
                await self._library.untrack(show.id)
        except TVMazeError as exc:
            if self.winfo_exists():
                self._set_message(f"Couldn't track {show.name}: {exc}", theme.DANGER)
        else:
            self._is_tracked = track
            done = "Now tracking" if track else "Stopped tracking"
            self._report(f"{done} {show.name}", 4000)
        finally:
            self._busy_tracking = False
            if self.winfo_exists():
                self._paint_track_button()

    def _fix_match(self) -> None:
        if not self._fixing:
            self._start(self._run_fix_match(), "fix_match")

    async def _run_fix_match(self) -> None:
        assert isinstance(self._target, TvTarget)
        self._fixing = True
        try:
            show = await self._track_correct_show(self._target.show)
        finally:
            self._fixing = False
        if show is not None and self.winfo_exists():
            self._report(f"Now tracking {show.name} (TVMaze {show.id})", 5000)
            self._on_reopen(TvTarget(show, self._season, self._episode))

    async def _track_correct_show(self, old: Show) -> Show | None:
        """Ask for the right TVMaze id and track that show in place of `old` (untracking `old`
        if it was tracked). None when the user cancels or the show can't be fetched."""
        answer = (await self._ask_tvmaze_id() or "").strip()
        if not answer or not self.winfo_exists():
            return None
        if not (answer.isascii() and answer.isdigit()):
            self._set_message(f"“{answer}” is not a TVMaze ID (a number).", theme.DANGER)
            return None
        new_id = int(answer)
        if new_id == old.id:
            return None
        self._set_message(f"Fetching TVMaze show {new_id}…", theme.TEXT_MUTED)
        try:
            was_tracked = any(entry.show.id == old.id for entry in await self._library.tracked())
            show = await self._library.track(new_id)
            if was_tracked:
                await self._library.untrack(old.id)
        except TVMazeNotFound:
            message = f"TVMaze has no show with ID {new_id}."
        except TVMazeError as exc:
            message = f"Couldn't fetch TVMaze show {new_id}: {exc}"
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
            text="Enter the correct TVMaze ID for this show:",
            title="Fix Match",
        )
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        x = self.winfo_rootx() + self.winfo_width() // 2 - round(170 * scaling)
        y = self.winfo_rooty() + self.winfo_height() // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            return await answer
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

    # -- header: selectors ---------------------------------------------------------------------

    async def _load_episodes(self, show: Show) -> None:
        episodes: list[Episode] = []
        try:
            episodes = await self._library.episodes(show.id)
        finally:
            # Also on failure: the selectors then offer the target's own season and episode.
            if self.winfo_exists():
                self._episodes = episodes
                self._render_seasons()
                self._render_episodes()

    def _render_seasons(self) -> None:
        for button in self._season_buttons.values():
            button.destroy()
        seasons = sorted({episode.season for episode in self._episodes} | {self._season})
        self._season_buttons = {
            season: self._selector_button(
                self._season_row, str(season), functools.partial(self._select_season, season)
            )
            for season in seasons
        }
        self._paint_selectors()

    def _render_episodes(self) -> None:
        for button in self._episode_buttons.values():
            button.destroy()
        numbers = [
            episode.number
            for episode in self._episodes
            if episode.season == self._season and episode.number is not None
        ]
        last = max([*numbers, self._episode or 0])
        pack = self._selector_button(
            self._episode_row,
            "Pack",
            self._select_pack,
            width=45,
            padx=(3, 8),
            font=theme.font(11, bold=True),
        )
        buttons: dict[int | None, ctk.CTkButton] = {None: pack}
        for number in range(1, last + 1):
            buttons[number] = self._selector_button(
                self._episode_row, str(number), functools.partial(self._select_episode, number)
            )
        self._episode_buttons = buttons
        self._paint_selectors()

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
            command=command,
            **options,
        )
        button.pack(side="left", padx=padx)
        return button

    @staticmethod
    def _paint(button: ctk.CTkButton, selected: bool) -> None:
        button.configure(
            fg_color=theme.ACCENT if selected else theme.CHIP,
            hover_color=theme.ACCENT_HOVER if selected else theme.CHIP_HOVER,
        )

    def _paint_selectors(self) -> None:
        for season, button in self._season_buttons.items():
            self._paint(button, season == self._season)
        for number, button in self._episode_buttons.items():
            self._paint(button, number == self._episode)

    def _select_season(self, season: int) -> None:
        if season == self._season:
            return
        self._season = season
        if self._episode is not None:
            self._episode = 1
        self._render_episodes()
        self._paint_selectors()
        self._target_changed()

    def _select_pack(self) -> None:
        if self._episode is not None:
            self._episode = None
            self._paint_selectors()
            self._target_changed()

    def _select_episode(self, number: int) -> None:
        if number != self._episode:
            self._episode = number
            self._paint_selectors()
            self._target_changed()

    def _target_changed(self) -> None:
        self._update_title()
        self._start_search()

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
            button.configure(
                text=text,
                fg_color=theme.ACCENT if selected else theme.CHIP,
                hover_color=theme.ACCENT_HOVER if selected else theme.CHIP_HOVER,
            )

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
        self._search_task = spawn(self._run_search(), name="IndexerDialog.search")

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
        if self._episode is None:
            return self._search.season_request(target.show, self._season)
        return self._search.episode_request(target.show, self._season, self._episode)

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
            self._loading = LoadingBar(self._results_card)
            self._loading.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
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
            self._chip_tips[status.name].set_text(CHIP_TIP if status.web_url else "")

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
            text=f"Searching… {found} found" if self._searching else f"Torrents ({found})"
        )
        self._render_rows()
        hidden_releases = update.hidden if update else ()
        hidden = sum(matches_quality(release, self._quality) for release in hidden_releases)
        if hidden:
            self._hint.configure(text=hidden_text(hidden, showing=self._show_hidden))
            self._hint.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 2))
        else:
            self._hint.grid_remove()
        self._render_empty_state()

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
            season = self._season if isinstance(self._target, TvTarget) else None
            look = (searching_text(season, self._episode), "bold")
        else:
            look = ("No matching torrents found.", "normal")
        if look == self._empty_look:
            return
        self._empty_look = look
        text, weight = look
        self._empty.configure(
            text=text,
            text_color=theme.ACCENT if weight == "bold" else "gray50",
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
                text=heading + arrow,
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

    def _set_message(self, text: str, color: str = theme.TEXT_MUTED) -> None:
        if text:
            self._message.configure(text=text, text_color=color)
            self._message.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 8))
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
                season=self._season,
                episode=self._episode,
            )
        return GrabTarget(media_type=MediaType.MOVIE, media_id=target.movie.id)

    def _grab(self, iid: str) -> None:
        row = self._rows.get(iid)
        running = self._grab_tasks.get(iid)
        if row is None or (running is not None and not running.done()):
            return
        self._row_states[iid] = "grabbing"
        self._refresh_row(iid)
        task = spawn(self._run_grab(iid, row[0], self._grab_target()), name="IndexerDialog.grab")
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
        self, iid: str, state: RowState, message: str, color: str, clear_after_ms: int
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

    def _report(self, text: str, clear_after_ms: int) -> None:
        # The status bar belongs to the main window, which may outlive this one (or not).
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass

    # -- teardown ------------------------------------------------------------------------------

    def destroy(self) -> None:
        if self._activate_job is not None:
            self.after_cancel(self._activate_job)
            self._activate_job = None
        tasks = [self._search_task, *self._grab_tasks.values(), *self._tasks]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        self._search_task = None
        self._grab_tasks = {}
        self._tasks = set()
        if self._loading is not None:
            self._loading.destroy()
            self._loading = None
        super().destroy()
