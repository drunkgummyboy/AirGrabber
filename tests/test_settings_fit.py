"""The Settings window's pages fit their width (round 2): at the narrowest page a screen gives
(app/ui/settings_layout.py's NARROW_PAGE_WIDTH, a 1366 x 768 screen at 150 %) and at the widest,
in English and in Dutch, no widget on any page reaches past what holds it, and no text (a label,
or a switch's, tick's or radio button's) gets less room than it asks for, so none is cut off at
its right edge. Every page is walked with MediaForge's parts built and ready, Plex connected and
every fold opened, once as the settings start and once with the remote program's form shown and
the folder library on. A wrapping label is tried with a long text too: whatever it will say must
wrap inside its room. Fakes throughout (tests/test_settings_registry.py's window)."""

from __future__ import annotations

import tkinter as tk
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.i18n import Language
from app.models import DownloadMode, RemoteKind, Settings
from app.models.folder_library import FolderLibraryStatus
from app.services import SettingsService
from app.ui.settings_dialog import SettingsDialog
from app.ui.settings_layout import NARROW_PAGE_WIDTH, WIDE_PAGE_WIDTH, Fold, page_width
from tests.forge.tk import pump
from tests.forge.ui.test_settings_library import READY, FakeForge, FakeSettings
from tests.test_backup_dialog import FakeBackup
from tests.test_folder_library_settings import FakeFolders
from tests.test_plex_ui import CONNECTED, FakePlex
from tests.test_settings_dialog import FakeProwlarr, FakeRepository, FakeShortcut, settle
from tests.test_settings_registry import RegistryCase

LONG_PATH = r"\\nas\video\A folder with a long name\and another one inside it\Season 01"
LONG = "A note that goes on for a while, as a path or a reason can, " * 6


def descendants(widget: tk.Misc) -> list[tk.Misc]:
    found: list[tk.Misc] = []
    for child in widget.winfo_children():
        found += [child, *descendants(child)]
    return found


def wraps(label: ctk.CTkLabel) -> bool:
    return int(label.cget("wraplength") or 0) > 0


class PagesFitTests(RegistryCase):
    def open(self, screen_width: float) -> SettingsDialog:
        """The window over the (see-through) root on a screen `screen_width` wide (logical),
        from the default settings (`self.service`; the folder library `self.folders`)."""
        self.root.deiconify()  # mapped, so that its pages are laid out
        self.root.attributes("-alpha", 0.0)
        scaling = ctk.ScalingTracker.get_window_scaling(self.root)
        area = (0, 0, round(screen_width * scaling), round(1000 * scaling))
        service = self.service = SettingsService(FakeRepository(), Settings())  # type: ignore[arg-type]
        self.folders = FakeFolders(service)
        with mock.patch("app.ui.settings_dialog.screen_area", return_value=area):
            dialog = SettingsDialog(
                self.root,
                service,
                search=mock.MagicMock(),
                station=mock.MagicMock(),
                updates=SimpleNamespace(current="3.2.0", previous_version="3.1.0"),  # type: ignore[arg-type]
                torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
                discovery=mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True)),
                prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
                startup_shortcut=FakeShortcut(),
                backup=FakeBackup(),  # type: ignore[arg-type]
                problem_reports=mock.MagicMock(),
                plex=FakePlex(CONNECTED),  # type: ignore[arg-type]
                folder_library=self.folders,  # type: ignore[arg-type]
                phone=SimpleNamespace(available=True, can_switch=False),  # type: ignore[arg-type]
                forge=FakeForge(FakeSettings(READY)),  # type: ignore[arg-type]
            )
        dialog.attributes("-alpha", 0.0)
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        return dialog

    async def to_the_other_state(self) -> None:
        """The open window's settings as the second walk sees them: the remote program's form
        shown, the folder library on with long paths it couldn't read or recognise."""
        await self.service.update(
            download_mode=DownloadMode.REMOTE,
            remote_kind=RemoteKind.QBITTORRENT,
            remote_url="http://192.168.1.20:8080",
            folder_library_on=True,
            series_folders=(r"\\nas\video\Series", LONG_PATH),
            film_folders=(r"\\nas\video\Movies",),
        )
        self.folders.set(
            FolderLibraryStatus(
                on=True,
                films=2,
                shows=1,
                episodes=12,
                unrecognised=(LONG_PATH,),
                unreachable=(LONG_PATH,),
            )
        )
        await settle()

    async def cut_off(self, dialog: SettingsDialog) -> list[str]:
        """Each widget shown on a page that reaches past what holds it, and each text (a label,
        or a switch's, tick's or radio button's) that gets less room than it asks for."""
        await settle()
        found: list[str] = []
        for section in dialog.sections:
            dialog.show_section(section)
            await pump(self.root, 0.02)
            page = dialog.page(section)
            for widget in descendants(page):
                command = getattr(widget, "_command", None)
                fold = getattr(command, "__self__", None)
                if isinstance(fold, Fold):
                    fold.set_open(True)
            dialog.update()
            shown = [w for w in descendants(page) if w.winfo_ismapped()]
            labels = [w for w in shown if isinstance(w, ctk.CTkLabel)]
            texts = {w: str(w.cget("text")) for w in labels}
            for label in labels:
                if wraps(label):
                    label.configure(text=LONG)
            dialog.update()
            edge = dialog._scroll._parent_canvas
            for widget in shown:
                problem = self._problem(widget, edge)
                if problem:
                    found.append(f"{section}: {type(widget).__name__} {_text(widget)!r} {problem}")
            for label, text in texts.items():
                label.configure(text=text)
        return found

    @staticmethod
    def _problem(widget: tk.Misc, edge: tk.Misc) -> str:
        asked, given = widget.winfo_reqwidth(), widget.winfo_width()
        if isinstance(widget, _TEXTS) and given + 1 < asked:
            return f"given {given} of {asked}"
        right = widget.winfo_rootx() + given
        holder: tk.Misc | None = widget.master
        while holder is not None:
            past = right - (holder.winfo_rootx() + holder.winfo_width())
            if past > 1:
                return f"{past} past its {type(holder).__name__}"
            if holder is edge:
                break
            holder = holder.master
        return ""

    async def walk_both_widths(self) -> None:
        # One window per width, walked in both states (building it is what takes the time).
        for screen, width in ((700, NARROW_PAGE_WIDTH), (1920 / 1.75, WIDE_PAGE_WIDTH)):
            dialog = self.open(screen)
            self.assertEqual(page_width(), width)
            await self.every_page_shown(dialog)
            with self.subTest(width=width, state="first"):
                self.assertEqual(await self.cut_off(dialog), [])
            await self.to_the_other_state()
            with self.subTest(width=width, state="remote, folders on"):
                self.assertEqual(await self.cut_off(dialog), [])
            dialog.destroy()

    async def test_nothing_is_cut_off_at_the_narrowest_and_widest_page(self) -> None:
        await self.walk_both_widths()

    async def test_nothing_is_cut_off_in_dutch_either(self) -> None:
        i18n.set_language(Language.NL)  # longer words than English's
        await self.walk_both_widths()


# What says a text whose room must not be cut: a label, and a switch's, tick's or radio
# button's text beside its mark.
_TEXTS = (ctk.CTkLabel, ctk.CTkSwitch, ctk.CTkCheckBox, ctk.CTkRadioButton)


def _text(widget: tk.Misc) -> str:
    try:
        return str(widget.cget("text"))[:50]
    except (tk.TclError, ValueError, AttributeError):
        return ""
