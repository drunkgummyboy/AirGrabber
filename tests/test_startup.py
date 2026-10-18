"""Start with Windows: the Startup-folder shortcut's contents and switching it on and off, in a
temporary folder (never the real Startup folder)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.identity import APP_USER_MODEL_ID
from app.services import startup
from app.services.powershell import (
    HELPER_ENV,
    HELPER_TYPE,
    LOAD_HELPER,
    NO_WINDOW,
    SHORTCUT_HELPER,
    PowerShellResult,
)
from app.services.startup import (
    SHORTCUT_NAME,
    ShortcutSpec,
    StartupError,
    is_enabled,
    set_enabled,
    shortcut_command,
    shortcut_spec,
)

INSTALL = Path(r"C:\Users\Alex\AppData\Local\Programs\AirGrabber")
PYTHON = Path(r"C:\Users\Alex\AppData\Local\Programs\Python\Python313\python.exe")
STARTUP = Path(r"C:\Users\Alex\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup")


class ShortcutSpecTests(unittest.TestCase):
    def test_spec(self) -> None:
        spec = shortcut_spec(INSTALL, PYTHON, STARTUP)
        self.assertEqual(
            spec,
            ShortcutSpec(
                path=str(STARTUP / "AirGrabber.lnk"),
                target=r"C:\Users\Alex\AppData\Local\Programs\Python\Python313\pythonw.exe",
                arguments=r'"C:\Users\Alex\AppData\Local\Programs\AirGrabber\main.py" --tray',
                working_dir=r"C:\Users\Alex\AppData\Local\Programs\AirGrabber",
                icon=r"C:\Users\Alex\AppData\Local\Programs\AirGrabber\app\assets\airgrabber.ico,0",
                description="Track your shows. Grab your movies.",
                app_id="drunkgummyboy.AirGrabber",
            ),
        )

    def test_already_running_under_pythonw(self) -> None:
        spec = shortcut_spec(INSTALL, PYTHON.with_name("pythonw.exe"), STARTUP)
        self.assertEqual(spec.target, str(PYTHON.with_name("pythonw.exe")))

    def test_paths_with_spaces_stay_one_argument(self) -> None:
        spec = shortcut_spec(Path(r"D:\My Apps\Air Grabber"), PYTHON, STARTUP)
        self.assertEqual(spec.arguments, r'"D:\My Apps\Air Grabber\main.py" --tray')


class ShortcutCommandTests(unittest.TestCase):
    """The PowerShell that writes the shortcut also gives it AirGrabber's AppUserModelID."""

    def setUp(self) -> None:
        self.spec = shortcut_spec(INSTALL, PYTHON, STARTUP)
        self.script, self.env = shortcut_command(self.spec)

    def test_every_value_travels_in_the_environment(self) -> None:
        self.assertEqual(self.env["AIRGRABBER_LNK_PATH"], self.spec.path)
        self.assertEqual(self.env["AIRGRABBER_LNK_ARGUMENTS"], self.spec.arguments)
        self.assertEqual(self.env["AIRGRABBER_LNK_APP_ID"], APP_USER_MODEL_ID)
        self.assertEqual(self.env[HELPER_ENV], str(SHORTCUT_HELPER))
        for value in self.env.values():
            self.assertNotIn(value, self.script)  # nothing is pasted into the script itself

    def test_the_app_id_step_follows_the_save(self) -> None:
        save = self.script.index("$link.Save()")
        load = self.script.index(LOAD_HELPER)
        set_id = self.script.index(
            f"[{HELPER_TYPE}]::Set($env:AIRGRABBER_LNK_PATH, $env:AIRGRABBER_LNK_APP_ID)"
        )
        self.assertLess(save, load)
        self.assertLess(load, set_id)
        # Optional: a failure is caught and reported on stderr, not fatal.
        self.assertIn("catch {", self.script[set_id:])

    def test_a_failed_app_id_step_is_logged_not_raised(self) -> None:
        async def fake_run(script: str, env: object) -> PowerShellResult:
            return PowerShellResult(0, "", startup.APP_ID_STEP_FAILED + "csc.exe is missing")

        with (
            mock.patch.object(startup, "run_powershell", fake_run),
            self.assertLogs("app.services.startup", "WARNING") as logs,
        ):
            asyncio.run(startup.write_shortcut(self.spec))
        self.assertIn("csc.exe is missing", logs.output[0])

    def test_a_failed_script_raises(self) -> None:
        async def fake_run(script: str, env: object) -> PowerShellResult:
            return PowerShellResult(1, "", "Access is denied")

        with (
            mock.patch.object(startup, "run_powershell", fake_run),
            self.assertLogs("app.services.startup", "WARNING"),
            self.assertRaises(StartupError),
        ):
            asyncio.run(startup.write_shortcut(self.spec))


class EnableTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / "Startup"
        self.folder.mkdir()
        self.python_dir = self.root / "Python"
        self.python_dir.mkdir()
        self.python = self.python_dir / "python.exe"
        self.python.write_bytes(b"")
        self.written: list[ShortcutSpec] = []

    async def _write(self, spec: ShortcutSpec) -> None:
        self.written.append(spec)
        Path(spec.path).write_bytes(b"lnk")

    def _set(self, on: bool) -> None:
        asyncio.run(
            set_enabled(
                on,
                startup_dir=self.folder,
                install_dir=INSTALL,
                python=self.python,
                write=self._write,
            )
        )

    def test_on_then_off(self) -> None:
        self.assertFalse(is_enabled(startup_dir=self.folder))
        self._set(True)
        self.assertTrue(is_enabled(startup_dir=self.folder))
        self.assertEqual(len(self.written), 1)
        self.assertEqual(self.written[0].path, str(self.folder / SHORTCUT_NAME))
        self.assertTrue(self.written[0].arguments.endswith(" --tray"))
        self._set(False)
        self.assertFalse(is_enabled(startup_dir=self.folder))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_off_when_already_off(self) -> None:
        self._set(False)
        self.assertFalse(is_enabled(startup_dir=self.folder))

    def test_on_again_rewrites_the_shortcut(self) -> None:
        self._set(True)
        self._set(True)
        self.assertEqual(len(self.written), 2)
        self.assertTrue(is_enabled(startup_dir=self.folder))

    def test_pythonw_beside_python(self) -> None:
        pythonw = self.python_dir / "pythonw.exe"
        pythonw.write_bytes(b"")
        self._set(True)
        self.assertEqual(self.written[0].target, str(pythonw))

    def test_python_when_there_is_no_pythonw(self) -> None:
        self._set(True)  # as install.ps1 does
        self.assertEqual(self.written[0].target, str(self.python))

    def test_missing_startup_folder_is_created(self) -> None:
        self.folder.rmdir()
        self._set(True)
        self.assertTrue(is_enabled(startup_dir=self.folder))

    def test_a_failed_write_leaves_it_off(self) -> None:
        async def failing(spec: ShortcutSpec) -> None:
            raise StartupError("AirGrabber could not be added to Windows startup.")

        with self.assertRaises(StartupError):
            asyncio.run(set_enabled(True, startup_dir=self.folder, python=self.python, write=failing))
        self.assertFalse(is_enabled(startup_dir=self.folder))

    def test_a_folder_in_the_way_cannot_be_removed(self) -> None:
        (self.folder / SHORTCUT_NAME).mkdir()  # unlink() fails on a directory
        with self.assertRaises(StartupError), self.assertLogs("app.services.startup", "WARNING"):
            self._set(False)

    def test_nothing_happens_outside_windows(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(startup.default_startup_dir())
            self.assertFalse(is_enabled())
            asyncio.run(set_enabled(True, write=self._write))
        self.assertEqual(self.written, [])


@unittest.skipUnless(sys.platform == "win32", "WScript.Shell is Windows-only")
class PowerShellWriterTests(unittest.TestCase):
    """The real writer, into a temporary folder: the .lnk holds every field of the spec, its
    AppUserModelID included."""

    def test_writes_a_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp, "Start up 'quoted'")  # spaces and quotes reach PowerShell intact
            folder.mkdir()
            install = Path(temp, "Air Grabber")
            spec = shortcut_spec(install, Path(sys.executable), folder)
            asyncio.run(startup.write_shortcut(spec))
            self.assertTrue(is_enabled(startup_dir=folder))
            self.assertEqual(asyncio.run(_read_shortcut(Path(spec.path))), spec)


async def _read_shortcut(path: Path) -> ShortcutSpec:
    script = "; ".join(
        (
            LOAD_HELPER,
            "$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AG_TEST_LNK)",
            "$l.TargetPath",
            "$l.Arguments",
            "$l.WorkingDirectory",
            "$l.IconLocation",
            "$l.Description",
            f"[{HELPER_TYPE}]::Get($env:AG_TEST_LNK)",
        )
    )
    process = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script,
        stdout=asyncio.subprocess.PIPE,
        env={**os.environ, "AG_TEST_LNK": str(path), HELPER_ENV: str(SHORTCUT_HELPER)},
        creationflags=NO_WINDOW,
    )  # fmt: skip
    stdout, _ = await process.communicate()
    target, arguments, working_dir, icon, description, app_id = stdout.decode().splitlines()[:6]
    return ShortcutSpec(str(path), target, arguments, working_dir, icon, description, app_id)


if __name__ == "__main__":
    unittest.main()
