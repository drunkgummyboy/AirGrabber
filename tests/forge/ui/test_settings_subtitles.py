"""Settings › Subtitles in MediaForge's group (app/forge/ui/settings_subtitles.py): its boxes in
order, what Save stores, a refused key storing nothing, the texts and rows; and what left AirGrabber:
no Subtitles section, no CC, no periodic run, the folder's box on Where downloads go."""

from __future__ import annotations

import ast
import asyncio
import gc
import tkinter as tk
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.forge.models.subtitles import (
    KeyRejected,
    LoginRejected,
    SubtitleAccount,
    SubtitleQuota,
    Unreachable,
)
from app.forge.ui.look import app_look
from app.forge.ui.settings_pages import FORGE_SECTIONS, SECTION_LIBRARY
from app.forge.ui.settings_naming import SECTION_NAMING
from app.forge.ui.settings_subtitles import (
    CREDIT_TEXT,
    SECTION_SUBTITLES,
    SUBTITLES_ICON,
    AccountRow,
    LanguageTags,
    OwnKeyRow,
    SubtitlesSection,
    account_text,
    addable_languages,
    own_key_needs_check,
    quota_line,
    subtitle_problem,
)
from app.models import HearingImpaired, Settings
from app.services import Services, SettingsService
from app.ui import main_window as main_window_module
from app.ui.main_window import settings_affect_pages
from app.ui.settings_dialog import SECTION_GROUPS, SECTION_WHERE, SECTIONS, section_for_field
from app.ui.settings_layout import SettingsPage
from tests.forge.tk import pump, pump_until
from tests.forge.ui.settings_kit import forge_settings
from tests.forge.ui.subtitle_kit import FakeSubtitleSource
from tests.test_settings_dialog import FakeRepository

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class TextTests(unittest.TestCase):
    def test_quota_line(self) -> None:
        self.assertIsNone(quota_line(SubtitleQuota(), NOW))
        self.assertEqual(
            quota_line(SubtitleQuota(remaining=18), NOW), "18 subtitle downloads left today"
        )
        self.assertEqual(
            quota_line(SubtitleQuota(remaining=1), NOW), "1 subtitle download left today"
        )
        self.assertEqual(
            quota_line(SubtitleQuota(remaining=0), NOW), "No subtitle downloads left today"
        )
        later = SubtitleQuota(remaining=0, reset_at=NOW + timedelta(hours=2))
        self.assertIn("more after", quota_line(later, NOW) or "")

    def test_account_text(self) -> None:
        self.assertEqual(
            account_text(SubtitleAccount(allowed_downloads=20, remaining_downloads=18)),
            "✓ Signed in · 20 downloads a day, 18 left today",
        )
        self.assertEqual(account_text(SubtitleAccount()), "✓ Signed in")

    def test_what_save_needs(self) -> None:
        fine = {"languages": ("en",), "username": "", "password": ""}
        self.assertIsNone(subtitle_problem(**fine))  # type: ignore[arg-type]
        self.assertIn(
            "both",
            subtitle_problem(languages=("en",), username="alex", password="") or "",
        )
        self.assertIn("both", subtitle_problem(languages=("en",), username="", password="x") or "")
        self.assertIn("language", subtitle_problem(languages=(), username="", password="") or "")

    def test_addable_languages(self) -> None:
        self.assertNotIn("en", addable_languages(("en",)))
        self.assertIn("nl", addable_languages(("en",)))

    def test_needs_check(self) -> None:
        self.assertFalse(own_key_needs_check("", "OLD"))
        self.assertFalse(own_key_needs_check("OLD ", "OLD"))
        self.assertTrue(own_key_needs_check("NEW", "OLD"))
        self.assertTrue(own_key_needs_check("NEW", None))


class RegistryTests(unittest.TestCase):
    def test_after_naming_with_its_icon(self) -> None:
        names = list(FORGE_SECTIONS)
        self.assertEqual(names.index(SECTION_SUBTITLES), names.index(SECTION_NAMING) + 1)
        self.assertEqual(FORGE_SECTIONS[SECTION_SUBTITLES].icon, SUBTITLES_ICON)
        self.assertEqual(SUBTITLES_ICON, "💬")
        self.assertNotEqual(names[0], SECTION_SUBTITLES)
        self.assertEqual(names[0], SECTION_LIBRARY)


class SaveKeepsThePagesTests(unittest.TestCase):
    def test_saving_this_page_doesnt_reload_airgrabbers_pages(self) -> None:
        old = Settings()
        for changes in (
            {"subtitle_languages": ("nl",)},
            {"subtitles_hearing_impaired": HearingImpaired.PREFER},
            {"opensubtitles_username": "alex"},
            {"opensubtitles_password": "pw"},
            {"opensubtitles_own_key": "KEY"},
            {"subtitles_folder": "D:/x"},
        ):
            new = Settings.model_validate({**old.model_dump(), **changes})
            self.assertFalse(settings_affect_pages(old, new), changes)


