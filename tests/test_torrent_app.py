"""Torrent program detection (fake registry) and the qBittorrent install (patched winget)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from app.models import TorrentApp
from app.services.torrent_app import (
    TorrentAppService,
    command_executable,
    find_torrent_app,
    friendly_name,
    install_qbittorrent,
)

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
        self.assertTrue(installed)
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
        self.assertFalse(await install_qbittorrent(None, lambda: None))

    async def test_non_zero_exit_fails_and_logs_the_code_only(self) -> None:
        self.patch_winget(-1978335212)
        find = mock.Mock(return_value=TorrentApp(name="qBittorrent"))
        with self.assertLogs("app.services.torrent_app", "INFO") as logs:
            self.assertFalse(await install_qbittorrent(None, find))
        self.assertIn("-1978335212", "\n".join(logs.output))
        find.assert_not_called()

    async def test_winget_missing(self) -> None:
        spawn = self.patch_winget(0, path=None)
        self.assertFalse(await install_qbittorrent())
        spawn.assert_not_awaited()

    async def test_winget_cannot_start(self) -> None:
        self.patch_winget(None)
        self.assertFalse(await install_qbittorrent(None, lambda: TorrentApp(name="qBittorrent")))

    async def test_service_wraps_find_and_install(self) -> None:
        self.patch_winget(0)
        service = TorrentAppService(lambda: TorrentApp(name="qBittorrent"))
        self.assertEqual(await service.find(), TorrentApp(name="qBittorrent"))
        self.assertTrue(await service.install_qbittorrent())

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


if __name__ == "__main__":
    unittest.main()
