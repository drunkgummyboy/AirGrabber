"""The file check: a renamed video's audio languages and picture, read with ffprobe (faked)."""

from __future__ import annotations

import asyncio
import json
import logging
import unittest
from pathlib import Path
from typing import Any

from app.forge.core.activity import ActivityFeed
from app.forge.core.executor import Executor
from app.forge.core.file_check import (
    AudioTrack,
    FileCheck,
    Probe,
    findings,
    promised_height,
    read_probe,
)
from app.forge.core.planner import build_plan
from app.forge.models.history import RunSource
from app.forge.models.preset import PresetOptions
from tests.forge.core.journal_base import FakePosters, HistoryTestCase, make_inputs


def probe_json(height: int, width: int, *audio: tuple[str | None, bool]) -> str:
    streams: list[dict[str, Any]] = [{"codec_type": "video", "width": width, "height": height}]
    for language, default in audio:
        stream: dict[str, Any] = {"codec_type": "audio", "disposition": {"default": int(default)}}
        if language is not None:
            stream["tags"] = {"language": language}
        streams.append(stream)
    return json.dumps({"streams": streams})


class ReadTests(unittest.TestCase):
    def test_the_probe(self) -> None:
        probe = read_probe(probe_json(1080, 1920, ("rus", True), ("eng", False), (None, False)))
        assert probe is not None
        self.assertEqual(probe.height, 1080)
        self.assertEqual(
            probe.audio,
            (AudioTrack("ru", True), AudioTrack("en", False), AudioTrack("", False)),
        )
        self.assertIsNone(read_probe("not json"))
        self.assertEqual(read_probe("{}"), Probe(height=None, audio=()))

    def test_a_wide_film_counts_by_its_width(self) -> None:
        probe = read_probe(probe_json(800, 1920))
        assert probe is not None
        self.assertEqual(probe.height, 1080)

    def test_the_promised_resolution(self) -> None:
        self.assertEqual(promised_height("Show.S01E01.1080p.WEB.h264-GRP.mkv"), 1080)
        self.assertEqual(promised_height("Film 2024 2160p UHD"), 2160)
        self.assertIsNone(promised_height("Show.S01E01.WEB.h264-GRP.mkv"))
        self.assertIsNone(promised_height("Show.S01E01.10800p"))


class FindingTests(unittest.TestCase):
    def find(
        self, probe: Probe, name: str = "Show.S01E01.1080p.mkv", understood: tuple[str, ...] = ("nl",)
    ) -> list[str]:
        return findings(probe, name, understood, "Show S01E01.mkv")

    def test_audio_nobody_understands(self) -> None:
        probe = Probe(1080, (AudioTrack("ru", True),))
        self.assertEqual(self.find(probe), ["Show S01E01.mkv: the audio is Russian"])

    def test_a_dub_that_plays_first(self) -> None:
        probe = Probe(1080, (AudioTrack("ru", True), AudioTrack("en", False)))
        self.assertEqual(
            self.find(probe), ["Show S01E01.mkv: plays Russian first; English is there too"]
        )

    def test_fine_audio(self) -> None:
        self.assertEqual(self.find(Probe(1080, (AudioTrack("en", True),))), [])
        self.assertEqual(self.find(Probe(1080, (AudioTrack("nl", True),))), [])  # understood
        self.assertEqual(self.find(Probe(1080, (AudioTrack("", True),))), [])  # says nothing
        self.assertEqual(self.find(Probe(1080, ())), [])

    def test_a_smaller_picture(self) -> None:
        self.assertEqual(
            self.find(Probe(720, ())), ["Show S01E01.mkv: says 1080p, but the picture is 720p"]
        )
        self.assertEqual(self.find(Probe(1036, ())), [])  # a little cropped is still 1080p
        self.assertEqual(self.find(Probe(720, ()), name="Show.S01E01.WEB.mkv"), [])


class FakeCheck:
    def __init__(self, found: list[str]) -> None:
        self.found = found
        self.calls: list[tuple[str, str]] = []

    async def check(self, video: Path, release_name: str) -> list[str]:
        self.calls.append((video.name, release_name))
        return self.found


class BrokenCheck:
    async def check(self, video: Path, release_name: str) -> list[str]:
        raise RuntimeError("boom")


class ExecutorCheckTests(HistoryTestCase):
    async def run_with(self, checker: object) -> tuple[int, list[str]]:
        inputs = make_inputs(self.tmp, {"x.1080p.mkv": "Dune"})
        plan = build_plan(inputs, "{ny}", PresetOptions())
        activity = ActivityFeed()
        executor = Executor(
            self.history, FakePosters(), None, activity=activity, checker=checker  # type: ignore[arg-type]
        )
        result = await executor.run(
            plan,
            source=RunSource.APP,
            preset_name=None,
            on_progress=lambda _event: None,
            cancel=asyncio.Event(),
        )
        self.assertEqual(result.renamed, 1)
        warnings = [line.text for line in activity.lines() if line.level >= logging.WARNING]
        return result.warnings, warnings

    async def test_each_finding_is_a_warning(self) -> None:
        checker = FakeCheck(["Dune (2024).mkv: the audio is Russian"])
        count, warnings = await self.run_with(checker)
        self.assertEqual(checker.calls, [("Dune (2024).mkv", "x.1080p.mkv")])
        self.assertEqual(count, 1)
        self.assertEqual(warnings, ["Dune (2024).mkv: the audio is Russian"])

    async def test_a_broken_check_never_fails_the_rename(self) -> None:
        count, warnings = await self.run_with(BrokenCheck())
        self.assertEqual((count, warnings), (0, []))


class NoToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_ffprobe_nothing_is_checked(self) -> None:
        check = FileCheck(Path(self.id()) / "missing-tools", lambda: ("en",))
        self.assertFalse(check.available())
        self.assertEqual(await check.check(Path("Dune.mkv"), "Dune.1080p.mkv"), [])


if __name__ == "__main__":
    unittest.main()
