"""MediaForge's Start-menu shortcut (app/forge/shortcut.py, D8): the pure spec,
the decisions, the once-only make with a fake PowerShell, and one real PowerShell round trip in
temporary folders (never the real Start menu)."""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

from app.forge import shortcut
from app.forge.shortcut import (
    APP_USER_MODEL_ID,
    READ_SCRIPT,
    Outcome,
    ensure_start_menu_shortcut,
    parse_read_output,
    shortcut_spec,
    skip_reason,
)
from app.identity import APP_USER_MODEL_ID as AIRGRABBER_ID
from app.services.powershell import HELPER_TYPE, LOAD_HELPER, NO_WINDOW, PowerShellResult, helper_env, powershell_exe
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase

PROJECT = Path(__file__).resolve().parents[2]
INSTALL = Path(r"C:\Users\Alex\AppData\Local\Programs\AirGrabber")
PYTHONW = Path(r"C:\Python313\pythonw.exe")
PROGRAMS = Path(r"C:\Users\Alex\AppData\Roaming\Microsoft\Windows\Start Menu\Programs")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _read_output(app: str, forge: str) -> str:
    return f"APP:{_b64(app)}\r\nFORGE:{_b64(forge)}\r\n"


class FakePowerShell:
    """Answers READ_SCRIPT with `read` and every other script with `write`; records the calls."""

    def __init__(self, read: PowerShellResult, write: PowerShellResult | None = None) -> None:
        self.read = read
        self.write = write or PowerShellResult(0, "", "")
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, script: str, env: Mapping[str, str]) -> PowerShellResult:
        self.calls.append((script, dict(env)))
        return self.read if script == READ_SCRIPT else self.write


class SpecTests(ForgeTestCase):
    def test_the_id_is_its_own(self) -> None:
        self.assertEqual(APP_USER_MODEL_ID, "drunkgummyboy.AirGrabber.MediaForge")
        self.assertNotEqual(APP_USER_MODEL_ID, AIRGRABBER_ID)

    def test_the_shortcut(self) -> None:
        spec = shortcut_spec(INSTALL, PYTHONW, PROGRAMS)
        self.assertEqual(spec.path, str(PROGRAMS / "MediaForge.lnk"))
        self.assertEqual(spec.target, str(PYTHONW))
        self.assertEqual(spec.arguments, f'"{INSTALL}\\main.py" --forge')
        self.assertEqual(spec.working_dir, str(INSTALL))
        self.assertEqual(spec.icon, f"{INSTALL}\\app\\assets\\forge\\MediaForge.ico,0")
        self.assertEqual(spec.description, "Rename and organise video files")
        self.assertEqual(spec.app_id, APP_USER_MODEL_ID)

    def test_the_icon_ships_with_the_app(self) -> None:
        self.assertTrue((PROJECT / shortcut.ICON_PATH.replace("\\", "/")).is_file())

    def test_skip_reason(self) -> None:
        ours = f'"{INSTALL}\\main.py"'
        forge = f'"{INSTALL}\\main.py" --forge'
        old = r'"C:\Users\Alex\AppData\Local\Programs\MediaForge\main.py"'
        other_copy = r'"D:\AirGrabber\main.py"'
        cases = [
            ((ours, None), None),  # make it
            ((ours, forge), Outcome.OURS),
            ((None, forge), Outcome.OURS),
            ((ours, old), Outcome.OTHER_PROGRAM),
            ((ours, ""), Outcome.OTHER_PROGRAM),
            ((None, None), Outcome.NO_APP_SHORTCUT),
            ((other_copy, None), Outcome.NO_APP_SHORTCUT),
            ((ours.upper(), None), None),  # letter case aside
        ]
        for (app, forge_args), expected in cases:
            with self.subTest(app=app, forge=forge_args):
                self.assertEqual(skip_reason(app, forge_args, INSTALL), expected)

    def test_parse_read_output(self) -> None:
        self.assertEqual(parse_read_output(_read_output("a b", "")), ("a b", ""))
        self.assertEqual(parse_read_output(_read_output("Jürgen", "x")), ("Jürgen", "x"))
        self.assertIsNone(parse_read_output("APP:" + _b64("a")))
        self.assertIsNone(parse_read_output("APP:!!\r\nFORGE:\r\n"))
        self.assertIsNone(parse_read_output(""))

    def test_install_ps1_says_the_same(self) -> None:
        text = (PROJECT / "install.ps1").read_text(encoding="ascii")
        self.assertIn(f"$ForgeAppId = '{APP_USER_MODEL_ID}'", text)
        self.assertIn(f"$ForgeIconPath = '{shortcut.ICON_PATH}'", text)
        self.assertIn(f"$link.Description = '{shortcut.DESCRIPTION}'", text)


