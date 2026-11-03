"""Missing episodes in the UI: the amber "3 missing", "Download missing"'s texts and the run
states behind it, an episode's quiet mark ("● Missing", "● New", a grey ✓: `mark_label`) and
the small option menus ("I already have this").

The main window runs "Download missing" (one run per show at a time; since 3.0.1 its episodes
go into the download queue as one batch, `queued_run_report` sums up how it went) and keeps
each show's run state in a `MissingRuns`; every My shows card showing a show attaches to it,
so a card rebuilt mid-run (pages reload after every grab) picks the run's state up, and a
finished run repaints whichever cards show the show by then. Which episodes are missing is
decided in app.services.missing.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import customtkinter as ctk
from PIL import Image

from app.i18n import N_, t, tn
from app.models import MissingRunReport, QueueEvent, QueueState, Show
from app.ui import theme
from app.ui.borderless import disc_image
from app.ui.tooltip import add_tooltip

MISSING_COLOR = theme.WARNING  # amber: something is missing rather than broken
OUTCOME_MS = 60_000  # a finished run's outcome stays on its button this long
# An episode's mark (CTk units): a small dot, then its text.
MARK_FONT_SIZE = 13
MARK_DOT = 8
MARK_DOT_GAP = 6

# The texts are shown through t().
DOWNLOAD_MISSING_TEXT = N_("⬇ Download missing")
STARTING_TEXT = N_("Finding missing episodes…")
STARTING_SHORT = N_("Starting…")
FAILED_TEXT = N_("Couldn't download")
NOTHING_MISSING_TEXT = N_("Nothing missing")
CHIP_MISSING_TEXT = N_("Missing")
HAVE_MARK = "✓"  # an episode marked "I already have this": a quiet ✓, no text
HAVE_TIP = N_("You have this")
CHIP_TIP = N_("Options")
HAVE_EPISODE_TEXT = N_("I already have this")
HAVE_SEASON_TEXT = N_("I already have this season")
MARK_MISSING_TEXT = N_("Mark as missing again")
SEASON_MENU_TIP = N_("More options")


# -- texts -------------------------------------------------------------------------------------


def missing_text(count: int) -> str:
    """"3 missing"."""
    return t("{n} missing", n=count)


def download_missing_text(count: int | None = None) -> str:
    """"⬇ Download missing (3)", or without the count "⬇ Download missing"."""
    text = t(DOWNLOAD_MISSING_TEXT)
    return text if count is None else f"{text} ({count})"


def progress_texts(index: int, total: int) -> tuple[str, ...]:
    """"Downloading 2 of 3…" and shorter forms; before the first episode starts, "Finding
    missing episodes…"."""
    if index <= 0 or total <= 0:
        return t(STARTING_TEXT), t(STARTING_SHORT)
    return (
        t("Downloading {index} of {total}…", index=index, total=total),
        t("Getting {index} of {total}…", index=index, total=total),
        t("{index} of {total}…", index=index, total=total),
    )


def _got(report: MissingRunReport) -> int:
    """Episodes the run leaves downloaded: grabbed now, or found grabbed meanwhile."""
    return len(report.downloaded) + len(report.already)


def outcome_texts(report: MissingRunReport) -> tuple[str, ...]:
    """A finished run in plain words, longest first: "✓ 2 downloaded · 1 not found yet"."""
    got, missed = _got(report), len(report.not_found)
    if report.error is not None:
        if got:
            return (
                t("✓ {got} downloaded · stopped", got=got),
                t("✓ {got} · stopped", got=got),
                t("Stopped"),
            )
        return t(FAILED_TEXT), t("Failed")
    if got and missed:
        return (
            t("✓ {got} downloaded · {missed} not found yet", got=got, missed=missed),
            t("✓ {got} downloaded · {missed} not found", got=got, missed=missed),
            t("✓ {got} · {missed} not found", got=got, missed=missed),
            t("✓ {got} downloaded", got=got),
        )
    if got:
        return t("✓ {got} downloaded", got=got), f"✓ {got}"
    if missed:
        return t("{missed} not found yet", missed=missed), t("Not found yet")
    return (t(NOTHING_MISSING_TEXT),)