class FakeParts:
    def __init__(self, subtitles: FakeSubtitleSource, settings: SettingsService) -> None:
        self.subtitles = subtitles
        self.airgrabber_settings = settings


class FakeStarter:
    def __init__(self, parts: FakeParts | None, *, started: bool = True) -> None:
        self._parts = parts
        self.services = parts if started else None
        self.reason = "MediaForge isn't ready yet"

    async def start(self) -> FakeParts | None:
        await asyncio.sleep(0)
        self.services = self._parts
        return self._parts


def _labels(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            text = str(child.cget("text"))
            if text and child.grid_info():
                found.append(text)
            elif text and child.winfo_manager():
                found.append(text)
        found += _labels(child)
    return found


class TkCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0
        self.source = FakeSubtitleSource()
        self.source.quota = SubtitleQuota(remaining=18)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def settings(self, **stored: Any) -> SettingsService:
        return SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=Path("D:/dl"), **stored),
        )

    def section(self, settings: SettingsService | None = None, *, started: bool = True) -> SubtitlesSection:
        self.stored = settings if settings is not None else self.settings()
        page = SettingsPage(self.root, "Subtitles", "note")
        page.frame.pack()
        self.page = page
        parts = FakeParts(self.source, self.stored)
        return SubtitlesSection(page, FakeStarter(parts, started=started))


