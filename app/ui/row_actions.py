"""The download controls of the show window's episode rows and the film info window (2.0.7).

Two quiet controls instead of one big filled button: an outlined "⬇ Auto" (the one-click
download: AirGrabber picks the best release) and a text-style "☰ Choose…" (the info window's
releases, to pick one yourself), each with a hover tip. A downloaded title shows a green
"✓ Downloaded" and a quiet "↻ Again", both opening a small menu with the same two again
(`again_items`).

The one-click runs keep their state in the main window's DownloadStates (shared with the
cards' DownloadControl), so a row rebuilt mid-run picks it up: while it works "Finding the best
one…", once it grabbed the green outcome ("✓ Sent", what it picked as the tip, then "↻ Again"),
when nothing was good enough an amber "No good release found" beside "☰ Choose…" outlined
in amber. `row_look` is the rule; RowActions draws it.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.download_control import (
    WORKING_SHORT,
    WORKING_TEXT,
    Base,
    DownloadActions,
    DownloadKey,
    Phase,
    RunState,
    fit_text,
    outcome_text,
    picked_text,
)
from app.ui.missing_control import MenuItem, popup_menu
from app.ui.text_fit import SLACK, Measure, unscaled_width
from app.ui.tooltip import add_tooltip

T = TypeVar("T")

# Logical (unscaled) CTk units.
HEIGHT = 32
FONT_SIZE = 13
BUTTON_PAD = 12  # left and right of a button's text
GAP = 6  # between the controls
RADIUS = 7
MIN_WIDTH = 190  # the slot never gets narrower than this
LABEL_PAD = 16  # what a status label takes beyond its text (CTk's own padding)

# Shown through t().
AUTO_TEXT = N_("⬇ Auto")
AUTO_TIP = N_("Download automatically: AirGrabber picks the best release")
CHOOSE_TEXT = N_("☰ Choose…")
CHOOSE_TIP = N_("Choose a release yourself from the search results")
DOWNLOADED_TEXT = N_("✓ Downloaded")
AGAIN_TEXT = N_("↻ Again")
AGAIN_TIP = N_("Download it again")
AGAIN_AUTO_TEXT = N_("⬇ Download again automatically")
AGAIN_CHOOSE_TEXT = N_("☰ Choose a release…")
NOTHING_TEXT = N_("No good release found")
NOTHING_SHORT = N_("None found")
NOTHING_TIP = N_("Nothing good enough was found. Choose a download yourself.")

RowKind = Literal["download", "search", "working", "nothing", "done", "downloaded", "unavailable"]
Button = Literal["auto", "choose", "again"]


@dataclass(frozen=True, slots=True)
class RowLook:
    """How the controls look: a status text (longest first, then shorter ones; none = no
    status) in `color`, then buttons. `clickable`: the status opens the ↻ Again menu; `alert`:
    ☰ Choose… outlined in amber (nothing was good enough)."""

    kind: RowKind
    status: tuple[str, ...] = ()
    color: theme.Color = theme.TEXT_MUTED
    buttons: tuple[Button, ...] = ()
    clickable: bool = False
    alert: bool = False


def row_look(base: Base, state: RunState | None, *, downloaded: bool = False) -> RowLook:
    """The controls for a title's base look, its run state and whether it is in the download
    history. A run working or done wins; then downloaded; then a disabled base ("Not
    Released"); then a run that found nothing; else ⬇ Auto + ☰ Choose… (☰ Choose… alone for a
    title without a number to download by)."""
    if state is not None and state.phase is Phase.WORKING:
        return RowLook("working", (t(WORKING_TEXT), t(WORKING_SHORT)))
    if state is not None and state.phase is Phase.DONE and state.outcome is not None:
        return RowLook(
            "done", (outcome_text(state.outcome),), theme.SUCCESS, ("again",), clickable=True
        )
    if downloaded or base.kind == "grabbed" or (state is not None and state.phase is Phase.DONE):
        return RowLook(
            "downloaded", (t(DOWNLOADED_TEXT),), theme.SUCCESS, ("again",), clickable=True
        )
    if base.kind == "unavailable":
        return RowLook("unavailable", (base.text,))
    if state is not None and state.phase is Phase.NOTHING:
        return RowLook(
            "nothing", (t(NOTHING_TEXT), t(NOTHING_SHORT)), theme.PENDING, ("choose",), alert=True
        )
    if base.kind == "search":
        return RowLook("search", buttons=("choose",))
    return RowLook("download", buttons=("auto", "choose"))


def button_width(text: str, measure: Measure) -> int:
    """A button just wide enough for its text."""
    return round(measure(text)) + 2 * BUTTON_PAD


def slot_width(measure: Measure) -> int:
    """The room the controls take in a row (logical units, in the current language): the
    widest of ⬇ Auto + ☰ Choose…, ✓ Downloaded + ↻ Again, "None found" + ☰ Choose… and the
    working text."""
    choose = button_width(t(CHOOSE_TEXT), measure)
    again = button_width(t(AGAIN_TEXT), measure)
    pairs = (
        button_width(t(AUTO_TEXT), measure) + GAP + choose,
        round(measure(t(DOWNLOADED_TEXT))) + LABEL_PAD + GAP + again,
        round(measure(t(NOTHING_SHORT))) + LABEL_PAD + GAP + choose,
        round(measure(t(WORKING_TEXT))),
    )
    return max(MIN_WIDTH, *pairs) + SLACK


def again_items(
    download_again: Callable[[], None] | None, choose: Callable[[], None]
) -> list[MenuItem]:
    """↻ Again's menu: download again automatically (when offered), choose a release."""
    items: list[MenuItem] = []
    if download_again is not None:
        items.append((t(AGAIN_AUTO_TEXT), download_again))
    items.append((t(AGAIN_CHOOSE_TEXT), choose))
    return items