def outcome_status_text(show_name: str, report: MissingRunReport) -> str:
    """The status bar after a run: "Severance: 2 downloaded · 1 not found yet"."""
    got, missed = _got(report), len(report.not_found)
    parts: list[str] = []
    if got:
        parts.append(t("{got} downloaded", got=got))
    if missed:
        parts.append(t("{missed} not found yet", missed=missed))
    if report.left_over:
        parts.append(
            t(
                "{n} more missing: download missing again for those",
                n=report.left_over,
            )
        )
    if report.error is not None:
        parts.append(t("stopped: {error}", error=report.error))
    if not parts:
        parts.append(t("nothing missing"))
    return f"{show_name}: " + " · ".join(parts)


def have_status_text(show_name: str, count: int, *, have: bool) -> str:
    """The status bar after "I already have this" / "Mark as missing again"."""
    episodes = tn("{n} episode", "{n} episodes", count)
    if have:
        return t("{show}: {episodes} marked as already had", show=show_name, episodes=episodes)
    return t("{show}: {episodes} marked as missing again", show=show_name, episodes=episodes)


# -- run states --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MissingRunState:
    """A show's "Download missing" run: working on episode `index` of `total` (0 = not
    started yet), waiting in the download queue (`queued`), or finished with `report`."""

    index: int = 0
    total: int = 0
    report: MissingRunReport | None = None
    queued: bool = False

    @property
    def working(self) -> bool:
        return self.report is None


def queued_run_report(
    show_id: int, total: int, events: Sequence[QueueEvent], left_over: int = 0
) -> MissingRunReport:
    """What a "Download missing" run did, from its episodes' last events in the download
    queue: grabbed, downloaded already, nothing good enough yet; a failed grab's reason ends
    it (the queue dropped the rest of the batch). `left_over`: missing beyond the run's cap."""
    downloaded: list[str] = []
    already: list[str] = []
    not_found: list[str] = []
    error: str | None = None
    for event in events:
        if event.state is QueueState.DONE:
            grabbed = event.result is not None and event.result.grab is not None
            (downloaded if grabbed else already).append(event.label)
        elif event.state is QueueState.NOTHING:
            not_found.append(event.label)
        elif event.state is QueueState.FAILED and error is None:
            error = event.message
    return MissingRunReport(
        show_id=show_id,
        total=total,
        downloaded=tuple(downloaded),
        already=tuple(already),
        not_found=tuple(not_found),
        left_over=left_over,
        error=error,
    )


class RunView(Protocol):
    """What shows a show's run state and follows it: a My shows card."""

    @property
    def show_id(self) -> int: ...

    def repaint(self) -> None: ...

    def winfo_exists(self) -> bool: ...


class MissingRuns:
    """The "Download missing" run state of every show, and the views (My shows cards) showing
    them."""

    def __init__(self) -> None:
        self._states: dict[int, MissingRunState] = {}
        self._buttons: dict[int, list[RunView]] = {}

    def get(self, show_id: int) -> MissingRunState | None:
        return self._states.get(show_id)

    def is_working(self, show_id: int) -> bool:
        state = self._states.get(show_id)
        return state is not None and state.working

    def set(self, show_id: int, state: MissingRunState | None) -> None:
        """Record `state` (None: back to the views' own look) and repaint the show's views."""
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

    def attach(self, button: RunView) -> None:
        self._live(button.show_id).append(button)

    def detach(self, button: RunView) -> None:
        buttons = self._buttons.get(button.show_id, [])
        if button in buttons:
            buttons.remove(button)

    def _live(self, show_id: int) -> list[RunView]:
        """The show's views whose widgets still exist (cards go when a page reloads)."""
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


def mark_dot(master: tk.Misc, color: theme.Color) -> ctk.CTkImage:
    """A MARK_DOT disc of `color` (light and dark), baked at the screen's scaling, with
    MARK_DOT_GAP see-through room right of it (the gap to the text beside it)."""
    scaling: float = ctk.ScalingTracker.get_widget_scaling(master)
    side = max(1, round(MARK_DOT * scaling))
    room = round((MARK_DOT + MARK_DOT_GAP) * scaling)

    def baked(dark: bool) -> Image.Image:
        red, green, blue = master.winfo_rgb(theme.pick(color, dark=dark))
        out = Image.new("RGBA", (max(side, room), side), (0, 0, 0, 0))
        out.paste(disc_image(side, (red >> 8, green >> 8, blue >> 8)), (0, 0))
        return out

    return ctk.CTkImage(
        light_image=baked(False), dark_image=baked(True), size=(MARK_DOT + MARK_DOT_GAP, MARK_DOT)
    )


