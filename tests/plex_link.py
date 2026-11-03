"""Plex connected or not, for the tests of what you have and miss: since 3.1.0 only a connected
Plex (a token stored, app/services/plex.py) makes AirGrabber show it at all."""

from __future__ import annotations

from typing import Any

from app.database import Database
from app.models import Settings
from app.services.settings import SettingsService

TOKEN = "made-up-plex-token"


def plex_settings(db: Database, *, connected: bool = True, **values: Any) -> SettingsService:
    """A SettingsService on `db`'s settings, with a Plex token while `connected`."""
    if connected:
        values["plex_token"] = TOKEN
    return SettingsService(db.settings, Settings(**values))


async def have_marks(db: Database, show_id: int) -> frozenset[int]:
    """The ids of a show's episodes marked "I already have this" (the v4 table, kept as it
    is but no longer read by the app)."""
    rows = await db.fetch_all(
        "SELECT m.episode_id FROM have_episodes AS m "
        "JOIN episodes AS e ON e.id = m.episode_id WHERE e.show_id = ?",
        (show_id,),
    )
    return frozenset(row["episode_id"] for row in rows)


async def mark_have(db: Database, episode_ids: list[int], when: str) -> None:
    """Old "I already have this" marks, as an older AirGrabber left them (`when`: UTC ISO)."""
    async with db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO have_episodes (episode_id, marked_at) VALUES (?, ?) "
            "ON CONFLICT (episode_id) DO NOTHING",
            [(episode_id, when) for episode_id in episode_ids],
        )