def status_tip(look: RowLook, state: RunState | None) -> str:
    """The status text's hover tip: what a finished run picked, why nothing was downloaded,
    or that a downloaded title can be downloaded again."""
    if look.kind == "done" and state is not None:
        return picked_text(state.quality, state.size_bytes) or t(AGAIN_TIP)
    if look.kind == "downloaded":
        return t(AGAIN_TIP)
    if look.kind == "nothing":
        return t(NOTHING_TIP)
    return ""


class RowActions(ctk.CTkFrame, Generic[T]):
    """The controls of one title (see the module docstring), right-aligned in their frame."""

    def __init__(
        self,
        master: Any,
        *,
        item: T,
        key: DownloadKey,
        base: Base,
        actions: DownloadActions[T],
        downloaded: bool = False,
        height: int = HEIGHT,
        align: Literal["left", "right"] = "right",
    ) -> None:
        """`align`: where the controls sit in the frame (the show window's rows: right; the
        film window's buttons: left)."""
        super().__init__(master, fg_color="transparent", height=height)
        self._align = align
        if align == "right":  # a fixed slot; on the left it is as wide as what it shows
            self.pack_propagate(False)
        self.key = key
        self._height = height
        self._item = item
        self._base = base
        self._downloaded = downloaded
        self._actions = actions
        self._look: RowLook | None = None
        self._width: float | None = None  # unscaled, once laid out
        self._font = theme.font(FONT_SIZE, bold=True)
        measure = self._font.measure

        self.status = ctk.CTkLabel(
            self, text="", height=height, font=self._font, text_color=theme.TEXT_MUTED, anchor="e"
        )
        self._status_tip = add_tooltip(self.status, "")
        # CTk's own bind: the click lands on the label's inner Tk label.
        self.status.bind("<Button-1>", self._status_clicked, add="+")
        self.auto = self._button(t(AUTO_TEXT), measure, outlined=True, command=self._auto)
        self.choose = self._button(t(CHOOSE_TEXT), measure, outlined=False, command=self._choose)
        self.again = self._button(t(AGAIN_TEXT), measure, outlined=False, command=self.open_again)
        # Before the tip's own <Enter>: the tip reads Settings as they are when hovered.
        tk.Misc.bind(self.auto, "<Enter>", self._update_auto_tip, add="+")
        self._auto_tip = add_tooltip(self.auto, t(AUTO_TIP))
        add_tooltip(self.choose, t(CHOOSE_TIP))
        add_tooltip(self.again, t(AGAIN_TIP))
        self.bind("<Configure>", self._on_resize, add="+")
        actions.states.attach(self)
        self.repaint()

    def _button(
        self, text: str, measure: Measure, *, outlined: bool, command: Callable[[], None]
    ) -> ctk.CTkButton:
        return ctk.CTkButton(
            self,
            text=text,
            width=button_width(text, measure),
            height=self._height,
            corner_radius=RADIUS,
            font=self._font,
            fg_color="transparent",
            hover_color=theme.HOVER if outlined else theme.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT if outlined else theme.TEXT_MUTED,
            border_width=1 if outlined else 0,
            border_color=theme.EDGE,
            command=command,
        )

    # -- state ---------------------------------------------------------------------------------

    def set_base(self, base: Base, *, downloaded: bool = False) -> None:
        if (base, downloaded) != (self._base, self._downloaded):
            self._base, self._downloaded = base, downloaded
            self.repaint()

    def current_look(self) -> RowLook:
        return row_look(
            self._base, self._actions.states.get(self.key), downloaded=self._downloaded
        )

    @property
    def look(self) -> RowLook | None:
        return self._look

    def repaint(self) -> None:
        look = self.current_look()
        if look == self._look:
            return
        self._look = look
        for widget in (self.status, self.auto, self.choose, self.again):
            widget.pack_forget()
        widgets = {"auto": self.auto, "choose": self.choose, "again": self.again}
        if look.status:
            self.status.configure(text_color=look.color, cursor="hand2" if look.clickable else "")
        if self._align == "right":
            for name in reversed(look.buttons):
                widgets[name].pack(side="right", padx=(GAP, 0))
            if look.status:
                self.status.configure(anchor="e")
                self.status.pack(side="right", fill="x", expand=True)
        else:
            if look.status:
                self.status.configure(anchor="w")
                self.status.pack(side="left")
            for index, name in enumerate(look.buttons):
                gap = GAP if index or look.status else 0
                widgets[name].pack(side="left", padx=(gap, 0))
        self.choose.configure(
            border_width=1 if look.alert else 0,
            border_color=theme.PENDING if look.alert else theme.EDGE,
            text_color=theme.PENDING if look.alert else theme.TEXT_MUTED,
        )
        self._status_tip.set_text(status_tip(look, self._actions.states.get(self.key)))
        self._fit()

    def _on_resize(self, event: Any) -> None:
        if event.width > 1:
            self._width = unscaled_width(self, event.width)
            self._fit()

    def _fit(self) -> None:
        look = self._look
        if look is None or not look.status:
            return
        text = look.status[0]
        # On the left the frame is as wide as what it shows: nothing to fit into.
        if self._width is not None and self._align == "right":
            shown = {"auto": self.auto, "choose": self.choose, "again": self.again}
            buttons = sum(
                unscaled_width(self, shown[name].winfo_reqwidth()) + GAP for name in look.buttons
            )
            text = fit_text(look.status, self._width - buttons - SLACK, self._font.measure)
        self.status.configure(text=text)

    # -- clicks --------------------------------------------------------------------------------

    def _update_auto_tip(self, _event: Any = None) -> None:
        looks_for = self._actions.states.looks_for(self.key)
        self._auto_tip.set_text(f"{t(AUTO_TIP)}\n{looks_for}" if looks_for else t(AUTO_TIP))

    def _auto(self) -> None:
        self._actions.download(self._item)

    def _choose(self) -> None:
        self._actions.choose(self._item)

    def _status_clicked(self, _event: Any = None) -> None:
        if self._look is not None and self._look.clickable:
            self.open_again()

    def open_again(self) -> None:
        """↻ Again's menu, under the ↻ Again button (also a click on ✓ Downloaded or on the
        downloaded row)."""
        look = self._look
        if look is None or not (look.clickable or "again" in look.buttons):
            return
        again = self._actions.download_again
        item = self._item
        download_again = (lambda: again(item)) if again is not None else None
        anchor = self.again if self.again.winfo_ismapped() else self
        popup_menu(anchor, again_items(download_again, self._choose))

    def destroy(self) -> None:
        self._actions.states.detach(self)
        super().destroy()
