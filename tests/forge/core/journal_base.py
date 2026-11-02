"""Shared set-up for the executor, undo and recovery tests: a fresh forge.db with its history
repository (MediaForge's `history` fixture), fake posters and movie plan inputs. All files live
in the test's own temporary folder."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.forge.core.executor import Executor, PosterFetcher, ProgressEvent, RunResult
from app.forge.core.executor import TrailerDownloader
from app.forge.db.history import HistoryRepo
from app.forge.models.history import RunSource
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.forge.models.plan import Plan, PlanInput
from tests.forge.db.base import ForgeDbTestCase


class FakePosters:
    def __init__(self, data: bytes = b"jpeg-bytes") -> None:
        self.data = data
        self.calls: list[tuple[str, str]] = []

    async def image(self, path: str, size: str) -> bytes:
        self.calls.append((path, size))
        return self.data


def film(title: str, year: int = 2024) -> ResolvedMedia:
    parsed = ParsedMedia(source_name="m.mkv", kind=MediaKind.MOVIE, title=title)
    return ResolvedMedia(
        parsed=parsed,
        title=title,
        year=year,
        episode_title=None,
        match_state=MatchState.CONFIDENT,
        poster_path="/p.jpg",
    )


def make_inputs(root: Path, names: dict[str, str]) -> list[PlanInput]:
    """A small video file per name under `root`, each planned as the movie named beside it."""
    inputs: list[PlanInput] = []
    for i, (filename, title) in enumerate(names.items(), start=1):
        src = root / filename
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"video")
        inputs.append(
            PlanInput(item_id=i, source=src, root=root, dropped_folder=root, media=film(title))
        )
    return inputs


class HistoryTestCase(ForgeDbTestCase):
    history: HistoryRepo

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.history = HistoryRepo(self.db)

    async def execute(
        self,
        plan: Plan,
        events: list[ProgressEvent] | None = None,
        cancel: asyncio.Event | None = None,
        posters: PosterFetcher | None = None,
        trailers: TrailerDownloader | None = None,
        preset_name: str | None = "Test",
    ) -> RunResult:
        executor = Executor(self.history, posters or FakePosters(), trailers)
        sink: list[ProgressEvent] = events if events is not None else []
        return await executor.run(
            plan,
            source=RunSource.APP,
            preset_name=preset_name,
            on_progress=sink.append,
            cancel=cancel or asyncio.Event(),
        )
