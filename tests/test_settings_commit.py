"""Instant saving in Settings (app/ui/settings_commit.py; spec §3): a change stores itself when it
is made, a refusal is said in its box and stores nothing, "✓ Saved · Undo" after each stored
change, Undo stores what was before; text fields store on Enter or leaving them, only when
changed; no Save or Cancel; Restart now just restarts; closing during a store is harmless."""

from __future__ import annotations

import asyncio
from typing import Any

import customtkinter as ctk

from app.models import DownloadMode, QualityProfile, Settings, ThemeMode
from app.services import SettingsService
from app.services.startup import StartupError
from app.ui.settings_commit import Committer, Refused, TextCommit, commit_fields
from app.ui.settings_layout import SettingsBox
from app.ui.settings_pages_registry import (
    PAGE_LOOKS,
    PAGE_PAGES,
    PAGE_QUALITY,
    PAGE_WINDOWS,
)
from tests.test_settings_dialog import DialogCase, click, find, labels, settle


class SlowRepository:
    """Stores only once `go` is set (a store under way while the window closes)."""

    def __init__(self) -> None:
        self.go = asyncio.Event()
        self.saved: list[Settings] = []

    async def save(self, settings: Settings) -> None:
        await self.go.wait()
        self.saved.append(settings)


class FailingShortcut:
    def __init__(self) -> None:
        self.on = False

    def is_enabled(self) -> bool:
        return self.on

    async def set_enabled(self, on: bool) -> None:
        raise StartupError("Windows didn't let AirGrabber change its Startup folder.")


class Shortcut:
    def __init__(self) -> None:
        self.on = False
        self.calls: list[bool] = []

    def is_enabled(self) -> bool:
        return self.on

    async def set_enabled(self, on: bool) -> None:
        self.calls.append(on)
        self.on = on


def buttons(widget: Any) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton):
            found.append(str(child.cget("text")))
        found += buttons(child)
    return found


