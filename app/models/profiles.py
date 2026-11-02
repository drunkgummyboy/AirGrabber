"""The people who share one AirGrabber (app/services/profiles.py): each has their own followed
series, film watchlist and calendar; everything else (settings, folders, the torrent program,
the downloads, "I already have this") is shared.

The first person is made by the database itself (v8) without a name: the UI shows them as
"Me" (translated) until they are renamed."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.models.types import UtcDatetime

FIRST_PROFILE_ID = 1  # the person the database's upgrade to v8 made; everything before is theirs
NAME_MAX = 30  # the longest name a person may have


class ProfileColour(StrEnum):
    """A person's colour (their avatar and name button), in swatch order; the UI's tokens are
    app/ui/theme.py's PERSON_COLOURS."""

    BLUE = "blue"
    PINK = "pink"
    GREEN = "green"
    COPPER = "copper"
    PURPLE = "purple"


class Profile(BaseModel):
    """A person. `name` None: the first person, never named (shown as "Me")."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str | None = None
    colour: ProfileColour = ProfileColour.BLUE
    created_at: UtcDatetime
