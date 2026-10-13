"""Shared look of the roomy setup dialogs (first-run welcome, TMDB key guide): sizes, buttons,
headings, links, and placing a modal dialog over its parent."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.ui import theme

# Logical (unscaled) CTk units.
DIALOG_WIDTH = 640
PAD_X = 32
PAD_Y = 28
CONTENT_WIDTH = DIALOG_WIDTH - 2 * PAD_X
BLOCK_GAP = 24  # between the blocks of a step
LINE_GAP = 8  # between a heading and its muted line
NAV_GAP = 28  # above the Back / Next row
BUTTON_HEIGHT = 40
BUTTON_RADIUS = 8

HEADING_SIZE = 22
BODY_SIZE = 16
DETAIL_SIZE = 15  # muted lines and descriptions
SMALL_SIZE = 13  # "Step 2 of 3", hints


def primary_button(
    master: Any, text: str, command: Callable[[], None], *, width: int = 140
) -> ctk.CTkButton:
    return ctk.CTkButton(
        master,
        text=text,
        width=width,
        height=BUTTON_HEIGHT,
        corner_radius=BUTTON_RADIUS,
        fg_color=theme.ACCENT,
        hover_color=theme.ACCENT_HOVER,
        font=theme.font(15, bold=True),
        command=command,
    )


def secondary_button(
    master: Any, text: str, command: Callable[[], None], *, width: int = 140
) -> ctk.CTkButton:
    return ctk.CTkButton(
        master,
        text=text,
        width=width,
        height=BUTTON_HEIGHT,
        corner_radius=BUTTON_RADIUS,
        fg_color="transparent",
        border_width=1,
        border_color=theme.EDGE,
        hover_color=theme.SURFACE,
        text_color=theme.TEXT,
        font=theme.font(15),
        command=command,
    )


def heading_row(master: Any, title: str, step: str | None = None) -> ctk.CTkFrame:
    """A step's heading, with "Step N of 3" at the top right when `step` is given."""
    row = ctk.CTkFrame(master, fg_color="transparent")
    row.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(
        row,
        text=title,
        font=theme.font(HEADING_SIZE, bold=True),
        text_color=theme.TEXT,
        anchor="w",
        justify="left",
    ).grid(row=0, column=0, sticky="w")
    if step is not None:
        ctk.CTkLabel(
            row, text=step, font=theme.font(SMALL_SIZE), text_color=theme.TEXT_MUTED
        ).grid(row=0, column=1, sticky="ne", padx=(16, 0))
    return row


def text_label(
    master: Any,
    text: str,
    *,
    size: int = BODY_SIZE,
    color: str = theme.TEXT,
    wraplength: int = CONTENT_WIDTH,
    bold: bool = False,
) -> ctk.CTkLabel:
    """Left-aligned wrapping text (CTk scales `wraplength` with the DPI)."""
    return ctk.CTkLabel(
        master,
        text=text,
        font=theme.font(size, bold=bold),
        text_color=color,
        anchor="w",
        justify="left",
        wraplength=wraplength,
    )


def link_label(master: Any, text: str, url: str, *, size: int = DETAIL_SIZE) -> ctk.CTkLabel:
    """Underlined link text that opens `url` in the browser."""
    link = ctk.CTkLabel(
        master,
        text=text,
        font=theme.font(size, underline=True),
        text_color=theme.LINK,
        cursor="hand2",
    )
    link.bind("<Button-1>", lambda _event: webbrowser.open(url))
    return link


def place_over(dialog: ctk.CTkToplevel, master: Any) -> None:
    """Centre `dialog` horizontally over `master`, in its upper third (as Settings does)."""
    dialog.update_idletasks()
    x = master.winfo_rootx() + (master.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = master.winfo_rooty() + (master.winfo_height() - dialog.winfo_reqheight()) // 3
    dialog.geometry(f"+{max(0, x)}+{max(0, y)}")


def activate_later(dialog: ctk.CTkToplevel) -> None:
    """Raise, focus and grab `dialog` once it is mapped: CTkToplevel can open behind its parent
    on Windows."""

    def activate() -> None:
        if dialog.winfo_exists():
            dialog.lift()
            dialog.focus_force()
            dialog.grab_set()

    dialog.after(150, activate)
