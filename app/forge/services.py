"""Forge's composition root: every Forge service, built on AirGrabber's shared clients.

`ForgeServices.create` opens `forge.db` (`AppPaths.forge`), settles the journal rows a crash left
unfinished, seeds the default presets and builds the settings service, TMDB adapter, resolver,
queue, executor, undo, trailers, the move records and the automatic renamer. It does not start
the renamer: `ForgeRuntime` (app/forge/runtime.py) does that once whoever shows Forge's state is
listening.

The window gets services, never repositories: `history`, `presets`, `matches` and `ignored` (what
Auto-rename leaves alone, read at the start) are thin wrappers over `app.forge.db`, so
`app/forge/ui` never imports it.

Settings are saved with `settings.update(...)`; when a save changes one of `RENAMER_FIELDS`
the renamer is told at once (`AutoRenamer.reconfigure`), so Settings' Save needs nothing more.

The warm-up (INTEGRATION.md 3.6): guessit builds its rules on its first parse (with its import
~0.5-0.8 s of loop time, once per run). `warm_up()` does that with one made-up name: when the
renamer starts looking (`start_watching`, Forge's own start, a quiet moment 15 s after AirGrabber's
window), else once the loop has been quiet for a while (`wait_until_quiet`), or at once when the
page opens first (it shows "Getting ready…" meanwhile, app/forge/ui/host.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import AppPaths
from app.forge.clients.opensubtitles import APP_API_KEY, OpenSubtitlesClient, effective_api_key
from app.forge.clients.posters import CachedPosters
from app.forge.clients.tmdb import ForgeTmdb
from app.forge.clients.trailers import ToolInstallError, TrailerService
from app.forge.core.activity import ActivityFeed
from app.forge.core.airgrabber_folders import AirGrabberFolderSync, watched_folders
from app.forge.core.auto import AutoRenamer, RenamerParts
from app.forge.core.file_check import FileCheck
from app.forge.core.duplicates import Duplicate, find_duplicates, quarantine_plan
from app.forge.core.executor import Executor
from app.forge.core.history_service import HistoryService
from app.forge.core.matcher import Resolver
from app.forge.core.ignored import IgnoreService
from app.forge.core.matches_service import MatchService
from app.forge.core.moves import ForgeMoveService
from app.forge.core.parser import parse_filename
from app.forge.core.presets_service import PresetService
from app.forge.core.queue import Queue, QueueEvent
from app.forge.core.recovery import recover
from app.forge.core.settings import ForgeSettingsService
from app.forge.core.library import carry_move_option
from app.forge.core.subtitles import ForgeSubtitles, carry_subtitles
from app.forge.core.undo import UndoService
from app.forge.db.cache import CacheRepo
from app.forge.db.database import Database, db_path
from app.forge.db.history import HistoryRepo
from app.forge.models.history import RunSource
from app.forge.db.ignored import IgnoredRepo
from app.forge.db.matches import MatchRepo
from app.forge.db.moves import MoveRepository
from app.forge.db.presets import PresetRepo
from app.forge.db.settings import SettingsRepo
from app.forge.models.settings import ForgeSettings
from app.i18n import t, tn

if TYPE_CHECKING:
    import httpx

    from app.services.images import ImageCache
    from app.services.settings import SettingsService
    from app.services.tmdb import TMDBClient

log = logging.getLogger(__name__)

TOOLS_DIR = "tools"  # yt-dlp and ffmpeg, under AppPaths.forge (never under app/)

# The warm-up's file name (made up; any release-style name builds guessit's rules).
WARM_UP_NAME = "Alex.Example.Show.S01E02.1080p.WEB.x264-GRP.mkv"
# A quiet moment: the loop answered every QUIET_TICK_S sleep within QUIET_LAG_S, QUIET_TICKS
# times in a row (no page being built, no pictures being made); after QUIET_MAX_S it goes anyway.
QUIET_TICK_S = 0.1
QUIET_LAG_S = 0.02
QUIET_TICKS = 5
QUIET_MAX_S = 60.0

_parser_warm = False  # guessit's rules are built once per process: one warm-up per run


def _warm_parser() -> None:
    """Import guessit and parse one name (its rules are built on the first parse)."""
    parse_filename(WARM_UP_NAME)


async def wait_until_quiet(
    *,
    tick_s: float = QUIET_TICK_S,
    lag_s: float = QUIET_LAG_S,
    ticks: int = QUIET_TICKS,
    max_s: float = QUIET_MAX_S,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until the event loop (and with it Tk, which async-tkinter-loop pumps on it) has run
    `ticks` sleeps of `tick_s` in a row, each late by at most `lag_s`. True when it was quiet,
    False when `max_s` passed first."""
    deadline = clock() + max_s
    quiet = 0
    while quiet < ticks:
        if clock() >= deadline:
            return False
        before = clock()
        await asyncio.sleep(tick_s)
        quiet = quiet + 1 if clock() - before - tick_s <= lag_s else 0
    return True

