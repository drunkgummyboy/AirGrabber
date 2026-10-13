"""Service layer: the only objects the UI is allowed to talk to."""

from __future__ import annotations

import contextlib

import httpx

from app import APP_NAME, __version__
from app.config import AppPaths
from app.database import Database
from app.indexers import default_indexers
from app.models import Settings
from app.services.discovery import DiscoveryService
from app.services.downloads import DownloadService
from app.services.images import ImageCache
from app.services.library import LibraryService
from app.services.prowlarr_setup import ProwlarrSetupService
from app.services.quick_download import QuickDownloadService
from app.services.search import TorrentSearchService
from app.services.settings import SettingsService
from app.services.synology import DownloadStationClient
from app.services.tmdb import TMDBClient
from app.services.torrent_app import TorrentAppService
from app.services.tvmaze import TVMazeClient
from app.services.updates import UpdateError, UpdateService

USER_AGENT = f"{APP_NAME}/{__version__} (+https://github.com/drunkgummyboy/AirGrabber)"


class Services:
    """Composition root, built once at startup inside the event loop and closed on exit.

    Views receive the individual services they need; none of them sees the database.
    """

    __slots__ = (
        "_db",
        "_http",
        "discovery",
        "downloads",
        "images",
        "library",
        "prowlarr_setup",
        "quick_download",
        "search",
        "settings",
        "station",
        "torrent_app",
        "updates",
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
        station: DownloadStationClient,
        search: TorrentSearchService,
        updates: UpdateService,
        prowlarr_setup: ProwlarrSetupService,
        torrent_app: TorrentAppService,
    ) -> None:
        self._db = db
        self._http = http
        self.settings = settings
        self.library = library
        self.images = images
        self.discovery = discovery
        self.downloads = downloads
        self.station = station  # also used directly by Settings ("Test connection")
        self.search = search
        # One-click Download: search, pick the best release, grab it.
        self.quick_download = QuickDownloadService(search, downloads, settings)
        self.updates = updates
        self.prowlarr_setup = prowlarr_setup
        self.torrent_app = torrent_app  # which torrent program is set up; qBittorrent install

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
            library = LibraryService(db.library, db.history, tvmaze)
            # Read on every request, so a key changed in Settings applies at once.
            tmdb = TMDBClient(
                http, api_key=lambda: settings.current.tmdb_api_key.get_secret_value()
            )
            discovery = DiscoveryService(tmdb, tvmaze, library)
            _clear_discovery_on_key_change(settings, discovery)
            images = ImageCache(http, paths.posters)
            # Reads the NAS address and credentials from the settings on every call.
            station = DownloadStationClient(settings)
            downloads = DownloadService(http, db.history, settings, station, paths.torrents)
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
                station,
                search,
                UpdateService(http),
                # Connects a Prowlarr on this PC and adds public sites to it (once).
                ProwlarrSetupService(http, settings),
                TorrentAppService(),
            )
            cleanup.pop_all()
        return services

    async def aclose(self) -> None:
        try:
            await self._http.aclose()
        finally:
            await self._db.close()


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
    "DiscoveryService",
    "DownloadService",
    "DownloadStationClient",
    "ImageCache",
    "LibraryService",
    "ProwlarrSetupService",
    "QuickDownloadService",
    "Services",
    "SettingsService",
    "TorrentAppService",
    "TorrentSearchService",
    "UpdateError",
    "UpdateService",
]
