"""A list of names as chips in a Settings box ("Prefer releases from", "Never use releases from";
docs/settings/2026-11-19-settings-redesign.md §6, mockups/settings-reorganized.html): each name a
chip with ×, then "+ Add", which opens a small field for one name or a few, separated with commas.

Each add or × is one change (`on_change(names)`, stored at once by the window). The field stores
what it holds on Enter, or when the keyboard leaves it inside the window (a `TextCommit`: the
window closing stores it too), and stays open after Enter for the next name; Escape takes it
away (what it held too) without closing Settings. A name already listed (whatever its case)
isn't added twice. `show(names)` shows what is stored (after a change here, elsewhere or an
Undo).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.dialog_kit import BUTTON_HEIGHT, BUTTON_RADIUS, DETAIL_SIZE, SMALL_SIZE
from app.ui.settings_commit import Committer, TextCommit

ADD_TEXT = N_("+ Add")
HINT_TEXT = N_("A name, or a few separated with commas")

CHIP_HEIGHT = 28
CHIP_GAP = 6
CHIP_ROOM = 34  # a chip's padding and its "  ×" beside the name's own width
ENTRY_WIDTH = 300


def added(names: Sequence[str], typed: str) -> list[str]:
    """`names` with what was typed (one name or a few, separated with commas) after them; a
    name already there (whatever its case) isn't added twice."""
    result = list(names)
    seen = {name.casefold() for name in result}
    for part in typed.split(","):
        name = part.strip()
        if name and name.casefold() not in seen:
            result.append(name)
            seen.add(name.casefold())
    return result


class ChipList(ctk.CTkFrame):
    """The chips, "+ Add" and, once asked, the field (see the module docstring)."""

    def __init__(
        self,
        master: Any,
        *,
        names: Sequence[str],
        on_change: Callable[[list[str]], object],
        width: int,
        committer: Committer | None = None,
    ) -> None:
        """`width`: the room for a line of chips (logical); more wrap onto the next line."""
        super().__init__(master, fg_color="transparent")
        self._names = list(names)
        self._on_change = on_change
        self._width = width
        self._font = theme.font(SMALL_SIZE + 1)
        self.chips: dict[str, ctk.CTkButton] = {}
        self._lines: list[ctk.CTkFrame] = []
        self.add_button = ctk.CTkButton(
            self,
            text=t(ADD_TEXT),
            height=CHIP_HEIGHT,
            width=0,
            corner_radius=CHIP_HEIGHT // 2,
            font=theme.font(SMALL_SIZE + 1, bold=True),
            fg_color="transparent",
            hover_color=theme.CHIP_HOVER,
            text_color=theme.LINK,
            command=self.open_field,
        )
        self.entry = ctk.CTkEntry(
            self,
            width=ENTRY_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            placeholder_text=t(HINT_TEXT),
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
        )
        self.field = TextCommit([self.entry], stored=lambda: [""], commit=self._typed)
        if committer is not None:
            committer.watch(self.field)
        self.entry.bind("<Escape>", self._escape, add="+")
        self.entry.bind("<FocusOut>", self._left, add="+")
        self._paint()

    @property
    def names(self) -> list[str]:
        return list(self._names)

    @property
    def field_open(self) -> bool:
        return bool(self.entry.winfo_manager())

    def show(self, names: Sequence[str]) -> None:
        """Show these (stored here, elsewhere, or by an Undo)."""
        if list(names) != self._names and self.winfo_exists():
            self._names = list(names)
            self._paint()

    def open_field(self) -> None:
        """"+ Add": the field, with the keyboard."""
        if not self.field_open:
            self.entry.pack(anchor="w", pady=(6, 0))
        self.entry.focus_set()

    def close_field(self) -> None:
        self.entry.delete(0, "end")
        self.entry.pack_forget()

    def _escape(self, _event: object) -> str:
        self.close_field()
        return "break"  # Escape here takes the field away, it doesn't close Settings

    def add(self, typed: str) -> None:
        """Add what was typed (one name or a few, separated with commas): one change."""
        names = added(self._names, typed)
        if names != self._names:
            self._change(names)

    def remove(self, name: str) -> None:
        """×: one change."""
        if name in self._names:
            self._change([known for known in self._names if known != name])

    def _has_focus(self) -> bool:
        try:
            focused = self.entry.focus_get()
        except (KeyError, tk.TclError):  # a popdown's name Tk can't resolve
            return False
        return focused is not None and focused in (self.entry, getattr(self.entry, "_entry", None))

    def _typed(self, texts: list[str]) -> None:
        # Enter, or the keyboard leaving the field: what it holds is added, the field emptied
        # (it stays open after Enter for the next name; leaving it takes it away).
        typed = texts[0]
        self.entry.delete(0, "end")
        if not self._has_focus():
            self.entry.pack_forget()
        self.add(typed)

    def _left(self, _event: object) -> None:
        """The keyboard left an empty field: it goes (what it held is stored by `field`)."""
        if self.entry.winfo_exists():
            self.entry.after_idle(self._close_if_empty)

    def _close_if_empty(self) -> None:
        if self.entry.winfo_exists() and not self.entry.get() and not self._has_focus():
            self.entry.pack_forget()

    def _change(self, names: list[str]) -> None:
        self._names = names
        self._paint()
        self._on_change(list(names))

    def _line(self) -> ctk.CTkFrame:
        line = ctk.CTkFrame(self, fg_color="transparent")
        if self.field_open:  # the lines stay above the open field
            line.pack(fill="x", before=self.entry)
        else:
            line.pack(fill="x")
        self._lines.append(line)
        return line

    def _paint(self) -> None:
        """The chips in lines of `width`, "+ Add" after the last one."""
        self.add_button.pack_forget()
        for line in self._lines:
            line.destroy()
        self._lines = []
        self.chips = {}
        line, used = self._line(), 0
        for name in self._names:
            room = int(self._font.measure(name)) + CHIP_ROOM + CHIP_GAP
            if used and used + room > self._width:
                line, used = self._line(), 0
            used += room
            chip = ctk.CTkButton(
                line,
                text=f"{name}  ×",
                height=CHIP_HEIGHT,
                width=0,
                corner_radius=CHIP_HEIGHT // 2,
                font=theme.font(SMALL_SIZE + 1),
                fg_color=theme.SURFACE,
                hover_color=theme.CHIP_HOVER,
                border_width=1,
                border_color=theme.EDGE,
                text_color=theme.TEXT,
                command=lambda n=name: self.remove(n),
            )
            chip.pack(side="left", padx=(0, CHIP_GAP), pady=2)
            self.chips[name] = chip
        self.add_button.pack(side="left", pady=2, in_=line)
