"""The torrent-program status line ("✓ qBittorrent found" / "No torrent program found" with an
"Install qBittorrent" button), shared by the first-run welcome and Settings.

With qBittorrent it also sets qBittorrent up for AirGrabber (no "Add new torrent" pop-up for each
download, app/services/qbittorrent_setup.py): "✓ qBittorrent is installed and ready" once it is,
else a "Set up qBittorrent for AirGrabber" button. An "Install qBittorrent" from here sets it up
right after installing. qBittorrent's settings only change while it is closed: while it runs, the
line asks to close it and the setup tries again every few seconds.

Once qBittorrent is set up, an "AirGrabber look in qBittorrent" switch shows under the line (the
setup turns that look on when qBittorrent has no theme yet; not shown while a theme of the user's
own is in use). Switching it changes qBittorrent's settings under the same rule: while qBittorrent
runs the line asks to close it, and the change is tried again every few seconds."""

from __future__ import annotations

import asyncio
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models import QBittorrentSetupResult, QBittorrentState, TorrentApp
from app.services import TorrentAppService
from app.ui import theme
from app.ui.dialog_kit import (
    BUTTON_HEIGHT,
    DETAIL_SIZE,
    link_label,
    secondary_button,
    text_label,
)
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip

QBITTORRENT = "qBittorrent"  # TorrentApp.name of qBittorrent (app/services/torrent_app.py)
QBITTORRENT_URL = "https://www.qbittorrent.org/download"
INSTALL_BUTTON_WIDTH = 170
# Shown through t().
SETUP_BUTTON_TEXT = N_("Set up qBittorrent for AirGrabber (no pop-up for each download)")
LOOK_SWITCH_TEXT = N_("AirGrabber look in qBittorrent")
LOOK_TIP = N_(
    "qBittorrent in AirGrabber's colours, light or dark like Windows (qBittorrent 5). "
    "AirGrabber changes it while qBittorrent is closed."
)
LOOK_WAITING_TEXT = N_(
    "Close qBittorrent to finish: AirGrabber changes its look as soon as it's closed."
)
RETRY_SECONDS = 3.0  # while qBittorrent runs, try the setup again this often
RETRY_FOR_SECONDS = 20 * 60  # then stop trying and show the button again


class TorrentStatus(Enum):
    LOOKING = "looking"
    FOUND = "found"  # a torrent program other than qBittorrent
    MISSING = "missing"
    INSTALLING = "installing"
    INSTALLED = "installed"
    FAILED = "failed"  # installing failed
    SETUP_NEEDED = "setup_needed"  # qBittorrent, still with its pop-up for each download
    SETTING_UP = "setting_up"
    SETUP_WAITING = "setup_waiting"  # qBittorrent runs: waiting for it to close
    SETUP_FAILED = "setup_failed"
    READY = "ready"  # qBittorrent, set up for AirGrabber


@dataclass(frozen=True, slots=True)
class TorrentLook:
    text: str
    color: theme.Color
    install_button: bool = False
    download_link: bool = False
    setup_button: bool = False


def torrent_look(status: TorrentStatus, name: str = "") -> TorrentLook:
    """The status line under "Open in my torrent program"."""
    if status is TorrentStatus.FOUND:
        return TorrentLook(t("✓ {name} found", name=name), theme.SUCCESS)
    if status is TorrentStatus.MISSING:
        return TorrentLook(t("No torrent program found"), theme.WARNING, install_button=True)
    if status is TorrentStatus.INSTALLING:
        return TorrentLook(t("Installing… Windows may ask for permission"), theme.TEXT_MUTED)
    if status is TorrentStatus.INSTALLED:
        return TorrentLook(t("✓ qBittorrent installed"), theme.SUCCESS)
    if status is TorrentStatus.FAILED:
        return TorrentLook(
            t("Couldn't install it automatically."), theme.DANGER, download_link=True
        )
    if status is TorrentStatus.SETUP_NEEDED:
        return TorrentLook(
            t("✓ {name} found", name=QBITTORRENT), theme.SUCCESS, setup_button=True
        )
    if status is TorrentStatus.SETTING_UP:
        return TorrentLook(t("Setting up qBittorrent…"), theme.TEXT_MUTED)
    if status is TorrentStatus.SETUP_WAITING:
        return TorrentLook(
            t("Close qBittorrent to finish: AirGrabber sets it up as soon as it's closed."),
            theme.WARNING,
        )
    if status is TorrentStatus.SETUP_FAILED:
        return TorrentLook(
            t("Couldn't change qBittorrent's settings."), theme.DANGER, setup_button=True
        )
    if status is TorrentStatus.READY:
        return TorrentLook(t("✓ qBittorrent is installed and ready"), theme.SUCCESS)
    return TorrentLook("", theme.TEXT_MUTED)


