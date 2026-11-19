"""Filesystem locations and logging setup."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import APP_NAME

DATA_DIR_ENV = "AIRGRABBER_DATA_DIR"


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path

    @classmethod
    def default(cls) -> AppPaths:
        """Per-user data directory, overridable with $AIRGRABBER_DATA_DIR.

        Deliberately *not* next to the scripts: that folder is synced by Google Drive, and
        sync clients lock/upload SQLite's WAL files mid-write ("database is locked").
        """
        if override := os.environ.get(DATA_DIR_ENV):
            return cls(Path(override).expanduser())
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        else:
            base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        return cls(base / APP_NAME)

    @property
    def database(self) -> Path:
        return self.root / "airgrabber.db"

    @property
    def databases(self) -> tuple[Path, ...]:
        """AirGrabber's and Forge's: what an update keeps a copy of (app/update_guard.py)."""
        return (self.database, self.forge / "forge.db")

    @property
    def posters(self) -> Path:
        return self.root / "posters"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def torrents(self) -> Path:
        """Cache of .torrent files handed to the torrent program ("open in my torrent program")."""
        return self.root / "torrents"

    @property
    def forge(self) -> Path:
        """Forge's data (forge.db, its caches). Not made by `ensure`: Forge makes it when it starts."""
        return self.root / "forge"

    def ensure(self) -> AppPaths:
        for directory in (self.root, self.posters, self.logs, self.torrents):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def store_python(prefix: str | None = None) -> bool:
    """Whether this is the Microsoft Store's Python (it lives under WindowsApps). It writes AppData
    into a private copy of its own that other programs can't see under the usual name, so
    AirGrabber's data folder and what it writes for qBittorrent stay hidden from them. install.ps1
    skips it and installs the normal Python."""
    return "\\windowsapps\\" in (sys.prefix if prefix is None else prefix).lower()


def configure_logging(paths: AppPaths, level: int = logging.INFO) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            paths.logs / "airgrabber.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    ]
    if sys.stderr is not None:  # None under pythonw.exe
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = handlers
    for noisy in ("aiosqlite", "httpx", "httpcore", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
