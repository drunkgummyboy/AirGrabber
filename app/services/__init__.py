"""Service layer: the only objects the UI is allowed to talk to."""

from __future__ import annotations

import contextlib
import functools

import httpx

from app import APP_NAME, __version__
from app.config import AppPaths
from app.database import Database
from app.indexers import default_indexers
from app.models import Settings
from app.services.airdates import AirdatesClient, AirdatesError, AirdatesImportService
from app.services.auto_download import AutoDownloadService
from app.services.discovery import DiscoveryService
from app.services.download_status import DownloadStatusService
from app.services.downloads import DownloadService
from app.services.flaresolverr import FlareSolverrService
from app.services.images import ImageCache
from app.services.library import LibraryService
from app.services.missing import MissingDownloadService
from app.services.prowlarr_setup import ProwlarrSetupService
from app.services.quick_download import QuickDownloadService
from app.services.remote import RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.search import TorrentSearchService
from app.services import opensubtitles
from app.services.opensubtitles import OpenSubtitlesClient
from app.services.settings import SettingsService
from app.services.subtitles import SubtitleService
from app.services.synology import DownloadStationClient
from app.services.tmdb import TMDBClient, TMDBError
from app.services.torrent_app import TorrentAppService
from app.services.tvmaze import TVMazeClient
from app.services.updates import UpdateError, UpdateService
from app.services.watchlist import WatchlistService

USER_AGENT = f"{APP_NAME}/{__version__} (+https://github.com/drunkgummyboy/AirGrabber)"


class Services:
    """Composition root, built once at startup inside the event loop and closed on exit.

    Views receive the individual services they need; none of them sees the database.
    """

    __slots__ = (
        "_db",
        "_http",
        "airdates",
        "auto_download",
        "discovery",
        "download_status",
        "downloads",
        "flaresolverr",
        "images",
        "library",
        "missing",
        "prowlarr_setup",
        "quick_download",
        "remote",
        "search",
        "settings",
        "subtitles",
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
    ) -> None:
        self._db = db
        self._http = http
        self.settings = settings
        self.library = library
        self.images = images
        self.discovery = discovery
        self.downloads = downloads
        # The torrent program on a NAS or another computer; also Settings' "Test connection".
        self.remote = remote
        self.search = search
        # One-click Download: search, pick the best release, grab it.
        self.quick_download = QuickDownloadService(search, downloads, settings)
        # "Download missing": a tracked show's missing episodes, one at a time.
        self.missing = MissingDownloadService(library, self.quick_download)
        # The movie watchlist (in memory once `load`ed; Services.create does that).
        self.watchlist = WatchlistService(db.watchlist, discovery, downloads)
        # Downloads new episodes of the shows whose switch is on, and the watchlist films whose
        # "Download when it's out" is on (`run_periodic` from the UI).
        self.auto_download = AutoDownloadService(
            db.library, self.quick_download, settings, watchlist=self.watchlist
        )
        # Previews the shows followed on airdates.tv (import goes through library.import_shows).
        self.airdates = AirdatesImportService(AirdatesClient(http), library)
        self.updates = updates
        self.prowlarr_setup = prowlarr_setup
        self.torrent_app = torrent_app  # which torrent program is set up; qBittorrent install
        # Whether FlareSolverr runs on this PC (the Add FlareSolverr guide's Check).
        self.flaresolverr = FlareSolverrService(http)
        # Subtitles from OpenSubtitles.com next to the grabbed videos (`run_periodic` from the
        # UI), with the user's own API key when Settings has one, else AirGrabber's shipped key
        # (read on every request, so a changed key applies at once), and the user's optional
        # OpenSubtitles account.
        self.subtitles = SubtitleService(
            db.subtitles,
            OpenSubtitlesClient(
                http,
                api_key=lambda: opensubtitles.effective_api_key(
                    _own_opensubtitles_key(settings), opensubtitles.APP_API_KEY
                ),
                own_key=lambda: bool(_own_opensubtitles_key(settings)),
            ),
            settings,
            show=db.library.get_show,
            movie_imdb_id=functools.partial(_movie_imdb_id, discovery),
        )
        # The Downloads page: recent grabs with the torrent program's progress, free space
        # (`run_periodic` from the UI); looks again soon after every grab.
        self.download_status = DownloadStatusService(
            db.history, db.subtitles, settings, remote, show=db.library.get_show
        )
        downloads.subscribe(self.download_status.after_grab)

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
            # Reads the program, its address and account from the settings on every call.
            remote = RemoteDownloads(settings)
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
                UpdateService(http),
                # Connects a Prowlarr on this PC and adds public sites to it (once).
                ProwlarrSetupService(http, settings),
                TorrentAppService(),
            )
            await services.watchlist.load()
            cleanup.pop_all()
        return services

    @property
    def station(self) -> RemoteDownloads:
        """The older name of `remote`; main_window.py still passes it to Settings."""
        return self.remote

    async def aclose(self) -> None:
        try:
            await self._http.aclose()
        finally:
            await self._db.close()


def _own_opensubtitles_key(settings: SettingsService) -> str:
    return settings.current.opensubtitles_own_key.get_secret_value().strip()


async def _movie_imdb_id(discovery: DiscoveryService, movie_id: int) -> str | None:
    """A film's IMDb id from TMDB's details; None without a TMDB key or when TMDB fails."""
    try:
        return (await discovery.movie_details(movie_id)).imdb_id
    except TMDBError:
        return None


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
    "DiscoveryService",
    "DownloadService",
    "DownloadStatusService",
    "DownloadStationClient",
    "FlareSolverrService",
    "ImageCache",
    "LibraryService",
    "MissingDownloadService",
    "ProwlarrSetupService",
    "QuickDownloadService",
    "RemoteDownloads",
    "RemoteError",
    "Services",
    "SettingsService",
    "SubtitleService",
    "TorrentAppService",
    "TorrentSearchService",
    "UpdateError",
    "UpdateService",
    "WatchlistService",
]
