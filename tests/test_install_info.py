"""The "Installed apps" entry: created like install.ps1 does, refreshed, foreign entries left alone.

Uses a fake registry; the real one is never touched.
"""

from __future__ import annotations

import math
import re
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from app.services.install_info import (
    UNINSTALL_KEY,
    RegistryValue,
    ensure_uninstall_entry,
    entry_values,
    same_path,
)

PROJECT = Path(__file__).resolve().parents[1]


class FakeRegistry:
    """HKCU as a dict of keys; records every write."""

    def __init__(self, keys: dict[str, dict[str, RegistryValue]] | None = None) -> None:
        self.keys: dict[str, dict[str, RegistryValue]] = keys or {}
        self.writes: list[tuple[str, dict[str, RegistryValue]]] = []

    def read(self, key: str) -> dict[str, RegistryValue] | None:
        values = self.keys.get(key)
        return None if values is None else dict(values)

    def write(self, key: str, values: Mapping[str, RegistryValue]) -> None:
        self.writes.append((key, dict(values)))
        self.keys.setdefault(key, {}).update(values)


class BrokenRegistry(FakeRegistry):
    def read(self, key: str) -> dict[str, RegistryValue] | None:
        raise PermissionError("access denied")


class InstallInfoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name) / "AirGrabber"
        (self.dir / "app").mkdir(parents=True)
        (self.dir / "main.py").write_bytes(b"x" * 1000)
        (self.dir / "app" / "__init__.py").write_bytes(b"y" * 2000)
        self.root = str(self.dir)


class EnsureUninstallEntryTests(InstallInfoTestCase):
    def test_creates_the_entry_as_install_ps1_does(self) -> None:
        registry = FakeRegistry()
        with self.assertLogs("app.services.install_info", "INFO"):
            ensure_uninstall_entry(self.dir, "2.0.0", registry=registry)
        uninstall = (
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "
            f'"{self.root}\\app\\uninstall.ps1"'
        )
        self.assertEqual(
            registry.keys[UNINSTALL_KEY],
            {
                "DisplayName": "AirGrabber",
                "DisplayVersion": "2.0.0",
                "Publisher": "drunkgummyboy",
                "DisplayIcon": f"{self.root}\\app\\assets\\airgrabber.ico",
                "InstallLocation": self.root,
                "URLInfoAbout": "https://github.com/drunkgummyboy/AirGrabber",
                "UninstallString": uninstall,
                "QuietUninstallString": uninstall + " -Quiet",
                "NoModify": 1,
                "NoRepair": 1,
                "EstimatedSize": math.ceil(3000 / 1024),
            },
        )

    def test_refreshes_only_the_display_version_of_its_own_entry(self) -> None:
        own: dict[str, RegistryValue] = {
            "DisplayName": "AirGrabber",
            "DisplayVersion": "2.0.0",
            "InstallLocation": self.root.upper() + "\\",  # same folder, written differently
            "EstimatedSize": 7,
        }
        registry = FakeRegistry({UNINSTALL_KEY: dict(own)})
        with self.assertLogs("app.services.install_info", "INFO"):
            ensure_uninstall_entry(self.dir, "2.1.0", registry=registry)
        self.assertEqual(registry.keys[UNINSTALL_KEY], own | {"DisplayVersion": "2.1.0"})
        self.assertEqual(registry.writes, [(UNINSTALL_KEY, {"DisplayVersion": "2.1.0"})])

    def test_an_up_to_date_entry_is_not_written(self) -> None:
        registry = FakeRegistry()
        ensure_uninstall_entry(self.dir, "2.0.0", registry=registry)
        registry.writes.clear()
        ensure_uninstall_entry(self.dir, "2.0.0", registry=registry)
        self.assertEqual(registry.writes, [])

    def test_an_entry_for_another_folder_is_never_touched(self) -> None:
        for location in (r"C:\Users\someone\AppData\Local\Programs\AirGrabber", None):
            with self.subTest(location=location):
                foreign: dict[str, RegistryValue] = {"DisplayVersion": "2.0.0"}
                if location is not None:
                    foreign["InstallLocation"] = location
                registry = FakeRegistry({UNINSTALL_KEY: dict(foreign)})
                with self.assertLogs("app.services.install_info", "INFO"):
                    ensure_uninstall_entry(self.dir, "2.1.0", registry=registry)
                self.assertEqual(registry.keys[UNINSTALL_KEY], foreign)
                self.assertEqual(registry.writes, [])

    def test_registry_errors_are_logged_not_raised(self) -> None:
        with self.assertLogs("app.services.install_info", "WARNING"):
            ensure_uninstall_entry(self.dir, "2.0.0", registry=BrokenRegistry())

    def test_same_path(self) -> None:
        self.assertTrue(same_path(r"C:\Programs\AirGrabber", "c:\\programs\\airgrabber\\"))
        self.assertFalse(same_path(r"C:\Programs\AirGrabber", r"C:\Programs\AirGrabber2"))
        self.assertFalse(same_path("", r"C:\Programs\AirGrabber"))


class InstallerParityTests(InstallInfoTestCase):
    """install.ps1's Register-Uninstaller and ensure_uninstall_entry must stay in step."""

    def setUp(self) -> None:
        super().setUp()
        script = (PROJECT / "install.ps1").read_text(encoding="utf-8")
        match = re.search(r"function Register-Uninstaller \{(.*?)\n\}", script, re.DOTALL)
        assert match is not None
        self.function = match[1]
        self.script = script

    def _names(self, variable: str) -> set[str]:
        block = re.search(rf"\${variable} = \[ordered\]@\{{(.*?)\}}", self.function, re.DOTALL)
        assert block is not None
        return set(re.findall(r"(\w+)\s*=", block[1]))

    def test_same_value_names_and_types(self) -> None:
        values = entry_values(self.root, "2.0.0")
        strings = {name for name, value in values.items() if isinstance(value, str)}
        dwords = {name for name, value in values.items() if isinstance(value, int)}
        self.assertEqual(strings, self._names("strings"))
        self.assertEqual(dwords, self._names("dwords"))

    def test_same_key_and_command(self) -> None:
        self.assertIn("$UninstallKey = 'HKCU:\\" + UNINSTALL_KEY + "'", self.script)
        self.assertIn(
            "'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"'",
            self.function,
        )
        self.assertIn("Join-Path $root 'app\\uninstall.ps1'", self.function)
        self.assertIn("$uninstall + ' -Quiet'", self.function)
        self.assertIn("$IconPath = 'app\\assets\\airgrabber.ico'", self.script)


if __name__ == "__main__":
    unittest.main()
