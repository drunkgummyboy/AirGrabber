"""Settings saved by an older MediaForge still load: the library settings at their defaults,
the keys AirGrabber now owns ignored."""

from __future__ import annotations

from app.forge.db.settings import SettingsRepo
from app.forge.models.settings import ForgeSettings
from tests.forge.db.base import ForgeDbTestCase


class SettingsRepoTest(ForgeDbTestCase):
    async def test_new_settings_have_defaults_and_old_settings_still_load(self) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('app', ?)",
                ('{"tmdb_language": "nl-NL", "theme": "Light", "accent": "teal"}',),
            )
        loaded = await SettingsRepo(self.db).load()
        assert loaded.tmdb_language == "nl-NL"
        assert not hasattr(loaded, "theme") and not hasattr(loaded, "accent")
        assert loaded.watch_folders == []
        assert (loaded.series_folder, loaded.movies_folder) == ("", "")
        assert (loaded.series_preset, loaded.movies_preset) == ("Series (Folder)", "Movie (Folder)")
        assert loaded.automatic == "off"
        assert loaded.clean_downloads and loaded.notify_needs and loaded.daily_summary
        assert loaded.summary_sent_on == "" and loaded.imported_from == ""

    async def test_a_mediaforge_document_with_its_secret_key_loads_without_it(self) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('app', ?)",
                ('{"tmdb_key": "made-up-key", "auto_update": false, "scan_subfolders": false}',),
            )
        repo = SettingsRepo(self.db)
        loaded = await repo.load()
        assert loaded.scan_subfolders is False
        assert "made-up-key" not in loaded.model_dump_json()
        await repo.save(loaded)  # saving drops the keys Forge doesn't keep
        stored = await self.scalar("SELECT value FROM settings WHERE key = 'app'")
        assert isinstance(stored, str) and "tmdb_key" not in stored

    async def test_roundtrip_and_defaults(self) -> None:
        repo = SettingsRepo(self.db)
        assert await repo.load() == ForgeSettings()
        changed = ForgeSettings(
            tmdb_language="nl-NL",
            scan_subfolders=False,
            context_menu_presets=["Auto"],
            automatic="trial",
            imported_from="mediaforge 1.3",
        )
        await repo.save(changed)
        assert await repo.load() == changed
        await repo.save(changed.model_copy(update={"automatic": "on"}))
        assert (await repo.load()).automatic == "on"
        assert await self.scalar("SELECT COUNT(*) FROM settings") == 1
