"""PresetService tells its listeners after every change to the presets (the queue's preset
bar reads them again), never before and never for a read."""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from typing import cast

from app.forge.core.presets_service import PresetService
from app.forge.db.presets import PresetRepo
from app.forge.models.media import TypeHint
from app.forge.models.preset import Preset


class _Repo:
    def __init__(self) -> None:
        self.stored: list[Preset] = []

    async def list(self) -> list[Preset]:
        return list(self.stored)

    async def save(self, preset: Preset, *, old_name: str | None = None) -> None:
        self.stored.append(preset)

    async def delete(self, name: str) -> None:
        self.stored = [p for p in self.stored if p.name != name]

    async def replace_all(self, presets: Sequence[Preset]) -> None:
        self.stored = list(presets)


class PresetServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repo = _Repo()
        self.service = PresetService(cast(PresetRepo, self.repo))
        self.heard: list[int] = []

    def listen(self) -> None:
        self.heard.append(len(self.repo.stored))  # what is stored when it is told

    async def test_every_change_is_told_after_it_is_stored(self) -> None:
        unsubscribe = self.service.subscribe(self.listen)
        preset = Preset(name="Mine", type=TypeHint.AUTO, format="{ny}")
        await self.service.save(preset)
        await self.service.replace_all([preset, Preset(name="Other", type=TypeHint.AUTO, format="{ny}")])
        await self.service.delete("Other")
        await self.service.list()  # a read says nothing
        self.assertEqual(self.heard, [1, 2, 1])
        unsubscribe()
        unsubscribe()  # twice is fine
        await self.service.save(Preset(name="Later", type=TypeHint.AUTO, format="{ny}"))
        self.assertEqual(self.heard, [1, 2, 1])

    async def test_a_failing_listener_is_logged_and_the_others_still_hear(self) -> None:
        def broken() -> None:
            raise RuntimeError("boom")

        self.service.subscribe(broken)
        self.service.subscribe(self.listen)
        with self.assertLogs("app.forge.core.presets_service", "ERROR"):
            await self.service.save(Preset(name="Mine", type=TypeHint.AUTO, format="{ny}"))
        self.assertEqual(self.heard, [1])


if __name__ == "__main__":
    unittest.main()
