"""The Forge test base classes (tests/forge/base.py) and Tk helpers (tests/forge/tk.py)."""

from __future__ import annotations

import os
import sys
import tkinter as tk
import types
import unittest
from unittest import mock

from app.config import DATA_DIR_ENV, AppPaths
from tests.forge import base
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase, RealContextMenuWrite
from tests.forge.tk import pump, pump_until


class ForgeTestCaseTests(ForgeTestCase):
    def test_a_temporary_data_folder_and_tmp(self) -> None:
        self.assertEqual(os.environ[DATA_DIR_ENV], str(self.data_dir))
        self.assertEqual(AppPaths.default().forge, self.data_dir / "forge")
        self.assertTrue(self.tmp.is_dir())
        self.assertNotEqual(self.tmp, self.data_dir)


class ForgeAsyncTestCaseTests(ForgeAsyncTestCase):
    async def test_a_temporary_data_folder_and_tmp(self) -> None:
        self.assertEqual(os.environ[DATA_DIR_ENV], str(self.data_dir))
        self.assertTrue(self.tmp.is_dir())


class ContextMenuGuardTests(unittest.TestCase):
    def fake_module(self) -> types.ModuleType:
        module = types.ModuleType(base.CONTEXT_MENU_MODULE)
        for name in base.REFUSED_WRITES:
            setattr(module, name, lambda: "written")
        for name in base.NOT_INSTALLED:
            setattr(module, name, lambda: "a command")
        self.enterContext(mock.patch.dict(sys.modules, {base.CONTEXT_MENU_MODULE: module}))
        self.enterContext(mock.patch.object(base, "_context_menu_exists", return_value=True))
        return module

    def test_a_no_op_while_the_module_is_missing(self) -> None:
        self.enterContext(mock.patch.object(base, "_context_menu_exists", return_value=False))
        base.guard_context_menu(self)  # nothing to patch, nothing raised

    def test_writes_refuse_and_nothing_reads_as_installed(self) -> None:
        module = self.fake_module()

        class Probe(ForgeTestCase):
            def runTest(self) -> None:
                for name in base.REFUSED_WRITES:
                    with self.assertRaises(RealContextMenuWrite):
                        getattr(module, name)()
                for name in base.NOT_INSTALLED:
                    self.assertIsNone(getattr(module, name)())

        result = unittest.TestResult()
        Probe().run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        for name in base.REFUSED_WRITES:  # restored after the test
            self.assertEqual(getattr(module, name)(), "written")

    def test_a_missing_name_fails_the_test(self) -> None:
        module = self.fake_module()
        delattr(module, base.REFUSED_WRITES[-1])

        class Probe(ForgeTestCase):
            def runTest(self) -> None:
                pass

        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(len(result.errors), 1)


class PumpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no Tk: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    async def test_pump_until(self) -> None:
        seen: list[int] = []
        self.root.after(30, lambda: seen.append(1))
        await pump_until(self.root, lambda: seen)
        self.assertEqual(seen, [1])
        with self.assertRaises(TimeoutError):
            await pump_until(self.root, lambda: False, within=0.05)
        await pump(self.root, 0.02)


if __name__ == "__main__":
    unittest.main()
