"""ForgeSettingsService: current settings in memory, every change validated, saved in forge.db and
announced; one update at a time."""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from app.forge.core.settings import ForgeSettingsService
from app.forge.db.database import Database
from app.forge.db.settings import SettingsRepo
from app.forge.models.settings import ForgeSettings
from tests.forge.db.base import ForgeDbTestCase


class GatedRepo(SettingsRepo):
    """Holds every save until `gate` is set, to let two updates overlap."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.gate = asyncio.Event()
        self.saves = 0

    async def save(self, settings: ForgeSettings) -> None:
        self.saves += 1
        await self.gate.wait()
        await super().save(settings)


class ForgeSettingsServiceTest(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.repo = SettingsRepo(self.db)

    async def test_loads_the_defaults_when_nothing_is_stored(self) -> None:
        service = await ForgeSettingsService.load(self.repo)
        assert service.current == ForgeSettings()
        assert service.current.automatic == "off"  # renaming on its own is off unless chosen

    async def test_loads_what_is_stored(self) -> None:
        await self.repo.save(ForgeSettings(series_folder=r"C:\lib\Series", automatic="trial"))
        service = await ForgeSettingsService.load(self.repo)
        assert service.current.series_folder == r"C:\lib\Series"
        assert service.current.automatic == "trial"

    async def test_update_is_saved_and_kept(self) -> None:
        service = await ForgeSettingsService.load(self.repo)
        updated = await service.update(kept_back=[r"c:\dl\a.mkv"], notify_needs=False)
        assert updated is service.current
        assert updated.kept_back == [r"c:\dl\a.mkv"] and not updated.notify_needs
        stored = await self.repo.load()
        assert stored == updated
        assert stored.daily_summary  # the rest stays as it was

    async def test_listeners_hear_each_change_until_they_unsubscribe(self) -> None:
        service = await ForgeSettingsService.load(self.repo)
        heard: list[ForgeSettings] = []
        unsubscribe = service.subscribe(heard.append)
        await service.update(automatic="trial")
        await service.update(automatic="trial")  # no change: nothing saved or announced
        assert [s.automatic for s in heard] == ["trial"]
        unsubscribe()
        unsubscribe()  # twice is harmless
        await service.update(automatic="on")
        assert len(heard) == 1

    async def test_a_failing_listener_doesnt_stop_the_others(self) -> None:
        service = await ForgeSettingsService.load(self.repo)
        heard: list[str] = []

        def broken(settings: ForgeSettings) -> None:
            raise RuntimeError("listener bug")

        service.subscribe(broken)
        service.subscribe(lambda s: heard.append(s.automatic))
        with self.assertLogs("app.forge.core.settings", "ERROR"):
            await service.update(automatic="on")
        assert heard == ["on"]
        assert (await self.repo.load()).automatic == "on"

    async def test_a_bad_value_is_refused_and_nothing_is_saved(self) -> None:
        service = await ForgeSettingsService.load(self.repo)
        with self.assertRaises(ValidationError):
            await service.update(automatic="always")
        assert service.current.automatic == "off"
        assert await self.scalar("SELECT COUNT(*) FROM settings") == 0

    async def test_an_unknown_name_is_refused(self) -> None:
        # ForgeSettings ignores unknown keys when loading; an update must not drop a typo silently.
        service = await ForgeSettingsService.load(self.repo)
        with self.assertRaises(TypeError):
            await service.update(summary_send_on="2026-11-01")
        assert service.current == ForgeSettings()

    async def test_updates_are_serialized_and_merged(self) -> None:
        repo = GatedRepo(self.db)
        service = await ForgeSettingsService.load(repo)
        first = asyncio.create_task(service.update(kept_back=["a"]))
        second = asyncio.create_task(service.update(summary_sent_on="2026-11-01"))
        await asyncio.sleep(0.05)
        assert repo.saves == 1  # the second waits for the first to be saved
        repo.gate.set()
        await asyncio.gather(first, second)
        for settings in (service.current, await self.repo.load()):
            assert settings.kept_back == ["a"]
            assert settings.summary_sent_on == "2026-11-01"
