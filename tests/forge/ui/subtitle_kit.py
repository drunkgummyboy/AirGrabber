"""A stand-in for ForgeSubtitles (app/forge/core/subtitles.py), for the tests of what shows it."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.forge.models.subtitles import (
    SubtitleAccount,
    SubtitleFetch,
    SubtitleFetchOutcome,
    SubtitleIdentity,
    SubtitleQuota,
)

RESET = datetime(2099, 1, 1, tzinfo=UTC)


class FakeSubtitleSource:
    def __init__(
        self, *, available: bool = True, languages: tuple[str, ...] = ("en", "nl")
    ) -> None:
        self.available = available
        self.languages = languages
        self.used_up = False
        self.quota = SubtitleQuota(reset_at=RESET)
        self.result: SubtitleFetch | None = None
        self.asked: list[tuple[Path, SubtitleIdentity, tuple[str, ...], int | None]] = []
        self.sign_ins: list[tuple[str, str]] = []
        self.checked: list[str] = []
        self.sign_in_error: Exception | None = None
        self.key_error: Exception | None = None

    async def fetch(
        self,
        video: Path,
        identity: SubtitleIdentity,
        languages: Sequence[str] | None = None,
        *,
        run_id: int | None = None,
    ) -> SubtitleFetch:
        wanted = tuple(self.languages if languages is None else languages)
        self.asked.append((video, identity, wanted, run_id))
        if self.result is not None:
            return self.result
        return SubtitleFetch(outcome=SubtitleFetchOutcome.SAVED, saved=wanted)

    async def missing(self, video: Path) -> tuple[str, ...]:
        return self.languages

    async def sign_in(self, username: str, password: str) -> SubtitleAccount:
        self.sign_ins.append((username, password))
        if self.sign_in_error is not None:
            raise self.sign_in_error
        return SubtitleAccount(allowed_downloads=20, remaining_downloads=18)

    async def check_key(self, key: str) -> None:
        self.checked.append(key)
        if self.key_error is not None:
            raise self.key_error
