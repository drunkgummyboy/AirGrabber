"""A match choice the user made, so the same title isn't asked about twice."""

from __future__ import annotations

from pydantic import BaseModel

from app.forge.models.media import MediaKind


class RememberedMatch(BaseModel, frozen=True):
    kind: MediaKind
    norm_title: str
    year: int | None
    tmdb_id: int | None  # None means "don't use TMDB for this title"
    label: str
