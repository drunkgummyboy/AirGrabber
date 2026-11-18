"""RuleService (app/forge/core/rules_service.py) on a real forge.db: today's setup carried over
into rules once, AirGrabber's rule made sure of and switched by its setting, the fresh flags, the
rules following a renamed preset, listeners, and logs that name no folder."""

from __future__ import annotations

import asyncio

from app.forge.core.rules import rules_from_settings
from app.forge.core.rules_service import RuleService
from app.forge.core.settings import ForgeSettingsService
from app.forge.db.rules import RuleRepo
from app.forge.db.settings import SettingsRepo
from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import ForgeSettings
from tests.forge.db.base import ForgeDbTestCase


class RuleServiceTest(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.settings = await ForgeSettingsService.load(SettingsRepo(self.db))
        self.service = RuleService(RuleRepo(self.db), self.settings)

    async def looked(self) -> None:
        """The carried rules had their first look (the carry-over stores them fresh)."""
        await self.service.first_look_done([rule.id for rule in self.service.current])

    async def test_todays_setup_becomes_rules_once(self) -> None:
        await self.settings.update(
            watch_folders=[r"D:\Downloads", r"E:\Torrents"],
            series_preset="Series (Flat)",
            automatic="on",
            use_airgrabber_downloads=True,
        )
        await self.service.load()
        owners, ag = self.service.current[:2], self.service.current[2]
        self.assertEqual([r.folder for r in owners], [r"D:\Downloads", r"E:\Torrents"])
        self.assertTrue(
            all(
                r.take == "anything" and r.quality == "any" and r.subfolders and not r.stays
                for r in owners
            )
        )
        self.assertEqual(owners[0].shows, RuleTarget(preset="Series (Flat)"))
        self.assertEqual(owners[0].films, RuleTarget(preset="Movie (Folder)"))
        self.assertEqual({r.mode for r in self.service.current}, {"auto"})
        self.assertTrue(ag.airgrabber and ag.on and ag.folder == "")
        # Fresh: the first look queues what is already there as Manual once (nothing unseen).
        self.assertTrue(all(r.fresh for r in self.service.current))
        self.assertEqual(self.service.airgrabber_rule, ag)
        self.assertTrue(self.settings.current.rules_carried)
        await RuleService(RuleRepo(self.db), self.settings).load()  # a second start
        self.assertEqual(len(await RuleRepo(self.db).list()), 3)  # nothing doubled

    async def test_a_changed_setup_after_the_carry_over_isnt_carried_again(self) -> None:
        await self.service.load()
        await self.settings.update(watch_folders=[r"D:\Later"])
        again = RuleService(RuleRepo(self.db), self.settings)
        await again.load()
        self.assertEqual([r.airgrabber for r in again.current], [True])

    async def test_rules_already_there_are_kept(self) -> None:
        # Not carried yet, but the table holds rules (an earlier try stored them): marked only.
        [stored] = await RuleRepo(self.db).replace_all([Rule(airgrabber=True, mode="auto")])
        await self.settings.update(watch_folders=[r"D:\Downloads"])
        await self.service.load()
        self.assertEqual(self.service.current, (stored,))
        self.assertTrue(self.settings.current.rules_carried)

    async def test_manual_and_off_carry_as_manual(self) -> None:
        for automatic in ("trial", "off"):
            settings = ForgeSettings(watch_folders=[r"D:\Downloads"], automatic=automatic)
            self.assertEqual({r.mode for r in rules_from_settings(settings)}, {"manual"})
        await self.settings.update(watch_folders=[r"D:\Downloads"], automatic="trial")
        await self.service.load()
        self.assertEqual({r.mode for r in self.service.current}, {"manual"})

    async def test_airgrabbers_switch_is_the_setting(self) -> None:
        await self.service.load()
        self.assertIsNone(self.settings.current.use_airgrabber_downloads)
        self.assertTrue(self.service.airgrabber_rule.on)  # not chosen yet, no import: on
        ag = self.service.airgrabber_rule
        await self.service.replace_all([ag.model_copy(update={"on": False})])
        self.assertIs(self.settings.current.use_airgrabber_downloads, False)
        self.assertFalse(self.service.airgrabber_rule.on)
        await self.settings.update(use_airgrabber_downloads=True)  # changed elsewhere
        self.assertTrue(self.service.airgrabber_rule.on)  # never the stored column

    async def test_an_imported_setup_starts_with_airgrabbers_rule_off(self) -> None:
        await self.settings.update(imported_from="MediaForge 1.3.0, 2026-11-02")
        await self.service.load()
        self.assertFalse(self.service.airgrabber_rule.on)
        await self.service.replace_all(self.service.current)  # unchanged: the setting stays
        self.assertIsNone(self.settings.current.use_airgrabber_downloads)

    async def test_airgrabbers_rule_is_made_when_missing(self) -> None:
        await self.settings.update(rules_carried=True)
        await self.service.load()
        [ag] = self.service.current
        self.assertTrue(ag.airgrabber and ag.id > 0)
        self.assertEqual(len(await RuleRepo(self.db).list()), 1)

    async def test_airgrabbers_rule_left_out_of_a_save_stays(self) -> None:
        await self.settings.update(watch_folders=[r"D:\Downloads"])
        await self.service.load()
        owner, ag = self.service.current
        saved = await self.service.replace_all([owner])
        self.assertEqual([r.id for r in saved], [owner.id, ag.id])

    async def test_airgrabbers_rule_left_out_keeps_the_setting(self) -> None:
        # Imported: the carry-over stored the rule on, the setting says off (not chosen yet).
        await self.settings.update(
            watch_folders=[r"D:\Downloads"], imported_from="MediaForge 1.3.0, 2026-11-02"
        )
        await self.service.load()
        await self.looked()
        self.assertTrue((await RuleRepo(self.db).list())[-1].on)  # the column differs
        owner, ag = self.service.current
        self.assertFalse(ag.on)
        saved = await self.service.replace_all([owner])
        self.assertIsNone(self.settings.current.use_airgrabber_downloads)  # untouched
        self.assertEqual((saved[-1].on, saved[-1].fresh), (False, False))
        # The other way round: switched off here, then on elsewhere; a save without it.
        await self.service.replace_all([owner, ag.model_copy(update={"on": False})])
        await self.settings.update(use_airgrabber_downloads=True)
        saved = await self.service.replace_all([owner])
        self.assertIs(self.settings.current.use_airgrabber_downloads, True)
        self.assertEqual((saved[-1].on, saved[-1].fresh), (True, False))

    async def test_a_fresh_flag_set_while_settings_saves_is_kept(self) -> None:
        await self.settings.update(watch_folders=[r"D:\a"])
        await self.service.load()
        rule, ag = self.service.current
        await asyncio.gather(
            self.service.mark_fresh(rule.id),
            self.service.replace_all([rule.model_copy(update={"mode": "auto"}), ag]),
        )
        self.assertTrue(self.service.current[0].fresh)
        self.assertTrue((await RuleRepo(self.db).list())[0].fresh)

    async def test_new_moved_or_switched_on_rules_are_fresh(self) -> None:
        await self.settings.update(watch_folders=[r"D:\a", r"D:\b", r"D:\c", r"D:\d"])
        await self.service.load()
        await self.looked()
        a, b, c, d, ag = self.service.current
        await self.service.replace_all([a, b, c.model_copy(update={"on": False}), d, ag])
        await self.service.mark_fresh(d.id)
        a, b, c, d, ag = self.service.current
        saved = await self.service.replace_all(
            [
                a.model_copy(update={"folder": r"D:\elsewhere"}),
                b.model_copy(update={"subfolders": False}),
                c.model_copy(update={"on": True}),
                d.model_copy(update={"name": "Mine", "mode": "manual"}),
                ag.model_copy(
                    update={"films": RuleTarget(preset="Movie (Flat)"), "mode": "manual"}
                ),
                Rule(folder=r"D:\new"),
            ]
        )
        self.assertEqual([r.fresh for r in saved], [True, True, True, True, False, True])
        stored = await RuleRepo(self.db).list()
        self.assertEqual([r.fresh for r in stored], [r.fresh for r in saved])
        await self.service.first_look_done([saved[1].id])
        off = await self.service.replace_all([saved[0], saved[1].model_copy(update={"on": False})])
        self.assertEqual([r.fresh for r in off], [True, False, False])  # switched off: as it was

    async def test_first_look_done_clears_fresh(self) -> None:
        await self.settings.update(watch_folders=[r"D:\a"])
        await self.service.load()
        rule_id = self.service.current[0].id
        told: list[object] = []
        self.service.subscribe(told.append)
        await self.service.mark_fresh(rule_id)
        rule = self.service.get(rule_id)
        self.assertTrue(rule is not None and rule.fresh)
        await self.service.first_look_done([rule_id])
        rule = self.service.get(rule_id)
        self.assertTrue(rule is not None and not rule.fresh)
        self.assertFalse((await RuleRepo(self.db).list())[0].fresh)
        self.assertEqual(told, [])  # fresh flags tell nobody
        self.assertIsNone(self.service.get(9999))

    async def test_presets_renamed_follow(self) -> None:
        await self.settings.update(watch_folders=[r"D:\a"])
        await self.service.load()
        told: list[tuple[Rule, ...]] = []
        unsubscribe = self.service.subscribe(told.append)
        await self.service.presets_renamed({"Movie (Folder)": "Film"})
        self.assertEqual({r.films.preset for r in self.service.current}, {"Film"})
        self.assertEqual({r.films.preset for r in await RuleRepo(self.db).list()}, {"Film"})
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0], self.service.current)
        unsubscribe()
        await self.service.presets_renamed({"Film": "Movie (Folder)"})
        self.assertEqual(len(told), 1)

    async def test_two_presets_swapping_names_swap_in_the_rules(self) -> None:
        await self.service.load()
        await self.service.presets_renamed(
            {"Series (Folder)": "Movie (Folder)", "Movie (Folder)": "Series (Folder)"}
        )
        [ag] = self.service.current
        self.assertEqual((ag.shows.preset, ag.films.preset), ("Movie (Folder)", "Series (Folder)"))

    async def test_listeners_hear_saves_and_reloads(self) -> None:
        await self.service.load()
        told: list[tuple[Rule, ...]] = []
        self.service.subscribe(told.append)

        def fails(_rules: tuple[Rule, ...]) -> None:
            raise ZeroDivisionError

        self.service.subscribe(fails)  # a failing listener is logged only
        with self.assertLogs("app.forge.core.rules_service", "ERROR"):
            await self.service.replace_all([*self.service.current, Rule(folder=r"D:\x")])
        with self.assertLogs("app.forge.core.rules_service", "ERROR"):
            await self.service.reload()
        self.assertEqual(len(told), 2)
        self.assertEqual(len(told[1]), 2)

    async def test_carry_over_logs_counts_only(self) -> None:
        await self.settings.update(watch_folders=[r"D:\Downloads", r"E:\Torrents"])
        with self.assertLogs("app.forge.core.rules_service", "INFO") as logs:
            await self.service.load()
            await self.service.replace_all(
                [*self.service.current, Rule(folder=r"F:\Downloads", name="The Long Road")]
            )
        self.assertTrue(any("2" in line for line in logs.output))
        for line in logs.output:
            for word in ("Downloads", "Torrents", "Long Road", "D:", "E:", "F:"):
                self.assertNotIn(word, line)
