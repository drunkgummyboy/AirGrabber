"""Settings → Subtitles: the texts (pure), the language tags, Sign in, the credit line,
the section's rows and Save (and its one line while this version ships no API key); the
settings fields (an older build's per-user key is dropped); and the show window's "CC EN NL"
mark."""

from __future__ import annotations

import asyncio
import gc
import json
import tempfile
import tkinter as tk
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.database import Database
from app.models import (
    SUBTITLE_LANGUAGES,
    CalendarEntry,
    Episode,
    ProwlarrState,
    ProwlarrStatus,
    Settings,
    Show,
    SubtitleAccount,
    SubtitleProblem,
    SubtitleQuota,
)
from app.services import SettingsService
from app.services.opensubtitles import (
    LOGIN_REJECTED_TEXT,
    OWN_KEY_REJECTED_TEXT,
    UNREACHABLE_TEXT,
    KeyRejected,
    LoginRejected,
    Unreachable,
)
from app.ui.main_window import subtitle_status_text
from app.ui.settings_dialog import (
    KEY_GUIDE_LINK,
    SECTION_AUTOMATIC,
    SECTION_LOOKS,
    SECTION_SUBTITLES,
    SettingsDialog,
)
from app.ui.show_details import _EpisodeRow
from app.ui.subtitle_settings import (
    CREDIT_TEXT,
    CREDIT_URL,
    SIGNUP_URL,
    UNAVAILABLE_TEXT,
    KEY_ACCEPTED_TEXT,
    KEY_UNCHECKED_TEXT,
    NO_OWN_KEY_TEXT,
    OWN_KEY_HINT,
    OWN_KEY_LABEL,
    OWN_KEY_PLACEHOLDER,
    AccountRow,
    LanguageTags,
    OwnKeyRow,
    account_text,
    own_key_needs_check,
    addable_languages,
    quota_line,
    subtitle_mark,
    subtitle_problem,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class TextTests(unittest.TestCase):
    def test_subtitle_mark(self) -> None:
        self.assertEqual(subtitle_mark({"nl", "en"}, ("en", "nl")), "CC EN NL")
        self.assertEqual(subtitle_mark({"nl", "en"}, ("nl",)), "CC NL EN")
        self.assertEqual(subtitle_mark({"pt-BR", "de"}), "CC DE PT-BR")
        self.assertEqual(subtitle_mark(set()), "")

    def test_quota_line(self) -> None:
        self.assertIsNone(quota_line(SubtitleQuota(), NOW))
        self.assertEqual(quota_line(SubtitleQuota(remaining=18), NOW), "18 subtitle downloads left today")
        self.assertEqual(quota_line(SubtitleQuota(remaining=1), NOW), "1 subtitle download left today")
        later = NOW + timedelta(hours=12)
        text = quota_line(SubtitleQuota(remaining=0, reset_at=later), NOW)
        self.assertEqual(text, f"No subtitle downloads left today; more after {later.astimezone():%H:%M}")
        self.assertEqual(quota_line(SubtitleQuota(remaining=0), NOW), "No subtitle downloads left today")

    def test_account_text(self) -> None:
        account = SubtitleAccount(level="Sub leecher", allowed_downloads=20, remaining_downloads=18)
        self.assertEqual(account_text(account), "✓ Signed in · 20 downloads a day, 18 left today")
        self.assertEqual(account_text(SubtitleAccount()), "✓ Signed in")

    def test_what_save_needs(self) -> None:
        fine: dict[str, Any] = dict(
            enabled=True, folder=r"\\NAS\video", languages=("en",), username="beau", password="pw"
        )
        self.assertIsNone(subtitle_problem(**fine))
        cases: dict[str, dict[str, Any]] = {
            "Choose at least one subtitle language": {"languages": ()},
            "Choose where finished downloads end up, for subtitles": {"folder": " "},
            "Fill in both the OpenSubtitles user name and password, or neither": {"password": ""},
        }
        for message, change in cases.items():
            with self.subTest(message=message):
                self.assertEqual(subtitle_problem(**{**fine, **change}), message)
        off = {**fine, "enabled": False, "folder": "", "languages": (), "username": "", "password": ""}
        self.assertIsNone(subtitle_problem(**off))

    def test_addable_languages(self) -> None:
        offered = addable_languages(["en", "nl"])
        self.assertNotIn("en", offered)
        self.assertEqual(offered[0], "de")
        self.assertEqual(len(offered), len(SUBTITLE_LANGUAGES) - 2)

    def test_the_credit_and_sign_up_links(self) -> None:
        self.assertEqual(CREDIT_TEXT, "Subtitles provided by OpenSubtitles.com")
        self.assertEqual(CREDIT_URL, "https://www.opensubtitles.com")
        self.assertEqual(SIGNUP_URL, "https://www.opensubtitles.com/en/users/sign_up")

    def test_status_bar_text(self) -> None:
        self.assertEqual(
            subtitle_status_text(SubtitleProblem(message=LOGIN_REJECTED_TEXT)),
            f"⚠ Subtitles: {LOGIN_REJECTED_TEXT}",
        )


class SettingsFieldTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults(self) -> None:
        settings = Settings()
        self.assertFalse(settings.subtitles_enabled)
        self.assertEqual(settings.subtitle_languages, ("en",))
        self.assertEqual(settings.subtitles_folder, "")

    def test_languages_are_known_codes_in_their_spelling(self) -> None:
        settings = Settings(subtitle_languages=["NL", "pt-br", "xx", "nl", " en "])
        self.assertEqual(settings.subtitle_languages, ("nl", "pt-BR", "en"))
        self.assertEqual(Settings(subtitle_languages="de, fr").subtitle_languages, ("de", "fr"))

    def test_secrets_are_stored_but_never_shown(self) -> None:
        settings = Settings(
            opensubtitles_password=" pass word ",
            opensubtitles_username=" beau ",
            subtitles_folder=r"  \\NAS\video  ",
        )
        stored = settings.model_dump(mode="json")
        self.assertEqual(stored["opensubtitles_password"], " pass word ")  # never stripped
        self.assertEqual(stored["opensubtitles_username"], "beau")
        self.assertEqual(stored["subtitles_folder"], r"\\NAS\video")
        for text in (repr(settings), str(settings)):
            self.assertNotIn("pass word", text)
        self.assertEqual(Settings.model_validate(stored), settings)

    def test_the_own_key_is_a_stored_secret(self) -> None:
        settings = Settings(opensubtitles_own_key="  OWNKEY  ")
        self.assertEqual(settings.opensubtitles_own_key.get_secret_value(), "OWNKEY")
        self.assertEqual(settings.model_dump(mode="json")["opensubtitles_own_key"], "OWNKEY")
        self.assertNotIn("OWNKEY", repr(settings))
        self.assertEqual(Settings().opensubtitles_own_key.get_secret_value(), "")

    async def test_an_older_builds_key_is_dropped(self) -> None:
        self.assertNotIn("opensubtitles_api_key", Settings.model_fields)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = await Database.open(Path(tmp.name) / "x.db")
        self.addAsyncCleanup(db.close)
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?), (?, ?)",
                ("opensubtitles_api_key", json.dumps("OLD-USER-KEY"), "subtitles_enabled", "true"),
            )
        loaded = await db.settings.load()  # ignored on load, the rest kept
        self.assertTrue(loaded.subtitles_enabled)
        self.assertNotIn("OLD-USER-KEY", repr(loaded.model_dump()))
        await db.settings.save(loaded)  # and gone after the next save
        keys = {row["key"] for row in await db.fetch_all("SELECT key FROM settings")}
        self.assertNotIn("opensubtitles_api_key", keys)
        self.assertIn("subtitles_enabled", keys)


