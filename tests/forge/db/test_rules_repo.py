"""The rules for watched folders in forge.db (app/forge/db/rules.py, schema v5) and their model
(app/forge/models/rules.py)."""

from __future__ import annotations

import sqlite3

from app.forge.db.rules import RuleRepo
from app.forge.models.media import MediaKind
from app.forge.models.rules import Rule, RuleTarget
from tests.forge.base import ForgeTestCase
from tests.forge.db.base import ForgeDbTestCase


class RuleRepoTest(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.repo = RuleRepo(self.db)

    async def test_replace_all_keeps_order_ids_and_targets(self) -> None:
        a = Rule(
            folder=r"D:\Downloads",
            take="films",
            quality="4k",
            films=RuleTarget(
                preset="Movie (Folder)",
                extras={"trailer": False},
                to="folder",
                folder=r"\\NAS\Movies 4K",
            ),
        )
        b = Rule(airgrabber=True)
        stored = await self.repo.replace_all([a, b])
        self.assertEqual([r.id > 0 for r in stored], [True, True])
        self.assertEqual(await self.repo.list(), stored)
        self.assertEqual(stored[0].films.extras, {"trailer": False})
        self.assertEqual(stored[0].films.folder, r"\\NAS\Movies 4K")
        again = await self.repo.replace_all([stored[1], stored[0]])
        self.assertEqual([r.id for r in again], [stored[1].id, stored[0].id])  # ids kept
        self.assertEqual(await self.repo.list(), again)  # the order is the list's

    async def test_every_field_comes_back(self) -> None:
        rule = Rule(
            on=False,
            name="4K films",
            folder=r"E:\Torrents",
            subfolders=False,
            take="shows",
            quality="not_4k",
            stays=True,
            shows=RuleTarget(preset="Series (Flat)", extras={"nfo": True, "subtitles": False}),
            mode="auto",
            fresh=True,
        )
        [stored] = await self.repo.replace_all([rule])
        self.assertEqual(stored, rule.model_copy(update={"id": stored.id}))
        self.assertEqual(await self.repo.list(), [stored])

    async def test_a_rule_left_out_is_deleted(self) -> None:
        stored = await self.repo.replace_all([Rule(folder=r"D:\a"), Rule(folder=r"D:\b")])
        await self.repo.replace_all([stored[0]])
        self.assertEqual(await self.repo.list(), [stored[0]])
        self.assertEqual(await self.repo.replace_all([]), [])
        self.assertEqual(await self.repo.list(), [])

    async def test_only_one_airgrabber_rule(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            await self.repo.replace_all([Rule(airgrabber=True), Rule(airgrabber=True)])
        self.assertEqual(await self.repo.list(), [])  # nothing stored

    async def test_set_fresh(self) -> None:
        stored = await self.repo.replace_all([Rule(folder="a"), Rule(folder="b")])
        await self.repo.set_fresh([stored[1].id, 999], True)
        self.assertEqual([r.fresh for r in await self.repo.list()], [False, True])
        await self.repo.set_fresh([], False)  # nothing to do
        await self.repo.set_fresh([stored[1].id], False)
        self.assertEqual([r.fresh for r in await self.repo.list()], [False, False])

    async def test_rename_preset_follows_both_kinds(self) -> None:
        both = Rule(folder="a", shows=RuleTarget(preset="Movie (Folder)"))
        other = Rule(folder="b", films=RuleTarget(preset="Movie (Flat)"))
        await self.repo.replace_all([both, other])
        self.assertEqual(await self.repo.rename_preset("Movie (Folder)", "Film"), 1)
        first, second = await self.repo.list()
        self.assertEqual((first.shows.preset, first.films.preset), ("Film", "Film"))
        self.assertEqual(
            (second.shows.preset, second.films.preset), ("Series (Folder)", "Movie (Flat)")
        )
        self.assertEqual(await self.repo.rename_preset("Nobody's", "X"), 0)

    async def test_rename_presets_swaps_in_one_go(self) -> None:
        a = Rule(folder="a", shows=RuleTarget(preset="A"), films=RuleTarget(preset="B"))
        b = Rule(folder="b", shows=RuleTarget(preset="C"), films=RuleTarget(preset="A"))
        c = Rule(folder="c", shows=RuleTarget(preset="X"), films=RuleTarget(preset="Y"))
        await self.repo.replace_all([a, b, c])
        self.assertEqual(await self.repo.rename_presets({"A": "B", "B": "A", "C": "C"}), 2)
        self.assertEqual(
            [(r.shows.preset, r.films.preset) for r in await self.repo.list()],
            [("B", "A"), ("C", "B"), ("X", "Y")],
        )
        self.assertEqual(await self.repo.rename_presets({}), 0)


class RuleModelTest(ForgeTestCase):
    def test_unknown_extras_are_dropped(self) -> None:
        target = RuleTarget(preset="x", extras={"clean_junk": True, "nfo": True})
        self.assertEqual(target.extras, {"nfo": True})
        target = RuleTarget(preset="x", extras={"move_to_library": True, "prune_empty": True})
        self.assertEqual(target.extras, {})

    def test_target_and_kinds(self) -> None:
        rule = Rule(shows=RuleTarget(preset="S"), films=RuleTarget(preset="F"))
        self.assertEqual(rule.target(MediaKind.EPISODE).preset, "S")
        self.assertEqual(rule.target(MediaKind.MOVIE).preset, "F")
        self.assertEqual(rule.kinds(), (MediaKind.EPISODE, MediaKind.MOVIE))
        self.assertEqual(Rule(take="shows").kinds(), (MediaKind.EPISODE,))
        self.assertEqual(Rule(take="films").kinds(), (MediaKind.MOVIE,))

    def test_same_routing_leaves_out_id_name_and_fresh(self) -> None:
        rule = Rule(folder="a", mode="auto")
        renamed = rule.model_copy(update={"id": 4, "name": "x", "fresh": True})
        self.assertTrue(rule.same_routing(renamed))
        self.assertFalse(rule.same_routing(rule.model_copy(update={"mode": "manual"})))
        self.assertFalse(rule.same_routing(rule.model_copy(update={"on": False})))
        elsewhere = RuleTarget(preset="Movie (Folder)", to="folder", folder="x")
        moved = rule.model_copy(update={"films": elsewhere})
        self.assertFalse(rule.same_routing(moved))
