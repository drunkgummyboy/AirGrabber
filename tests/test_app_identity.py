"""AirGrabber's AppUserModelID: one constant everywhere, the process ID, and the once-per-launch
fix of an older install's Start-menu shortcut (fake PowerShell; the real one only in temporary
folders, never the real Start menu)."""

from __future__ import annotations

import asyncio
import base64
import inspect
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

import main
from app.identity import APP_USER_MODEL_ID, set_process_app_id
from app.services import app_identity
from app.services.app_identity import (
    READ_SCRIPT,
    WRITE_SCRIPT,
    Outcome,
    ensure_start_menu_app_id,
    first_argument,
    parse_read_output,
    skip_reason,
    starts_this_install,
)
from app.services.powershell import (
    HELPER_ENV,
    HELPER_TYPE,
    NO_WINDOW,
    SHORTCUT_HELPER,
    PowerShellResult,
    run_powershell,
)
from app.services.startup import shortcut_spec

PROJECT = Path(__file__).resolve().parents[1]
INSTALL = Path(r"C:\Users\Alex\AppData\Local\Programs\AirGrabber")


def _read_output(arguments: str, app_id: str) -> str:
    """READ_SCRIPT's output for a shortcut with these values."""

    def b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    return f"ARGUMENTS:{b64(arguments)}\r\nAPP_ID:{b64(app_id)}\r\n"