def mark_label(
    master: Any, text: str, color: theme.Color, *, dot: bool, height: int
) -> ctk.CTkLabel:
    """A quiet episode mark, no box: `text` in `color`, after a small dot of that colour with
    `dot` ("● Missing", "● New"), else alone (the ✓ of "I already have this")."""
    return ctk.CTkLabel(
        master,
        text=text,
        image=mark_dot(master, color) if dot else None,
        compound="left",
        font=theme.font(MARK_FONT_SIZE),
        text_color=color,
        height=height,
        fg_color="transparent",
        anchor="w",
    )


MenuItem = tuple[str, Callable[[], None]]


@dataclass(frozen=True, slots=True)
class MenuEntry:
    """A popup menu item that can be greyed out: `action` None = not available now, `tip`
    says why (or what it does). `image`: a picture before the text (a CTkImage: a person's
    avatar); `checked`: a ✓ at the item's right end (the person selected); `separator`: a line
    between items instead of an item (MENU_SEPARATOR); `danger`: red text (a destructive item,
    such as the Downloads page's "🗑 Remove from Download Station…")."""

    label: str
    action: Callable[[], None] | None
    tip: str = ""
    image: Any = None
    checked: bool = False
    separator: bool = False
    danger: bool = False


MENU_SEPARATOR = MenuEntry("", None, separator=True)


def _entry(item: MenuItem | MenuEntry) -> MenuEntry:
    return item if isinstance(item, MenuEntry) else MenuEntry(item[0], item[1])

MENU_ITEM_HEIGHT = 34
MENU_FONT_SIZE = 14
MENU_MIN_WIDTH = 200
MENU_SEPARATOR_PAD = 8  # a separator line's room left and right
MENU_SEPARATOR_GAP = 5  # … above and under it
MENU_CHECK_WIDTH = 18  # a ✓ at an item's right end
MENU_CHECK_PAD = 10


def menu_position(
    anchor: tuple[int, int, int, int],
    size: tuple[int, int],
    bounds: tuple[int, int, int, int],
    *,
    align: Literal["left", "right"] = "right",
) -> tuple[int, int]:
    """Where a popup menu of `size` goes for an `anchor` box (x, y, width, height): below it,
    right edges aligned (left edges with `align="left"`), kept within `bounds` (x, y, width,
    height: the anchor's window); above the anchor when there is no room below. All in screen
    pixels."""
    ax, ay, aw, ah = anchor
    width, height = size
    bx, by, bw, bh = bounds
    edge = ax if align == "left" else ax + aw - width
    x = max(bx, min(edge, bx + bw - width))
    y = ay + ah + 2
    if y + height > by + bh and ay - height - 2 >= by:
        y = ay - height - 2
    return x, y


