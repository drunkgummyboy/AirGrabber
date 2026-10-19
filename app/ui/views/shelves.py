"""Shared base of the TMDB dashboard pages (Discover, Movies): titled shelves of cards."""

from __future__ import annotations

import asyncio
import tkinter as tk
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import customtkinter as ctk

from app.services import ImageCache
from app.services.tmdb import TMDBAuthError, TMDBError, TMDBNotConfigured
from app.ui import theme
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.poster_card import POSTER_CARD_WIDTH
from app.ui.tasks import spawn
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, LoadingBar, MessagePanel, destroy_tree

T = TypeVar("T")

_MESSAGE_HEIGHT = 300  # message panels sit in a scroll frame, which gives them no height


@dataclass(frozen=True, slots=True)
class Shelf(Generic[T]):
    """One section of a dashboard page."""

    icon: str
    title: str
    color: str  # of the section title
    items: Sequence[T]
    failed: bool = False  # TMDB couldn't load this shelf (the others did)


class ShelvesView(BaseView, Generic[T]):
    """A scrolling column of shelves from one TMDB dashboard.

    Subclasses fetch the dashboard (`_fetch_shelves`) and build one card per item (`_card`,
    at `self._scale`: the card size `card_scale` gave when the load started).
    `reload()` refetches from TMDB instead of using the dashboard cache.
    """

    _body_pady: tuple[int, int] = (5, 5)

    def __init__(
        self,
        master: Any,
        *,
        images: ImageCache,
        status: StatusReporter,
        on_open_settings: Callable[[], None],
        card_scale: Callable[[], CardScale] = lambda: FULL,
        **kwargs: Any,
    ) -> None:
        """`card_scale`: the card size (Settings), read at every load."""
        super().__init__(master, **kwargs)
        self._images = images
        self._status = status
        self._on_open_settings = on_open_settings
        self._card_scale = card_scale
        self._scale = card_scale()
        self._force = False  # the next load bypasses the dashboard cache
        self._tasks: set[asyncio.Task[None]] = set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.grid(row=1, column=0, sticky="nsew", padx=15, pady=self._body_pady)

    @abstractmethod
    async def _fetch_shelves(self, *, force: bool) -> list[Shelf[T]]:
        """The dashboard's shelves in display order. TMDB errors propagate."""

    @abstractmethod
    def _card(self, master: Any, item: T) -> None:
        """Create the card for `item` as a child of `master` (a FlexGrid lays it out)."""

    def reload(self) -> None:
        """Reload with fresh TMDB lists."""
        self._force = True
        self.refresh()

    # -- loading -------------------------------------------------------------------------------

    def _clear_body(self) -> None:
        for child in self._body.winfo_children():
            destroy_tree(child)  # three full shelves take ~600 ms to destroy() widget by widget

    async def load(self) -> None:
        force, self._force = self._force, False
        self._scale = self._card_scale()
        self._clear_body()
        LoadingBar(self._body).pack(fill="x")
        try:
            shelves = await self._fetch_shelves(force=force)
        except asyncio.CancelledError:
            self._force = self._force or force  # a reload cut short still owes fresh lists
            raise
        except TMDBNotConfigured:
            self._show_problem(
                "TMDB API key needed",
                "Add your TMDB API key in ⚙ Settings to see trending, airing and upcoming titles.",
                ("Open Settings", self._on_open_settings),
            )
            return
        except TMDBAuthError:
            self._show_problem(
                "TMDB rejected the API key",
                "Check the key in ⚙ Settings.",
                ("Open Settings", self._on_open_settings),
            )
            return
        except TMDBError as exc:
            self._show_problem("Couldn't reach TMDB", str(exc), ("Retry", self.reload))
            return
        if not self.winfo_exists():
            return
        self._clear_body()
        for shelf in shelves:
            if shelf.failed or shelf.items:
                self._render_shelf(shelf)
                # Lets Tk draw the shelf and handle input before the next one is built.
                await asyncio.sleep(0.001)

    def _show_problem(self, title: str, body: str, action: tuple[str, Callable[[], None]]) -> None:
        if not self.winfo_exists():
            return
        self._clear_body()
        MessagePanel(self._body, title, body, action=action, height=_MESSAGE_HEIGHT).pack(
            fill="x", expand=True
        )

    def _render_shelf(self, shelf: Shelf[T]) -> None:
        section = ctk.CTkFrame(self._body, fg_color="transparent")
        section.pack(fill="x", pady=(0, 25))
        ctk.CTkLabel(
            section,
            text=f"{shelf.icon} {shelf.title}",
            font=theme.font(18, bold=True),
            text_color=shelf.color,
            anchor="w",
        ).pack(anchor="w", padx=10, pady=(0, 10))
        if shelf.failed:
            ctk.CTkLabel(
                section,
                text=f"Couldn't load {shelf.title} right now.",
                font=theme.font(13),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).pack(anchor="w", padx=10)
            return
        item_width = grid_item_width(POSTER_CARD_WIDTH, self._scale)
        grid = FlexGrid(section, item_width=item_width, pad_x=8, pad_y=10)
        grid.pack(fill="x")
        for item in shelf.items:
            self._card(grid, item)
        grid.relayout()

    # -- actions -------------------------------------------------------------------------------

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _report(self, text: str, *, clear_after_ms: int | None = None) -> None:
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass  # window already destroyed

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        super().destroy()