class FakeSubtitles:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.quota = SubtitleQuota(remaining=18)
        self.sign_ins: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.checked: list[str] = []
        self.key_error: Exception | None = None

    async def check_key(self, key: str) -> None:
        self.checked.append(key)
        if self.key_error is not None:
            raise self.key_error

    async def sign_in(self, username: str, password: str) -> SubtitleAccount:
        self.sign_ins.append((username, password))
        if self.error is not None:
            raise self.error
        return SubtitleAccount(level="Sub leecher", allowed_downloads=20, remaining_downloads=18)


class FakeProwlarr:
    async def status(self) -> ProwlarrStatus:
        return ProwlarrStatus(state=ProwlarrState.NOT_INSTALLED)


class FakeShortcut:
    def is_enabled(self) -> bool:
        return False

    async def set_enabled(self, on: bool) -> None:
        pass


class FakeSettingsRepository:
    async def save(self, settings: Settings) -> None:
        pass


class TkTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        self.root.update()


class OwnKeyRowTests(TkTestCase):
    def row(self, subtitles: FakeSubtitles, key: str = "") -> OwnKeyRow:
        row = OwnKeyRow(
            self.root, subtitles=subtitles, key=key, field_width=400  # type: ignore[arg-type]
        )
        self.addCleanup(row.destroy)
        return row

    async def test_check(self) -> None:
        subtitles = FakeSubtitles()
        row = self.row(subtitles)
        row.check()
        self.assertEqual(row.result_text, NO_OWN_KEY_TEXT)
        row.entry.insert(0, "KEY1")
        self.assertTrue(row.needs_check)
        row.check()
        await self.settle()
        self.assertEqual(subtitles.checked, ["KEY1"])
        self.assertEqual(row.result_text, KEY_ACCEPTED_TEXT)
        self.assertFalse(row.needs_check)  # accepted: Save won't ask again

    async def test_refused_and_unreachable(self) -> None:
        subtitles = FakeSubtitles()
        row = self.row(subtitles, key="OLD")
        self.assertFalse(row.needs_check)
        row.entry.delete(0, "end")
        row.entry.insert(0, "SECRETKEY")
        subtitles.key_error = KeyRejected(OWN_KEY_REJECTED_TEXT)
        self.assertEqual(await row.verify(), OWN_KEY_REJECTED_TEXT)
        self.assertNotIn("SECRETKEY", row.result_text)
        subtitles.key_error = Unreachable(UNREACHABLE_TEXT)
        self.assertIsNone(await row.verify())
        self.assertEqual(row.result_text, KEY_UNCHECKED_TEXT)
        self.assertTrue(row.needs_check)

    def test_needs_check(self) -> None:
        self.assertFalse(own_key_needs_check("", None))
        self.assertFalse(own_key_needs_check("  ", "OLD"))
        self.assertTrue(own_key_needs_check("NEW", None))
        self.assertTrue(own_key_needs_check("NEW", "OLD"))
        self.assertFalse(own_key_needs_check(" OLD ", "OLD"))


