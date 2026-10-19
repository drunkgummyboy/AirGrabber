"""Missing episodes in the UI: the amber "3 missing" chip, the "⬇ Download missing" button with
the run states behind it, and the small option menus ("I already have this").

The main window runs "Download missing" (one run per show at a time) and keeps each show's run
state in a `MissingRuns`; every button showing a show attaches to it, so a card or show window
rebuilt mid-run (pages reload after every grab) picks the run's state up, and a finished run
repaints whichever buttons show the show by then. Which episodes are missing is decided in
app.services.missing.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import customtkinter as ctk

from app.models import MissingRunReport, Show
from app.ui import theme
from app.ui.download_control import CORNER_RADIUS, Action, Look, fit_text
from app.ui.text_fit import SLACK, unscaled_width
from app.ui.tooltip import add_tooltip

MISSING_COLOR = theme.WARNING  # amber: something is missing rather than broken
OUTCOME_MS = 60_000  # a finished run's outcome stays on its button this long

DOWNLOAD_MISSING_TEXT = "⬇ Download missing"
DOWNLOAD_MISSING_SHORT = ("⬇ Download", "⬇")
STARTING_TEXT = "Finding missing episodes…"
STARTING_SHORT = "Starting…"
FAILED_TEXT = "Couldn't download"
NOTHING_MISSING_TEXT = "Nothing missing"
CHIP_MISSING_TEXT = "Missing"
CHIP_HAVE_TEXT = "✓ Have it"
CHIP_TIP = "Options"
HAVE_EPISODE_TEXT = "I already have this"
HAVE_SEASON_TEXT = "I already have this season"
MARK_MISSING_TEXT = "Mark as missing again"
SEASON_MENU_TIP = "More options"


# -- texts -------------------------------------------------------------------------------------


def missing_text(count: int) -> str:
    """"3 missing"."""
    return f"{count} missing"


def download_missing_text(count: int | None = None) -> str:
    """"⬇ Download missing (3)", or without the count "⬇ Download missing"."""
    return DOWNLOAD_MISSING_TEXT if count is None else f"{DOWNLOAD_MISSING_TEXT} ({count})"


def progress_texts(index: int, total: int) -> tuple[str, ...]:
    """"Downloading 2 of 3…" and shorter forms; before the first episode starts, "Finding
    missing episodes…"."""
    if index <= 0 or total <= 0:
        return STARTING_TEXT, STARTING_SHORT
    return f"Downloading {index} of {total}…", f"Getting {index} of {total}…", f"{index} of {total}…"


def _got(report: MissingRunReport) -> int:
    """Episodes the run leaves downloaded: grabbed now, or found grabbed meanwhile."""
    return len(report.downloaded) + len(report.already)


def outcome_texts(report: MissingRunReport) -> tuple[str, ...]:
    """A finished run in plain words, longest first: "✓ 2 downloaded · 1 not found yet"."""
    got, missed = _got(report), len(report.not_found)
    if report.error is not None:
        if got:
            return f"✓ {got} downloaded · stopped", f"✓ {got} · stopped", "Stopped"
        return FAILED_TEXT, "Failed"
    if got and missed:
        return (
            f"✓ {got} downloaded · {missed} not found yet",
            f"✓ {got} downloaded · {missed} not found",
            f"✓ {got} · {missed} not found",
            f"✓ {got} downloaded",
        )
    if got:
        return f"✓ {got} downloaded", f"✓ {got}"
    if missed:
        return f"{missed} not found yet", "Not found yet"
    return (NOTHING_MISSING_TEXT,)


def outcome_status_text(show_name: str, report: MissingRunReport) -> str:
    """The status bar after a run: "Severance: 2 downloaded · 1 not found yet"."""
    got, missed = _got(report), len(report.not_found)
    parts: list[str] = []
    if got:
        parts.append(f"{got} downloaded")
    if missed:
        parts.append(f"{missed} not found yet")
    if report.left_over:
        parts.append(f"{report.left_over} more missing: download missing again for those")
    if report.error is not None:
        parts.append(f"stopped: {report.error}")
    if not parts:
        parts.append("nothing missing")
    return f"{show_name}: " + " · ".join(parts)


