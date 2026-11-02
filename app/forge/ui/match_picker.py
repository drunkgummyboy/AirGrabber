"""Pick the right TMDB entry when Forge isn't sure (ported from MediaForge's ui/match_picker.py):
up to MAX_CANDIDATES candidates with thumbnails and overviews; Use this / Skip TMDB / Use top
result.

Built on AirGrabber's dialog kit: no title bar (app/ui/borderless.py; the title as a heading),
placed over the window that asks, Escape and ✕ close it (= "cancel"), the Forge look's copper.
Never `transient` (MediaForge's lesson, see app/forge/ui/widgets/dialogs.py). Thumbnails come
from AirGrabber's image cache at physical size.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk

from app.forge.clients.tmdb import TmdbError
from app.forge.core.matcher import MAX_CANDIDATES
from app.forge.core.queue import QueueItem
from app.forge.models.media import MediaKind
from app.forge.ui.images import tmdb_image_url
from app.forge.ui.look import forge_look
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
_ROOM_AROUND_LIST = 200  # heading, file name, hint, buttons and padding
_TEXT_WRAP = 320
_THUMB_W, _THUMB_H = 50, 75
THUMB_SIZE = "w154"  # TMDB's size: 50 units are 75 pixels at 150 %
OVERVIEW_MAX = 160
MONO_FAMILY = "Consolas"  # old file names (D10)


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
        self._look: theme.Look = forge_look() if look is None else look
        p = self._look
        super().__init__(master, fg_color=p.BG)
        self.services = services
        self.item = item
        self.result: asyncio.Future[PickResult] = asyncio.get_running_loop().create_future()
        self.candidates: list[TMDBTitle] = []
        self._images: list[ctk.CTkImage] = []
        self._tasks: list[asyncio.Task[None]] = []
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
        ).pack(anchor="w", pady=(2, 10))
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

    async def load(self) -> None:
        """Fill the list: the lookup's candidates, or a fresh TMDB search when it had none."""
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
        if self.result.done() or not self.winfo_exists():
            return
        self.candidates = candidates[:MAX_CANDIDATES]
        if not self.candidates:
            ctk.CTkLabel(
                self.list,
                text=t("TMDB found nothing for this file."),
                font=theme.font(12),
                text_color=theme.TEXT_MUTED,
            ).pack(pady=20)
            self.top_button.configure(state="disabled")
        for result in self.candidates:
            self._add_row(result)

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
            self._tasks.append(
                spawn(self._thumb(thumb, result.poster_path), name="MatchPicker.thumb")
            )

    async def _thumb(self, label: ctk.CTkLabel, path: str) -> None:
        scale = ctk.ScalingTracker.get_widget_scaling(label)
        size = (max(1, round(_THUMB_W * scale)), max(1, round(_THUMB_H * scale)))
        picture = await self.services.images.get(tmdb_image_url(path, THUMB_SIZE), size)
        if picture is None or not label.winfo_exists():
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
