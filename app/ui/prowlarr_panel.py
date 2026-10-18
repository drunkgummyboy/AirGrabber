"""Settings → Advanced: the Prowlarr panel. A status line (connected / not running / not
installed …), "Install Prowlarr" when it is missing, "Use Prowlarr", "Open Prowlarr", and a link
that shows the fields for a Prowlarr on another computer (rows from `remote_row`, filled by the
Settings dialog)."""

from __future__ import annotations

import asyncio
import tkinter as tk
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import customtkinter as ctk

from app.models import ProwlarrState, ProwlarrStatus
from app.services import ProwlarrSetupService
from app.services.prowlarr_setup import INSTALLING_TEXT, on_this_pc
from app.ui import theme
from app.ui.dialog_kit import (
    BUTTON_HEIGHT,
    DETAIL_SIZE,
    SMALL_SIZE,
    link_label,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.tasks import spawn

PROWLARR_URL = "https://prowlarr.com"

# Logical (unscaled) CTk units.
PANEL_PAD_X = 18
PANEL_PAD_Y = 16
_PANEL_RADIUS = 12
_LINE_GAP = 10  # between the panel's lines
_REMOTE_GAP = 14  # above the "another computer" fields, and between them
_CONTROL_GAP = 16  # between the controls of one line
_INSTALL_BUTTON_WIDTH = 170
_OPEN_BUTTON_WIDTH = 150
_GET_LINK_ROOM = 120  # left of the status text for the "Get Prowlarr" link

TITLE_NOTE = " · a free helper program that searches many more sites"
INSTALL_NOTE = "Windows will ask for permission once; AirGrabber then sets it up"
TOGGLE_TEXT = "Use a Prowlarr on another computer…"

# Makes a checkbox in the Settings dialog's style: (master, text, variable).
CheckboxFactory = Callable[[Any, str, ctk.BooleanVar], ctk.CTkCheckBox]


@dataclass(frozen=True, slots=True)
class ProwlarrLook:
    """What the panel shows: the status line and which controls go with it."""

    text: str
    color: str
    open_button: bool = False  # "Open Prowlarr"
    install_button: bool = False  # "Install Prowlarr" and its note
    get_link: bool = False  # "Get Prowlarr" (prowlarr.com) after a failed install


CHECKING = ProwlarrLook("Checking…", theme.TEXT_MUTED)
INSTALL_FAILED = ProwlarrLook("Couldn't install it automatically.", theme.DANGER, get_link=True)


def prowlarr_look(status: ProwlarrStatus) -> ProwlarrLook:
    """The status line for `status` (ProwlarrSetupService.status)."""
    can_open = bool(status.address)
    state = status.state
    if state is ProwlarrState.CONNECTED_LOCAL:
        text = "✓ Connected to Prowlarr on this PC"
        if status.sites is not None:
            text += f" · {site_count(status.sites)}"
        return ProwlarrLook(text, theme.SUCCESS, open_button=can_open)
    if state is ProwlarrState.CONNECTED_REMOTE:
        return ProwlarrLook(
            f"✓ Connected to Prowlarr at {host_port(status.address)}",
            theme.SUCCESS,
            open_button=can_open,
        )
    if state is ProwlarrState.NOT_RUNNING:
        return ProwlarrLook(
            "Prowlarr is installed but isn't answering. "
            "Restart your PC, or open it from the Start menu.",
            theme.WARNING,
            open_button=can_open,
        )
    if state is ProwlarrState.NOT_INSTALLED:
        return ProwlarrLook("Prowlarr isn't installed", theme.WARNING, install_button=True)
    return ProwlarrLook("Not connected", theme.WARNING, open_button=can_open)


def site_count(sites: int) -> str:
    return "1 site" if sites == 1 else f"{sites} sites"


def host_port(url: str) -> str:
    """The 'host:port' part of `url`, never a user name or password in it."""
    try:
        netloc = urlsplit(url.strip()).netloc
    except ValueError:
        return ""
    return netloc.rpartition("@")[2]


def shows_remote_fields(url: str) -> bool:
    """The "another computer" fields start open when Settings holds a Prowlarr address that is
    not on this PC."""
    return bool(url.strip()) and not on_this_pc(url)


def _alive(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole application is gone
        return False


class ProwlarrPanel(ctk.CTkFrame):
    """The bordered Prowlarr block. `refresh()` reads the status (again); `on_status` hears every
    status shown (the dialog then shows a URL and key saved meanwhile)."""

    def __init__(
        self,
        master: Any,
        *,
        prowlarr: ProwlarrSetupService,
        use: ctk.BooleanVar,
        checkbox: CheckboxFactory,
        width: int,
        label_width: int,
        expanded: bool,
        on_status: Callable[[ProwlarrStatus], None] | None = None,
    ) -> None:
        """`width` is the panel's outer width; `label_width` the label column of the
        "another computer" rows (logical units)."""
        super().__init__(
            master,
            fg_color="transparent",
            border_width=1,
            border_color=theme.PANEL_EDGE,
            corner_radius=_PANEL_RADIUS,
        )
        self._prowlarr = prowlarr
        self._on_status = on_status
        self._address = ""
        self._expanded = False
        self._installing = False
        self._read_again = False
        self._status_task: asyncio.Task[None] | None = None
        self._install_task: asyncio.Task[None] | None = None
        inner = width - 2 * (PANEL_PAD_X + 1)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PANEL_PAD_X, pady=PANEL_PAD_Y)
        body.grid_columnconfigure(0, weight=1)

        title = ctk.CTkFrame(body, fg_color="transparent")
        title.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title, text="Prowlarr", font=theme.font(16, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            title, text=TITLE_NOTE, font=theme.font(DETAIL_SIZE), text_color=theme.TEXT_MUTED
        ).pack(side="left")

        line = ctk.CTkFrame(body, fg_color="transparent")
        line.grid(row=1, column=0, sticky="ew", pady=(_LINE_GAP, 0))
        self._label = text_label(line, "", size=DETAIL_SIZE, wraplength=inner - _GET_LINK_ROOM)
        self._label.pack(side="left")
        self._get_link = link_label(line, "Get Prowlarr", PROWLARR_URL)

        self._install_row = ctk.CTkFrame(body, fg_color="transparent")
        self._install_button = primary_button(
            self._install_row, "Install Prowlarr", self._install, width=_INSTALL_BUTTON_WIDTH
        )
        self._install_button.pack(side="left", anchor="n")
        text_label(
            self._install_row,
            INSTALL_NOTE,
            size=SMALL_SIZE,
            color=theme.TEXT_MUTED,
            wraplength=inner - _INSTALL_BUTTON_WIDTH - 12,
        ).pack(side="left", padx=(12, 0))

        controls = ctk.CTkFrame(body, fg_color="transparent")
        controls.grid(row=3, column=0, sticky="w", pady=(_LINE_GAP, 0))
        checkbox(controls, "Use Prowlarr", use).grid(row=0, column=0, sticky="w")
        self._open_button = secondary_button(
            controls, "Open Prowlarr", self._open, width=_OPEN_BUTTON_WIDTH
        )
        self._toggle = ctk.CTkLabel(
            controls,
            text=TOGGLE_TEXT,
            font=theme.font(SMALL_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
        )
        self._toggle.bind("<Button-1>", lambda _event: self.set_expanded(not self._expanded))
        self._toggle.grid(row=0, column=2, sticky="w", padx=(_CONTROL_GAP, 0))

        self.remote = ctk.CTkFrame(body, fg_color="transparent")
        # Plain Tk grid options are not DPI-scaled by CTk.
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        self.remote.grid_columnconfigure(0, minsize=round(label_width * scaling))
        self.remote.grid_columnconfigure(1, weight=1)
        self._remote_rows = 0

        self.set_expanded(expanded)
        self.show_look(CHECKING)

    # -- layout --------------------------------------------------------------------------------

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """Show or hide the fields for a Prowlarr on another computer."""
        self._expanded = expanded
        if expanded:
            self.remote.grid(row=4, column=0, sticky="ew", pady=(_REMOTE_GAP, 0))
        else:
            self.remote.grid_remove()

    def remote_row(self, label: str) -> ctk.CTkFrame:
        """A labelled row in the "another computer" area; the caller fills the returned cell."""
        row = self._remote_rows
        self._remote_rows += 1
        pady = (0 if row == 0 else _REMOTE_GAP, 0)
        ctk.CTkLabel(
            self.remote,
            text=label,
            height=BUTTON_HEIGHT,
            font=theme.font(DETAIL_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        ).grid(row=row, column=0, sticky="nw", pady=pady)
        cell = ctk.CTkFrame(self.remote, fg_color="transparent")
        cell.grid(row=row, column=1, sticky="ew", pady=pady)
        return cell

    def show(self, status: ProwlarrStatus) -> None:
        self._address = status.address
        self.show_look(prowlarr_look(status))
        if self._on_status is not None:
            self._on_status(status)

    def show_look(self, look: ProwlarrLook) -> None:
        self._label.configure(text=look.text, text_color=look.color)
        if look.get_link:
            self._get_link.pack(side="left", padx=(12, 0))
        else:
            self._get_link.pack_forget()
        if look.install_button:
            self._install_row.grid(row=2, column=0, sticky="w", pady=(_LINE_GAP, 0))
        else:
            self._install_row.grid_remove()
        if look.open_button:
            self._open_button.grid(row=0, column=1, sticky="w", padx=(_CONTROL_GAP, 0))
        else:
            self._open_button.grid_remove()

    # -- status and install --------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the status (again): after a test, or when Prowlarr may have changed. Not while
        installing; a read already running reads once more when done."""
        if self._installing:
            return
        self.show_look(CHECKING)
        if self._status_task is not None and not self._status_task.done():
            self._read_again = True
            return
        self._status_task = spawn(self._read_status(), name="prowlarr-status")

    async def _read_status(self) -> None:
        while True:
            self._read_again = False
            status = await self._prowlarr.status()
            if not _alive(self) or self._installing:
                return
            if not self._read_again:
                break
        self.show(status)

    def _install(self) -> None:
        if self._install_task is not None and not self._install_task.done():
            return
        self._installing = True
        self.show_look(ProwlarrLook(INSTALLING_TEXT, theme.TEXT_MUTED))
        # Not cancelled with the panel: stopping winget halfway could leave a broken install.
        self._install_task = spawn(self._run_install(), name="prowlarr-install")

    async def _run_install(self) -> None:
        status: ProwlarrStatus | None = None
        try:
            installed = await self._prowlarr.install(self._progress)
            if installed and _alive(self):
                status = await self._prowlarr.status()
        finally:
            self._installing = False
        if not _alive(self):
            return
        if status is None:
            self.show_look(INSTALL_FAILED)
        else:
            self.show(status)

    def _progress(self, text: str) -> None:
        if _alive(self):
            self.show_look(ProwlarrLook(text, theme.TEXT_MUTED))

    def _open(self) -> None:
        if self._address:
            webbrowser.open(self._address)

    def destroy(self) -> None:
        if self._status_task is not None:
            self._status_task.cancel()
        super().destroy()
