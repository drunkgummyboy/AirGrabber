"""ForgeHost: MediaForge's page and confirm windows inside AirGrabber (docs/forge/PAGE.md).

AirGrabber's main window owns one (F-21) and calls it:
- `show()` — the main window shows MediaForge's page (`Mode.FORGE`): the first visit builds the page
  (app/forge/ui/page.py) into `page_master` and the tabs row's left part (`tabs_row`), starting
  Forge if it hasn't. `await open()` is the same, awaited: the page, or None when AirGrabber is
  shutting down. While Forge is starting or its file-name reader warms up (F-28,
  app/forge/services.py) the page says "Getting ready…"; while it isn't ready (guessit couldn't be
  installed, NOT_READY) one plain frame with Try again: forget guessit's remembered install failure
  (`app.bootstrap.retry_optional`), then AirGrabber's restart. When forge.db couldn't be opened
  (FAILED), Try again starts Forge again.
- `close()` — another page shows: the tabs row's part goes, Activity closes. Nothing else stops: a
  run goes on, on any page and in the tray.
- `show_queue()`, `toggle_history()` (🕘), `refresh_page()` (F5) — handed to the page.
- `open_settings(section)` — Settings at `section` (the status line's click), through AirGrabber's
  main window (`show_settings`).
- `may_close(retry)` — before AirGrabber quits or restarts: False while the queue's Forge run
  stops after its current file (`QueueView.request_close`), History's undo finishes or a
  confirm window renames or undoes (`ConfirmWindow.busy`); `retry` is called then.
- `handle(request)` — a right-click / "MediaForge" shortcut / tray request or a drop
  (app/single_instance.py's ForgeRequest): plain → `show_page()` (the main window shown,
  un-minimised and raised, on this page), the paths added to the queue with the preset (for those
  files; without files the preset goes to the queue's bands);
  confirm → ConfirmBatcher (app/forge/core/batcher.py) → one ConfirmWindow per preset, whose
  "Open in MediaForge" comes back here as a plain request. A confirm request shows only its
  window: the main window stays as it is, hidden too (with Forge off it says why on the page).
- `await shutdown()` — at quit: no more requests, the page and every confirm window gone. Before
  `Services.aclose()`.

What it hands the auto-renamer once Forge has started (`ForgeRuntime.when_ready`, before the
renamer starts): `on_status` → the page's status line (the last status is kept for a page built
later); `on_needs` → `needs` and the host's own `on_needs(count)` (the "needs you" count for the
tray and the switch: the renamer's until the queue is there, then the queue's, which also has the
files added by hand); `notify` → the host's `notify(title, text)` (the tray's notification).
`on_closed()` is called whenever a confirm window closes and `windows_open` says whether any still
shows or a plain request is on its way (D5: a right-click start leaves AirGrabber when the last one
closes, if its main window never showed). `on_view(history)`: History or the queue shows (🕘).
`on_message(text)`: what the queue says, for the main window's status bar.

The old MediaForge (F-26, D4 / D13): at the first visit to the page in a run with Forge ready, while
the old MediaForge's data exists and nothing was imported yet (`ForgeSettings.imported_from`), the
import step shows over the main window (app/forge/ui/import_step.py; "Not now" = asked again at
AirGrabber's next start; a page is visited too often to ask at each). After an import — from that step or Settings' "Bring over…" — the queue's presets and what
shows are read again (`imported`: the host hears every change of `imported_from` through Forge's
settings). `on_remove_old` (F-27) is the result's "Remove the old MediaForge"; `find_old_install` is
injectable (app/forge/migration.py's by default).

Light at import (AirGrabber's main window imports it at start): the page, the confirm window, the
import step and Forge's services are imported only when first needed (INTEGRATION.md 3.6).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from app import bootstrap
from app.forge.core.batcher import ConfirmBatcher
from app.forge.runtime import ForgeState
from app.i18n import N_, t
from app.single_instance import ForgeRequest
from app.ui import tasks

if TYPE_CHECKING:
    from app.forge.core.auto import WatchStatus
    from app.forge.migration import ImportResult, OldInstall
    from app.forge.models.settings import ForgeSettings
    from app.forge.services import ForgeServices
    from app.forge.ui.confirm_window import ConfirmWindow
    from app.forge.ui.import_step import ImportStep
    from app.forge.ui.page import ForgePage
    from app.ui import theme

log = logging.getLogger(__name__)

GUESSIT = "guessit"  # the optional package Forge needs, as pip names it
CONFIRM_RETRY_MS = 250  # how often quitting asks again while a confirm window renames
GETTING_READY_DRAW_S = 0.06  # Tk's turns (every 10 ms) to draw "Getting ready…" before the warm-up
# The page's plain frame while guessit isn't installed (with Try again).
NOT_READY_TEXT = N_("MediaForge isn't ready yet: one of its parts couldn't be installed.")


class Runtime(Protocol):
    """What the host needs of `ForgeRuntime` (app/forge/runtime.py)."""

    @property
    def state(self) -> ForgeState: ...

    @property
    def reason(self) -> str: ...

    @property
    def services(self) -> ForgeServices | None: ...

    def when_ready(self, callback: Callable[[ForgeServices], None]) -> None: ...

    async def start(self) -> ForgeServices | None: ...


def _retry_guessit(name: str) -> object:
    return bootstrap.retry_optional(name)


def _find_old_install() -> OldInstall | None:
    from app.forge.migration import find_old_install

    return find_old_install()


class ForgeHost:
    def __init__(
        self,
        root: Any,
        runtime: Runtime,
        *,
        page_master: Any,
        tabs_row: Any,
        show_page: Callable[[], None] | None = None,
        show_settings: Callable[[str | None], object] | None = None,
        restart: Callable[[], None] | None = None,
        retry_install: Callable[[str], object] | None = None,
        look: theme.Look | None = None,
        find_old_install: Callable[[], OldInstall | None] | None = None,
    ) -> None:
        """`root`: AirGrabber's main window (the confirm windows' and the import step's master).
        `page_master`, `tabs_row`: where the page and the tabs row's left part go. `show_page()`:
        the main window shown and raised on the page (a plain request; without it just `show`).
        `show_settings(section)`: Settings at `section`. `restart`: AirGrabber's restart (Try
        again). `retry_install`: forget a package's remembered install failure.
        `find_old_install`: the old MediaForge (blocking; run in a thread)."""
        self._root = root
        self._runtime = runtime
        self._page_master = page_master
        self._tabs_row = tabs_row
        self._show_page = show_page or self.show
        self._show_settings = show_settings or (lambda section: None)
        self._restart = restart or (lambda: None)
        self._retry_install = retry_install or _retry_guessit
        self._look = look
        self._find_old_install = find_old_install or _find_old_install
        self._page: ForgePage | None = None
        self._showing = False  # the main window shows the page
        self._offer_due = True  # the import step may still ask in this run
        self.import_step: ImportStep | None = None  # the old MediaForge's import, while it shows
        self._offering = False  # looking for the old MediaForge
        self._imported_from = ""  # the last `ForgeSettings.imported_from` heard
        self.on_remove_old: Callable[[], None] | None = None  # F-27's remover
        self.confirm_windows: list[ConfirmWindow] = []
        self._batcher = ConfirmBatcher(self._confirm_batch)
        self._status: WatchStatus | None = None
        self._renamer_count = 0  # the renamer's "needs you"
        self._queue_count: int | None = None  # the queue's, once it is there
        self._told = 0  # the count last told
        self._opening = 0  # plain requests on their way to the page
        self._shut = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self.on_needs: Callable[[int], None] = lambda count: None
        self.notify: Callable[[str, str], None] = lambda title, text: None
        self.on_closed: Callable[[], None] = lambda: None
        self.on_view: Callable[[bool], None] = lambda history: None
        self.on_message: Callable[[str], None] = lambda text: None
        runtime.when_ready(self._wire_renamer)

    # --- what it shows --------------------------------------------------------------------------

    @property
    def page(self) -> ForgePage | None:
        """The page, on screen or not; None until the first visit."""
        page = self._page
        try:
            if page is not None and page.winfo_exists():
                return page
        except Exception:  # its interpreter is gone
            pass
        return None

    @property
    def showing(self) -> bool:
        """Whether the main window shows the page."""
        return self._showing

    @property
    def windows_open(self) -> bool:
        """Whether a confirm window shows, or a plain request is on its way to the page."""
        return bool(self._opening or self.confirm_windows)

    @property
    def needs(self) -> int:
        """How many files need the owner, as the switch and the tray say it."""
        return self._told

    @property
    def status(self) -> WatchStatus | None:
        """The renamer's last status (None before Forge has started)."""
        return self._status

    def show(self) -> None:
        """The main window shows the page: its tabs-row part at once, the rest (a first visit's
        building, Forge's start) in a coroutine."""
        if self._shut:
            return
        self._mark_showing()
        page = self.page
        if page is not None:
            page.set_shown(True)
        self._spawn(self._build())

    async def open(self) -> ForgePage | None:
        """`show`, awaited: start Forge if it hasn't and build the page. None when AirGrabber is
        shutting down."""
        if self._shut:
            return None
        self._mark_showing()
        return await self._build()

    def _mark_showing(self) -> None:
        self._showing = True

    async def _build(self) -> ForgePage | None:
        if self._shut:
            return None
        runtime = self._runtime
        page = self.page or self._make_page()
        page.set_shown(self._showing)
        services = runtime.services
        if services is None and runtime.state is not ForgeState.NOT_READY:
            page.show_getting_ready()
            await asyncio.sleep(GETTING_READY_DRAW_S)
            services = await runtime.start()
        if self._shut or runtime.state is ForgeState.CLOSED or not page.winfo_exists():
            return None
        if services is not None and page.services is None and not services.parser_warm:
            await self._warm_up(page, services)
            if self._shut or not page.winfo_exists():
                return None
        if services is not None:
            if page.services is None:  # the import and the building, a turn of Tk's between them
                page.load_views()
                await asyncio.sleep(GETTING_READY_DRAW_S / 4)
                if self._shut or not page.winfo_exists():
                    return None
            page.show_forge(services, self._status)
            if self._offer_due and self._showing:
                self._offer_due = False
                self._offer_import(services)
        elif runtime.state is ForgeState.NOT_READY:
            page.show_problem(t(NOT_READY_TEXT), self.try_again)
        else:
            page.show_problem(runtime.reason, self.try_again)
        return page

    async def _warm_up(self, page: ForgePage, services: ForgeServices) -> None:
        """Forge's file-name reader isn't warm yet (the page opened before its idle warm-up,
        F-28): "Getting ready…" shows and is drawn, then the warm-up holds the loop once."""
        page.show_getting_ready()
        await asyncio.sleep(GETTING_READY_DRAW_S)
        if not self._shut and page.winfo_exists():
            services.warm_up()

    def _make_page(self) -> ForgePage:
        from app.forge.ui.page import ForgePage, make_shortcut_once

        page = ForgePage(self._page_master, tabs_row=self._tabs_row, look=self._look)
        page.pack(fill="both", expand=True)
        page.on_settings = self.open_settings
        page.on_needs = self._queue_needs
        page.on_view = lambda history: self.on_view(history)
        page.on_message = lambda text: self.on_message(text)
        self._page = page
        make_shortcut_once()
        return page

    def close(self) -> None:
        """Another page shows. AirGrabber and a Forge run keep going."""
        self._showing = False
        page = self.page
        if page is not None:
            page.set_shown(False)

    def show_queue(self) -> None:
        """MediaForge in the switch, a request: the queue (it is what a first visit shows)."""
        page = self.page
        if page is not None:
            page.show_view("queue")

    def toggle_history(self) -> None:
        """🕘: History, or back to the queue. Nothing before the queue is there."""
        page = self.page
        if page is not None:
            page.toggle_history()

    def refresh_page(self) -> None:
        """F5 on the page: what shows is read again."""
        page = self.page
        if page is not None:
            page.refresh_page()

    def may_close(self, retry: Callable[[], None]) -> bool:
        """True if AirGrabber may quit or restart now; otherwise `retry` is called later (a Forge
        run stops after its current file, an undo finishes, a confirm window's rename or undo
        finishes)."""
        if any(confirm.busy for confirm in self.confirm_windows):
            log.info("Waiting for a confirm window's rename to finish")
            self._root.after(CONFIRM_RETRY_MS, retry)
            return False
        page = self.page
        return page is None or page.may_close(retry)

    def try_again(self) -> None:
        """The plain frame's Try again: install guessit again (a restart), or start Forge again."""
        if self._runtime.state is ForgeState.NOT_READY:
            log.info("Trying to install MediaForge's missing part again: restarting")
            self._retry_install(GUESSIT)
            self._restart()
        else:
            self._spawn(self._build())

    # --- Settings --------------------------------------------------------------------------------

    def open_settings(self, section: str | None = None) -> None:
        """Settings at `section` (see `show_settings`)."""
        if not self._shut:
            self._show_settings(section)

    # --- the old MediaForge ---------------------------------------------------------------------

    def _offer_import(self, services: ForgeServices) -> None:
        """The import step over the main window, once per run, while there is something to
        bring over (D13)."""
        step = self.import_step
        if self._offering or (step is not None and step.winfo_exists()):
            return
        if services.settings.current.imported_from:
            return
        self._offering = True
        self._spawn(self._ask_import(services))

    async def _ask_import(self, services: ForgeServices) -> None:
        try:
            old = await asyncio.to_thread(self._find_old_install)
        except Exception as exc:
            log.warning("Couldn't look for the old MediaForge (%s)", type(exc).__name__)
            old = None
        finally:
            self._offering = False
        if old is None or not old.has_data or self._shut:
            return
        if services.settings.current.imported_from:
            return
        if not self._showing:
            self._offer_due = True  # another page meanwhile: asked at the next visit
            return
        from app.forge.ui.import_step import ImportStep

        self.import_step = ImportStep(
            self._root,
            services,
            old,
            look=self._look,
            on_remove_old=self.on_remove_old,
            on_done=self._import_done,
        )

    def _import_done(self, result: ImportResult | None) -> None:
        self.import_step = None  # the page is refreshed by `imported` (any import)

    def _settings_saved(self, settings: ForgeSettings) -> None:
        """Forge's settings changed: a new `imported_from` is an import, wherever it started."""
        if settings.imported_from and settings.imported_from != self._imported_from:
            self._imported_from = settings.imported_from
            self.imported()
        else:
            self._imported_from = settings.imported_from

    def imported(self) -> None:
        """The old MediaForge's data came over (from the step or from Settings): the queue reads
        its presets and what shows is read again."""
        page = self.page
        if page is None:
            return
        try:
            if page.queue is not None:
                page.queue.reload_presets()
            page.refresh_page()
        except Exception:
            log.exception("The page wasn't refreshed after the import")

    # --- requests -------------------------------------------------------------------------------

    def handle(self, request: ForgeRequest) -> None:
        """A request from a right-click, the "MediaForge" shortcut, the tray or a drop (see the
        module's notes)."""
        if self._shut:
            return
        paths = [Path(path) for path in request.paths]
        if request.confirm and paths:
            self._batcher.add(paths, request.preset)
            return
        self._opening += 1
        try:
            self._show_page()
        except Exception:
            log.exception("Showing MediaForge's page failed")
        self._spawn(self._open_with(paths, request.preset))

    async def _open_with(self, paths: Sequence[Path], preset: str | None) -> None:
        try:
            page = await self.open()
        finally:
            self._opening -= 1
        if page is None:
            return
        if paths:
            page.add_paths(paths, preset=preset)  # the preset names the files it adds
        elif preset:
            page.select_preset(preset)

    def _confirm_batch(self, preset: str | None, paths: list[Path]) -> None:
        """The batcher's: one confirm window for these files (on the loop, no Tk event)."""
        if not self._shut:
            self._spawn(self._open_confirm(preset, paths))

    async def _open_confirm(self, preset: str | None, paths: list[Path]) -> None:
        runtime = self._runtime
        services = runtime.services
        if services is None and runtime.state is not ForgeState.NOT_READY:
            services = await runtime.start()
        if self._shut:
            return
        if services is None:  # Forge is off: the page says why
            self._show_page()
            await self.open()
            return
        from app.forge.ui.confirm_window import ConfirmWindow

        window = ConfirmWindow(self._root, services, paths, preset, look=self._look)
        window.on_closed = self._confirm_closed
        window.on_open_in_forge = self._open_in_forge
        self.confirm_windows.append(window)

    def _confirm_closed(self, window: ConfirmWindow) -> None:
        if window in self.confirm_windows:
            self.confirm_windows.remove(window)
        self._closed_one()

    def _open_in_forge(self, paths: list[Path], preset: str | None) -> None:
        self.handle(ForgeRequest(paths=tuple(str(path) for path in paths), preset=preset))

    def _closed_one(self) -> None:
        try:
            self.on_closed()
        except Exception:
            log.exception("The confirm window's close callback failed")

    # --- the renamer ----------------------------------------------------------------------------

    def _wire_renamer(self, services: ForgeServices) -> None:
        auto = services.auto
        auto.on_status = self._renamer_status
        auto.on_needs = self._renamer_needs
        auto.notify = self._renamer_notify
        self._imported_from = services.settings.current.imported_from
        services.settings.subscribe(self._settings_saved)

    def _renamer_status(self, status: WatchStatus) -> None:
        self._status = status
        page = self.page
        if page is not None:
            page.set_watch_status(status)

    def _renamer_needs(self, count: int) -> None:
        self._renamer_count = count
        self._tell_needs()

    def _queue_needs(self, count: int) -> None:
        self._queue_count = count
        self._tell_needs()

    def _tell_needs(self) -> None:
        """The queue's count once it is there (it has the renamer's files too, and it follows a
        choice at once); before the first visit the renamer's."""
        count = self._renamer_count if self._queue_count is None else self._queue_count
        self._told = count
        try:
            self.on_needs(count)
        except Exception:
            log.exception("The 'needs you' count callback failed")

    def _renamer_notify(self, title: str, text: str) -> None:
        try:
            self.notify(title, text)
        except Exception:
            log.exception("The renamer's notification callback failed")

    # --- shutting down --------------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = tasks.spawn(coro, name="ForgeHost")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        """AirGrabber quits (after `may_close`): take no more requests, and close the page and
        every confirm window. Never raises."""
        self._shut = True
        self._showing = False
        self._batcher.close()
        page = self.page
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        for confirm in list(self.confirm_windows):
            try:
                confirm.destroy()
            except Exception:
                log.debug("A confirm window was gone already", exc_info=True)
        self.confirm_windows.clear()
        if page is not None:
            try:
                page.destroy()
            except Exception:
                log.debug("The page was gone already", exc_info=True)
        self._page = None