class SectionTests(TkCase):
    async def test_the_boxes_in_order_and_nothing_of_the_old_page(self) -> None:
        self.section()
        texts = _labels(self.page.frame)
        order = [
            "Languages",
            "Hearing impaired",
            "OpenSubtitles account",
            "Create a free account",
            "Own API key",
            "18 subtitle downloads left today",
            "Subtitles provided by OpenSubtitles.com",
        ]
        positions = [texts.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        for gone in ("When", "For", "Get subtitles", "Where finished downloads end up"):
            self.assertNotIn(gone, texts)
        switches = [
            w for w in self.page.frame.winfo_children() if isinstance(w, ctk.CTkSwitch)
        ]
        self.assertEqual(switches, [])

    async def test_it_shows_what_is_stored(self) -> None:
        part = self.section(
            self.settings(
                subtitle_languages=("nl", "en"),
                subtitles_hearing_impaired=HearingImpaired.PREFER,
                opensubtitles_username="alex",
                opensubtitles_password="pw",
                opensubtitles_own_key="KEY",
            )
        )
        assert part.languages and part.hearing and part.account and part.own_key
        self.assertEqual(part.languages.languages, ("nl", "en"))
        self.assertEqual(part.hearing.get(), "Prefer")
        self.assertEqual(part.account.values, ("alex", "pw"))
        self.assertEqual(part.own_key.value, "KEY")
        self.assertEqual(part.changes(), {})

    async def test_without_a_key_one_quiet_line(self) -> None:
        self.source.available = False
        part = self.section()
        self.assertFalse(part.ready)
        self.assertEqual(
            str(part.waiting_label.cget("text")), "Subtitles aren't available in this version yet"
        )
        self.assertNotIn("Languages", _labels(self.page.frame))
        self.assertIsNone(part.problem())
        self.assertIsNone(await part.save())

    async def test_it_waits_for_forge(self) -> None:
        part = self.section(started=False)
        self.assertFalse(part.ready)
        await pump_until(self.root, lambda: part.ready)
        self.assertEqual(self.page.frame.winfo_exists(), 1)

    async def test_forge_cant_start(self) -> None:
        page = SettingsPage(self.root, "Subtitles", "note")
        part = SubtitlesSection(page, FakeStarter(None, started=False))
        await pump(self.root, 0.05)
        self.assertEqual(str(part.waiting_label.cget("text")), "MediaForge isn't ready yet")
        self.assertIsNone(part.problem())
        self.assertIsNone(await part.save())


class SaveTests(TkCase):
    async def test_only_what_changed_is_stored(self) -> None:
        part = self.section()
        assert part.languages and part.hearing
        self.assertIsNone(await part.save())
        part.languages.add("nl")
        part.hearing.set("Either")
        self.assertEqual(
            part.changes(),
            {
                "subtitle_languages": ("en", "nl"),
                "subtitles_hearing_impaired": HearingImpaired.EITHER,
            },
        )
        self.assertIsNone(part.problem())
        self.assertIsNone(await part.save())
        current = self.stored.current
        self.assertEqual(current.subtitle_languages, ("en", "nl"))
        self.assertEqual(current.subtitles_hearing_impaired, HearingImpaired.EITHER)
        self.assertEqual(part.changes(), {})

    async def test_the_old_fields_are_not_touched(self) -> None:
        part = self.section(self.settings(subtitles_enabled=True, subtitles_folder="D:/x"))
        assert part.languages
        part.languages.add("nl")
        await part.save()
        self.assertTrue(self.stored.current.subtitles_enabled)
        self.assertEqual(self.stored.current.subtitles_folder, "D:/x")

    async def test_the_account_is_stored_with_both_filled_in(self) -> None:
        part = self.section()
        assert part.account
        part.account.username.insert(0, "alex")
        self.assertIn("both", part.problem() or "")
        part.account.password.insert(0, "S3cret")
        self.assertIsNone(part.problem())
        await part.save()
        self.assertEqual(self.stored.current.opensubtitles_username, "alex")
        self.assertEqual(self.stored.current.opensubtitles_password.get_secret_value(), "S3cret")
        self.assertEqual(self.source.sign_ins, [])  # Save doesn't sign in

    async def test_no_language_is_refused(self) -> None:
        part = self.section()
        assert part.languages
        part.languages.remove("en")
        self.assertEqual(part.problem(), "Choose at least one subtitle language")

    async def test_unchanged_settings_never_stop_save(self) -> None:
        part = self.section(self.settings(opensubtitles_username="alex"))
        self.assertIsNone(part.problem())

    async def test_a_new_own_key_is_checked_and_stored(self) -> None:
        part = self.section()
        assert part.own_key
        part.own_key.entry.insert(0, "NEWKEY")
        self.assertIsNone(await part.save())
        self.assertEqual(self.source.checked, ["NEWKEY"])
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "NEWKEY")

    async def test_a_refused_key_stores_nothing_and_says_why(self) -> None:
        self.source.key_error = KeyRejected("OpenSubtitles refused your own API key")
        part = self.section()
        assert part.own_key and part.languages
        part.languages.add("nl")
        part.own_key.entry.insert(0, "BADKEY")
        problem = await part.save()
        self.assertEqual(problem, "Own API key: OpenSubtitles refused your own API key")
        self.assertNotIn("BADKEY", problem or "")
        current = self.stored.current
        self.assertEqual(current.opensubtitles_own_key.get_secret_value(), "")
        self.assertEqual(current.subtitle_languages, ("en",))  # nothing of the page
        self.assertIn("refused", part.own_key.result_text)

    async def test_an_unreachable_check_still_saves(self) -> None:
        self.source.key_error = Unreachable("no answer")
        part = self.section()
        assert part.own_key
        part.own_key.entry.insert(0, "NEWKEY")
        self.assertIsNone(await part.save())
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "NEWKEY")
        self.assertIn("saved anyway", part.own_key.result_text)

    async def test_a_saved_or_empty_key_is_not_checked_again(self) -> None:
        part = self.section(self.settings(opensubtitles_own_key="OLD"))
        assert part.languages and part.own_key
        part.languages.add("nl")
        await part.save()
        self.assertEqual(self.source.checked, [])
        part.own_key.entry.delete(0, "end")
        await part.save()
        self.assertEqual(self.source.checked, [])
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "")

    async def test_a_store_that_fails(self) -> None:
        part = self.section()
        assert part.languages
        part.languages.add("nl")
        with mock.patch.object(self.stored, "update", side_effect=OSError("disk full")):
            problem = await part.save()
        self.assertIn("couldn't be saved", problem or "")
        self.assertNotIn("disk full", problem or "")


class RowTests(TkCase):
    async def test_tags(self) -> None:
        tags = LanguageTags(self.root, ("en",), app_look())
        tags.add("nl")
        tags.add("nl")
        tags.add("xx")
        self.assertEqual(tags.languages, ("en", "nl"))
        tags.remove("en")
        self.assertEqual(tags.languages, ("nl",))

    async def account(self) -> AccountRow:
        return AccountRow(
            self.root,
            subtitles=self.source,
            username="",
            password="",
            look=app_look(),
            field_width=400,
        )

    async def test_sign_in(self) -> None:
        signed: list[SubtitleAccount] = []
        row = AccountRow(
            self.root,
            subtitles=self.source,
            username="alex",
            password="pw",
            look=app_look(),
            on_signed_in=signed.append,
            field_width=400,
        )
        row.sign_in()
        await pump_until(self.root, lambda: signed)
        self.assertEqual(self.source.sign_ins, [("alex", "pw")])
        self.assertIn("✓ Signed in", row.result_text)

    async def test_a_wrong_password(self) -> None:
        self.source.sign_in_error = LoginRejected("Wrong OpenSubtitles user name or password")
        row = AccountRow(
            self.root,
            subtitles=self.source,
            username="alex",
            password="bad",
            look=app_look(),
            field_width=400,
        )
        row.sign_in()
        await pump_until(self.root, lambda: "Wrong" in row.result_text)

    async def test_sign_in_needs_both_fields(self) -> None:
        row = await self.account()
        row.sign_in()
        self.assertIn("Fill in", row.result_text)
        self.assertEqual(self.source.sign_ins, [])

    async def test_check(self) -> None:
        row = OwnKeyRow(self.root, subtitles=self.source, key="", look=app_look(), field_width=400)
        row.check()
        self.assertEqual(row.result_text, "Empty: AirGrabber's own key is used")
        row.entry.insert(0, "KEY")
        row.check()
        await pump_until(self.root, lambda: "accepted" in row.result_text)
        self.assertEqual(self.source.checked, ["KEY"])
        self.assertFalse(row.needs_check)

    def test_the_credit_says_what_opensubtitles_asks(self) -> None:
        self.assertEqual(CREDIT_TEXT, "Subtitles provided by OpenSubtitles.com")


