"""What Auto-rename leaves alone: files and folders the owner chose to ignore (forge.db `ignored`,
schema v4). Ignoring only applies to watching and Auto-rename; a file added by hand is never
refused."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class IgnoredPath(BaseModel, frozen=True):
    """One ignored file, or a folder with everything under it."""

    path: Path  # as shown
    folder: bool
    added_at: datetime  # local time, without a zone

    @property
    def name(self) -> str:
        """Its last name (what the Activity line and the log may name)."""
        return self.path.name or self.path.anchor
