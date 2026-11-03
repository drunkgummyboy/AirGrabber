"""Settings → Plex: the "Plex" box (mockups/plex.html, states 1 and 3) over PlexService
(app/services/plex.py).

Not connected: what Plex does for AirGrabber in one line and "Connect to Plex…" (Plex's yellow,
dark text), which opens the connect window (app/ui/plex_connect.py). Connected: a dot and
"Connected to <server>" (amber with the reason when the last check failed: "Plex didn't answer:
the last list from 2 h ago is used", "Plex asks to connect again" with the Connect button
again), what was found ("812 episodes of 24 of your series · 146 films · checked 3 min ago"),
the server menu while the account has several, a tick box per show and film library, "↻ Check
now" and "Disconnect" (after a confirm it forgets the token, the server and what was read).

Everything acts at once (like Backup's buttons), not on Save: Plex's settings are its own. The
box follows the service (`PlexService.subscribe`) and stops when it goes.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.models.plex import PlexCounts, PlexLibrary, PlexProblem, PlexServer, PlexStatus
from app.ui import theme
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.dialog_kit import BUTTON_HEIGHT, BUTTON_RADIUS, DETAIL_SIZE, secondary_button
from app.ui.missing_control import mark_dot
from app.ui.plex_connect import PlexConnectWindow
from app.ui.settings_layout import BOX_INNER, TEXT_SIZE, SettingsBox, SettingsPage
from app.ui.tasks import spawn
from app.ui.watchlist_cards import searched_ago

if TYPE_CHECKING:
    from app.services.plex import PlexService

TITLE_TEXT = N_("Plex")
INTRO_TEXT = N_(
    "Let AirGrabber see what's in your Plex libraries, so it never downloads something you "
    "already have. It only reads; your Plex password isn't needed."
)
CONNECT_TEXT = N_("Connect to Plex…")
CHECK_NOW_TEXT = N_("↻ Check now")
CHECKING_TEXT = N_("Checking…")
DISCONNECT_TEXT = N_("Disconnect")
LIBRARIES_TEXT = N_("Libraries:")
SERVER_TEXT = N_("Server:")
NOT_CHECKED_TEXT = N_("Not checked yet")
NO_LIBRARIES_TEXT = N_("No show or film libraries found on this server")
YOUR_SERVER_TEXT = N_("your Plex server")
UNREACHABLE_TEXT = N_("Plex didn't answer")
SIGNED_OUT_TEXT = N_("Plex asks to connect again")
NO_SERVER_TEXT = N_("Your Plex account has no server AirGrabber can read")
CHOOSE_SERVER_TEXT = N_("Choose which Plex server AirGrabber reads")
FAILED_TEXT = N_("Plex's answer didn't read")
CONFIRM_TITLE = N_("Disconnect from Plex?")
CONFIRM_TEXT = N_(
    "AirGrabber forgets its Plex sign-in and what it read from Plex. Nothing changes in Plex "
    "itself."
)

LOGO_SIDE = 22
LOGO_RADIUS = 5
CONNECT_WIDTH = 170
CHECK_WIDTH = 140
DISCONNECT_WIDTH = 130
SERVER_MENU_WIDTH = 260


# -- texts (pure) -------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def counts_text(counts: PlexCounts, now: datetime) -> str:
    """"812 episodes of 24 of your series · 146 films · checked 3 min ago", or "Not checked
    yet" before the first check."""
    if counts.checked_at is None:
        return t(NOT_CHECKED_TEXT)
    return t(
        "{episodes} of {series} · {films} · checked {when}",
        episodes=tn("{n} episode", "{n} episodes", counts.episodes),
        series=tn("{n} of your series", "{n} of your series", counts.shows),
        films=tn("{n} film", "{n} films", counts.films),
        when=searched_ago(counts.checked_at, now),
    )


def problem_text(problem: PlexProblem, counts: PlexCounts, now: datetime) -> str:
    """Why the last check failed, in plain words, and that the last list is used when there
    is one."""
    if problem is PlexProblem.SIGNED_OUT:
        return t(SIGNED_OUT_TEXT)
    if problem is PlexProblem.NO_SERVER:
        return t(NO_SERVER_TEXT)
    if problem is PlexProblem.CHOOSE_SERVER:
        return t(CHOOSE_SERVER_TEXT)
    reason = t(UNREACHABLE_TEXT) if problem is PlexProblem.UNREACHABLE else t(FAILED_TEXT)
    if counts.checked_at is None:
        return reason
    return t(
        "{reason}: the last list from {when} is used",
        reason=reason,
        when=searched_ago(counts.checked_at, now),
    )


def status_text(status: PlexStatus, now: datetime) -> tuple[str, theme.Color]:
    """The box's status line and its dot's colour: "Connected to Alex-NAS" (green), or the
    problem (amber)."""
    if status.problem is not None:
        return problem_text(status.problem, status.counts, now), theme.WARNING
    name = status.server.name if status.server is not None and status.server.name else ""
    return (
        t("Connected to {server}", server=name or t(YOUR_SERVER_TEXT)),
        theme.SUCCESS,
    )