class EnsureTests(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.install = self.tmp / "Air Grabber"  # an installed copy: no tests folder
        self.install.mkdir()
        self.programs = self.tmp / "Programs"
        self.programs.mkdir()
        self.marker = self.data_dir / "forge" / shortcut.MARKER_NAME
        self.main = f'"{self.install}\\main.py"'

    def link(self, name: str) -> None:
        (self.programs / name).write_bytes(b"lnk")  # only its being there counts: PowerShell is fake

    async def ensure(self, fake: FakePowerShell) -> Outcome:
        return await ensure_start_menu_shortcut(
            install_dir=self.install, programs_dir=self.programs, marker=self.marker, python=PYTHONW, run=fake
        )

    async def test_makes_it_once_beside_airgrabbers(self) -> None:
        self.link("AirGrabber.lnk")
        fake = FakePowerShell(PowerShellResult(0, _read_output(self.main, ""), ""))
        self.assertEqual(await self.ensure(fake), Outcome.MADE)
        self.assertEqual(len(fake.calls), 2)
        read_env = fake.calls[0][1]
        self.assertEqual(set(read_env.values()), {str(self.programs / "AirGrabber.lnk"), str(self.programs / "MediaForge.lnk")})
        script, env = fake.calls[1]
        self.assertIn(f"[{HELPER_TYPE}]::Set(", script)
        values = set(env.values())
        self.assertIn(str(self.programs / "MediaForge.lnk"), values)
        self.assertIn(f"{self.main} --forge", values)
        self.assertIn(APP_USER_MODEL_ID, values)
        self.assertTrue(self.marker.is_file())
        # The marker: never again, even after the user removed it.
        again = FakePowerShell(fake.read)
        self.assertEqual(await self.ensure(again), Outcome.DONE_BEFORE)
        self.assertEqual(again.calls, [])

    async def test_the_installers_shortcut_is_remembered(self) -> None:
        self.link("AirGrabber.lnk")
        self.link("MediaForge.lnk")
        fake = FakePowerShell(PowerShellResult(0, _read_output(self.main, f"{self.main} --forge"), ""))
        self.assertEqual(await self.ensure(fake), Outcome.OURS)
        self.assertEqual(len(fake.calls), 1)  # read only
        self.assertTrue(self.marker.is_file())

    async def test_the_old_mediaforges_shortcut_is_left_alone_and_asked_again(self) -> None:
        self.link("AirGrabber.lnk")
        self.link("MediaForge.lnk")
        old = r'"C:\Users\Alex\AppData\Local\Programs\MediaForge\main.py"'
        fake = FakePowerShell(PowerShellResult(0, _read_output(self.main, old), ""))
        self.assertEqual(await self.ensure(fake), Outcome.OTHER_PROGRAM)
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(self.marker.exists())  # made once the old one has gone

    async def test_no_airgrabber_shortcut_no_mediaforge_shortcut(self) -> None:
        fake = FakePowerShell(PowerShellResult(0, _read_output("", ""), ""))
        self.assertEqual(await self.ensure(fake), Outcome.NO_APP_SHORTCUT)
        self.assertEqual(fake.calls, [])  # nothing there: PowerShell isn't even started
        self.link("AirGrabber.lnk")
        other = FakePowerShell(PowerShellResult(0, _read_output(r'"D:\Other\main.py"', ""), ""))
        self.assertEqual(await self.ensure(other), Outcome.NO_APP_SHORTCUT)
        self.assertEqual(len(other.calls), 1)
        self.assertFalse(self.marker.exists())

    async def test_never_in_a_development_copy(self) -> None:
        self.link("AirGrabber.lnk")
        fake = FakePowerShell(PowerShellResult(0, _read_output(self.main, ""), ""))
        outcome = await ensure_start_menu_shortcut(
            install_dir=PROJECT, programs_dir=self.programs, marker=self.marker, run=fake
        )
        self.assertEqual(outcome, Outcome.DEVELOPMENT_COPY)
        self.assertEqual(fake.calls, [])

    async def test_failures_are_logged_never_raised(self) -> None:
        self.link("AirGrabber.lnk")
        cases = [
            FakePowerShell(PowerShellResult(1, "", "boom")),
            FakePowerShell(PowerShellResult(0, "garbage", "")),
            FakePowerShell(PowerShellResult(0, _read_output(self.main, ""), ""), PowerShellResult(1, "", "no")),
        ]
        for fake in cases:
            with self.subTest(calls=len(fake.calls)), self.assertLogs("app.forge.shortcut", "WARNING"):
                self.assertEqual(await self.ensure(fake), Outcome.FAILED)
        self.assertFalse(self.marker.exists())

        async def crash(script: str, env: Mapping[str, str]) -> PowerShellResult:
            raise OSError("no PowerShell")

        with self.assertLogs("app.forge.shortcut", "WARNING"):
            outcome = await ensure_start_menu_shortcut(
                install_dir=self.install, programs_dir=self.programs, marker=self.marker, run=crash
            )
        self.assertEqual(outcome, Outcome.FAILED)

    async def test_the_default_marker_is_in_forges_data_folder(self) -> None:
        self.assertEqual(shortcut.default_marker(), self.data_dir / "forge" / shortcut.MARKER_NAME)


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell is Windows-only")
class RealPowerShellTests(ForgeAsyncTestCase):
    """The real scripts, on shortcuts in a temporary folder."""

    async def test_makes_the_shortcut_with_its_id(self) -> None:
        install = self.tmp / "Air Grabber"
        install.mkdir()
        programs = self.tmp / "Programs"
        programs.mkdir()
        notepad = Path(os.environ.get("SystemRoot", r"C:\Windows"), "notepad.exe")
        await asyncio.to_thread(
            _powershell,
            "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AG_LNK); "
            "$link.TargetPath = $env:AG_TARGET; $link.Arguments = $env:AG_ARGS; $link.Save()",
            {"AG_LNK": str(programs / "AirGrabber.lnk"), "AG_TARGET": str(notepad), "AG_ARGS": f'"{install}\\main.py"'},
        )
        outcome = await ensure_start_menu_shortcut(
            install_dir=install, programs_dir=programs, marker=self.tmp / "marker", python=notepad
        )
        self.assertEqual(outcome, Outcome.MADE)
        link = programs / "MediaForge.lnk"
        output = await asyncio.to_thread(
            _powershell,
            f"$ErrorActionPreference = 'Stop'; {LOAD_HELPER}; "
            "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AG_LNK); "
            "'ARGUMENTS ' + $link.Arguments; 'TARGET ' + $link.TargetPath; 'ICON ' + $link.IconLocation; "
            f"'APP_ID ' + [{HELPER_TYPE}]::Get($env:AG_LNK)",
            {**helper_env(), "AG_LNK": str(link)},
        )
        lines = output.splitlines()
        self.assertEqual(os.path.normcase(lines.pop(1)), os.path.normcase(f"TARGET {notepad}"))
        self.assertEqual(
            lines,
            [
                f'ARGUMENTS "{install}\\main.py" --forge',
                f"ICON {install}\\app\\assets\\forge\\MediaForge.ico,0",
                f"APP_ID {APP_USER_MODEL_ID}",
            ],
        )
        # Read back through the module's own script: it is ours now.
        again = await ensure_start_menu_shortcut(install_dir=install, programs_dir=programs, marker=self.tmp / "m2")
        self.assertEqual(again, Outcome.OURS)


def _powershell(script: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        [powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=120,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0 or result.stderr.strip():
        raise AssertionError(f"PowerShell failed ({result.returncode}): {result.stderr}")
    return result.stdout


if __name__ == "__main__":
    unittest.main()