# The settings the renamer works from: a save that changes one of them reconfigures it.
RENAMER_FIELDS: tuple[str, ...] = (
    "automatic",
    "watch_folders",
    "series_folder",
    "movies_folder",
    "series_preset",
    "movies_preset",
    "clean_downloads",
)

QueueListener = Callable[[QueueEvent], None]


class QueueEvents:
    """Fans the queue's events out to whichever views are listening (MediaForge's EventHub)."""

    def __init__(self) -> None:
        self._listeners: list[QueueListener] = []

    def emit(self, event: QueueEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("Queue listener %r failed", listener)

    def subscribe(self, listener: QueueListener) -> Callable[[], None]:
        """Call `listener` with every queue event. Returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def close(self) -> None:
        self._listeners.clear()


def _own_key(settings: SettingsService) -> str:
    return settings.current.opensubtitles_own_key.get_secret_value().strip()


def _renamer_view(settings: ForgeSettings) -> tuple[object, ...]:
    # The folders watched include AirGrabber's (B-01), which follow other fields than these.
    fields = tuple(getattr(settings, name) for name in RENAMER_FIELDS)
    return (*fields, tuple(watched_folders(settings)))


class ForgeServices:
    """Everything MediaForge's page, the tray and the right-click requests talk to."""

    def __init__(
        self,
        *,
        paths: AppPaths,
        db: Database,
        settings: ForgeSettingsService,
        tmdb: ForgeTmdb,
        images: ImageCache,
        http: httpx.AsyncClient,
        airgrabber_settings: SettingsService,
    ) -> None:
        self._db = db
        self.paths = paths
        self.settings = settings
        self.airgrabber_settings = airgrabber_settings  # AirGrabber's own (language, folders …)
        self.tmdb = tmdb
        self.images = images
        self.activity = ActivityFeed()  # the Activity strip's translated lines
        match_repo = MatchRepo(db)
        # The move records: a rename into a library folder is recorded in its journal step (the
        # executor asks `note` with the caller's media, HistoryRepo writes and reports), an undo
        # takes it out again.
        self.moves = ForgeMoveService(MoveRepository(db), settings=lambda: settings.current)
        history_repo = HistoryRepo(db, on_moves_change=self.moves.changed)
        self.history = HistoryService(history_repo)
        self.presets = PresetService(PresetRepo(db))
        self.matches = MatchService(match_repo)
        # What Auto-rename leaves alone (the queue's Ignore, Settings › Auto-rename).
        self.ignored = IgnoreService(IgnoredRepo(db), activity=self.activity)
        self.resolver = Resolver(tmdb, match_repo, activity=self.activity)
        self.queue_events = QueueEvents()
        self.queue = Queue(self.resolver, self.queue_events.emit)
        self.trailers = TrailerService(
            paths.forge / TOOLS_DIR,
            http,
            settings,
            activity=self.activity,
            season_trailer=tmdb.season_trailer_url,
        )
        # Subtitles from OpenSubtitles.com: the settings stay AirGrabber's (the same fields), the
        # user's own API key when Settings has one, else the shipped key (read on every request,
        # so a changed key applies at once).
        self.subtitles = ForgeSubtitles(
            OpenSubtitlesClient(
                http,
                api_key=lambda: effective_api_key(_own_key(airgrabber_settings), APP_API_KEY),
                own_key=lambda: bool(_own_key(airgrabber_settings)),
            ),
            airgrabber_settings,
            history_repo,
            own_key=lambda: bool(_own_key(airgrabber_settings)),
        )
        self.executor = Executor(
            history_repo,
            CachedPosters(images),
            self.trailers,
            activity=self.activity,
            moves=self.moves,
            subtitles=self.subtitles,
            # Each renamed video read with the trailer tools' ffprobe: audio the user doesn't
            # understand (English or a Settings › Subtitles language), a smaller picture.
            checker=FileCheck(
                paths.forge / TOOLS_DIR,
                lambda: airgrabber_settings.current.subtitle_languages,
            ),
        )
        self.undo = UndoService(history_repo, activity=self.activity)
        self.run_lock = asyncio.Lock()  # one rename run at a time: the owner's or the renamer's
        self.auto = AutoRenamer(
            RenamerParts(
                queue=self.queue,
                history=self.history,
                presets=self.presets,
                executor=self.executor,
                trailers=self.trailers,
                tmdb=tmdb,
                settings=settings,
                run_lock=self.run_lock,
                ignored=self.ignored,
            ),
            activity=self.activity,
        )
        # Where AirGrabber's downloads end up, kept in Forge's settings (B-01; started with the
        # renamer).
        self.airgrabber_folders = AirGrabberFolderSync(
            settings, airgrabber_settings, activity=self.activity
        )
        self._renamer_settings = _renamer_view(settings.current)
        self._unsubscribe = settings.subscribe(self._settings_saved)
        self._closed = False
        self._tools_task: asyncio.Task[None] | None = None  # fetching the trailer tools
        self._warm_task: asyncio.Task[None] | None = None  # the idle warm-up, while it waits

    @classmethod
    async def create(
        cls,
        paths: AppPaths,
        http: httpx.AsyncClient,
        tmdb_client: TMDBClient,
        images: ImageCache,
        airgrabber_settings: SettingsService,
    ) -> ForgeServices:
        """Open forge.db and build every service. The database is closed again if this fails."""
        async with contextlib.AsyncExitStack() as cleanup:  # unwinds only if creation fails
            db = await Database.open(db_path(paths))
            cleanup.push_async_callback(db.close)
            settled = await recover(HistoryRepo(db))
            if settled:
                log.warning("Settled %d change(s) left unfinished last time", settled)
            await PresetRepo(db).seed_defaults_if_empty()
            settings = await ForgeSettingsService.load(SettingsRepo(db))
            # The names language is Forge's own (file names stay English with a Dutch interface)
            # and is read on every lookup, so a changed setting applies at once.
            tmdb = ForgeTmdb(
                tmdb_client,
                cache=CacheRepo(db),
                language=lambda: settings.current.tmdb_language,
            )
            services = cls(
                paths=paths,
                db=db,
                settings=settings,
                tmdb=tmdb,
                images=images,
                http=http,
                airgrabber_settings=airgrabber_settings,
            )
            await carry_subtitles(settings, services.presets, airgrabber_settings)
            await carry_move_option(settings, services.presets)
            await services.ignored.load()
            await services.ignored.forget_gone(map(Path, watched_folders(settings.current)))
            if settled:
                services.activity.say(
                    tn(
                        "Checked {n} change left unfinished last time",
                        "Checked {n} changes left unfinished last time",
                        settled,
                    ),
                    level=logging.WARNING,
                )
            cleanup.pop_all()
        return services

    def _settings_saved(self, updated: ForgeSettings) -> None:
        view = _renamer_view(updated)
        if view != self._renamer_settings:
            self._renamer_settings = view
            self.auto.reconfigure()

    @property
    def parser_warm(self) -> bool:
        """Whether guessit has been warmed up this run (or tried: a failure shows at the first
        real parse)."""
        return _parser_warm

    def warm_up(self) -> None:
        """Warm guessit up now: import it and parse one name. Holds the loop ~0.2-0.8 s the first
        time in a run, then does nothing. Never raises."""
        global _parser_warm
        if _parser_warm:
            return
        started = time.perf_counter()
        try:
            _warm_parser()
        except Exception as exc:  # noqa: BLE001 - a missing or broken guessit shows elsewhere
            log.warning("Warming up the file-name reader failed (%s)", type(exc).__name__)
        else:
            log.info("File-name reader warmed up in %.0f ms", (time.perf_counter() - started) * 1000)
        _parser_warm = True

    async def _warm_up_when_quiet(self) -> None:
        try:
            await wait_until_quiet()
            if not self._closed:
                self.warm_up()
        finally:
            self._warm_task = None

    def start_watching(self) -> None:
        """Start the automatic renamer (it does nothing while renaming on its own is Off). A
        renamer that looks warms guessit up first (its first look parses right after);
        otherwise the warm-up waits for a quiet moment."""
        if not self._closed:
            if self.settings.current.automatic != "off" and not self.auto.blocked_by_old_install:
                self.warm_up()
            elif not self.parser_warm and self._warm_task is None:
                self._warm_task = asyncio.get_running_loop().create_task(
                    self._warm_up_when_quiet(), name="forge-warm-up"
                )
            self.auto.start()
            self.airgrabber_folders.start()

    def library_folders(self) -> list[Path]:
        """The Series and Movies folders Settings › Auto-rename names (those set)."""
        current = self.settings.current
        return [Path(folder) for folder in (current.series_folder, current.movies_folder) if folder]

    async def find_duplicates(self) -> list[Duplicate]:
        """The episodes and films found twice in the library folders (read in a thread)."""
        current = self.settings.current
        series = Path(current.series_folder) if current.series_folder else None
        movies = Path(current.movies_folder) if current.movies_folder else None
        found = await asyncio.to_thread(find_duplicates, series, movies)
        log.info("Found %d duplicate(s) in the library folders", len(found))
        return found

    async def move_duplicates(self, duplicates: Sequence[Duplicate]) -> int:
        """Move the extra copies to the Duplicates folder beside their library folder, as one
        run (History can undo it); how many moved."""
        plan = quarantine_plan(duplicates, self.library_folders())
        if not plan.files:
            return 0
        async with self.run_lock:  # never alongside another run
            result = await self.executor.run(
                plan,
                source=RunSource.APP,
                preset_name=None,
                on_progress=lambda _event: None,
                cancel=asyncio.Event(),
                check_files=False,
            )
        log.info("Moved %d duplicate copies aside", result.renamed)
        return result.renamed

    def fetch_trailer_tools(self) -> None:
        """Get yt-dlp and ffmpeg in the background when a preset wants trailers and they aren't
        there (the owner's decision: no question first, about 110 MB once). A failure is said in
        Activity; the next start, or the question on Forge, tries again."""
        if self._closed or self._tools_task is not None or self.trailers.tools_present():
            return
        self._tools_task = asyncio.get_running_loop().create_task(
            self._fetch_trailer_tools(), name="forge-trailer-tools"
        )

    async def _fetch_trailer_tools(self) -> None:
        try:
            if any(preset.options.trailer for preset in await self.presets.list()):
                await self.trailers.install_tools(lambda _step, _fraction: None)
        except ToolInstallError as exc:  # its text is translated and names no address
            log.warning("Couldn't install the trailer tools at the start")
            self.activity.say(
                t("Couldn't install the trailer tools: {reason}", reason=exc),
                level=logging.WARNING,
            )
        except Exception as exc:  # the type only: a message could hold a path or an address
            log.warning("Couldn't install the trailer tools at the start (%s)", type(exc).__name__)

    async def aclose(self) -> None:
        """Stop the renamer (a run stops after its current file, a pending restart never
        happens), drop the queue and close forge.db. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        if self._warm_task is not None:
            self._warm_task.cancel()
        if self._tools_task is not None:
            self._tools_task.cancel()
            await asyncio.gather(self._tools_task, return_exceptions=True)
        try:
            await self.auto.close()
        finally:
            await self.airgrabber_folders.close()  # before forge.db goes: it may be storing
            self.moves.close()
            self.queue_events.close()
            self.activity.close()
            self.queue.clear()  # cancels the lookups still running
            await asyncio.sleep(0)  # let them unwind before the database goes
            await self._db.close()
