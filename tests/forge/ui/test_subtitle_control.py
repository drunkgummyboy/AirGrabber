"""CC ▾ (app/forge/ui/subtitle_control.py): its menu, its states and outcomes, on the queue's done
bands and History's rows (drawn on the list's canvas), greyed with the reason; and the Subtitles
extra on the routes that rename: the queue's Forge, the right-click window, Auto-rename."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

from app.forge.clients.opensubtitles import OpenSubtitlesClient
from app.forge.core.auto import AutoRenamer
from app.forge.core.executor import Executor
from app.forge.core.subtitles import ForgeSubtitles
from app.forge.models.media import MediaKind
from app.forge.models.moves import MoveNote
from app.forge.models.preset import Preset
from app.forge.models.subtitles import SubtitleFetch, SubtitleFetchOutcome as Outcome, SubtitleIdentity
from app.forge.ui.confirm_window import ConfirmWindow
from app.forge.ui.queue_rows import build_queue_rows
from app.forge.ui.queue_parts import OPTION_GROUPS, OPTION_LABELS, summary_text
from app.forge.ui.subtitle_control import (
    CC_ACTION,
    CC_LABEL,
    OUTCOME_MS,
    WORKING,
    CcLook,
    RunPhase,
    SubtitleDesk,
    SubtitleRun,
    SubtitleRuns,
    VideoTarget,
    cc_menu,
    fetch_run,
    language_name,
)
from app.forge.core.executor import RunResult
from app.ui.missing_control import PopupMenu
from tests.forge.core.test_auto_renamer import Clock, RenamerTestCase
from tests.forge.core.test_subtitles import EPISODE, FakeClient, FakeSettings
from tests.forge.tk import pump, pump_until
from tests.forge.ui.subtitle_kit import FakeSubtitleSource
from tests.forge.ui.test_confirm_window import ConfirmCase, make_files
from tests.forge.ui.test_history_view import HistoryViewCase
from tests.forge.ui.test_queue_view import QueueCase

KEY = "row"


class TextTests(unittest.TestCase):
    def test_menu(self) -> None:
        asked: list[tuple[str, ...]] = []
        items = cc_menu(("en", "nl"), asked.append)
        self.assertEqual(
            [label for label, _ in items],
            ["Get English subtitles", "Get Dutch subtitles", "Get all my languages"],
        )
        for _label, action in items:
            action()
        self.assertEqual(asked, [("en",), ("nl",), ("en", "nl")])

    def test_one_language_has_no_all_item(self) -> None:
        items = cc_menu(("pt-BR",), lambda languages: None)
        self.assertEqual([label for label, _ in items], ["Get Portuguese (Brazil) subtitles"])
        self.assertEqual(language_name("xx"), "XX")

    def test_outcomes_in_plain_words(self) -> None:
        def text(outcome: Outcome, **fields: Any) -> str:
            return fetch_run(SubtitleFetch(outcome=outcome, **fields)).text

        self.assertEqual(text(Outcome.SAVED, saved=("en", "nl")), "Found: EN NL")
        self.assertEqual(text(Outcome.ALREADY), "Already there")
        self.assertEqual(text(Outcome.PARTLY, not_found=("nl", "de")), "Not found yet: NL DE")
        self.assertEqual(text(Outcome.NOT_FOUND), "Not found yet")
        self.assertEqual(text(Outcome.NO_VIDEO), "The video is no longer there")
        self.assertEqual(text(Outcome.QUOTA, message="used up"), "Today's limit reached")
        self.assertEqual(text(Outcome.PROBLEM, message="No answer"), "No answer")
        self.assertEqual(text(Outcome.PROBLEM), "Couldn't get subtitles")

    def test_runs_expire_only_when_unchanged(self) -> None:
        changes: list[int] = []
        runs = SubtitleRuns(lambda: changes.append(1))
        first = SubtitleRun(RunPhase.DONE, "Not found yet")
        runs.set(KEY, first)
        runs.set(KEY, WORKING)
        runs.expire(KEY, first)
        self.assertIs(runs.get(KEY), WORKING)
        self.assertTrue(runs.is_working(KEY))
        runs.set(KEY, first)
        runs.expire(KEY, first)
        self.assertIsNone(runs.get(KEY))
        self.assertEqual(len(changes), 4)

    def test_the_extra_is_listed(self) -> None:
        self.assertEqual(OPTION_LABELS["subtitles"], "Subtitles")
        names = [spec.name for _group, specs in OPTION_GROUPS for spec in specs]
        self.assertIn("Subtitles", names)

    def test_a_forges_summary_counts_them(self) -> None:
        text = summary_text(RunResult(renamed=1, subtitles_fetched=2))
        self.assertIn("2 subtitles", text)


class DeskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.source = FakeSubtitleSource()
        self.tasks: list[asyncio.Task[None]] = []
        self.later: list[tuple[int, Callable[[], None]]] = []
        self.changes = 0
        self.done: list[object] = []
        self.desk = SubtitleDesk(
            self.source,
            spawn=self._spawn,
            after=lambda ms, call: self.later.append((ms, call)),
            on_change=self._changed,
            on_done=self.done.append,
        )
        self.targets = [
            VideoTarget(Path("D:/a.mkv"), EPISODE, 3),
            VideoTarget(Path("D:/b.mkv"), EPISODE, None),
        ]

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        self.tasks.append(asyncio.ensure_future(coro))

    def _changed(self) -> None:
        self.changes += 1

    async def _targets(self) -> list[VideoTarget]:
        return self.targets

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks)

    def test_no_key_or_no_language_shows_nothing(self) -> None:
        self.source.available = False
        self.assertIsNone(self.desk.look(KEY))
        self.source.available = True
        self.source.languages = ()
        self.assertIsNone(self.desk.look(KEY))

    def test_offered(self) -> None:
        self.assertEqual(self.desk.look(KEY), CcLook())

    def test_greyed_with_the_reason(self) -> None:
        self.assertEqual(self.desk.look(KEY, gone="gone"), CcLook(greyed=True, tip="gone"))
        self.source.used_up = True
        look = self.desk.look(KEY)
        assert look is not None
        self.assertTrue(look.greyed)
        self.assertIn("used up", look.tip)

    async def test_a_request_for_every_video(self) -> None:
        self.desk.fetch(KEY, self._targets, ("en",))
        look = self.desk.look(KEY)
        assert look is not None
        self.assertEqual(look, CcLook(greyed=True, note="Looking for subtitles…"))
        self.desk.fetch(KEY, self._targets, ("nl",))  # one at a time
        await self.settle()
        self.assertEqual(
            [(video.name, langs, run) for video, _i, langs, run in self.source.asked],
            [("a.mkv", ("en",), 3), ("b.mkv", ("en",), None)],
        )
        look = self.desk.look(KEY)
        assert look is not None
        self.assertEqual(look, CcLook(note="Found: EN"))
        self.assertEqual(self.done, [KEY])
        self.assertGreaterEqual(self.changes, 2)
        ms, expire = self.later[0]
        self.assertEqual(ms, OUTCOME_MS)
        expire()
        self.assertEqual(self.desk.look(KEY), CcLook())

    async def test_a_used_up_allowance_ends_the_request(self) -> None:
        self.source.result = SubtitleFetch(outcome=Outcome.QUOTA, message="m")
        self.desk.fetch(KEY, self._targets, ("en",))
        await self.settle()
        self.assertEqual(len(self.source.asked), 1)
        look = self.desk.look(KEY)
        assert look is not None
        self.assertEqual(look.note, "Today's limit reached")

    async def test_no_videos_left(self) -> None:
        self.targets = []
        self.desk.fetch(KEY, self._targets, ("en",))
        await self.settle()
        look = self.desk.look(KEY)
        assert look is not None
        self.assertEqual(look.note, "The video is no longer there")

    def test_the_menu_starts_it_with_the_languages_chosen(self) -> None:
        asked: list[Any] = []
        self.desk.fetch = lambda key, targets, languages: asked.append((key, languages))  # type: ignore[method-assign]
        items = self.desk.menu(KEY, self._targets)
        items[1][1]()
        items[2][1]()
        self.assertEqual(asked, [(KEY, ("nl",)), (KEY, ("en", "nl"))])


class QueueRowsTests(unittest.TestCase):
    """The band's buttons as `build_queue_rows` makes them."""

    def item(self, **fields: Any) -> Any:
        from tests.forge.ui.test_queue_rows import movie

        return movie(Path("D:/in"), "Dune.mkv", "Dune", 2021, **fields)

    def band(self, item: Any, **kwargs: Any) -> Any:
        built = build_queue_rows([item], lambda _item: "{ny}", **kwargs)
        return next(r for r in built.rows if r.kind == "band")

    def done(self) -> Any:
        from app.forge.core.queue import ItemStatus

        return self.item(status=ItemStatus.DONE)

    def test_without_cc_undo_alone(self) -> None:
        self.assertEqual([a for a, _ in self.band(self.done()).actions], ["explorer", "undo"])
        none = self.band(self.done(), cc=lambda group: None)
        self.assertEqual([a for a, _ in none.actions], ["explorer", "undo"])

    def test_a_done_band(self) -> None:
        band = self.band(self.done(), cc=lambda group: CcLook())
        self.assertEqual(band.actions[1:], ((CC_ACTION, CC_LABEL), ("undo", "Undo")))
        self.assertEqual(band.greyed, ())

    def test_greyed_with_a_note(self) -> None:
        look = CcLook(greyed=True, tip="why", note="Looking for subtitles…")
        band = self.band(self.done(), cc=lambda group: look)
        self.assertEqual(band.greyed, ((CC_ACTION, "why"),))
        self.assertEqual(band.note, "Looking for subtitles…")

    def test_a_band_not_renamed_yet_never_has_it(self) -> None:
        band = self.band(self.item(), cc=lambda group: CcLook())
        self.assertNotIn(CC_ACTION, [a for a, _ in band.actions])


