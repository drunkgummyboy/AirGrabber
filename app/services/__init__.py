"""Service layer: the only objects the UI is allowed to talk to."""

from __future__ import annotations

import contextlib
import functools

import httpx

from app import APP_NAME, __version__
from app.config import AppPaths
from app.database import BackupRepository, Database
from app.forge.runtime import ForgeRuntime
from app.indexers import default_indexers
from app.models import Movie, Settings, Show
from app.services.airdates import AirdatesClient, AirdatesError, AirdatesImportService
from app.services.auto_download import AutoDownloadService
from app.services.backup import BackupService
from app.services.discovery import DiscoveryService
from app.services.download_queue import DownloadQueue
from app.services.download_status import DownloadStatusService, forge_moves
from app.services.downloads import DownloadService
from app.services.images import ImageCache
from app.services.library import LibraryService
from app.services.phone.coming_up import ComingUp, GrabbedFilms
from app.services.phone.downloads import PhoneDownloads
from app.services.phone.forge import PhoneForge
from app.services.phone.service import PhoneService
from app.services.plex import PlexClient, PlexService
from app.services.plex_library import PlexLibraryService
from app.services.plex_scans import PlexScansAfterMoves
from app.services.sonarr import SonarrClient
from app.services.profiles import ProfileService
from app.services.prowlarr_setup import ProwlarrSetupService
from app.services.qbittorrent_local import QBittorrentStarter
from app.services.qbittorrent_setup import QBittorrentSetup
from app.services.qbittorrent_theme import ThemeFolder
from app.services.quick_download import QuickDownloadService
from app.services.remote import RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.search import TorrentSearchService
from app.services.settings import SettingsService
from app.services.spotlight import SpotlightService
from app.services.synology import DownloadStationClient
from app.services.tmdb import TMDBClient
from app.services.torrent_app import (
    QBittorrentLinker,
    TorrentAppService,
    qbittorrent_executable,
)
from app.services.tvmaze import TVMazeClient
from app.services.updates import UpdateError, UpdateService
from app.services.watchlist import WatchlistService

USER_AGENT = f"{APP_NAME}/{__version__} (+https://github.com/drunkgummyboy/AirGrabber)"


