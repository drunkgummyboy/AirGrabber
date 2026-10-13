"""SettingsService: concurrent updates are serialized, so none undoes another."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.database import Database, SettingsRepository
from app.models import QualityProfile, Settings, Weekday
from app.services import SettingsService


class GatedRepository(SettingsRepository):
    """Saves for real, but the first save waits for `gate` first (a slow disk)."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.gate = asyncio.Event()
        self.saves: list[Settings] = []

    async def save(self, settings: Settings) -> None:
        self.saves.append(settings)
        if len(self.saves) == 1:
            await self.gate.wait()
        await super().save(settings)


class ConcurrentUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = await Database.open(Path(self.tmp.name) / "settings.db")
        self.addAsyncCleanup(self.db.close)

    async def test_overlapping_updates_both_survive(self) -> None:
        repository = GatedRepository(self.db)
        service = await SettingsService.load(repository)
        notified: list[Settings] = []
        service.subscribe(notified.append)

        first = asyncio.create_task(service.update(quality="720p"))
        await asyncio.sleep(0.01)  # the first update is now waiting inside save()
        second = asyncio.create_task(service.update(first_day="Sunday", setup_done=True))
        await asyncio.sleep(0.01)
        self.assertEqual(len(repository.saves), 1)  # the second waits for the first
        repository.gate.set()
        await asyncio.gather(first, second)

        for settings in (service.current, await SettingsRepository(self.db).load()):
            with self.subTest(settings=settings):
                self.assertIs(settings.quality, QualityProfile.HD)
                self.assertIs(settings.first_day, Weekday.SUNDAY)
                self.assertTrue(settings.setup_done)
        # Listeners saw each change, in order, the second including the first.
        self.assertEqual([s.quality for s in notified], [QualityProfile.HD, QualityProfile.HD])
        self.assertEqual([s.first_day for s in notified], [Weekday.MONDAY, Weekday.SUNDAY])

    async def test_a_failed_update_does_not_block_the_next(self) -> None:
        service = await SettingsService.load(SettingsRepository(self.db))
        with self.assertRaises(ValueError):  # pydantic.ValidationError is a ValueError
            await service.update(weeks_to_show=99)
        await service.update(setup_done=True)
        self.assertTrue(service.current.setup_done)
        self.assertEqual(service.current.weeks_to_show, Settings().weeks_to_show)


if __name__ == "__main__":
    unittest.main()