def have_status_text(show_name: str, count: int, *, have: bool) -> str:
    """The status bar after "I already have this" / "Mark as missing again"."""
    episodes = "1 episode" if count == 1 else f"{count} episodes"
    if have:
        return f"{show_name}: {episodes} marked as already had"
    return f"{show_name}: {episodes} marked as missing again"


# -- run states --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MissingRunState:
    """A show's "Download missing" run: working on episode `index` of `total` (0 = not
    started yet), or finished with `report`."""

    index: int = 0
    total: int = 0
    report: MissingRunReport | None = None

    @property
    def working(self) -> bool:
        return self.report is None


def missing_look(count: int, state: MissingRunState | None, *, with_count: bool) -> Look | None:
    """How a "Download missing" button looks for the show's `count` missing episodes and its
    run state; None = not shown (nothing missing and no run to tell about).

    A run working or finished wins; a finished run's button starts another run while
    episodes are still missing."""
    action: Action | None = "download" if count > 0 else None
    if state is not None and state.report is None:
        index_texts = progress_texts(state.index, state.total)
        return Look("label", index_texts[0], text_color=theme.TEXT_MUTED, short=index_texts[1:])
    if state is not None and state.report is not None:
        report = state.report
        text, *short = outcome_texts(report)
        if report.error is not None:
            return Look(
                "button", text, "transparent", theme.CHIP_HOVER, theme.DANGER,
                action=action, short=tuple(short), border=theme.DANGER,
            )
        if _got(report):
            return Look(
                "button", text, theme.GRABBED, theme.ACCENT_HOVER, theme.TEXT,
                action=action, short=tuple(short),
            )
        return Look(
            "button", text, "transparent", theme.CHIP_HOVER, theme.PENDING,
            action=action, short=tuple(short), border=theme.PENDING,
        )
    if count <= 0:
        return None
    return Look(
        "button",
        download_missing_text(count if with_count else None),
        action="download",
        short=DOWNLOAD_MISSING_SHORT,
    )


class MissingRuns:
    """The "Download missing" run state of every show, and the buttons showing them."""

    def __init__(self) -> None:
        self._states: dict[int, MissingRunState] = {}
        self._buttons: dict[int, list[MissingButton]] = {}

    def get(self, show_id: int) -> MissingRunState | None:
        return self._states.get(show_id)

    def is_working(self, show_id: int) -> bool:
        state = self._states.get(show_id)
        return state is not None and state.working

    def set(self, show_id: int, state: MissingRunState | None) -> None:
        """Record `state` (None: back to the buttons' own look) and repaint the show's buttons."""
        if state is None:
            self._states.pop(show_id, None)
        else:
            self._states[show_id] = state
        for button in self._live(show_id):
            button.repaint()

    def expire(self, show_id: int, state: MissingRunState) -> None:
        """Forget a finished run's outcome, unless another run has replaced it meanwhile."""
        if self._states.get(show_id) is state:
            self.set(show_id, None)

    def attach(self, button: MissingButton) -> None:
        self._live(button.show_id).append(button)

    def detach(self, button: MissingButton) -> None:
        buttons = self._buttons.get(button.show_id, [])
        if button in buttons:
            buttons.remove(button)

    def _live(self, show_id: int) -> list[MissingButton]:
        """The show's buttons whose widgets still exist (cards go when a page reloads)."""
        live = [button for button in self._buttons.get(show_id, []) if _exists(button)]
        self._buttons = {k: v for k, v in self._buttons.items() if v and k != show_id}
        self._buttons[show_id] = live
        return live


