"""`ForgeRuntime`: starts Forge inside AirGrabber when something first needs it, and stops it.

AirGrabber's `Services` holds one (`Services.forge`). Nothing here imports Forge's services, its
window or guessit at import time: `start()` imports them, so AirGrabber's own start doesn't pay
for Forge (INTEGRATION.md 3.6). `start()` is idempotent and may be awaited by several callers at
once (the delayed start after the main window shows, the Forge window opened early, a
right-click request): they all share one start.

Without guessit (an optional package; the first start after an update may be offline) Forge
stays off: `state` is NOT_READY with the reason, nothing is watched, AirGrabber runs as before.
The start asks through `app.forge.core.parser.prepare_parser`, which imports guessit and builds
its rules in small pieces between turns of the loop (F-33: at once it held the window 0.3-1 s).

While the old MediaForge is still installed (app/forge/migration.py's `blocked_by_old_install`,
asked once per start in a thread; injectable as `old_install_blocks`) the renamer is started
blocked (`AutoRenamer.blocked_by_old_install`): a stored Trial / On waits until it is removed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from app.i18n import N_, t

if TYPE_CHECKING:
    import httpx

    from app.config import AppPaths
    from app.forge.services import ForgeServices
    from app.services.images import ImageCache
    from app.services.settings import SettingsService
    from app.services.tmdb import TMDBClient

log = logging.getLogger(__name__)

# The reasons `ForgeRuntime.reason` gives (shown by the Forge window: t() them there or here).
NOT_READY_REASON = N_(
    "MediaForge isn't ready yet: a part it needs to read file names (guessit) isn't installed."
)
FAILED_REASON = N_("MediaForge couldn't start. The log has the details.")

ReadyCallback = Callable[["ForgeServices"], None]


class ForgeState(StrEnum):
    OFF = "off"  # not started yet
    STARTING = "starting"
    READY = "ready"
    NOT_READY = "not_ready"  # guessit isn't installed: Forge stays off this run
    FAILED = "failed"  # forge.db couldn't be opened (or another error): start() may try again
    CLOSED = "closed"  # AirGrabber is shutting down


def _blocked_by_old_install() -> bool:
    from app.forge.migration import blocked_by_old_install

    return blocked_by_old_install()


async def _parser_ready() -> bool:
    from app.forge.core.parser import prepare_parser  # guessit itself only inside its functions

    return await prepare_parser()


class ForgeRuntime:
    def __init__(
        self,
        paths: AppPaths,
        http: httpx.AsyncClient,
        tmdb_client: TMDBClient,
        images: ImageCache,
        airgrabber_settings: SettingsService,
        *,
        parser_ready: Callable[[], bool] | None = None,
        old_install_blocks: Callable[[], bool] | None = None,
    ) -> None:
        self._paths = paths
        self._http = http
        self._tmdb_client = tmdb_client
        self._images = images
        self._airgrabber_settings = airgrabber_settings
        self._parser_ready = parser_ready  # None: the parser's own check (tests fake it)
        # Whether the old MediaForge is still installed, so the renamer stays idle (F-26 / F-27;
        # blocking: asked in a thread before the renamer starts).
        self._old_install_blocks = old_install_blocks or _blocked_by_old_install
        self._state = ForgeState.OFF
        self._services: ForgeServices | None = None
        self._starting: asyncio.Future[ForgeServices | None] | None = None
        self._on_ready: list[ReadyCallback] = []
        self._closed = False

    @property
    def state(self) -> ForgeState:
        return self._state

    @property
    def reason(self) -> str:
        """Why Forge is off (translated); empty unless the state is NOT_READY or FAILED."""
        if self._state is ForgeState.NOT_READY:
            return t(NOT_READY_REASON)
        if self._state is ForgeState.FAILED:
            return t(FAILED_REASON)
        return ""

    @property
    def services(self) -> ForgeServices | None:
        """Forge's services; None until `start()` has finished (and when Forge is off)."""
        return self._services

    def when_ready(self, callback: ReadyCallback) -> None:
        """Call `callback` with the services once Forge has started, before the renamer starts
        (so its status and notifications can be wired first); at once when it has already."""
        if self._services is not None:
            callback(self._services)
        else:
            self._on_ready.append(callback)

    async def start(self) -> ForgeServices | None:
        """Start Forge once; returns its services, or None when it is off (see `state`).
        A caller that is cancelled while waiting doesn't cancel the start."""
        if self._closed or self._state in (ForgeState.READY, ForgeState.NOT_READY):
            return self._services
        if self._starting is None or self._starting.done():  # first start, or again after FAILED
            self._starting = asyncio.ensure_future(self._start())
        return await asyncio.shield(self._starting)

    async def _start(self) -> ForgeServices | None:
        if self._closed:
            return None
        self._state = ForgeState.STARTING
        ready = self._parser_ready() if self._parser_ready is not None else await _parser_ready()
        if not ready:
            log.warning("MediaForge stays off: guessit isn't installed")
            self._state = ForgeState.NOT_READY
            return None
        if self._closed:  # aclose() came while guessit was being prepared
            return None
        try:
            blocked = await asyncio.to_thread(self._old_install_blocks)
        except Exception as exc:
            log.warning("Couldn't look for the old MediaForge (%s)", type(exc).__name__)
            blocked = False
        try:
            from app.forge.services import ForgeServices

            services = await ForgeServices.create(
                self._paths, self._http, self._tmdb_client, self._images, self._airgrabber_settings
            )
        except Exception as exc:
            # The type only: a message or traceback could name paths with the user's name.
            log.error("MediaForge couldn't start (%s)", type(exc).__name__)
            self._state = ForgeState.FAILED
            return None
        if self._closed:  # aclose() came while it was starting
            await services.aclose()
            return None
        services.auto.blocked_by_old_install = blocked
        if blocked:
            log.info("The old MediaForge is still installed: renaming on its own waits")
        self._services = services
        self._state = ForgeState.READY
        log.info("MediaForge started")
        callbacks, self._on_ready = self._on_ready, []
        for callback in callbacks:
            try:
                callback(services)
            except Exception:
                log.exception("A MediaForge start callback failed")
        services.start_watching()
        return services

    async def aclose(self) -> None:
        """Stop Forge (a start in progress is finished first, then closed). Never raises:
        AirGrabber's own shutdown must go on."""
        self._closed = True
        if self._starting is not None and not self._starting.done():
            await asyncio.gather(self._starting, return_exceptions=True)
        self._state = ForgeState.CLOSED
        services, self._services = self._services, None
        self._on_ready.clear()
        if services is None:
            return
        try:
            await services.aclose()
        except Exception as exc:
            log.error("MediaForge didn't close cleanly (%s)", type(exc).__name__)
        log.info("MediaForge stopped")
