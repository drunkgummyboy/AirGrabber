"""ForgeHost (app/forge/ui/host.py): opening the Forge window (Forge started first; the plain
page and Try again while guessit is missing or forge.db failed), closing it (hidden, size and
place remembered, AirGrabber untouched), may_close, right-click and shortcut requests (plain →
the Inbox; confirm → one confirm window per preset; "Open in MediaForge" back to the Inbox), the
renamer's status / count / notifications handed on, a drop through file_drops' fake seam, the
shutdown, and that importing the host stays light (INTEGRATION.md 3.6).

On a real ForgeRuntime with a fake guessit check (a temporary forge.db, TMDB without a key, HTTP
on a MockTransport)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest import mock

import httpx

from app.config import AppPaths
from app.forge.core.auto import WatchStatus
from app.forge.core.queue import ItemStatus
from app.forge.runtime import FAILED_REASON, ForgeRuntime, ForgeState
from app.forge.services import ForgeServices
from app.forge.ui.confirm_window import ConfirmWindow
from app.forge.ui.host import GUESSIT, NOT_READY_TEXT, ForgeHost
from app.forge.ui.settings_menu import SECTION_MENU
from app.forge.ui.settings_pages import FORGE_SECTIONS, SECTION_LIBRARY
from app.forge.ui.settings_window import ForgeSettingsWindow
from app.forge.ui.window import ForgeWindow, parse_geometry
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from app.single_instance import ForgeRequest
from app.ui import theme
from tests.forge.tk import pump, pump_until
from tests.forge.ui.test_window import TkCase

ROOT = Path(__file__).resolve().parents[3]


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


class HostCase(TkCase):
    ready = True  # whether guessit "is installed"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.addAsyncCleanup(http.aclose)
        self.runtime = ForgeRuntime(
            AppPaths(self.data_dir),
            http,
            TMDBClient(http, api_key=lambda: ""),
            ImageCache(http, self.data_dir / "posters"),
            cast(SettingsService, mock.Mock(spec=SettingsService)),
            parser_ready=lambda: self.ready,
        )
        self.addAsyncCleanup(self.runtime.aclose)
        self.airgrabber_settings: list[int] = []
        self.restarts: list[int] = []
        self.retried: list[str] = []
        self.host = ForgeHost(
            self.root,
            self.runtime,
            open_airgrabber_settings=lambda: self.airgrabber_settings.append(1),
            restart=lambda: self.restarts.append(1),
            retry_install=self.retried.append,
        )
        self.addAsyncCleanup(self.host.shutdown)
        self.closed: list[int] = []
        self.host.on_closed = lambda: self.closed.append(1)
        original = ForgeHost._make_window

        def see_through(host: ForgeHost, services: ForgeServices | None) -> ForgeWindow:
            window = original(host, services)
            window.attributes("-alpha", 0.0)  # laid out and mapped, but see-through
            return window

        self.enterContext(mock.patch.object(ForgeHost, "_make_window", see_through))

    async def open(self) -> ForgeWindow:
        window = await self.host.open()
        assert window is not None
        if window.queue is not None:
            queue = window.queue
            await pump_until(window, lambda: queue.loaded)
            await pump_until(window, lambda: window.pages_built)  # History, Auto-rename: later
        await pump(window, 0.1)
        return window

    def services(self) -> ForgeServices:
        services = self.runtime.services
        assert services is not None
        return services

    def files(self, folder: str, *names: str) -> list[Path]:
        (self.tmp / folder).mkdir(parents=True, exist_ok=True)
        paths = [self.tmp / folder / name for name in names]
        for path in paths:
            path.write_bytes(b"video")
        return paths


class OpenTests(HostCase):
    async def test_open_starts_forge_and_shows_the_window(self) -> None:
        self.assertIs(self.runtime.state, ForgeState.OFF)
        self.assertIsNone(self.host.window)
        window = await self.open()
        self.assertIs(self.runtime.state, ForgeState.READY)
        self.assertIs(self.host.window, window)
        self.assertEqual(window.state(), "normal")
        self.assertIsNotNone(window.queue)
        self.assertTrue(self.host.windows_open)
        self.assertIs(await self.host.open(), window)  # once, then raised again

    async def test_files_that_need_you_before_the_window_opens_light_its_badge(self) -> None:
        services = await self.runtime.start()
        assert services is not None
        await services.queue.add_paths(self.files("in", "Dune.2021.mkv", "Heat.1995.mkv"), True)
        await services.queue.wait_idle()
        for item in services.queue.items:  # TMDB isn't sure about either: they need you
            item.status = ItemStatus.NEEDS_CHOICE
        window = await self.open()
        await pump_until(window, lambda: window.needs == 2)
        self.assertEqual(window.band.item_text("badge"), "2")

    async def test_two_opens_at_once_make_one_window(self) -> None:
        first, second = await asyncio.gather(self.host.open(), self.host.open())
        self.assertIs(first, second)

    async def test_a_window_comes_back_where_it_was(self) -> None:
        services = await self.runtime.start()
        assert services is not None
        await services.settings.update(window_geometry="1100x700+50+60")
        window = await self.open()
        saved = parse_geometry(window.saved_geometry())
        assert saved is not None
        self.assertEqual(saved[:2], (1100, 700))

    async def test_more_in_settings_opens_mediaforges_settings_at_library(self) -> None:
        window = await self.open()
        assert window.library is not None
        window.library.more_button.invoke()
        settings = self.host.settings_window
        assert settings is not None
        self.assertIsInstance(settings, ForgeSettingsWindow)
        self.assertEqual(settings.section, SECTION_LIBRARY)
        self.assertIs(settings.master, window)
        self.assertEqual(self.airgrabber_settings, [])  # not AirGrabber's Settings


class SettingsWindowTests(HostCase):
    """MediaForge's own Settings window (app/forge/ui/settings_window.py) through the host."""

    async def settings(self) -> tuple[ForgeWindow, ForgeSettingsWindow]:
        window = await self.open()
        window.band.settings_button.invoke()  # ⚙
        settings = self.host.settings_window
        assert settings is not None
        settings.attributes("-alpha", 0.0)
        await pump(window, 0.05)
        return window, settings

    async def test_the_gear_opens_it_at_its_first_section(self) -> None:
        window, settings = await self.settings()
        self.assertEqual(settings.section, next(iter(FORGE_SECTIONS)))
        self.assertEqual(settings.sections, list(FORGE_SECTIONS))
        self.assertEqual(str(settings.transient()), str(window))  # it belongs to the Forge window
        self.assertEqual(settings.title(), "MediaForge Settings")
        self.assertEqual(self.airgrabber_settings, [])

    async def test_a_second_open_raises_the_same_one_at_the_asked_section(self) -> None:
        window, settings = await self.settings()
        self.assertIs(self.host.open_settings(SECTION_MENU), settings)
        self.assertEqual(settings.section, SECTION_MENU)
        self.assertIs(self.host.open_settings(), settings)  # ⚙ again: the page it is on
        self.assertEqual(settings.section, SECTION_MENU)
        assert window.library is not None
        window.library.more_button.invoke()
        self.assertIs(self.host.settings_window, settings)
        self.assertEqual(settings.section, SECTION_LIBRARY)
        tops = [w for w in window.winfo_children() if isinstance(w, ForgeSettingsWindow)]
        self.assertEqual(tops, [settings])

    async def test_open_airgrabbers_settings_calls_the_hosts_callback(self) -> None:
        _window, settings = await self.settings()
        settings.airgrabber_button.invoke()
        self.assertEqual(self.airgrabber_settings, [1])
        self.assertTrue(settings.winfo_exists())  # MediaForge's own stays open

    async def test_closing_the_forge_window_closes_it_too(self) -> None:
        window, settings = await self.settings()
        self.host.close()
        await pump(window, 0.05)
        self.assertFalse(settings.winfo_exists())
        self.assertIsNone(self.host.settings_window)
        window = await self.open()
        self.host.open_settings()
        self.assertIsNotNone(self.host.settings_window)
        await self.host.shutdown()
        self.assertIsNone(self.host.settings_window)

    async def test_without_a_forge_window_nothing_opens(self) -> None:
        self.assertIsNone(self.host.open_settings())
        self.assertIsNone(self.host.settings_window)


