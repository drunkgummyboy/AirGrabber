"""The Downloads page: the grabs of the last 30 days, newest first, with where each went, what
the torrent program says about it (Downloading 42 % / Ready / Waiting / Stopped / Not found in
<program>) and its saved subtitles; a hint when no program can be asked, one plain line when it
can't be reached, and an amber banner when the downloads' disk is almost full.

The data comes from DownloadStatusService, which checks every 10 s while this page is on screen
(`<Map>` / `<Unmap>`) and tells the page about every new overview; rows are updated in place
while the list of grabs stays the same. A row opens the show's window (an episode) or the film's
info window (`on_open_film`; on IMDb when TMDB can't describe the film, or without it).
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
import webbrowser
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime
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
)
from app.i18n import N_, month_short, number, t
from app.services import DiscoveryService, DownloadStatusService, ImageCache, SettingsService
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.subtitle_settings import subtitle_mark
from app.ui.tasks import spawn
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
        **kwargs: Any,
    ) -> None:
        """`on_open_film(tmdb_id)`: opens a film's info window; False when it couldn't (the
        row then opens the film on IMDb)."""
        super().__init__(master, **kwargs)
        self._on_open_film = on_open_film
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
        super().destroy()

    def _on_overview(self, overview: DownloadsOverview) -> None:
        try:
            if self.winfo_exists():
                self._render(overview)
        except tk.TclError:
            pass  # the window is closing

    def _render(self, overview: DownloadsOverview) -> None:
        if self._loading is not None:
            self._loading.destroy()
            self._loading = None
        self._show_note(overview)
        self._show_banner(overview.space)
        if not overview.entries:
            self._clear_rows()
            if self._empty is None:
                self._empty = MessagePanel(
                    self._body, t(EMPTY_TITLE), t(EMPTY_TEXT), height=300
                )
                self._empty.grid(row=0, column=0, sticky="nsew")
            return
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        now = overview.checked_at or datetime.now().astimezone()
        order = self._settings.current.subtitle_languages
        if same_rows(overview.entries, self._row_ids):
            for row, entry in zip(self._rows, overview.entries, strict=True):
                row.show_entry(entry, overview.program, now, order)
            return
        self._clear_rows()
        for index, entry in enumerate(overview.entries):
            row = _Row(self._body, images=self._images, on_click=self._open)
            row.grid(row=index, column=0, sticky="ew", padx=10, pady=4)
            row.show_entry(entry, overview.program, now, order)
            self._rows.append(row)
        self._row_ids = [entry.record.id for entry in overview.entries]

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
    """One grab: poster, name, when · where (+ subtitles), state and a thin progress bar."""

    def __init__(
        self,
        master: Any,
        *,
        images: ImageCache,
        on_click: Callable[[DownloadEntry], None],
    ) -> None:
        super().__init__(master, fg_color=theme.SURFACE, corner_radius=8, cursor="hand2")
        self._images = images
        self._on_click = on_click
        self._entry: DownloadEntry | None = None
        self._poster_url: str | None = None
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
        for widget in (self, self._title, line, self._meta, self._subtitles, state_box,
                       self._state):
            widget.bind("<Button-1>", lambda _event: self._clicked(), add="+")

    def _clicked(self) -> None:
        if self._entry is not None:
            self._on_click(self._entry)

    def show_entry(
        self, entry: DownloadEntry, program: str, now: datetime, order: Sequence[str]
    ) -> None:
        self._entry = entry
        self._title.configure(text=entry.title)
        self._meta.configure(text=meta_text(entry, now))
        subtitles = subtitles_text(entry.subtitles, order)
        self._subtitles.configure(text=f"  ·  {subtitles}" if subtitles else "")
        if subtitles:
            self._subtitles.pack(side="left")
        else:
            self._subtitles.pack_forget()
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