class LanguageTagTests(TkTestCase):
    async def test_add_and_remove(self) -> None:
        tags = LanguageTags(self.root, ("en",))
        tags.add("nl")
        tags.add("nl")
        tags.add("xx")
        self.assertEqual(tags.languages, ("en", "nl"))
        tags.remove("en")
        self.assertEqual(tags.languages, ("nl",))
        labels = [child.cget("text") for child in tags.winfo_children()]
        self.assertEqual(labels, ["Dutch  ×", "+ Add"])
        tags.destroy()


class AccountRowTests(TkTestCase):
    def row(self, subtitles: FakeSubtitles) -> AccountRow:
        row = AccountRow(
            self.root, subtitles=subtitles, username="beau", password="pw",  # type: ignore[arg-type]
            field_width=400,
        )
        self.addCleanup(row.destroy)
        return row

    async def test_sign_in(self) -> None:
        subtitles = FakeSubtitles()
        row = self.row(subtitles)
        row.sign_in()
        await self.settle()
        self.assertEqual(subtitles.sign_ins, [("beau", "pw")])
        self.assertEqual(row.result_text, "✓ Signed in · 20 downloads a day, 18 left today")

    async def test_wrong_password(self) -> None:
        subtitles = FakeSubtitles()
        subtitles.error = LoginRejected(LOGIN_REJECTED_TEXT)
        row = self.row(subtitles)
        row.sign_in()
        await self.settle()
        self.assertEqual(row.result_text, f"✗ {LOGIN_REJECTED_TEXT}")

    async def test_needs_both_fields(self) -> None:
        subtitles = FakeSubtitles()
        row = self.row(subtitles)
        row.password.delete(0, "end")
        row.sign_in()
        self.assertEqual(row.result_text, "Fill in your OpenSubtitles user name and password")
        self.assertEqual(subtitles.sign_ins, [])


