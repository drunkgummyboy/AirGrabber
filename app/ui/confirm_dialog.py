"""A small question before something that can't be undone (the Downloads page's "Remove from
Download Station?" and "Clear finished downloads?"): a heading, one line of text, Cancel and a
red button that does it. Borderless in AirGrabber's style (app/ui/dialog_kit.py), over its
window; Escape, the round ✕ and Cancel close it without doing anything.

The red button closes the dialog and calls `on_confirm` (the page shows the change at once and
reports a failure itself)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.i18n import t
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

WIDTH = 420  # the text's width (CTk units)
BUTTON_WIDTH = 120


class ConfirmDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        *,
        title: str,
        text: str,
        confirm: str,
        on_confirm: Callable[[], object],
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._on_confirm = on_confirm
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, title, right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        self.text = text_label(
            body, text, size=BODY_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH
        )
        self.text.pack(anchor="w", pady=(LINE_GAP, 0))
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(NAV_GAP, 0))
        self.confirm_button = ctk.CTkButton(
            buttons,
            text=confirm,
            width=BUTTON_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            fg_color=theme.DANGER_BUTTON,
            hover_color=theme.DANGER_BUTTON_HOVER,
            text_color=theme.ON_COLOR,
            font=theme.font(15, bold=True),
            command=self.confirm,
        )
        self.confirm_button.pack(side="right", padx=(12, 0))
        self.cancel_button = secondary_button(buttons, t("Cancel"), self.close, width=BUTTON_WIDTH)
        self.cancel_button.pack(side="right")
        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading, buttons))
        place_over(self, master)
        activate_later(self)

    def confirm(self) -> None:
        self.close()
        self._on_confirm()

    def close(self) -> None:
        if self.winfo_exists():
            self.destroy()
