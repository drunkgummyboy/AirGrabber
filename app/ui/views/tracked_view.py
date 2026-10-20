"""The user's tracked TV library: a responsive grid of poster cards (`ShowCard`) with import
and cleanup."""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from typing import Any

import customtkinter as ctk

from app.models import ImportResult, Show
from app.services import ImageCache, LibraryService
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.airdates_import import PreviewFinder
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.cards import ShowCard, card_info_line
from app.ui.import_dialog import ImportShowsDialog
from app.ui.missing_control import MissingActions
from app.ui.poster_card import POSTER_CARD_WIDTH
from app.ui.tasks import spawn
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, LoadingBar, MessagePanel, show_info

log = logging.getLogger(__name__)

_MAX_NAMES = 10
_EMPTY_HEIGHT = 300


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_MAX_NAMES])
    extra = len(names) - _MAX_NAMES
    return f"{shown} … and {extra} more" if extra > 0 else shown


def import_summary(result: ImportResult) -> str:
    """A short human-readable report of an import, one line per non-empty group."""
    lines: list[str] = []
    if result.added:
        lines.append(f"Added {_plural(len(result.added), 'show')}.")
    if result.already_tracked:
        lines.append(
            f"Already tracked: {len(result.already_tracked)} ({_names(result.already_tracked)})"
        )
    if result.not_found:
        lines.append(f"Not found: {_names(result.not_found)}")
    if result.failed:
        lines.append(f"Failed (network): {_names(result.failed)}")
    return "\n".join(lines) if lines else "Nothing to import."


