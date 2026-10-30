"""The Downloads page: the grabs of the last 30 days, newest first, with where each went, what
the torrent program says about it (Downloading 42 % / Ready / Waiting / Stopped / Not found in
<program>) and its saved subtitles; a hint when no program can be asked, one plain line when it
can't be reached, and an amber banner when the downloads' disk is almost full.

The data comes from DownloadStatusService, which checks every 10 s while this page is on screen
(`<Map>` / `<Unmap>`) and tells the page about every new overview; rows are updated in place
while the list of grabs stays the same. A row opens the show's window (an episode) or the film's
info window (`on_open_film`; on IMDb when TMDB can't describe the film, or without it).

At the top a filter (All / Downloading / Finished / Problems, `entry_group`; kept for the
session) and "✓ Clear finished", which takes the finished grabs off the list (never out of the
history, never their files: DownloadStatusService.clear). Each row has a ⋯ menu (`row_menu`):
Open the folder (once its video is found in "Where finished downloads end up", else greyed
with the reason as tip), Download again… (⬇ Auto / ☰ Choose…), Get subtitles (the CC menu of
app/ui/subtitle_control.py; its progress shows in the row) and Remove from list.
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

from app.models import (
    DownloadEntry,
    DownloadsOverview,
    DownloadState,
    FreeSpace,
    GrabOutcome,
    MediaType,
    Show,
    SubtitleOffer,
)
from app.i18n import N_, month_short, number, t
from app.services import DiscoveryService, DownloadStatusService, ImageCache, SettingsService
from app.services.download_status import Located
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.download_control import DownloadKey
from app.ui.missing_control import MenuEntry, MenuItem, popup_menu
from app.ui.row_actions import again_items
from app.ui.subtitle_control import LOOKING_TEXT, OFF_TIP, RunPhase, SubtitleActions
from app.ui.subtitle_settings import subtitle_mark
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
    "Takes the finished downloads off this list. Your files and AirGrabber's history stay."
)
MENU_TIP = N_("More options")
NO_MATCH_TEXT = N_("Nothing here right now.")
OPEN_FOLDER_TEXT = N_("Open the folder")
AGAIN_TEXT = N_("Download again…")
SUBTITLES_TEXT = N_("Get subtitles")
REMOVE_TEXT = N_("Remove from list")
REMOVE_TIP = N_("Only takes it off this list. Your files stay where they are.")
NO_FOLDER_TIP = N_("Choose where finished downloads end up first (Settings → Subtitles)")
NOT_FOUND_TIP = N_("Not found in the folder where finished downloads end up")
UNREACHABLE_TIP = N_("The folder where finished downloads end up can't be reached")
NO_SHOW_TIP = N_("AirGrabber doesn't know this show any more")
NO_SUBTITLES_TIP = N_(
    "Subtitles need the folder where finished downloads end up (Settings → Subtitles)"
)
IMDB_TITLE_URL = "https://www.imdb.com/title/{}/"
IMDB_FIND_URL = "https://www.imdb.com/find/?q="
_POSTER = (40, 60)
_STATE_WIDTH = 210
_DETAILS_WAIT_S = 5.0

_STATE_COLORS: dict[DownloadState, theme.Color] = {
    DownloadState.DOWNLOADING: theme.TEXT,
    DownloadState.WAITING: theme.TEXT_MUTED,
    DownloadState.STOPPED: theme.WARNING,
    DownloadState.READY: theme.SUCCESS,
    DownloadState.PROBLEM: theme.DANGER,
    DownloadState.NOT_FOUND: theme.WARNING,
    DownloadState.UNKNOWN: theme.TEXT_DIM,
}


# -- the filter ---------------------------------------------------------------------------------


class EntryFilter(StrEnum):
    """Which grabs the page lists (the bar at the top; kept for the session)."""

    ALL = "all"
    DOWNLOADING = "downloading"  # on its way: downloading, waiting or stopped
    FINISHED = "finished"  # ready
    PROBLEMS = "problems"  # a problem in the program, or not found there


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
    DownloadState.READY: EntryFilter.FINISHED,
    DownloadState.PROBLEM: EntryFilter.PROBLEMS,
    DownloadState.NOT_FOUND: EntryFilter.PROBLEMS,
}


def entry_group(state: DownloadState) -> EntryFilter | None:
    """The filter a grab's state belongs to; None for an unknown state (only under All)."""
    return _GROUPS.get(state)