class QueueCcTests(QueueCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.source = FakeSubtitleSource()
        self.view.subtitle_desk._source = self.source

    async def done_band(self) -> Any:
        folder = self.tmp / "in"
        folder.mkdir()
        (folder / "Dune.2021.1080p.mkv").write_bytes(b"video")
        await self.add_and_settle(folder)
        await self.choose("Movie (Flat)")
        await self.view.forge()
        await pump(self.root, 0.2)
        self.view.refresh()
        await pump(self.root, 0.1)
        return next(r for r in self.rows("band") if r.state == "done")

    async def test_done_bands_have_it_and_others_dont(self) -> None:
        folder = self.tmp / "in"
        folder.mkdir()
        (folder / "Dune.2021.1080p.mkv").write_bytes(b"video")
        await self.add_and_settle(folder)
        waiting = self.rows("band")[0]
        self.assertNotIn(CC_ACTION, [a for a, _ in waiting.actions])
        await self.choose("Movie (Flat)")
        await self.view.forge()
        self.view.refresh()
        await pump(self.root, 0.1)
        band = next(r for r in self.rows("band") if r.state == "done")
        self.assertEqual(band.actions[1:], ((CC_ACTION, CC_LABEL), ("undo", "Undo")))
        self.assertEqual(band.greyed, ())

    async def test_the_menu_and_a_request(self) -> None:
        band = await self.done_band()
        self.view.list.hover_band(band.key)
        await pump(self.root, 0.05)
        self.view._on_band_action(band.key, CC_ACTION)
        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(
            menu.labels,
            ("Get English subtitles", "Get Dutch subtitles", "Get all my languages"),
        )
        menu.buttons[2].invoke()
        await pump_until(self.root, lambda: self.source.asked)
        video, identity, languages, run_id = self.source.asked[0]
        self.assertEqual(video.name, "Dune (2021).mkv")
        self.assertEqual(identity.title, "Dune")
        self.assertEqual(identity.kind, MediaKind.MOVIE)
        self.assertEqual(languages, ("en", "nl"))
        self.assertIsNotNone(run_id)
        await pump_until(
            self.root,
            lambda: any(r.note == "Found: EN NL" for r in self.rows("band")),
        )

    async def test_greyed_when_the_allowance_is_used_up_with_the_reason(self) -> None:
        band = await self.done_band()
        self.source.used_up = True
        self.view.refresh()
        await pump(self.root, 0.1)
        band = next(r for r in self.rows("band") if r.state == "done")
        self.assertIn("used up", dict(band.greyed)[CC_ACTION])
        self.view.list.hover_band(band.key)
        await pump(self.root, 0.05)
        self.assertIsNone(self.view.list.action_box(band.key, CC_ACTION))  # takes no click
        box = self.view.list.greyed_box(band.key, CC_ACTION)
        assert box is not None
        self.view.list._point_at((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        self.assertIn("used up", self.view.list.tip_shown)
        self.view.list._point_at(0, 0)
        self.assertEqual(self.view.list.tip_shown, "")

    async def test_without_a_key_no_cc(self) -> None:
        band = await self.done_band()
        self.source.available = False
        self.view.refresh()
        await pump(self.root, 0.1)
        band = next(r for r in self.rows("band") if r.state == "done")
        self.assertEqual([a for a, _ in band.actions], ["explorer", "undo"])

    async def test_the_cards_extra_is_there_with_a_key(self) -> None:
        self.assertIn("Subtitles", self.view.option_buttons)


class NoKeyCardTests(QueueCase):
    async def test_no_extra_on_the_card_without_a_key(self) -> None:
        self.services.subtitles = FakeSubtitleSource(available=False)  # type: ignore[assignment]
        view = await self.make_view()
        self.assertNotIn("Subtitles", view.option_buttons)
        self.assertNotIn("Subtitles", view.option_keys())


class HistoryCcTests(HistoryViewCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.source = self.parts.subtitles
        self.run_id = await self.forge({"a.mkv": "Alien"}, "Movie (Flat)")
        await self.view.reload()
        self.card = self.view.cards[0]
        await self.card.expand()
        await pump(self.root, 0.05)
        assert self.card.files is not None
        self.files = self.card.files
        self.op = next(o for o in self.card.operations if o.dst and o.dst.endswith(".mkv"))

    def row(self) -> Any:
        return next(r for r in self.files.rows if r.key == self.op.id)

    async def again(self) -> None:
        await self.card.expand()
        await pump(self.root, 0.05)

    async def test_a_videos_row_has_it(self) -> None:
        self.assertEqual(self.row().actions, ((CC_ACTION, CC_LABEL),))
        self.assertEqual(self.row().greyed, ())
        self.assertIsNotNone(self.files.action_box(self.op.id, CC_ACTION))

    async def test_the_menu(self) -> None:
        labels = [label for label, _ in self.card.cc_items(self.op.id)]
        self.assertEqual(
            labels, ["Get English subtitles", "Get Dutch subtitles", "Get all my languages"]
        )
        self.card.file_action(self.op.id, CC_ACTION)
        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(len(menu.labels), 3)
        menu.close()

    async def test_one_language_has_no_all_item(self) -> None:
        self.source.languages = ("en",)
        labels = [label for label, _ in self.card.cc_items(self.op.id)]
        self.assertEqual(labels, ["Get English subtitles"])

    async def test_a_request_uses_the_record_and_the_name(self) -> None:
        self.card.cc_items(self.op.id)[0][1]()
        await pump_until(self.root, lambda: self.source.asked)
        video, identity, languages, run_id = self.source.asked[0]
        self.assertEqual(str(video), self.op.dst)
        self.assertEqual(identity.title, "Alien")
        self.assertEqual(languages, ("en",))
        self.assertEqual(run_id, self.run_id)
        await pump_until(self.root, lambda: "Found: EN" in self.row().subtitle)

    async def test_the_video_is_gone(self) -> None:
        Path(self.op.dst or "").unlink()
        await self.again()
        self.assertEqual(dict(self.row().greyed)[CC_ACTION], "The video is no longer there")
        self.assertIsNone(self.files.action_box(self.op.id, CC_ACTION))
        self.assertEqual(
            self.files.greyed_tip(self.op.id, CC_ACTION), "The video is no longer there"
        )

    async def test_the_allowance_is_used_up(self) -> None:
        self.source.used_up = True
        await self.again()
        self.assertIn("used up", dict(self.row().greyed)[CC_ACTION])

    async def test_without_a_key_no_cc(self) -> None:
        self.source.available = False
        await self.again()
        self.assertEqual(self.row().actions, ())

    async def test_a_click_on_it_or_a_greyed_one(self) -> None:
        box = self.files.action_box(self.op.id, CC_ACTION)
        assert box is not None
        asked: list[tuple[int, str]] = []
        self.files.on_action = lambda key, action: asked.append((key, action))
        self.files.canvas.event_generate(
            "<Button-1>", x=(box[0] + box[2]) // 2, y=(box[1] + box[3]) // 2
        )
        self.root.update()
        self.assertEqual(asked, [(self.op.id, CC_ACTION)])

    async def test_a_failed_line_of_the_extra_says_why(self) -> None:
        repo = self.parts.repo
        target = Path(self.op.dst or "").with_suffix(".nl.srt")
        from app.forge.models.history import OpKind

        op = await repo.record_pending(self.run_id, OpKind.CREATE_FILE, None, target)
        await repo.mark_failed(op, "no subtitle found")
        await self.again()
        row = next(r for r in self.files.rows if r.key == op)
        self.assertEqual(row.subtitle, "subtitle  ·  no subtitle found")
        self.assertTrue(row.check_disabled)
        self.assertEqual(row.actions, ())

    async def test_the_identity_from_a_move_record(self) -> None:
        from app.forge.db.moves import MoveRepository, record_move

        db = self.parts.db
        async with db.transaction() as conn:
            await record_move(
                conn,
                self.op.id,
                MoveNote(
                    source=Path(self.op.src or ""),
                    target=Path(self.op.dst or ""),
                    kind=MediaKind.MOVIE,
                    tmdb_id=348,
                ),
            )
        self.parts.moves = MoveRepository(db)
        target = await self.view.video_target(self.op)
        self.assertEqual(target.identity.tmdb_id, 348)
        self.assertEqual(target.identity, SubtitleIdentity(
            kind=MediaKind.MOVIE, title="Alien", year=target.identity.year, tmdb_id=348,
            release=Path(self.op.src or "").name,
        ))


class _Rename:
    """The real ForgeSubtitles on a fake OpenSubtitles, for a route's real executor."""

    @staticmethod
    def fetcher(history: Any) -> tuple[ForgeSubtitles, FakeClient]:
        client = FakeClient()
        return (
            ForgeSubtitles(cast(OpenSubtitlesClient, client), FakeSettings(), history),
            client,
        )


class QueueRouteTests(QueueCase):
    async def forge_with(self, *, extra: bool) -> Path:
        client = FakeClient()
        self.services.subtitles._client = cast(OpenSubtitlesClient, client)
        self.services.subtitles._settings = FakeSettings()
        folder = self.tmp / "in"
        folder.mkdir()
        (folder / "Dune.2021.1080p.mkv").write_bytes(b"video")
        await self.add_and_settle(folder)
        await self.choose("Movie (Flat)")
        if extra:
            self.view._option_clicked("Subtitles", True)
        await self.view.forge()
        return folder

    async def test_on(self) -> None:
        folder = await self.forge_with(extra=True)
        self.assertTrue((folder / "Dune (2021).mkv").exists())
        self.assertTrue((folder / "Dune (2021).en.srt").exists())
        self.assertTrue((folder / "Dune (2021).nl.srt").exists())

    async def test_off(self) -> None:
        folder = await self.forge_with(extra=False)
        self.assertTrue((folder / "Dune (2021).mkv").exists())
        self.assertEqual(list(folder.glob("*.srt")), [])


class ConfirmRouteTests(ConfirmCase):
    async def rename_with(self, *, extra: bool) -> Path:
        self.services.subtitles._client = cast(OpenSubtitlesClient, FakeClient())
        self.services.subtitles._settings = FakeSettings()
        presets = await self.services.presets.list()
        flat: Preset = next(p for p in presets if p.name == "Movie (Flat)")
        options = flat.options.model_copy(update={"subtitles": extra})
        await self.services.presets.save(flat.model_copy(update={"options": options}))
        folder = self.tmp / "m"
        files = make_files(folder, ["Dune.2021.mkv"])
        win: ConfirmWindow = await self.open_window(files)
        await win.rename()
        await pump(self.root, 0.1)
        return folder

    async def test_on(self) -> None:
        folder = await self.rename_with(extra=True)
        self.assertTrue((folder / "Dune (2021).mkv").exists())
        self.assertTrue((folder / "Dune (2021).en.srt").exists())

    async def test_off(self) -> None:
        folder = await self.rename_with(extra=False)
        self.assertTrue((folder / "Dune (2021).mkv").exists())
        self.assertEqual(list(folder.glob("*.srt")), [])


class AutoRouteTests(RenamerTestCase):
    async def rename_with(self, *, extra: bool) -> Path:
        from app.forge.db.presets import PresetRepo
        from dataclasses import replace

        subtitles, self.client = _Rename.fetcher(self.history)
        self.parts = replace(self.parts, executor=Executor(self.history, None, None, subtitles=subtitles))
        presets = PresetRepo(self.db)
        for preset in await presets.list():
            options = preset.options.model_copy(update={"subtitles": extra})
            await presets.save(preset.model_copy(update={"options": options}))
        await self.use()
        self.folders.download("VisionQuest.S01E02.Theseus.1080p.WEB.mkv")
        clock = Clock()
        auto: AutoRenamer = self.renamer(clock)
        await self.two_looks(auto, clock)
        return (
            self.folders.series
            / "VisionQuest (2025)"
            / "VisionQuest (2025) - Season 01"
        )

    async def test_on(self) -> None:
        season = await self.rename_with(extra=True)
        self.assertTrue((season / "VisionQuest (2025) - S01E02 - Theseus.mkv").exists())
        self.assertTrue((season / "VisionQuest (2025) - S01E02 - Theseus.en.srt").exists())
        by_identity = self.client.searches[-1]
        self.assertEqual(by_identity["parent_tmdb_id"], 7)
        self.assertEqual(
            (by_identity["season_number"], by_identity["episode_number"]), (1, 2)
        )

    async def test_off(self) -> None:
        season = await self.rename_with(extra=False)
        self.assertTrue((season / "VisionQuest (2025) - S01E02 - Theseus.mkv").exists())
        self.assertEqual(list(season.glob("*.srt")), [])
        self.assertEqual(self.client.searches, [])

    async def test_a_used_up_allowance_leaves_the_move_done(self) -> None:
        from app.forge.db.presets import PresetRepo
        from dataclasses import replace

        subtitles, client = _Rename.fetcher(self.history)
        client.quota_error = True
        self.parts = replace(self.parts, executor=Executor(self.history, None, None, subtitles=subtitles))
        presets = PresetRepo(self.db)
        for preset in await presets.list():
            options = preset.options.model_copy(update={"subtitles": True})
            await presets.save(preset.model_copy(update={"options": options}))
        await self.use()
        self.folders.download("VisionQuest.S01E02.Theseus.1080p.WEB.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        [item] = self.queue.items
        self.assertEqual(item.path.name, "VisionQuest (2025) - S01E02 - Theseus.mkv")
        self.assertTrue(item.path.exists())


if __name__ == "__main__":
    unittest.main()
