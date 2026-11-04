"""Pick the right TMDB entry when Forge isn't sure (ported from MediaForge's ui/match_picker.py):
up to MAX_CANDIDATES candidates with thumbnails and overviews; Use this / Skip TMDB / Use top
result. A search row (prefilled with the parsed title, no year; "Name 2023" searches that year)
replaces the candidates with TMDB's results when none of them is right.

Built on AirGrabber's dialog kit: no title bar (app/ui/borderless.py; the title as a heading),
placed over the window that asks, Escape and ✕ close it (= "cancel"), the look's accent.
Never `transient` (MediaForge's lesson, see app/forge/ui/widgets/dialogs.py). Thumbnails come
from AirGrabber's image cache at physical size.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tkinter as tk
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk

from app.forge.clients.tmdb import TmdbError
from app.forge.core.matcher import MAX_CANDIDATES
from app.forge.core.queue import QueueItem
from app.forge.models.media import MediaKind
from app.forge.ui.images import tmdb_image_url
from app.forge.ui.look import app_look
from app.i18n import t
from app.models.tmdb import TMDBTitle
from app.ui import theme
from app.ui.borderless import clearance, make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    activate_later,
    heading_row,
    max_dialog_height,
    place_over,
    secondary_button,
)
from app.ui.scrollable import ScrollableFrame
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.forge.services import ForgeServices

log = logging.getLogger(__name__)

type PickResult = TMDBTitle | None | Literal["cancel"]
"""A candidate (Use this / Use top result), None (Skip TMDB for this file) or "cancel"."""

_PAD_X = 25  # as Forge's ask_choice
_PAD_Y = 20
_WIDTH = 520  # the content's width, CTk units
_LIST_HEIGHT = 420
_SEARCH_ROW = 44  # the search entry (34) and its padding below (10)
_ROOM_AROUND_LIST = 200 + _SEARCH_ROW  # heading, file name, hint, search, buttons and padding
_TEXT_WRAP = 320
_THUMB_W, _THUMB_H = 50, 75
THUMB_SIZE = "w154"  # TMDB's size: 50 units are 75 pixels at 150 %
OVERVIEW_MAX = 160
MONO_FAMILY = "Consolas"  # old file names (D10)


_YEAR_AT_END = re.compile(
    r"^(?P<rest>.*?\S)\s+(?:\((?P<paren>(?:19|20)\d{2})\)|(?P<bare>(?:19|20)\d{2}))$"
)


def split_year(query: str) -> tuple[str, int | None]:
    """`query` without a trailing year of its own ("The Ark 2023", "The Ark (2023)"), and that
    year (1900–2099); else the stripped query and None."""
    query = query.strip()
    found = _YEAR_AT_END.match(query)
    if found is None:
        return query, None
    return found["rest"], int(found["paren"] or found["bare"])


def short_overview(text: str) -> str:
    """The overview cut to OVERVIEW_MAX characters, with "…" where it was cut."""
    text = text.strip()
    return text if len(text) <= OVERVIEW_MAX else text[: OVERVIEW_MAX - 3] + "…"


class MatchPicker(ctk.CTkToplevel):
    """Use `await MatchPicker.ask(master, item, services)`; the window may also be built and
    destroyed straight away (never shown)."""

    def __init__(
        self,
        master: Any,
        item: QueueItem,
        services: ForgeServices,
        *,
        look: theme.Look | None = None,
    ) -> None:
        self._look: theme.Look = app_look() if look is None else look
        p = self._look
        super().__init__(master, fg_color=p.BG)
        self.services = services
        self.item = item
        self.result: asyncio.Future[PickResult] = asyncio.get_running_loop().create_future()
        self.candidates: list[TMDBTitle] = []
        self._images: list[ctk.CTkImage] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._row_tasks: list[asyncio.Task[None]] = []  # the shown rows' thumbnails
        self._generation = 0  # a newer search (or the first fill) wins over an older one
        self.search_entry: ctk.CTkEntry | None = None
        self.search_button: ctk.CTkButton | None = None
        episode = item.parsed.kind is MediaKind.EPISODE

        self.title(t("Choose the right match"))
        set_window_icon(self)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("cancel"))
        self.bind("<Destroy>", self._gone, add="+")
        self.bind("<Escape>", lambda _event: self._finish("cancel"))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=_PAD_X, pady=_PAD_Y)
        heading = heading_row(
            body,
            t("Which show is this?") if episode else t("Which movie is this?"),
            right_room=clearance(_PAD_X),
        )
        heading.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            body,
            text=item.path.name,
            font=ctk.CTkFont(family=MONO_FAMILY, size=11),
            text_color=theme.TEXT_MUTED,
            wraplength=_WIDTH,
            justify="left",
            anchor="w",
        ).pack(anchor="w", fill="x")
        ctk.CTkLabel(
            body,
            text=t("Your choice is remembered for this title."),
            font=theme.font(11),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", pady=(2, 8))
        if services.tmdb.configured():
            self._build_search_row(body)
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        room = max_dialog_height(master, scaling) - _ROOM_AROUND_LIST
        self.list = ScrollableFrame(
            body,
            width=_WIDTH,
            height=max(150, min(_LIST_HEIGHT, room)),
            fg_color=p.SURFACE,
            corner_radius=8,
            scrollbar_button_color=p.CHIP,
            scrollbar_button_hover_color=p.EDGE,
        )
        self.list.pack(fill="both", expand=True)
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(fill="x", pady=(14, 0))
        self.top_button = secondary_button(
            footer, t("Use top result"), self._use_top, width=130, look=p
        )
        self.top_button.pack(side="right")
        self.skip_button = secondary_button(
            footer, t("Skip TMDB for this file"), lambda: self._finish(None), width=170, look=p
        )
        self.skip_button.pack(side="right", padx=(0, 8))

        make_borderless(
            self,
            drag_widgets=(self, body, heading),
            on_close=lambda: self._finish("cancel"),
            look=p,
        )
        place_over(self, master)
        activate_later(self)

    def _build_search_row(self, body: ctk.CTkFrame) -> None:
        p = self._look
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(0, 10))
        self.search_button = secondary_button(
            row, t("🔍 Search"), self._search_clicked, width=100, look=p
        )
        self.search_button.pack(side="right")
        self.search_entry = ctk.CTkEntry(
            row,
            height=34,
            fg_color=p.SURFACE,
            border_color=p.EDGE,
            text_color=theme.TEXT,
            placeholder_text=t("Search TMDB by name…"),
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        if self.item.parsed.title:
            self.search_entry.insert(0, self.item.parsed.title)
        self.search_entry.bind("<Return>", lambda _event: self._search_clicked())
        self.after(250, self._focus_search)  # after activate_later took the window's focus

    def _focus_search(self) -> None:
        entry = self.search_entry
        if entry is not None and entry.winfo_exists() and not self.result.done():
            entry.focus_set()

    def _search_clicked(self) -> None:
        if self.search_entry is None or self.result.done():
            return
        query, year = split_year(self.search_entry.get())
        if not query:
            return
        self._generation += 1
        self._tasks.append(
            spawn(self._search(query, year, self._generation), name="MatchPicker.search")
        )

    async def _search(self, query: str, year: int | None, generation: int) -> None:
        self._set_searching(True)
        self.candidates = []
        self._clear_rows()
        self._show_message(t("Searching…"))
        self.top_button.configure(state="disabled")
        tmdb = self.services.tmdb
        failed = False
        results: list[TMDBTitle] = []
        try:
            if self.item.parsed.kind is MediaKind.EPISODE:
                results = await tmdb.search_tv(query, year)
            else:
                results = await tmdb.search_movie(query, year)
        except TmdbError as exc:
            log.warning("TMDB search in the match picker failed (%s)", type(exc).__name__)
            failed = True
        if generation != self._generation or self.result.done() or not self.winfo_exists():
            return
        self._set_searching(False)
        if failed:
            self._fill([], t("TMDB couldn't be reached. Try again."))
        else:
            self._fill(results, t("Nothing found for \"{query}\".", query=query))

    def _set_searching(self, searching: bool) -> None:
        if self.search_button is None:
            return
        self.search_button.configure(
            text=t("Searching…") if searching else t("🔍 Search"),
            state="disabled" if searching else "normal",
        )

    def _clear_rows(self) -> None:
        for task in self._row_tasks:
            task.cancel()
        self._row_tasks = []
        for child in self.list.winfo_children():
            child.destroy()
        self._images = []
        self.list._parent_canvas.yview_moveto(0)  # CTk has no scroll API

    def _show_message(self, text: str) -> None:
        ctk.CTkLabel(
            self.list,
            text=text,
            font=theme.font(12),
            text_color=theme.TEXT_MUTED,
            wraplength=_TEXT_WRAP,
        ).pack(pady=20)

    def _fill(self, results: list[TMDBTitle], nothing: str) -> None:
        """Replace the rows with `results` (at most MAX_CANDIDATES), or `nothing` when empty."""
        self._clear_rows()
        self.candidates = results[:MAX_CANDIDATES]
        if not self.candidates:
            self._show_message(nothing)
            self.top_button.configure(state="disabled")
            return
        self.top_button.configure(state="normal")
        for result in self.candidates:
            self._add_row(result)

    async def load(self) -> None:
        """Fill the list: the lookup's candidates, or a fresh TMDB search when it had none. A
        search started meanwhile wins."""
        generation = self._generation
        candidates = list(self.item.resolution.candidates) if self.item.resolution else []
        tmdb = self.services.tmdb
        if not candidates and tmdb.configured():
            parsed = self.item.parsed
            try:
                if parsed.kind is MediaKind.EPISODE:
                    candidates = await tmdb.search_tv(parsed.title, parsed.year)
                else:
                    candidates = await tmdb.search_movie(parsed.title, parsed.year)
            except TmdbError as exc:
                log.warning("TMDB search for the match picker failed (%s)", type(exc).__name__)
        if self.result.done() or not self.winfo_exists() or generation != self._generation:
            return
        self._fill(candidates, t("TMDB found nothing for this file."))

    def _add_row(self, result: TMDBTitle) -> None:
        p = self._look
        row = ctk.CTkFrame(self.list, fg_color=p.BG, corner_radius=6)
        row.pack(fill="x", padx=4, pady=4)
        row.grid_columnconfigure(1, weight=1)
        thumb = ctk.CTkLabel(
            row,
            text="🎬",
            width=_THUMB_W,
            height=_THUMB_H,
            fg_color=p.CHIP,
            corner_radius=4,
            text_color=theme.TEXT_MUTED,
        )
        thumb.grid(row=0, column=0, rowspan=2, padx=8, pady=8)
        year = result.year or "—"
        ctk.CTkLabel(
            row,
            text=f"{result.title} ({year})",
            font=theme.font(13, bold=True),
            text_color=theme.TEXT,
            anchor="w",
            justify="left",
            wraplength=_TEXT_WRAP,
        ).grid(row=0, column=1, sticky="w", pady=(8, 0))
        ctk.CTkLabel(
            row,
            text=short_overview(result.overview) or t("No description."),
            font=theme.font(10),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=_TEXT_WRAP,
        ).grid(row=1, column=1, sticky="w", pady=(0, 8))
        ctk.CTkButton(
            row,
            text=t("Use this"),
            width=84,
            fg_color=p.ACCENT,
            hover_color=p.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
            font=theme.font(12),
            command=lambda r=result: self._finish(r),
        ).grid(row=0, column=2, rowspan=2, padx=8)
        if result.poster_path:
            task = spawn(self._thumb(thumb, result.poster_path), name="MatchPicker.thumb")
            self._tasks.append(task)
            self._row_tasks.append(task)

    async def _thumb(self, label: ctk.CTkLabel, path: str) -> None:
        scale = ctk.ScalingTracker.get_widget_scaling(label)
        size = (max(1, round(_THUMB_W * scale)), max(1, round(_THUMB_H * scale)))
        picture = await self.services.images.get(tmdb_image_url(path, THUMB_SIZE), size)
        if picture is None or self.result.done() or not label.winfo_exists():
            return
        image = ctk.CTkImage(light_image=picture, dark_image=picture, size=(_THUMB_W, _THUMB_H))
        self._images.append(image)
        label.configure(image=image, text="")

    def _use_top(self) -> None:
        if self.candidates:
            self._finish(self.candidates[0])

    def _gone(self, event: tk.Event[Any]) -> None:
        if event.widget is self:
            self._settle("cancel")

    def _settle(self, value: PickResult) -> None:
        if not self.result.done():
            self.result.set_result(value)
        for task in self._tasks:
            task.cancel()

    def _finish(self, value: PickResult) -> None:
        self._settle(value)
        try:
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass  # gone with its interpreter

    @classmethod
    async def ask(
        cls,
        master: Any,
        item: QueueItem,
        services: ForgeServices,
        *,
        look: theme.Look | None = None,
    ) -> PickResult:
        """Show the picker over `master` and wait for the answer; cancelling the wait closes
        it."""
        picker = cls(master, item, services, look=look)
        try:
            await picker.load()
            return await picker.result
        finally:
            picker._finish("cancel")
