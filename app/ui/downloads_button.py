"""The header's ⬇ button (next to the settings gear): opens the Downloads page, shows a small
number badge while downloads are on their way, and puts the once-a-day "almost full" notice in
the status bar."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models import DownloadsOverview, FreeSpace
from app.services import DownloadStatusService
from app.ui import theme
from app.ui.tooltip import add_tooltip
from app.ui.views.downloads_view import badge_text, space_status_text

TIP = N_("Downloads")
SPACE_NOTICE_MS = 15_000  # how long the "almost full" notice stays in the status bar


class DownloadsButton(ctk.CTkFrame):
    def __init__(
        self,
        master: Any,
        *,
        downloads: DownloadStatusService,
        command: Callable[[], None],
        report: Callable[[str, int], None],
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        self._report = report
        self._button = ctk.CTkButton(
            self,
            text="⬇",
            width=40,
            height=40,
            corner_radius=10,
            fg_color="transparent",
            hover_color=theme.SURFACE,
            text_color=theme.BUTTON_TEXT,
            font=theme.font(24),
            command=command,
        )
        self._button.pack()
        add_tooltip(self._button, t(TIP))
        self._badge = ctk.CTkLabel(
            self,
            text="",
            width=18,
            height=18,
            corner_radius=9,
            fg_color=theme.SUCCESS,
            text_color=theme.ON_COLOR,
            font=theme.font(10, bold=True),
        )
        self._badge.bind("<Button-1>", lambda _event: command(), add="+")
        self._unsubscribe = downloads.subscribe(self._on_overview)
        self._unsubscribe_space = downloads.subscribe_space(self._on_low_space)

    def set_selected(self, selected: bool) -> None:
        """Highlighted like the Series / Movies switch while the Downloads page shows."""
        self._button.configure(
            fg_color=theme.ACCENT if selected else "transparent",
            text_color=theme.ON_ACCENT if selected else theme.BUTTON_TEXT,
        )

    def _on_overview(self, overview: DownloadsOverview) -> None:
        try:
            if not self.winfo_exists():
                return
            text = badge_text(overview.in_progress)
            if text:
                self._badge.configure(text=text)
                self._badge.place(in_=self._button, relx=1.0, rely=0.0, anchor="ne", x=2, y=-2)
                self._badge.lift()
            else:
                self._badge.place_forget()
        except tk.TclError:
            pass  # the window is closing

    def _on_low_space(self, space: FreeSpace) -> None:
        self._report(space_status_text(space), SPACE_NOTICE_MS)

    def destroy(self) -> None:
        self._unsubscribe()
        self._unsubscribe_space()
        super().destroy()