class NotReadyTests(HostCase):
    ready = False

    async def test_the_plain_page_and_try_again_restart(self) -> None:
        window = await self.open()
        self.assertIs(self.runtime.state, ForgeState.NOT_READY)
        assert window.message_label is not None and window.try_again_button is not None
        self.assertEqual(
            window.message_label.cget("text"),
            "MediaForge isn't ready yet: one of its parts couldn't be installed.",
        )
        self.assertEqual(NOT_READY_TEXT, window.message_label.cget("text"))
        self.assertIsNone(window.queue)
        self.assertIsNone(window.tabs)
        window.try_again_button.invoke()
        self.assertEqual(self.retried, [GUESSIT])
        self.assertEqual(self.restarts, [1])
        self.assertIs(await self.host.open(), window)  # not started again: it stays off

    async def test_a_right_click_shows_the_forge_window_instead(self) -> None:
        paths = self.files("rc", "Dune.2021.mkv")
        self.host.handle(ForgeRequest(paths=(str(paths[0]),), confirm=True))
        await pump_until(self.root, lambda: self.host.window is not None, within=5)
        window = self.host.window
        assert window is not None
        await pump_until(window, lambda: window.message_label is not None)
        self.assertEqual(self.host.confirm_windows, [])

    async def test_the_default_try_again_forgets_guessits_failure(self) -> None:
        host = ForgeHost(self.root, self.runtime, restart=lambda: self.restarts.append(1))
        self.addAsyncCleanup(host.shutdown)
        with mock.patch("app.bootstrap.retry_optional", return_value=True) as retry:
            await self.runtime.start()
            host.try_again()
        retry.assert_called_once_with("guessit")
        self.assertEqual(self.restarts, [1])


