"""qBittorrent on this PC through its Web UI, set up by AirGrabber (app/services/qbittorrent_local.py):
when the setup may point AirGrabber at it (APP mode, its own, a new install; never a NAS or a
folder the user chose), what it stores, main.py's new-install path, starting qBittorrent when it
isn't running (fake process check, launcher, Web UI check and clock: nothing is started), the
client wrapper and RemoteDownloads, a grab end to end on httpx.MockTransport, the welcome's
choice and Settings' line and reload. Temporary files and databases only."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk
import httpx

from app.config import AppPaths
from app.database import Database, SettingsRepository
from app.models import DownloadMode, QBittorrentSetupResult, RemoteKind, RemoteStatus, Settings
from app.services import SettingsService
from app.services import qbittorrent_local as local
from app.services.qbittorrent_local import (
    NOT_STARTED,
    NOTICE_WAITING,
    SLOW_START,
    LocalQBittorrent,
    QBittorrentStarter,
    airgrabber_changes,
    known_login,
    local_port,
    may_point_here,
    set_up_by_airgrabber,
    set_up_new_install,
)
from app.services.qbittorrent_setup import IniKey, QBittorrentSetup, raw_value, run_setup
from app.services.qbittorrent_webui import PortCheck, WebUiLogin, WebUiResult
from app.services.remote import DiskSpace, RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.torrent_app import TorrentAppService, qbittorrent_executable
from app.ui.settings_dialog import SettingsDialog, download_settings
from app.ui.welcome_dialog import download_changes
from tests.test_settings_dialog import FakeProwlarr, FakeRepository, FakeShortcut

FIXTURES = Path(__file__).parent / "fixtures" / "qbittorrent"
HOME = Path(r"C:\Users\Alex")
SALT = bytes(range(16))
LOGIN = WebUiLogin(8081, "airgrabber", "pw-1")
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def local_settings(**values: Any) -> Settings:
    """Settings pointed at AirGrabber's qBittorrent on this PC."""
    base = Settings().model_dump()
    base.update(airgrabber_changes(Settings(), LOGIN, r"D:\Torrents"))
    base.update(values)
    return Settings.model_validate(base)


class FakeRunning:
    def __init__(self, running: bool = False) -> None:
        self.running = running

    def __call__(self) -> bool:
        return self.running


class FakePortsSetup(QBittorrentSetup):
    """The real setup on a temporary file, with 8080 taken and a fixed password and salt."""

    def set_up_web_ui(
        self,
        known: WebUiLogin | None = None,
        *,
        port_free: PortCheck = lambda port: port != 8080,
        spare_port: Any = lambda: 50123,
        new_password: Any = lambda: "pw-1",
        new_salt: Any = lambda: SALT,
    ) -> tuple[WebUiResult, WebUiLogin | None]:
        return super().set_up_web_ui(
            known,
            port_free=lambda port: port != 8080,
            spare_port=lambda: 50123,
            new_password=lambda: "pw-1",
            new_salt=lambda: SALT,
        )


class MemoryRepository:
    def __init__(self) -> None:
        self.saved: list[Settings] = []

    async def save(self, settings: Settings) -> None:
        self.saved.append(settings)


def service(settings: Settings) -> SettingsService:
    return SettingsService(MemoryRepository(), settings)  # type: ignore[arg-type]


# -- AirGrabber's settings ------------------------------------------------------------------------


