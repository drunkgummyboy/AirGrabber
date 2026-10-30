"""qBittorrent on this PC through its Web UI, set up by AirGrabber.

qBittorrent's setup (app/services/qbittorrent_setup.py) can turn qBittorrent's Web UI on for
AirGrabber (127.0.0.1, a free port, a password of AirGrabber's: app/services/qbittorrent_webui.py).
Then AirGrabber sends its downloads there (DownloadMode.REMOTE, qBittorrent at
http://127.0.0.1:<port>, no destination = qBittorrent's own save folder) instead of opening each
one, so the Downloads page shows their progress and nothing pops up. This module decides when,
points AirGrabber at it, and keeps it reachable:

- `set_up_qbittorrent` (the "Set up qBittorrent for AirGrabber" button, through
  TorrentAppService) and `set_up_new_install` (main.py `--setup-qbittorrent` from install.ps1, on
  a new AirGrabber install only): once the "Add new torrent" pop-up is off, the Web UI is turned
  on and AirGrabber's settings point at it, only when `may_point_here`: in APP mode (qBittorrent
  on this PC gets the downloads already), when it is AirGrabber's own already, or on a new
  install (nothing chosen yet). A NAS / other computer or a folder the user chose stays as it is
  (then the Web UI isn't turned on either), and so does a Web UI of the user's own (AirGrabber
  keeps opening downloads in qBittorrent). "Where finished downloads end up" becomes
  qBittorrent's save folder while it is empty.
- `set_up_by_airgrabber(settings)`: the remote program is that qBittorrent (qBittorrent at an
  http address on this PC, with the user name "airgrabber"); Settings says so in plain words.
- LocalQBittorrent / QBittorrentStarter: a program on a NAS must be running to receive, and so
  must this one. When a grab, the Downloads page's check or Test connection can't reach it while
  qBittorrent isn't running, AirGrabber starts qBittorrent (its exe from the registry, without
  its splash screen, minimized; in the normal way while its first-run notice still waits for the
  user, who accepts that in qBittorrent itself), waits up to START_TIMEOUT_S for its Web UI and
  tries once more; else a plain message.

The password is AirGrabber's `remote_password` (a secret: never logged or shown); it travels in
the Web UI's login POST body only (app/services/qbittorrent.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from app.config import AppPaths
from app.database import Database
from app.i18n import N_, t
from app.models import DownloadMode, RemoteKind, RemoteStatus, Settings
from app.models import QBittorrentSetupResult as SetupResult
from app.services.qbittorrent_setup import QBittorrentSetup, qbittorrent_running
from app.services.qbittorrent_webui import ADDRESS, USERNAME, WebUiLogin, WebUiResult
from app.services.remote import DiskSpace, RemoteClient, RemoteError, RemoteStatusClient
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

_T = TypeVar("_T")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
START_TIMEOUT_S = 20.0  # how long a start waits for the Web UI
POLL_S = 0.5
RECENT_START_S = 60.0  # qBittorrent started this recently by AirGrabber: worth another try
RETRY_START_AFTER_S = 60.0  # after a start that failed, none again for this long
START_ARGUMENTS = ("--no-splash",)  # src/app/cmdoptions.cpp
SW_SHOWMINNOACTIVE = 7  # minimized, not in front
# Shown through t().
NOT_STARTED = N_(
    "qBittorrent isn't running, and AirGrabber couldn't start it. Start qBittorrent, then try "
    "again."
)
SLOW_START = N_("qBittorrent didn't start in time. Try again in a moment.")
NOTICE_WAITING = N_(
    "qBittorrent is waiting for you: accept its notice in its window, then try again."
)


# -- AirGrabber's settings (pure) -----------------------------------------------------------------


def local_port(url: str) -> int | None:
    """The port of an http address on this PC (127.0.0.1, localhost, ::1); else None."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "http" or host is None or host.casefold() not in LOOPBACK_HOSTS:
        return None
    return port or 80


def set_up_by_airgrabber(settings: Settings) -> bool:
    """The remote program in `settings` is the qBittorrent on this PC that AirGrabber set up."""
    return (
        settings.remote_kind is RemoteKind.QBITTORRENT
        and settings.remote_username == USERNAME
        and local_port(settings.remote_url) is not None
    )


def known_login(settings: Settings) -> WebUiLogin | None:
    """The Web UI login AirGrabber's settings hold for it; None when they hold none."""
    port = local_port(settings.remote_url)
    password = settings.remote_password.get_secret_value()
    if not set_up_by_airgrabber(settings) or port is None or not password:
        return None
    return WebUiLogin(port, settings.remote_username, password)


