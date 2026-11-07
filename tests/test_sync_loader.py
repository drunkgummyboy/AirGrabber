"""The footer's loader while Plex is read: it shows only during a check."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

import customtkinter as ctk

from app.i18n import t
from app.ui.main_window import MainWindow
from app.ui.sync_loader import PLEX_SYNC_TEXT, SyncLoader


class SyncLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ctk.CTk()
        self.addCleanup(self.root.destroy)
        self.loader = SyncLoader(self.root, t(PLEX_SYNC_TEXT))
        self.loader.grid(row=0, column=0)
        self.loader.grid_remove()

    def test_it_shows_only_while_busy(self) -> None:
        self.assertEqual(self.loader.grid_info(), {})
        self.loader.set_busy(True)
        self.assertTrue(self.loader.busy)
        self.assertNotEqual(self.loader.grid_info(), {})
        self.loader.set_busy(True)  # twice changes nothing
        self.loader.set_busy(False)
        self.assertFalse(self.loader.busy)
        self.assertEqual(self.loader.grid_info(), {})

    def test_it_can_go_while_busy(self) -> None:
        loader = SyncLoader(self.root, "x")
        loader.grid(row=1, column=0)
        loader.set_busy(True)
        loader.destroy()
        self.root.update()


class FooterTests(unittest.TestCase):
    def test_the_footer_follows_the_plex_check(self) -> None:
        busy: list[bool] = []
        window: Any = SimpleNamespace(
            _services=SimpleNamespace(plex=SimpleNamespace(checking=True)),
            _plex_loader=SimpleNamespace(set_busy=busy.append),
        )
        MainWindow._show_plex_sync(window)
        window._services.plex.checking = False
        MainWindow._show_plex_sync(window)
        self.assertEqual(busy, [True, False])