class SettingsRuleTests(unittest.TestCase):
    def test_qbittorrent_on_this_pc_set_up_by_airgrabber(self) -> None:
        self.assertTrue(set_up_by_airgrabber(local_settings()))
        self.assertTrue(set_up_by_airgrabber(local_settings(remote_url="http://localhost:8081")))
        cases: dict[str, dict[str, Any]] = {
            "a NAS": {"remote_url": "http://192.168.1.10:8081"},
            "https": {"remote_url": "https://127.0.0.1:8081"},
            "another user": {"remote_username": "admin"},
            "Download Station": {"remote_kind": RemoteKind.SYNOLOGY},
        }
        for name, values in cases.items():
            with self.subTest(name):
                self.assertFalse(set_up_by_airgrabber(local_settings(**values)))
        self.assertFalse(set_up_by_airgrabber(Settings()))

    def test_ports(self) -> None:
        self.assertEqual(local_port("http://127.0.0.1:8081"), 8081)
        self.assertEqual(local_port("http://[::1]:9000"), 9000)
        self.assertEqual(local_port("http://localhost"), 80)
        self.assertIsNone(local_port("http://nas.local:8080"))
        self.assertIsNone(local_port("not a url"))

    def test_the_known_login(self) -> None:
        self.assertEqual(known_login(local_settings()), LOGIN)
        self.assertIsNone(known_login(local_settings(remote_password="")))
        self.assertIsNone(known_login(Settings()))

    def test_when_the_setup_may_point_airgrabber_at_it(self) -> None:
        synology = {"remote_url": "https://nas:5001", "remote_username": "u", "remote_password": "p"}
        cases = [
            ("APP", Settings(download_mode=DownloadMode.APP), False, True),
            ("its own already", local_settings(), False, True),
            ("a NAS", Settings(download_mode=DownloadMode.REMOTE, **synology), True, False),
            ("a folder the user chose", Settings(download_mode=DownloadMode.FOLDER), False, False),
            ("a new install", Settings(), True, True),
            ("new, with a NAS for magnets", Settings(**synology), True, False),
        ]
        for name, settings, fresh, expected in cases:
            with self.subTest(name):
                self.assertIs(may_point_here(settings, fresh=fresh), expected)

    def test_what_is_stored(self) -> None:
        changes = airgrabber_changes(Settings(download_mode=DownloadMode.APP), LOGIN, r"D:\Torrents")
        self.assertEqual(
            changes,
            {
                "download_mode": DownloadMode.REMOTE,
                "remote_kind": RemoteKind.QBITTORRENT,
                "remote_url": "http://127.0.0.1:8081",
                "remote_username": "airgrabber",
                "remote_password": "pw-1",
                "remote_destination": "",
                "subtitles_folder": r"D:\Torrents",
            },
        )
        chosen = airgrabber_changes(Settings(subtitles_folder=r"\\NAS\video"), LOGIN, r"D:\T")
        self.assertNotIn("subtitles_folder", chosen)  # a chosen folder stays
        self.assertNotIn("pw-1", repr(Settings.model_validate({**Settings().model_dump(), **changes})))


# -- the setup ------------------------------------------------------------------------------------


class SetupTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "Roaming" / "qBittorrent"
        self.path = self.folder / "qBittorrent.ini"
        self.running = FakeRunning()
        self.setup = FakePortsSetup(self.path, running=self.running, home=HOME)

    def write(self, name: str) -> bytes:
        data = (FIXTURES / name).read_bytes()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)
        return data

    def ini(self, section: str, key: str) -> str | None:
        return raw_value(self.path.read_bytes().decode("utf-8"), IniKey(section, key))


