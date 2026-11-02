"""The right-click batcher (app/forge/core/batcher.py), ported from the ConfirmBatcher cases of
MediaForge's tests/platform/test_single_instance.py: requests gathered until it's quiet, one batch
per preset; plus the loop's own timer, the same path twice, and closing."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from app.forge.core.batcher import QUIET_S, ConfirmBatcher

Batch = tuple[str | None, list[Path]]


class BatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.batches: list[Batch] = []
        self.batcher = ConfirmBatcher(lambda preset, paths: self.batches.append((preset, paths)))

    def test_waits_for_quiet_then_one_batch_per_preset(self) -> None:
        b = self.batcher
        b.add(["a.mkv"], "Auto", now=10.0)
        b.add(["b.mkv"], "Auto", now=10.2)
        b.add(["c.mkv"], "Series (Flat)", now=10.3)
        b.flush_due(now=10.5)
        self.assertEqual(self.batches, [])
        b.flush_due(now=10.8)
        self.assertEqual(
            self.batches,
            [("Auto", [Path("a.mkv"), Path("b.mkv")]), ("Series (Flat)", [Path("c.mkv")])],
        )
        b.flush_due(now=20.0)
        self.assertEqual(len(self.batches), 2)  # handed over once

    def test_the_default_quiet_time_is_400_ms(self) -> None:
        self.assertAlmostEqual(QUIET_S, 0.4)

    def test_the_same_path_twice_is_kept_once(self) -> None:
        b = self.batcher
        b.add(["C:/Films/Dune.2021.mkv"], None, now=1.0)
        b.add(["c:\\films\\dune.2021.mkv", "C:/Films/Heat.1995.mkv"], None, now=1.1)
        self.assertEqual(b.pending, 2)
        b.flush_due(now=2.0)
        [(preset, paths)] = self.batches
        self.assertIsNone(preset)
        self.assertEqual([p.name for p in paths], ["Dune.2021.mkv", "Heat.1995.mkv"])

    def test_the_same_path_for_two_presets_goes_to_both(self) -> None:
        b = self.batcher
        b.add(["a.mkv"], "Auto", now=1.0)
        b.add(["a.mkv"], "Movie (Flat)", now=1.0)
        b.flush_due(now=2.0)
        self.assertEqual([preset for preset, _ in self.batches], ["Auto", "Movie (Flat)"])

    def test_a_path_may_come_again_in_a_later_batch(self) -> None:
        b = self.batcher
        b.add(["a.mkv"], "Auto", now=1.0)
        b.flush_due(now=2.0)
        b.add(["a.mkv"], "Auto", now=3.0)
        b.flush_due(now=4.0)
        self.assertEqual(len(self.batches), 2)

    def test_an_empty_request_opens_nothing(self) -> None:
        self.batcher.add([], "Auto", now=1.0)
        self.batcher.flush_due(now=5.0)
        self.assertEqual(self.batches, [])

    def test_a_failing_handler_does_not_stop_the_other_presets(self) -> None:
        seen: list[str | None] = []

        def handler(preset: str | None, paths: list[Path]) -> None:
            seen.append(preset)
            if preset == "Auto":
                raise RuntimeError("boom")

        b = ConfirmBatcher(handler)
        b.add(["a.mkv"], "Auto", now=1.0)
        b.add(["b.mkv"], "Movie (Flat)", now=1.0)
        with self.assertLogs("app.forge.core.batcher", "ERROR"):
            b.flush_due(now=2.0)
        self.assertEqual(seen, ["Auto", "Movie (Flat)"])

    def test_the_injected_clock_is_used(self) -> None:
        now = [100.0]
        b = ConfirmBatcher(lambda p, paths: self.batches.append((p, paths)), clock=lambda: now[0])
        b.add(["a.mkv"], None)
        b.flush_due()
        self.assertEqual(self.batches, [])
        now[0] = 100.5
        b.flush_due()
        self.assertEqual(len(self.batches), 1)


class BatcherOnTheLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_hundred_requests_become_one_batch_after_the_quiet_time(self) -> None:
        batches: list[Batch] = []
        b = ConfirmBatcher(lambda preset, paths: batches.append((preset, paths)), quiet_s=0.05)
        for n in range(200):
            b.add([f"Movie.{n}.mkv"], "Movie (Flat)")
            if n % 50 == 0:
                await asyncio.sleep(0.01)  # arriving over a while, never quiet for 50 ms
        self.assertEqual(batches, [])
        await asyncio.sleep(0.15)
        [(preset, paths)] = batches
        self.assertEqual(preset, "Movie (Flat)")
        self.assertEqual(len(paths), 200)
        self.assertEqual(b.pending, 0)

    async def test_close_drops_what_waits(self) -> None:
        batches: list[Batch] = []
        b = ConfirmBatcher(lambda preset, paths: batches.append((preset, paths)), quiet_s=0.02)
        b.add(["a.mkv"], None)
        b.close()
        b.add(["b.mkv"], None)  # taken no more
        await asyncio.sleep(0.08)
        self.assertEqual(batches, [])
        self.assertEqual(b.pending, 0)


if __name__ == "__main__":
    unittest.main()