class SettingsSectionTests(TkTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def dialog(
        self, subtitles: FakeSubtitles | None = None, **stored: Any
    ) -> tuple[SettingsDialog, SettingsService]:
        settings = SettingsService(
            FakeSettingsRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder, **stored),
        )
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.0"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            subtitles=subtitles or FakeSubtitles(),  # type: ignore[arg-type]
        )
        self.addCleanup(self._close, dialog)
        return dialog, settings

    @staticmethod
    def _close(dialog: SettingsDialog) -> None:
        if dialog.winfo_exists():
            dialog.destroy()

    @staticmethod
    def texts(widget: Any) -> list[str]:
        found: list[str] = []
        for child in widget.winfo_children():
            if isinstance(child, ctk.CTkLabel):
                found.append(str(child.cget("text")))
            found += SettingsSectionTests.texts(child)
        return found

    async def test_the_rows_show_while_the_switch_is_on(self) -> None:
        dialog, _settings = self.dialog()
        rows = dialog._subtitle_rows
        assert rows is not None
        self.assertFalse(rows.winfo_ismapped() or rows.grid_info())
        dialog._subtitles_on.set(True)
        dialog._show_subtitle_rows()
        self.assertTrue(rows.grid_info())
        self.assertEqual(dialog._quota.cget("text"), "18 subtitle downloads left today")

    async def test_the_choices_are_greyed_while_the_switch_is_off(self) -> None:
        dialog, _settings = self.dialog()
        texts = self.texts(dialog.page(SECTION_SUBTITLES))
        self.assertIn("Get subtitles", texts)
        self.assertIn("AirGrabber finds subtitles for what you download on OpenSubtitles.com.", texts)
        self.assertNotIn("Automatic subtitles", texts)
        self.assertEqual(len(dialog._subtitle_choices), 4)  # When, Hearing impaired, For, Languages
        tags = dialog._language_tags
        assert tags is not None

        def states() -> set[str]:
            found = {str(widget.cget("state")) for widget in tags.winfo_children()}
            for box, _set_enabled in dialog._subtitle_choices[:3]:
                found |= {
                    str(child.cget("state"))
                    for child in box.body.winfo_children()
                    if isinstance(child, ctk.CTkSegmentedButton)
                }
            return found

        self.assertEqual(states(), {"disabled"})
        dialog._subtitles_on.set(True)
        dialog._show_subtitle_rows()
        self.assertEqual(states(), {"normal"})

    async def test_the_credit_line_and_the_sign_up_link(self) -> None:
        dialog, _settings = self.dialog()
        texts = self.texts(dialog.page(SECTION_SUBTITLES))
        self.assertIn(CREDIT_TEXT, texts)
        self.assertIn("Create a free account", texts)
        self.assertNotIn(KEY_GUIDE_LINK, texts)  # the TMDB key has its own page

    async def test_without_a_shipped_key_one_quiet_line(self) -> None:
        dialog, settings = self.dialog(FakeSubtitles(available=False), subtitles_enabled=True)
        texts = self.texts(dialog.page(SECTION_SUBTITLES))
        self.assertIn(UNAVAILABLE_TEXT, texts)
        self.assertNotIn(CREDIT_TEXT, texts)
        self.assertIsNone(dialog._subtitle_rows)
        await dialog._save()  # the stored subtitle settings stay as they are
        self.assertFalse(dialog.winfo_exists())
        self.assertTrue(settings.current.subtitles_enabled)

    async def test_save_stores_the_section(self) -> None:
        dialog, settings = self.dialog()
        dialog._subtitles_on.set(True)
        assert dialog._language_tags is not None and dialog._account is not None
        dialog._language_tags.add("nl")
        dialog._subtitles_folder.set(r"\\NAS\video")
        dialog._account.username.insert(0, " beau ")
        dialog._account.password.insert(0, "pw")
        await dialog._save()
        current = settings.current
        self.assertTrue(current.subtitles_enabled)
        self.assertEqual(current.subtitle_languages, ("en", "nl"))
        self.assertEqual(current.subtitles_folder, r"\\NAS\video")
        self.assertEqual(current.opensubtitles_username, "beau")
        self.assertEqual(current.opensubtitles_password.get_secret_value(), "pw")
        self.assertFalse(dialog.winfo_exists())

    async def test_on_without_an_account_saves(self) -> None:
        dialog, settings = self.dialog(subtitles_folder=r"\\NAS\video")
        dialog._subtitles_on.set(True)
        await dialog._save()
        self.assertFalse(dialog.winfo_exists())
        self.assertTrue(settings.current.subtitles_enabled)

    async def test_the_own_key_row_sits_with_the_account(self) -> None:
        dialog, _settings = self.dialog()
        texts = self.texts(dialog.page(SECTION_SUBTITLES))
        self.assertIn(OWN_KEY_HINT, texts)
        self.assertGreater(texts.index(OWN_KEY_LABEL), texts.index("OpenSubtitles account"))
        assert dialog._own_key is not None
        self.assertEqual(dialog._own_key.entry.cget("placeholder_text"), OWN_KEY_PLACEHOLDER)
        self.assertEqual(dialog._own_key.value, "")

    async def test_save_checks_a_new_own_key_and_stores_it(self) -> None:
        subtitles = FakeSubtitles()
        dialog, settings = self.dialog(subtitles)
        assert dialog._own_key is not None
        dialog._own_key.entry.insert(0, "  MYKEY123  ")
        await dialog._save()
        self.assertEqual(subtitles.checked, ["MYKEY123"])
        self.assertEqual(settings.current.opensubtitles_own_key.get_secret_value(), "MYKEY123")
        self.assertFalse(dialog.winfo_exists())

    async def test_a_refused_own_key_is_not_saved(self) -> None:
        subtitles = FakeSubtitles()
        subtitles.key_error = KeyRejected(OWN_KEY_REJECTED_TEXT)
        dialog, settings = self.dialog(subtitles)
        assert dialog._own_key is not None
        dialog._own_key.entry.insert(0, "BADKEY")
        await dialog._save()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(settings.current.opensubtitles_own_key.get_secret_value(), "")
        error = dialog._error.cget("text")
        self.assertEqual(error, f"{OWN_KEY_LABEL}: {OWN_KEY_REJECTED_TEXT}")
        self.assertNotIn("BADKEY", error)
        self.assertEqual(dialog._own_key.result_text, f"\u2717 {OWN_KEY_REJECTED_TEXT}")
        self.assertTrue(dialog._subtitles_on.get())  # the row shows, so it can be fixed
        self.assertEqual(dialog.section, SECTION_SUBTITLES)

    async def test_an_unreachable_check_still_saves(self) -> None:
        subtitles = FakeSubtitles()
        subtitles.key_error = Unreachable(UNREACHABLE_TEXT)
        dialog, settings = self.dialog(subtitles)
        assert dialog._own_key is not None
        dialog._own_key.entry.insert(0, "MYKEY")
        await dialog._save()
        self.assertEqual(settings.current.opensubtitles_own_key.get_secret_value(), "MYKEY")

    async def test_a_saved_or_empty_key_is_not_checked_again(self) -> None:
        subtitles = FakeSubtitles()
        dialog, settings = self.dialog(subtitles, opensubtitles_own_key="SAVED")
        assert dialog._own_key is not None
        self.assertEqual(dialog._own_key.value, "SAVED")
        await dialog._save()
        self.assertEqual(subtitles.checked, [])
        dialog, settings = self.dialog(subtitles, opensubtitles_own_key="SAVED")
        assert dialog._own_key is not None
        dialog._own_key.entry.delete(0, "end")  # back to AirGrabber's key
        await dialog._save()
        self.assertEqual(subtitles.checked, [])
        self.assertEqual(settings.current.opensubtitles_own_key.get_secret_value(), "")

    async def test_the_card_size_control(self) -> None:
        dialog, settings = self.dialog()
        self.assertEqual(dialog._card_size.get(), "M")  # the default
        texts = self.texts(dialog.page(SECTION_LOOKS))
        self.assertLess(texts.index("Card size"), texts.index("Week starts on"))
        dialog._card_size.set("S")
        await dialog._save()
        self.assertEqual(settings.current.card_size.value, "s")

    async def test_problems_open_the_subtitles_page(self) -> None:
        dialog, _settings = self.dialog()
        dialog._show_section(SECTION_AUTOMATIC)
        dialog._subtitles_on.set(True)  # no folder chosen
        await dialog._save()
        self.assertEqual(dialog.section, SECTION_SUBTITLES)
        self.assertEqual(
            dialog._error.cget("text"), "Choose where finished downloads end up, for subtitles"
        )

    async def test_off_needs_nothing(self) -> None:
        dialog, settings = self.dialog()
        await dialog._save()
        self.assertFalse(dialog.winfo_exists())
        self.assertFalse(settings.current.subtitles_enabled)

    async def test_without_the_service_there_is_no_section(self) -> None:
        settings = SettingsService(FakeSettingsRepository(), Settings(download_dir=self.folder))  # type: ignore[arg-type]
        dialog = SettingsDialog(
            self.root, settings, search=mock.MagicMock(), station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.0"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(), discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
        )
        self.addCleanup(dialog.destroy)
        self.assertIsNone(dialog._subtitle_rows)
        self.assertNotIn(SECTION_SUBTITLES, dialog._items)