def filter_entries(
    entries: Sequence[DownloadEntry], chosen: EntryFilter
) -> tuple[DownloadEntry, ...]:
    """The grabs the chosen filter lists, in their order."""
    if chosen is EntryFilter.ALL:
        return tuple(entries)
    return tuple(entry for entry in entries if entry_group(entry.state) is chosen)


def finished_ids(entries: Iterable[DownloadEntry]) -> list[int]:
    """The history ids "✓ Clear finished" takes off the list: the ready grabs."""
    return [
        entry.record.id
        for entry in entries
        if entry.state is DownloadState.READY and entry.record.id is not None
    ]


def filter_from_name(name: str) -> EntryFilter:
    for kind, text in FILTER_NAMES.items():
        if t(text) == name:
            return kind
    return EntryFilter.ALL


def entry_key(entry: DownloadEntry) -> DownloadKey:
    """The title a grab is for (its subtitles' and one-click runs' key)."""
    record = entry.record
    return (record.media_type, record.media_id, record.season, record.episode)


# -- the ⋯ menu --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageActions:
    """What the rows' ⋯ menus do besides the folder and the list (the main window provides
    them): a grab's title downloaded again (`auto`: the one-click download; else the search
    window) and its subtitles."""

    again: Callable[[DownloadEntry, bool], None]
    subtitles: SubtitleActions | None = None


def folder_tip(located: Located) -> str:
    """Why "Open the folder" is greyed out ("" when it isn't)."""
    if located.folder is not None:
        return ""
    if located.problem == "no_folder":
        return t(NO_FOLDER_TIP)
    if located.problem == "unreachable":
        return t(UNREACHABLE_TIP)
    return t(NOT_FOUND_TIP)


def row_menu(
    entry: DownloadEntry,
    located: Located,
    *,
    open_folder: Callable[[str], object],
    again: Callable[[], None] | None,
    subtitles: Callable[[], None] | None,
    subtitles_tip: str,
    remove: Callable[[], None],
) -> list[MenuEntry]:
    """A row's ⋯ menu: Open the folder, Download again…, Get subtitles, Remove from list;
    the ones that can't be done now greyed with their reason as tip."""
    folder = located.folder
    return [
        MenuEntry(
            t(OPEN_FOLDER_TEXT),
            (lambda: _call(open_folder, folder)) if folder is not None else None,
            folder_tip(located),
        ),
        MenuEntry(t(AGAIN_TEXT), again, "" if again is not None else t(NO_SHOW_TIP)),
        MenuEntry(t(SUBTITLES_TEXT), subtitles, "" if subtitles is not None else subtitles_tip),
        MenuEntry(t(REMOVE_TEXT), remove, t(REMOVE_TIP)),
    ]


def _call(action: Callable[[str], object], value: str) -> None:
    action(value)


# -- texts -------------------------------------------------------------------------------------


def percent(progress: float | None) -> int:
    """Whole percent, rounded down (99.9 % is not done yet)."""
    if progress is None:
        return 0
    return max(0, min(100, int(progress * 100)))


def state_text(state: DownloadState, progress: float | None, program: str) -> str:
    """"Downloading 42 %", "Ready", "Not found in Download Station", …"""
    if state is DownloadState.DOWNLOADING:
        return t("Downloading {percent} %", percent=percent(progress))
    if state is DownloadState.WAITING:
        return t("Waiting")
    if state is DownloadState.STOPPED:
        return t("Stopped")
    if state is DownloadState.READY:
        return t("Ready")
    if state is DownloadState.PROBLEM:
        return t("Problem in {program}", program=program) if program else t("Problem")
    if state is DownloadState.NOT_FOUND:
        return t("Not found in {program}", program=program) if program else t("Not found")
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


def subtitles_text(languages: Iterable[str], order: Sequence[str] = ()) -> str:
    """"Subtitles EN NL" (Settings' order first); "" without any."""
    mark = subtitle_mark(languages, order)
    return t("Subtitles {languages}", languages=mark.removeprefix("CC ")) if mark else ""