def point_position(
    point: tuple[int, int], size: tuple[int, int], bounds: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Where a right-click menu of `size` goes for the pointer at `point`: its top-left corner
    there, left of / above the pointer where it would leave `bounds` (x, y, width, height).
    All in screen pixels."""
    px, py = point
    width, height = size
    bx, by, bw, bh = bounds
    x = px if px + width <= bx + bw else max(bx, px - width)
    y = py if py + height <= by + bh else max(by, py - height)
    return x, y


class PopupMenu:
    """A small menu of items under a widget, drawn like the rest of the app (Tk's own menus
    ignore the colours on Windows). Closes on a choice, Escape, a click elsewhere, when its
    anchor goes or when the app switches between light and dark. One at a time."""

    _open: PopupMenu | None = None

    def __init__(
        self,
        anchor: tk.Misc,
        items: Sequence[MenuItem | MenuEntry],
        *,
        point: tuple[int, int] | None = None,
        under: tuple[int, int, int, int] | None = None,
        look: theme.Look | None = None,
        align: Literal["left", "right"] = "right",
    ) -> None:
        """`point`: open at the pointer (screen pixels) instead of under `anchor`. `under`: open
        under this part of `anchor` instead (x, y, width, height in screen pixels: a button
        drawn on a canvas). `look`: a window's own accent tokens (the Forge window's copper)
        instead of AirGrabber's. `align`: which edges line up with what it opens under."""
        edge = theme.EDGE if look is None else look.EDGE
        surface = theme.SURFACE if look is None else look.SURFACE
        menu_hover = theme.MENU_HOVER if look is None else look.MENU_HOVER
        if PopupMenu._open is not None:
            PopupMenu._open.close()
        PopupMenu._open = self
        self._closed = False
        entries = [_entry(item) for item in items]
        self.labels = tuple(entry.label for entry in entries if not entry.separator)
        self.checks: list[ctk.CTkLabel] = []  # the ✓ marks
        top = self._top = tk.Toplevel(anchor.winfo_toplevel(), bg=theme.pick(edge))
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        box = ctk.CTkFrame(top, fg_color=surface, corner_radius=0)
        box.pack(fill="both", expand=True, padx=1, pady=1)
        self.buttons: list[ctk.CTkButton] = []
        first = True
        for entry in entries:
            if entry.separator:
                tk.Frame(box, height=1, bg=theme.pick(edge)).pack(
                    fill="x", padx=MENU_SEPARATOR_PAD, pady=MENU_SEPARATOR_GAP
                )
                continue
            action = entry.action
            button = ctk.CTkButton(
                box,
                text=entry.label,
                image=entry.image,
                compound="left",
                width=MENU_MIN_WIDTH,
                height=MENU_ITEM_HEIGHT,
                corner_radius=0,
                anchor="w",
                font=theme.font(MENU_FONT_SIZE),
                fg_color="transparent",
                hover_color=menu_hover,
                text_color=theme.DANGER if entry.danger else theme.TEXT,
                text_color_disabled=theme.TEXT_FAINT,
                state="normal" if action is not None else "disabled",
                command=(lambda action=action: self._choose(action)) if action else None,
            )
            button.pack(fill="x", padx=4, pady=(4 if first else 0, 0))
            first = False
            if entry.checked:
                self._check(button, action)
            if entry.tip:
                add_tooltip(button, entry.tip)
            self.buttons.append(button)
        tk.Frame(box, height=4, bg=theme.pick(surface)).pack()  # bottom padding
        # Closed once the repaint is over: destroying it now would upset CTk's list of widgets
        # being repainted.
        theme.on_mode_change(top, lambda: top.nametowidget(".").after_idle(self.close))
        top.bind("<Escape>", lambda _event: self.close())
        top.bind("<FocusOut>", self._on_focus_out)
        self._anchor = anchor
        self._anchor_binding = tk.Misc.bind(anchor, "<Destroy>", self._on_anchor_gone, add="+")
        top.update_idletasks()
        # The anchor's window bounds the menu: winfo_* are all in the same (physical) pixels,
        # unlike wm_maxsize() of a new window.
        window = anchor.winfo_toplevel()
        size = (top.winfo_reqwidth(), top.winfo_reqheight())
        bounds = (window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height())
        if point is not None:
            x, y = point_position(point, size, bounds)
        else:
            if under is None:
                under = (
                    anchor.winfo_rootx(),
                    anchor.winfo_rooty(),
                    anchor.winfo_width(),
                    anchor.winfo_height(),
                )
            x, y = menu_position(under, size, bounds, align=align)
        top.geometry(f"+{x}+{y}")
        top.deiconify()
        top.lift()
        top.focus_force()

    @property
    def is_open(self) -> bool:
        return not self._closed

    def _check(self, button: ctk.CTkButton, action: Callable[[], None] | None) -> None:
        """A ✓ at `button`'s right end (a click on it is a click on the item)."""
        mark = ctk.CTkLabel(
            button,
            text="✓",
            font=theme.font(MENU_FONT_SIZE, bold=True),
            text_color=theme.LINK,
            fg_color="transparent",
            width=MENU_CHECK_WIDTH,
        )
        mark.place(relx=1.0, rely=0.5, x=-MENU_CHECK_PAD, anchor="e")
        if action is not None:
            mark.bind("<Button-1>", lambda _event: self._choose(action), add="+")
        self.checks.append(mark)

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


def popup_menu(
    anchor: tk.Misc,
    items: Sequence[MenuItem | MenuEntry],
    *,
    point: tuple[int, int] | None = None,
    under: tuple[int, int, int, int] | None = None,
    look: theme.Look | None = None,
    align: Literal["left", "right"] = "right",
) -> PopupMenu | None:
    """A small menu of `items` ((label, action) or a MenuEntry, which may be greyed out with a
    tip) under `anchor` (or `under` a part drawn on it), or at the pointer's `point`;
    none without items. `look`: a window's own accent tokens (the Forge window's copper)
    instead of AirGrabber's. `align`: right edges lined up (the default) or left edges."""
    if not items:
        return None
    return PopupMenu(anchor, items, point=point, under=under, look=look, align=align)
