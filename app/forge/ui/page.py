"""MediaForge's page in AirGrabber's main window (docs/forge/PAGE.md, the sketch
mockups/forge-as-a-page.html). AirGrabber's main window (`Mode.FORGE`) shows it; `ForgeHost`
(app/forge/ui/host.py) makes it at the first visit and fills it.

Top to bottom, under the band's top row:
1. The main window's tabs row, whose left part is the page's (`tabs_row`; the switch stays at its
   right end). With the queue: + Files, + Folder (the queue's own `toolbar`) and the status line,
   the renamer's light and words (`WatchStatus`, a quiet "Auto-rename off" while off; cut to fit; a
   click opens Settings at Auto-rename, `on_settings`). With History: the heading, its muted line
   and the Activity button.
2. The queue (QueueView: the preset card, the filters with Clear queue, Preview and Forge, the
   cards) or History (HistoryView) in the same place; 🕘 in the band switches them
   (`toggle_history`), MediaForge in the switch goes back to the queue (`show_view`).

The Activity panel opens only from History's button, over the top right of the page; ✕ or Escape
closes it, and so does the page changing. Escape is bound on the main window, so it acts only while
the panel shows and never while the focus is in a text field.

Built in steps (F-33): `show_forge(services)` builds the queue, History comes a later turn or at
its first opening (`pages_built`). Until Forge is there the page has one plain frame instead:
`show_getting_ready()` or `show_problem(text, on_try_again)`.

Also what the first visit starts once per run in an installed copy: the "MediaForge" Start-menu
shortcut for an AirGrabber that updated from before it had one (`make_shortcut_once`).

Light at import: the views and Forge's core are imported when they are first built (Forge's
services have started by then).
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk
from PIL import Image, ImageDraw

from app.forge.ui.look import Pair, app_look, light_colour
from app.forge.ui.widgets.chips import fit_text
from app.i18n import N_, t
from app.services.updates import is_development_copy
from app.ui import palettes, tasks, theme
from app.ui.dialog_kit import primary_button

if TYPE_CHECKING:
    from app.forge.core.auto import WatchStatus
    from app.forge.services import ForgeServices
    from app.forge.ui.activity_panel import ActivityPanel
    from app.forge.ui.history_view import HistoryView
    from app.forge.ui.queue_view import QueueView

log = logging.getLogger(__name__)

type View = Literal["queue", "history"]

AUTO_OFF = N_("Auto-rename off")  # the status line while renaming on its own is off
HISTORY = N_("History")
ACTIVITY = N_("Activity")
TRY_AGAIN = N_("Try again")
GETTING_READY = N_("Getting ready…")

# CTk units.
TOOLS_PAD = 15  # the tabs row's left part from the window's edge, as the Series tabs
STATUS_GAP = 14  # + Folder → the status line
STATUS_SIZE = 13
STATUS_HEIGHT = 30
LIGHT_SIZE = 10  # the status line's light
STATUS_ROOM = 72  # the status button around its words: its rounded ends, the light, the spaces
SWITCH_GAP = 16  # the status line → the switch
HEADING_SIZE = 17
MUTED_SIZE = 12
MESSAGE_WRAP = 520
HISTORY_RETRY_MS = 250  # an undo in History: ask again this often before closing
LATER_STEP_MS = 30  # the first visit's later building steps: Tk draws and takes a click between
PANEL_RIGHT = 8  # the Activity panel from the page's right edge
PANEL_TOP = 6  # … and top
PANEL_HEIGHT = 0.7  # of the page
_TYPING_CLASSES = ("Entry", "Text", "TEntry")  # Escape there isn't for the panel


def light_dot(colour: Pair, size: int = LIGHT_SIZE) -> ctk.CTkImage:
    """The status line's light: a round dot with a soft glow, in `colour`'s light and dark shade
    (a CTkImage follows the look by itself). A tiny picture, made on the loop."""

    def draw(fill: str) -> Image.Image:
        scale = 4  # drawn large, scaled down by CTkImage: smooth edges
        side = size * scale
        image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        red, green, blue = (int(fill[i : i + 2], 16) for i in (1, 3, 5))
        draw_on = ImageDraw.Draw(image)
        for reach, alpha in ((0, 60), (side // 10, 110)):
            box = (reach, reach, side - reach, side - reach)
            draw_on.ellipse(box, fill=(red, green, blue, alpha))
        inner = side // 4
        draw_on.ellipse((inner, inner, side - inner, side - inner), fill=(red, green, blue, 255))
        return image

    light, dark = (palettes.to_hex(c) for c in colour)
    return ctk.CTkImage(light_image=draw(light), dark_image=draw(dark), size=(size, size))


_shortcut_checked = False


def make_shortcut_once(install_dir: Path | None = None) -> bool:
    """Start `shortcut.ensure_start_menu_shortcut` the first time the page is made in this run,
    only in an installed copy on Windows (it never raises). False: nothing to do."""
    from app.forge import shortcut

    global _shortcut_checked
    folder = install_dir if install_dir is not None else shortcut.INSTALL_DIR
    if _shortcut_checked or sys.platform != "win32" or is_development_copy(folder):
        return False
    _shortcut_checked = True
    tasks.spawn(
        shortcut.ensure_start_menu_shortcut(install_dir=folder), name="forge-start-menu-shortcut"
    )
    return True


class ForgePage(ctk.CTkFrame):
    """`ForgePage(master, tabs_row=, look=)`; then `show_forge(services)` or `show_problem`. The
    host sets `on_settings(section)`, `on_needs(count)` (the queue's "needs you"), `on_view(history)`
    (🕘 lit or not) and `on_message(text)` (what the queue says, for the status bar), and tells it
    `set_shown` when the main window shows it or another page."""

    def __init__(self, master: Any, *, tabs_row: Any, look: theme.Look | None = None) -> None:
        self.look: theme.Look = app_look() if look is None else look
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self._tabs_row = tabs_row
        self.services: ForgeServices | None = None
        self.on_settings: Callable[..., object] = lambda section, spot=None: None
        self.on_needs: Callable[[int], None] = lambda count: None
        self.on_view: Callable[[bool], None] = lambda history: None
        self.on_message: Callable[[str], None] = lambda text: None
        self.queue: QueueView | None = None
        self.history: HistoryView | None = None
        self.activity: ActivityPanel | None = None
        self.view: View = "queue"
        self.shown = False
        self.message: ctk.CTkFrame | None = None  # the plain frame while Forge isn't there
        self.message_label: ctk.CTkLabel | None = None
        self.try_again_button: ctk.CTkButton | None = None
        self.queue_tools: ctk.CTkFrame | None = None  # the tabs row's left part with the queue
        self.history_tools: ctk.CTkFrame | None = None  # … with History
        self.status_button: ctk.CTkButton | None = None
        self.activity_button: ctk.CTkButton | None = None
        self.status: WatchStatus | None = None
        self.status_text = ""  # the status line's words, whole (it may show them cut)
        self._status_font = theme.font(STATUS_SIZE)
        self._lights: dict[Pair, ctk.CTkImage] = {}
        self._later: list[Callable[[], None]] = []  # building steps still to come
        self._later_id: str | None = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._top = self.winfo_toplevel()
        self._escape_id = tk.Misc.bind(self._top, "<Escape>", self._escape, "+")
        self._row_id = tk.Misc.bind(self._tabs_row, "<Configure>", self._row_resized, "+")

    # --- building ---------------------------------------------------------------------------

    @staticmethod
    def load_views() -> None:
        """Import the queue, a step of its own in a first visit (F-33: Tk gets a turn after it)."""
        from app.forge.ui import queue_view  # noqa: F401

    def show_forge(self, services: ForgeServices, status: WatchStatus | None = None) -> None:
        """Build the queue on `services` (once; the plain frame goes); History comes later."""
        if self.services is not None:
            return
        from app.forge.ui.queue_view import QueueView

        self.services = services
        self._clear_message()
        self.queue_tools = ctk.CTkFrame(self._tabs_row, fg_color="transparent", corner_radius=0)
        queue = QueueView(self, services, look=self.look, tools_master=self.queue_tools)
        self.queue = queue
        queue.toolbar.pack(side="left")
        self.status_button = ctk.CTkButton(
            self.queue_tools,
            text="",
            width=28,
            height=STATUS_HEIGHT,
            corner_radius=STATUS_HEIGHT // 2,
            compound="left",
            fg_color="transparent",
            hover_color=self.look.CHIP,
            text_color=theme.TEXT_MUTED,
            font=self._status_font,
            command=self._status_clicked,
        )
        self.status_button.pack(side="left", padx=(STATUS_GAP, 0))
        queue.on_counts = lambda needs: self.on_needs(needs)
        queue.on_message = lambda text: self.on_message(text)
        queue.on_choose_folders = self._folders_clicked
        queue.refresh()
        self.set_watch_status(status or services.auto.status([]))
        self.show_view("queue")
        self._build_later(self._build_history)

    def _build_later(self, *steps: Callable[[], None]) -> None:
        self._later.extend(steps)
        if self._later_id is None:
            self._later_id = self.after(LATER_STEP_MS, self._next_later)

    def _next_later(self) -> None:
        self._later_id = None
        if not self._later or not self.winfo_exists():
            return
        step = self._later.pop(0)
        try:
            step()
        finally:
            if self._later:
                self._later_id = self.after(LATER_STEP_MS, self._next_later)

    @property
    def pages_built(self) -> bool:
        """The queue and History are both there (`show_forge` builds History in a later turn)."""
        return self.services is not None and not self._later and self._later_id is None

    def _build_history(self) -> None:
        if self.history is not None or self.services is None:
            return
        from app.forge.models.history import HISTORY_LIMIT
        from app.forge.ui.history_view import HistoryView

        self.history = HistoryView(self, self.services, look=self.look)
        look = self.look
        tools = ctk.CTkFrame(self._tabs_row, fg_color="transparent", corner_radius=0)
        ctk.CTkLabel(
            tools, text=t(HISTORY), font=theme.font(HEADING_SIZE, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            tools,
            text=t("your last {n} runs · newest first", n=HISTORY_LIMIT),
            font=theme.font(MUTED_SIZE),
            text_color=theme.TEXT_MUTED,
        ).pack(side="left", padx=(10, 0))
        self.activity_button = ctk.CTkButton(
            tools,
            text="☰  " + t(ACTIVITY),
            width=40,
            height=36,
            corner_radius=9,
            fg_color=look.SURFACE,
            border_width=1,
            border_color=look.CHIP_HOVER,
            text_color=theme.TEXT,
            hover_color=look.CHIP,
            font=theme.font(14, bold=True),
            command=self.toggle_activity,
        )
        self.activity_button.pack(side="left", padx=(STATUS_GAP, 0))
        self.history_tools = tools

    def show_problem(self, text: str, on_try_again: Callable[[], None] | None) -> None:
        """One plain frame instead of the queue: why Forge is off, and Try again."""
        if self.services is not None:
            return
        self._clear_message()
        frame = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        frame.grid(row=0, column=0, sticky="nsew")
        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.place(relx=0.5, rely=0.4, anchor="center")
        self.message_label = ctk.CTkLabel(
            inner,
            text=text,
            font=theme.font(15, bold=True),
            text_color=theme.TEXT,
            wraplength=MESSAGE_WRAP,
            justify="center",
        )
        self.message_label.pack()
        if on_try_again is not None:
            self.try_again_button = primary_button(
                inner, t(TRY_AGAIN), on_try_again, width=160, look=self.look
            )
            self.try_again_button.pack(pady=(18, 0))
        self.message = frame

    def show_getting_ready(self) -> None:
        """The plain frame with "Getting ready…" (no button): Forge starts, or its file-name
        reader warms up, before the queue is built (ForgeHost.open)."""
        self.show_problem(t(GETTING_READY), None)

    def _clear_message(self) -> None:
        if self.message is not None:
            self.message.destroy()
        self.message = None
        self.message_label = None
        self.try_again_button = None

    # --- what shows -------------------------------------------------------------------------

    def set_shown(self, shown: bool) -> None:
        """The main window shows this page, or another one: the tabs row's left part comes or
        goes, and Activity closes with the page."""
        self.shown = shown
        if not shown:
            self.hide_activity()
        self._place_tools()

    def _place_tools(self) -> None:
        """The tabs row's left part for what shows now (none on another page)."""
        wanted = self.history_tools if self.view == "history" else self.queue_tools
        for tools in (self.queue_tools, self.history_tools):
            if tools is not None and (tools is not wanted or not self.shown):
                tools.grid_remove()
        if wanted is not None and self.shown:
            wanted.grid(row=0, column=0, sticky="w", padx=(TOOLS_PAD, 0))
        self._fit_status()

    def show_view(self, view: View) -> None:
        """The queue or History in the page's place (History is built now if its turn hasn't
        come; it reads its runs when it shows). Activity closes."""
        queue = self.queue
        if queue is None:
            return
        if view == "history":
            self._build_history()
        if view != self.view:
            self.hide_activity()
        self.view = view
        history = self.history
        if view == "history" and history is not None:
            queue.grid_remove()
            history.grid(row=0, column=0, sticky="nsew")
        else:
            if history is not None:
                history.grid_remove()
            queue.grid(row=0, column=0, sticky="nsew")
        self._place_tools()
        self.on_view(view == "history")
        if view == "history" and self.history is not None:
            self.history.on_show()

    def toggle_history(self) -> None:
        """🕘: History, or back to the queue."""
        self.show_view("queue" if self.view == "history" else "history")

    def refresh_page(self) -> None:
        """What shows is read again (F5, an import)."""
        if self.view == "history" and self.history is not None:
            self.history.on_show()
        elif self.queue is not None:
            self.queue.refresh()

    # --- the Activity panel -----------------------------------------------------------------

    @property
    def activity_open(self) -> bool:
        panel = self.activity
        return panel is not None and bool(panel.winfo_manager())

    def toggle_activity(self) -> None:
        if self.activity_open:
            self.hide_activity()
        else:
            self.show_activity()

    def show_activity(self) -> None:
        """The Activity panel over the top right of the page (built the first time)."""
        if self.services is None:
            return
        if self.activity is None:
            from app.forge.ui.activity_panel import ActivityPanel

            self.activity = ActivityPanel(
                self, self.services.activity, look=self.look, on_close=self.hide_activity
            )
        self.activity.place(
            relx=1.0, x=-PANEL_RIGHT, y=PANEL_TOP, anchor="ne", relheight=PANEL_HEIGHT
        )
        self.activity.lift()
        self.activity.see_end()
        self._paint_activity_button(True)

    def hide_activity(self) -> None:
        if self.activity is not None and self.activity.winfo_manager():
            self.activity.place_forget()
            self._paint_activity_button(False)

    def _paint_activity_button(self, opened: bool) -> None:
        button, look = self.activity_button, self.look
        if button is not None:
            button.configure(
                fg_color=look.ROW_SELECTED if opened else look.SURFACE,
                border_color=look.ACCENT if opened else look.CHIP_HOVER,
            )

    def _escape(self, _event: tk.Event[Any]) -> str | None:
        """Escape on the main window: closes the panel while it shows; else not this page's."""
        if not self.activity_open or not self.winfo_viewable():
            return None
        focused = self.focus_get()
        if focused is not None and focused.winfo_class() in _TYPING_CLASSES:
            return None
        self.hide_activity()
        return "break"

    # --- the status line --------------------------------------------------------------------

    def set_watch_status(self, status: WatchStatus) -> None:
        """The renamer's status: the light and the words (a quiet "Auto-rename off" while off,
        but for a reason worth reading: the old MediaForge's guard)."""
        from app.forge.core.auto import OLD_INSTALL_STATUS

        if not self.winfo_exists():
            return
        self.status = status
        self.status_text = (
            t(AUTO_OFF)
            if status.state == "off" and status.text != t(OLD_INSTALL_STATUS)
            else status.text
        )
        button = self.status_button
        if button is None:
            return
        colour = light_colour(status.state)
        if colour not in self._lights:
            self._lights[colour] = light_dot(colour)
        button.configure(
            image=self._lights[colour],
            text_color=theme.TEXT_MUTED if status.state == "off" else theme.TEXT,
        )
        self._fit_status()

    @property
    def light(self) -> Pair:
        """The status line's light colour (muted while off)."""
        return light_colour(self.status.state if self.status is not None else "off")

    @property
    def status_shown(self) -> str:
        """The words the status line shows (cut to fit)."""
        button = self.status_button
        return "" if button is None else str(button.cget("text")).strip()

    def _row_resized(self, _event: tk.Event[Any]) -> None:
        self._fit_status()

    def _fit_status(self) -> None:
        """The status line's words, cut so they never run into the switch."""
        button, queue = self.status_button, self.queue
        if button is None or queue is None or not self.winfo_exists():
            return
        text = "  " + self.status_text
        row = self._tabs_row
        width = row.winfo_width()
        if width > 1:
            scale = float(ctk.ScalingTracker.get_widget_scaling(self)) or 1.0
            switch = max((s.winfo_reqwidth() for s in row.grid_slaves(column=2)), default=0)
            used = switch + queue.toolbar.winfo_reqwidth()
            pad = TOOLS_PAD + STATUS_GAP + SWITCH_GAP + STATUS_ROOM
            room = round((width - used) / scale) - pad
            text = "  " + fit_text(self.status_text, self._status_font, max(room, 0))
        if button.cget("text") != text:
            button.configure(text=text)

    def _folders_clicked(self) -> None:
        from app.forge.ui.settings_pages import LIBRARY_FOLDERS_SPOT, SECTION_LIBRARY

        self.on_settings(SECTION_LIBRARY, LIBRARY_FOLDERS_SPOT)

    def _status_clicked(self) -> None:
        from app.forge.ui.settings_pages import SECTION_LIBRARY

        self.on_settings(SECTION_LIBRARY)

    # --- what the host asks -----------------------------------------------------------------

    def add_paths(self, paths: Sequence[Path], *, preset: str | None = None) -> None:
        """Queue files and folders (drops, right-click "Open in MediaForge"; `preset`: the
        request's, for the files it adds)."""
        if self.queue is not None and paths:
            self.show_view("queue")
            self.queue.add_paths(paths, preset=preset)

    def select_preset(self, name: str) -> None:
        if self.queue is not None:
            self.queue.select_preset(name)

    def may_close(self, retry: Callable[[], None]) -> bool:
        """True if AirGrabber may quit now. Otherwise `retry` is called later: a Forge run stops
        after its current file first, and an undo in History is allowed to finish."""
        if self.queue is not None and not self.queue.request_close(retry):
            return False
        if self.history is not None and self.history.busy:
            log.info("Waiting for the undo to finish")
            self.after(HISTORY_RETRY_MS, retry)
            return False
        return True

    def destroy(self) -> None:
        self._later.clear()
        if self._later_id is not None:
            try:
                self.after_cancel(self._later_id)
            except tk.TclError:
                pass  # gone with its interpreter
            self._later_id = None
        try:
            tk.Misc.unbind(self._top, "<Escape>", self._escape_id)
            tk.Misc.unbind(self._tabs_row, "<Configure>", self._row_id)
        except tk.TclError:
            pass  # the window is going too
        tools = (self.queue_tools, self.history_tools)
        super().destroy()  # the queue and History cancel their tasks
        for frame in tools:
            if frame is not None:
                try:
                    frame.destroy()
                except tk.TclError:
                    pass  # gone with the main window