class SetUpQBittorrentTests(SetupTestCase):
    async def run_setup(self, settings: Settings, *, fresh: bool = False) -> tuple[QBittorrentSetupResult, SettingsService]:
        settings_service = service(settings)
        result = await local.set_up_qbittorrent(self.setup, settings_service, fresh=fresh)
        return result, settings_service

    async def test_app_mode_is_pointed_at_the_web_ui(self) -> None:
        self.write("settings_5.ini")
        result, settings = await self.run_setup(Settings(download_mode=DownloadMode.APP))
        self.assertIs(result, QBittorrentSetupResult.DONE)
        current = settings.current
        self.assertIs(current.download_mode, DownloadMode.REMOTE)
        self.assertIs(current.remote_kind, RemoteKind.QBITTORRENT)
        self.assertEqual(current.remote_url, "http://127.0.0.1:8081")  # 8080 was taken
        self.assertEqual(current.remote_username, "airgrabber")
        self.assertEqual(current.remote_password.get_secret_value(), "pw-1")
        self.assertEqual(current.remote_destination, "")
        self.assertEqual(current.subtitles_folder, r"D:\Torrents")  # qBittorrent's save folder
        self.assertTrue(set_up_by_airgrabber(current))
        self.assertEqual(self.ini("AddNewTorrentDialog", "Enabled"), "false")  # the pop-up too
        self.assertEqual(self.ini("WebUI", "Port"), "8081")
        self.assertEqual(self.ini("Preferences", "WebUI\\Enabled"), "true")

    async def test_pressing_it_again_changes_nothing(self) -> None:
        self.write("settings_5.ini")
        _result, settings = await self.run_setup(Settings(download_mode=DownloadMode.APP))
        before = self.path.read_bytes()
        result = await local.set_up_qbittorrent(self.setup, settings, fresh=False)
        self.assertIs(result, QBittorrentSetupResult.ALREADY)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(known_login(settings.current), LOGIN)

    async def test_a_nas_or_a_chosen_folder_stays_and_gets_no_web_ui(self) -> None:
        synology = Settings(
            download_mode=DownloadMode.REMOTE,
            remote_url="https://nas:5001",
            remote_username="u",
            remote_password="p",
        )
        for name, settings in (
            ("NAS", synology),
            ("folder", Settings(download_mode=DownloadMode.FOLDER, setup_done=True)),
        ):
            with self.subTest(name):
                self.write("settings_5.ini")
                result, settings_service = await self.run_setup(settings)
                self.assertIs(result, QBittorrentSetupResult.DONE)  # the pop-up is off
                self.assertEqual(settings_service.current, settings)
                self.assertIsNone(self.ini("WebUI", "Enabled"))
                self.assertIsNone(self.ini("Preferences", "WebUI\\Enabled"))

    async def test_a_new_install_is_pointed_at_it(self) -> None:
        result, settings = await self.run_setup(Settings(), fresh=True)
        self.assertIs(result, QBittorrentSetupResult.DONE)
        self.assertTrue(set_up_by_airgrabber(settings.current))
        self.assertIs(settings.current.download_mode, DownloadMode.REMOTE)

    async def test_the_users_own_web_ui_keeps_app_mode(self) -> None:
        original = self.write("settings_5_3_webui.ini")
        result, settings = await self.run_setup(Settings(download_mode=DownloadMode.APP))
        self.assertIs(result, QBittorrentSetupResult.ALREADY)  # its pop-up was off already
        self.assertIs(settings.current.download_mode, DownloadMode.APP)
        self.assertEqual(self.path.read_bytes(), original)

    async def test_nothing_while_qbittorrent_runs(self) -> None:
        original = self.write("settings_5.ini")
        self.running.running = True
        result, settings = await self.run_setup(Settings(download_mode=DownloadMode.APP))
        self.assertIs(result, QBittorrentSetupResult.RUNNING)
        self.assertIs(settings.current.download_mode, DownloadMode.APP)
        self.assertEqual(self.path.read_bytes(), original)

    async def test_the_service_counts_an_unfinished_first_run_as_new(self) -> None:
        settings = service(Settings())
        app = TorrentAppService(lambda: None, qbittorrent=self.setup, settings=settings)
        self.assertIs(await app.set_up_qbittorrent(), QBittorrentSetupResult.DONE)
        self.assertTrue(set_up_by_airgrabber(settings.current))
        done = service(Settings(setup_done=True))  # a folder chosen in the welcome
        self.path.unlink()
        app = TorrentAppService(lambda: None, qbittorrent=self.setup, settings=done)
        await app.set_up_qbittorrent()
        self.assertIs(done.current.download_mode, DownloadMode.FOLDER)
        # Without settings: only qBittorrent's own file, as before.
        plain = TorrentAppService(lambda: None, qbittorrent=self.setup)
        self.assertIs(await plain.set_up_qbittorrent(), QBittorrentSetupResult.ALREADY)


class NewInstallTests(SetupTestCase):
    def stored(self, data_dir: Path) -> Settings:
        async def load() -> Settings:
            db = await Database.open(AppPaths(data_dir).database)
            try:
                return await SettingsRepository(db).load()
            finally:
                await db.close()

        return asyncio.run(load())

    def test_a_new_install_is_pointed_at_the_web_ui(self) -> None:
        self.write("settings_5.ini")
        data = self.root / "Local" / "AirGrabber"
        data.mkdir(parents=True)
        self.assertEqual(run_setup(self.setup, data_dir=data), 0)  # main.py's way in
        stored = self.stored(data)
        self.assertTrue(set_up_by_airgrabber(stored))
        self.assertEqual(stored.remote_password.get_secret_value(), "pw-1")
        self.assertEqual(stored.subtitles_folder, r"D:\Torrents")
        self.assertEqual(self.ini("WebUI", "Enabled"), "true")

    def test_an_existing_install_keeps_its_settings(self) -> None:
        self.write("settings_5.ini")
        data = self.root / "Local" / "AirGrabber"
        data.mkdir(parents=True)
        AppPaths(data).database.write_bytes(b"")  # a database is there already
        set_up_new_install(self.setup, data)
        self.assertIsNone(self.ini("WebUI", "Enabled"))
        self.assertEqual(AppPaths(data).database.read_bytes(), b"")

    def test_it_never_raises(self) -> None:
        data = self.root / "Local" / "AirGrabber"
        data.mkdir(parents=True)
        with mock.patch.object(local, "Database") as database:
            database.open = mock.AsyncMock(side_effect=OSError("disk"))
            with self.assertLogs(local.log, "ERROR"):
                set_up_new_install(self.setup, data)


