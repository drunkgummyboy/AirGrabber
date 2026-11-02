"""F-28: Forge stays off AirGrabber's start path, and guessit is warmed up once, out of the way
(INTEGRATION.md 3.6).

- Importing what AirGrabber's start imports (`main`, `app.services`, `app.ui.main_window`) loads
  neither guessit (nor rebulk / babelfish), Forge's services and forge.db, nor Forge's window: of
  `app.forge.ui` only the few light modules the main window wires in at start (checked in a
  fresh interpreter).
- `ForgeServices.warm_up()` parses one name once per run; `start_watching` warms up first when
  the renamer will look, else when the loop has been quiet for a while (`wait_until_quiet`).
- A Forge window opened before that shows "Getting ready…" while it warms up.

Temporary data dirs, HTTP on a MockTransport; the parser is faked except in one smoke test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import httpx

from app.config import AppPaths
from app.forge import services as forge_services
from app.forge.services import ForgeServices, wait_until_quiet
from app.forge.ui.window import GETTING_READY, ForgeWindow
from app.i18n import t
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump_until
from tests.forge.ui.test_host import HostCase

ROOT = Path(__file__).resolve().parents[2]
NEVER_AT_START = ("guessit", "rebulk", "babelfish", "app.forge.services", "app.forge.db")
# The Forge UI modules AirGrabber's main window imports at start (all light: no window, no
# Inbox, no Pillow work): the host it owns (F-21), the Forge look of the band and the tray
# (F-22), and the MediaForge group of its Settings (F-24 / F-25). Anything else is a regression.
LIGHT_AT_START = frozenset(
    {
        "app.forge.ui",
        "app.forge.ui.host",
        "app.forge.ui.look",
        "app.forge.ui.settings_pages",
        "app.forge.ui.settings_menu",
        "app.forge.ui.settings_naming",
    }
)
_PROBE = (
    "import json, sys\n"
    "import main\n"
    "import app.services\n"
    "import app.ui.main_window\n"
    "print(json.dumps(sorted(sys.modules)))\n"
)


def _loaded(prefixes: tuple[str, ...], modules: list[str]) -> list[str]:
    return [m for m in modules if any(m == p or m.startswith(p + ".") for p in prefixes)]


class StartPathImportsTest(unittest.TestCase):
    modules: list[str] = []

    @classmethod
    def setUpClass(cls) -> None:
        done = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if done.returncode != 0:
            raise AssertionError(done.stderr[-2000:])
        cls.modules = json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_probe_saw_airgrabbers_start(self) -> None:
        self.assertIn("app.ui.main_window", self.modules)
        self.assertIn("customtkinter", self.modules)

    def test_no_guessit_rebulk_babelfish_forge_services_or_forge_db(self) -> None:
        self.assertEqual(_loaded(NEVER_AT_START, self.modules), [])

    def test_of_forges_ui_only_the_light_modules(self) -> None:
        heavy = sorted(set(_loaded(("app.forge.ui",), self.modules)) - LIGHT_AT_START)
        self.assertEqual(heavy, [], "Forge UI modules imported at AirGrabber's start")
        self.assertNotIn("app.forge.ui.window", self.modules)


class _Parts:
    def __init__(self, root: Path) -> None:
        self.paths = AppPaths(root)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        self.tmdb = TMDBClient(self.http, api_key=lambda: "")
        self.images = ImageCache(self.http, root / "posters")
        self.settings = cast(SettingsService, mock.Mock(spec=SettingsService))


class WarmUpTests(ForgeAsyncTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        parts = _Parts(self.data_dir)
        self.addAsyncCleanup(parts.http.aclose)
        self.services = await ForgeServices.create(
            parts.paths, parts.http, parts.tmdb, parts.images, parts.settings
        )
        self.addAsyncCleanup(self.services.aclose)
        self.warmed: list[bool] = []  # whether the renamer was running at each warm-up
        self.enterContext(mock.patch.object(forge_services, "_parser_warm", False))
        self.enterContext(mock.patch.object(forge_services, "_warm_parser", self.fake_parser))
        self.quiet = asyncio.Event()

        async def until_quiet() -> bool:
            await self.quiet.wait()
            return True

        self.enterContext(mock.patch.object(forge_services, "wait_until_quiet", until_quiet))

    def fake_parser(self) -> None:
        self.warmed.append(self.services.auto.running)

    async def test_warm_up_parses_once_per_run(self) -> None:
        self.assertFalse(self.services.parser_warm)
        self.services.warm_up()
        self.services.warm_up()
        self.assertEqual(self.warmed, [False])
        self.assertTrue(self.services.parser_warm)

    async def test_a_failed_warm_up_is_logged_and_not_tried_again(self) -> None:
        def broken() -> None:
            raise ImportError(r"C:\Users\Alex\guessit is broken")

        with (
            mock.patch.object(forge_services, "_warm_parser", broken),
            self.assertLogs("app.forge.services", logging.WARNING) as logs,
        ):
            self.services.warm_up()
            self.services.warm_up()
        self.assertEqual(len(logs.records), 1)
        self.assertNotIn("Alex", logs.output[0])  # the type only, never the message
        self.assertTrue(self.services.parser_warm)

    async def test_a_renamer_that_looks_warms_up_before_it_starts(self) -> None:
        await self.services.settings.update(automatic="trial")  # starts it: stop it again
        await self.services.auto.stop()
        self.services.start_watching()
        self.assertEqual(self.warmed, [False])  # before the renamer's first look
        self.assertTrue(self.services.auto.running)
        self.assertIsNone(self.services._warm_task)

    async def test_with_the_renamer_off_the_warm_up_waits_for_a_quiet_moment(self) -> None:
        self.services.start_watching()
        await asyncio.sleep(0.05)
        self.assertEqual(self.warmed, [])
        self.quiet.set()
        await asyncio.sleep(0.05)
        self.assertEqual(self.warmed, [False])
        self.assertIsNone(self.services._warm_task)

    async def test_a_renamer_blocked_by_the_old_install_waits_too(self) -> None:
        await self.services.settings.update(automatic="trial")
        await self.services.auto.stop()
        self.services.auto.blocked_by_old_install = True
        self.services.start_watching()
        await asyncio.sleep(0.05)
        self.assertEqual(self.warmed, [])
        self.assertIsNotNone(self.services._warm_task)

    async def test_closing_cancels_a_waiting_warm_up(self) -> None:
        self.services.start_watching()
        task = self.services._warm_task
        assert task is not None
        await self.services.aclose()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.warmed, [])

    async def test_a_warm_up_done_meanwhile_isnt_repeated(self) -> None:
        self.services.start_watching()
        self.services.warm_up()  # the Forge window opened first
        self.quiet.set()
        await asyncio.sleep(0.05)
        self.assertEqual(self.warmed, [False])


class QuietTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_idle_loop_is_quiet(self) -> None:
        started = time.monotonic()
        self.assertTrue(await wait_until_quiet(tick_s=0.01, lag_s=0.05, ticks=3, max_s=5))
        self.assertLess(time.monotonic() - started, 1.0)

    async def test_a_busy_loop_isnt_and_the_wait_ends_anyway(self) -> None:
        stop = False

        async def busy() -> None:
            while not stop:
                time.sleep(0.03)  # holds the loop, as building a page would
                await asyncio.sleep(0)

        worker = asyncio.create_task(busy())
        try:
            self.assertFalse(await wait_until_quiet(tick_s=0.01, lag_s=0.01, ticks=3, max_s=0.4))
        finally:
            stop = True
            await worker


class RealWarmUpTest(ForgeAsyncTestCase):
    """One real warm-up (guessit is in requirements.txt's optional part: skipped without it)."""

    async def test_the_real_parser_warms_up(self) -> None:
        try:
            import guessit  # noqa: F401
        except ImportError:
            self.skipTest("guessit isn't installed")
        parts = _Parts(self.data_dir)
        self.addAsyncCleanup(parts.http.aclose)
        services = await ForgeServices.create(
            parts.paths, parts.http, parts.tmdb, parts.images, parts.settings
        )
        self.addAsyncCleanup(services.aclose)
        with (
            mock.patch.object(forge_services, "_parser_warm", False),
            self.assertLogs("app.forge.services", logging.INFO) as logs,
        ):
            services.warm_up()
            self.assertTrue(services.parser_warm)
        self.assertIn("File-name reader warmed up", logs.output[0])


class GettingReadyTests(HostCase):
    async def test_a_window_opened_before_the_warm_up_says_getting_ready_first(self) -> None:
        seen: list[tuple[str | None, bool]] = []

        def warm() -> None:
            window = self.host.window
            assert window is not None
            label = window.message_label
            seen.append((label.cget("text") if label is not None else None, window.queue is None))

        with (
            mock.patch.object(forge_services, "_parser_warm", False),
            mock.patch.object(forge_services, "_warm_parser", warm),
        ):
            window = await self.open()
        self.assertEqual(seen, [(t(GETTING_READY), True)])
        self.assertIsNotNone(window.queue)  # then the pages, and the plain page is gone
        self.assertIsNone(window.message)

    async def test_once_warm_the_window_opens_straight_to_its_pages(self) -> None:
        shown: list[int] = []
        original = ForgeWindow.show_getting_ready

        def spy(window: ForgeWindow) -> None:
            shown.append(1)
            original(window)

        with (
            mock.patch.object(forge_services, "_parser_warm", True),
            mock.patch.object(ForgeWindow, "show_getting_ready", spy),
        ):
            window = await self.open()
        self.assertEqual(shown, [])
        self.assertIsNotNone(window.queue)

    async def test_requested_files_still_reach_the_inbox_after_the_warm_up(self) -> None:
        paths = self.files("dl", "Dune.2021.mkv")
        with (
            mock.patch.object(forge_services, "_parser_warm", False),
            mock.patch.object(forge_services, "_warm_parser", lambda: None),
        ):
            await self.host._open_with(paths, None)
        services = self.services()
        await pump_until(self.root, lambda: len(services.queue.items) == 1)
        self.assertEqual([item.path for item in services.queue.items], paths)
