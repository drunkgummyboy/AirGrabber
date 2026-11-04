"""Small question windows that can be awaited (ported from MediaForge's ui/widgets/dialogs.py),
built on AirGrabber's dialog kit: no title bar (app/ui/borderless.py; the title as a heading),
placed over the window that asks, and accent buttons through the look.

MediaForge's lesson stays: a pop-up is never `transient()`. Tk on Windows could crash later when
a transient window (with a size set) was destroyed before it was ever shown
(tests/forge/ui/test_dialogs.py keeps that as a test). `activate_later` raises, focuses and grabs
the window instead.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Sequence
from functools import partial
from typing import Any

import customtkinter as ctk

from app.forge.ui.look import app_look
from app.i18n import t
from app.ui import theme
from app.ui.borderless import clearance, make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    LINE_GAP,
    activate_later,
    heading_row,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)

_PAD_X = 25  # as widgets.show_info
_PAD_Y = 20
_MESSAGE_WIDTH = 420
_MESSAGE_SIZE = 13
_BUTTON_GAP = 8
_BUTTONS_GAP = 16  # above the buttons


async def ask_choice(
    master: Any,
    title: str,
    message: str,
    choices: Sequence[str],
    *,
    look: theme.Look | None = None,
) -> str | None:
    """Show a question with a button per choice; returns the chosen label, or None when the
    window is closed (✕, Escape, destroyed) or the waiting task is cancelled. The first choice
    is the main (accent) button, first in the row at the right (as in MediaForge); Enter picks
    it. Texts are shown as given."""
    colours: theme.Look = app_look() if look is None else look
    future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    dialog = ctk.CTkToplevel(master, fg_color=colours.BG)
    dialog.title(title)
    set_window_icon(dialog)
    dialog.resizable(False, False)

    def finish(value: str | None) -> None:
        if not future.done():
            future.set_result(value)
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:
            pass  # gone with its interpreter

    def gone(event: tk.Event[Any]) -> None:
        if event.widget is dialog and not future.done():
            future.set_result(None)

    dialog.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    dialog.bind("<Destroy>", gone, add="+")

    body = ctk.CTkFrame(dialog, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=_PAD_X, pady=_PAD_Y)
    heading = heading_row(body, title, right_room=clearance(_PAD_X))
    heading.pack(fill="x", pady=(0, LINE_GAP))
    text_label(
        body, message, size=_MESSAGE_SIZE, color=theme.TEXT_MUTED, wraplength=_MESSAGE_WIDTH
    ).pack(anchor="w", fill="x")
    buttons = ctk.CTkFrame(body, fg_color="transparent")
    buttons.pack(fill="x", pady=(_BUTTONS_GAP, 0))
    labels = list(choices)
    for index, label in reversed(list(enumerate(labels))):
        make = primary_button if index == 0 else secondary_button
        make(buttons, label, partial(finish, label), width=100, look=colours).pack(
            side="right", padx=(_BUTTON_GAP, 0)
        )

    dialog.bind("<Escape>", lambda _event: finish(None))
    if labels:
        dialog.bind("<Return>", lambda _event: finish(labels[0]))
    make_borderless(
        dialog, drag_widgets=(dialog, body, heading), on_close=lambda: finish(None), look=colours
    )
    place_over(dialog, master)
    activate_later(dialog)
    try:
        return await future
    finally:
        finish(None)  # cancelled: the window goes too


async def ask_text(
    master: Any,
    title: str,
    message: str,
    ok: str,
    *,
    initial: str = "",
    look: theme.Look | None = None,
) -> str | None:
    """Ask for a short text (a preset's name): the text typed, without spaces around it, or None
    when the window is closed or Cancel chosen. `ok` (the main button, also Enter) only takes a
    text that isn't empty. Texts are shown as given."""
    colours: theme.Look = app_look() if look is None else look
    future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    dialog = ctk.CTkToplevel(master, fg_color=colours.BG)
    dialog.title(title)
    set_window_icon(dialog)
    dialog.resizable(False, False)

    def finish(value: str | None) -> None:
        if not future.done():
            future.set_result(value)
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:
            pass  # gone with its interpreter

    def gone(event: tk.Event[Any]) -> None:
        if event.widget is dialog and not future.done():
            future.set_result(None)

    dialog.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    dialog.bind("<Destroy>", gone, add="+")
    body = ctk.CTkFrame(dialog, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=_PAD_X, pady=_PAD_Y)
    heading = heading_row(body, title, right_room=clearance(_PAD_X))
    heading.pack(fill="x", pady=(0, LINE_GAP))
    text_label(
        body, message, size=_MESSAGE_SIZE, color=theme.TEXT_MUTED, wraplength=_MESSAGE_WIDTH
    ).pack(anchor="w", fill="x")
    value = tk.StringVar(master=dialog, value=initial)
    entry = ctk.CTkEntry(
        body,
        textvariable=value,
        width=_MESSAGE_WIDTH,
        fg_color=colours.SURFACE,
        border_color=colours.PLACEHOLDER,
        text_color=theme.TEXT,
        font=theme.font(_MESSAGE_SIZE),
    )
    entry.pack(fill="x", pady=(LINE_GAP, 0))

    def accept() -> None:
        if value.get().strip():
            finish(value.get().strip())

    buttons = ctk.CTkFrame(body, fg_color="transparent")
    buttons.pack(fill="x", pady=(_BUTTONS_GAP, 0))
    secondary_button(buttons, t("Cancel"), lambda: finish(None), width=100, look=colours).pack(
        side="right", padx=(_BUTTON_GAP, 0)
    )
    primary_button(buttons, ok, accept, width=100, look=colours).pack(
        side="right", padx=(_BUTTON_GAP, 0)
    )
    dialog.bind("<Escape>", lambda _event: finish(None))
    dialog.bind("<Return>", lambda _event: accept())
    make_borderless(
        dialog, drag_widgets=(dialog, body, heading), on_close=lambda: finish(None), look=colours
    )
    place_over(dialog, master)
    activate_later(dialog)
    dialog.after(50, lambda: entry.focus_set() if entry.winfo_exists() else None)
    try:
        return await future
    finally:
        finish(None)  # cancelled: the window goes too


async def confirm(
    master: Any, title: str, message: str, yes: str, *, look: theme.Look | None = None
) -> bool:
    """A yes / Cancel question: True when `yes` was chosen."""
    return await ask_choice(master, title, message, [yes, t("Cancel")], look=look) == yes