# -- starting qBittorrent -------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class StarterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.running = FakeRunning()
        self.exe: str | None = r"C:\Program Files\qBittorrent\qbittorrent.exe"
        self.accepted = True
        self.launches: list[tuple[str, bool]] = []
        self.launch_error: OSError | None = None
        self.up_after = 2  # the Web UI answers from the third look
        self.looks: list[int] = []
        self.clock = FakeClock()
        self.starter = QBittorrentStarter(
            find_exe=lambda: self.exe,
            running=self.running,
            notice_accepted=lambda: self.accepted,
            launch=self.launch,
            web_ui_up=self.web_ui_up,
            timeout=5.0,
            poll=0.5,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def launch(self, exe: str, minimized: bool) -> object:
        if self.launch_error is not None:
            raise self.launch_error
        self.launches.append((exe, minimized))
        self.running.running = True
        return object()

    async def web_ui_up(self, port: int) -> bool:
        self.looks.append(port)
        return bool(self.launches) and len(self.looks) > self.up_after

    async def test_starts_it_minimized_and_waits_for_the_web_ui(self) -> None:
        self.assertTrue(await self.starter.start(8081))
        self.assertEqual(self.launches, [(self.exe, True)])
        self.assertEqual(self.looks, [8081, 8081, 8081])
        self.assertAlmostEqual(self.clock.now, 1001.0)  # two half-second waits

    async def test_shown_while_its_notice_waits_for_the_user(self) -> None:
        self.accepted = False
        self.up_after = 1000  # the notice is never answered
        with self.assertRaises(RemoteError) as raised:
            await self.starter.start(8081)
        self.assertEqual(self.launches, [(self.exe, False)])
        self.assertEqual(str(raised.exception), NOTICE_WAITING)
        # Still running, the notice still waiting: said again, nothing started twice.
        with self.assertRaises(RemoteError) as again:
            await self.starter.start(8081)
        self.assertEqual(str(again.exception), NOTICE_WAITING)
        self.assertEqual(len(self.launches), 1)

    async def test_too_slow(self) -> None:
        self.up_after = 1000
        with self.assertRaises(RemoteError) as raised:
            await self.starter.start(8081)
        self.assertEqual(str(raised.exception), SLOW_START)
        self.assertGreaterEqual(self.clock.now, 1005.0)

    async def test_not_found_or_not_startable(self) -> None:
        self.exe = None
        with self.assertRaises(RemoteError) as raised:
            await self.starter.start(8081)
        self.assertEqual(str(raised.exception), NOT_STARTED)
        self.assertEqual(self.launches, [])
        # Not again for a minute, then once more.
        self.exe = r"C:\q\qbittorrent.exe"
        self.clock.now += 30
        with self.assertRaises(RemoteError):
            await self.starter.start(8081)
        self.assertEqual(self.launches, [])
        self.clock.now += 31
        self.launch_error = PermissionError("denied")
        with self.assertRaises(RemoteError) as failed, self.assertLogs(local.log, "WARNING"):
            await self.starter.start(8081)
        self.assertEqual(str(failed.exception), NOT_STARTED)

    async def test_already_running_the_failure_stands(self) -> None:
        self.running.running = True
        self.assertFalse(await self.starter.start(8081))
        self.assertEqual(self.launches, [])
        self.assertEqual(self.looks, [])

    async def test_just_started_by_another_call(self) -> None:
        self.assertTrue(await self.starter.start(8081))
        self.clock.now += 10
        self.assertTrue(await self.starter.start(8081))  # running, started just now, up
        self.assertEqual(len(self.launches), 1)
        self.clock.now += 120
        self.assertFalse(await self.starter.start(8081))  # long ago: the failure stands

    async def test_one_start_at_a_time(self) -> None:
        first, second = await asyncio.gather(self.starter.start(8081), self.starter.start(8081))
        self.assertTrue(first and second)
        self.assertEqual(len(self.launches), 1)


class FakeClient:
    """A RemoteClient with status calls: fails `failures` times with RemoteError, then works."""

    kind = RemoteKind.QBITTORRENT

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.calls: list[str] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.failures > 0:
            self.failures -= 1
            raise RemoteError("qBittorrent at 127.0.0.1:8081 couldn't be reached.")

    async def test(self) -> str:
        self._call("test")
        return "qBittorrent v5.1.2"

    async def add_magnet(self, magnet: str) -> None:
        self._call("add_magnet")

    async def add_torrent(self, content: bytes, filename: str) -> None:
        self._call("add_torrent")

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        self._call("status")
        return RemoteStatus(program="qBittorrent", tasks=())

    async def free_space(self) -> DiskSpace | None:
        self._call("free_space")
        return DiskSpace(5)


class FakeStarter:
    def __init__(self, answer: bool | RemoteError = True) -> None:
        self.answer = answer
        self.ports: list[int] = []

    async def start(self, port: int) -> bool:
        self.ports.append(port)
        if isinstance(self.answer, RemoteError):
            raise self.answer
        return self.answer


class WrapperTests(unittest.IsolatedAsyncioTestCase):
    def wrap(self, client: FakeClient, starter: FakeStarter) -> LocalQBittorrent:
        return LocalQBittorrent(client, starter, 8081)  # type: ignore[arg-type]

    async def test_a_failed_call_starts_it_and_tries_once_more(self) -> None:
        for name in ("test", "add_magnet", "add_torrent", "status", "free_space"):
            with self.subTest(name):
                client, starter = FakeClient(), FakeStarter()
                wrapper = self.wrap(client, starter)
                calls: dict[str, Callable[[], Awaitable[object]]] = {
                    "test": wrapper.test,
                    "add_magnet": lambda: wrapper.add_magnet(MAGNET),
                    "add_torrent": lambda: wrapper.add_torrent(b"d4:infoe", "x.torrent"),
                    "status": wrapper.status,
                    "free_space": wrapper.free_space,
                }
                await calls[name]()
                self.assertEqual(client.calls, [name, name])
                self.assertEqual(starter.ports, [8081])

    async def test_a_working_call_starts_nothing(self) -> None:
        client, starter = FakeClient(failures=0), FakeStarter()
        await self.wrap(client, starter).add_magnet(MAGNET)
        self.assertEqual((client.calls, starter.ports), (["add_magnet"], []))

    async def test_running_already_the_first_failure_stands(self) -> None:
        client, starter = FakeClient(), FakeStarter(False)
        with self.assertRaises(RemoteError) as raised:
            await self.wrap(client, starter).add_magnet(MAGNET)
        self.assertIn("couldn't be reached", str(raised.exception))
        self.assertEqual(client.calls, ["add_magnet"])

    async def test_a_start_that_fails_says_so(self) -> None:
        client, starter = FakeClient(), FakeStarter(RemoteError("plain words"))
        with self.assertRaises(RemoteError) as raised:
            await self.wrap(client, starter).add_magnet(MAGNET)
        self.assertEqual(str(raised.exception), "plain words")

    async def test_remote_downloads_wraps_only_airgrabbers_qbittorrent(self) -> None:
        clients = {kind: FakeClient(failures=0) for kind in RemoteKind}
        starter = FakeStarter()
        settings = service(local_settings())
        remote = RemoteDownloads(settings, clients=clients, starter=starter)  # type: ignore[arg-type]
        self.assertIsInstance(remote.client(), LocalQBittorrent)
        self.assertEqual(await remote.status(), RemoteStatus(program="qBittorrent", tasks=()))
        await settings.update(remote_url="http://192.168.1.10:8080")
        self.assertIs(remote.client(), clients[RemoteKind.QBITTORRENT])
        await settings.update(remote_url="http://127.0.0.1:8081", remote_kind=RemoteKind.SYNOLOGY)
        self.assertIs(remote.client(), clients[RemoteKind.SYNOLOGY])
        unwrapped = RemoteDownloads(service(local_settings()), clients=clients)
        self.assertIs(unwrapped.client(), clients[RemoteKind.QBITTORRENT])  # no starter


class GrabEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """A magnet sent to the Web UI while qBittorrent is closed: refused connection, qBittorrent
    started (fakes), signed in, added; the password only in the login POST body."""

    async def test_sent_after_starting_qbittorrent(self) -> None:
        started = False
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not started:
                raise httpx.ConnectError("refused", request=request)
            requests.append(request)
            if request.url.path == "/api/v2/auth/login":
                return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"})
            return httpx.Response(200, text="Ok.")

        def launch(exe: str, minimized: bool) -> object:
            nonlocal started
            started = True
            return object()

        async def up(port: int) -> bool:
            return started

        starter = QBittorrentStarter(
            find_exe=lambda: r"C:\q\qbittorrent.exe",
            running=lambda: started,
            notice_accepted=lambda: True,
            launch=launch,
            web_ui_up=up,
        )
        remote = RemoteDownloads(
            service(local_settings()), transport=httpx.MockTransport(handler), starter=starter
        )
        await remote.client().add_magnet(MAGNET)
        paths = [request.url.path for request in requests]
        self.assertEqual(paths, ["/api/v2/auth/login", "/api/v2/torrents/add", "/api/v2/auth/logout"])
        login = requests[0]
        self.assertIn(b"password=pw-1", login.content)
        self.assertTrue(all("pw-1" not in str(request.url) for request in requests))
        self.assertTrue(all(request.url.host == "127.0.0.1" for request in requests))


class ExecutableTests(unittest.TestCase):
    def test_qbittorrents_exe_from_the_registry(self) -> None:
        exe = r"C:\Program Files\qBittorrent\qbittorrent.exe"
        values = {
            ("HKCR", r"magnet\shell\open\command", ""): r'"C:\uT\uTorrent.exe" "%1"',
            ("HKCR", ".torrent", ""): "qBittorrent.File.Torrent",
            ("HKCR", r"qBittorrent.File.Torrent\shell\open\command", ""): f'"{exe}" "%1"',
        }

        def read(hive: str, key: str, value: str) -> str | None:
            return values.get((hive, key, value))

        self.assertEqual(qbittorrent_executable(read, lambda path: True), exe)
        self.assertIsNone(qbittorrent_executable(read, lambda path: path != exe))
        self.assertIsNone(qbittorrent_executable(lambda *_: None, lambda path: True))


# -- the welcome and Settings ---------------------------------------------------------------------


class WelcomeChoiceTests(unittest.TestCase):
    def test_open_in_my_torrent_program_keeps_airgrabbers_qbittorrent(self) -> None:
        current = local_settings()
        changes = download_changes(current, DownloadMode.APP, current.download_dir, {"remote_url": ""})
        self.assertEqual(changes, {"download_mode": DownloadMode.REMOTE})

    def test_other_choices_as_before(self) -> None:
        nas = Settings(download_mode=DownloadMode.REMOTE, remote_url="https://nas:5001")
        self.assertEqual(
            download_changes(nas, DownloadMode.APP, nas.download_dir),
            {"download_mode": DownloadMode.APP},
        )
        current = local_settings()
        folder = download_changes(current, DownloadMode.FOLDER, current.download_dir)
        self.assertEqual(folder, {"download_mode": DownloadMode.FOLDER})


class SettingsDialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def dialog(self, settings: Settings) -> tuple[SettingsDialog, SettingsService]:
        settings_service = SettingsService(FakeRepository(), settings)  # type: ignore[arg-type]
        dialog = SettingsDialog(
            self.root,
            settings_service,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.5"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
        )
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        return dialog, settings_service

    async def test_says_qbittorrent_on_this_pc(self) -> None:
        dialog, _settings = self.dialog(local_settings(download_dir=self.folder))
        self.assertEqual(dialog._local_line.winfo_manager(), "grid")
        self.assertEqual(
            str(dialog._local_line.cget("text")), "qBittorrent on this PC (set up by AirGrabber)"
        )
        nas, _ = self.dialog(Settings(download_dir=self.folder, download_mode=DownloadMode.REMOTE))
        self.assertEqual(nas._local_line.winfo_manager(), "")

    async def test_a_setup_while_open_shows_and_save_keeps_it(self) -> None:
        dialog, settings = self.dialog(
            Settings(download_dir=self.folder, download_mode=DownloadMode.APP)
        )
        self.assertEqual(dialog._mode.get(), "app")
        # What the "Set up qBittorrent for AirGrabber" button stores.
        await settings.update(**airgrabber_changes(settings.current, LOGIN, str(self.folder)))
        self.assertEqual(dialog._mode.get(), "remote")
        self.assertEqual(dialog._remote_form.changes()["remote_url"], "http://127.0.0.1:8081")
        self.assertEqual(dialog._local_line.winfo_manager(), "grid")
        await dialog._store()  # Save
        self.assertTrue(set_up_by_airgrabber(settings.current))
        self.assertEqual(settings.current.remote_password.get_secret_value(), "pw-1")
        self.assertEqual(download_settings(settings.current), dialog._download_shown)


if __name__ == "__main__":
    unittest.main()