class CommitTests(DialogCase):
    async def test_a_switch_stores_at_once_and_shows_undo(self) -> None:
        dialog, settings = self.dialog(keep_in_tray=False)
        bar = dialog.committer.bar
        assert bar is not None
        self.assertFalse(bar.shown)
        click(find(dialog.page(PAGE_WINDOWS), dialog._keep_in_tray))
        await settle()
        self.assertTrue(settings.current.keep_in_tray)
        self.assertTrue(bar.shown)
        self.assertEqual(bar.saved_label.cget("text"), "✓ Saved")
        self.assertEqual(bar.undo_link.cget("text"), "Undo")
        self.assertTrue(dialog.committer.can_undo)

    async def test_undo_restores_the_values_before(self) -> None:
        dialog, settings = self.dialog(theme_mode=ThemeMode.DARK)
        click(find(dialog.page(PAGE_LOOKS), dialog._theme_mode), "Light")
        await settle()
        self.assertIs(settings.current.theme_mode, ThemeMode.LIGHT)
        dialog.committer.undo()  # the bar's Undo
        await settle()
        self.assertIs(settings.current.theme_mode, ThemeMode.DARK)
        self.assertEqual(dialog._theme_mode.get(), "Dark")  # the control shows it again
        bar = dialog.committer.bar
        assert bar is not None
        self.assertFalse(bar.shown)
        self.assertFalse(dialog.committer.can_undo)  # Undo can't be undone

    async def test_undo_of_two_fields_together(self) -> None:
        dialog, settings = self.dialog(size_tv_min_gb=1, size_tv_max_gb=2)
        dialog._size_tv_min.set("3")
        dialog._size_tv_max.set("6")
        dialog._size_fields["tv"].commit_now()
        await settle()
        self.assertEqual((settings.current.size_tv_min_gb, settings.current.size_tv_max_gb), (3, 6))
        dialog.focus_set()  # the keyboard elsewhere: the fields show what is stored again
        dialog.committer.undo()
        await settle()
        self.assertEqual((settings.current.size_tv_min_gb, settings.current.size_tv_max_gb), (1, 2))
        self.assertEqual((dialog._size_tv_min.get(), dialog._size_tv_max.get()), ("1", "2"))

    async def test_a_refused_size_says_why_in_its_box_and_stores_nothing(self) -> None:
        dialog, settings = self.dialog()
        dialog._size_movie_min.set("lots")
        dialog._size_fields["movie"].commit_now()
        await settle()
        box = dialog.box(PAGE_QUALITY, "Movies")
        self.assertEqual(box.refusal, "Movies size: Use a number, like 2.5")
        self.assertIn("Movies size: Use a number, like 2.5", labels(box.frame))
        self.assertIsNone(settings.current.size_movie_min_gb)
        self.assertEqual(dialog._size_movie_min.get(), "lots")  # kept as typed
        bar = dialog.committer.bar
        assert bar is not None
        self.assertFalse(bar.shown)
        dialog._size_movie_min.set("2")  # corrected: stored, the refusal goes
        dialog._size_fields["movie"].commit_now()
        await settle()
        self.assertEqual(settings.current.size_movie_min_gb, 2)
        self.assertEqual(box.refusal, "")

    async def test_a_stored_change_keeps_another_controls_refusal(self) -> None:
        # Triage T2 M2: picking a quality doesn't hide the sizes' red line (still refused).
        dialog, settings = self.dialog()
        box = dialog.box(PAGE_QUALITY, "Movies")
        dialog._size_movie_min.set("lots")
        dialog._size_fields["movie"].commit_now()
        await settle()
        refused = "Movies size: Use a number, like 2.5"
        self.assertEqual(box.refusal, refused)
        click(find(dialog.page(PAGE_QUALITY), dialog._quality_movie), "4K (2160p)")
        await settle()
        self.assertIs(settings.current.quality_movie, QualityProfile.UHD)
        self.assertEqual(box.refusal, refused)  # the sizes' refusal stays
        self.assertIn(refused, labels(box.frame))
        dialog._size_movie_min.set("2")  # the sizes stored: their refusal goes
        dialog._size_fields["movie"].commit_now()
        await settle()
        self.assertEqual(box.refusal, "")

    async def test_a_store_from_elsewhere_takes_the_undo_away(self) -> None:
        # Final review Minor 1: Undo never puts a value back over one stored elsewhere since.
        dialog, settings = self.dialog(download_mode=DownloadMode.FOLDER)
        bar = dialog.committer.bar
        assert bar is not None
        dialog._mode.set(DownloadMode.APP.value)  # "On this PC" clicked
        dialog._mode_changed()
        await settle()
        self.assertIs(settings.current.download_mode, DownloadMode.APP)
        self.assertTrue(dialog.committer.can_undo)
        await settings.update(theme_mode=ThemeMode.LIGHT)  # another field: Undo stays
        self.assertTrue(dialog.committer.can_undo)
        self.assertTrue(bar.shown)
        # qBittorrent's setup points AirGrabber at its Web UI (not through the Committer).
        await settings.update(download_mode=DownloadMode.REMOTE)
        self.assertFalse(dialog.committer.can_undo)
        self.assertFalse(bar.shown)
        dialog.committer.undo()  # a click that was already on its way: nothing
        await settle()
        self.assertIs(settings.current.download_mode, DownloadMode.REMOTE)

    async def test_text_commits_on_enter_and_focus_out_only_when_changed(self) -> None:
        dialog, settings = self.dialog(excluded_keywords="cam")
        field = dialog._word_fields["excluded_keywords"]
        dialog._excluded.set("cam, telesync")
        await settle()
        self.assertEqual(settings.current.excluded_keywords, ("cam",))  # typing stores nothing
        self.assertTrue(field.pending)
        field.left(None)  # the keyboard went to another program: nothing stored, text kept
        await settle()
        self.assertEqual(settings.current.excluded_keywords, ("cam",))
        self.assertEqual(dialog._excluded.get(), "cam, telesync")
        field.commit_now()  # Enter (its binding: CommitterTests)
        await settle()
        self.assertEqual(settings.current.excluded_keywords, ("cam", "telesync"))
        self.assertFalse(field.pending)
        # Leaving it for another control of the window stores a change, and only a change.
        dialog._excluded.set("cam, telesync, ts")
        field.left(dialog)
        await settle()
        self.assertEqual(settings.current.excluded_keywords, ("cam", "telesync", "ts"))
        bar = dialog.committer.bar
        assert bar is not None
        bar.hide()
        field.left(dialog)  # unchanged: nothing stored, no bar
        await settle()
        self.assertFalse(bar.shown)

    async def test_min_and_max_commit_together(self) -> None:
        dialog, settings = self.dialog(size_tv_min_gb=1, size_tv_max_gb=2)
        field = dialog._size_fields["tv"]
        low, high = field.entries
        dialog._size_tv_min.set("5")  # alone it would be refused (5 > 2)
        field.left(high)  # on to the max: no store yet
        await settle()
        self.assertEqual(settings.current.size_tv_min_gb, 1)
        self.assertEqual(dialog.box(PAGE_QUALITY, "Series").refusal, "")
        dialog._size_tv_max.set("8")
        field.left(dialog)  # out of both: one change
        await settle()
        self.assertEqual((settings.current.size_tv_min_gb, settings.current.size_tv_max_gb), (5, 8))

    async def test_external_change_shows_unless_the_field_is_being_edited(self) -> None:
        dialog, settings = self.dialog(prowlarr_url="", blocked_groups="")
        await settings.update(prowlarr_url="http://127.0.0.1:9696", theme_mode=ThemeMode.LIGHT)
        self.assertEqual(dialog._prowlarr_url.get(), "http://127.0.0.1:9696")
        self.assertEqual(dialog._theme_mode.get(), "Light")
        chips = dialog.group_chips["blocked_groups"]
        chips.open_field()
        chips.entry.insert(0, "yify")  # typed, not stored
        await settings.update(blocked_groups="evo")
        self.assertEqual(chips.entry.get(), "yify")  # kept
        self.assertEqual(chips.names, ["evo"])  # the stored chips show
        chips.field.commit_now()
        await settle()
        self.assertEqual(settings.current.blocked_groups, ("evo", "yify"))

    async def test_closing_during_a_commit_touches_nothing(self) -> None:
        repository = SlowRepository()
        settings = SettingsService(repository, Settings(download_dir=self.folder))  # type: ignore[arg-type]
        dialog = self.open(settings)
        click(find(dialog.page(PAGE_PAGES), dialog._spotlight_motion))
        await asyncio.sleep(0.05)  # the store is under way
        dialog.close()
        self.assertFalse(dialog.winfo_exists())
        repository.go.set()
        await settle()  # no error from the closed window
        self.assertFalse(settings.current.spotlight_motion)
        self.assertEqual(len(repository.saved), 1)

    async def test_closing_stores_what_a_field_holds(self) -> None:
        dialog, settings = self.dialog()
        chips = dialog.group_chips["preferred_groups"]
        chips.open_field()
        chips.entry.insert(0, "ntb")
        dialog.close()
        await settle()
        self.assertEqual(settings.current.preferred_groups, ("ntb",))

    async def test_escape_closes_as_the_cross_does_storing_what_was_typed(self) -> None:
        # Decided (review M6): Escape is the ✕ of the keyboard, also from inside a field.
        dialog, settings = self.dialog()
        from tests.forge.tk import press

        blocked = dialog.group_chips["blocked_groups"]
        blocked.open_field()
        blocked.entry.insert(0, "evo")
        self.root.deiconify()  # a window on screen, as Settings is over the main window
        self.root.attributes("-alpha", 0.0)
        dialog.attributes("-alpha", 0.0)
        dialog.deiconify()
        dialog.update()
        press(dialog, "<Escape>")
        await settle()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(settings.current.blocked_groups, ("evo",))

    # -- text the model rewrites (review C1) -----------------------------------------------------

    async def test_a_rewritten_text_shows_as_stored_and_undo_then_close_stores_nothing(
        self,
    ) -> None:
        dialog, settings = self.dialog(excluded_keywords="")
        field = dialog._word_fields["excluded_keywords"]
        dialog._excluded.set("CAM")
        field.left(dialog)
        await settle()
        self.assertEqual(settings.current.excluded_keywords, ("cam",))
        self.assertEqual(dialog._excluded.get(), "cam")  # the stored form
        self.assertFalse(field.pending)
        dialog.committer.undo()
        await settle()
        self.assertEqual(settings.current.excluded_keywords, ())
        self.assertEqual(dialog._excluded.get(), "")  # Undo shows in it
        dialog.close()
        await settle()
        self.assertEqual(settings.current.excluded_keywords, ())  # nothing stored again

    async def test_a_browsed_folder_shows_as_stored_and_undo_shows_the_old_one(self) -> None:
        from unittest import mock

        dialog, settings = self.dialog()
        before = settings.current.download_dir
        new = self.folder / "sub"
        new.mkdir()
        chosen = str(new).replace("\\", "/")  # what askdirectory gives on Windows
        with mock.patch(
            "app.ui.settings_dialog.filedialog.askdirectory", return_value=chosen
        ):
            dialog._browse()
        await settle()
        self.assertEqual(settings.current.download_dir, new)
        self.assertEqual(dialog._download_dir.get(), str(new))
        dialog.committer.undo()
        await settle()
        self.assertEqual(settings.current.download_dir, before)
        self.assertEqual(dialog._download_dir.get(), str(before))
        dialog.close()
        await settle()
        self.assertEqual(settings.current.download_dir, before)

    async def test_a_typed_slash_path_and_a_size_show_their_stored_form(self) -> None:
        dialog, settings = self.dialog(size_movie_min_gb=1)
        typed = str(self.folder).replace("\\", "/") + "/"
        dialog._download_dir.set(typed)
        dialog._download_dir_field.commit_now()
        dialog._size_movie_min.set("2.50")
        dialog._size_fields["movie"].commit_now()
        await settle()
        self.assertEqual(dialog._download_dir.get(), str(settings.current.download_dir))
        self.assertEqual(dialog._size_movie_min.get(), "2.5")
        self.assertFalse(dialog._download_dir_field.pending)
        self.assertFalse(dialog._size_fields["movie"].pending)
        dialog.committer.undo()  # the size, the last change
        await settle()
        self.assertEqual(dialog._size_movie_min.get(), "1")

    async def test_text_typed_while_its_store_runs_is_kept(self) -> None:
        dialog, settings = self.dialog(excluded_keywords="")
        field = dialog._word_fields["excluded_keywords"]
        dialog._excluded.set("NTB")
        field.commit_now()
        dialog._excluded.set("NTB, flux")  # typed on before the store landed
        await settle()
        self.assertEqual(settings.current.excluded_keywords, ("ntb",))
        self.assertEqual(dialog._excluded.get(), "NTB, flux")
        self.assertTrue(field.pending)

    async def test_no_save_or_cancel_buttons(self) -> None:
        dialog, _settings = self.dialog()
        shown = buttons(dialog)
        self.assertNotIn("Save", shown)
        self.assertNotIn("Cancel", shown)
        self.assertFalse(hasattr(dialog, "cancel_button"))
        self.assertFalse(hasattr(dialog, "_save_button"))

    async def test_restart_now_just_restarts(self) -> None:
        restarts: list[bool] = []
        settings = SettingsService(
            SlowRepository(), Settings(download_dir=self.folder)  # type: ignore[arg-type]
        )
        dialog = self.open(settings, on_restart=lambda: restarts.append(True))
        dialog._restart_now()
        self.assertEqual(restarts, [True])
        self.assertFalse(dialog.committer.can_undo)  # nothing was stored

    async def test_start_with_windows_failure_flips_back(self) -> None:
        settings = SettingsService(
            FakeRepo(), Settings(download_dir=self.folder)  # type: ignore[arg-type]
        )
        dialog = self.open(settings, startup_shortcut=FailingShortcut())
        tick = find(dialog.page(PAGE_WINDOWS), dialog._start_with_windows)
        click(tick)
        self.assertTrue(dialog._start_with_windows.get())
        await settle()
        self.assertFalse(dialog._start_with_windows.get())  # back
        self.assertFalse(settings.current.start_with_windows)
        self.assertEqual(
            dialog.box(PAGE_WINDOWS, "In the background").refusal,
            "Windows didn't let AirGrabber change its Startup folder.",
        )

    async def test_start_with_windows_writes_the_shortcut_then_stores(self) -> None:
        settings = SettingsService(
            FakeRepo(), Settings(download_dir=self.folder)  # type: ignore[arg-type]
        )
        shortcut = Shortcut()
        dialog = self.open(settings, startup_shortcut=shortcut)
        click(find(dialog.page(PAGE_WINDOWS), dialog._start_with_windows))
        await settle()
        self.assertEqual(shortcut.calls, [True])
        self.assertTrue(settings.current.start_with_windows)
        dialog.committer.undo()
        await settle()
        self.assertEqual(shortcut.calls, [True, False])
        self.assertFalse(settings.current.start_with_windows)
        self.assertFalse(dialog._start_with_windows.get())

    def open(self, settings: SettingsService, **options: Any) -> Any:
        from types import SimpleNamespace
        from unittest import mock

        from app.ui.settings_dialog import SettingsDialog
        from tests.test_settings_dialog import FakeProwlarr, FakeShortcut

        options.setdefault("startup_shortcut", FakeShortcut())
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.4", previous_version=None),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=self.discovery,
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            **options,
        )
        self.addCleanup(self._close, dialog)
        return dialog


