"""Settings → Advanced: the Prowlarr panel's status lines, the built-in sites + "Use Prowlarr"
boxes as stored in disabled_indexers, and the panel inside the dialog with a fake Prowlarr
service (no network, never winget)."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models import ProwlarrState, ProwlarrStatus, Settings
from app.services import SettingsService
from app.ui import theme
from app.ui.prowlarr_panel import (
    CHECKING,
    INSTALL_FAILED,
    ProwlarrLook,
    host_port,
    prowlarr_look,
    shows_remote_fields,
    site_count,
)
from app.ui.settings_dialog import SettingsDialog, disabled_indexers

BASE = "http://127.0.0.1:9696"
KEY = "0123456789abcdef0123456789abcdef"
SITES = {"apibay": True, "eztv": True, "bitsearch": True, "yts": True}


def status(state: ProwlarrState, *, sites: int | None = None, address: str = "") -> ProwlarrStatus:
    return ProwlarrStatus(state=state, sites=sites, address=address)


class ProwlarrLookTests(unittest.TestCase):
    def test_connected_on_this_pc(self) -> None:
        self.assertEqual(
            prowlarr_look(status(ProwlarrState.CONNECTED_LOCAL, sites=5, address=BASE)),
            ProwlarrLook(
                "✓ Connected to Prowlarr on this PC · 5 sites", theme.SUCCESS, open_button=True
            ),
        )
        one = prowlarr_look(status(ProwlarrState.CONNECTED_LOCAL, sites=1, address=BASE))
        self.assertEqual(one.text, "✓ Connected to Prowlarr on this PC · 1 site")
        unknown = prowlarr_look(status(ProwlarrState.CONNECTED_LOCAL, address=BASE))
        self.assertEqual(unknown.text, "✓ Connected to Prowlarr on this PC")

    def test_connected_elsewhere(self) -> None:
        self.assertEqual(
            prowlarr_look(
                status(ProwlarrState.CONNECTED_REMOTE, address="http://nas.local:9696/prowlarr")
            ),
            ProwlarrLook(
                "✓ Connected to Prowlarr at nas.local:9696", theme.SUCCESS, open_button=True
            ),
        )

    def test_not_running(self) -> None:
        self.assertEqual(
            prowlarr_look(status(ProwlarrState.NOT_RUNNING, address=BASE)),
            ProwlarrLook(
                "Prowlarr is installed but isn't answering. "
                "Restart your PC, or open it from the Start menu.",
                theme.WARNING,
                open_button=True,
            ),
        )

    def test_not_installed(self) -> None:
        self.assertEqual(
            prowlarr_look(status(ProwlarrState.NOT_INSTALLED)),
            ProwlarrLook("Prowlarr isn't installed", theme.WARNING, install_button=True),
        )

    def test_not_connected(self) -> None:
        self.assertEqual(
            prowlarr_look(status(ProwlarrState.NOT_CONNECTED, address=BASE)),
            ProwlarrLook("Not connected", theme.WARNING, open_button=True),
        )
        self.assertFalse(prowlarr_look(status(ProwlarrState.NOT_CONNECTED)).open_button)

    def test_only_not_installed_offers_the_install(self) -> None:
        for state in ProwlarrState:
            with self.subTest(state=state):
                look = prowlarr_look(status(state, address=BASE))
                self.assertEqual(look.install_button, state is ProwlarrState.NOT_INSTALLED)
                self.assertFalse(look.get_link)

    def test_transient_looks(self) -> None:
        self.assertEqual(CHECKING, ProwlarrLook("Checking…", theme.TEXT_MUTED))
        self.assertEqual(
            INSTALL_FAILED,
            ProwlarrLook("Couldn't install it automatically.", theme.DANGER, get_link=True),
        )


class HelperTests(unittest.TestCase):
    def test_site_count(self) -> None:
        self.assertEqual(
            [site_count(n) for n in (0, 1, 2, 12)], ["0 sites", "1 site", "2 sites", "12 sites"]
        )

    def test_host_port_never_shows_credentials(self) -> None:
        cases = {
            "http://nas.local:9696": "nas.local:9696",
            "https://user:secret@nas.local:9696/prowlarr": "nas.local:9696",
            "http://[fd00::5]:9696": "[fd00::5]:9696",
            "http://nas.local": "nas.local",
            "": "",
        }
        for url, shown in cases.items():
            with self.subTest(url=url):
                self.assertEqual(host_port(url), shown)

    def test_remote_fields_start_open_for_another_computer_only(self) -> None:
        self.assertTrue(shows_remote_fields("http://nas.local:9696"))
        self.assertTrue(shows_remote_fields("http://192.168.1.20:9696"))
        for url in ("", "  ", BASE, "http://localhost:9696", "http://[::1]:9696"):
            with self.subTest(url=url):
                self.assertFalse(shows_remote_fields(url))


class DisabledIndexersTests(unittest.TestCase):
    def test_everything_on(self) -> None:
        self.assertEqual(disabled_indexers(SITES, use_prowlarr=True), ())

    def test_unticked_sites_in_search_order_then_prowlarr(self) -> None:
        sites = {**SITES, "yts": False, "apibay": False}
        self.assertEqual(disabled_indexers(sites, use_prowlarr=True), ("apibay", "yts"))
        self.assertEqual(
            disabled_indexers(sites, use_prowlarr=False), ("apibay", "yts", "prowlarr")
        )
        self.assertEqual(disabled_indexers(SITES, use_prowlarr=False), ("prowlarr",))

    def test_unknown_ids_are_kept(self) -> None:
        self.assertEqual(
            disabled_indexers({**SITES, "eztv": False}, use_prowlarr=False, unknown=("oldsite",)),
            ("eztv", "prowlarr", "oldsite"),
        )

    def test_round_trip_through_settings(self) -> None:
        stored = disabled_indexers({**SITES, "bitsearch": False}, use_prowlarr=False)
        self.assertEqual(Settings(disabled_indexers=stored).disabled_indexers, stored)


# --- the panel in the dialog ------------------------------------------------------------------


class FakeProwlarr:
    """ProwlarrSetupService for the panel: answers `answer`; `install` runs `on_install`."""

    def __init__(self, answer: ProwlarrStatus) -> None:
        self.answer = answer
        self.reads = 0
        self.installed = True
        self.on_install: Callable[[], Any] | None = None
        self.progress_seen: list[str] = []

    async def status(self) -> ProwlarrStatus:
        self.reads += 1
        return self.answer

    async def install(self, progress: Callable[[str], None] | None = None) -> bool:
        for text in ("Installing… Windows may ask for permission", "Setting up Prowlarr…"):
            if progress is not None:
                progress(text)
            self.progress_seen.append(text)
            await asyncio.sleep(0)
        if self.on_install is not None:
            await self.on_install()
        return self.installed


class FakeSettingsRepository:
    async def save(self, settings: Settings) -> None:
        pass


class FakeShortcut:
    def is_enabled(self) -> bool:
        return False

    async def set_enabled(self, on: bool) -> None:
        raise AssertionError("not changed here")


class ProwlarrPanelDialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # collect the dialog's Tk variables on the main thread

    @staticmethod
    def _close(dialog: SettingsDialog) -> None:
        if dialog.winfo_exists():
            dialog.destroy()

    async def dialog(
        self, prowlarr: FakeProwlarr, **values: Any
    ) -> tuple[SettingsDialog, SettingsService]:
        settings = SettingsService(
            FakeSettingsRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder, **values),
        )
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.0"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(),
            discovery=mock.MagicMock(),
            prowlarr=prowlarr,  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
        )
        self.addCleanup(self._close, dialog)
        task = dialog._prowlarr_panel._status_task
        assert task is not None
        await task
        return dialog, settings

    async def test_shows_the_status(self) -> None:
        answer = status(ProwlarrState.CONNECTED_LOCAL, sites=5, address=BASE)
        dialog, _settings = await self.dialog(FakeProwlarr(answer), prowlarr_url=BASE)
        panel = dialog._prowlarr_panel
        self.assertEqual(
            panel._label.cget("text"), "✓ Connected to Prowlarr on this PC · 5 sites"
        )
        self.assertTrue(panel._open_button.winfo_manager())
        self.assertFalse(panel._install_row.winfo_manager())
        self.assertFalse(panel.expanded)  # a Prowlarr on this PC: the fields stay folded

    async def test_fields_start_open_for_another_computer(self) -> None:
        answer = status(ProwlarrState.CONNECTED_REMOTE, address="http://nas.local:9696")
        dialog, _settings = await self.dialog(
            FakeProwlarr(answer), prowlarr_url="http://nas.local:9696", prowlarr_api_key="k"
        )
        self.assertTrue(dialog._prowlarr_panel.expanded)
        self.assertEqual(dialog._prowlarr_url.get(), "http://nas.local:9696")

    async def test_the_toggle_folds_the_fields(self) -> None:
        dialog, _settings = await self.dialog(FakeProwlarr(status(ProwlarrState.NOT_INSTALLED)))
        panel = dialog._prowlarr_panel
        self.assertTrue(panel._install_row.winfo_manager())
        self.assertFalse(panel._open_button.winfo_manager())
        panel._toggle._label.event_generate("<Button-1>")
        self.assertTrue(panel.expanded)
        panel._toggle._label.event_generate("<Button-1>")
        self.assertFalse(panel.expanded)

    async def test_save_stores_the_boxes(self) -> None:
        answer = status(ProwlarrState.NOT_INSTALLED)
        dialog, settings = await self.dialog(
            FakeProwlarr(answer), disabled_indexers=("yts", "oldsite")
        )
        self.assertFalse(dialog._sources["yts"].get())
        self.assertTrue(dialog._use_prowlarr.get())
        dialog._sources["eztv"].set(False)
        dialog._use_prowlarr.set(False)
        await dialog._save()
        self.assertEqual(
            settings.current.disabled_indexers, ("eztv", "yts", "prowlarr", "oldsite")
        )

    async def test_use_prowlarr_reflects_the_setting(self) -> None:
        answer = status(ProwlarrState.NOT_INSTALLED)
        dialog, settings = await self.dialog(FakeProwlarr(answer), disabled_indexers=("prowlarr",))
        self.assertFalse(dialog._use_prowlarr.get())
        self.assertTrue(all(var.get() for var in dialog._sources.values()))
        dialog._use_prowlarr.set(True)
        await dialog._save()
        self.assertEqual(settings.current.disabled_indexers, ())

    async def test_install_connects_and_save_keeps_the_new_connection(self) -> None:
        prowlarr = FakeProwlarr(status(ProwlarrState.NOT_INSTALLED))
        dialog, settings = await self.dialog(prowlarr)
        panel = dialog._prowlarr_panel

        async def connected() -> None:  # what ProwlarrSetupService.install saves
            await settings.update(prowlarr_url=BASE, prowlarr_api_key=KEY)
            prowlarr.answer = status(ProwlarrState.CONNECTED_LOCAL, sites=5, address=BASE)

        prowlarr.on_install = connected
        panel._install_button.invoke()
        self.assertEqual(panel._label.cget("text"), "Installing… Windows may ask for permission")
        self.assertFalse(panel._install_row.winfo_manager())
        task = panel._install_task
        assert task is not None
        await task
        self.assertEqual(
            panel._label.cget("text"), "✓ Connected to Prowlarr on this PC · 5 sites"
        )
        self.assertEqual((dialog._prowlarr_url.get(), dialog._prowlarr_key.get()), (BASE, KEY))
        await dialog._save()
        current = settings.current
        self.assertEqual(
            (current.prowlarr_url, current.prowlarr_api_key.get_secret_value()), (BASE, KEY)
        )

    async def test_a_failed_install(self) -> None:
        prowlarr = FakeProwlarr(status(ProwlarrState.NOT_INSTALLED))
        prowlarr.installed = False
        dialog, _settings = await self.dialog(prowlarr)
        panel = dialog._prowlarr_panel
        reads = prowlarr.reads
        panel._install_button.invoke()
        task = panel._install_task
        assert task is not None
        await task
        self.assertEqual(panel._label.cget("text"), "Couldn't install it automatically.")
        self.assertTrue(panel._get_link.winfo_manager())
        self.assertFalse(panel._install_row.winfo_manager())
        self.assertEqual(prowlarr.reads, reads)  # nothing to read after a failure

    async def test_edited_fields_are_not_overwritten(self) -> None:
        prowlarr = FakeProwlarr(status(ProwlarrState.NOT_CONNECTED))
        dialog, settings = await self.dialog(prowlarr)
        dialog._prowlarr_url.insert(0, "http://nas.local:9696")
        await settings.update(prowlarr_url=BASE, prowlarr_api_key=KEY)
        dialog._prowlarr_panel.refresh()
        task = dialog._prowlarr_panel._status_task
        assert task is not None
        await task
        self.assertEqual(dialog._prowlarr_url.get(), "http://nas.local:9696")

    async def test_a_prowlarr_field_error_unfolds_the_fields(self) -> None:
        dialog, _settings = await self.dialog(FakeProwlarr(status(ProwlarrState.NOT_INSTALLED)))
        dialog._prowlarr_url.insert(0, "localhost:9696")
        await dialog._save()
        self.assertTrue(dialog.winfo_exists())
        self.assertTrue(dialog._error.cget("text").startswith("Prowlarr URL: "))
        self.assertTrue(dialog._prowlarr_panel.expanded)


if __name__ == "__main__":
    unittest.main()
