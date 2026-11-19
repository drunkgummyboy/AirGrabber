"""Settings › Subtitles in MediaForge's group (app/forge/ui/settings_subtitles.py): its boxes in
order, what each change stores (at once; the account's two fields together; the own key after
OpenSubtitles took it), a refusal said in its box and storing nothing, the texts and rows; and what
left AirGrabber: no Subtitles section, no CC, no periodic run, the folder's box on Folders."""

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
from app.forge.ui.settings_pages import FORGE_PARTS
from app.forge.ui.settings_subtitles import (
    CREDIT_TEXT,
    AccountRow,
    LanguageTags,
    OwnKeyRow,
    SubtitlesSection,
    account_text,
    addable_languages,
    account_problem,
    own_key_needs_check,
    quota_line,
)
from app.models import HearingImpaired, Settings
from app.services import Services, SettingsService
from app.ui import main_window as main_window_module
from app.ui.main_window import settings_affect_pages
from app.ui.settings_pages_registry import (
    GROUPS,
    PAGE_ACCOUNTS,
    PAGE_FOLDERS,
    PAGE_NAMING,
    PAGE_SUBTITLES,
    PAGES,
)
from app.ui.settings_layout import SettingsPage
from tests.forge.tk import pump, pump_until
from tests.forge.ui.settings_kit import forge_settings
from tests.forge.ui.subtitle_kit import FakeSubtitleSource
from tests.test_settings_dialog import FakeRepository, settle

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

    def test_what_the_account_needs(self) -> None:
        self.assertIsNone(account_problem(username="", password=""))
        self.assertIsNone(account_problem(username="alex", password="pw"))
        self.assertIn("both", account_problem(username="alex", password="") or "")
        self.assertIn("both", account_problem(username="", password="x") or "")

    def test_addable_languages(self) -> None:
        self.assertNotIn("en", addable_languages(("en",)))
        self.assertIn("nl", addable_languages(("en",)))

    def test_needs_check(self) -> None:
        self.assertFalse(own_key_needs_check("", "OLD"))
        self.assertFalse(own_key_needs_check("OLD ", "OLD"))
        self.assertTrue(own_key_needs_check("NEW", "OLD"))
        self.assertTrue(own_key_needs_check("NEW", None))