class TrackedView(BaseView):
    def __init__(
        self,
        master: Any,
        *,
        library: LibraryService,
        airdates: PreviewFinder,
        images: ImageCache,
        status: StatusReporter,
        on_open_show: Callable[[Show], None] | None = None,
        missing_actions: MissingActions | None = None,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        **kwargs: Any,
    ) -> None:
        """`on_open_show`: what a click on a card's poster or title does (the show details).
        `missing_actions`: the cards show their missing episodes and "Download missing".
        `card_scale`: the card size (Settings), read at every load."""
        super().__init__(master, **kwargs)
        self._card_scale = card_scale
        self._library = library
        self._airdates = airdates
        self._images = images
        self._status = status
        self._on_open_show = on_open_show
        self._missing_actions = missing_actions
        self._tasks: set[asyncio.Task[None]] = set()
        self._import_dialog: ImportShowsDialog | None = None
        self._cards: dict[int, ShowCard] = {}
        # Switch changes in flight: the library change they cause needs no reload of this page.
        self._own_changes = 0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=5)
        self._title = ctk.CTkLabel(
            header, text="Tracked TV Shows", font=theme.font(18, bold=True), text_color=theme.TEXT
        )
        self._title.pack(side="left", padx=(0, 10))
        self._import_button = ctk.CTkButton(
            header,
            text="Import Shows",
            width=120,
            height=30,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._open_import,
        )
        self._import_button.pack(side="left", padx=5)
        self._cleanup_button = ctk.CTkButton(
            header,
            text="Cleanup Ended",
            width=120,
            height=30,
            fg_color=theme.DANGER_BUTTON,
            hover_color=theme.DANGER_BUTTON_HOVER,
            command=lambda: self._start(self._cleanup(), "TrackedView.cleanup"),
        )
        self._cleanup_button.pack(side="left")

        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 5))

    # -- loading -----------------------------------------------------------------------------

    def _clear_body(self) -> None:
        self._cards.clear()
        for child in self._body.winfo_children():
            child.destroy()

    def refresh(self) -> None:
        if self._own_changes:  # this page's own switch: it already shows the change
            self._stale = False
            return
        super().refresh()

    async def load(self) -> None:
        self._clear_body()
        LoadingBar(self._body).pack(fill="x")
        entries = await self._library.tracked()
        activity = await self._library.activity()
        missing = (
            await self._library.missing_counts() if self._missing_actions is not None else {}
        )
        now = datetime.now().astimezone()
        self._clear_body()
        self._title.configure(text=f"Tracked TV Shows ({len(entries)})")
        if not entries:
            MessagePanel(
                self._body,
                "No tracked shows yet",
                "Search for a show at the top right, or use Import Shows to paste a list.",
                height=_EMPTY_HEIGHT,
            ).pack(fill="x", expand=True)
            return
        scale = self._card_scale()
        grid = FlexGrid(
            self._body, item_width=grid_item_width(POSTER_CARD_WIDTH, scale), pad_x=8, pad_y=10
        )
        grid.pack(fill="x", expand=True)
        for entry in entries:
            self._cards[entry.show.id] = ShowCard(
                grid,
                entry.show,
                images=self._images,
                on_remove=self._remove,
                auto_download=entry.auto_download,
                info_line=card_info_line(activity.get(entry.show.id), now),
                on_auto_download=self._toggle_auto,
                on_open=self._on_open_show,
                missing=missing.get(entry.show.id, 0),
                missing_actions=self._missing_actions,
                scale=scale,
            )
        grid.relayout()

    # -- actions -----------------------------------------------------------------------------

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _report(self, text: str, *, clear_after_ms: int | None = None) -> None:
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass  # window already destroyed

    def _remove(self, show: Show) -> None:
        self._start(self._untrack(show), "TrackedView.remove")

    def _toggle_auto(self, show: Show, on: bool) -> None:
        self._own_changes += 1
        self._start(self._set_auto(show, on), "TrackedView.auto-download")

    async def _set_auto(self, show: Show, on: bool) -> None:
        try:
            await self._library.set_auto_download(show.id, on)
        except Exception:
            log.exception("Switching automatic downloads for TVMaze show %d failed", show.id)
            card = self._cards.get(show.id)
            if card is not None and card.winfo_exists():
                card.set_auto_download(not on)
            state = "on" if on else "off"
            self._report(
                f"Could not switch automatic downloads {state} for {show.name}",
                clear_after_ms=8000,
            )
        finally:
            self._own_changes -= 1

    async def _untrack(self, show: Show) -> None:
        await self._library.untrack(show.id)
        self._report(f"Removed {show.name}", clear_after_ms=4000)

    async def _cleanup(self) -> None:
        self._cleanup_button.configure(state="disabled")
        try:
            removed = await self._library.remove_ended()
            if removed:
                self._report(f"Removed {removed} ended show(s)", clear_after_ms=5000)
            else:
                self._report("No ended shows to remove", clear_after_ms=5000)
        finally:
            if self._cleanup_button.winfo_exists():
                self._cleanup_button.configure(state="normal")

    def _open_import(self) -> None:
        dialog = self._import_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus_force()
            return
        if (window := self._window()) is not None:
            self._import_dialog = ImportShowsDialog(
                window, on_import=self._on_import, airdates=self._airdates, library=self._library
            )

    def _window(self) -> ctk.CTk | ctk.CTkToplevel | None:
        toplevel = self.winfo_toplevel()
        return toplevel if isinstance(toplevel, (ctk.CTk, ctk.CTkToplevel)) else None

    def _on_import(self, lines: list[str]) -> None:
        self._start(self._import(lines), "TrackedView.import")

    async def _import(self, lines: list[str]) -> None:
        self._import_button.configure(state="disabled")
        try:
            result = await self._library.import_shows(
                lines,
                progress=lambda done, total: self._report(f"Importing shows… {done}/{total}"),
            )
        except TVMazeError as exc:
            self._report(f"Import failed: {exc}", clear_after_ms=8000)
            return
        finally:
            if self._import_button.winfo_exists():
                self._import_button.configure(state="normal")
        self._report("Import finished", clear_after_ms=3000)
        if self.winfo_exists() and (window := self._window()) is not None:
            show_info(window, "Import complete", import_summary(result))

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        super().destroy()