def _exists(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole window is gone
        return False


@dataclass(frozen=True, slots=True)
class MissingActions:
    """What the missing-episode controls do (the main window provides them)."""

    download: Callable[[Show], None]  # start "Download missing" for the show
    mark_have: Callable[[Show, tuple[int, ...]], None]  # "I already have this" (episode ids)
    unmark_have: Callable[[Show, tuple[int, ...]], None]  # "Mark as missing again"
    runs: MissingRuns


# -- widgets -----------------------------------------------------------------------------------


class MissingButton(ctk.CTkFrame):
    """The "⬇ Download missing" button of one show; while a run works, "Downloading 2 of 3…";
    then the run's outcome. Blank (but keeping its place) while nothing is missing."""

    def __init__(
        self,
        master: Any,
        *,
        show: Show,
        count: int,
        actions: MissingActions,
        with_count: bool,
        height: int,
        font_size: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(master, fg_color="transparent", height=height, **kwargs)
        self.pack_propagate(False)
        self.show_id = show.id
        self._show = show
        self._count = count
        self._actions = actions
        self._with_count = with_count
        self._look: Look | None = None
        self._painted = False
        self._width: float | None = None  # unscaled, once laid out
        self._font = theme.font(font_size, bold=True)
        self._label_font = theme.font(font_size)
        self._button = ctk.CTkButton(
            self,
            text="",
            height=height,
            corner_radius=CORNER_RADIUS,
            font=self._font,
            border_width=0,
            command=self._click,
        )
        self._label = ctk.CTkLabel(
            self, text="", height=height, font=self._label_font, text_color=theme.TEXT_MUTED
        )
        self._tip = add_tooltip(self._button, "")
        self._label_tip = add_tooltip(self._label, "")
        self.bind("<Configure>", self._on_resize, add="+")
        actions.runs.attach(self)
        self.repaint()

    @property
    def look(self) -> Look | None:
        return self._look

    def set_count(self, count: int) -> None:
        if count != self._count:
            self._count = count
            self.repaint()

    def repaint(self) -> None:
        look = missing_look(
            self._count, self._actions.runs.get(self.show_id), with_count=self._with_count
        )
        if self._painted and look == self._look:
            return
        previous, self._look, self._painted = self._look, look, True
        if previous is None or look is None or previous.layout != look.layout:
            self._button.pack_forget()
            self._label.pack_forget()
            if look is not None:
                widget = self._label if look.layout == "label" else self._button
                widget.pack(fill="both", expand=True)
        if look is not None and look.layout == "button":
            text_color = look.text_color or theme.TEXT
            self._button.configure(
                fg_color=look.color,
                hover_color=look.hover,
                text_color=text_color,
                text_color_disabled=text_color,
                border_width=1 if look.border else 0,
                border_color=look.border or theme.EDGE,
                state="normal" if look.action else "disabled",
            )
        self._fit_text()

    def _on_resize(self, event: Any) -> None:
        if event.width > 1:
            self._width = unscaled_width(self, event.width)
            self._fit_text()

    def _fit_text(self) -> None:
        look = self._look
        if look is None:
            return
        text = look.text
        if look.layout == "label":
            if self._width is not None:
                room = self._width - 2 * CORNER_RADIUS - SLACK  # the label's own padding
                text = fit_text(look.texts, room, self._label_font.measure)
            self._label.configure(text=text)
            self._label_tip.set_text(look.text if text != look.text else "")
            return
        if self._width is not None:
            room = self._width - 2 * CORNER_RADIUS - SLACK
            text = fit_text(look.texts, room, self._font.measure)
        self._button.configure(text=text)
        self._tip.set_text(look.text if text != look.text else "")

    def _click(self) -> None:
        look = self._look
        if look is not None and look.action is not None:
            self._actions.download(self._show)

    def destroy(self) -> None:
        self._actions.runs.detach(self)
        super().destroy()


def chip(
    master: Any,
    text: str,
    color: str,
    *,
    height: int,
    font_size: int,
    width: int = 0,
    clickable: bool = False,
) -> ctk.CTkButton:
    """A small outlined pill ("3 missing", "Missing"); a `clickable` one gets its command
    configured by the caller."""
    button = ctk.CTkButton(
        master,
        text=text,
        width=width,
        height=height,
        corner_radius=height // 2,
        font=theme.font(font_size, bold=True),
        fg_color="transparent",
        hover_color=theme.CHIP_HOVER,
        hover=clickable,
        border_width=1,
        border_color=color,
        text_color=color,
        text_color_disabled=color,
        state="normal" if clickable else "disabled",
    )
    if clickable:
        button.configure(cursor="hand2")
    return button


MenuItem = tuple[str, Callable[[], None]]

MENU_ITEM_HEIGHT = 34
MENU_FONT_SIZE = 14
MENU_MIN_WIDTH = 200


def menu_position(
    anchor: tuple[int, int, int, int],
    size: tuple[int, int],
    bounds: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Where a popup menu of `size` goes for an `anchor` box (x, y, width, height): below it,
    right edges aligned, kept within `bounds` (x, y, width, height: the anchor's window);
    above the anchor when there is no room below. All in screen pixels."""
    ax, ay, aw, ah = anchor
    width, height = size
    bx, by, bw, bh = bounds
    x = max(bx, min(ax + aw - width, bx + bw - width))
    y = ay + ah + 2
    if y + height > by + bh and ay - height - 2 >= by:
        y = ay - height - 2
    return x, y


class PopupMenu:
    """A small dark menu of items under a widget, drawn like the rest of the app (Tk's own
    menus ignore the colours on Windows). Closes on a choice, Escape, a click elsewhere or
    when its anchor goes. One at a time."""

    _open: PopupMenu | None = None

    def __init__(self, anchor: tk.Misc, items: Sequence[MenuItem]) -> None:
        if PopupMenu._open is not None:
            PopupMenu._open.close()
        PopupMenu._open = self
        self._closed = False
        self.labels = tuple(label for label, _ in items)
        top = self._top = tk.Toplevel(anchor.winfo_toplevel(), bg=theme.EDGE)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        box = ctk.CTkFrame(top, fg_color=theme.SURFACE, corner_radius=0)
        box.pack(fill="both", expand=True, padx=1, pady=1)
        self.buttons: list[ctk.CTkButton] = []
        for label, action in items:
            button = ctk.CTkButton(
                box,
                text=label,
                width=MENU_MIN_WIDTH,
                height=MENU_ITEM_HEIGHT,
                corner_radius=0,
                anchor="w",
                font=theme.font(MENU_FONT_SIZE),
                fg_color="transparent",
                hover_color=theme.ACCENT,
                text_color=theme.TEXT,
                command=lambda action=action: self._choose(action),
            )
            button.pack(fill="x", padx=4, pady=(4 if not self.buttons else 0, 0))
            self.buttons.append(button)
        tk.Frame(box, height=4, bg=theme.SURFACE).pack()  # bottom padding
        top.bind("<Escape>", lambda _event: self.close())
        top.bind("<FocusOut>", self._on_focus_out)
        self._anchor = anchor
        self._anchor_binding = tk.Misc.bind(anchor, "<Destroy>", self._on_anchor_gone, add="+")
        top.update_idletasks()
        # The anchor's window bounds the menu: winfo_* are all in the same (physical) pixels,
        # unlike wm_maxsize() of a new window.
        window = anchor.winfo_toplevel()
        x, y = menu_position(
            (anchor.winfo_rootx(), anchor.winfo_rooty(), anchor.winfo_width(), anchor.winfo_height()),
            (top.winfo_reqwidth(), top.winfo_reqheight()),
            (window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height()),
        )
        top.geometry(f"+{x}+{y}")
        top.deiconify()
        top.lift()
        top.focus_force()

    @property
    def is_open(self) -> bool:
        return not self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if PopupMenu._open is self:
            PopupMenu._open = None
        try:
            tk.Misc.unbind(self._anchor, "<Destroy>", self._anchor_binding)
        except tk.TclError:
            pass  # the anchor is gone
        try:
            self._top.destroy()
        except tk.TclError:
            pass

    def _choose(self, action: Callable[[], None]) -> None:
        self.close()
        action()

    def _on_anchor_gone(self, event: Any) -> None:
        if event.widget is self._anchor:
            self.close()

    def _on_focus_out(self, _event: Any) -> None:
        # Focus moving between the menu's own parts is no leaving: check once it has settled.
        def check() -> None:
            if self._closed:
                return
            try:
                focus = self._top.focus_get()
            except (tk.TclError, KeyError):
                focus = None
            if focus is None or focus.winfo_toplevel() is not self._top:
                self.close()

        try:
            # On the root: a job of the menu's own window would outlive it as a dead command.
            self._top.nametowidget(".").after(50, check)
        except tk.TclError:
            self.close()


def popup_menu(anchor: tk.Misc, items: Sequence[MenuItem]) -> PopupMenu | None:
    """A small dark menu of `items` (label, action) under `anchor`; none without items."""
    if not items:
        return None
    return PopupMenu(anchor, items)