def may_point_here(settings: Settings, *, fresh: bool) -> bool:
    """Whether the setup may point AirGrabber at qBittorrent's Web UI: in APP mode, when the
    remote program is it already, or (`fresh`: a new install, nothing chosen yet) a folder no
    one chose with no NAS / other computer set up. Never over the user's own choice."""
    if settings.download_mode is DownloadMode.APP:
        return True
    if settings.download_mode is DownloadMode.REMOTE:
        return set_up_by_airgrabber(settings)
    return fresh and not settings.remote_configured


def airgrabber_changes(settings: Settings, login: WebUiLogin, save_path: str) -> dict[str, Any]:
    """What AirGrabber stores to send its downloads to the Web UI: REMOTE, qBittorrent at its
    address with the login, qBittorrent's own save folder (no destination), and that folder as
    "Where finished downloads end up" while that is empty."""
    changes: dict[str, Any] = {
        "download_mode": DownloadMode.REMOTE,
        "remote_kind": RemoteKind.QBITTORRENT,
        "remote_url": login.url,
        "remote_username": login.username,
        "remote_password": login.password,
        "remote_destination": "",
    }
    if not settings.subtitles_folder.strip() and save_path:
        changes["subtitles_folder"] = save_path
    return changes


# -- the setup ------------------------------------------------------------------------------------


async def set_up_qbittorrent(
    setup: QBittorrentSetup, settings: SettingsService, *, fresh: bool
) -> SetupResult:
    """The button's setup: the "Add new torrent" pop-up off (and AirGrabber's look), then, when
    `may_point_here`, the Web UI on and AirGrabber pointed at it. RUNNING while qBittorrent
    runs (nothing changed). The blocking parts run in a thread."""
    result = await asyncio.to_thread(setup.set_up)
    if result not in (SetupResult.DONE, SetupResult.ALREADY):
        return result
    web = await set_up_web_ui(setup, settings, fresh=fresh)
    if web is WebUiResult.RUNNING:
        return SetupResult.RUNNING
    return SetupResult.DONE if web is WebUiResult.CREATED else result


async def set_up_web_ui(
    setup: QBittorrentSetup, settings: SettingsService, *, fresh: bool
) -> WebUiResult | None:
    """The Web UI on and AirGrabber's settings pointed at it, when `may_point_here`; None when
    not (nothing changed). A failure to turn it on is logged; AirGrabber's settings then stay."""
    if not may_point_here(settings.current, fresh=fresh):
        return None
    web, login = await asyncio.to_thread(setup.set_up_web_ui, known_login(settings.current))
    if login is None:
        return web
    save_path = await asyncio.to_thread(_save_path, setup)
    current = settings.current  # it may have changed meanwhile
    if may_point_here(current, fresh=fresh):
        await settings.update(**airgrabber_changes(current, login, save_path))
        log.info("Downloads now go to qBittorrent's Web UI on this PC")
    return web


def _save_path(setup: QBittorrentSetup) -> str:
    try:
        return setup.state().save_path
    except OSError:
        return ""


def set_up_new_install(setup: QBittorrentSetup, data_dir: Path) -> None:
    """main.py `--setup-qbittorrent` (install.ps1, right after it installed qBittorrent), after
    the pop-up was turned off: on a new AirGrabber install (no database yet, so no AirGrabber
    runs or has settings to keep) the Web UI on and AirGrabber pointed at it. An existing
    install keeps its settings (Settings' button does it there). Blocking; never raises."""
    database = AppPaths(data_dir).database
    if database.exists():
        log.info("Not a new install: AirGrabber's download settings stay as they are")
        return
    try:
        asyncio.run(_set_up_new_install(setup, database))
    except Exception:
        log.exception("Pointing AirGrabber at qBittorrent's Web UI failed")


async def _set_up_new_install(setup: QBittorrentSetup, database: Path) -> None:
    db = await Database.open(database)
    try:
        settings = await SettingsService.load(db.settings)
        await set_up_web_ui(setup, settings, fresh=True)
    finally:
        await db.close()


# -- keeping it reachable -------------------------------------------------------------------------

# (exe, minimized) -> the started process (kept, never waited for).
Launcher = Callable[[str, bool], object]
WebUiCheck = Callable[[int], Awaitable[bool]]


