"""A small loader for the main window's footer: a word and a moving bar while something runs in
the background (Plex being read), nothing otherwise."""

from __future__ import annotations

from typing import Any

import customtkinter as ctk

from app.i18n import N_
from app.ui import theme

PLEX_SYNC_TEXT = N_("Syncing with Plex…")
BAR_WIDTH = 56
BAR_HEIGHT = 4
TEXT_GAP = 8


class SyncLoader(ctk.CTkFrame):
    """Grid it, `grid_remove()` it, then `set_busy` shows and hides it where it was gridded."""

    def __init__(self, master: Any, text: str) -> None:
        super().__init__(master, fg_color="transparent")
        self._busy = False
        ctk.CTkLabel(
            self, text=text, font=theme.font(11), text_color=theme.TEXT_MUTED, height=16
        ).pack(side="left", padx=(0, TEXT_GAP))
        self._bar = ctk.CTkProgressBar(
            self,
            width=BAR_WIDTH,
            height=BAR_HEIGHT,
            mode="indeterminate",
            fg_color=theme.EDGE,
            progress_color=theme.ACCENT,
        )
        self._bar.pack(side="left")

    @property
    def busy(self) -> bool:
        return self._busy

    def set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self.grid()
            self._bar.start()
        else:
            self._bar.stop()
            self.grid_remove()

    def destroy(self) -> None:
        self._bar.stop()
        super().destroy()
