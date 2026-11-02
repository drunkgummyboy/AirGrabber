"""ForgeHost: the Forge window's host inside AirGrabber (docs/forge/INTEGRATION.md 2 and 3.11).

AirGrabber's main window owns one (F-21) and calls it:
- `await open()` — starts Forge if it hasn't (`ForgeRuntime.start()`), then shows and raises the
  Forge window (app/forge/ui/window.py; made once, with the size and place it had). While Forge
  isn't ready (guessit couldn't be installed, NOT_READY) the window shows one plain page with Try
  again: forget guessit's remembered install failure (`app.bootstrap.retry_optional`), then
  AirGrabber's restart. When forge.db couldn't be opened (FAILED), Try again starts Forge again.
  Opened before Forge's file-name reader has warmed up (F-28, app/forge/services.py), the window
  shows "Getting ready…" first, then `ForgeServices.warm_up()` runs and the pages are built.
- `close()` — hides the Forge window (its ✕ does the same) and remembers its size and place
  (ForgeSettings.window_geometry). Closing it never quits AirGrabber.
- `may_close(retry)` — before AirGrabber quits or restarts: False while the Inbox's Forge run
  stops after its current file (`QueueView.request_close`), History's undo finishes or a
  confirm window renames or undoes (`ConfirmWindow.busy`); `retry` is called then.
- `handle(request)` — a right-click / "MediaForge" shortcut request (app/single_instance.py's
  ForgeRequest): plain → the Forge window, the preset chosen, the paths added to the Inbox;
  confirm → ConfirmBatcher (app/forge/core/batcher.py) → one ConfirmWindow per preset, whose
  "Open in MediaForge" comes back here as a plain request.
- `await shutdown()` — at quit: no more requests, the size and place saved, every Forge window
  gone. Before `Services.aclose()`.

What it hands the auto-renamer once Forge has started (`ForgeRuntime.when_ready`, before the
renamer starts): `on_status` → the window's watching light and Library page (the last status is
kept for a window opened later); `on_needs` → `needs` and the host's own `on_needs(count)` (the
"needs you" count for the tray and AirGrabber's MediaForge button, F-21 / F-22 set it); `notify`
→ the host's `notify(title, text)` (the tray's notification). `on_closed()` is called whenever a
Forge window closes (the Forge window hidden, a confirm window gone) and `windows_open` says
whether any still shows (D5: a right-click start leaves AirGrabber when the last one closes).

The old MediaForge (F-26, D4 / D13): at an open that shows the Forge window (made, or shown
from hidden) with Forge ready, while the old MediaForge's data exists and nothing was imported
yet (`ForgeSettings.imported_from`), the window gets the import step
(app/forge/ui/import_step.py; "Not now" = asked again at the next open). After an import —
from that step or Settings' "Bring over…" — the Inbox's presets and the page showing are read
again (`imported`: the host hears every change of `imported_from` through Forge's settings). `on_remove_old` (F-27) is the result's
"Remove the old MediaForge"; `find_old_install` is injectable (app/forge/migration.py's by
default).

Light at import (AirGrabber's main window may import it at start): the window, the confirm window,
the import step and Forge's services are imported only when first needed (INTEGRATION.md 3.6).
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
    from app.forge.ui.window import ForgeWindow
    from app.ui import theme

log = logging.getLogger(__name__)

GUESSIT = "guessit"  # the optional package Forge needs, as pip names it
CONFIRM_RETRY_MS = 250  # how often quitting asks again while a confirm window renames
GETTING_READY_DRAW_S = 0.06  # Tk's turns (every 10 ms) to draw "Getting ready…" before the warm-up
# The Forge window's plain page while guessit isn't installed (with Try again).
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
        open_settings: Callable[[], None] | None = None,
        restart: Callable[[], None] | None = None,
        retry_install: Callable[[str], object] | None = None,
        look: theme.Look | None = None,
        find_old_install: Callable[[], OldInstall | None] | None = None,
    ) -> None:
        """`root`: AirGrabber's main window (the Forge windows' master). `open_settings`: Settings
        at MediaForge › Library & watching ("More in Settings…"). `restart`: AirGrabber's
        restart (Try again). `retry_install`: forget a package's remembered install failure.
        `find_old_install`: the old MediaForge (blocking; run in a thread)."""
        self._root = root
        self._runtime = runtime
        self._open_settings = open_settings or (lambda: None)
        self._restart = restart or (lambda: None)
        self._retry_install = retry_install or _retry_guessit
        self._look = look
        self._find_old_install = find_old_install or _find_old_install
        self._window: ForgeWindow | None = None
        self.import_step: ImportStep | None = None  # the old MediaForge's import, while it shows
        self._offering = False  # looking for the old MediaForge
        self._imported_from = ""  # the last `ForgeSettings.imported_from` heard
        self.on_remove_old: Callable[[], None] | None = None  # F-27's remover
        self.confirm_windows: list[ConfirmWindow] = []
        self._batcher = ConfirmBatcher(self._confirm_batch)
        self._status: WatchStatus | None = None
        self._needs = 0
        self._opening = 0  # plain requests on their way to the Forge window
        self._shut = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self.on_needs: Callable[[int], None] = lambda count: None
        self.notify: Callable[[str, str], None] = lambda title, text: None
        self.on_closed: Callable[[], None] = lambda: None
        runtime.when_ready(self._wire_renamer)

    # --- what it shows --------------------------------------------------------------------------

    @property
    def window(self) -> ForgeWindow | None:
        """The Forge window, shown or hidden; None until it is first opened."""
        window = self._window
        try:
            if window is not None and window.winfo_exists():
                return window
        except Exception:  # its interpreter is gone
            pass
        return None

    @property
    def windows_open(self) -> bool:
        """Whether a Forge window shows (or is about to): the Forge window, a confirm window."""
        if self._opening or self.confirm_windows:
            return True
        window = self.window
        return window is not None and window.state() != "withdrawn"

    @property
    def needs(self) -> int:
        """How many watched downloads need the owner (the renamer's last count)."""
        return self._needs

    @property
    def status(self) -> WatchStatus | None:
        """The renamer's last status (None before Forge has started)."""
        return self._status

    async def open(self) -> ForgeWindow | None:
        """Start Forge if it hasn't, then show and raise the Forge window. None when AirGrabber
        is shutting down."""
        if self._shut:
            return None
        runtime = self._runtime
        services = runtime.services
        if services is None and runtime.state is not ForgeState.NOT_READY:
            services = await runtime.start()
        if self._shut or runtime.state is ForgeState.CLOSED:
            return None
        window = self.window
        shows_now = window is None or window.state() == "withdrawn"
        if window is None:
            window = self._make_window(services)
        if services is not None and window.services is None and not services.parser_warm:
            await self._warm_up(window, services)
            if self._shut or not window.winfo_exists():
                return None
        if services is not None:
            window.show_forge(services, self._status)
        elif runtime.state is ForgeState.NOT_READY:
            window.show_problem(t(NOT_READY_TEXT), self.try_again)
        else:
            window.show_problem(runtime.reason, self.try_again)
        self._raise(window)
        if services is not None and shows_now:
            self._offer_import(window, services)
        return window

    async def _warm_up(self, window: ForgeWindow, services: ForgeServices) -> None:
        """Forge's file-name reader isn't warm yet (the window opened before its idle warm-up,
        F-28): "Getting ready…" shows and is drawn, then the warm-up holds the loop once."""
        window.show_getting_ready()
        self._raise(window)
        await asyncio.sleep(GETTING_READY_DRAW_S)
        if not self._shut and window.winfo_exists():
            services.warm_up()

    def _make_window(self, services: ForgeServices | None) -> ForgeWindow:
        from app.forge.ui.window import ForgeWindow

        geometry = services.settings.current.window_geometry if services is not None else None
        window = ForgeWindow(self._root, look=self._look, geometry=geometry)
        window.on_close = self.close
        window.on_more_settings = self._more_settings
        self._window = window
        return window

    def _raise(self, window: ForgeWindow) -> None:
        hidden = window.state() == "withdrawn"
        window.deiconify()
        window.lift()
        window.focus_force()
        if hidden:
            window.refresh_page()

    def close(self) -> None:
        """Hide the Forge window, remembering its size and place. AirGrabber keeps running."""
        window = self.window
        if window is None or window.state() == "withdrawn":
            return
        geometry = window.saved_geometry()
        window.withdraw()
        services = self._runtime.services
        if geometry and services is not None:
            self._spawn(self._save_geometry(services, geometry))
        self._closed_one()

    async def _save_geometry(self, services: ForgeServices, geometry: str) -> None:
        if services.settings.current.window_geometry != geometry:
            await services.settings.update(window_geometry=geometry)

    def may_close(self, retry: Callable[[], None]) -> bool:
        """True if AirGrabber may quit or restart now; otherwise `retry` is called later (a Forge
        run stops after its current file, an undo finishes, a confirm window's rename or undo
        finishes)."""
        if any(confirm.busy for confirm in self.confirm_windows):
            log.info("Waiting for a confirm window's rename to finish")
            self._root.after(CONFIRM_RETRY_MS, retry)
            return False
        window = self.window
        return window is None or window.may_close(retry)

    def try_again(self) -> None:
        """The plain page's Try again: install guessit again (a restart), or start Forge again."""
        if self._runtime.state is ForgeState.NOT_READY:
            log.info("Trying to install MediaForge's missing part again: restarting")
            self._retry_install(GUESSIT)
            self._restart()
        else:
            self._spawn(self.open())

    def _more_settings(self) -> None:
        self._open_settings()

    # --- the old MediaForge ---------------------------------------------------------------------

    def _offer_import(self, window: ForgeWindow, services: ForgeServices) -> None:
        """The import step over the window, once per open, while there is something to bring
        over (D13)."""
        step = self.import_step
        if self._offering or (step is not None and step.winfo_exists()):
            return
        if services.settings.current.imported_from:
            return
        self._offering = True
        self._spawn(self._ask_import(window, services))

    async def _ask_import(self, window: ForgeWindow, services: ForgeServices) -> None:
        try:
            old = await asyncio.to_thread(self._find_old_install)
        except Exception as exc:
            log.warning("Couldn't look for the old MediaForge (%s)", type(exc).__name__)
            old = None
        finally:
            self._offering = False
        if old is None or not old.has_data or self._shut:
            return
        if services.settings.current.imported_from or not window.winfo_exists():
            return
        if window.state() == "withdrawn":  # closed meanwhile: asked at the next open
            return
        from app.forge.ui.import_step import ImportStep

        self.import_step = ImportStep(
            window,
            services,
            old,
            look=self._look,
            on_remove_old=self.on_remove_old,
            on_done=lambda result: self._import_done(window, result),
        )

    def _import_done(self, window: ForgeWindow, result: ImportResult | None) -> None:
        self.import_step = None  # the window is refreshed by `imported` (any import)

    def _settings_saved(self, settings: ForgeSettings) -> None:
        """Forge's settings changed: a new `imported_from` is an import, wherever it started."""
        if settings.imported_from and settings.imported_from != self._imported_from:
            self._imported_from = settings.imported_from
            self.imported()
        else:
            self._imported_from = settings.imported_from

    def imported(self) -> None:
        """The old MediaForge's data came over (from the window or from Settings): the Forge
        window reads its presets and the page showing again."""
        window = self.window
        if window is None:
            return
        try:
            if window.queue is not None:
                window.queue.reload_presets()
            window.refresh_page()
        except Exception:
            log.exception("The Forge window wasn't refreshed after the import")

    # --- requests -------------------------------------------------------------------------------

    def handle(self, request: ForgeRequest) -> None:
        """A request from a right-click or the "MediaForge" shortcut (see the module's notes)."""
        if self._shut:
            return
        paths = [Path(path) for path in request.paths]
        if request.confirm and paths:
            self._batcher.add(paths, request.preset)
            return
        self._opening += 1
        self._spawn(self._open_with(paths, request.preset))

    async def _open_with(self, paths: Sequence[Path], preset: str | None) -> None:
        try:
            window = await self.open()
        finally:
            self._opening -= 1
        if window is None:
            return
        if preset:
            window.select_preset(preset)
        if paths:
            window.add_paths(paths)

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
        if services is None:  # Forge is off: the Forge window says why
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
            log.exception("The Forge window's close callback failed")

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
        window = self.window
        if window is not None:
            window.set_watch_status(status)

    def _renamer_needs(self, count: int) -> None:
        self._needs = count
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
        """AirGrabber quits (after `may_close`): take no more requests, save the window's size
        and place, and close every Forge window. Never raises."""
        self._shut = True
        self._batcher.close()
        window = self.window
        services = self._runtime.services
        if window is not None and services is not None:
            geometry = window.saved_geometry()
            if geometry:
                try:
                    await self._save_geometry(services, geometry)
                except Exception as exc:
                    log.warning("The Forge window's size wasn't saved (%s)", type(exc).__name__)
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
        if window is not None:
            try:
                window.destroy()
            except Exception:
                log.debug("The Forge window was gone already", exc_info=True)
        self._window = None