def launch_qbittorrent(exe: str, minimized: bool) -> object:
    """Start qBittorrent on its own (not tied to AirGrabber), without its splash screen, and
    when `minimized` minimized and not in front. Raises OSError. Blocking, briefly."""
    if sys.platform != "win32":
        raise OSError("qBittorrent is only started on Windows")
    info = subprocess.STARTUPINFO()
    if minimized:
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = SW_SHOWMINNOACTIVE
    return subprocess.Popen(
        [exe, *START_ARGUMENTS],
        cwd=str(Path(exe).parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        startupinfo=info,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


async def web_ui_listening(port: int) -> bool:
    """Something accepts connections at 127.0.0.1:`port` (qBittorrent's Web UI once it is up)."""
    try:
        async with asyncio.timeout(1.0):
            _reader, writer = await asyncio.open_connection(ADDRESS, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


def _notice_accepted() -> bool:
    return QBittorrentSetup().legal_notice_accepted()


class QBittorrentStarter:
    """Starts qBittorrent on this PC when its Web UI can't be reached because it isn't running
    (one start at a time). Everything it touches is injectable, so tests start nothing."""

    def __init__(
        self,
        *,
        find_exe: Callable[[], str | None],
        running: Callable[[], bool] = qbittorrent_running,
        notice_accepted: Callable[[], bool] = _notice_accepted,
        launch: Launcher = launch_qbittorrent,
        web_ui_up: WebUiCheck = web_ui_listening,
        timeout: float = START_TIMEOUT_S,
        poll: float = POLL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._find_exe = find_exe
        self._running = running
        self._notice_accepted = notice_accepted
        self._launch = launch
        self._web_ui_up = web_ui_up
        self._timeout = timeout
        self._poll = poll
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._started_at: float | None = None
        self._failed_at: float | None = None
        self._process: object | None = None  # the last one started (kept, never waited for)

    async def start(self, port: int) -> bool:
        """After the Web UI at `port` couldn't be reached: True = try again (qBittorrent was
        started just now and its Web UI answers); False = qBittorrent was running, so the
        failure stands. Raises RemoteError (plain words) when qBittorrent couldn't be started,
        its Web UI didn't answer in time, or its first-run notice waits for the user."""
        async with self._lock:
            now = self._clock()
            if await asyncio.to_thread(self._running):
                recent = self._started_at is not None and now - self._started_at < RECENT_START_S
                if recent and await self._web_ui_up(port):
                    return True
                if not await asyncio.to_thread(self._notice_accepted):
                    raise RemoteError(t(NOTICE_WAITING))
                return False
            if self._failed_at is not None and now - self._failed_at < RETRY_START_AFTER_S:
                raise RemoteError(t(NOT_STARTED))
            accepted = await self._launch_now()
            if await self._wait(port):
                self._failed_at = None
                return True
            self._failed_at = self._clock()
            log.info("qBittorrent's Web UI didn't answer in time")
            raise RemoteError(t(SLOW_START if accepted else NOTICE_WAITING))

    async def _launch_now(self) -> bool:
        """Start qBittorrent: minimized once its notice was accepted, else in the normal way (the
        user sees the notice). Returns whether it was accepted. Raises RemoteError."""
        exe = await asyncio.to_thread(self._find_exe)
        if exe is None:
            self._failed_at = self._clock()
            log.info("qBittorrent isn't running and wasn't found")
            raise RemoteError(t(NOT_STARTED))
        accepted = await asyncio.to_thread(self._notice_accepted)
        failure = ""
        try:
            self._process = await asyncio.to_thread(self._launch, exe, accepted)
        except OSError as exc:
            failure = type(exc).__name__
        if failure:
            self._failed_at = self._clock()
            log.warning("Couldn't start qBittorrent (%s)", failure)
            raise RemoteError(t(NOT_STARTED))
        self._started_at = self._clock()
        log.info("qBittorrent wasn't running: started it (%s)", "minimized" if accepted else "shown")
        return accepted

    async def _wait(self, port: int) -> bool:
        deadline = self._clock() + self._timeout
        while True:
            if await self._web_ui_up(port):
                return True
            if self._clock() >= deadline:
                return False
            await self._sleep(self._poll)


class LocalQBittorrent:
    """The qBittorrent client for AirGrabber's qBittorrent on this PC (RemoteClient, with the
    Downloads page's status calls): when a call can't reach it, `starter` starts qBittorrent and
    the call is tried once more."""

    def __init__(self, client: RemoteClient, starter: QBittorrentStarter, port: int) -> None:
        self._client = client
        self._starter = starter
        self._port = port

    @property
    def kind(self) -> RemoteKind:
        return self._client.kind

    async def test(self) -> str:
        return await self._again(self._client.test)

    async def add_magnet(self, magnet: str) -> None:
        await self._again(lambda: self._client.add_magnet(magnet))

    async def add_torrent(self, content: bytes, filename: str) -> None:
        await self._again(lambda: self._client.add_torrent(content, filename))

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        return await self._again(lambda: self._status_client().status(hashes))

    async def free_space(self) -> DiskSpace | None:
        return await self._again(lambda: self._status_client().free_space())

    def _status_client(self) -> RemoteStatusClient:
        client = self._client
        if not isinstance(client, RemoteStatusClient):
            raise RemoteError(
                t("AirGrabber can't read the downloads of {program}.", program=client.kind.program)
            )
        return client

    async def _again(self, call: Callable[[], Awaitable[_T]]) -> _T:
        try:
            return await call()
        except RemoteError as exc:
            failure = exc
        if not await self._starter.start(self._port):
            raise failure
        return await call()