class Services:
    """Composition root, built once at startup inside the event loop and closed on exit.

    Views receive the individual services they need; none of them sees the database.
    """

    __slots__ = (
        "plex_scans",
        "plex_library",
        "plex_posters",
        "sonarr",
        "_db",
        "_http",
        "airdates",
        "auto_download",
        "backup",
        "discovery",
        "download_queue",
        "download_status",
        "downloads",
        "forge",
        "images",
        "library",
        "phone",
        "plex",
        "profiles",
        "prowlarr_setup",
        "quick_download",
        "remote",
        "search",
        "settings",
        "spotlight",
        "torrent_app",
        "updates",
        "watchlist",
    )

    def __init__(
        self,
        db: Database,
        http: httpx.AsyncClient,
        settings: SettingsService,
        library: LibraryService,
        images: ImageCache,
        discovery: DiscoveryService,
        downloads: DownloadService,
        remote: RemoteDownloads,
        search: TorrentSearchService,
        updates: UpdateService,
        prowlarr_setup: ProwlarrSetupService,
        torrent_app: TorrentAppService,
        *,
        forge: ForgeRuntime,
        paths: AppPaths,
    ) -> None:
        self._db = db
        self._http = http
        self.settings = settings
        self.library = library
        self.images = images
        self.discovery = discovery
        # The header's spotlight band: tonight's episodes, trending shows, new films.
        self.spotlight = SpotlightService(library, discovery)
        self.downloads = downloads
        # The torrent program on a NAS or another computer; also Settings' "Test connection".
        self.remote = remote
        self.search = search
        # One-click Download: search, pick the best release, grab it.
        self.quick_download = QuickDownloadService(search, downloads, settings)
        # Every one-click download the user asks for, a couple at a time; as long as the app
        # runs (closing a window never cancels one).
        self.download_queue = DownloadQueue(self.quick_download)
        # The film watchlists (the selected person's in memory once `load`ed: profiles.load).
        self.watchlist = WatchlistService(db.watchlist, discovery, downloads)
        # The people sharing AirGrabber and who is selected (Services.create `load`s them):
        # each has their own followed series, watchlist and calendar.
        self.profiles = ProfileService(db.profiles, settings, self.watchlist)
        # Settings → Backup: people, shows, watchlists, download history and settings in one
        # file.
        self.backup = BackupService(BackupRepository(db), settings, self.profiles)
        # Downloads new episodes of the shows whose switch is on, and the watchlist films whose
        # "Download when it's out" is on, on everyone's lists (`run_periodic` from the UI).
        self.auto_download = AutoDownloadService(
            db.library, self.quick_download, settings, watchlist=self.watchlist
        )
        # Previews the shows followed on airdates.tv (import goes through library.import_shows).
        self.airdates = AirdatesImportService(AirdatesClient(http), library)
        self.updates = updates
        self.prowlarr_setup = prowlarr_setup
        self.torrent_app = torrent_app  # which torrent program is set up; qBittorrent install
        # The Downloads page: recent grabs with the torrent program's progress, free space
        # (`run_periodic` from the UI); looks again soon after every grab.
        # Where Forge moved a finished download, once Forge runs (`forge_moves`).
        # In sync with the torrent program: its tasks added there are recognised by anyone's
        # followed shows and watchlist films (`known`).
        self.download_status = DownloadStatusService(
            db.history,
            settings,
            remote,
            show=db.library.get_show,
            forge=forge_moves(forge),
            known=functools.partial(_known_titles, db),
        )
        downloads.subscribe(self.download_status.after_grab)
        # MediaForge inside AirGrabber: off until something awaits `forge.start()` (the delayed
        # start after the main window shows, or MediaForge's page opened earlier).
        self.forge = forge
        # What is in the user's Plex libraries counts as had (Settings → Plex; `run_periodic`
        # from the UI). A check that changes it reloads the pages, as a library change does.
        self.plex = PlexService(
            PlexClient(http, client_id=lambda: settings.current.plex_client_id),
            db.plex,
            settings,
            on_change=library.notify_changed,
        )
        # The Library page: Plex's shows and films, their posters from the Plex server.
        self.plex_library = PlexLibraryService(self.plex, library)
        self.plex_posters = ImageCache(http, paths.posters / "plex", fetch=self.plex.poster)
        # Import Shows → From Sonarr (a one-off read; nothing of it is stored).
        self.sonarr = SonarrClient(http)
        # What MediaForge moves into a library folder, Plex scans soon after (once Forge runs).
        self.plex_scans = PlexScansAfterMoves(self.plex)
        forge.when_ready(lambda started: self.plex_scans.attach(started.moves))
        # The Companion app's way in, on the home network; off until turned on in Settings
        # (`run` from the UI). What it may ask besides pairing comes from `routes_factory`.
        grabbed_films = GrabbedFilms(self.download_status)
        phone_routes = {
            **ComingUp(
                library,
                self.watchlist,
                self.download_status,
                downloads,
                self.quick_download,
                self.download_queue,
                self.profiles,
                grabbed_films,
            ).routes(),
            **PhoneDownloads(
                library,
                self.watchlist,
                search,
                downloads,
                self.quick_download,
                settings,
                self.profiles,
                grabbed_films,
            ).routes(),
            **PhoneForge(forge, self.profiles).routes(),
        }
        self.phone = PhoneService(
            settings, paths, self.profiles, routes_factory=lambda: phone_routes
        )

    @classmethod
    async def create(cls, paths: AppPaths) -> Services:
        async with contextlib.AsyncExitStack() as cleanup:  # unwinds only if creation fails
            db = await Database.open(paths.database)
            cleanup.push_async_callback(db.close)
            http = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            cleanup.push_async_callback(http.aclose)
            settings = await SettingsService.load(db.settings)
            tvmaze = TVMazeClient(http)  # one client, so library and discovery share its limits
            library = LibraryService(db.library, db.history, tvmaze, settings=settings)
            # Read on every request, so a key changed in Settings applies at once.
            tmdb = TMDBClient(
                http, api_key=lambda: settings.current.tmdb_api_key.get_secret_value()
            )
            discovery = DiscoveryService(tmdb, tvmaze, library)
            _clear_discovery_on_key_change(settings, discovery)
            images = ImageCache(http, paths.posters)
            # Reads the program, its address and account from the settings on every call; starts
            # the qBittorrent on this PC that AirGrabber set up when it isn't running.
            remote = RemoteDownloads(
                settings, starter=QBittorrentStarter(find_exe=qbittorrent_executable)
            )
            downloads = DownloadService(http, db.history, settings, remote, paths.torrents)
            # The Prowlarr address and key are read from the settings on every call.
            search = TorrentSearchService(
                default_indexers(
                    http,
                    prowlarr_url=lambda: settings.current.prowlarr_url,
                    prowlarr_api_key=lambda: settings.current.prowlarr_api_key.get_secret_value(),
                ),
                settings,
            )
            services = cls(
                db,
                http,
                settings,
                library,
                images,
                discovery,
                downloads,
                remote,
                search,
                UpdateService(http, databases=paths.databases),
                # Connects a Prowlarr on this PC and adds public sites to it (once).
                ProwlarrSetupService(http, settings),
                # qBittorrent's setup, with AirGrabber's copy of its qBittorrent theme; it may
                # point AirGrabber at qBittorrent's Web UI (the settings), and links an
                # installed qBittorrent for torrents when nothing else is set (the linker).
                TorrentAppService(
                    qbittorrent=QBittorrentSetup(theme=ThemeFolder.in_data_dir(paths.root)),
                    settings=settings,
                    linker=QBittorrentLinker(),
                ),
                # Forge shares the HTTP client, the TMDB client (its own names language) and
                # the poster cache; nothing of it is imported or opened until it starts.
                forge=ForgeRuntime(paths, http, tmdb, images, settings, fetch_tools=True),
                paths=paths,
            )
            await services.profiles.load()  # also the selected person's watchlist
            await services.downloads.learn_groups()  # the groups whose grabs were kept
            await services.torrent_app.refresh_qbittorrent_theme()  # after an update: new files
            cleanup.pop_all()
        return services

    @property
    def station(self) -> RemoteDownloads:
        """The older name of `remote`; main_window.py still passes it to Settings."""
        return self.remote

    async def aclose(self) -> None:
        # The queue is kept in memory only: what still waits is dropped (the log says how many).
        self.download_queue.close()
        try:
            await self.phone.aclose()  # no phone is answered from here on
        finally:
            try:
                # Before these: Forge uses the HTTP client, and forge.db's aiosqlite thread is no
                # daemon.
                await self.plex_scans.aclose()
                await self.forge.aclose()
            finally:
                try:
                    await self._http.aclose()
                finally:
                    await self._db.close()