def meta_text(entry: DownloadEntry, now: datetime) -> str:
    """"Today 14:32 · Sent to Download Station"."""
    parts = [when_text(entry.record.downloaded_at, now)]
    if where := destination_text(entry.outcome, entry.program):
        parts.append(where)
    return " · ".join(parts)


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


def imdb_find_url(title: str) -> str:
    return IMDB_FIND_URL + quote(title, safe="")


def same_rows(entries: Sequence[DownloadEntry], ids: Sequence[int | None]) -> bool:
    """Whether the page's rows (by history id) are still these grabs, in this order."""
    return [entry.record.id for entry in entries] == list(ids)


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
        **kwargs: Any,
    ) -> None:
        """`on_open_film(tmdb_id)`: opens a film's info window; False when it couldn't (the
        row then opens the film on IMDb). `actions`: the ⋯ menus' Download again… and Get
        subtitles (greyed out without them)."""
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
        self._rows: list[_Row] = []
        self._row_ids: list[int | None] = []
        self._open_task: asyncio.Task[None] | None = None

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
        add_tooltip(self.clear_button, t(CLEAR_TIP))
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
        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.grid_columnconfigure(0, weight=1)
        self._body.grid(row=3, column=0, sticky="nsew", padx=5, pady=(0, 5))
        self._empty: MessagePanel | None = None
        self._loading: LoadingBar | None = None

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
            self._loading.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        await self._downloads.check()  # the listener renders it

    def destroy(self) -> None:
        self._unsubscribe()
        self._downloads.set_visible(False)
        if self._open_task is not None:
            self._open_task.cancel()
        if self._menu_task is not None:
            self._menu_task.cancel()
        super().destroy()

    # -- the filter and the list ---------------------------------------------------------------

    @property
    def entry_filter(self) -> EntryFilter:
        return self._filter

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
        ids = finished_ids(overview.entries) if overview is not None else []
        if ids:
            spawn(self._downloads.clear(ids), name="DownloadsView.clear")

    def _remove(self, entry: DownloadEntry) -> None:
        if entry.record.id is not None:
            spawn(self._downloads.clear([entry.record.id]), name="DownloadsView.remove")

    # -- a row's ⋯ menu ------------------------------------------------------------------------

    def open_menu(self, row: _Row) -> None:
        """Find the row's video (for "Open the folder"), then show its menu."""
        entry = row.entry
        if entry is None:
            return
        if self._menu_task is not None and not self._menu_task.done():
            self._menu_task.cancel()
        self._menu_task = spawn(self._show_menu(row, entry), name="DownloadsView.menu")

    async def _show_menu(self, row: _Row, entry: DownloadEntry) -> None:
        row.set_busy(True)
        try:
            located = await self._downloads.locate(entry)
        finally:
            if row.winfo_exists():
                row.set_busy(False)
        if not row.winfo_exists():
            return
        popup_menu(row.more, self.menu_items(row, entry, located))

    def menu_items(self, row: _Row, entry: DownloadEntry, located: Located) -> list[MenuEntry]:
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
        subtitles: Callable[[], None] | None = None
        subtitle_actions = actions.subtitles if actions is not None else None
        offer = subtitle_actions.offer() if subtitle_actions is not None else SubtitleOffer.NONE
        if subtitle_actions is not None and offer is SubtitleOffer.ON:
            key = entry_key(entry)

            def subtitles_menu() -> None:
                assert subtitle_actions is not None
                items: list[MenuItem] = subtitle_actions.menu(key)
                popup_menu(row.more, items)

            subtitles = subtitles_menu
        return row_menu(
            entry,
            located,
            open_folder=self._downloads.open_folder,
            again=again,
            subtitles=subtitles,
            subtitles_tip=t(OFF_TIP if offer is SubtitleOffer.OFF else NO_SUBTITLES_TIP),
            remove=functools.partial(self._remove, entry),
        )

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
        has_finished = bool(finished_ids(overview.entries))
        self.clear_button.configure(state="normal" if has_finished else "disabled")
        if not overview.entries:
            self._clear_rows()
            self._show_no_match(False)
            if self._empty is None:
                self._empty = MessagePanel(
                    self._body, t(EMPTY_TITLE), t(EMPTY_TEXT), height=300
                )
                self._empty.grid(row=0, column=0, sticky="nsew")
            return
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        entries = filter_entries(overview.entries, self._filter)
        self._show_no_match(not entries)
        now = overview.checked_at or datetime.now().astimezone()
        order = self._settings.current.subtitle_languages
        if same_rows(entries, self._row_ids):
            for row, entry in zip(self._rows, entries, strict=True):
                row.show_entry(entry, overview.program, now, order)
            return
        self._clear_rows()
        subtitles = self._actions.subtitles if self._actions is not None else None
        for index, entry in enumerate(entries):
            row = _Row(
                self._body,
                images=self._images,
                on_click=self._open,
                on_menu=self.open_menu,
                subtitles=subtitles,
            )
            row.grid(row=index, column=0, sticky="ew", padx=10, pady=4)
            row.show_entry(entry, overview.program, now, order)
            self._rows.append(row)
        self._row_ids = [entry.record.id for entry in entries]

    def _show_no_match(self, show: bool) -> None:
        if show and self._no_match is None:
            self._no_match = ctk.CTkLabel(
                self._body,
                text=t(NO_MATCH_TEXT),
                font=theme.font(14),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            )
            self._no_match.grid(row=0, column=0, sticky="w", padx=15, pady=15)
        elif not show and self._no_match is not None:
            self._no_match.destroy()
            self._no_match = None

    def _clear_rows(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows = []
        self._row_ids = []

    def _show_note(self, overview: DownloadsOverview) -> None:
        if overview.problem:
            text, color = f"⚠ {overview.problem}", theme.WARNING
        elif not overview.progress_available and overview.entries:
            text, color = t(PROGRESS_HINT), theme.TEXT_MUTED
        else:
            text, color = "", theme.TEXT_MUTED
        self._note.configure(text=text, text_color=color)
        if text:
            self._note.grid(row=1, column=0, sticky="ew", padx=15, pady=(0, 6))
        else:
            self._note.grid_remove()

    def _show_banner(self, space: FreeSpace | None) -> None:
        if space is not None and space.low:
            self._banner_label.configure(text=f"⚠ {space_text(space)}")
            self._banner.grid(row=2, column=0, sticky="ew", padx=15, pady=(0, 8))
        else:
            self._banner.grid_remove()

    def _open(self, entry: DownloadEntry) -> None:
        if entry.record.media_type is MediaType.TV:
            if entry.show is not None:
                self._on_open_show(entry.show)
            return
        if self._open_task is not None and not self._open_task.done():
            return
        self._open_task = spawn(self._open_film(entry), name="DownloadsView.open_film")

    async def _open_film(self, entry: DownloadEntry) -> None:
        """The film's info window; else the film on IMDb: its page when TMDB knows its IMDb
        id, else IMDb's search."""
        if self._on_open_film is not None and await self._on_open_film(entry.record.media_id):
            return
        url = imdb_find_url(entry.title)
        try:
            async with asyncio.timeout(_DETAILS_WAIT_S):
                details = await self._discovery.movie_details(entry.record.media_id)
            if details.imdb_id:
                url = IMDB_TITLE_URL.format(details.imdb_id)
        except (TMDBError, TimeoutError):
            log.info("No IMDb id for TMDB film %d; opening IMDb's search", entry.record.media_id)
        webbrowser.open(url)


class _Row(ctk.CTkFrame):
    """One grab: poster, name, when · where (+ subtitles, or a subtitle request's progress),
    state and a thin progress bar, and its ⋯ menu (`on_menu`)."""

    def __init__(
        self,
        master: Any,
        *,
        images: ImageCache,
        on_click: Callable[[DownloadEntry], None],
        on_menu: Callable[[_Row], None] | None = None,
        subtitles: SubtitleActions | None = None,
    ) -> None:
        super().__init__(master, fg_color=theme.SURFACE, corner_radius=8, cursor="hand2")
        self._images = images
        self._on_click = on_click
        self._subtitles_actions = subtitles
        self._entry: DownloadEntry | None = None
        self._order: Sequence[str] = ()
        self._poster_url: str | None = None
        self.key: DownloadKey | None = None  # the title, for its subtitle requests
        self.grid_columnconfigure(1, weight=1)

        self._poster = PosterImage(self, width=_POSTER[0], height=_POSTER[1])
        self._poster.grid(row=0, column=0, rowspan=2, padx=(8, 12), pady=8)
        self._poster.set_on_click(self._clicked)
        self._title = ctk.CTkLabel(
            self, text="", font=theme.font(14, bold=True), text_color=theme.TEXT, anchor="w"
        )
        self._title.grid(row=0, column=1, sticky="sw", pady=(10, 0))
        line = ctk.CTkFrame(self, fg_color="transparent")
        line.grid(row=1, column=1, sticky="nw", pady=(0, 10))
        self._meta = ctk.CTkLabel(
            line, text="", font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w"
        )
        self._meta.pack(side="left")
        self._subtitles = ctk.CTkLabel(
            line, text="", font=theme.font(12), text_color=theme.SUCCESS, anchor="w"
        )
        state_box = ctk.CTkFrame(self, fg_color="transparent", width=_STATE_WIDTH)
        state_box.grid(row=0, column=2, rowspan=2, sticky="e", padx=(12, 16))
        self._state = ctk.CTkLabel(state_box, text="", font=theme.font(13), anchor="e")
        self._state.pack(anchor="e")
        self._bar = ctk.CTkProgressBar(
            state_box,
            width=_STATE_WIDTH - 30,
            height=4,
            progress_color=theme.ACCENT,
            fg_color=theme.PLACEHOLDER,
        )
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
        for widget in (self, self._title, line, self._meta, self._subtitles, state_box,
                       self._state):
            widget.bind("<Button-1>", lambda _event: self._clicked(), add="+")
        if subtitles is not None:
            subtitles.runs.attach(self)

    @property
    def entry(self) -> DownloadEntry | None:
        return self._entry

    def set_busy(self, busy: bool) -> None:
        """The ⋯ while its menu is being put together (the video looked for)."""
        self.more.configure(text="…" if busy else "⋯", state="disabled" if busy else "normal")

    def _clicked(self) -> None:
        if self._entry is not None:
            self._on_click(self._entry)

    def repaint(self) -> None:
        """A subtitle request for this title changed: its line again."""
        if self._entry is not None:
            self._show_subtitles(self._entry)

    def _show_subtitles(self, entry: DownloadEntry) -> None:
        run = None
        if self._subtitles_actions is not None and self.key is not None:
            run = self._subtitles_actions.runs.get(self.key)
        if run is not None:
            text = t(LOOKING_TEXT) if run.phase is RunPhase.WORKING else run.text
            color = theme.WARNING if run.warn else theme.TEXT_MUTED
        else:
            text, color = subtitles_text(entry.subtitles, self._order), theme.SUCCESS
        self._subtitles.configure(text=f"  ·  {text}" if text else "", text_color=color)
        if text:
            self._subtitles.pack(side="left")
        else:
            self._subtitles.pack_forget()

    def destroy(self) -> None:
        if self._subtitles_actions is not None:
            self._subtitles_actions.runs.detach(self)
        super().destroy()

    def show_entry(
        self, entry: DownloadEntry, program: str, now: datetime, order: Sequence[str]
    ) -> None:
        self._entry = entry
        self._order = order
        self.key = entry_key(entry)
        self._title.configure(text=entry.title)
        self._meta.configure(text=meta_text(entry, now))
        self._show_subtitles(entry)
        asked = program or entry.program
        self._state.configure(
            text=state_text(entry.state, entry.progress, asked),
            text_color=state_color(entry.state),
        )
        if entry.state is DownloadState.DOWNLOADING:
            self._bar.set(entry.progress or 0.0)
            self._bar.pack(anchor="e", pady=(4, 0))
        else:
            self._bar.pack_forget()
        url = _poster_url(entry)
        if url != self._poster_url:
            self._poster_url = url
            self._poster.show(self._images, url)


def _poster_url(entry: DownloadEntry) -> str | None:
    """An episode's show poster (TVMaze's medium one, cached); films have none to hand."""
    show = entry.show
    return (show.poster_url or show.poster_url_original) if show is not None else None
