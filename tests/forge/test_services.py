"""Forge's composition root (`ForgeServices`), the runtime AirGrabber starts (`ForgeRuntime`) and
the Activity strip's feed. Everything runs on temporary data dirs with fake HTTP; nothing
reaches TMDB, GitHub or the real app data."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import cast
from unittest import mock

import httpx

from app.config import AppPaths
from app.forge.core.activity import ActivityFeed, ActivityLine, quiet, say_to
from app.forge.core.presets_service import LastPresetError
from app.forge.db import presets as db_presets
from app.forge.db.database import Database, db_path
from app.forge.db.history import HistoryRepo
from app.forge.models.history import OpKind, OpState, RunSource
from app.forge.models.media import MediaKind, ParsedMedia
from app.forge.models.preset import DEFAULT_PRESETS, PresetOptions
from app.forge.core.planner import build_plan
from app.forge.core.queue import QueueEvent
from app.forge.runtime import FAILED_REASON, NOT_READY_REASON, ForgeRuntime, ForgeState
from app.forge.services import ForgeServices, QueueEvents
from app.i18n import Language, current_language, set_language, t
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.core.journal_base import make_inputs


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def _forge_threads() -> set[threading.Thread]:
    """Threads that would keep the process alive (the loop's own worker pool excepted)."""
    return {
        th
        for th in threading.enumerate()
        if th.is_alive() and not th.daemon and th is not threading.main_thread()
        and not th.name.startswith("asyncio")
    }


async def _until_gone(before: set[threading.Thread], within: float = 3.0) -> set[threading.Thread]:
    deadline = time.monotonic() + within
    while True:
        left = _forge_threads() - before
        if not left or time.monotonic() > deadline:
            return left
        await asyncio.sleep(0.02)


class _Parts:
    """AirGrabber's shared clients, faked: HTTP on a MockTransport, TMDB without a key."""

    def __init__(self, root: Path) -> None:
        self.paths = AppPaths(root)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.tmdb = TMDBClient(self.http, api_key=lambda: "")
        self.images = ImageCache(self.http, root / "posters")
        self.settings = cast(SettingsService, mock.Mock(spec=SettingsService))

    async def create(self) -> ForgeServices:
        return await ForgeServices.create(
            self.paths, self.http, self.tmdb, self.images, self.settings
        )

    def runtime(self, *, parser_ready: bool = True) -> ForgeRuntime:
        return ForgeRuntime(
            self.paths,
            self.http,
            self.tmdb,
            self.images,
            self.settings,
            parser_ready=lambda: parser_ready,
        )


# --- the activity feed -------------------------------------------------------------------------


class ActivityFeedTest(ForgeTestCase):
    def test_lines_are_kept_bounded_and_handed_to_listeners(self) -> None:
        feed = ActivityFeed(max_lines=3)
        heard: list[ActivityLine] = []
        unsubscribe = feed.subscribe(heard.append)
        for i in range(5):
            feed.say(f"line {i}")
        feed.say("done", success=True)
        feed.say("oops", level=logging.ERROR)
        self.assertEqual([line.text for line in feed.lines()], ["line 4", "done", "oops"])
        self.assertEqual(len(heard), 7)
        self.assertTrue(heard[5].success)
        self.assertEqual(heard[6].level, logging.ERROR)
        self.assertEqual(heard[0].level, logging.INFO)
        unsubscribe()
        unsubscribe()  # twice is harmless
        feed.say("unheard")
        self.assertEqual(len(heard), 7)

    def test_a_failing_listener_neither_stops_the_line_nor_the_others(self) -> None:
        feed = ActivityFeed()
        heard: list[str] = []

        def broken(line: ActivityLine) -> None:
            raise RuntimeError("view gone")

        feed.subscribe(broken)
        feed.subscribe(lambda line: heard.append(line.text))
        with self.assertLogs("app.forge.core.activity", logging.ERROR):
            feed.say("still here")
        self.assertEqual(heard, ["still here"])
        self.assertEqual(feed.lines()[-1].text, "still here")

    def test_close_forgets_the_listeners(self) -> None:
        feed = ActivityFeed()
        heard: list[ActivityLine] = []
        feed.subscribe(heard.append)
        feed.close()
        feed.say("after")
        self.assertEqual(heard, [])

    def test_without_a_feed_lines_go_nowhere(self) -> None:
        self.assertIs(say_to(None), quiet)
        quiet("nothing", level=logging.WARNING, success=True)
        feed = ActivityFeed()
        say_to(feed)("something")
        self.assertEqual(feed.lines()[0].text, "something")


class QueueEventsTest(ForgeTestCase):
    def test_events_reach_every_listener_until_unsubscribed(self) -> None:
        hub = QueueEvents()
        first: list[QueueEvent] = []
        second: list[QueueEvent] = []

        def broken(event: QueueEvent) -> None:
            raise RuntimeError("view gone")

        unsubscribe = hub.subscribe(first.append)
        hub.subscribe(broken)
        hub.subscribe(second.append)
        with self.assertLogs("app.forge.services", logging.ERROR):
            hub.emit(QueueEvent(kind="cleared"))
        unsubscribe()
        hub.emit(QueueEvent(kind="cleared"))
        self.assertEqual((len(first), len(second)), (1, 2))
        hub.close()
        hub.emit(QueueEvent(kind="cleared"))
        self.assertEqual(len(second), 2)


# --- ForgeServices -----------------------------------------------------------------------------


class ForgeServicesTest(ForgeAsyncTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.parts = _Parts(self.data_dir)
        self.addAsyncCleanup(self.parts.http.aclose)

    async def open(self) -> ForgeServices:
        services = await self.parts.create()
        self.addAsyncCleanup(services.aclose)
        return services

    async def test_create_and_close_leave_no_thread_alive(self) -> None:
        before = _forge_threads()
        services = await self.parts.create()
        self.assertTrue(db_path(self.parts.paths).is_file())
        self.assertEqual(db_path(self.parts.paths).parent, self.data_dir / "forge")
        await services.aclose()
        await services.aclose()  # twice is harmless
        self.assertEqual(await _until_gone(before), set())

    async def test_a_new_forge_has_the_default_presets_and_settings(self) -> None:
        services = await self.open()
        self.assertEqual(await services.presets.list(), list(DEFAULT_PRESETS))
        self.assertEqual(services.settings.current.automatic, "off")
        self.assertEqual(services.settings.current.tmdb_language, "en-US")
        self.assertEqual(await services.history.list_runs(), [])
        self.assertEqual(await services.matches.list(), [])
        self.assertFalse(services.tmdb.configured())
        self.assertFalse(services.auto.running)  # create() never starts watching
        self.assertEqual(services.activity.lines(), ())

    async def test_the_names_language_follows_forge_settings(self) -> None:
        services = await self.open()
        self.assertEqual(services.tmdb.language, "en-US")
        await services.settings.update(tmdb_language="de-DE")
        self.assertEqual(services.tmdb.language, "de-DE")

    async def test_unfinished_journal_rows_are_settled_at_start(self) -> None:
        src = self.tmp / "film.mkv"
        src.write_bytes(b"video")
        dst = self.tmp / "Film (2024).mkv"  # the rename never happened
        db = await Database.open(db_path(self.parts.paths))
        try:
            history = HistoryRepo(db)
            run_id = await history.start_run(RunSource.APP, "Auto")
            await history.record_pending(run_id, OpKind.RENAME, src, dst)
        finally:
            await db.close()

        services = await self.open()
        [run] = await services.history.list_runs()
        [op] = await services.history.operations(run.id)
        self.assertEqual(op.state, OpState.FAILED)
        lines = services.activity.lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].level, logging.WARNING)
        self.assertIn("1 change", lines[0].text)

    async def test_the_last_preset_cannot_be_deleted(self) -> None:
        services = await self.open()
        self.assertIs(LastPresetError, db_presets.LastPresetError)  # one class, models-owned
        names = [p.name for p in await services.presets.list()]
        for name in names[:-1]:
            await services.presets.delete(name)
        with self.assertRaises(LastPresetError):
            await services.presets.delete(names[-1])
        [left] = await services.presets.list()
        await services.presets.save(left.model_copy(update={"name": "Mine"}), old_name=left.name)
        self.assertEqual([p.name for p in await services.presets.list()], ["Mine"])
        await services.presets.replace_all(DEFAULT_PRESETS)
        self.assertEqual(len(await services.presets.list()), len(DEFAULT_PRESETS))

    async def test_remembered_matches_are_listed_forgotten_and_cleared(self) -> None:
        services = await self.open()
        for title in ("Alpha", "Beta"):
            parsed = ParsedMedia(source_name=f"{title}.mkv", kind=MediaKind.MOVIE, title=title)
            await services.resolver.apply_choice(parsed, None)  # "don't use TMDB" for it
        alpha, beta = await services.matches.list()
        await services.matches.forget(alpha)
        self.assertEqual(await services.matches.list(), [beta])
        await services.matches.clear()
        self.assertEqual(await services.matches.list(), [])

    async def test_a_rename_and_its_undo_reach_the_activity_feed(self) -> None:
        services = await self.open()
        heard: list[ActivityLine] = []
        services.activity.subscribe(heard.append)
        inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        plan = build_plan(inputs, "{ny}", PresetOptions(movie_poster=True))
        result = await services.executor.run(
            plan,
            source=RunSource.APP,
            preset_name="Auto",
            on_progress=lambda event: None,
            cancel=asyncio.Event(),
        )
        self.assertEqual((result.renamed, result.posters), (1, 0))  # the poster fetch got a 404
        renamed = heard[0]
        self.assertEqual(renamed.text, "Renamed: x.mkv → Dune (2024).mkv")
        self.assertTrue(renamed.success)
        self.assertEqual(heard[1].level, logging.WARNING)  # "Couldn't get poster …"
        self.assertIn("Dune (2024)", heard[1].text)
        assert result.run_id is not None
        await services.undo.undo_run(result.run_id)
        self.assertEqual(heard[-1].text, "Put back: x.mkv")
        self.assertTrue((self.tmp / "x.mkv").exists())

    async def test_activity_lines_are_in_the_interface_language(self) -> None:
        previous = current_language()
        set_language(Language.NL)
        self.addCleanup(set_language, previous)
        services = await self.open()
        inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        await services.executor.run(
            build_plan(inputs, "{ny}", PresetOptions()),
            source=RunSource.APP,
            preset_name="Auto",
            on_progress=lambda event: None,
            cancel=asyncio.Event(),
        )
        self.assertEqual(services.activity.lines()[0].text, "Hernoemd: x.mkv → Dune (2024).mkv")

    async def test_a_save_that_touches_the_renamer_reconfigures_it(self) -> None:
        services = await self.open()
        with mock.patch.object(services.auto, "reconfigure") as reconfigure:
            await services.settings.update(tmdb_language="nl-NL", notify_needs=False)
            reconfigure.assert_not_called()
            await services.settings.update(automatic="trial")
            reconfigure.assert_called_once_with()
            await services.settings.update(automatic="trial")  # nothing changed
            await services.settings.update(watch_folders=[str(self.tmp)])
            self.assertEqual(reconfigure.call_count, 2)

    async def test_switching_on_in_settings_starts_the_renamer(self) -> None:
        services = await self.open()
        await services.settings.update(automatic="trial")
        self.assertTrue(services.auto.running)
        await services.settings.update(automatic="off")
        await asyncio.sleep(0.05)
        self.assertFalse(services.auto.running)

    async def test_close_after_off_then_trial_never_starts_the_renamer_again(self) -> None:
        services = await self.parts.create()
        await services.settings.update(automatic="trial")
        self.assertTrue(services.auto.running)
        await services.settings.update(automatic="off")  # a stop is now pending …
        await services.settings.update(automatic="trial")  # … and a restart after it
        await services.aclose()  # quit at once
        await asyncio.sleep(0.1)
        self.assertFalse(services.auto.running)
        services.start_watching()  # closed: does nothing
        self.assertFalse(services.auto.running)

    async def test_close_with_the_renamer_running(self) -> None:
        before = _forge_threads()
        services = await self.parts.create()
        await services.settings.update(automatic="on")
        services.start_watching()
        self.assertTrue(services.auto.running)
        await services.aclose()
        self.assertFalse(services.auto.running)
        self.assertEqual(await _until_gone(before), set())


