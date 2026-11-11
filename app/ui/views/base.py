"""Base class for every page in the window."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Protocol

import customtkinter as ctk

from app.models import GrabTarget
from app.ui.tasks import spawn
from app.ui.widgets import MessagePanel


class StatusReporter(Protocol):
    """Shows a message in the window's status bar (`MainWindow.set_status`)."""

    def __call__(self, text: str, *, clear_after_ms: int | None = None) -> None: ...


class BaseView(ctk.CTkFrame, ABC):
    """A page that loads its own data asynchronously from injected services.

    Subclasses implement `load()`. `refresh()` (re)starts it and cancels a load still in flight,
    so rapid tab switches or setting changes can never render stale results.

    A view starts out stale; `invalidate()` marks it stale again when its data changed, and
    `refresh_if_stale()` reloads only then, so showing an unchanged page again costs nothing.

    `refresh_quietly()` is `refresh()` for a page on screen whose data changed under it (a
    show tracked, say): `load()` finds `quiet_load` set and, where the page knows how, keeps
    what it shows until the new content is ready, without a blank or loading state and with
    its scroll position. Pages that don't look at it reload as `refresh()` does.
    """

    def __init__(self, master: Any, **kwargs: Any) -> None:
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        self._load_task: asyncio.Task[None] | None = None
        self._stale = True
        # Whether the load just started came from `refresh_quietly`. Set before the load's
        # task starts: `load()` reads it before its first await.
        self.quiet_load = False

    @abstractmethod
    async def load(self) -> None:
        """Fetch data from the services and render it."""

    def refresh(self) -> None:
        self._start_load(quiet=False)

    def refresh_quietly(self) -> None:
        """Load again with `quiet_load` set (see the class docstring)."""
        self._start_load(quiet=True)

    def _start_load(self, *, quiet: bool) -> None:
        self._stale = False
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
        self.quiet_load = quiet
        task = spawn(self.load(), name=f"{type(self).__name__}.load")
        task.add_done_callback(self._load_finished)
        self._load_task = task

    def reload(self) -> None:
        """Load again now (F5 in the main window). Pages that cache what they fetch override it
        to fetch afresh."""
        self.refresh()

    def take_grab(self, target: GrabTarget) -> bool:
        """A download was just grabbed while this page is on screen: update what the page shows
        of it in place and return True, or return False to be reloaded instead (the default)."""
        return False

    def invalidate(self) -> None:
        """The data behind this view changed: the next `refresh_if_stale()` reloads it."""
        self._stale = True

    def is_stale(self) -> bool:
        """Whether showing the view again needs a reload. Subclasses may add conditions."""
        return self._stale

    def refresh_if_stale(self) -> None:
        if self.is_stale():
            self.refresh()

    def _load_finished(self, task: asyncio.Task[None]) -> None:
        # A failed load leaves the view stale, so showing it again retries.
        if task is self._load_task and not task.cancelled() and task.exception() is not None:
            self._stale = True

    def show_message(self, title: str, body: str = "") -> None:
        self.clear()
        MessagePanel(self, title, body).pack(fill="both", expand=True)

    def clear(self) -> None:
        for child in self.winfo_children():
            child.destroy()

    def destroy(self) -> None:
        if self._load_task is not None:
            self._load_task.cancel()
        super().destroy()
