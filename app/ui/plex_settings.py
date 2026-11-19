"""Settings → Plex & library: the "Plex" box (mockups/plex.html, states 1 and 3;
mockups/folder-library.html for its first line) over PlexService (app/services/plex.py).

Not connected: what Plex does for AirGrabber in one line ("Have a Plex server? Connect it and Plex
is your library.", or, while your own folders are on, that Plex would be a shelf beside them)
and "Connect to Plex…" (Plex's yellow, dark text), which opens the connect window
(app/ui/plex_connect.py). Connected: a dot and
"Connected to <server>" (amber with the reason when the last check failed: "Plex didn't answer:
the last list from 2 h ago is used", "Plex asks to connect again" with the Connect button
again), what was found ("812 episodes of 24 of your series · 146 films · checked 3 min ago"),
the server menu while the account has several, a tick box per show and film library, "↻ Check
now" and "Disconnect" (after a confirm it forgets the token, the server and what was read).
Given the settings, two boxes on Settings → Playing (`play_page`): `play_box` holds "Play in:
AirGrabber's player / VLC" (`Settings.play_in`, docs/player/2026-11-13-pc-player-design.md),
and `explorer_box` the switch for Explorer's "Open with" (`Settings.open_with_player`,
app/services/open_with.py, docs/player/2026-11-14-open-with-design.md), applied with it; with
Plex or without. Both are changes as the rest of Settings' (the window's `committer`: stored at
once, "✓ Saved · Undo", a refusal in their box; Undo applies "Open with" back too). `PlexAccountBox` is Accounts & keys' line about
Plex, with the way to Plex & library. `FoldersLine` is Plex & library's line about the user's own
folders while they count, with the way to Folders (where they are set).

Plex's own actions (Connect, Check now, a library's tick, the server, Disconnect) act at once
through PlexService, as Backup's buttons do, without Undo. The box follows the service (`PlexService.subscribe`), and the folder library's (`folders`: its
first line), and stops when it goes.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.models import PlayIn
from app.models.plex import PlexCounts, PlexLibrary, PlexProblem, PlexServer, PlexStatus
from app.ui import theme
from app.ui.confirm_dialog import ConfirmDialog
from app.services.library_source import library_source
from app.ui.dialog_kit import BUTTON_HEIGHT, BUTTON_RADIUS, DETAIL_SIZE, secondary_button
from app.ui.folder_library_settings import FILM_MORE, FILM_ONE, SERIES_MORE, SERIES_ONE, counted
from app.ui.missing_control import mark_dot
from app.ui.plex_connect import PlexConnectWindow
from app.ui.settings_commit import Committer, commit_fields
from app.ui.settings_layout import BOX_INNER, TEXT_SIZE, SettingsBox, SettingsPage
from app.ui.tasks import spawn
from app.services import open_with
from app.ui.watchlist_cards import searched_ago

if TYPE_CHECKING:
    from app.models.folder_library import FolderLibraryStatus
    from app.services.plex import PlexService
    from app.services.settings import SettingsService


class FolderSwitch(Protocol):
    """FolderLibraryService as the Plex box asks it: whether your own folders are the library."""

    @property
    def status(self) -> FolderLibraryStatus: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...


TITLE_TEXT = N_("Plex")
INTRO_TEXT = N_("Have a Plex server? Connect it and Plex is your library.")
INTRO_FOLDERS_TEXT = N_("Connect Plex and it becomes a shelf beside your folders.")
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
PLAY_IN_TEXT = N_("Play in")
PLAYER_TEXT = N_("AirGrabber's player")
VLC_TEXT = N_("VLC")
OPEN_WITH_TEXT = N_("Offer AirGrabber's player in Explorer's \"Open with\" for any video")
EXPLORER_TITLE = N_("Explorer")  # the Open with tick's box
NOT_CONNECTED_TEXT = N_("Not connected")
PLEX_PAGE_LINK = N_("Plex & library →")
FOLDERS_TITLE = N_("Your own folders")
FOLDERS_BESIDE_PLEX = N_("{films} · {series} from your own folders count beside Plex.")
FOLDERS_ALONE = N_("{films} · {series} from your own folders count as your library.")
FOLDERS_PAGE_LINK = N_("Set under Folders →")
# Settings.play_in's choices, in the control's order (shown through t()).
PLAY_IN_NAMES = {PlayIn.AIRGRABBER: PLAYER_TEXT, PlayIn.VLC: VLC_TEXT}
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
PLAY_IN_WIDTH = 300


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
        settings: SettingsService | None = None,
        clock: Callable[[], datetime] = _now,
        apply_open_with: Callable[[bool], Awaitable[None]] | None = None,
        folders: FolderSwitch | None = None,
        play_page: SettingsPage | None = None,
        committer: Committer | None = None,
    ) -> None:
        """`apply_open_with`: writes or removes Explorer's "Open with" registration
        (None: app/services/open_with.py's `apply`). `folders`: the folder library, whose
        being on changes the first line while Plex isn't connected. `play_page`: where the
        "Play in" and "Explorer" boxes go (Settings → Playing; `page` without it).
        `committer`: the window's, which stores those two (one of its own without it)."""
        self._plex = plex
        self._folders = folders
        self._settings = settings
        self._committer = committer if committer is not None else Committer()
        self._apply_open_with = apply_open_with
        self._clock = clock
        self._tasks: set[asyncio.Task[None]] = set()
        self._load_task: asyncio.Task[None] | None = None
        self.status: PlexStatus | None = None
        self.connect_window: PlexConnectWindow | None = None
        self.confirm: ConfirmDialog | None = None
        # Settings' search finds it as "Plex · Plex & library" (its heading is drawn here).
        self.box: SettingsBox = page.box(None, None, search_title=t(TITLE_TEXT))
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
        # "Play in", and "Open with" in its own box (Settings → Playing).
        self.play_box: SettingsBox | None = None
        self.explorer_box: SettingsBox | None = None
        self.player_line: ctk.CTkFrame | None = None
        self.play_in_control: ctk.CTkSegmentedButton | None = None
        self._play_in = ctk.StringVar(master=self.content)
        self.open_with_box: ctk.CTkSwitch | None = None
        self._open_with = ctk.BooleanVar(master=self.content)
        if settings is not None:
            playing = play_page if play_page is not None else page
            play_box = self.play_box = playing.box(None, None, search_title=t(PLAY_IN_TEXT))
            self.player_line = self._player_line(play_box)
            play_box.add(self.player_line, gap=0, sticky="w")
            self._play_in.set(t(PLAY_IN_NAMES[settings.current.play_in]))
            self._open_with.set(settings.current.open_with_player)
            explorer = self.explorer_box = playing.box(t(EXPLORER_TITLE))
            self.open_with_box = ctk.CTkSwitch(
                explorer.body,
                text=t(OPEN_WITH_TEXT),
                variable=self._open_with,
                height=BUTTON_HEIGHT,
                font=theme.font(DETAIL_SIZE),
                progress_color=theme.ACCENT,
                text_color=theme.TEXT,
                command=lambda: self.set_open_with(self._open_with.get()),
            )
            explorer.add(self.open_with_box, gap=10, sticky="w")
        # Filled by `show`.
        self.connect_button: ctk.CTkButton | None = None
        self.check_button: ctk.CTkButton | None = None
        self.disconnect_button: ctk.CTkButton | None = None
        self.status_label: ctk.CTkLabel | None = None
        self.counts_label: ctk.CTkLabel | None = None
        self.server_menu: ctk.CTkOptionMenu | None = None
        self.library_boxes: dict[str, ctk.CTkCheckBox] = {}
        self._unsubscribe = [plex.subscribe(self.refresh)]
        if folders is not None:
            self._unsubscribe.append(folders.subscribe(self.refresh))
        # Tk's own bind: CTkFrame.bind would bind its canvas.
        if settings is not None:  # "Play in" and "Open with" stored elsewhere, or undone
            self._unsubscribe.append(settings.subscribe(lambda _settings: self._show_stored()))
        tk.Misc.bind(self.content, "<Destroy>", self._on_destroy, add="+")
        self.refresh()

    # -- following the service -------------------------------------------------------------------

    def _show_stored(self) -> None:
        if _exists(self.content) and self._settings is not None:
            self._show_player_line()
            if self._open_with.get() != self._settings.current.open_with_player:
                self._open_with.set(self._settings.current.open_with_player)

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
        for unsubscribe in self._unsubscribe:
            unsubscribe()
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
        self._show_player_line()
        row = 0

        def place(widget: Any, *, gap: int = 8) -> None:
            nonlocal row
            widget.grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else gap, 0))
            row += 1

        if not status.connected:
            folders_on = self._folders is not None and self._folders.status.on
            place(self._text(t(INTRO_FOLDERS_TEXT if folders_on else INTRO_TEXT)))
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

    def _player_line(self, box: SettingsBox) -> ctk.CTkFrame:
        line = ctk.CTkFrame(box.body, fg_color="transparent")
        ctk.CTkLabel(
            line, text=t(PLAY_IN_TEXT), font=theme.font(TEXT_SIZE), text_color=theme.TEXT_MUTED
        ).pack(side="left", padx=(0, 10))
        self.play_in_control = ctk.CTkSegmentedButton(
            line,
            values=[t(name) for name in PLAY_IN_NAMES.values()],
            variable=self._play_in,
            command=self.choose_play_in,
            width=PLAY_IN_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE, bold=True),
            fg_color=theme.SURFACE,
            selected_color=theme.SELECTED,
            selected_hover_color=theme.SELECTED_HOVER,
            unselected_color=theme.SURFACE,
            unselected_hover_color=theme.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
        )
        self.play_in_control.pack(side="left")
        return line

    def _show_player_line(self) -> None:
        """The "Play in" line showing the saved choice (it plays from Plex or your folders)."""
        if self.player_line is None or self._settings is None:
            return
        self._play_in.set(t(PLAY_IN_NAMES[self._settings.current.play_in]))

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

    def choose_play_in(self, name: str) -> asyncio.Task[bool] | None:
        """Store the "Play in" choice at once (`name` as the control shows it): one change."""
        settings = self._settings
        chosen = next((key for key, text in PLAY_IN_NAMES.items() if t(text) == name), None)
        box = self.play_box
        if settings is None or box is None or chosen is None or chosen is settings.current.play_in:
            return None
        return spawn(
            commit_fields(self._committer, settings, box, play_in=chosen), name="plex-play-in"
        )

    def set_open_with(self, on: bool) -> asyncio.Task[bool] | None:
        """Store the "Open with" switch and write or remove the registration at once: one
        change; Undo stores and applies it back."""
        settings = self._settings
        box = self.explorer_box
        if settings is None or box is None:
            return None
        apply_open_with = self._apply_open_with or open_with.apply
        before = settings.current.open_with_player

        def setting(wanted: bool) -> Callable[[], Awaitable[bool]]:
            async def store() -> bool:
                if settings.current.open_with_player == wanted:
                    return False
                await settings.update(open_with_player=wanted)
                await apply_open_with(wanted)
                return True

            return store

        async def change() -> bool:
            stored = await self._committer.commit(setting(on), setting(before), box=box)
            if _exists(self.content):
                self._open_with.set(settings.current.open_with_player)
            return stored

        return spawn(change(), name="plex-open-with")

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


