"""Settings → Where downloads go → "Reinstall qBittorrent…": what a reinstall does, then a choice
(the owner's wish, 2026-11-08: both). Repair installs qBittorrent again over itself and keeps its
settings and torrents; Clean start also sets those aside first (renamed beside where they were,
never deleted). Borderless in AirGrabber's style, like app/ui/confirm_dialog.py; Escape, the ✕ and
Cancel close it without doing anything. A choice closes it and calls `on_choice(clean)`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    CLOSE_CLEARANCE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)

TITLE_TEXT = N_("Reinstall qBittorrent?")
REPAIR_TITLE = N_("Repair")
REPAIR_TEXT = N_("Installs qBittorrent again over itself. Your torrents and its settings stay.")
CLEAN_TITLE = N_("Clean start")
CLEAN_TEXT = N_(
    "Also puts qBittorrent's settings and torrent list aside, in a folder beside them, and sets "
    "it up anew. Downloaded files stay where they are."
)
NOTE_TEXT = N_("Close qBittorrent first. Windows may ask for permission.")
WIDTH = 440  # the text's width (CTk units)
BUTTON_WIDTH = 130


class ReinstallDialog(ctk.CTkToplevel):
    def __init__(self, master: Any, *, on_choice: Callable[[bool], object]) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title(t(TITLE_TEXT))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._on_choice = on_choice
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, t(TITLE_TEXT), right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        for title, text in ((REPAIR_TITLE, REPAIR_TEXT), (CLEAN_TITLE, CLEAN_TEXT)):
            text_label(
                body, t(title), size=BODY_SIZE, color=theme.TEXT, wraplength=WIDTH, bold=True
            ).pack(anchor="w", pady=(LINE_GAP + 4, 0))
            text_label(
                body, t(text), size=BODY_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH
            ).pack(anchor="w", pady=(2, 0))
        text_label(body, t(NOTE_TEXT), size=BODY_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH).pack(
            anchor="w", pady=(LINE_GAP + 4, 0)
        )
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(NAV_GAP, 0))
        self.repair_button = primary_button(
            buttons, t(REPAIR_TITLE), lambda: self.choose(False), width=BUTTON_WIDTH
        )
        self.repair_button.pack(side="right", padx=(12, 0))
        self.clean_button = secondary_button(
            buttons, t(CLEAN_TITLE), lambda: self.choose(True), width=BUTTON_WIDTH
        )
        self.clean_button.pack(side="right", padx=(12, 0))
        self.cancel_button = secondary_button(buttons, t("Cancel"), self.close, width=BUTTON_WIDTH)
        self.cancel_button.pack(side="right")
        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading, buttons))
        place_over(self, master)
        activate_later(self)

    def choose(self, clean: bool) -> None:
        self.close()
        self._on_choice(clean)

    def close(self) -> None:
        if self.winfo_exists():
            self.destroy()
