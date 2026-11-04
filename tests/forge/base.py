"""Base classes for Forge's tests: what MediaForge's autouse pytest fixtures did.

Every test gets
- a temporary AIRGRABBER_DATA_DIR (`self.data_dir`), so nothing touches the real app data;
- `self.tmp`, a temporary folder of its own (what pytest's `tmp_path` was);
- a guard on the real Explorer right-click menu: its writers refuse and it reads as not
  installed. The guard patches CONTEXT_MENU_MODULE's REFUSED_WRITES and NOT_INSTALLED names
  (the real registry's lowest-level functions, which F-25's module keeps in step with these
  names); without that module it is a no-op. A name listed here but missing from the module
  fails the test, so a rename can't leave the menu unguarded;
- the owner's old MediaForge hidden (`guard_old_install`: no "Installed apps" entry, an empty
  %APPDATA%, not running), so the import (app/forge/migration.py) never finds the real one;
- a guard on the real Explorer (`guard_explorer`): the one function that starts it refuses, so a
  forgotten test fails instead of opening a window. Tests patch that seam with a fake.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from typing import NoReturn
from unittest import mock

from app.config import DATA_DIR_ENV

CONTEXT_MENU_MODULE = "app.forge.context_menu"
EXPLORER_MODULE = "app.forge.ui.file_browser"
# The real registry's lowest level (app/forge/context_menu.py's WindowsRegistry goes through
# these): every path to the real menu passes here, while a test's fake registry never does.
REFUSED_WRITES: tuple[str, ...] = ("_write_key", "_delete_tree")  # these raise in tests
# These return None in tests: no value, no subkey, no key (the menu reads as off).
NOT_INSTALLED: tuple[str, ...] = ("_read_value", "_first_subkey", "_key_exists")


class RealContextMenuWrite(AssertionError):
    """A test tried to change the real Explorer right-click menu."""


def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
    raise RealContextMenuWrite("tests must never change the real right-click menu: use a fake")


def _refuse_explorer(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("tests must never start the real Explorer: fake file_browser._start")


def _not_installed(*_args: object, **_kwargs: object) -> None:
    return None


def _context_menu_exists() -> bool:
    try:
        return importlib.util.find_spec(CONTEXT_MENU_MODULE) is not None
    except ModuleNotFoundError:
        return False


class _ForgeIsolation:
    """The shared set-up; mixed into a TestCase (it uses `enterContext`)."""

    tmp: Path
    data_dir: Path

    def _isolate(self, case: unittest.TestCase) -> None:
        self.tmp = _temporary_folder(case, "forge-test-")
        self.data_dir = _temporary_folder(case, "forge-data-")
        case.enterContext(mock.patch.dict(os.environ, {DATA_DIR_ENV: str(self.data_dir)}))
        guard_context_menu(case)
        guard_old_install(case, self.tmp / "no-appdata")
        guard_explorer(case)


def _temporary_folder(case: unittest.TestCase, prefix: str) -> Path:
    # ignore_cleanup_errors: a file still open on Windows (a database) must not fail the test.
    folder = tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
    return Path(case.enterContext(folder))


def guard_context_menu(case: unittest.TestCase) -> None:
    """Make the real right-click menu unwritable for the rest of `case` (a no-op until
    CONTEXT_MENU_MODULE exists)."""
    if not _context_menu_exists():
        return
    module = importlib.import_module(CONTEXT_MENU_MODULE)
    for name in REFUSED_WRITES:
        case.enterContext(mock.patch.object(module, name, _refuse))
    for name in NOT_INSTALLED:
        case.enterContext(mock.patch.object(module, name, _not_installed))


def guard_explorer(case: unittest.TestCase) -> None:
    """Refuse to start the real Explorer for the rest of `case` (app/forge/ui/file_browser.py)."""
    module = importlib.import_module(EXPLORER_MODULE)
    case.enterContext(mock.patch.object(module, "_start", _refuse_explorer))


MIGRATION_MODULE = "app.forge.migration"


def guard_old_install(case: unittest.TestCase, appdata: Path) -> None:
    """Hide the owner's real old MediaForge for the rest of `case` (F-26's import): its
    "Installed apps" entry reads as missing, %APPDATA% is `appdata` (an empty folder of the
    test's) and the old app as not running. Tests that want an old install pass fakes."""
    module = importlib.import_module(MIGRATION_MODULE)
    case.enterContext(mock.patch.object(module, "_read_uninstall_values", _not_installed))
    case.enterContext(mock.patch.object(module, "_appdata", lambda: appdata))
    case.enterContext(mock.patch.object(module, "old_app_running", lambda name=None: False))


class ForgeTestCase(_ForgeIsolation, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._isolate(self)


class ForgeAsyncTestCase(_ForgeIsolation, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._isolate(self)