class FailedStartTests(HostCase):
    async def test_try_again_starts_forge_again(self) -> None:
        with mock.patch.object(ForgeServices, "create", side_effect=OSError("locked")):
            window = await self.open()
        self.assertIs(self.runtime.state, ForgeState.FAILED)
        assert window.message_label is not None and window.try_again_button is not None
        self.assertEqual(window.message_label.cget("text"), FAILED_REASON)
        window.try_again_button.invoke()
        await pump_until(window, lambda: window.queue is not None)
        self.assertIs(self.runtime.state, ForgeState.READY)
        self.assertIsNone(window.message)
        self.assertEqual(self.restarts, [])


class CloseTests(HostCase):
    async def test_close_hides_the_window_and_remembers_its_size(self) -> None:
        window = await self.open()
        window.geometry("1150x720+40+50")
        await pump(window, 0.2)
        self.host.close()
        self.assertEqual(window.state(), "withdrawn")
        self.assertTrue(window.winfo_exists())
        self.assertTrue(self.root.winfo_exists())  # AirGrabber keeps running
        self.assertEqual(self.closed, [1])
        self.assertFalse(self.host.windows_open)
        settings = self.services().settings
        await pump_until(self.root, lambda: settings.current.window_geometry is not None)
        saved = parse_geometry(settings.current.window_geometry)
        assert saved is not None
        self.assertEqual(saved[:2], (1150, 720))
        self.host.close()  # hidden already: nothing more
        self.assertEqual(self.closed, [1])
        self.assertIs(await self.host.open(), window)
        self.assertEqual(window.state(), "normal")

    async def test_the_windows_close_button_hides_it(self) -> None:
        window = await self.open()
        window.on_close()
        self.assertEqual(window.state(), "withdrawn")
        self.assertEqual(self.closed, [1])

    async def test_may_close(self) -> None:
        retry = mock.Mock()
        self.assertTrue(self.host.may_close(retry))  # no window yet
        window = await self.open()
        self.assertTrue(self.host.may_close(retry))
        assert window.queue is not None
        window.queue.request_close = lambda close: False  # type: ignore[method-assign]
        self.assertFalse(self.host.may_close(retry))

    async def test_shutdown_saves_and_closes_every_window(self) -> None:
        window = await self.open()
        paths = self.files("rc", "Dune.2021.mkv")
        self.host.handle(ForgeRequest(paths=(str(paths[0]),), confirm=True))
        await pump_until(self.root, lambda: len(self.host.confirm_windows) == 1)
        confirm = self.host.confirm_windows[0]
        await self.host.shutdown()
        self.assertFalse(window.winfo_exists())
        self.assertFalse(confirm.winfo_exists())
        self.assertIsNone(self.host.window)
        self.assertEqual(self.closed, [])  # quitting isn't "a window closed"
        self.assertIsNotNone(self.services().settings.current.window_geometry)
        self.assertIsNone(await self.host.open())
        self.host.handle(ForgeRequest(paths=(str(paths[0]),)))
        await pump(self.root, 0.1)
        self.assertIsNone(self.host.window)