class ConstantTests(unittest.TestCase):
    """One AppUserModelID for the process and every shortcut AirGrabber makes."""

    def test_the_id(self) -> None:
        self.assertEqual(APP_USER_MODEL_ID, "drunkgummyboy.AirGrabber")

    def test_main_sets_it_for_the_process(self) -> None:
        self.assertIs(vars(main)["set_process_app_id"], set_process_app_id)
        default = inspect.signature(set_process_app_id).parameters["app_id"].default
        self.assertEqual(default, APP_USER_MODEL_ID)
        self.assertIn("    set_process_app_id()\n", inspect.getsource(main.main))

    def test_install_ps1(self) -> None:
        script = (PROJECT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn(f"$AppId = '{APP_USER_MODEL_ID}'", script)
        self.assertIn("$AppIdHelper = 'app\\shortcut_app_id.cs'", script)
        self.assertIn(f"[{HELPER_TYPE}]::Set($Path, $AppId)", script)
        new_shortcut = re.search(r"function New-Shortcut\b.*?\n\}", script, re.DOTALL)
        assert new_shortcut is not None
        self.assertIn("Set-ShortcutAppId $Path", new_shortcut[0])  # Start menu and desktop

    def test_startup_shortcut(self) -> None:
        spec = shortcut_spec(INSTALL, Path(r"C:\Python313\python.exe"), Path(r"C:\Startup"))
        self.assertEqual(spec.app_id, APP_USER_MODEL_ID)

    def test_start_menu_fix(self) -> None:
        self.assertIs(vars(app_identity)["APP_USER_MODEL_ID"], APP_USER_MODEL_ID)
        # (EnsureTests checks it is the value written.)

    def test_the_shared_helper(self) -> None:
        self.assertEqual(SHORTCUT_HELPER, PROJECT / "app" / "shortcut_app_id.cs")
        source = SHORTCUT_HELPER.read_text(encoding="ascii")
        namespace, _, name = HELPER_TYPE.rpartition(".")
        self.assertIn(f"namespace {namespace}", source)
        self.assertIn(f"public static class {name}", source)
        # PKEY_AppUserModel_ID
        self.assertIn('new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")', source)
        self.assertIn("AppUserModelIdProperty = 5;", source)


class MainStartsTheFixTests(unittest.TestCase):
    """main.py starts the Start-menu fix as a background task once the loop runs."""

    def _run(self, fake: object) -> None:
        loop = asyncio.new_event_loop()
        try:
            with mock.patch.object(app_identity, "ensure_start_menu_app_id", fake):
                vars(main)["_fix_start_menu_shortcut_soon"](loop)
                self.assertEqual(calls, [])  # nothing before the loop runs
                loop.run_until_complete(asyncio.sleep(0.01))
        finally:
            loop.close()

    def test_runs_in_the_background(self) -> None:
        calls.clear()

        async def fake() -> Outcome:
            calls.append("ran")
            return Outcome.DEVELOPMENT_COPY

        self._run(fake)
        self.assertEqual(calls, ["ran"])
        self.assertEqual(vars(main)["_background_tasks"], set())  # released once done

    def test_an_unexpected_failure_is_logged(self) -> None:
        calls.clear()

        async def fake() -> Outcome:
            calls.append("ran")
            raise RuntimeError("bug")

        with self.assertLogs("airgrabber", "ERROR"):
            self._run(fake)
        self.assertEqual(calls, ["ran"])


calls: list[str] = []


class ProcessIdTests(unittest.TestCase):
    def test_nothing_outside_windows(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(set_process_app_id())

    @unittest.skipUnless(sys.platform == "win32", "AppUserModelIDs are Windows-only")
    def test_windows_reports_the_id(self) -> None:
        # In a child process, so this one keeps its own identity.
        probe = (
            "import ctypes, sys\n"
            "from ctypes import wintypes\n"
            "from app.identity import set_process_app_id\n"
            "assert set_process_app_id()\n"
            "shell32 = ctypes.WinDLL('shell32')\n"
            "getter = shell32.GetCurrentProcessExplicitAppUserModelID\n"
            "getter.argtypes = (ctypes.POINTER(ctypes.c_void_p),)\n"
            "getter.restype = ctypes.c_long\n"
            "pointer = ctypes.c_void_p()\n"
            "assert getter(ctypes.byref(pointer)) == 0\n"
            "sys.stdout.write(ctypes.wstring_at(pointer.value))\n"
            "ctypes.windll.ole32.CoTaskMemFree(pointer)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, APP_USER_MODEL_ID)


class DecisionTests(unittest.TestCase):
    def test_first_argument(self) -> None:
        cases = {
            '"C:\\Air Grabber\\main.py"': "C:\\Air Grabber\\main.py",
            '"C:\\Air Grabber\\main.py" --tray': "C:\\Air Grabber\\main.py",
            '  "C:\\Air Grabber\\main.py"   --tray --more ': "C:\\Air Grabber\\main.py",
            '"C:\\Air Grabber\\main.py': "C:\\Air Grabber\\main.py",  # no closing quote
            "C:\\AirGrabber\\main.py": "C:\\AirGrabber\\main.py",  # unquoted: all of it
            "": "",
        }
        for arguments, expected in cases.items():
            with self.subTest(arguments=arguments):
                self.assertEqual(first_argument(arguments), expected)

    def test_starts_this_install(self) -> None:
        main_py = f'"{INSTALL}\\main.py"'
        self.assertTrue(starts_this_install(main_py, INSTALL))
        self.assertTrue(starts_this_install(main_py + " --tray", INSTALL))
        self.assertTrue(starts_this_install(main_py.lower(), INSTALL))  # Windows paths: any case
        self.assertFalse(starts_this_install(r'"D:\Other\AirGrabber\main.py"', INSTALL))
        self.assertFalse(starts_this_install(f'"{INSTALL}\\other.py"', INSTALL))
        self.assertFalse(starts_this_install("", INSTALL))

    def test_skip_reason(self) -> None:
        ours = f'"{INSTALL}\\main.py"'
        self.assertIsNone(skip_reason(ours, "", INSTALL))  # fix it
        self.assertIsNone(skip_reason(ours + " --tray", "  ", INSTALL))
        self.assertIs(skip_reason(ours, APP_USER_MODEL_ID, INSTALL), Outcome.HAS_APP_ID)
        self.assertIs(skip_reason(ours, "someone.Else", INSTALL), Outcome.HAS_APP_ID)
        self.assertIs(skip_reason(r'"D:\Other\main.py"', "", INSTALL), Outcome.NOT_THIS_INSTALL)

    def test_parse_read_output(self) -> None:
        arguments = '"C:\\Users\\Zoë\\AirGrabber\\main.py" --tray'
        self.assertEqual(parse_read_output(_read_output(arguments, "")), (arguments, ""))
        self.assertEqual(
            parse_read_output("noise\n" + _read_output("", APP_USER_MODEL_ID)),
            ("", APP_USER_MODEL_ID),
        )
        self.assertIsNone(parse_read_output(""))
        self.assertIsNone(parse_read_output("ARGUMENTS:\n"))  # no APP_ID line
        self.assertIsNone(parse_read_output("ARGUMENTS:%%%\nAPP_ID:\n"))


class FakeShell:
    """Stands in for PowerShell: answers READ_SCRIPT with one shortcut's values, records writes."""

    def __init__(self, arguments: str = "", app_id: str = "") -> None:
        self.read = PowerShellResult(0, _read_output(arguments, app_id), "")
        self.write = PowerShellResult(0, "", "")
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.error: BaseException | None = None

    async def __call__(self, script: str, env: Mapping[str, str]) -> PowerShellResult:
        self.calls.append((script, dict(env)))
        if self.error is not None:
            raise self.error
        if script == READ_SCRIPT:
            return self.read
        if script == WRITE_SCRIPT:
            return self.write
        raise AssertionError(f"unexpected script: {script}")

    @property
    def scripts(self) -> list[str]:
        return [script for script, _env in self.calls]


class EnsureTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.install = root / "Air Grabber"  # an installed copy: no tests folder, no .git
        self.install.mkdir()
        self.programs = root / "Programs"
        self.programs.mkdir()
        self.link = self.programs / "AirGrabber.lnk"
        self.link.write_bytes(b"lnk")  # the fake shell supplies its contents
        self.ours = f'"{self.install}\\main.py"'

    def _ensure(self, shell: FakeShell) -> Outcome:
        return asyncio.run(
            ensure_start_menu_app_id(install_dir=self.install, programs_dir=self.programs, run=shell)
        )

    def test_this_install_without_the_id_is_fixed(self) -> None:
        shell = FakeShell(self.ours)
        with self.assertLogs("app.services.app_identity", "INFO"):
            self.assertIs(self._ensure(shell), Outcome.FIXED)
        self.assertEqual(shell.scripts, [READ_SCRIPT, WRITE_SCRIPT])
        read_env, write_env = shell.calls[0][1], shell.calls[1][1]
        self.assertEqual(read_env["AIRGRABBER_APPID_LNK"], str(self.link))
        self.assertEqual(read_env[HELPER_ENV], str(SHORTCUT_HELPER))
        self.assertEqual(write_env["AIRGRABBER_APPID_LNK"], str(self.link))
        self.assertEqual(write_env["AIRGRABBER_APPID_VALUE"], APP_USER_MODEL_ID)

    def test_trailing_options_still_count_as_this_install(self) -> None:
        shell = FakeShell(self.ours + " --tray")
        with self.assertLogs("app.services.app_identity", "INFO"):
            self.assertIs(self._ensure(shell), Outcome.FIXED)

    def test_another_install_is_skipped(self) -> None:
        shell = FakeShell(r'"C:\Users\Alex\AppData\Local\Programs\AirGrabber\main.py"')
        self.assertIs(self._ensure(shell), Outcome.NOT_THIS_INSTALL)
        self.assertEqual(shell.scripts, [READ_SCRIPT])  # read, never written

    def test_an_existing_id_is_left_alone(self) -> None:
        for app_id in (APP_USER_MODEL_ID, "someone.Else"):
            with self.subTest(app_id=app_id):
                shell = FakeShell(self.ours, app_id)
                self.assertIs(self._ensure(shell), Outcome.HAS_APP_ID)
                self.assertEqual(shell.scripts, [READ_SCRIPT])

    def test_a_development_copy_is_skipped(self) -> None:
        for marker in ("tests", ".git"):
            with self.subTest(marker=marker):
                (self.install / marker).mkdir()
                shell = FakeShell(self.ours)
                self.assertIs(self._ensure(shell), Outcome.DEVELOPMENT_COPY)
                self.assertEqual(shell.calls, [])  # PowerShell never started
                (self.install / marker).rmdir()

    def test_no_start_menu_shortcut(self) -> None:
        self.link.unlink()
        shell = FakeShell(self.ours)
        self.assertIs(self._ensure(shell), Outcome.NO_SHORTCUT)
        self.assertEqual(shell.calls, [])

    def test_a_failed_read_is_logged(self) -> None:
        for result in (
            PowerShellResult(1, "", "Add-Type: csc.exe failed"),
            PowerShellResult(0, "garbage", ""),
        ):
            with self.subTest(result=result):
                shell = FakeShell(self.ours)
                shell.read = result
                with self.assertLogs("app.services.app_identity", "WARNING"):
                    self.assertIs(self._ensure(shell), Outcome.FAILED)
                self.assertEqual(shell.scripts, [READ_SCRIPT])

    def test_a_failed_write_is_logged(self) -> None:
        shell = FakeShell(self.ours)
        shell.write = PowerShellResult(1, "", "Access is denied")
        with self.assertLogs("app.services.app_identity", "WARNING") as logs:
            self.assertIs(self._ensure(shell), Outcome.FAILED)
        self.assertIn("Access is denied", logs.output[0])

    def test_powershell_that_cannot_start_or_hangs_is_logged(self) -> None:
        for error in (FileNotFoundError("powershell.exe"), TimeoutError()):
            with self.subTest(error=error):
                shell = FakeShell(self.ours)
                shell.error = error
                with self.assertLogs("app.services.app_identity", "WARNING"):
                    self.assertIs(self._ensure(shell), Outcome.FAILED)

    def test_nothing_happens_outside_windows(self) -> None:
        shell = FakeShell(self.ours)
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(app_identity.default_programs_dir())
            outcome = asyncio.run(ensure_start_menu_app_id(install_dir=self.install, run=shell))
        self.assertIs(outcome, Outcome.NOT_WINDOWS)
        self.assertEqual(shell.calls, [])


@unittest.skipUnless(sys.platform == "win32", "shortcuts are Windows-only")
class RealPowerShellTests(unittest.TestCase):
    """The real scripts and C# helper, on shortcuts in a temporary folder."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.install = root / "Air Grabber 'q'"
        self.install.mkdir()
        self.programs = root / "Programs"
        self.programs.mkdir()
        self.link = self.programs / "AirGrabber.lnk"

    def _make_shortcut(self, arguments: str) -> None:
        """A shortcut without an AppUserModelID, as install.ps1 made them before."""
        script = "; ".join(
            (
                "$ErrorActionPreference = 'Stop'",
                "$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AG_TEST_LNK)",
                "$l.TargetPath = $env:SystemRoot + '\\notepad.exe'",
                "$l.Arguments = $env:AG_TEST_ARGS",
                "$l.Save()",
            )
        )
        env = {"AG_TEST_LNK": str(self.link), "AG_TEST_ARGS": arguments}
        result = asyncio.run(run_powershell(script, env))
        self.assertEqual(result.returncode, 0, result.stderr)

    def _read(self) -> tuple[str, str] | None:
        env = {HELPER_ENV: str(SHORTCUT_HELPER), "AIRGRABBER_APPID_LNK": str(self.link)}
        result = asyncio.run(run_powershell(READ_SCRIPT, env))
        self.assertEqual(result.returncode, 0, result.stderr)
        return parse_read_output(result.stdout)

    def _ensure(self) -> Outcome:
        return asyncio.run(ensure_start_menu_app_id(install_dir=self.install, programs_dir=self.programs))

    def test_fixes_this_install_once(self) -> None:
        arguments = f'"{self.install}\\main.py"'
        self._make_shortcut(arguments)
        self.assertEqual(self._read(), (arguments, ""))
        with self.assertLogs("app.services.app_identity", "INFO"):
            self.assertIs(self._ensure(), Outcome.FIXED)
        self.assertEqual(self._read(), (arguments, APP_USER_MODEL_ID))  # Arguments kept
        self.assertIs(self._ensure(), Outcome.HAS_APP_ID)

    def test_leaves_another_install_alone(self) -> None:
        self._make_shortcut(r'"D:\Elsewhere\AirGrabber\main.py"')
        before = self.link.read_bytes()
        self.assertIs(self._ensure(), Outcome.NOT_THIS_INSTALL)
        self.assertEqual(self.link.read_bytes(), before)


class ShellOutputEncodingTests(unittest.TestCase):
    def test_read_script_output_is_ascii(self) -> None:
        # Base64 lines: no console code page can garble a path with non-ASCII letters.
        self.assertIn("[Convert]::ToBase64String", READ_SCRIPT)
        self.assertIn(f"[{HELPER_TYPE}]::Get($lnk)", READ_SCRIPT)
        self.assertIn(
            f"[{HELPER_TYPE}]::Set($env:AIRGRABBER_APPID_LNK, $env:AIRGRABBER_APPID_VALUE)", WRITE_SCRIPT
        )


if __name__ == "__main__":
    unittest.main()
