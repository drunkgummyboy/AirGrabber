"""A plan: exactly what a run will do, worked out before anything changes on disk."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.forge.models.media import ResolvedMedia
from app.forge.models.preset import PresetOptions
from app.forge.models.subtitles import SubtitleIdentity


class PlanInput(BaseModel, frozen=True):
    item_id: int
    source: Path
    root: Path
    dropped_folder: Path | None
    media: ResolvedMedia


class PlannedFile(BaseModel, frozen=True):
    item_id: int
    source: Path
    target: Path
    skip_reason: str | None = None


class PlannedPoster(BaseModel, frozen=True):
    kind: Literal["movie", "series", "season"]
    tmdb_path: str
    target: Path
    item_ids: tuple[int, ...] = ()  # created only if one of these files made it


class PlannedTrailer(BaseModel, frozen=True):
    query: str
    title: str
    year: int | None
    target_stem: Path
    item_ids: tuple[int, ...] = ()


class PlannedSubtitles(BaseModel, frozen=True):
    """A video that gets its subtitles once it is in its place (the languages are read then)."""

    item_id: int
    video: Path
    identity: SubtitleIdentity


class PlannedLeftovers(BaseModel, frozen=True):
    """A download's folder to clear once all of these files have moved out of it."""

    folder: Path
    item_ids: tuple[int, ...] = ()


class Plan(BaseModel, frozen=True):
    files: tuple[PlannedFile, ...]
    posters: tuple[PlannedPoster, ...] = ()
    trailers: tuple[PlannedTrailer, ...] = ()
    subtitles: tuple[PlannedSubtitles, ...] = ()
    clean_roots: tuple[Path, ...] = ()
    prune_dirs: tuple[Path, ...] = ()
    leftovers: tuple[PlannedLeftovers, ...] = ()
    options: PresetOptions = PresetOptions()
