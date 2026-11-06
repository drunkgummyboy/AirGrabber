"""ForgeHost (app/forge/ui/host.py): MediaForge's page built at the first visit (Forge started
first; the plain frame and Try again while guessit is missing or forge.db failed), another page
showing, may_close, right-click and shortcut requests (plain → the queue; confirm → one confirm
window per preset; "Open in MediaForge" back to the queue), the renamer's status / count /
notifications handed on, the shutdown, and that importing the host stays light (INTEGRATION.md
3.6). With the real main window: tests/forge/ui/test_page_in_main_window.py.

On a real ForgeRuntime with a fake guessit check (a temporary forge.db, TMDB without a key, HTTP
on a MockTransport)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import httpx

from app.config import AppPaths
from app.forge.core.auto import WatchStatus
from app.forge.core.queue import ItemStatus
from app.forge.runtime import FAILED_REASON, ForgeRuntime, ForgeState
from app.forge.services import ForgeServices
from app.forge.ui.confirm_window import ConfirmWindow
from app.forge.ui.host import GUESSIT, NOT_READY_TEXT, ForgeHost
from app.forge.ui.page import ForgePage
from app.forge.ui.settings_menu import SECTION_MENU
from app.forge.ui.settings_pages import SECTION_LIBRARY
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from app.single_instance import ForgeRequest
from app.ui import theme
from tests.forge.tk import pump, pump_until
from tests.forge.ui.test_page import TkCase

ROOT = Path(__file__).resolve().parents[3]


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


class HostCase(TkCase):
    ready = True  # whether guessit "is installed"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.make_stage()
        self.show_root()
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
        self.settings_asked: list[str | None] = []
        self.shown: list[int] = []
        self.restarts: list[int] = []
        self.retried: list[str] = []
        self.host = self.make_host()
        self.closed: list[int] = []
        self.host.on_closed = lambda: self.closed.append(1)

    def make_host(self) -> ForgeHost:
        def show_page() -> None:
            self.shown.append(1)
            host.show()

        host = ForgeHost(
            self.root,
            self.runtime,
            page_master=self.holder,
            tabs_row=self.tabs_row,
            show_page=show_page,
            show_settings=self.settings_asked.append,
            restart=lambda: self.restarts.append(1),
            retry_install=self.retried.append,
        )
        self.addAsyncCleanup(host.shutdown)
        return host

    async def open(self) -> ForgePage:
        page = await self.host.open()
        assert page is not None
        if page.queue is not None:
            queue = page.queue
            await pump_until(self.root, lambda: queue.loaded)
            await pump_until(self.root, lambda: page.pages_built)  # History: later
        await pump(self.root, 0.1)
        return page

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
    async def test_open_starts_forge_and_builds_the_page(self) -> None:
        self.assertIs(self.runtime.state, ForgeState.OFF)
        self.assertIsNone(self.host.page)
        page = await self.open()
        self.assertIs(self.runtime.state, ForgeState.READY)
        self.assertIs(self.host.page, page)
        self.assertIs(page.master, self.holder)
        self.assertIsNotNone(page.queue)
        self.assertTrue(self.host.showing)
        self.assertFalse(self.host.windows_open)  # no confirm window, no request on its way
        queue = page.queue
        self.assertIs(await self.host.open(), page)  # built once
        self.assertIs(page.queue, queue)

    async def test_files_that_need_you_before_the_first_visit_light_its_count(self) -> None:
        told: list[int] = []
        self.host.on_needs = told.append
        services = await self.runtime.start()
        assert services is not None
        await services.queue.add_paths(self.files("in", "Dune.2021.mkv", "Heat.1995.mkv"), True)
        await services.queue.wait_idle()
        for item in services.queue.items:  # TMDB isn't sure about either: they need you
            item.status = ItemStatus.NEEDS_CHOICE
        await self.open()
        await pump_until(self.root, lambda: self.host.needs == 2)
        self.assertEqual(told[-1], 2)

    async def test_two_opens_at_once_make_one_page(self) -> None:
        first, second = await asyncio.gather(self.host.open(), self.host.open())
        self.assertIs(first, second)

    async def test_show_is_open_in_the_background(self) -> None:
        self.host.show()
        self.assertTrue(self.host.showing)
        await pump_until(self.root, lambda: self.host.page is not None)
        page = self.host.page
        assert page is not None
        await pump_until(self.root, lambda: page.queue is not None)
        self.assertTrue(page.shown)

    async def test_another_page_showing(self) -> None:
        page = await self.open()
        self.host.close()
        self.assertFalse(self.host.showing)
        self.assertFalse(page.shown)
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [])
        self.assertEqual(self.closed, [])  # not a confirm window: nobody may leave for it
        self.host.show()
        self.assertTrue(page.shown)
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [page.queue_tools])

    async def test_the_views_go_through_it(self) -> None:
        views: list[bool] = []
        self.host.on_view = views.append
        self.host.toggle_history()  # no page yet: nothing
        page = await self.open()
        views.clear()
        self.host.toggle_history()
        self.assertEqual(page.view, "history")
        self.host.show_queue()
        self.assertEqual(page.view, "queue")
        self.assertEqual(views, [True, False])

    async def test_what_the_queue_says_reaches_on_message(self) -> None:
        heard: list[str] = []
        self.host.on_message = heard.append
        page = await self.open()
        assert page.queue is not None
        page.queue._say("Put back 1 file")
        self.assertEqual(heard, ["Put back 1 file"])


class SettingsTests(HostCase):
    """Settings is the main window's: the host asks for it. On the real window:
    tests/forge/ui/test_page_in_main_window.py."""

    async def test_the_status_line_asks_for_settings_at_auto_rename(self) -> None:
        page = await self.open()
        assert page.status_button is not None
        page.status_button.invoke()
        self.host.open_settings(SECTION_MENU)
        self.assertEqual(self.settings_asked, [SECTION_LIBRARY, SECTION_MENU])

    async def test_nothing_once_shut(self) -> None:
        await self.host.shutdown()
        self.host.open_settings(SECTION_MENU)
        self.assertEqual(self.settings_asked, [])


class NotReadyTests(HostCase):
    ready = False

    async def test_the_plain_frame_and_try_again_restart(self) -> None:
        page = await self.open()
        self.assertIs(self.runtime.state, ForgeState.NOT_READY)
        assert page.message_label is not None and page.try_again_button is not None
        self.assertEqual(
            page.message_label.cget("text"),
            "MediaForge isn't ready yet: one of its parts couldn't be installed.",
        )
        self.assertEqual(NOT_READY_TEXT, page.message_label.cget("text"))
        self.assertIsNone(page.queue)
        page.try_again_button.invoke()
        self.assertEqual(self.retried, [GUESSIT])
        self.assertEqual(self.restarts, [1])
        self.assertIs(await self.host.open(), page)  # not started again: it stays off

    async def test_a_right_click_shows_the_page_saying_why_instead(self) -> None:
        paths = self.files("rc", "Dune.2021.mkv")
        self.host.handle(ForgeRequest(paths=(str(paths[0]),), confirm=True))
        await pump_until(self.root, lambda: self.host.page is not None, within=5)
        page = self.host.page
        assert page is not None
        await pump_until(self.root, lambda: page.try_again_button is not None)
        self.assertEqual(self.shown, [1])  # the main window comes: it has to say why
        self.assertEqual(self.host.confirm_windows, [])

    async def test_the_default_try_again_forgets_guessits_failure(self) -> None:
        host = ForgeHost(
            self.root,
            self.runtime,
            page_master=self.holder,
            tabs_row=self.tabs_row,
            restart=lambda: self.restarts.append(1),
        )
        self.addAsyncCleanup(host.shutdown)
        with mock.patch("app.bootstrap.retry_optional", return_value=True) as retry:
            await self.runtime.start()
            host.try_again()
        retry.assert_called_once_with("guessit")
        self.assertEqual(self.restarts, [1])


class FailedStartTests(HostCase):
    async def test_try_again_starts_forge_again(self) -> None:
        with mock.patch.object(ForgeServices, "create", side_effect=OSError("locked")):
            page = await self.open()
        self.assertIs(self.runtime.state, ForgeState.FAILED)
        assert page.message_label is not None and page.try_again_button is not None
        self.assertEqual(page.message_label.cget("text"), FAILED_REASON)
        page.try_again_button.invoke()
        await pump_until(self.root, lambda: page.queue is not None)
        self.assertIs(self.runtime.state, ForgeState.READY)
        self.assertIsNone(page.message)
        self.assertEqual(self.restarts, [])


class ClosingTests(HostCase):
    async def test_may_close(self) -> None:
        retry = mock.Mock()
        self.assertTrue(self.host.may_close(retry))  # no page yet
        page = await self.open()
        self.assertTrue(self.host.may_close(retry))
        assert page.queue is not None
        page.queue.request_close = lambda close: False  # type: ignore[method-assign]
        self.assertFalse(self.host.may_close(retry))
        page.queue.request_close = lambda close: True  # type: ignore[method-assign]

    async def test_shutdown_closes_the_page_and_every_confirm_window(self) -> None:
        page = await self.open()
        paths = self.files("rc", "Dune.2021.mkv")
        self.host.handle(ForgeRequest(paths=(str(paths[0]),), confirm=True))
        await pump_until(self.root, lambda: len(self.host.confirm_windows) == 1)
        confirm = self.host.confirm_windows[0]
        await self.host.shutdown()
        self.assertFalse(page.winfo_exists())
        self.assertFalse(confirm.winfo_exists())
        self.assertIsNone(self.host.page)
        self.assertEqual(self.closed, [])  # quitting isn't "a window closed"
        self.assertIsNone(await self.host.open())
        self.host.handle(ForgeRequest(paths=(str(paths[0]),)))
        await pump(self.root, 0.1)
        self.assertIsNone(self.host.page)
        self.assertEqual(self.shown, [])


class RequestTests(HostCase):
    async def test_a_plain_request_opens_the_queue_with_its_files_and_preset(self) -> None:
        paths = self.files("in", "Dune.2021.mkv", "Heat.1995.mkv")
        self.host.handle(
            ForgeRequest(paths=tuple(str(p) for p in paths), preset="Movie (Flat)")
        )
        self.assertTrue(self.host.windows_open)  # on its way
        self.assertEqual(self.shown, [1])  # the main window comes at once
        await pump_until(self.root, lambda: self.runtime.services is not None)
        services = self.runtime.services
        assert services is not None
        await pump_until(self.root, lambda: len(services.queue.items) == 2)
        page = self.host.page
        assert page is not None and page.queue is not None
        queue = page.queue
        await pump_until(self.root, lambda: queue.loaded)

        def named() -> list[str]:
            return [getattr(queue.choice_for(i).preset, "name", "") for i in services.queue.items]

        # The request's preset names the files it added (each film's band).
        await pump_until(self.root, lambda: named() == ["Movie (Flat)", "Movie (Flat)"])
        self.assertEqual(page.view, "queue")

    async def test_a_plain_request_on_history_goes_to_the_queue(self) -> None:
        page = await self.open()
        page.toggle_history()
        paths = self.files("in", "Dune.2021.mkv")
        self.host.handle(ForgeRequest(paths=(str(paths[0]),)))
        services = self.services()
        await pump_until(self.root, lambda: len(services.queue.items) == 1)
        self.assertEqual(page.view, "queue")

    async def test_forge_alone_shows_the_page(self) -> None:
        self.host.handle(ForgeRequest())
        self.assertEqual(self.shown, [1])
        await pump_until(self.root, lambda: self.host.page is not None)
        await pump_until(self.root, lambda: not self.host.windows_open)
        page = self.host.page
        assert page is not None
        self.assertIsNotNone(page.queue)

    async def test_a_preset_alone_goes_to_the_queues_bands(self) -> None:
        page = await self.open()
        with mock.patch.object(ForgePage, "select_preset") as select:
            self.host.handle(ForgeRequest(preset="Movie (Flat)"))
            await pump_until(self.root, lambda: not self.host.windows_open)
        select.assert_called_once_with("Movie (Flat)")
        self.assertIsNotNone(page.queue)

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
        self.assertIsNone(self.host.page)  # the page isn't built for it
        self.assertEqual(self.shown, [])  # and the main window stays as it is
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
        self.assertTrue(self.host.windows_open)  # the page is on its way: stay
        self.assertEqual(self.shown, [1])
        services = self.services()
        await pump_until(self.root, lambda: len(services.queue.items) == 1)
        self.assertIsNotNone(self.host.page)


class RenamerTests(HostCase):
    async def test_the_renamer_reports_through_the_host(self) -> None:
        needs: list[int] = []
        notes: list[tuple[str, str]] = []
        self.host.on_needs = needs.append
        self.host.notify = lambda title, text: notes.append((title, text))
        services = await self.runtime.start()
        assert services is not None
        auto = services.auto
        auto.on_status(WatchStatus("trial", "Manual: watching 1 folder"))  # before the page
        auto.on_needs(3)
        auto.notify("3 files need you", "Open MediaForge to pick or confirm them.")
        self.assertEqual(needs, [3])  # the renamer's until the queue is there
        self.assertEqual(self.host.needs, 3)
        self.assertEqual(notes, [("3 files need you", "Open MediaForge to pick or confirm them.")])
        assert self.host.status is not None
        self.assertEqual(self.host.status.state, "trial")
        page = await self.open()
        self.root.geometry("1600x800")  # room for all of it, whatever this PC's font measures
        await pump(self.root, 0.2)
        self.assertEqual(page.status_shown, "Manual: watching 1 folder")
        auto.on_status(WatchStatus("watching", "Watching 1 folder"))
        self.assertEqual(page.status_shown, "Watching 1 folder")

    async def test_the_queues_count_takes_over_once_it_is_there(self) -> None:
        needs: list[int] = []
        self.host.on_needs = needs.append
        services = await self.runtime.start()
        assert services is not None
        services.auto.on_needs(3)
        self.assertEqual(self.host.needs, 3)
        await self.open()
        await pump_until(self.root, lambda: self.host.needs == 0)  # the queue is empty
        services.auto.on_needs(4)  # the renamer's next look: the queue has its files
        self.assertEqual(self.host.needs, 0)
        self.assertEqual(needs[0], 3)

    async def test_the_real_renamer_lights_the_status_line(self) -> None:
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
        page = await self.open()
        self.root.geometry("1600x800")  # room for all of it, whatever this PC's font measures
        await pump(self.root, 0.2)
        self.assertEqual(page.status_shown, "Manual: watching 1 folder")
        self.assertEqual(page.light, theme.WARNING)
        self.assertEqual(self.host.needs, 0)

    async def test_a_failing_callback_doesnt_stop_the_renamer(self) -> None:
        def broken(*_args: object) -> None:
            raise RuntimeError("boom")

        self.host.on_needs = broken
        self.host.notify = broken
        services = await self.runtime.start()
        assert services is not None
        with self.assertLogs("app.forge.ui.host", "ERROR"):
            services.auto.on_needs(1)
            services.auto.notify("a", "b")
        self.assertEqual(self.host.needs, 1)


class ImportTests(unittest.TestCase):
    def test_importing_the_host_stays_light(self) -> None:
        code = (
            "import sys, app.forge.ui.host\n"
            "heavy = [m for m in sys.modules if m in ('app.forge.ui.page', "
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
