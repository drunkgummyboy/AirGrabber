"""Every disk check uses extended-length paths, so deep NAS folders work on PCs without
LongPathsEnabled, and none of the queue's runs on the loop's thread."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from unittest import mock

from app.forge.core import cleanup, fsops, planner
from app.forge.core.matcher import Resolution
from app.forge.core.queue import Queue, scan_paths
from app.forge.models.media import MediaKind, ParsedMedia, ResolvedMedia
from app.forge.models.plan import PlanInput
from app.forge.models.preset import PresetOptions
from app.models.tmdb import TMDBTitle
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase

EXTENDED = "\\\\?\\"


class LongPathsTest(ForgeTestCase):
    def record(self, module: Any, name: str) -> list[str]:
        seen: list[str] = []
        real = getattr(module, name)

        def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(str(path))
            return real(path, *args, **kwargs)

        self.enterContext(mock.patch.object(module, name, spy))
        return seen

    def test_walk_uses_extended_paths_and_returns_normal_ones(self) -> None:
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "a.mkv").write_bytes(b"v")
        walked = self.record(os, "walk")
        result = fsops.walk(self.tmp)
        assert walked and all(w.startswith(EXTENDED) for w in walked)
        assert (self.tmp / "sub", ["a.mkv"]) in result

    def test_planner_checks_existence_with_extended_paths(self) -> None:
        checked = self.record(os.path, "lexists")
        parsed = ParsedMedia(source_name="x.mkv", kind=MediaKind.MOVIE, title="Dune")
        media = ResolvedMedia.offline(parsed)
        item = PlanInput(
            item_id=1, source=self.tmp / "x.mkv", root=self.tmp, dropped_folder=None, media=media
        )
        planner.build_plan([item], "{ny}", PresetOptions())
        assert checked and all(c.startswith(EXTENDED) for c in checked)

    def test_cleanup_walks_with_extended_paths(self) -> None:
        (self.tmp / "Thumbs.db").write_bytes(b"j")
        walked = self.record(os, "walk")
        assert cleanup.find_cleanup_targets(self.tmp) == [self.tmp / "Thumbs.db"]
        assert walked and all(w.startswith(EXTENDED) for w in walked)

    def test_scan_walks_with_extended_paths_and_returns_normal_ones(self) -> None:
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "a.mkv").write_bytes(b"v")
        walked = self.record(os, "walk")
        result = scan_paths([self.tmp], recursive=True)
        assert walked and all(w.startswith(EXTENDED) for w in walked)
        assert result.files == [(self.tmp / "sub" / "a.mkv", self.tmp)]
        assert result.folders == [self.tmp]


class _Resolver:
    async def resolve(self, parsed: ParsedMedia) -> Resolution:
        return Resolution(media=ResolvedMedia.offline(parsed))

    async def apply_choice(
        self, parsed: ParsedMedia, chosen: TMDBTitle | None, *, remember: bool = True
    ) -> ResolvedMedia:
        return ResolvedMedia.offline(parsed)


class QueueThreadTest(ForgeAsyncTestCase):
    async def test_folder_checks_never_run_on_the_loop_thread(self) -> None:
        # Checking a dropped path on an unreachable share can block for the SMB timeout.
        (self.tmp / "a.mkv").write_bytes(b"v")
        threads: list[str] = []
        real = fsops.is_dir

        def spy(path: Path) -> bool:
            threads.append(threading.current_thread().name)
            return real(path)

        self.enterContext(mock.patch.object(fsops, "is_dir", spy))
        q = Queue(_Resolver(), lambda e: None)
        await q.add_paths([self.tmp, self.tmp / "a.mkv"], recursive=True)
        assert threads and "MainThread" not in threads
        assert os.path.normcase(str(q.items[0].path)) == os.path.normcase(str(self.tmp / "a.mkv"))
