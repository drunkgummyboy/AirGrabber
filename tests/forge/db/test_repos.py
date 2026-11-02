from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

from app.forge.db import cache as cache_module
from app.forge.db.cache import CacheRepo
from app.forge.db.matches import MatchRepo
from app.forge.db.presets import LastPresetError, PresetRepo
from app.forge.models.match import RememberedMatch
from app.forge.models.media import MediaKind, TypeHint
from app.forge.models.preset import Preset, PresetOptions
from tests.forge.db.base import ForgeDbTestCase

DEFAULT_NAMES = ["Auto", "Movie (Folder)", "Movie (Flat)", "Series (Folder)", "Series (Flat)"]


class PresetRepoTest(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.repo = PresetRepo(self.db)

    async def names(self) -> list[str]:
        return [p.name for p in await self.repo.list()]

    async def test_seed_defaults_order(self) -> None:
        await self.repo.seed_defaults_if_empty()
        await self.repo.seed_defaults_if_empty()  # second call is a no-op
        assert await self.names() == DEFAULT_NAMES

    async def test_save_new_goes_last_and_overwrite_keeps_place(self) -> None:
        await self.repo.seed_defaults_if_empty()
        await self.repo.save(Preset(name="Mine", type=TypeHint.TV, format="{t}"))
        await self.repo.save(Preset(name="Auto", type=TypeHint.AUTO, format="{t}"))
        presets = await self.repo.list()
        assert [p.name for p in presets] == [*DEFAULT_NAMES, "Mine"]
        assert presets[0].format == "{t}"

    async def test_rename_keeps_position(self) -> None:
        await self.repo.seed_defaults_if_empty()
        renamed = Preset(
            name="Films", type=TypeHint.MOVIE, format="{ny}", options=PresetOptions(trailer=True)
        )
        await self.repo.save(renamed, old_name="Movie (Flat)")
        assert await self.names() == [
            "Auto",
            "Movie (Folder)",
            "Films",
            "Series (Folder)",
            "Series (Flat)",
        ]
        assert (await self.repo.list())[2].options.trailer

    async def test_rename_onto_an_existing_name_replaces_it(self) -> None:
        await self.repo.seed_defaults_if_empty()
        await self.repo.save(
            Preset(name="Auto", type=TypeHint.MOVIE, format="{n}"), old_name="Movie (Flat)"
        )
        presets = await self.repo.list()
        assert [p.name for p in presets] == ["Movie (Folder)", "Auto", *DEFAULT_NAMES[3:]]
        assert presets[1].format == "{n}"

    async def test_cannot_delete_last_preset(self) -> None:
        await self.repo.replace_all([Preset(name="Only", type=TypeHint.AUTO, format="{ny}")])
        with self.assertRaises(LastPresetError):
            await self.repo.delete("Only")
        assert await self.names() == ["Only"]

    async def test_delete_and_replace_all(self) -> None:
        await self.repo.seed_defaults_if_empty()
        await self.repo.delete("Auto")
        assert "Auto" not in await self.names()
        await self.repo.replace_all([Preset(name="X", type=TypeHint.AUTO, format="{t}")])
        assert await self.names() == ["X"]

    async def test_a_failed_replace_all_leaves_the_presets_as_they_were(self) -> None:
        await self.repo.seed_defaults_if_empty()
        twice = Preset(name="Twice", type=TypeHint.AUTO, format="{t}")
        with self.assertRaises(Exception):
            await self.repo.replace_all([twice, twice])  # the second insert breaks the key
        assert await self.names() == DEFAULT_NAMES

    async def test_a_reader_never_sees_half_of_replace_all(self) -> None:
        # Review finding (INTEGRATION 3.4): with one shared connection and a commit after each
        # statement, a reader could land between replace_all's DELETE and its INSERTs.
        await self.repo.seed_defaults_if_empty()
        new = [Preset(name=f"P{i}", type=TypeHint.AUTO, format="{t}") for i in range(3)]
        seen: list[list[str]] = []

        async def read() -> None:
            for _ in range(20):
                seen.append(await self.names())
                await asyncio.sleep(0)

        await asyncio.gather(self.repo.replace_all(new), read())
        assert seen and all(
            names in (DEFAULT_NAMES, ["P0", "P1", "P2"]) for names in seen
        ), seen


class MatchRepoTest(ForgeDbTestCase):
    async def test_upsert_and_none_year(self) -> None:
        repo = MatchRepo(self.db)
        no_year = RememberedMatch(
            kind=MediaKind.EPISODE, norm_title="the office", year=None, tmdb_id=2316, label="US"
        )
        with_year = RememberedMatch(
            kind=MediaKind.EPISODE, norm_title="the office", year=2005, tmdb_id=2316, label="US"
        )
        await repo.remember(no_year)
        await repo.remember(with_year)
        await repo.remember(no_year.model_copy(update={"tmdb_id": None, "label": "skip"}))
        got = await repo.get(MediaKind.EPISODE, "the office", None)
        assert got is not None and got.tmdb_id is None and got.year is None
        assert await repo.get(MediaKind.EPISODE, "the office", 2005) == with_year
        assert await repo.get(MediaKind.MOVIE, "the office", 2005) is None
        assert len(await repo.list()) == 2

    async def test_forget_and_clear(self) -> None:
        repo = MatchRepo(self.db)
        m = RememberedMatch(
            kind=MediaKind.MOVIE, norm_title="dune", year=2021, tmdb_id=1, label="x"
        )
        await repo.remember(m)
        await repo.forget(MediaKind.MOVIE, "dune", 2021)
        assert await repo.get(MediaKind.MOVIE, "dune", 2021) is None
        await repo.remember(m)
        await repo.clear()
        assert await repo.list() == []

    async def test_the_model_is_still_importable_from_the_repository(self) -> None:
        from app.forge.db import matches

        assert matches.RememberedMatch is RememberedMatch


class CacheRepoTest(ForgeDbTestCase):
    async def test_expiry(self) -> None:
        repo = CacheRepo(self.db)
        now = [1000.0]
        self.enterContext(
            mock.patch.object(cache_module, "time", SimpleNamespace(time=lambda: now[0]))
        )
        await repo.put_json("k", {"a": [1, 2]})
        assert await repo.get_json("k", max_age_s=60) == {"a": [1, 2]}
        now[0] = 1061.0
        assert await repo.get_json("k", max_age_s=60) is None
        assert await repo.purge(older_than_s=60) == 1
        assert await repo.get_json("k", max_age_s=10_000) is None

    async def test_put_overwrites(self) -> None:
        repo = CacheRepo(self.db)
        await repo.put_json("k", [1])
        await repo.put_json("k", [2])
        assert await repo.get_json("k", max_age_s=60) == [2]
        assert await self.scalar("SELECT COUNT(*) FROM tmdb_cache") == 1
