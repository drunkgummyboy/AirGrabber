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
    def posters(self) -> Path:
        return self.root / "posters"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> AppPaths:
        for directory in (self.root, self.posters, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
        return self


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