class EpisodeRowMarkTests(TkTestCase):
    def row(self, state: str, cc: str) -> _EpisodeRow:
        show = Show(id=7, name="Severance")
        episode = Episode(id=1, show_id=7, season=2, number=4, name="Woe's Hollow", air_date=date(2025, 2, 7))
        entry = CalendarEntry(show=show, episode=episode, is_tracked=True, is_downloaded=state == "downloaded")
        row = _EpisodeRow(
            self.root, entry, state=state, now=NOW,  # type: ignore[arg-type]
            actions=mock.MagicMock(), cc=cc,
        )
        self.addCleanup(row.destroy)
        return row

    async def test_downloaded_rows_show_the_mark(self) -> None:
        row = self.row("downloaded", "CC EN NL")
        self.assertEqual(row.cc, "CC EN NL")
        self.assertEqual(row.cc_control.mark_label.cget("text"), "CC EN NL")  # no CC ▾ here

    async def test_other_rows_never_do(self) -> None:
        self.assertEqual(self.row("downloadable", "CC EN").cc, "")

    async def test_the_mark_follows_the_state(self) -> None:
        row = self.row("downloaded", "")
        self.assertEqual(row.cc, "")
        entry = CalendarEntry(
            show=Show(id=7, name="Severance"),
            episode=Episode(id=1, show_id=7, season=2, number=4, air_date=date(2025, 2, 7)),
            is_tracked=True,
            is_downloaded=True,
        )
        row.set_state(entry, "downloaded", NOW, cc="CC EN")
        self.assertEqual(row.cc, "CC EN")
        row.set_state(entry, "downloaded", NOW, cc="")
        self.assertEqual(row.cc, "")


if __name__ == "__main__":
    unittest.main()