def found_status(app: TorrentApp | None, qbittorrent: QBittorrentState | None) -> TorrentStatus:
    """What a look for the torrent program found: none, another program, or qBittorrent set up
    for AirGrabber or not (`qbittorrent`: its settings file; None when it couldn't be read)."""
    if app is None:
        return TorrentStatus.MISSING
    if app.name != QBITTORRENT:
        return TorrentStatus.FOUND
    if qbittorrent is not None and qbittorrent.quiet:
        return TorrentStatus.READY
    return TorrentStatus.SETUP_NEEDED


def setup_status(result: QBittorrentSetupResult) -> TorrentStatus:
    """The line after a setup try."""
    if result in (QBittorrentSetupResult.DONE, QBittorrentSetupResult.ALREADY):
        return TorrentStatus.READY
    if result is QBittorrentSetupResult.RUNNING:
        return TorrentStatus.SETUP_WAITING
    return TorrentStatus.SETUP_FAILED


def look_line(result: QBittorrentSetupResult) -> TorrentLook | None:
    """The line after switching AirGrabber's look in qBittorrent; None = "installed and ready"."""
    if result is QBittorrentSetupResult.RUNNING:
        return TorrentLook(t(LOOK_WAITING_TEXT), theme.WARNING)
    if result is QBittorrentSetupResult.FAILED:
        return TorrentLook(t("Couldn't change qBittorrent's settings."), theme.DANGER)
    return None


