"""AirGrabber's side of Forge (MediaForge inside AirGrabber; docs/forge/INTEGRATION.md 2, 3.11, D5).

- `RequestPump`: the running copy takes the Forge requests later starts hand over
  (app/single_instance.py's mailbox): the event every ~150 ms, the folder every few seconds (an
  elevated sender can't set the event), and once at once when it starts, right after the request
  this start carried itself (a first right-click start). A sender that couldn't delete its own
  request file starts AirGrabber anyway, so that first look drops one copy of the start's own
  request. It stops taking once AirGrabber begins to quit: what waits then stays in the folder.
- `QuitGate`: quitting and restarting wait for Forge (`ForgeHost.may_close(retry)`: a Forge run
  stops after its current file, an undo finishes), then close the Forge windows and Forge itself,
  bounded (a slow TMDB look by the renamer can't hold quitting up), then let the window go.
- `quits_after_forge`: D5. A start for Forge (the right-click menu) shows only the confirm window
  (or the Forge window); when the last Forge window closes while the main window is still hidden,
  AirGrabber stays in the tray if `keep_in_tray` is on and the tray icon works (as `close_action`
  decides for the main window), otherwise it quits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.single_instance import ForgeRequest
from app.ui.tasks import spawn
from app.ui.tray import CloseAction, close_action

log = logging.getLogger(__name__)

POLL_S = 0.15  # how often the hand-over event is checked
FOLDER_POLL_S = 3.0  # how often the folder is read without the event
FORGE_CLOSE_S = 5.0  # at most this long for Forge to close when AirGrabber quits


class Mailbox(Protocol):
    """What the pump needs of `InstanceLock` (app/single_instance.py)."""

    def forge_requested(self) -> bool: ...

    def take_requests(self) -> list[ForgeRequest]: ...


class RequestPump:
    """Hands every Forge request to `handle` (the Forge host's), on the loop. `run()` once the
    loop runs (spawned); `stop()` when AirGrabber begins to quit."""

    def __init__(
        self,
        mailbox: Mailbox | None,
        handle: Callable[[ForgeRequest], None],
        *,
        start_request: ForgeRequest | None = None,
        poll_s: float = POLL_S,
        folder_poll_s: float = FOLDER_POLL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mailbox = mailbox
        self._handle = handle
        self._start_request = start_request
        self._poll_s = poll_s
        self._folder_poll_s = folder_poll_s
        self._clock = clock
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        """Take no more requests (AirGrabber is quitting): they stay in the folder."""
        self._stopped = True

    async def run(self) -> None:
        if self._start_request is not None and not self._stopped:
            self._pass_on(self._start_request)
        mailbox = self._mailbox
        if mailbox is None:
            return
        self._take(mailbox, duplicate=self._start_request)  # whatever waited while starting
        last_folder = self._clock()
        while not self._stopped:
            await asyncio.sleep(self._poll_s)
            if self._stopped:
                break
            signalled = mailbox.forge_requested()
            now = self._clock()
            if signalled or now - last_folder >= self._folder_poll_s:
                last_folder = now
                self._take(mailbox)

    def _take(self, mailbox: Mailbox, *, duplicate: ForgeRequest | None = None) -> None:
        for request in mailbox.take_requests():
            if self._stopped:
                return  # quitting meanwhile (a handler may start it): the rest is dropped
            if duplicate is not None and request == duplicate:
                duplicate = None  # this start's own request, left in the folder: once
                log.info("Dropped a second copy of this start's own MediaForge request")
                continue
            self._pass_on(request)

    def _pass_on(self, request: ForgeRequest) -> None:
        try:
            self._handle(request)
        except Exception:
            log.exception("Handing a MediaForge request on failed")


class ClosingHost(Protocol):
    """What the gate needs of `ForgeHost` (app/forge/ui/host.py)."""

    def may_close(self, retry: Callable[[], None]) -> bool: ...

    async def shutdown(self) -> None: ...


class QuitGate:
    """Quit and restart through Forge: `request()` (again and again is fine) calls `on_asked()`
    once (the pump stops taking requests) and asks the host `may_close(request)`; once it may,
    `on_leaving()` (the window hides), then the host's `shutdown()` and `close_forge()`, each
    within `timeout_s`, then `finish()` (the main window destroyed: main.py's shutdown
    follows)."""

    def __init__(
        self,
        host: ClosingHost,
        *,
        close_forge: Callable[[], Awaitable[None]],
        finish: Callable[[], None],
        on_asked: Callable[[], None] = lambda: None,
        on_leaving: Callable[[], None] = lambda: None,
        timeout_s: float = FORGE_CLOSE_S,
    ) -> None:
        self._host = host
        self._close_forge = close_forge
        self._finish = finish
        self._on_asked = on_asked
        self._on_leaving = on_leaving
        self._timeout_s = timeout_s
        self._asked = False
        self._leaving = False

    @property
    def asked(self) -> bool:
        """Whether quitting (or restarting) was asked for, waiting for Forge or not."""
        return self._asked

    @property
    def leaving(self) -> bool:
        """Whether AirGrabber is closing now (Forge said it may)."""
        return self._leaving

    def request(self) -> None:
        if self._leaving:
            return
        if not self._asked:
            self._asked = True
            _call_logged(self._on_asked, "quit request")
        if not self._host.may_close(self.request):
            log.info("Quitting waits for MediaForge to finish its current file")
            return
        self._leaving = True
        _call_logged(self._on_leaving, "leaving")
        spawn(self._shut_down(), name="quit")

    async def _shut_down(self) -> None:
        try:
            # Each within its own bound: the windows first (they may still save their size).
            await _bounded(self._host.shutdown, self._timeout_s, "MediaForge's windows")
            await _bounded(self._close_forge, self._timeout_s, "MediaForge")
        finally:
            self._finish()


async def _bounded(step: Callable[[], Awaitable[None]], timeout_s: float, what: str) -> None:
    """Await `step()` for at most `timeout_s`. Never raises (but a cancellation)."""
    try:
        async with asyncio.timeout(timeout_s):
            await step()
    except TimeoutError:
        log.warning("%s didn't close within %.0f s: quitting anyway", what, timeout_s)
    except Exception:
        log.exception("Closing %s failed: quitting anyway", what)


def quits_after_forge(
    *,
    started_for_forge: bool,
    forge_windows_open: bool,
    main_hidden: bool,
    keep_in_tray: bool,
    tray_running: bool,
) -> bool:
    """D5: whether AirGrabber quits now that a Forge window closed. Only a start for Forge
    (the right-click menu, the "MediaForge" shortcut) whose main window still hasn't been shown
    and with no Forge window left; then as closing the main window would (no first-time tray
    notice: there is no main window it could explain)."""
    if not started_for_forge or forge_windows_open or not main_hidden:
        return False
    action = close_action(keep_in_tray=keep_in_tray, tray_running=tray_running, notice_shown=True)
    return action is CloseAction.QUIT


def _call_logged(callback: Callable[[], None], what: str) -> None:
    try:
        callback()
    except Exception:
        log.exception("The %s callback failed", what)