class RequestTests(HostCase):
    async def test_a_plain_request_opens_the_queue_with_its_files_and_preset(self) -> None:
        paths = self.files("in", "Dune.2021.mkv", "Heat.1995.mkv")
        self.host.handle(
            ForgeRequest(paths=tuple(str(p) for p in paths), preset="Movie (Flat)")
        )
        self.assertTrue(self.host.windows_open)  # on its way
        await pump_until(self.root, lambda: self.host.window is not None)
        services = self.runtime.services
        assert services is not None
        await pump_until(self.root, lambda: len(services.queue.items) == 2)
        window = self.host.window
        assert window is not None and window.queue is not None
        queue = window.queue
        await pump_until(window, lambda: queue.loaded)

        def named() -> list[str]:
            return [getattr(queue.choice_for(i).preset, "name", "") for i in services.queue.items]

        # The request's preset names the files it added (each film's band).
        await pump_until(window, lambda: named() == ["Movie (Flat)", "Movie (Flat)"])
        self.assertEqual(window.current_page, "queue")

    async def test_forge_alone_opens_the_window(self) -> None:
        self.host.handle(ForgeRequest())
        await pump_until(self.root, lambda: self.host.window is not None)
        window = self.host.window
        assert window is not None
        await pump_until(window, lambda: window.state() == "normal")

    async def test_confirm_requests_make_one_window_per_preset(self) -> None:
        series = self.files("tv", "Ted.Lasso.S04E03.mkv", "Ted.Lasso.S04E04.mkv")
        movies = self.files("films", "Dune.2021.mkv")
        requests = [  # Explorer starts one AirGrabber per file; one came twice
            *((path, "Series (Flat)") for path in series),
            (movies[0], "Movie (Flat)"),
            (series[0], "Series (Flat)"),
        ]
        for path, preset in requests:
            self.host.handle(ForgeRequest(paths=(str(path),), preset=preset, confirm=True))
        await pump_until(self.root, lambda: len(self.host.confirm_windows) == 2)
        await pump_until(self.root, lambda: all(w.preset for w in self.host.confirm_windows))
        windows = {w.preset.name: w for w in self.host.confirm_windows if w.preset}
        self.assertEqual(sorted(windows), ["Movie (Flat)", "Series (Flat)"])
        self.assertEqual(sorted(windows["Series (Flat)"].paths), sorted(series))  # once each
        self.assertEqual(windows["Movie (Flat)"].paths, movies)
        self.assertIsNone(self.host.window)  # the Forge window stays shut
        self.assertTrue(self.host.windows_open)
        windows["Movie (Flat)"].cancel()
        self.assertEqual(self.closed, [1])
        self.assertEqual(len(self.host.confirm_windows), 1)
        self.assertTrue(self.host.windows_open)
        windows["Series (Flat)"].cancel()
        self.assertEqual(self.closed, [1, 1])
        self.assertFalse(self.host.windows_open)  # D5: the last one closed

    async def test_open_in_mediaforge_hands_the_files_to_the_queue(self) -> None:
        paths = self.files("rc", "Dune.2021.mkv")
        request = ForgeRequest(paths=(str(paths[0]),), preset="Movie (Flat)", confirm=True)
        self.host.handle(request)
        await pump_until(self.root, lambda: len(self.host.confirm_windows) == 1)
        confirm: ConfirmWindow = self.host.confirm_windows[0]
        await confirm.loaded()
        confirm.open_in_forge()
        self.assertEqual(self.host.confirm_windows, [])
        self.assertTrue(self.host.windows_open)  # the Forge window is on its way: stay
        services = self.services()
        await pump_until(self.root, lambda: len(services.queue.items) == 1)
        window = self.host.window
        assert window is not None
        self.assertEqual(window.state(), "normal")