class AirGrabberTests(TkCase):
    """What left AirGrabber, and the folder's new place."""

    async def test_no_subtitles_section_under_downloading(self) -> None:
        self.assertNotIn("Subtitles", SECTIONS)
        downloading = next(names for group, names in SECTION_GROUPS if group == "Downloading")
        self.assertEqual(downloading, ("Where downloads go", "Quality", "Automatic", "Plex", "Phone connection"))

    def test_the_fields_section(self) -> None:
        self.assertEqual(section_for_field("subtitles_folder"), SECTION_WHERE)

    def test_no_subtitle_service(self) -> None:
        self.assertNotIn("subtitles", Services.__slots__)

    def test_no_periodic_run_or_events(self) -> None:
        source = (ROOT / "app" / "ui" / "main_window.py").read_text(encoding="utf-8")
        for gone in ("services.subtitles", "SubtitleRuns", "_on_subtitle_event", "subtitle_mark"):
            self.assertNotIn(gone, source)
        self.assertFalse(hasattr(main_window_module, "subtitle_status_text"))

    def test_no_cc_in_the_windows_and_the_page(self) -> None:
        for name in ("show_details.py", "movie_details.py", "views/downloads_view.py"):
            source = (ROOT / "app" / "ui" / name).read_text(encoding="utf-8")
            self.assertNotIn("subtitle_control", source, name)
            self.assertNotIn("CCControl", source, name)
            self.assertNotIn("cc_control", source, name)

    def test_nothing_imports_the_old_modules(self) -> None:
        gone = {
            "app.services.subtitles",
            "app.services.opensubtitles",
            "app.models.opensubtitles",
            "app.ui.subtitle_control",
            "app.ui.subtitle_settings",
        }
        for path in (ROOT / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in gone:
                    self.fail(f"{path} imports {node.module}")

    async def test_the_folder_box_on_where_downloads_go(self) -> None:
        settings = self.settings(subtitles_folder="D:/finished")
        window = forge_settings(self.root, None, settings=settings)
        self.addCleanup(window.destroy)
        window.attributes("-alpha", 0.0)
        await pump(self.root, 0.05)
        texts = _labels(window.page(SECTION_WHERE))
        self.assertIn("Where finished downloads end up", texts)
        hint = next(text for text in texts if text.startswith("Your torrent program's"))
        self.assertNotIn("subtitle", hint.casefold())
        self.assertEqual(window._finished_folder.get(), "D:/finished")
        window._finished_folder.set("D:/other")
        await window._store()
        self.assertEqual(settings.current.subtitles_folder, "D:/other")

    async def test_choose(self) -> None:
        settings = self.settings()
        window = forge_settings(self.root, None, settings=settings)
        self.addCleanup(window.destroy)
        window.attributes("-alpha", 0.0)
        with mock.patch(
            "app.ui.settings_dialog.filedialog.askdirectory", return_value="D:/chosen"
        ):
            window._choose_finished_folder()
        self.assertEqual(window._finished_folder.get(), str(Path("D:/chosen")))

    async def test_the_group_has_the_page(self) -> None:
        parts = SimpleNamespace(
            subtitles=self.source,
            airgrabber_settings=self.settings(),
        )
        forge = FakeStarter(parts)  # type: ignore[arg-type]
        window = forge_settings(self.root, forge, section=SECTION_SUBTITLES)
        self.addCleanup(window.destroy)
        window.attributes("-alpha", 0.0)
        self.assertEqual(window.section, SECTION_SUBTITLES)
        self.assertIsInstance(window.parts[SECTION_SUBTITLES], SubtitlesSection)
        names = window.sections
        self.assertEqual(names.index(SECTION_SUBTITLES), names.index(SECTION_NAMING) + 1)


if __name__ == "__main__":
    unittest.main()
