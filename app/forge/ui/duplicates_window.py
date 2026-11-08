"""MediaForge's "+ Folder ▾" → "🔁 Find duplicates…": the episodes and films found twice in the
library folders (app/forge/core/duplicates.py), and "Move {n} extra copies aside".

One row per duplicate: its name, the copy kept (the biggest, its size) and the extra ones with
theirs. The button moves the extra copies to the "Duplicates" folder beside the library folder
(`on_move`, through the executor: History's Undo puts them back) and the window says how many
went. Nothing is deleted. Borderless in AirGrabber's style, over its window; Escape and the round
✕ close it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import customtkinter as ctk

from app.forge.core.duplicates import Duplicate
from app.forge.ui.look import app_look
from app.i18n import N_, t, tn
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    BUTTON_HEIGHT,
    BUTTON_RADIUS,
    CLOSE_CLEARANCE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    place_over,
    secondary_button,
    text_label,
)
from app.ui.scrollable import ScrollableFrame
from app.ui.tasks import spawn

TITLE = N_("Duplicates in your library")
TEXT = N_(
    "The same episode or film more than once. The biggest copy stays; the others move to a "
    "\"Duplicates\" folder beside the library folder. Nothing is deleted, and History can undo it."
)
NONE_TEXT = N_("No duplicates: every episode and film is in your library once.")
NO_FOLDERS = N_("Choose your Series and Movies folders in Settings › Auto-rename first.")
KEEP_TEXT = N_("Keeps {name} ({size})")
EXTRA_TEXT = N_("Moves {name} ({size})")
MOVE_ONE = N_("Move {n} extra copy aside")
MOVE_MORE = N_("Move {n} extra copies aside")
MOVED_ONE = N_("✓ Moved {n} copy to Duplicates")
MOVED_MORE = N_("✓ Moved {n} copies to Duplicates")
MOVING = N_("Moving…")
WIDTH = 600
LIST_HEIGHT = 360


def size_text(size: int) -> str:
    """"1.4 GB", "700 MB"."""
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB"
    return f"{max(1, round(size / 1024**2))} MB"


class DuplicatesWindow(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        duplicates: Sequence[Duplicate],
        *,
        on_move: Callable[[Sequence[Duplicate]], Awaitable[int]],
        has_folders: bool = True,
    ) -> None:
        look = app_look()
        super().__init__(master, fg_color=look.BG)
        self.title(t(TITLE))
        set_window_icon(self)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._duplicates = list(duplicates)
        self._on_move = on_move
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, t(TITLE), right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        if not has_folders:
            text = t(NO_FOLDERS)
        else:
            text = t(TEXT) if self._duplicates else t(NONE_TEXT)
        text_label(body, text, size=BODY_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH).pack(
            anchor="w", pady=(LINE_GAP, 0)
        )
        if self._duplicates:
            rows = ScrollableFrame(body, fg_color="transparent", width=WIDTH, height=LIST_HEIGHT)
            rows.pack(fill="both", expand=True, pady=(NAV_GAP, 0))
            rows.grid_columnconfigure(0, weight=1)
            for index, duplicate in enumerate(self._duplicates):
                self._row(rows, index, duplicate)
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(NAV_GAP, 0))
        extra = sum(len(duplicate.extra) for duplicate in self._duplicates)
        self.move_button: ctk.CTkButton | None = None
        if extra:
            self.move_button = ctk.CTkButton(
                buttons,
                text=tn(MOVE_ONE, MOVE_MORE, extra),
                height=BUTTON_HEIGHT,
                corner_radius=BUTTON_RADIUS,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                text_color=theme.ON_ACCENT,
                font=theme.font(15, bold=True),
                command=self.move,
            )
            self.move_button.pack(side="right", padx=(12, 0))
        secondary_button(buttons, t("Close"), self.close, width=120).pack(side="right")
        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading, buttons))
        place_over(self, master)
        activate_later(self)

    def _row(self, master: Any, index: int, duplicate: Duplicate) -> None:
        row = ctk.CTkFrame(master, fg_color=theme.SURFACE, corner_radius=8)
        row.grid(row=index, column=0, sticky="ew", pady=(0, 6))
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            row,
            text=duplicate.label,
            font=theme.font(14, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))
        lines = [t(KEEP_TEXT, name=duplicate.keep.path.name, size=size_text(duplicate.keep.size))]
        lines += [
            t(EXTRA_TEXT, name=copy.path.name, size=size_text(copy.size))
            for copy in duplicate.extra
        ]
        ctk.CTkLabel(
            row,
            text="\n".join(lines),
            font=theme.font(12),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=WIDTH - 40,
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

    def move(self) -> None:
        button = self.move_button
        if button is None:
            return
        button.configure(text=t(MOVING), state="disabled")
        spawn(self._move(button), name="DuplicatesWindow.move")

    async def _move(self, button: ctk.CTkButton) -> None:
        moved = await self._on_move(self._duplicates)
        if button.winfo_exists():
            button.configure(text=tn(MOVED_ONE, MOVED_MORE, moved))

    def close(self) -> None:
        if self.winfo_exists():
            self.destroy()