# --- ForgeRuntime ------------------------------------------------------------------------------


class ForgeRuntimeTest(ForgeAsyncTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.parts = _Parts(self.data_dir)
        self.addAsyncCleanup(self.parts.http.aclose)

    def runtime(self, *, parser_ready: bool = True) -> ForgeRuntime:
        runtime = self.parts.runtime(parser_ready=parser_ready)
        self.addAsyncCleanup(runtime.aclose)
        return runtime

    async def test_it_is_off_until_started(self) -> None:
        runtime = self.runtime()
        self.assertIs(runtime.state, ForgeState.OFF)
        self.assertIsNone(runtime.services)
        self.assertEqual(runtime.reason, "")
        self.assertFalse((self.data_dir / "forge").exists())

    async def test_start_builds_forge_once_for_every_caller(self) -> None:
        runtime = self.runtime()
        first, second = await asyncio.gather(runtime.start(), runtime.start())
        self.assertIsNotNone(first)
        self.assertIs(first, second)
        self.assertIs(await runtime.start(), first)
        self.assertIs(runtime.services, first)
        self.assertIs(runtime.state, ForgeState.READY)
        self.assertEqual(runtime.reason, "")

    async def test_without_guessit_forge_stays_off(self) -> None:
        before = _forge_threads()
        runtime = self.runtime(parser_ready=False)
        with self.assertLogs("app.forge.runtime", logging.WARNING):
            self.assertIsNone(await runtime.start())
        self.assertIs(runtime.state, ForgeState.NOT_READY)
        self.assertEqual(runtime.reason, t(NOT_READY_REASON))
        self.assertIsNone(await runtime.start())  # asked once; this run stays off
        self.assertIsNone(runtime.services)
        self.assertFalse((self.data_dir / "forge").exists())  # nothing opened, nothing watched
        await runtime.aclose()
        self.assertIs(runtime.state, ForgeState.CLOSED)
        self.assertEqual(_forge_threads() - before, set())

    async def test_its_own_check_prepares_guessit_in_pieces(self) -> None:
        # F-33: without an injected check the start awaits parser.prepare_parser (guessit
        # imported and its rules built between turns of the loop), not one blocking import.
        parts = self.parts
        runtime = ForgeRuntime(parts.paths, parts.http, parts.tmdb, parts.images, parts.settings)
        self.addAsyncCleanup(runtime.aclose)
        prepare = mock.AsyncMock(return_value=False)
        with (
            mock.patch("app.forge.core.parser.prepare_parser", prepare),
            self.assertLogs("app.forge.runtime", logging.WARNING),
        ):
            self.assertIsNone(await runtime.start())
        prepare.assert_awaited_once()
        self.assertIs(runtime.state, ForgeState.NOT_READY)

    async def test_ready_callbacks_come_before_the_renamer_starts(self) -> None:
        runtime = self.runtime()
        seen: list[tuple[ForgeServices, bool]] = []
        runtime.when_ready(lambda services: seen.append((services, services.auto.running)))
        with mock.patch.object(ForgeServices, "start_watching", autospec=True) as watching:
            services = await runtime.start()
        assert services is not None
        self.assertEqual(seen, [(services, False)])
        watching.assert_called_once_with(services)
        late: list[ForgeServices] = []
        runtime.when_ready(late.append)  # already started: called at once
        self.assertEqual(late, [services])

    async def test_a_failing_callback_does_not_stop_the_start(self) -> None:
        runtime = self.runtime()

        def broken(services: ForgeServices) -> None:
            raise RuntimeError("tray gone")

        runtime.when_ready(broken)
        with self.assertLogs("app.forge.runtime", logging.ERROR):
            self.assertIsNotNone(await runtime.start())
        self.assertIs(runtime.state, ForgeState.READY)

    async def test_a_cancelled_caller_does_not_cancel_the_start(self) -> None:
        runtime = self.runtime()
        waiting = asyncio.ensure_future(runtime.start())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        services = await runtime.start()
        self.assertIsNotNone(services)
        self.assertIs(runtime.state, ForgeState.READY)

    async def test_a_failed_start_says_so_and_can_be_tried_again(self) -> None:
        blocker = self.data_dir / "forge"
        blocker.write_text("not a folder", encoding="utf-8")  # forge.db can't be made
        runtime = self.runtime()
        with self.assertLogs("app.forge.runtime", logging.ERROR) as logs:
            self.assertIsNone(await runtime.start())
        self.assertIs(runtime.state, ForgeState.FAILED)
        self.assertEqual(runtime.reason, t(FAILED_REASON))
        self.assertNotIn(str(self.data_dir), "\n".join(logs.output))  # the type only
        blocker.unlink()
        self.assertIsNotNone(await runtime.start())
        self.assertIs(runtime.state, ForgeState.READY)

    async def test_close_while_starting_closes_what_was_started(self) -> None:
        before = _forge_threads()
        runtime = self.runtime()
        starting = asyncio.ensure_future(runtime.start())
        await asyncio.sleep(0)  # the start is under way (opening forge.db)
        await runtime.aclose()
        self.assertIsNone(await starting)
        self.assertIs(runtime.state, ForgeState.CLOSED)
        self.assertIsNone(runtime.services)
        self.assertIsNone(await runtime.start())  # closed for good
        self.assertEqual(await _until_gone(before), set())

    async def test_close_stops_forge_and_leaves_no_thread_alive(self) -> None:
        before = _forge_threads()
        runtime = self.runtime()
        services = await runtime.start()
        assert services is not None
        await services.settings.update(automatic="trial")
        self.assertTrue(services.auto.running)
        await runtime.aclose()
        await runtime.aclose()  # twice is harmless
        self.assertFalse(services.auto.running)
        self.assertIs(runtime.state, ForgeState.CLOSED)
        self.assertIsNone(runtime.services)
        self.assertEqual(await _until_gone(before), set())

    async def test_a_close_that_fails_is_logged_not_raised(self) -> None:
        runtime = self.runtime()
        services = await runtime.start()
        assert services is not None
        with (
            mock.patch.object(services, "aclose", side_effect=OSError("disk gone")),
            self.assertLogs("app.forge.runtime", logging.ERROR),
        ):
            await runtime.aclose()
        await services.aclose()  # the real close, for the test's own clean-up
        self.assertIs(runtime.state, ForgeState.CLOSED)


class AirGrabberServicesTest(ForgeAsyncTestCase):
    """`Services.forge`: AirGrabber's composition root holds the runtime and closes it."""

    async def test_services_hold_a_forge_runtime_that_aclose_stops(self) -> None:
        from app.services import Services

        before = _forge_threads()
        paths = AppPaths(self.data_dir).ensure()
        services = await Services.create(paths)
        try:
            self.assertIsInstance(services.forge, ForgeRuntime)
            self.assertIs(services.forge.state, ForgeState.OFF)
            self.assertFalse((self.data_dir / "forge").exists())  # not opened until started
            with mock.patch("app.forge.runtime._parser_ready", return_value=True):
                forge = await services.forge.start()
            assert forge is not None
            self.assertTrue((self.data_dir / "forge" / "forge.db").is_file())
        finally:
            await services.aclose()
        self.assertIs(services.forge.state, ForgeState.CLOSED)
        self.assertEqual(await _until_gone(before), set())