def _alive(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole application is gone
        return False


class TorrentAppStatus(ctk.CTkFrame):
    """The status text, plus "Install qBittorrent" when none was found (or a "Get qBittorrent"
    link when installing failed), and under it "Set up qBittorrent for AirGrabber" while
    qBittorrent still opens a pop-up for each download, or once it is set up the "AirGrabber
    look in qBittorrent" switch. `refresh()` looks for the program (again)."""

    def __init__(self, master: Any, *, torrent_app: TorrentAppService, wraplength: int) -> None:
        super().__init__(master, fg_color="transparent")
        self._torrent_app = torrent_app
        self._status = TorrentStatus.LOOKING
        self._find_task: asyncio.Task[None] | None = None
        self._install_task: asyncio.Task[None] | None = None
        self._setup_task: asyncio.Task[None] | None = None
        self._look_task: asyncio.Task[None] | None = None
        self._look: bool | None = None  # AirGrabber's look in qBittorrent; None = no switch
        self._look_wanted = False  # the switch's latest position while it is being applied
        line = ctk.CTkFrame(self, fg_color="transparent")
        line.pack(fill="x")
        self._label = text_label(line, "", size=DETAIL_SIZE, wraplength=wraplength)
        self._label.pack(side="left")
        self._install_button = secondary_button(
            line, t("Install qBittorrent"), self._install, width=INSTALL_BUTTON_WIDTH
        )
        self._get_link = link_label(line, t("Get qBittorrent"), QBITTORRENT_URL)
        self._setup_button = secondary_button(self, t(SETUP_BUTTON_TEXT), self._set_up)
        self._look_var = tk.BooleanVar(self, value=False)
        # As tall as the setup button at most, so "ready" is never taller than "set up needed"
        # (the welcome and Settings fix their size with the button showing).
        self._look_switch = ctk.CTkSwitch(
            self,
            text=t(LOOK_SWITCH_TEXT),
            variable=self._look_var,
            command=self._look_toggled,
            height=BUTTON_HEIGHT,
            font=theme.font(DETAIL_SIZE),
            progress_color=theme.ACCENT,
        )
        add_tooltip(self._look_switch, t(LOOK_TIP))

    @property
    def status(self) -> TorrentStatus:
        return self._status

    def _busy(self) -> bool:
        if self._look_task is not None and not self._look_task.done():
            return True
        return self._status in (
            TorrentStatus.INSTALLING,
            TorrentStatus.SETTING_UP,
            TorrentStatus.SETUP_WAITING,
        )

    def refresh(self) -> None:
        """Look again: a program may have been installed (or set up) meanwhile. Not while
        installing, setting up or switching the look."""
        if self._busy():
            return
        if self._find_task is not None and not self._find_task.done():
            return
        self._find_task = spawn(self._find(), name="torrent-app-find")

    async def _find(self) -> None:
        app = await self._torrent_app.find()
        state = None
        if app is not None and app.name == QBITTORRENT:
            state = await self._torrent_app.qbittorrent_state()
        status = found_status(app, state)
        look = None
        if status is TorrentStatus.READY:
            look = await self._torrent_app.qbittorrent_look()
        if not _alive(self) or self._busy():
            return
        self._look = look
        self.show(status, app.name if app is not None else "")

    def _install(self) -> None:
        if self._install_task is not None and not self._install_task.done():
            return
        self.show(TorrentStatus.INSTALLING)
        # Not cancelled with the widget: stopping winget halfway could leave a broken install.
        self._install_task = spawn(self._run_install(), name="qbittorrent-install")

    async def _run_install(self) -> None:
        installed = await self._torrent_app.install_qbittorrent()
        if not _alive(self):
            return
        if not installed:
            self.show(TorrentStatus.FAILED)
            return
        self.show(TorrentStatus.INSTALLED)
        self._set_up()  # a fresh qBittorrent: no pop-up for each download from the start

    def _set_up(self) -> None:
        if self._setup_task is not None and not self._setup_task.done():
            return
        self.show(TorrentStatus.SETTING_UP)
        self._setup_task = spawn(self._run_setup(), name="qbittorrent-setup")

    async def _run_setup(self) -> None:
        """Set qBittorrent up; while it runs, ask to close it and try again every few seconds
        (for a while; then the button shows again, for next time)."""
        loop = asyncio.get_running_loop()
        give_up_at = loop.time() + RETRY_FOR_SECONDS
        while True:
            result = await self._torrent_app.set_up_qbittorrent()
            if not _alive(self):
                return
            status = setup_status(result)
            if status is not TorrentStatus.SETUP_WAITING:
                if status is TorrentStatus.READY:  # the setup may have turned the look on
                    self._look = await self._torrent_app.qbittorrent_look()
                    if not _alive(self):
                        return
                self.show(status, QBITTORRENT)
                return
            if loop.time() >= give_up_at:
                self.show(TorrentStatus.SETUP_NEEDED, QBITTORRENT)
                return
            self.show(status, QBITTORRENT)
            await asyncio.sleep(RETRY_SECONDS)
            if not _alive(self):
                return

    def _look_toggled(self) -> None:
        """The switch moved: apply it (one change at a time; a move while one runs is picked up
        by that one)."""
        self._look_wanted = bool(self._look_var.get())
        if self._look_task is None or self._look_task.done():
            self._look_task = spawn(self._run_look(), name="qbittorrent-look")

    async def _run_look(self) -> None:
        """Change qBittorrent's look to the switch's position; while qBittorrent runs, ask to
        close it and try again every few seconds (for a while). Then the switch shows what
        qBittorrent's settings say."""
        loop = asyncio.get_running_loop()
        give_up_at = loop.time() + RETRY_FOR_SECONDS
        while True:
            wanted = self._look_wanted
            result = await self._torrent_app.set_qbittorrent_look(wanted)
            if not _alive(self):
                return
            if wanted != self._look_wanted:
                continue  # switched back meanwhile
            if result is not QBittorrentSetupResult.RUNNING or loop.time() >= give_up_at:
                break
            self._show_line(look_line(result))
            await asyncio.sleep(RETRY_SECONDS)
            if not _alive(self):
                return
        look = await self._torrent_app.qbittorrent_look()
        if not _alive(self):
            return
        self._look = look
        self.show(TorrentStatus.READY, QBITTORRENT)
        if result is QBittorrentSetupResult.FAILED:
            self._show_line(look_line(result))

    def _show_line(self, line: TorrentLook | None) -> None:
        if line is not None:
            self._label.configure(text=line.text, text_color=line.color)

    def show(self, status: TorrentStatus, name: str = "") -> None:
        self._status = status
        look = torrent_look(status, name)
        self._label.configure(text=look.text, text_color=look.color)
        if look.install_button:
            self._install_button.pack(side="right")
        else:
            self._install_button.pack_forget()
        if look.download_link:
            self._get_link.pack(side="left", padx=(12, 0))
        else:
            self._get_link.pack_forget()
        if look.setup_button:
            self._setup_button.pack(anchor="w", pady=(10, 0))
        else:
            self._setup_button.pack_forget()
        if status is TorrentStatus.READY and self._look is not None:
            self._look_var.set(self._look)
            self._look_switch.pack(anchor="w", pady=(10, 0))
        else:
            self._look_switch.pack_forget()

    def destroy(self) -> None:
        for task in (self._find_task, self._setup_task, self._look_task):
            if task is not None:
                task.cancel()
        super().destroy()
