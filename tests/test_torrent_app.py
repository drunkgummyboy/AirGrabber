"""Torrent program detection (fake registry) and the qBittorrent install (patched winget): the
command (winget's own source only), and why a failed install failed, from winget's exit code.
Linking an installed but unlinked qBittorrent (a dict-backed fake registry: nothing here reads or
writes the real one): where qbittorrent.exe is found, the keys written (qBittorrent's own), and
that another torrent program or Windows' own choice is never taken over."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from app.models import QBittorrentInstallResult, TorrentApp
from app.services import torrent_app
from app.services.torrent_app import (
    LinkOutcome,
    QBittorrentLinker,
    TorrentAppService,
    association_values,
    command_executable,
    find_installed_qbittorrent,
    find_torrent_app,
    friendly_name,
    install_problem,
    install_qbittorrent,
    link_after_install,
)

Result = QBittorrentInstallResult

QBIT_EXE = r"C:\Program Files\qBittorrent\qbittorrent.exe"
QBIT_COMMAND = f'"{QBIT_EXE}" "%1"'
MAGNET_CHOICE = (
    r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\magnet\UserChoice"
)


class FakeRegistry:
    """(hive, key, value) → data; records every read."""

    def __init__(self, values: dict[tuple[str, str, str], str]) -> None:
        self.values = values
        self.reads: list[tuple[str, str, str]] = []

    def __call__(self, hive: str, key: str, value: str) -> str | None:
        self.reads.append((hive, key, value))
        return self.values.get((hive, key, value))


def exists_only(*paths: str) -> mock.Mock:
    return mock.Mock(side_effect=lambda path: path in paths)


class DetectionTests(unittest.TestCase):
    def test_user_choice_prog_id_wins(self) -> None:
        registry = FakeRegistry(
            {
                ("HKCU", MAGNET_CHOICE, "ProgId"): "qBittorrent.File.Magnet",
                ("HKCR", r"qBittorrent.File.Magnet\shell\open\command", ""): QBIT_COMMAND,
                ("HKCR", r"magnet\shell\open\command", ""): r'"C:\uT\uTorrent.exe" "%1"',
            }
        )
        app = find_torrent_app(registry, exists_only(QBIT_EXE, r"C:\uT\uTorrent.exe"))
        self.assertEqual(app, TorrentApp(name="qBittorrent"))

    def test_hkcu_classes_magnet_command(self) -> None:
        registry = FakeRegistry(
            {("HKCU", r"Software\Classes\magnet\shell\open\command", ""): QBIT_COMMAND}
        )
        self.assertEqual(find_torrent_app(registry, exists_only(QBIT_EXE)), TorrentApp(name="qBittorrent"))

    def test_hkcr_magnet_fallback(self) -> None:
        exe = r"C:\Program Files\Deluge\deluge.exe"
        registry = FakeRegistry({("HKCR", r"magnet\shell\open\command", ""): f'"{exe}" "%1"'})
        self.assertEqual(find_torrent_app(registry, exists_only(exe)), TorrentApp(name="Deluge"))

    def test_torrent_file_association_fallback(self) -> None:
        exe = r"C:\Tixati\tixati.exe"
        registry = FakeRegistry(
            {
                ("HKCR", ".torrent", ""): "Tixati.torrent",
                ("HKCR", r"Tixati.torrent\shell\open\command", ""): f'"{exe}" "%1"',
            }
        )
        self.assertEqual(find_torrent_app(registry, exists_only(exe)), TorrentApp(name="Tixati"))

    def test_missing_exe_is_not_reported(self) -> None:
        registry = FakeRegistry(
            {
                ("HKCU", MAGNET_CHOICE, "ProgId"): "qBittorrent.File.Magnet",
                ("HKCR", r"qBittorrent.File.Magnet\shell\open\command", ""): QBIT_COMMAND,
                ("HKCR", ".torrent", ""): "qBittorrent.File.Torrent",
                ("HKCR", r"qBittorrent.File.Torrent\shell\open\command", ""): QBIT_COMMAND,
            }
        )
        self.assertIsNone(find_torrent_app(registry, exists_only()))

    def test_stale_choice_falls_through_to_an_installed_program(self) -> None:
        exe = r"C:\BiglyBT\BiglyBT.exe"
        registry = FakeRegistry(
            {
                ("HKCU", MAGNET_CHOICE, "ProgId"): "Gone.Magnet",
                ("HKCR", r"Gone.Magnet\shell\open\command", ""): r'"C:\Gone\gone.exe" "%1"',
                ("HKCR", r"magnet\shell\open\command", ""): f"{exe} %1",
            }
        )
        self.assertEqual(find_torrent_app(registry, exists_only(exe)), TorrentApp(name="BiglyBT"))

    def test_nothing_registered(self) -> None:
        self.assertIsNone(find_torrent_app(FakeRegistry({}), exists_only()))

    def test_non_windows_without_a_reader_finds_nothing(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(find_torrent_app())

    def test_friendly_names(self) -> None:
        cases = {
            r"C:\x\qbittorrent.exe": "qBittorrent",
            r"C:\x\uTorrent.exe": "µTorrent",
            r"C:\x\BitTorrent.exe": "BitTorrent",
            r"C:\x\deluge.exe": "Deluge",
            r"C:\x\transmission-qt.exe": "Transmission",
            r"C:\x\tixati.exe": "Tixati",
            r"C:\x\Vuze.exe": "Vuze",
            r"C:\x\Azureus.exe": "Vuze",
            r"C:\x\BiglyBT.exe": "BiglyBT",
            r"C:\x\Fragments.exe": "Fragments",
        }
        for exe, name in cases.items():
            with self.subTest(exe=exe):
                self.assertEqual(friendly_name(exe), name)

    def test_command_executable(self) -> None:
        self.assertEqual(command_executable(QBIT_COMMAND), QBIT_EXE)
        self.assertEqual(
            command_executable(r"C:\Program Files\App\app.exe /open %1"),
            r"C:\Program Files\App\app.exe",
        )
        with mock.patch.dict("os.environ", {"APPDIR": r"C:\Apps"}):
            self.assertEqual(
                command_executable(r'"%APPDIR%\tixati.exe" "%1"'), r"C:\Apps\tixati.exe"
            )


class FakeProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, None]:
        return b"winget output", None

    def kill(self) -> None:
        pass


class InstallTests(unittest.IsolatedAsyncioTestCase):
    def patch_winget(self, returncode: int | None, path: str | None = r"C:\winget.exe") -> mock.Mock:
        self.enterContext(mock.patch.object(sys, "platform", "win32"))
        self.enterContext(mock.patch.object(shutil, "which", return_value=path))
        if returncode is None:
            spawn = mock.AsyncMock(side_effect=FileNotFoundError(2, "missing"))
        else:
            spawn = mock.AsyncMock(return_value=FakeProcess(returncode))
        self.enterContext(
            mock.patch.object(asyncio, "create_subprocess_exec", spawn)
        )
        return spawn

    async def test_success_after_detection(self) -> None:
        spawn = self.patch_winget(0)
        steps: list[str] = []
        installed = await install_qbittorrent(steps.append, lambda: TorrentApp(name="qBittorrent"))
        self.assertIs(installed, Result.INSTALLED)
        self.assertTrue(installed.installed)
        args = spawn.await_args
        assert args is not None
        self.assertEqual(
            args.args,
            (
                r"C:\winget.exe",
                "install",
                "--id",
                "qBittorrent.qBittorrent",
                "--exact",
                "--source",  # winget's own catalogue only: a failing Microsoft Store source
                "winget",  # made winget ask which source to use instead of installing
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ),
        )
        self.assertEqual(
            args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        self.assertEqual(args.kwargs["stdout"], subprocess.PIPE)
        self.assertTrue(steps)

    async def test_exit_zero_but_not_detected(self) -> None:
        self.patch_winget(0)
        self.assertIs(await install_qbittorrent(None, lambda: None), Result.NOT_LINKED)

    async def test_non_zero_exit_fails_and_logs_the_code_only(self) -> None:
        self.patch_winget(-1978335212)
        find = mock.Mock(return_value=TorrentApp(name="qBittorrent"))
        with self.assertLogs("app.services.torrent_app", "INFO") as logs:
            result = await install_qbittorrent(None, find)
        self.assertIs(result, Result.FAILED)
        self.assertFalse(result.installed)
        self.assertIn("-1978335212", "\n".join(logs.output))
        find.assert_not_called()

    async def test_the_reason_comes_from_the_exit_code(self) -> None:
        # The msstore certificate failure of the 2.0.6 VM test, as Windows reports it unsigned.
        self.patch_winget(0x8A15005E)
        find = mock.Mock(return_value=TorrentApp(name="qBittorrent"))
        self.assertIs(await install_qbittorrent(None, find), Result.OFFLINE)
        find.assert_not_called()

    async def test_there_already_looks_at_the_registry(self) -> None:
        self.patch_winget(-1978335189)  # UPDATE_NOT_APPLICABLE: qBittorrent is installed
        self.assertIs(
            await install_qbittorrent(None, lambda: TorrentApp(name="qBittorrent")),
            Result.INSTALLED,
        )
        self.assertIs(
            await install_qbittorrent(None, lambda: TorrentApp(name="Deluge")), Result.NOT_LINKED
        )

    async def test_winget_missing(self) -> None:
        spawn = self.patch_winget(0, path=None)
        self.assertIs(await install_qbittorrent(), Result.NO_WINGET)
        spawn.assert_not_awaited()

    async def test_winget_cannot_start(self) -> None:
        self.patch_winget(None)
        self.assertIs(
            await install_qbittorrent(None, lambda: TorrentApp(name="qBittorrent")),
            Result.NO_WINGET,
        )

    async def test_service_wraps_find_and_install(self) -> None:
        self.patch_winget(0)
        service = TorrentAppService(lambda: TorrentApp(name="qBittorrent"))
        self.assertEqual(await service.find(), TorrentApp(name="qBittorrent"))
        self.assertIs(await service.install_qbittorrent(), Result.INSTALLED)

    async def test_cancellation_kills_winget(self) -> None:
        gate = asyncio.Event()

        class SlowProcess(FakeProcess):
            killed = False

            async def communicate(self) -> tuple[bytes, None]:
                await gate.wait()
                return b"", None

            def kill(self) -> None:
                SlowProcess.killed = True

        self.enterContext(mock.patch.object(sys, "platform", "win32"))
        self.enterContext(mock.patch.object(shutil, "which", return_value="winget"))
        self.enterContext(
            mock.patch.object(
                asyncio,
                "create_subprocess_exec",
                mock.AsyncMock(return_value=SlowProcess(0)),
            )
        )
        task = asyncio.create_task(install_qbittorrent())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(SlowProcess.killed)


class InstallProblemTests(unittest.TestCase):
    def test_exit_codes(self) -> None:
        cases = {
            0x8A150008: Result.OFFLINE,  # DOWNLOAD_FAILED
            -1978335224: Result.OFFLINE,  # the same, signed
            0x8A15004B: Result.OFFLINE,  # FAILED_TO_OPEN_ALL_SOURCES
            0x80072EE7: Result.OFFLINE,  # name not resolved
            0x8A150107: Result.OFFLINE,  # INSTALL_NO_NETWORK
            0x8A15010C: Result.DECLINED,  # INSTALL_CANCELLED_BY_USER
            0x800704C7: Result.DECLINED,  # ERROR_CANCELLED
            0x8A150102: Result.BUSY,  # INSTALL_IN_PROGRESS
            1618: Result.BUSY,  # ERROR_INSTALL_ALREADY_RUNNING
            0x8A150105: Result.NO_SPACE,
            0x8A15010F: Result.BLOCKED,
            0x8A15003A: Result.BLOCKED,
            0x8A15010A: Result.RESTART,
            0x8A150014: Result.FAILED,  # NO_APPLICATIONS_FOUND
            0x8A150016: Result.FAILED,  # MULTIPLE_APPLICATIONS_FOUND
            1: Result.FAILED,
        }
        for code, problem in cases.items():
            with self.subTest(code=hex(code & 0xFFFFFFFF)):
                self.assertIs(install_problem(code), problem)

    def test_success_and_there_already_need_a_look(self) -> None:
        for code in (0, 0x8A15002B, -1978335189, 0x8A150061, 0x8A15010D, 0x8A15010E):
            self.assertIsNone(install_problem(code))



# -- linking an installed qBittorrent -------------------------------------------------------------

TORRENT_CHOICE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.torrent\UserChoice"
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\qBittorrent"
APP_PATHS = r"Software\Microsoft\Windows\CurrentVersion\App Paths\qbittorrent.exe"
QBIT_FOLDER = r"C:\Program Files\qBittorrent"
UTORRENT_EXE = r"C:\uT\uTorrent.exe"
UTORRENT_COMMAND = f'"{UTORRENT_EXE}" "%1"'

# What qBittorrent 5's NSIS installer (dist/windows/installer.nsh, release-5.3.0) writes: a
# 32-bit installer without SetRegView, so its Software keys land in the 32-bit view
# (WOW6432Node); Software\Classes is shared, so its ProgIds show in HKCR. No magnet / .torrent
# handler: Windows waits for the user to pick it in Default apps.
QBIT5_INSTALL: dict[tuple[str, str, str], str] = {
    ("HKLM32", r"SOFTWARE\qBittorrent", "InstallLocation"): QBIT_FOLDER,
    ("HKLM32", UNINSTALL, "DisplayIcon"): f'"{QBIT_EXE}",0',
    ("HKLM32", UNINSTALL, "UninstallString"): rf'"{QBIT_FOLDER}\uninst.exe"',
    ("HKCR", r"qBittorrent.File.Torrent\shell\open\command", ""): QBIT_COMMAND,
    ("HKCR", r"qBittorrent.Url.Magnet\shell\open\command", ""): QBIT_COMMAND,
    ("HKCR", "magnet", "URL Protocol"): "",
}

# qBittorrent's own per-user association (Utils::OS::setMagnetLinkAssoc / setTorrentFileAssoc,
# release-4.6.7, src/base/utils/os.cpp), under HKCU\Software\Classes.
MAGNET_KEYS = [
    ("magnet", "", "URL:Magnet link"),
    ("magnet", "Content Type", "application/x-magnet"),
    (r"magnet\DefaultIcon", "", f'"{QBIT_EXE}",1'),
    (r"magnet\shell", "", "open"),
    (r"magnet\shell\open\command", "", f'"{QBIT_EXE}" "%1"'),
    ("magnet", "URL Protocol", ""),
]
TORRENT_KEYS = [
    (".torrent", "", "qBittorrent.File.Torrent"),
    (".torrent", "Content Type", "application/x-bittorrent"),
]

Values = dict[tuple[str, str, str], str]


class FakeWindows:
    """A dict-backed registry, case-insensitive like Windows': (hive, key, value) → data. HKCR
    reads see HKCU\\Software\\Classes first, as Windows merges them; writes (REG_SZ) land under
    HKCU\\Software\\Classes and are recorded; so are shell notifications."""

    def __init__(self, values: Values | None = None) -> None:
        self.values = {self._key(*k): v for k, v in (values or {}).items()}
        self.writes: list[tuple[str, str, str]] = []
        self.notified = 0
        self.fail_writes = False

    @staticmethod
    def _key(hive: str, key: str, value: str) -> tuple[str, str, str]:
        return hive, key.lower(), value.lower()

    def add(self, values: Values) -> None:
        self.values.update({self._key(*k): v for k, v in values.items()})

    def read(self, hive: str, key: str, value: str) -> str | None:
        if hive == "HKCR":
            user = self.values.get(self._key("HKCU", rf"Software\Classes\{key}", value))
            if user is not None:
                return user
        return self.values.get(self._key(hive, key, value))

    def write(self, key: str, value: str, data: str) -> None:
        if self.fail_writes:
            raise PermissionError(5, "Access is denied")
        self.writes.append((key, value, data))
        self.values[self._key("HKCU", rf"Software\Classes\{key}", value)] = data

    def notify(self) -> None:
        self.notified += 1

    def linker(self, *exes: str, environ: dict[str, str] | None = None) -> QBittorrentLinker:
        return QBittorrentLinker(
            self.read, self.write, exists_only(*exes), self.notify, environ or {}
        )


class FindInstalledTests(unittest.TestCase):
    def find(self, values: Values, *exes: str, **env: str) -> str | None:
        return find_installed_qbittorrent(FakeWindows(values).read, exists_only(*exes), env)

    def test_each_place_qbittorrent_is_found(self) -> None:
        cases: dict[str, Values] = {
            "install key, 64-bit view": {
                ("HKLM", r"SOFTWARE\qBittorrent", "InstallLocation"): QBIT_FOLDER
            },
            "install key, 32-bit view": {
                ("HKLM32", r"SOFTWARE\qBittorrent", "InstallLocation"): QBIT_FOLDER
            },
            "App Paths": {("HKCU", APP_PATHS, ""): QBIT_EXE},
            "uninstall icon": {("HKLM", UNINSTALL, "DisplayIcon"): f'"{QBIT_EXE}",0'},
            "uninstall icon, unquoted": {("HKLM32", UNINSTALL, "DisplayIcon"): f"{QBIT_EXE},0"},
            "uninstaller": {("HKCU", UNINSTALL, "UninstallString"): rf'"{QBIT_FOLDER}\uninst.exe"'},
            "5.x ProgId": {
                ("HKCR", r"qBittorrent.File.Torrent\shell\open\command", ""): QBIT_COMMAND
            },
            "4.x ProgId": {("HKCR", r"qBittorrent\shell\open\command", ""): QBIT_COMMAND},
        }
        for name, values in cases.items():
            with self.subTest(name):
                self.assertEqual(self.find(values, QBIT_EXE), QBIT_EXE)

    def test_program_folders(self) -> None:
        self.assertEqual(self.find({}, QBIT_EXE, ProgramFiles=r"C:\Program Files"), QBIT_EXE)
        x86 = r"C:\Program Files (x86)\qBittorrent\qbittorrent.exe"
        env = {"ProgramFiles(x86)": r"C:\Program Files (x86)"}
        self.assertEqual(self.find({}, x86, **env), x86)
        user = r"C:\Users\u\AppData\Local\Programs\qBittorrent\qbittorrent.exe"
        self.assertEqual(self.find({}, user, LOCALAPPDATA=r"C:\Users\u\AppData\Local"), user)

    def test_missing_files_and_other_programs_are_not_qbittorrent(self) -> None:
        self.assertIsNone(self.find(QBIT5_INSTALL))  # the registry says so, the file is gone
        self.assertIsNone(self.find({("HKCU", APP_PATHS, ""): UTORRENT_EXE}, UTORRENT_EXE))
        self.assertIsNone(self.find({}))


class AssociationValuesTests(unittest.TestCase):
    def test_with_the_installers_torrent_prog_id(self) -> None:
        self.assertEqual(
            association_values(QBIT_EXE, "qBittorrent.File.Torrent"), [*MAGNET_KEYS, *TORRENT_KEYS]
        )

    def test_qbittorrent_4s_prog_id_and_the_earlier_one_kept(self) -> None:
        self.assertEqual(
            association_values(QBIT_EXE, "qBittorrent", "Gone.torrent"),
            [
                *MAGNET_KEYS,
                # qBittorrent 4's installer's ProgId (dist/windows/installer.nsi), per user.
                ("qBittorrent", "", "qBittorrent Torrent File"),
                ("qBittorrent", "FriendlyTypeName", "qBittorrent Torrent File"),
                (r"qBittorrent\shell", "", "open"),
                (r"qBittorrent\shell\open\command", "", f'"{QBIT_EXE}" "%1"'),
                (r"qBittorrent\DefaultIcon", "", f'"{QBIT_EXE}",1'),
                (r".torrent\OpenWithProgids", "Gone.torrent", ""),
                (".torrent", "", "qBittorrent"),
                (".torrent", "Content Type", "application/x-bittorrent"),
            ],
        )
        # qBittorrent's own ProgIds aren't kept as "earlier".
        values = association_values(QBIT_EXE, "qBittorrent.File.Torrent", "qBittorrent")
        self.assertNotIn(r".torrent\OpenWithProgids", [key for key, _, _ in values])

    def test_only_user_classes_are_written(self) -> None:
        for key, _, _ in association_values(QBIT_EXE, "qBittorrent", "Old"):
            self.assertNotIn("userchoice", key.lower())
            self.assertFalse(key.lower().startswith(("software", "hkey")))


class LinkTests(unittest.TestCase):
    def test_installed_and_unlinked_gets_linked(self) -> None:
        windows = FakeWindows(QBIT5_INSTALL)
        linker = windows.linker(QBIT_EXE)
        self.assertIsNone(linker.find())  # the 2.0.7 VM: installed, nothing opens torrents
        with self.assertLogs("app.services.torrent_app", "INFO") as logs:
            self.assertIs(linker.link_if_unlinked(), LinkOutcome.LINKED)
        self.assertEqual(windows.writes, [*MAGNET_KEYS, *TORRENT_KEYS])
        self.assertEqual(windows.notified, 1)
        self.assertEqual(linker.find(), TorrentApp(name="qBittorrent"))
        output = "\n".join(logs.output)
        self.assertIn("qBittorrent linked for magnet links and .torrent files", output)
        self.assertNotIn("Program Files", output)  # never a path
        # Once linked, nothing more is written.
        self.assertIs(linker.link_if_unlinked(), LinkOutcome.ALREADY)
        self.assertEqual(len(windows.writes), len(MAGNET_KEYS) + len(TORRENT_KEYS))

    def test_qbittorrent_4_without_the_new_prog_id(self) -> None:
        windows = FakeWindows(
            {("HKLM32", r"SOFTWARE\qBittorrent", "InstallLocation"): QBIT_FOLDER}
        )
        self.assertIs(windows.linker(QBIT_EXE).link_if_unlinked(), LinkOutcome.LINKED)
        self.assertIn((".torrent", "", "qBittorrent"), windows.writes)
        self.assertEqual(
            windows.read("HKCR", r"qBittorrent\shell\open\command", ""), QBIT_COMMAND
        )

    def test_a_handler_whose_program_is_gone_is_replaced(self) -> None:
        windows = FakeWindows(
            {
                **QBIT5_INSTALL,
                ("HKCR", r"magnet\shell\open\command", ""): r'"C:\Gone\gone.exe" "%1"',
                ("HKCU", r"Software\Classes\.torrent", ""): "Gone.torrent",
            }
        )
        self.assertIs(windows.linker(QBIT_EXE).link_if_unlinked(), LinkOutcome.LINKED)
        self.assertIn((r".torrent\OpenWithProgids", "Gone.torrent", ""), windows.writes)

    def test_another_program_is_never_taken_over(self) -> None:
        cases: dict[str, Values] = {
            "magnet handler": {("HKCR", r"magnet\shell\open\command", ""): UTORRENT_COMMAND},
            ".torrent handler": {
                ("HKCR", ".torrent", ""): "uTorrent",
                ("HKCR", r"uTorrent\shell\open\command", ""): UTORRENT_COMMAND,
            },
            "UserChoice": {
                ("HKCU", TORRENT_CHOICE, "ProgId"): "uTorrent",
                ("HKCR", r"uTorrent\shell\open\command", ""): UTORRENT_COMMAND,
            },
        }
        for name, values in cases.items():
            with self.subTest(name):
                windows = FakeWindows({**QBIT5_INSTALL, **values})
                linker = windows.linker(QBIT_EXE, UTORRENT_EXE)
                self.assertIs(linker.link_if_unlinked(), LinkOutcome.OTHER)
                self.assertEqual((windows.writes, windows.notified), ([], 0))

    def test_windows_own_choice_is_never_overridden(self) -> None:
        # A store app's ProgId (no open command) or a removed program's: only Default apps can
        # change what Windows uses, so nothing is written.
        gone = r'"C:\Gone\g.exe" "%1"'
        cases: dict[str, Values] = {
            "AppX4hxtad77fbk3jkkeerkrm0ze94wjf3s9": {},
            "Gone.Magnet": {("HKCR", r"Gone.Magnet\shell\open\command", ""): gone},
        }
        for prog_id, values in cases.items():
            with self.subTest(prog_id):
                windows = FakeWindows(
                    {**QBIT5_INSTALL, **values, ("HKCU", MAGNET_CHOICE, "ProgId"): prog_id}
                )
                linker = windows.linker(QBIT_EXE)
                self.assertIs(linker.link_if_unlinked(), LinkOutcome.USER_CHOICE)
                self.assertEqual(windows.writes, [])

    def test_windows_own_choice_of_qbittorrent_counts_as_linked(self) -> None:
        # Picked in Default apps: UserChoice names the installer's ProgId.
        windows = FakeWindows(
            {**QBIT5_INSTALL, ("HKCU", MAGNET_CHOICE, "ProgId"): "qBittorrent.Url.Magnet"}
        )
        linker = windows.linker(QBIT_EXE)
        self.assertEqual(linker.find(), TorrentApp(name="qBittorrent"))
        self.assertIs(linker.link_if_unlinked(), LinkOutcome.ALREADY)
        self.assertEqual(windows.writes, [])

    def test_not_installed(self) -> None:
        windows = FakeWindows({})
        self.assertIs(windows.linker().link_if_unlinked(), LinkOutcome.NOT_INSTALLED)
        self.assertEqual(windows.writes, [])

    def test_a_failed_write(self) -> None:
        windows = FakeWindows(QBIT5_INSTALL)
        windows.fail_writes = True
        with self.assertLogs("app.services.torrent_app", "INFO") as logs:
            self.assertIs(windows.linker(QBIT_EXE).link_if_unlinked(), LinkOutcome.FAILED)
        self.assertIn("PermissionError", "\n".join(logs.output))
        self.assertEqual(windows.notified, 0)

    def test_not_on_windows(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            linker = QBittorrentLinker(environ={"ProgramFiles": r"C:\Program Files"})
            self.assertIsNone(linker.installed_executable())
            self.assertIs(linker.link_if_unlinked(), LinkOutcome.NOT_INSTALLED)

    def test_after_install_never_raises(self) -> None:
        windows = FakeWindows(QBIT5_INSTALL)
        self.assertIs(link_after_install(windows.linker(QBIT_EXE)), LinkOutcome.LINKED)
        broken = mock.Mock(spec=QBittorrentLinker)
        broken.link_if_unlinked.side_effect = RuntimeError("boom")
        self.assertIs(link_after_install(broken), LinkOutcome.FAILED)


class LinkingInstallTests(unittest.IsolatedAsyncioTestCase):
    def patch_winget(self, returncode: int = 0) -> mock.AsyncMock:
        self.enterContext(mock.patch.object(sys, "platform", "win32"))
        self.enterContext(mock.patch.object(shutil, "which", return_value=r"C:\winget.exe"))
        spawn = mock.AsyncMock(return_value=FakeProcess(returncode))
        self.enterContext(mock.patch.object(asyncio, "create_subprocess_exec", spawn))
        return spawn

    async def test_try_again_links_without_winget(self) -> None:
        spawn = self.patch_winget()
        windows = FakeWindows(QBIT5_INSTALL)
        linker = windows.linker(QBIT_EXE)
        self.assertIs(await install_qbittorrent(None, linker.find, linker), Result.INSTALLED)
        spawn.assert_not_awaited()  # no reinstall, no permission prompt
        self.assertEqual(windows.writes, [*MAGNET_KEYS, *TORRENT_KEYS])

    async def test_try_again_when_windows_own_choice_points_elsewhere(self) -> None:
        spawn = self.patch_winget()
        windows = FakeWindows({**QBIT5_INSTALL, ("HKCU", MAGNET_CHOICE, "ProgId"): "AppXabc"})
        linker = windows.linker(QBIT_EXE)
        self.assertIs(await install_qbittorrent(None, linker.find, linker), Result.NOT_LINKED)
        spawn.assert_not_awaited()
        self.assertEqual(windows.writes, [])

    async def test_a_failed_link_is_not_linked(self) -> None:
        self.patch_winget()
        windows = FakeWindows(QBIT5_INSTALL)
        windows.fail_writes = True
        linker = windows.linker(QBIT_EXE)
        self.assertIs(await install_qbittorrent(None, linker.find, linker), Result.NOT_LINKED)

    async def test_winget_installs_then_it_is_linked(self) -> None:
        # The 2.0.7 VM: winget succeeded, but nothing opened torrents with qBittorrent.
        windows = FakeWindows({})
        present: set[str] = set()

        async def winget(*args: object, **kwargs: object) -> FakeProcess:
            windows.add(QBIT5_INSTALL)
            present.add(QBIT_EXE)
            return FakeProcess(0)

        self.patch_winget()
        spawn = mock.AsyncMock(side_effect=winget)
        self.enterContext(mock.patch.object(asyncio, "create_subprocess_exec", spawn))
        linker = QBittorrentLinker(
            windows.read, windows.write, lambda path: path in present, windows.notify, {}
        )
        self.assertIs(await install_qbittorrent(None, linker.find, linker), Result.INSTALLED)
        spawn.assert_awaited_once()
        self.assertEqual(windows.writes, [*MAGNET_KEYS, *TORRENT_KEYS])

    async def test_another_program_after_winget_stays(self) -> None:
        self.patch_winget(-1978335189)  # there already
        windows = FakeWindows({("HKCR", r"magnet\shell\open\command", ""): UTORRENT_COMMAND})
        linker = windows.linker(UTORRENT_EXE)  # qbittorrent.exe isn't there
        self.assertIs(await install_qbittorrent(None, linker.find, linker), Result.NOT_LINKED)
        self.assertEqual(windows.writes, [])

    async def test_the_service_links_when_it_looks(self) -> None:
        windows = FakeWindows(QBIT5_INSTALL)
        linker = windows.linker(QBIT_EXE)
        service = TorrentAppService(linker.find, linker=linker)
        self.assertEqual(await service.find(), TorrentApp(name="qBittorrent"))
        self.assertEqual(windows.notified, 1)

    async def test_the_service_without_a_linker_never_writes(self) -> None:
        windows = FakeWindows(QBIT5_INSTALL)
        linker = windows.linker(QBIT_EXE)
        self.assertIsNone(await TorrentAppService(linker.find).find())
        self.assertEqual(windows.writes, [])


class SetupQBittorrentEntryTests(unittest.TestCase):
    def test_setup_qbittorrent_links_too(self) -> None:
        import main
        from app.config import AppPaths
        from app.services import qbittorrent_setup

        with (
            mock.patch.object(main, "ensure_requirements", return_value=True),
            mock.patch.object(torrent_app, "link_after_install") as link,
            mock.patch.object(qbittorrent_setup, "run_setup", return_value=0) as run_setup,
        ):
            self.assertEqual(main._setup_qbittorrent(AppPaths(Path("unused"))), 0)
        link.assert_called_once_with()
        run_setup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