class FakeRepo:
    async def save(self, settings: Settings) -> None:
        pass


class SleepyRepo:
    """Each store takes a moment (another change can start meanwhile)."""

    async def save(self, settings: Settings) -> None:
        await asyncio.sleep(0.02)


class CommitterTests(DialogCase):
    """The Committer on its own: a part's refusal, nothing changed, Undo's own refusal."""

    async def test_a_parts_refusal_and_nothing_changed(self) -> None:
        box = SettingsBox(self.root, "A box")
        committer = Committer()  # no window: no bar
        stored: list[str] = []

        async def refuse() -> None:
            raise Refused("Not like that.")

        async def unchanged() -> bool:
            stored.append("asked")
            return False

        self.assertFalse(await committer.commit(refuse, None, box=box))
        self.assertEqual(box.refusal, "Not like that.")
        self.assertTrue(await committer.commit(unchanged, None, box=box))
        self.assertEqual(box.refusal, "")
        self.assertEqual(stored, ["asked"])
        self.assertFalse(committer.can_undo)

    async def test_commit_fields_and_its_undo(self) -> None:
        box = SettingsBox(self.root, "A box")
        committer = Committer(self.root)
        settings = SettingsService(FakeRepo(), Settings())  # type: ignore[arg-type]
        self.assertTrue(await commit_fields(committer, settings, box, auto_wait_hours=6))
        self.assertEqual(settings.current.auto_wait_hours, 6)
        self.assertTrue(await committer.run_undo())
        self.assertEqual(settings.current.auto_wait_hours, Settings().auto_wait_hours)

    async def test_quick_changes_of_one_control_undo_to_the_one_before(self) -> None:
        # Final review Minor 2: 2 → 4 → 6 faster than a store lands; Undo gives 4, not 2.
        box = SettingsBox(self.root, "A box")
        committer = Committer(self.root)
        settings = SettingsService(SleepyRepo(), Settings(auto_wait_hours=2))  # type: ignore[arg-type]
        first = asyncio.create_task(commit_fields(committer, settings, box, auto_wait_hours=4))
        second = asyncio.create_task(commit_fields(committer, settings, box, auto_wait_hours=6))
        self.assertEqual(await asyncio.gather(first, second), [True, True])
        self.assertEqual(settings.current.auto_wait_hours, 6)
        self.assertTrue(await committer.run_undo())
        self.assertEqual(settings.current.auto_wait_hours, 4)

    async def test_a_store_from_elsewhere_of_other_fields_keeps_the_undo(self) -> None:
        box = SettingsBox(self.root, "A box")
        committer = Committer(self.root)
        settings = SettingsService(FakeRepo(), Settings())  # type: ignore[arg-type]
        await commit_fields(committer, settings, box, auto_wait_hours=6)
        await settings.update(auto_wait_hours=6, keep_in_tray=not settings.current.keep_in_tray)
        self.assertTrue(committer.can_undo)  # its field still holds what it stored
        await settings.update(auto_wait_hours=12)
        self.assertFalse(committer.can_undo)
        self.assertFalse(await committer.run_undo())
        self.assertEqual(settings.current.auto_wait_hours, 12)

    async def test_a_text_field_with_hints(self) -> None:
        entry = ctk.CTkEntry(self.root, placeholder_text="hint")
        values = {"stored": ""}
        commits: list[str] = []
        field = TextCommit(
            [entry], stored=lambda: [values["stored"]], commit=lambda texts: commits.append(texts[0])
        )
        self.assertFalse(field.pending)  # the hint isn't text
        field.show(["kept"])
        self.assertEqual(entry.get(), "kept")
        entry.insert("end", "!")
        field.commit_now()
        self.assertEqual(commits, ["kept!"])

    async def test_enter_stores(self) -> None:
        self.root.deiconify()
        entry = ctk.CTkEntry(self.root)
        entry.pack()
        commits: list[str] = []
        field = TextCommit([entry], stored=lambda: [""], commit=lambda texts: commits.append(texts[0]))
        entry.insert(0, "typed")
        self.root.update()
        entry._entry.focus_force()
        self.root.update()
        entry._entry.event_generate("<Return>")
        self.root.update()
        self.assertEqual(commits, ["typed"])
        self.assertTrue(field.pending)  # nothing stores here: still not stored
