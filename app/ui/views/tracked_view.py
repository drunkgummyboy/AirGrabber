"""The user's tracked TV library: a responsive poster grid with import and cleanup."""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Coroutine, Sequence
from typing import Any

import customtkinter as ctk

from app.models import ImportResult, Show
from app.services import ImageCache, LibraryService
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.cards import CARD_WIDTH, ShowCard
from app.ui.import_dialog import ImportShowsDialog
from app.ui.tasks import spawn
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, LoadingBar, MessagePanel, show_info

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
        images: ImageCache,
        status: StatusReporter,
        **kwargs: Any,
    ) -> None:
        super().__init__(master, **kwargs)
        self._library = library
        self._images = images
        self._status = status
        self._tasks: set[asyncio.Task[None]] = set()
        self._import_dialog: ImportShowsDialog | None = None

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
        for child in self._body.winfo_children():
            child.destroy()

    async def load(self) -> None:
        self._clear_body()
        LoadingBar(self._body).pack(fill="x")
        entries = await self._library.tracked()
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
        grid = FlexGrid(self._body, item_width=CARD_WIDTH, pad_x=8, pad_y=10)
        grid.pack(fill="x", expand=True)
        for entry in entries:
            ShowCard(
                grid, entry.show, images=self._images, mode="library", on_remove=self._remove
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
            self._import_dialog = ImportShowsDialog(window, on_import=self._on_import)

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
