"""Service layer: the only objects the UI is allowed to talk to."""

from __future__ import annotations

import asyncio
import contextlib
import functools
from pathlib import Path

import httpx

from app import APP_NAME, __version__
from app.config import AppPaths
from app.database import BackupRepository, Database
from app.forge.runtime import ForgeRuntime
from app.indexers import default_indexers
from app.i18n import t
from app.models import Movie, SearchRequest, Settings, Show
from app.services.airdates import AirdatesClient, AirdatesError, AirdatesImportService
from app.services.auto_download import AutoDownloadService
from app.services.backup import BackupService
from app.services.discovery import DiscoveryService
from app.services.download_queue import QUEUE_FILE, DownloadQueue
from app.services.download_status import DownloadStatusService, forge_moves
from app.services.given_up import GivenUpService
from app.services.downloads import DownloadService, GrabError
from app.services.folder_library.playing import FolderPlaying
from app.services.folder_library.service import FolderLibraryService
from app.services.images import ImageCache
from app.services.library import LibraryService
from app.services.phone.collection import FilmSeries
from app.services.phone.coming_up import ComingUp, GrabbedFilms
from app.services.phone.copies import FOLDER as COPIES_FOLDER
from app.services.phone.copies import PhoneCopies
from app.services.phone.downloads import PhoneDownloads
from app.services.phone.forge import PhoneForge
from app.services.phone.library import PhoneLibrary, reachable_copy
from app.services.phone.play_links import PlayLinks
from app.services.phone.sends import PhoneSends
from app.services.phone.server import Route
from app.services.phone.service import PhoneService
from app.services.phone.shortlist import Shortlist
from app.services.phone.suggestions import Suggestions
from app.services.local_files import PLACES_FILE, LocalPlaces
from app.services.problem_report import ProblemReports
from app.services.playback import PlaybackService, forge_folders
from app.services.plex import PlexClient, PlexService
from app.services.plex_library import PlexLibraryService, library_posters
from app.services.plex_scans import PlexScansAfterMoves
from app.services.sonarr import SonarrClient
from app.services.profiles import ProfileService
from app.services.prowlarr_setup import ProwlarrSetupService
from app.services.qbittorrent_local import QBittorrentStarter
from app.services.qbittorrent_setup import QBittorrentSetup
from app.services.qbittorrent_theme import ThemeFolder
from app.services.quick_download import QuickDownloadService
from app.services.release_dates import RELEASE_DATES_FILE
from app.services.remote import RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.search import TorrentSearchService, movie_request
from app.services.settings import SettingsService
from app.services.spotlight import SpotlightService
from app.services.synology import DownloadStationClient
from app.services.tmdb import TMDBClient, TMDBError
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
        "playback",
        "local_places",
        "problem_reports",
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
        "folder_library",
        "folder_playing",
        "download_queue",
        "download_status",
        "downloads",
        "forge",
        "given_up",
        "images",
        "library",
        "phone",
        "phone_forge",
        "phone_sends",
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
        tmdb: TMDBClient,
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
        # runs (closing a window never cancels one). What waits at a quit is kept in the data
        # folder and queued again at the next start (`restore` from MainWindow._start).
        self.download_queue = DownloadQueue(
            self.quick_download,
            file=paths.root / QUEUE_FILE,
            film_request=functools.partial(_film_request, discovery),
        )
        # The film watchlists (the selected person's in memory once `load`ed: profiles.load);
        # a film added starts with "Download when it's out" as `auto_download_new_films` says.
        self.watchlist = WatchlistService(db.watchlist, discovery, downloads, settings=settings)
        # The people sharing AirGrabber and who is selected (Services.create `load`s them):
        # each has their own followed series, watchlist and calendar.
        self.profiles = ProfileService(db.profiles, settings, self.watchlist)
        # The Downloads page's "Couldn't find automatically": what the automatic downloads
        # gave up on, and its Try again (the give-up cleared, a download queued now).
        self.given_up = GivenUpService(
            library,
            self.watchlist,
            self.download_queue,
            film_request=functools.partial(_film_request, discovery),
        )
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
        # The folder library's side of ▶ Play (beside Plex or without it): its titles (its own
        # tables, `db.folders`) and files, and where each person left a video (`watch_places`).
        self.folder_playing = FolderPlaying(
            settings, db.library_files, db.watch_places, db.folders
        )
        # The Library page: Plex's shows and films, their posters from the Plex server (or the
        # folder library's, their posters TMDB's through the regular image cache).
        self.plex_library = PlexLibraryService(
            self.plex,
            library,
            folders=self.folder_playing,
            person=lambda: self.profiles.current_id,
        )
        self.plex_posters = ImageCache(
            http, paths.posters / "plex", fetch=library_posters(self.plex.poster, images.raw)
        )
        # ▶ Play in the info windows: Plex's file found through MediaForge's Series / Movies
        # folders (read from the Forge runtime when asked), or the folder library's own file,
        # played in AirGrabber's player (VLC as the fallback).
        self.playback = PlaybackService(
            self.plex,
            forge_folders(forge),
            folder_library=self.folder_playing,
            person=lambda: self.profiles.current_id,
            show_tmdb_id=discovery.show_tmdb_id,  # a show's folder marks when no title names it
        )
        # Where videos opened from Explorer's "Open with" were left (app/services/local_files.py).
        self.local_places = LocalPlaces(paths.root / PLACES_FILE)
        # Settings → Updates' "Report a problem": the logs in one zip on the desktop.
        self.problem_reports = ProblemReports(paths.logs, paths.root, __version__)
        # Import Shows → From Sonarr (a one-off read; nothing of it is stored).
        self.sonarr = SonarrClient(http)
        # What MediaForge moves into a library folder, Plex scans soon after (once Forge runs).
        self.plex_scans = PlexScansAfterMoves(self.plex)
        forge.when_ready(lambda started: self.plex_scans.attach(started.moves))
        # The user's own film and series folders, once switched on (Settings; `run` from the
        # UI), beside Plex or without it, in tables of their own (`db.folders`): read at the
        # start, every 6 hours and after MediaForge moves something into them (once Forge runs).
        self.folder_library = FolderLibraryService(
            settings, db.library_files, db.folders, tmdb, library, plex=self.plex
        )
        forge.when_ready(lambda started: self.folder_library.attach(started.moves))
        # The Companion app's way in, on the home network; off until turned on in Settings
        # (`run` from the UI). What it may ask besides pairing comes from `routes_factory`.
        grabbed_films = GrabbedFilms(self.download_status)
        suggestions = Suggestions(library, self.watchlist, discovery, self.profiles, settings)
        shortlist = Shortlist(settings, self.profiles, suggestions)
        # Phone-sized copies of 4K films, made on this PC's graphics card with MediaForge's
        # ffmpeg (app/services/phone/copies.py); the phone asks, or Send to phone does. A folder
        # library film's (Downloaded's, beside Plex too) is made from its own file that is there
        # now (no fetch).
        copies = PhoneCopies(
            self.plex,
            paths.root / COPIES_FOLDER,
            paths.forge / "tools" / "ffmpeg" / "bin",
            local_source=functools.partial(_own_file, self.folder_playing),
        )
        # Send to phone (the Library page) and each phone's menu of what it holds.
        self.phone_sends = PhoneSends(settings, copies=copies)
        # The folder library for a Companion that knows it, whenever the folders are on (without
        # Plex its library, beside Plex its Downloaded section): the films and shows, a title's
        # details and a show's episodes, with the phone's own person's places.
        # Play links (plain http) to a folder video for the phone's player and TVs: one set,
        # made by the library's route, served by the server the phone connection runs.
        play_links = PlayLinks()
        phone_library = PhoneLibrary(
            settings,
            self.folder_playing,
            db.library_files,
            db.watch_places,
            discovery,
            tmdb,
            play_links=play_links,
        )
        self.phone_forge = PhoneForge(forge, self.profiles)  # closed in aclose()
        phone_routes: dict[tuple[str, str], Route] = {
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
                suggestions,
            ).routes(),
            **suggestions.routes(),
            **shortlist.routes(),
            **self.phone_sends.routes(),
            **self.phone_forge.routes(),
            **FilmSeries(discovery.film_series).routes(),
            **phone_library.routes(),
        }
        phone_routes = {**phone_routes, **copies.routes()}
        self.phone = PhoneService(
            settings,
            paths,
            self.profiles,
            routes_factory=lambda: phone_routes,
            sends=self.phone_sends,
            copies=copies,
            play_links=play_links,
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
            discovery = DiscoveryService(
                tmdb, tvmaze, library, release_dates=paths.root / RELEASE_DATES_FILE
            )
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
                tmdb=tmdb,
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
        try:
            # What still waits (or runs) is written down for the next start.
            await self.download_queue.aclose()
            await self.phone.aclose()  # no phone is answered from here on
        finally:
            try:
                await self.phone_forge.aclose()  # its background reads stop waiting, whatever failed
            finally:
                await self._close_the_rest()

    async def _close_the_rest(self) -> None:
        try:
            # Before these: Forge uses the HTTP client, and forge.db's aiosqlite thread is no
            # daemon.
            await self.plex_scans.aclose()
            await self.folder_library.aclose()
            await self.forge.aclose()
        finally:
            try:
                await self._http.aclose()
            finally:
                await self._db.close()


FILM_LOOKUP_S = 15.0  # a kept film's TMDB description, at its turn after a restart


async def _film_request(discovery: DiscoveryService, movie_id: int, label: str) -> SearchRequest:
    """The request of a film the download queue kept across a restart, made at its turn as the
    film's ⬇ makes it (TMDB's description, then its details for the IMDb id)."""
    try:
        async with asyncio.timeout(FILM_LOOKUP_S):
            movie = await discovery.movie(movie_id)
    except (TMDBError, TimeoutError) as exc:
        raise GrabError(t("Couldn't look up {title} on TMDB", title=label)) from exc
    try:
        async with asyncio.timeout(FILM_LOOKUP_S):
            details = await discovery.movie_details(movie_id)
    except (TMDBError, TimeoutError):
        details = None  # searched by its title and year
    return movie_request(movie, details)


async def _own_file(playing: FolderPlaying, key: str) -> Path | None:
    """The folder library's best file of this video (a video key) that is there now, each copy
    stat'ed in a worker thread under the folder library's time limit; None when there is none,
    and while the folders are off (`playing.on` is `folders_on`, with or without Plex: an old
    list of files isn't read)."""
    if not playing.on:
        return None
    found = await reachable_copy(await playing.copies_of(key))
    return found[0] if found is not None else None


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
