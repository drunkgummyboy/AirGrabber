"""The torrent-program status line ("✓ qBittorrent found" / "No torrent program found" with an
"Install qBittorrent" button), shared by the first-run welcome and Settings."""

from __future__ import annotations

import asyncio
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from typing import Any

import customtkinter as ctk

from app.services import TorrentAppService
from app.ui import theme
from app.ui.dialog_kit import DETAIL_SIZE, link_label, secondary_button, text_label
from app.ui.tasks import spawn

QBITTORRENT_URL = "https://www.qbittorrent.org/download"
INSTALL_BUTTON_WIDTH = 170


class TorrentStatus(Enum):
    LOOKING = "looking"
    FOUND = "found"
    MISSING = "missing"
    INSTALLING = "installing"
    INSTALLED = "installed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TorrentLook:
    text: str
    color: str
    install_button: bool = False
    download_link: bool = False


def torrent_look(status: TorrentStatus, name: str = "") -> TorrentLook:
    """The status line under "Open in my torrent program"."""
    if status is TorrentStatus.FOUND:
        return TorrentLook(f"✓ {name} found", theme.SUCCESS)
    if status is TorrentStatus.MISSING:
        return TorrentLook("No torrent program found", theme.WARNING, install_button=True)
    if status is TorrentStatus.INSTALLING:
        return TorrentLook("Installing… Windows may ask for permission", theme.TEXT_MUTED)
    if status is TorrentStatus.INSTALLED:
        return TorrentLook("✓ qBittorrent installed", theme.SUCCESS)
    if status is TorrentStatus.FAILED:
        return TorrentLook("Couldn't install it automatically.", theme.DANGER, download_link=True)
    return TorrentLook("", theme.TEXT_MUTED)


def _alive(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole application is gone
        return False


class TorrentAppStatus(ctk.CTkFrame):
    """One row: the status text, plus "Install qBittorrent" when none was found (or a
    "Get qBittorrent" link when installing failed). `refresh()` looks for the program (again)."""

    def __init__(self, master: Any, *, torrent_app: TorrentAppService, wraplength: int) -> None:
        super().__init__(master, fg_color="transparent")
        self._torrent_app = torrent_app
        self._status = TorrentStatus.LOOKING
        self._find_task: asyncio.Task[None] | None = None
        self._install_task: asyncio.Task[None] | None = None
        self._label = text_label(self, "", size=DETAIL_SIZE, wraplength=wraplength)
        self._label.pack(side="left")
        self._install_button = secondary_button(
            self, "Install qBittorrent", self._install, width=INSTALL_BUTTON_WIDTH
        )
        self._get_link = link_label(self, "Get qBittorrent", QBITTORRENT_URL)

    @property
    def status(self) -> TorrentStatus:
        return self._status

    def refresh(self) -> None:
        """Look again: a program may have been installed meanwhile. Not while installing."""
        if self._status in (TorrentStatus.INSTALLING, TorrentStatus.INSTALLED):
            return
        if self._find_task is not None and not self._find_task.done():
            return
        self._find_task = spawn(self._find(), name="torrent-app-find")

    async def _find(self) -> None:
        app = await self._torrent_app.find()
        if not _alive(self) or self._status is TorrentStatus.INSTALLING:
            return
        if app is None:
            self.show(TorrentStatus.MISSING)
        else:
            self.show(TorrentStatus.FOUND, app.name)

    def _install(self) -> None:
        if self._install_task is not None and not self._install_task.done():
            return
        self.show(TorrentStatus.INSTALLING)
        # Not cancelled with the widget: stopping winget halfway could leave a broken install.
        self._install_task = spawn(self._run_install(), name="qbittorrent-install")

    async def _run_install(self) -> None:
        installed = await self._torrent_app.install_qbittorrent()
        if _alive(self):
            self.show(TorrentStatus.INSTALLED if installed else TorrentStatus.FAILED)

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

    def destroy(self) -> None:
        if self._find_task is not None:
            self._find_task.cancel()
        super().destroy()
