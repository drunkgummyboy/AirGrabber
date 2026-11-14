"""Explorer's "Open with" AirGrabber player (app/services/open_with.py) on a fake registry: what
is written (a ProgID and one OpenWithProgids value per video extension, never a default), whose
it is, turned on and off, and another copy's left alone. tests/__init__.py makes the real
registry's writers raise and its reads find nothing."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from pathlib import Path

from app.services import open_with
from app.services.local_files import VIDEO_SUFFIXES
from app.services.open_with import PROG_ID, PROG_KEY, OpenWithState

MAIN_PY = Path(r"C:\Users\Alex\AppData\Local\Programs\AirGrabber\main.py")
OTHER_MAIN_PY = Path(r"D:\dev\airgrabber\main.py")
PYTHONW = Path(r"C:\Python313\pythonw.exe")
ICON = r"C:\Users\Alex\AppData\Local\Programs\AirGrabber\app\assets\airgrabber.ico"


class FakeRegistry:
    """HKEY_CURRENT_USER as a dict of keys, each a dict of string values."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}
        self.told = 0

    def read(self, key: str, name: str = "") -> str | None:
        return self.keys.get(key.casefold(), {}).get(name)

    def write(self, key: str, values: Mapping[str, str]) -> None:
        self.keys.setdefault(key.casefold(), {}).update(values)

    def delete_tree(self, key: str) -> None:
        prefix = key.casefold()
        for name in [k for k in self.keys if k == prefix or k.startswith(prefix + "\\")]:
            del self.keys[name]

    def delete_value(self, key: str, name: str) -> None:
        self.keys.get(key.casefold(), {}).pop(name, None)

    def associations_changed(self) -> None:
        self.told += 1


class OpenWithTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = FakeRegistry()

    def refresh(self, on: bool, main_py: Path = MAIN_PY) -> bool:
        return open_with.refresh(
            on,
            self.registry,
            name="AirGrabber player",
            main_py=main_py,
            python=PYTHONW,
            icon=ICON,
        )

    def command(self) -> str | None:
        return self.registry.read(PROG_KEY + r"\shell\open\command")

    def test_on_writes_the_prog_id_and_every_extension(self) -> None:
        self.assertTrue(self.refresh(True))
        self.assertEqual(self.command(), f'"{PYTHONW}" "{MAIN_PY}" --play "%1"')
        self.assertEqual(self.registry.read(PROG_KEY + r"\Application", "ApplicationName"), "AirGrabber player")
        self.assertEqual(self.registry.read(PROG_KEY + r"\DefaultIcon"), ICON)
        for suffix in VIDEO_SUFFIXES:
            with self.subTest(suffix=suffix):
                key = rf"Software\Classes\{suffix}\OpenWithProgids"
                self.assertEqual(self.registry.read(key, PROG_ID), "")
                self.assertIsNone(self.registry.read(rf"Software\Classes\{suffix}"))  # no default
        self.assertEqual(self.registry.told, 1)
        self.assertEqual(open_with.state(self.registry, MAIN_PY), OpenWithState.OURS)

    def test_written_again_only_when_something_changed(self) -> None:
        self.refresh(True)
        self.assertFalse(self.refresh(True))
        self.assertEqual(self.registry.told, 1)
        self.assertTrue(
            open_with.refresh(
                True, self.registry, name="AirGrabber-speler", main_py=MAIN_PY, python=PYTHONW, icon=ICON
            )
        )  # another language: its name again
        self.assertEqual(
            self.registry.read(PROG_KEY + r"\Application", "ApplicationName"), "AirGrabber-speler"
        )

    def test_off_removes_ours(self) -> None:
        self.refresh(True)
        self.registry.write(r"Software\Classes\.mkv\OpenWithProgids", {"VLC.mkv": ""})
        self.assertTrue(self.refresh(False))
        self.assertIsNone(self.command())
        self.assertIsNone(self.registry.read(r"Software\Classes\.mkv\OpenWithProgids", PROG_ID))
        self.assertEqual(self.registry.read(r"Software\Classes\.mkv\OpenWithProgids", "VLC.mkv"), "")
        self.assertEqual(open_with.state(self.registry, MAIN_PY), OpenWithState.OFF)
        self.assertFalse(self.refresh(False))  # nothing to remove

    def test_another_copys_is_left_alone(self) -> None:
        self.refresh(True, main_py=OTHER_MAIN_PY)
        before = {key: dict(values) for key, values in self.registry.keys.items()}
        self.assertEqual(open_with.state(self.registry, MAIN_PY), OpenWithState.OTHER)
        self.assertFalse(self.refresh(True))
        self.assertFalse(self.refresh(False))
        self.assertEqual(self.registry.keys, before)

    def test_quoted_paths_with_spaces(self) -> None:
        main_py = Path(r"C:\Users\Alex Smith\AirGrabber\main.py")
        self.refresh(True, main_py=main_py)
        self.assertEqual(self.command(), f'"{PYTHONW}" "{main_py}" --play "%1"')
        self.assertEqual(open_with.state(self.registry, main_py), OpenWithState.OURS)
        self.assertEqual(open_with.state(self.registry, Path(r"C:\Users\Alex\main.py")), OpenWithState.OTHER)


class RealRegistryTests(unittest.TestCase):
    def test_tests_cannot_touch_the_real_registry(self) -> None:
        real = open_with.WindowsRegistry()
        self.assertIsNone(real.read(PROG_KEY))  # reads find nothing
        with self.assertRaises(AssertionError):
            real.write(PROG_KEY, {"": "x"})
        with self.assertRaises(AssertionError):
            real.delete_tree(PROG_KEY)
        with self.assertRaises(AssertionError):
            real.delete_value(r"Software\Classes\.mkv\OpenWithProgids", PROG_ID)
        with self.assertRaises(AssertionError):
            real.associations_changed()


if __name__ == "__main__":
    unittest.main()