async def _known_titles(db: Database) -> tuple[list[Show], list[Movie]]:
    """The shows anyone follows and the films on anyone's watchlist (the Downloads page
    recognises the tasks added in the torrent program by them)."""
    followed = await db.library.followed()
    films = await db.watchlist.films()
    return [entry.show for entry in followed], [entry.movie for entry in films]


def _clear_discovery_on_key_change(
    settings: SettingsService, discovery: DiscoveryService
) -> None:
    """Drop discovery's cached TMDB data whenever the TMDB key changes, so a fixed key doesn't
    keep showing an old failure or old data."""
    last_key = settings.current.tmdb_api_key

    def on_change(updated: Settings) -> None:
        nonlocal last_key
        if updated.tmdb_api_key != last_key:  # SecretStr compares the secret values
            last_key = updated.tmdb_api_key
            discovery.clear_cache()

    settings.subscribe(on_change)


__all__ = [
    "AirdatesClient",
    "AirdatesError",
    "AirdatesImportService",
    "AutoDownloadService",
    "BackupService",
    "DiscoveryService",
    "DownloadService",
    "DownloadStatusService",
    "DownloadStationClient",
    "ImageCache",
    "LibraryService",
    "PhoneService",
    "PlexService",
    "ProfileService",
    "ProwlarrSetupService",
    "QuickDownloadService",
    "RemoteDownloads",
    "RemoteError",
    "Services",
    "SettingsService",
    "SpotlightService",
    "TorrentAppService",
    "TorrentSearchService",
    "UpdateError",
    "UpdateService",
    "WatchlistService",
]
