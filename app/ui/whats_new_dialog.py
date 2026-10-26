"""The "What's new in AirGrabber 2.0.5" dialog, shown once on the first start after an update
(notes in app/whats_new.py). OK, Enter, Escape and the X close it."""

from __future__ import annotations

from collections.abc import Sequence

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    CONTENT_WIDTH,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    place_over,
    primary_button,
    text_label,
)
from app.whats_new import dialog_title

BULLET = "•"
BULLET_WIDTH = 22  # the bullet's column; the lines wrap beside it
OK_TEXT = N_("OK")


class WhatsNewDialog(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, *, version: str, lines: Sequence[str]) -> None:
        """`lines`: the notes in English (app/whats_new.py); they show through t()."""
        super().__init__(master, fg_color=theme.BG)
        title = dialog_title(version)
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        # Holds the content width.
        ctk.CTkFrame(body, width=CONTENT_WIDTH, height=1, fg_color="transparent").pack()
        heading_row(body, title).pack(fill="x")
        notes = ctk.CTkFrame(body, fg_color="transparent")
        notes.pack(fill="x", pady=(LINE_GAP * 2, 0))
        notes.grid_columnconfigure(1, weight=1)
        for row, line in enumerate(lines):
            pad = (0 if row == 0 else LINE_GAP, 0)
            # Both from the top, so the bullet stays level with a wrapped line's first row.
            ctk.CTkLabel(
                notes,
                text=BULLET,
                width=BULLET_WIDTH,
                height=1,  # as tall as its text: even gaps between the lines
                anchor="nw",
                font=theme.font(BODY_SIZE),
                text_color=theme.TEXT_MUTED,
            ).grid(row=row, column=0, sticky="nw", pady=pad)
            text = text_label(notes, t(line), wraplength=CONTENT_WIDTH - BULLET_WIDTH)
            text.configure(anchor="nw", height=1)
            text.grid(row=row, column=1, sticky="nw", pady=pad)
        nav = ctk.CTkFrame(body, fg_color="transparent")
        nav.pack(fill="x", pady=(NAV_GAP, 0))
        primary_button(nav, t(OK_TEXT), self.destroy).pack(side="right")

        self.bind("<Return>", lambda _event: self.destroy())
        self.bind("<Escape>", lambda _event: self.destroy())
        place_over(self, master)
        activate_later(self)