class PlexAccountBox:
    """Settings → Accounts & keys' "Plex" box: whether AirGrabber is connected to Plex
    ("Connected to Alex-NAS", the problem in amber, or "Not connected"), and a link to Plex &
    library (`on_open`), where Plex is connected and checked. It follows the service."""

    def __init__(
        self,
        page: SettingsPage,
        plex: PlexService,
        *,
        on_open: Callable[[], None],
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._plex = plex
        self._clock = clock
        self._on_open = on_open
        self._task: asyncio.Task[None] | None = None
        self.box: SettingsBox = page.box(t(TITLE_TEXT))
        row = self.box.frame_row(gap=6)
        self.status_label = ctk.CTkLabel(
            row,
            text="",
            font=theme.font(TEXT_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        )
        self.status_label.pack(side="left")
        self.link = ctk.CTkLabel(
            row,
            text=t(PLEX_PAGE_LINK),
            font=theme.font(TEXT_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
        )
        self.link.pack(side="left", padx=(12, 0))
        self.link.bind("<Button-1>", lambda _event: self.open())
        self._unsubscribe = plex.subscribe(self.refresh)
        tk.Misc.bind(self.status_label, "<Destroy>", self._on_destroy, add="+")
        self.refresh()

    def open(self) -> None:
        """The link: Plex & library."""
        self._on_open()

    def refresh(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = spawn(self._load(), name="PlexAccountBox.load")

    async def loaded(self) -> None:
        """Wait for the pending refresh (tests)."""
        if self._task is not None:
            await asyncio.wait({self._task})

    async def _load(self) -> None:
        status = await self._plex.status()
        if not _exists(self.status_label):
            return
        if status.connected:
            text, colour = status_text(status, self._clock())
        else:
            text, colour = t(NOT_CONNECTED_TEXT), theme.TEXT_MUTED
        self.status_label.configure(text=text, text_color=colour)

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self.status_label:
            return
        self._unsubscribe()
        if self._task is not None:
            self._task.cancel()


class FoldersLine:
    """Settings → Plex & library's line about the user's own folders, shown only while they
    count ("37 films · 6 series from your own folders count beside Plex."), with a link to the
    Folders page (`on_open`), where they are set. It follows the folder library."""

    def __init__(
        self,
        page: SettingsPage,
        folders: FolderSwitch,
        settings: SettingsService,
        *,
        on_open: Callable[[], None],
    ) -> None:
        self._folders = folders
        self._settings = settings
        self._on_open = on_open
        self.box: SettingsBox = page.box(t(FOLDERS_TITLE))
        row = self.box.frame_row(gap=6)
        self.label = ctk.CTkLabel(
            row,
            text="",
            font=theme.font(TEXT_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=BOX_INNER - 200,
        )
        self.label.pack(side="left")
        self.link = ctk.CTkLabel(
            row,
            text=t(FOLDERS_PAGE_LINK),
            font=theme.font(TEXT_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
        )
        self.link.pack(side="left", padx=(12, 0))
        self.link.bind("<Button-1>", lambda _event: self.open())
        self._unsubscribe = folders.subscribe(self.show)
        tk.Misc.bind(self.label, "<Destroy>", self._on_destroy, add="+")
        self.show()

    @property
    def shown(self) -> bool:
        return bool(self.box.frame.winfo_manager())

    def open(self) -> None:
        """The link: the Folders page."""
        self._on_open()

    def show(self) -> None:
        """Shown with the counts while the folders count; out of sight otherwise."""
        if not _exists(self.label):
            return
        status = self._folders.status
        if not status.on:
            self.box.frame.grid_remove()
            return
        beside_plex = library_source(self._settings.current) == "plex"
        text = FOLDERS_BESIDE_PLEX if beside_plex else FOLDERS_ALONE
        self.label.configure(
            text=t(
                text,
                films=counted(FILM_ONE, FILM_MORE, status.films),
                series=counted(SERIES_ONE, SERIES_MORE, status.shows),
            )
        )
        self.box.frame.grid()

    def _on_destroy(self, event: Any) -> None:
        if event.widget is self.label:
            self._unsubscribe()


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