class RegistryTests(unittest.TestCase):
    def test_after_naming_with_its_icon_and_its_box_on_accounts(self) -> None:
        files = next(pages for group, pages in GROUPS if group == "Your files")
        self.assertEqual(files.index(PAGE_SUBTITLES), files.index(PAGE_NAMING) + 1)
        self.assertEqual(PAGES[PAGE_SUBTITLES].icon, "💬")
        self.assertIsNotNone(FORGE_PARTS[PAGE_SUBTITLES].build)
        # The OpenSubtitles box on Accounts & keys is the Subtitles part's.
        self.assertEqual(FORGE_PARTS[PAGE_ACCOUNTS].owner, PAGE_SUBTITLES)


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
            "OpenSubtitles",
            "Create a free account",
            "Own API key",
            "18 subtitle downloads left today",
            "Subtitles provided by OpenSubtitles.com",
        ]
        positions = [texts.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        for gone in ("When", "For", "Get subtitles", "Finished downloads"):
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

    async def test_without_a_key_one_quiet_line(self) -> None:
        self.source.available = False
        part = self.section()
        self.assertFalse(part.ready)
        self.assertEqual(
            str(part.waiting_label.cget("text")), "Subtitles aren't available in this version yet"
        )
        self.assertNotIn("Languages", _labels(self.page.frame))
        self.assertIsNone(part._commit(part.waiting, subtitle_languages=("nl",)))  # nothing to

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
        self.assertFalse(part.ready)


class StoreTests(TkCase):
    async def test_each_change_stores_itself(self) -> None:
        part = self.section()
        assert part.languages and part.hearing
        part.languages.add("nl")  # "+ Add"'s menu
        await settle()
        self.assertEqual(self.stored.current.subtitle_languages, ("en", "nl"))
        part.hearing._dropdown_callback("Either")  # picked in the menu
        await settle()
        current = self.stored.current
        self.assertEqual(current.subtitles_hearing_impaired, HearingImpaired.EITHER)

    async def test_undo_and_a_change_stored_elsewhere_show(self) -> None:
        part = self.section()
        assert part.languages and part.hearing and part.account
        part.languages.add("nl")
        await settle()
        self.assertTrue(await part._committer.run_undo())
        self.assertEqual(self.stored.current.subtitle_languages, ("en",))
        self.assertEqual(part.languages.languages, ("en",))
        await self.stored.update(
            subtitles_hearing_impaired=HearingImpaired.PREFER, opensubtitles_username="sam"
        )
        self.assertEqual(part.hearing.get(), "Prefer")
        self.assertEqual(part.account.values[0], "sam")

    async def test_the_old_fields_are_not_touched(self) -> None:
        part = self.section(self.settings(subtitles_enabled=True, subtitles_folder="D:/x"))
        assert part.languages
        part.languages.add("nl")
        await settle()
        self.assertTrue(self.stored.current.subtitles_enabled)
        self.assertEqual(self.stored.current.subtitles_folder, "D:/x")

    async def test_the_account_is_stored_with_both_filled_in(self) -> None:
        part = self.section()
        assert part.account and part.account_field and part.account_box
        part.account.username.insert(0, "alex")
        part.account_field.left(part.account.password)  # on to the password: nothing yet
        await settle()
        self.assertEqual(part.account_box.refusal, "")
        part.account_field.left(self.root)  # out of both: one change, half filled in
        await settle()
        self.assertIn("both", part.account_box.refusal)
        self.assertEqual(self.stored.current.opensubtitles_username, "")
        part.account.password.insert(0, "S3cret")
        part.account_field.commit_now()
        await settle()
        self.assertEqual(self.stored.current.opensubtitles_username, "alex")
        self.assertEqual(self.stored.current.opensubtitles_password.get_secret_value(), "S3cret")
        self.assertEqual(part.account_box.refusal, "")
        self.assertEqual(self.source.sign_ins, [])  # storing doesn't sign in

    async def test_no_language_is_refused(self) -> None:
        part = self.section()
        assert part.languages
        part.languages.remove("en")
        await settle()
        self.assertEqual(part._languages_box.refusal, "Choose at least one subtitle language")
        self.assertEqual(self.stored.current.subtitle_languages, ("en",))
        self.assertEqual(part.languages.languages, ("en",))  # the tag is back

    async def test_a_new_own_key_is_checked_and_stored(self) -> None:
        part = self.section()
        assert part.own_key and part.key_field
        part.own_key.entry.insert(0, "NEWKEY")
        part.key_field.commit_now()  # Enter, or leaving the field
        await settle()
        self.assertEqual(self.source.checked, ["NEWKEY"])
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "NEWKEY")

    async def test_other_changes_dont_wait_for_the_own_keys_check(self) -> None:
        # Round 2, 1: OpenSubtitles' check runs before the settings lock is taken.
        release = asyncio.Event()

        class Slow(FakeSubtitleSource):
            async def check_key(self, key: str) -> None:
                await release.wait()
                await super().check_key(key)

        self.source = Slow()
        part = self.section()
        assert part.own_key and part.key_field and part.hearing
        part.own_key.entry.insert(0, "NEWKEY")
        part.key_field.commit_now()
        await asyncio.sleep(0.05)  # the check is out
        part.hearing._dropdown_callback("Either")
        await asyncio.wait_for(
            self._stored(lambda c: c.subtitles_hearing_impaired is HearingImpaired.EITHER), 2
        )
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "")
        release.set()
        await settle()
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "NEWKEY")

    async def _stored(self, done: Any) -> None:
        while not done(self.stored.current):
            await asyncio.sleep(0.01)

    async def test_a_refused_key_stores_nothing_and_says_why(self) -> None:
        self.source.key_error = KeyRejected("OpenSubtitles refused your own API key")
        part = self.section()
        assert part.own_key and part.key_field and part.account_box
        part.own_key.entry.insert(0, "BADKEY")
        part.key_field.commit_now()
        await settle()
        problem = part.account_box.refusal
        self.assertEqual(problem, "Own API key: OpenSubtitles refused your own API key")
        self.assertNotIn("BADKEY", problem)
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "")
        self.assertIn("refused", part.own_key.result_text)
        self.assertEqual(part.own_key.value, "BADKEY")  # kept as typed

    async def test_an_unreachable_check_still_stores(self) -> None:
        self.source.key_error = Unreachable("no answer")
        part = self.section()
        assert part.own_key and part.key_field
        part.own_key.entry.insert(0, "NEWKEY")
        part.key_field.commit_now()
        await settle()
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "NEWKEY")
        self.assertIn("saved anyway", part.own_key.result_text)

    async def test_a_stored_or_empty_key_is_not_checked_again(self) -> None:
        part = self.section(self.settings(opensubtitles_own_key="OLD"))
        assert part.own_key and part.key_field
        part.key_field.commit_now()  # unchanged: nothing
        await settle()
        self.assertEqual(self.source.checked, [])
        part.own_key.entry.delete(0, "end")
        part.key_field.commit_now()
        await settle()
        self.assertEqual(self.source.checked, [])
        self.assertEqual(self.stored.current.opensubtitles_own_key.get_secret_value(), "")


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

    async def test_no_subtitles_page_under_downloading(self) -> None:
        downloading = next(pages for group, pages in GROUPS if group == "Downloading")
        self.assertEqual(downloading, ("Torrent program", "Quality & sources", "Automatic"))

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

    async def test_the_folder_box_on_folders(self) -> None:
        settings = self.settings(subtitles_folder="D:/finished")
        window = forge_settings(self.root, None, settings=settings)
        self.addCleanup(window.destroy)
        window.attributes("-alpha", 0.0)
        await pump(self.root, 0.05)
        texts = _labels(window.page(PAGE_FOLDERS))
        self.assertIn("Finished downloads", texts)
        hint = next(text for text in texts if text.startswith("Your torrent program's"))
        self.assertNotIn("subtitle", hint.casefold())
        self.assertEqual(window._finished_folder.get(), "D:/finished")
        window._finished_folder.set("D:/other")
        window._finished_field.commit_now()  # Enter, or leaving the field
        await settle()
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
        await settle()
        self.assertEqual(settings.current.subtitles_folder, str(Path("D:/chosen")))  # at once

    async def test_the_group_has_the_page(self) -> None:
        parts = SimpleNamespace(
            subtitles=self.source,
            airgrabber_settings=self.settings(),
        )
        forge = FakeStarter(parts)  # type: ignore[arg-type]
        window = forge_settings(self.root, forge, section=PAGE_SUBTITLES)
        self.addCleanup(window.destroy)
        window.attributes("-alpha", 0.0)
        self.assertEqual(window.section, PAGE_SUBTITLES)
        self.assertIsInstance(window.parts[PAGE_SUBTITLES], SubtitlesSection)
        names = window.sections
        self.assertEqual(names.index(PAGE_SUBTITLES), names.index(PAGE_NAMING) + 1)
        # Its OpenSubtitles box is on Accounts & keys, the same part.
        self.assertIs(window.parts[PAGE_ACCOUNTS], window.parts[PAGE_SUBTITLES])
        part = window.parts[PAGE_SUBTITLES]
        assert isinstance(part, SubtitlesSection)
        await pump_until(self.root, lambda: part.ready)
        accounts = _labels(window.page(PAGE_ACCOUNTS))
        for text in ("OpenSubtitles", "Create a free account", "Own API key"):
            self.assertIn(text, accounts)
            self.assertNotIn(text, _labels(window.page(PAGE_SUBTITLES)))
        self.assertIn("Languages", _labels(window.page(PAGE_SUBTITLES)))


if __name__ == "__main__":
    unittest.main()
