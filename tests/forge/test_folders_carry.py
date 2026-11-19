"""Folders, set once (app/forge/core/folders_carry.py): MediaForge's Series / Movies folders are
added to AirGrabber's folder lists once, at Forge's start; nothing is taken out, the folder
library isn't switched, an empty destination adds nothing, and a second run does nothing."""

from __future__ import annotations

import asyncio
from typing import Any

from app.forge.core.folders_carry import carry_library_folders, same_folder
from app.forge.models.settings import ForgeSettings
from app.models import Settings
from app.services.settings import SettingsService
from tests.forge.base import ForgeAsyncTestCase


class FakeForgeSettings:
    """ForgeSettingsService's surface: current, update."""

    def __init__(self, current: ForgeSettings) -> None:
        self.current = current
        self.updates: list[dict[str, Any]] = []

    async def update(self, **changes: Any) -> ForgeSettings:
        await asyncio.sleep(0)
        self.updates.append(changes)
        self.current = ForgeSettings.model_validate({**self.current.model_dump(), **changes})
        return self.current


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[Settings] = []

    async def save(self, settings: Settings) -> None:
        self.saved.append(settings)


def services(
    forge: ForgeSettings, airgrabber: Settings
) -> tuple[FakeForgeSettings, SettingsService, FakeRepository]:
    repository = FakeRepository()
    return FakeForgeSettings(forge), SettingsService(repository, airgrabber), repository  # type: ignore[arg-type]


class CarryTests(ForgeAsyncTestCase):
    async def test_carry_appends_missing_destinations_once(self) -> None:
        forge, airgrabber, repository = services(
            ForgeSettings(series_folder=r"\\NAS\Video\Series", movies_folder=r"D:\Films"),
            Settings(series_folders=(r"E:\Old series",), film_folders=("d:/films/",)),
        )
        added = await carry_library_folders(forge, airgrabber)  # type: ignore[arg-type]
        self.assertTrue(added)  # AirGrabber's start has the folders checked soon
        # The Series folder is new to its list: last; the Movies folder is there already.
        self.assertEqual(
            airgrabber.current.series_folders, (r"E:\Old series", r"\\NAS\Video\Series")
        )
        self.assertEqual(airgrabber.current.film_folders, ("d:/films/",))
        self.assertTrue(forge.current.folders_carried)
        self.assertEqual(len(repository.saved), 1)  # one store for both lists
        # Once: the folder taken out of the list later stays out.
        await airgrabber.update(series_folders=(r"E:\Old series",))
        self.assertFalse(await carry_library_folders(forge, airgrabber))  # type: ignore[arg-type]
        self.assertEqual(airgrabber.current.series_folders, (r"E:\Old series",))
        self.assertEqual(forge.updates, [{"folders_carried": True}])

    async def test_carry_never_removes_or_switches_counting(self) -> None:
        for on in (False, True):
            with self.subTest(folder_library_on=on):
                forge, airgrabber, _repository = services(
                    ForgeSettings(series_folder=r"S:\Series", movies_folder=r"M:\Movies"),
                    Settings(
                        folder_library_on=on,
                        series_folders=(r"A:\One", r"B:\Two"),
                        film_folders=(r"C:\Three",),
                    ),
                )
                await carry_library_folders(forge, airgrabber)  # type: ignore[arg-type]
                current = airgrabber.current
                self.assertEqual(current.folder_library_on, on)
                self.assertEqual(current.series_folders, (r"A:\One", r"B:\Two", r"S:\Series"))
                self.assertEqual(current.film_folders, (r"C:\Three", r"M:\Movies"))
                # The destinations themselves are left as they were.
                self.assertEqual(forge.current.series_folder, r"S:\Series")
                self.assertEqual(forge.current.movies_folder, r"M:\Movies")

    async def test_carry_skips_empty_destinations(self) -> None:
        forge, airgrabber, repository = services(
            ForgeSettings(series_folder="", movies_folder="  "), Settings()
        )
        self.assertFalse(await carry_library_folders(forge, airgrabber))  # type: ignore[arg-type]
        self.assertEqual((airgrabber.current.series_folders, airgrabber.current.film_folders), ((), ()))
        self.assertEqual(repository.saved, [])  # nothing of AirGrabber's stored
        self.assertTrue(forge.current.folders_carried)

    async def test_a_failure_is_tried_again_next_start(self) -> None:
        forge, airgrabber, _repository = services(
            ForgeSettings(series_folder=r"S:\Series"), Settings()
        )

        async def broken(**_changes: Any) -> Settings:
            raise OSError(r"S:\Series is gone")

        airgrabber.update = broken  # type: ignore[method-assign]
        with self.assertLogs("app.forge.core.folders_carry", "WARNING") as logged:
            added = await carry_library_folders(forge, airgrabber)  # type: ignore[arg-type]
        self.assertFalse(added)
        self.assertFalse(forge.current.folders_carried)
        self.assertNotIn("Series", logged.output[0])  # the type only, never a folder

    def test_a_carried_folder_is_checked_soon(self) -> None:
        # Final review Minor 3: AirGrabber's start (app/services) asks the folder library, never
        # Forge's core; check_soon itself does nothing while the folders don't count.
        from types import SimpleNamespace

        from app.services import Services

        for added in (True, False):
            with self.subTest(added=added):
                calls: list[str] = []
                folders = SimpleNamespace(
                    attach=lambda _moves: calls.append("attach"),
                    check_soon=lambda: calls.append("check_soon"),
                )
                started = SimpleNamespace(moves=object(), library_folders_added=added)
                Services._forge_ready_for_folders(
                    SimpleNamespace(folder_library=folders), started  # type: ignore[arg-type]
                )
                self.assertEqual(calls, ["attach", "check_soon"] if added else ["attach"])

    def test_same_folder(self) -> None:
        self.assertTrue(same_folder(r"D:\Films", "d:/films/"))
        self.assertTrue(same_folder(r"\\NAS\Video", r"\\nas\video\\"))
        self.assertFalse(same_folder(r"D:\Films", r"D:\Films2"))
