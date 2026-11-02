"""Forge's move records: where Forge moved a video into the Series or Movies library folder from,
kept in forge.db (`moves`) for a year, longer than the 50 runs of undo history. AirGrabber's
Downloads page and subtitles read them (where a finished download ended up)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app.forge.models.media import MediaKind

MOVES_KEPT_S = 365 * 24 * 3600  # move records are kept for a year


class MoveNote(BaseModel, frozen=True):
    """A rename into a library folder, recorded in the same journal step as the rename: what
    the file is (`kind`; `tmdb_id` when the caller's queue knew it)."""

    source: Path
    target: Path
    kind: MediaKind
    tmdb_id: int | None = None  # the film's, or the series' TMDB id


class MoveRecord(BaseModel, frozen=True):
    """One move into a library folder (kept for a year)."""

    source: Path
    target: Path
    moved_at: datetime  # local time, without a zone
    kind: MediaKind
    tmdb_id: int | None
    run_id: int | None