class RenamerTests(HostCase):
    async def test_the_renamer_reports_through_the_host(self) -> None:
        needs: list[int] = []
        notes: list[tuple[str, str]] = []
        self.host.on_needs = needs.append
        self.host.notify = lambda title, text: notes.append((title, text))
        services = await self.runtime.start()
        assert services is not None
        auto = services.auto
        auto.on_status(WatchStatus("trial", "Trial: watching 1 folder"))  # before the window
        auto.on_needs(3)
        auto.notify("3 files need you", "Open MediaForge to pick or confirm them.")
        self.assertEqual(needs, [3])
        self.assertEqual(self.host.needs, 3)
        self.assertEqual(notes, [("3 files need you", "Open MediaForge to pick or confirm them.")])
        assert self.host.status is not None
        self.assertEqual(self.host.status.state, "trial")
        window = await self.open()
        self.assertEqual(window.band.chip_text, "Trial: watching 1 folder")
        assert window.library is not None
        auto.on_status(WatchStatus("watching", "Watching 1 folder"))
        self.assertEqual(window.band.chip_text, "Watching 1 folder")
        self.assertEqual(window.library.status_label.cget("text"), "Watching 1 folder")
        self.assertEqual(window.library.state, "watching")

    async def test_the_real_renamer_lights_the_band(self) -> None:
        services = await self.runtime.start()
        assert services is not None
        for name in ("downloads", "Series", "Movies"):
            (self.tmp / name).mkdir()
        await services.settings.update(
            watch_folders=[str(self.tmp / "downloads")],
            series_folder=str(self.tmp / "Series"),
            movies_folder=str(self.tmp / "Movies"),
            automatic="trial",
        )
        await pump_until(
            self.root, lambda: self.host.status is not None and self.host.status.state == "trial"
        )
        window = await self.open()
        self.assertEqual(window.band.chip_text, "Trial: watching 1 folder")
        self.assertEqual(window.band.light, theme.WARNING)
        assert window.library is not None
        self.assertEqual(window.library.state, "trial")
        self.assertEqual(self.host.needs, 0)

    async def test_a_failing_callback_doesnt_stop_the_renamer(self) -> None:
        def broken(*_args: Any) -> None:
            raise RuntimeError("boom")

        self.host.on_needs = broken
        self.host.notify = broken
        services = await self.runtime.start()
        assert services is not None
        with self.assertLogs("app.forge.ui.host", "ERROR"):
            services.auto.on_needs(1)
            services.auto.notify("a", "b")
        self.assertEqual(self.host.needs, 1)


class DropTests(HostCase):
    async def test_a_drop_on_the_window_reaches_the_queue(self) -> None:
        window = await self.open()
        deliver: Callable[[list[str]], None] = self.drops.targets[window.winfo_id()]
        paths = self.files("dropped", "Dune.2021.mkv")
        folder = self.files("folder", "Heat.1995.mkv")[0].parent
        deliver([str(paths[0]), str(folder)])
        services = self.services()
        await pump_until(window, lambda: len(services.queue.items) == 2)


class ImportTests(unittest.TestCase):
    def test_importing_the_host_stays_light(self) -> None:
        code = (
            "import sys, app.forge.ui.host\n"
            "heavy = [m for m in sys.modules if m in ('app.forge.ui.window', "
            "'app.forge.ui.confirm_window', 'app.forge.services', 'app.forge.ui.queue_view') "
            "or m == 'guessit' or m.startswith('guessit.')]\n"
            "print(heavy)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
