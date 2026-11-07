"""Settings › Naming's "Manage…": the remembered matches in a small window of their own, modal over
Settings. It only shows and asks: what is forgotten stays the form's (settings_naming's NamingPart)
and happens on Settings' Save, as everything else there, so Cancel still takes it all back.

No title bar (app/ui/borderless.py), never `transient` (see app/forge/ui/widgets/dialogs.py); it
hands the grab back to Settings when it closes, as app/forge/ui/import_step.py does."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

import customtkinter as ctk

from app.forge.models.match import RememberedMatch
from app.forge.ui.look import app_look
from app.i18n import N_, t
from app.ui import theme
from app.ui.borderless import clearance, make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    FitScroll,
    LINE_GAP,
    activate_later,
    heading_row,
    max_dialog_height,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)

WIDTH = 560
PAD_X = 25
PAD_Y = 20
TEXT_SIZE = 13
ROW_GAP = 8
BUTTON_WIDTH = 90
BUTTONS_GAP = 16  # above the buttons
ROOM_AROUND_LIST = 220  # the heading, the note and the buttons, when the list gets its height
LIST_MAX = 420  # the list's height, at most; a longer one scrolls

SAVE_NOTE = N_("Nothing is forgotten until you click Save in Settings.")
DONE = N_("Done")
NONE_YET = N_("None yet")


class MatchesWindow(ctk.CTkToplevel):
    """`text_of(match)`: a row's words. `on_forget(match)` / `on_clear()`: the form's; it answers
    with `show(matches)` when the list changed."""

    def __init__(
        self,
        master: Any,
        *,
        title: str,
        text: str,
        forget_text: str,
        clear_text: str,
        text_of: Callable[[RememberedMatch], str],
        on_forget: Callable[[RememberedMatch], None],
        on_clear: Callable[[], None],
    ) -> None:
        look = app_look()
        super().__init__(master, fg_color=look.BG)
        self._look = look
        self._forget_text = forget_text
        self._text_of = text_of
        self._on_forget = on_forget
        # Settings holds the grab while it shows: it gets it back when this closes.
        self._grab_before: tk.Misc | None = self.grab_current()
        self._closed = False
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()  # the width
        heading = heading_row(body, title, right_room=clearance(PAD_X))
        heading.pack(fill="x", pady=(0, LINE_GAP))
        text_label(
            body, f"{text} {t(SAVE_NOTE)}", size=TEXT_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH
        ).pack(anchor="w", fill="x", pady=(0, BUTTONS_GAP))

        self.scroll = FitScroll(body, follow=True)
        self.scroll.pack(fill="both", expand=True)
        self.match_list = ctk.CTkFrame(self.scroll, fg_color="transparent")
        self.match_list.grid(row=0, column=0, sticky="nsew")
        self.match_list.grid_columnconfigure(0, weight=1)
        scale = float(ctk.ScalingTracker.get_widget_scaling(self)) or 1.0
        room = min(LIST_MAX, max_dialog_height(self, scale) - ROOM_AROUND_LIST)
        self.scroll.set_limit(round(room * scale))

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(BUTTONS_GAP, 0))
        self.clear_button = self._button(buttons, clear_text, on_clear)
        self.clear_button.pack(side="left")
        self.done_button = primary_button(
            buttons, t(DONE), self.close, width=BUTTON_WIDTH, look=look
        )
        self.done_button.pack(side="right")

        self.bind("<Escape>", lambda _event: self.close())
        make_borderless(self, drag_widgets=(self, body, heading), on_close=self.close, look=look)
        place_over(self, master)
        activate_later(self)

    def _button(self, master: Any, text: str, command: Callable[[], None]) -> ctk.CTkButton:
        button = secondary_button(master, text, command, width=BUTTON_WIDTH, look=self._look)
        button.configure(text_color=theme.BUTTON_TEXT)
        return button

    @property
    def closed(self) -> bool:
        return self._closed

    def show(self, matches: Sequence[RememberedMatch]) -> None:
        """The list as it stands now."""
        for child in self.match_list.winfo_children():
            child.destroy()
        if not matches:
            ctk.CTkLabel(
                self.match_list,
                text=t(NONE_YET),
                font=theme.font(TEXT_SIZE),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
        for row, match in enumerate(matches):
            pady = (0 if row == 0 else ROW_GAP // 2, 0)
            ctk.CTkLabel(
                self.match_list,
                text=self._text_of(match),
                font=theme.font(TEXT_SIZE),
                text_color=theme.TEXT,
                anchor="w",
                justify="left",
                wraplength=WIDTH - BUTTON_WIDTH - 30,
            ).grid(row=row, column=0, sticky="w", pady=pady)
            self._button(
                self.match_list, self._forget_text, partial(self._on_forget, match)
            ).grid(row=row, column=1, sticky="e", padx=(10, 0), pady=pady)
        self.clear_button.configure(state="normal" if matches else "disabled")
        self.scroll.refit()

    def close(self) -> None:
        """Done / Esc / ✕."""
        if self._closed:
            return
        self._closed = True
        grab = self._grab_before
        if self.winfo_exists():
            self.destroy()
        try:
            if grab is not None and grab.winfo_exists():
                grab.grab_set()  # Settings, modal again
                grab.focus_force()
        except tk.TclError:  # gone meanwhile: only the focus is lost
            pass