def server_names(servers: tuple[PlexServer, ...]) -> dict[str, str]:
    """The server menu's entries: name → machine id (a name twice gets its id's start)."""
    names: dict[str, str] = {}
    for server in servers:
        name = server.name or server.machine_id[:8]
        if name in names:
            name = f"{name} ({server.machine_id[:6]})"
        names[name] = server.machine_id
    return names


# -- the box ------------------------------------------------------------------------------------


class PlexBox:
    """The Plex box on a Settings page (see the module docstring)."""

    def __init__(
        self,
        page: SettingsPage,
        plex: PlexService,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._plex = plex
        self._clock = clock
        self._tasks: set[asyncio.Task[None]] = set()
        self._load_task: asyncio.Task[None] | None = None
        self.status: PlexStatus | None = None
        self.connect_window: PlexConnectWindow | None = None
        self.confirm: ConfirmDialog | None = None
        self.box: SettingsBox = page.box(None, None)
        header = ctk.CTkFrame(self.box.body, fg_color="transparent")
        self.box.add(header, gap=0, sticky="w")
        ctk.CTkLabel(
            header,
            text="›",
            width=LOGO_SIDE,
            height=LOGO_SIDE,
            corner_radius=LOGO_RADIUS,
            fg_color=theme.PLEX,
            text_color=theme.ON_PLEX,
            font=theme.font(15, bold=True),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            header, text=t(TITLE_TEXT), font=theme.font(16, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        self.content = ctk.CTkFrame(self.box.body, fg_color="transparent")
        self.box.add(self.content, gap=6)
        # Filled by `show`.
        self.connect_button: ctk.CTkButton | None = None
        self.check_button: ctk.CTkButton | None = None
        self.disconnect_button: ctk.CTkButton | None = None
        self.status_label: ctk.CTkLabel | None = None
        self.counts_label: ctk.CTkLabel | None = None
        self.server_menu: ctk.CTkOptionMenu | None = None
        self.library_boxes: dict[str, ctk.CTkCheckBox] = {}
        self._unsubscribe = plex.subscribe(self.refresh)
        # Tk's own bind: CTkFrame.bind would bind its canvas.
        tk.Misc.bind(self.content, "<Destroy>", self._on_destroy, add="+")
        self.refresh()

    # -- following the service -------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the service's status again and show it (the newest call wins)."""
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = self._run(self._load(), "PlexBox.load")

    async def loaded(self) -> None:
        """Wait for the pending refresh (tests)."""
        task = self._load_task
        if task is not None:
            await asyncio.wait({task})

    async def _load(self) -> None:
        status = await self._plex.status()
        if _exists(self.content):
            self.show(status)

    def _run(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task: asyncio.Task[None] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self.content:
            return
        self._unsubscribe()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # -- drawing -----------------------------------------------------------------------------------

    def show(self, status: PlexStatus) -> None:
        """Draw the box for `status`."""
        self.status = status
        for child in self.content.winfo_children():
            child.destroy()
        self.connect_button = self.check_button = self.disconnect_button = None
        self.status_label = self.counts_label = None
        self.server_menu = None
        self.library_boxes = {}
        self.content.grid_columnconfigure(0, weight=1)
        row = 0

        def place(widget: Any, *, gap: int = 8) -> None:
            nonlocal row
            widget.grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else gap, 0))
            row += 1

        if not status.connected:
            place(self._text(t(INTRO_TEXT)))
            self.connect_button = self._connect_button()
            place(self.connect_button, gap=14)
            return

        now = self._clock()
        text, colour = status_text(status, now)
        self.status_label = ctk.CTkLabel(
            self.content,
            text=text,
            image=mark_dot(self.content, colour),
            compound="left",
            font=theme.font(TEXT_SIZE, bold=status.problem is None),
            text_color=theme.TEXT,
            anchor="w",
            justify="left",
            wraplength=BOX_INNER - 20,
        )
        place(self.status_label)
        if status.problem is PlexProblem.SIGNED_OUT:
            self.connect_button = self._connect_button()
            place(self.connect_button, gap=10)
        self.counts_label = self._text(counts_text(status.counts, now))
        place(self.counts_label, gap=6)
        if len(status.servers) > 1:
            place(self._server_row(status), gap=12)
        if status.libraries:
            place(self._library_row(status.libraries), gap=12)
        elif status.counts.checked_at is not None:
            place(self._text(t(NO_LIBRARIES_TEXT)), gap=12)
        buttons = ctk.CTkFrame(self.content, fg_color="transparent")
        self.check_button = secondary_button(
            buttons,
            t(CHECKING_TEXT) if status.checking else t(CHECK_NOW_TEXT),
            self.check_now,
            width=CHECK_WIDTH,
        )
        if status.checking:
            self.check_button.configure(state="disabled")
        self.check_button.pack(side="left")
        self.disconnect_button = secondary_button(
            buttons, t(DISCONNECT_TEXT), self.ask_disconnect, width=DISCONNECT_WIDTH
        )
        self.disconnect_button.pack(side="left", padx=(12, 0))
        place(buttons, gap=14)

    def _text(self, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            self.content,
            text=text,
            font=theme.font(TEXT_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=BOX_INNER,
        )

    def _connect_button(self) -> ctk.CTkButton:
        return ctk.CTkButton(
            self.content,
            text=t(CONNECT_TEXT),
            width=CONNECT_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            fg_color=theme.PLEX,
            hover_color=theme.PLEX_HOVER,
            text_color=theme.ON_PLEX,
            font=theme.font(15, bold=True),
            command=self.open_connect,
        )

    def _server_row(self, status: PlexStatus) -> ctk.CTkFrame:
        line = ctk.CTkFrame(self.content, fg_color="transparent")
        ctk.CTkLabel(
            line, text=t(SERVER_TEXT), font=theme.font(TEXT_SIZE), text_color=theme.TEXT_MUTED
        ).pack(side="left", padx=(0, 10))
        names = server_names(status.servers)
        chosen = status.server.machine_id if status.server is not None else ""
        current = next((name for name, mid in names.items() if mid == chosen), "")
        self.server_menu = ctk.CTkOptionMenu(
            line,
            values=list(names),
            width=SERVER_MENU_WIDTH,
            height=BUTTON_HEIGHT - 6,
            font=theme.font(DETAIL_SIZE),
            fg_color=theme.SURFACE,
            button_color=theme.EDGE,
            button_hover_color=theme.ACCENT_HOVER,
            text_color=theme.TEXT,
            dropdown_font=theme.font(DETAIL_SIZE),
            command=lambda name: self.choose_server(names.get(name, "")),
        )
        self.server_menu.set(current or t(CHOOSE_SERVER_TEXT))
        self.server_menu.pack(side="left")
        return line

    def _library_row(self, libraries: tuple[PlexLibrary, ...]) -> ctk.CTkFrame:
        line = ctk.CTkFrame(self.content, fg_color="transparent")
        ctk.CTkLabel(
            line, text=t(LIBRARIES_TEXT), font=theme.font(TEXT_SIZE), text_color=theme.TEXT_MUTED
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))
        for index, library in enumerate(libraries):
            variable = ctk.BooleanVar(value=library.on)
            box = ctk.CTkCheckBox(
                line,
                text=library.title,
                variable=variable,
                width=0,
                height=BUTTON_HEIGHT - 8,
                checkbox_width=22,
                checkbox_height=22,
                corner_radius=6,
                font=theme.font(DETAIL_SIZE),
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                border_color=theme.EDGE,
                command=lambda key=library.key, var=variable: self.set_library(key, var.get()),
            )
            # Three a line; the rest wrap under them.
            box.grid(row=index // 3, column=1 + index % 3, sticky="w", padx=(0, 18), pady=2)
            self.library_boxes[library.key] = box
        return line

    # -- actions -----------------------------------------------------------------------------------

    def open_connect(self) -> None:
        window = self.connect_window
        if window is not None and window.winfo_exists():
            window.lift()
            return
        self.connect_window = PlexConnectWindow(
            self.content.winfo_toplevel(), plex=self._plex, on_connected=self._connected
        )

    def _connected(self) -> None:
        """The token arrived: the server is picked and the first check runs."""
        spawn(self._plex.check(), name="plex-first-check")

    def check_now(self) -> None:
        if self.check_button is not None:
            self.check_button.configure(text=t(CHECKING_TEXT), state="disabled")
        spawn(self._plex.check(), name="plex-check-now")

    def choose_server(self, machine_id: str) -> None:
        if machine_id:
            spawn(self._plex.choose_server(machine_id), name="plex-choose-server")

    def set_library(self, key: str, on: bool) -> None:
        spawn(self._plex.set_library(key, on), name="plex-library")

    def ask_disconnect(self) -> None:
        dialog = self.confirm
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            return
        owner = self.content.winfo_toplevel()
        self.confirm = ConfirmDialog(
            owner,
            title=t(CONFIRM_TITLE),
            text=t(CONFIRM_TEXT),
            confirm=t(DISCONNECT_TEXT),
            on_confirm=self.disconnect,
        )
        # Settings is modal: it gets the grab back once the question goes.
        dialog = self.confirm
        dialog.bind("<Destroy>", lambda event: _regrab(event, dialog, owner), add="+")

    def disconnect(self) -> None:
        spawn(self._plex.disconnect(), name="plex-disconnect")


def _regrab(event: Any, dialog: tk.Misc, owner: tk.Misc) -> None:
    if event.widget is not dialog:
        return
    try:
        if owner.winfo_exists():
            owner.after_idle(lambda: _grab(owner))
    except tk.TclError:
        pass


def _grab(owner: tk.Misc) -> None:
    try:
        if owner.winfo_exists():
            owner.grab_set()
            owner.focus_force()
    except tk.TclError:
        pass


def _exists(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False
