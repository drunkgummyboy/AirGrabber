"""qBittorrent set up for AirGrabber (app/services/qbittorrent_setup.py): the "Add new torrent"
pop-up turned off in its settings file for 4.x and 5.x, only while qBittorrent is closed, every
other byte kept, a backup made once, the save folder read; AirGrabber's look (its theme copied
into a temporary data folder, the two theme keys; a theme of the user's own kept, turned off
stays off); plus TorrentAppService's wrappers and main.py's `--setup-qbittorrent`. Fixture files
in tests/fixtures/qbittorrent/ and temporary folders only; the process check is a fake (the real
one only in a patched smoke test)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from app.models import QBittorrentSetupResult, QBittorrentState, TorrentApp
from app.services import qbittorrent_setup as qs
from app.services.qbittorrent_setup import (
    DIALOG_KEYS,
    EXIT_CODES,
    QUIET_VALUES,
    THEME_ON_KEY,
    THEME_PATH_KEY,
    IniKey,
    Look,
    QBittorrentSetup,
    dialog_off,
    escape,
    look_state,
    look_values,
    qt_bool,
    raw_value,
    run_setup,
    save_path,
    set_values,
    settings_file,
    unescape,
)
from app.services.qbittorrent_theme import SOURCE_DIR, ThemeFolder, shipped_files
from app.services.torrent_app import TorrentAppService

FIXTURES = Path(__file__).parent / "fixtures" / "qbittorrent"
NEW_KEY, OLD_KEY = DIALOG_KEYS
HOME = Path(r"C:\Users\Alex")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeRunning:
    """The process check: answers `running`, counts the calls."""

    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.running


class SetupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A folder name with non-ASCII letters, as a Windows user name can have.
        self.folder = Path(tmp.name) / "Zoë" / "AppData" / "Roaming" / "qBittorrent"
        self.path = self.folder / "qBittorrent.ini"
        self.running = FakeRunning()
        self.setup = QBittorrentSetup(self.path, running=self.running, home=HOME)

    def write(self, data: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)

    def backup(self) -> Path:
        return self.path.with_name("qBittorrent.ini.airgrabber-backup")


class QBittorrent5Tests(SetupTestCase):
    def test_turns_the_pop_up_off_and_keeps_every_other_byte(self) -> None:
        original = fixture("settings_5.ini")
        self.write(original)
        self.assertFalse(self.setup.state().quiet)

        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)

        changed = self.path.read_bytes()
        text = changed.decode("utf-8")
        # Its own section gets the key at its end; the 4.3 key goes into [Preferences].
        self.assertIn(
            "[AddNewTorrentDialog]\r\nDialogSize=@Size(900 620)\r\nRememberLastSavePath=false\r\n"
            "Enabled=false\r\n\r\n[Application]",
            text,
        )
        self.assertTrue(text.endswith("WebUI\\Port=8080\r\nDownloads\\NewAdditionDialog=false\r\n"))
        # Without the two new lines the file is the original, byte for byte.
        lines = changed.split(b"\r\n")
        kept = [line for line in lines if line not in (b"Enabled=false", b"Downloads\\NewAdditionDialog=false")]
        self.assertEqual(b"\r\n".join(kept), original)
        self.assertEqual(len(lines) - len(kept), 2)
        # The legal notice stays as qBittorrent left it; nothing accepts it for the user.
        self.assertEqual(raw_value(text, IniKey("LegalNotice", "Accepted")), "true")
        self.assertEqual(self.backup().read_bytes(), original)
        self.assertEqual(
            self.setup.state(), QBittorrentState(exists=True, quiet=True, save_path="D:\\Torrents")
        )

    def test_a_second_time_changes_nothing(self) -> None:
        original = fixture("settings_5.ini")
        self.write(original)
        self.setup.set_up()
        after_first = self.path.read_bytes()
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.ALREADY)
        self.assertEqual(self.path.read_bytes(), after_first)

    def test_the_backup_is_made_once(self) -> None:
        original = fixture("settings_5.ini")
        self.write(original)
        self.setup.set_up()
        # The user turns the pop-up on again in qBittorrent; setting up again keeps the first
        # backup (the file as it was before AirGrabber ever changed it).
        self.write(self.path.read_bytes().replace(b"Enabled=false", b"Enabled=true"))
        self.assertFalse(self.setup.state().quiet)
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)
        self.assertEqual(self.backup().read_bytes(), original)
        self.assertTrue(self.setup.state().quiet)
        self.assertEqual(self.path.read_bytes().count(b"Enabled=false"), 1)  # replaced, not added

    def test_never_shown_again_in_qbittorrent_counts_as_set_up(self) -> None:
        # qBittorrent 5's "Never show again" writes only the new key.
        self.write(fixture("settings_5.ini").replace(
            b"[AddNewTorrentDialog]\r\n", b"[AddNewTorrentDialog]\r\nEnabled=false\r\n"
        ))  # fmt: skip
        self.assertTrue(self.setup.state().quiet)
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.ALREADY)
        self.assertFalse(self.backup().exists())


class QBittorrent4Tests(SetupTestCase):
    def test_turns_the_old_key_off_in_place(self) -> None:
        original = fixture("settings_4.ini")
        self.write(original)
        self.assertFalse(self.setup.state().quiet)

        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)

        text = self.path.read_bytes().decode("utf-8")
        self.assertIn("Downloads\\NewAdditionDialog=false\r\nDownloads\\NewAdditionDialogFront=true", text)
        self.assertNotIn("NewAdditionDialog=true", text)
        # The new key in its own section at the end, one blank line below the rest.
        self.assertTrue(
            text.endswith("General\\UseRandomPort=false\r\n\r\n[AddNewTorrentDialog]\r\nEnabled=false\r\n")
        )
        before_end = text[: text.index("\r\n[AddNewTorrentDialog]")]
        self.assertEqual(
            before_end.encode("utf-8"),
            original.replace(b"NewAdditionDialog=true", b"NewAdditionDialog=false"),
        )
        self.assertEqual(self.backup().read_bytes(), original)
        # 4.3's save folder, as a Windows path.
        self.assertEqual(self.setup.state().save_path, r"C:\Users\Alex\Downloads")


class FileTests(SetupTestCase):
    def test_a_missing_file_and_folder_are_made(self) -> None:
        self.assertEqual(
            self.setup.state(),
            QBittorrentState(exists=False, quiet=False, save_path=r"C:\Users\Alex\Downloads"),
        )
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)
        self.assertEqual(
            self.path.read_bytes(),
            b"[AddNewTorrentDialog]\r\nEnabled=false\r\n\r\n"
            b"[Preferences]\r\nDownloads\\NewAdditionDialog=false\r\n",
        )
        self.assertFalse(self.backup().exists())  # nothing to back up
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["qBittorrent.ini"])

    def test_nothing_changes_while_qbittorrent_runs(self) -> None:
        original = fixture("settings_5.ini")
        self.write(original)
        self.running.running = True
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.RUNNING)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.backup().exists())
        # A missing file isn't made either.
        self.path.unlink()
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.RUNNING)
        self.assertFalse(self.path.exists())
        # Closed: now it's done.
        self.running.running = False
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)

    def test_unicode_paths_and_bytes_survive(self) -> None:
        # Qt 6 writes UTF-8; Qt 5 wrote \x escapes; a byte that isn't UTF-8 is kept as it was.
        original = (
            "[BitTorrent]\r\nSession\\DefaultSavePath=E:/Séries & Films/Zoë\r\n\r\n"
            "[RSS]\r\nAutoDownloader\\Name=Caf\\xe9\r\nOdd=\udcff\r\n"
        ).encode("utf-8", errors="surrogateescape")
        self.write(original)
        self.assertEqual(self.setup.state().save_path, r"E:\Séries & Films\Zoë")
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)
        changed = self.path.read_bytes()
        self.assertTrue(changed.startswith(original))
        self.assertIn(b"Odd=\xff\r\n", changed)
        self.assertEqual(
            save_path("[Preferences]\nDownloads\\SavePath=C:/Users/Zo\\xeb/Downloads/\n"),
            r"C:\Users\Zoë\Downloads",
        )

    def test_line_endings_and_bom_are_kept(self) -> None:
        self.write(b"\xef\xbb\xbf[Preferences]\nGeneral\\Locale=nl")  # LF, no final line ending
        self.setup.set_up()
        self.assertEqual(
            self.path.read_bytes(),
            b"\xef\xbb\xbf[Preferences]\nGeneral\\Locale=nl\nDownloads\\NewAdditionDialog=false"
            b"\n\n[AddNewTorrentDialog]\nEnabled=false\n",
        )

    def test_a_file_it_cant_write_is_a_failure_not_a_crash(self) -> None:
        self.folder.mkdir(parents=True)
        self.path.mkdir()  # a folder where the file should be
        with self.assertLogs(qs.log, "WARNING"):
            self.assertIs(self.setup.set_up(), QBittorrentSetupResult.FAILED)
        blocked = QBittorrentSetup(
            Path(self.path, "x", "qBittorrent.ini"), running=FakeRunning(), home=HOME
        )
        (self.path / "x").write_bytes(b"")  # a file where its folder should be
        with self.assertLogs(qs.log, "WARNING"):
            self.assertIs(blocked.set_up(), QBittorrentSetupResult.FAILED)

    def test_the_settings_file_is_in_appdata(self) -> None:
        self.assertEqual(
            settings_file(r"C:\Users\Alex\AppData\Roaming"),
            Path(r"C:\Users\Alex\AppData\Roaming\qBittorrent\qBittorrent.ini"),
        )


class IniTextTests(unittest.TestCase):
    def test_dialog_off_rules(self) -> None:
        cases = {
            "": False,  # qBittorrent's default: the dialog shows
            "[AddNewTorrentDialog]\nEnabled=false\n": True,
            "[Preferences]\nDownloads\\NewAdditionDialog=false\n": True,
            "[AddNewTorrentDialog]\nEnabled=true\n": False,
            # Both present: both must say no (each version reads its own).
            "[AddNewTorrentDialog]\nEnabled=false\n[Preferences]\nDownloads\\NewAdditionDialog=true\n": False,
            "[addnewtorrentdialog]\nenabled = FALSE\n": True,  # Windows INI keys ignore case
            "[Preferences]\nDownloads/NewAdditionDialog=false\n": True,  # "/" reads as "\"
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dialog_off(text), expected)

    def test_values_unescape_like_qsettings(self) -> None:
        self.assertEqual(unescape(' "D:/Movies, Series" '), "D:/Movies, Series")
        self.assertEqual(unescape(r"C:\\Temp"), "C:\\Temp")
        self.assertEqual(unescape(r"Zo\xeb\x31"), "Zoë1")
        self.assertEqual(unescape("@@home"), "@home")

    def test_only_the_wanted_lines_change(self) -> None:
        text = "; comment\r\n[A]\r\nx=1\r\n\r\n[B]\r\ny=2\r\n"
        self.assertEqual(
            set_values(text, {IniKey("A", "z"): "3", IniKey("B", "y"): "false"}),
            "; comment\r\n[A]\r\nx=1\r\nz=3\r\n\r\n[B]\r\ny=false\r\n",
        )
        self.assertEqual(set_values("", QUIET_VALUES).count("\r\n"), 5)

    def test_the_save_folder_falls_back_to_downloads(self) -> None:
        self.assertEqual(save_path(None, HOME), r"C:\Users\Alex\Downloads")
        self.assertEqual(save_path("[BitTorrent]\nSession\\DefaultSavePath=\n", HOME), r"C:\Users\Alex\Downloads")
        self.assertEqual(
            save_path("[BitTorrent]\nSession\\DefaultSavePath=//nas/films/Series\n"),
            r"\\nas\films\Series",
        )
        self.assertEqual(raw_value("[A]\nk=1\nk=2\n", IniKey("A", "k")), "2")  # the last one wins


class ProcessCheckTests(unittest.TestCase):
    def test_finds_qbittorrent_by_name(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.object(qs, "_process_names", return_value=["System", "QBITTORRENT.EXE"]):
                self.assertTrue(qs.qbittorrent_running())
            with mock.patch.object(qs, "_process_names", return_value=["System", "explorer.exe"]):
                self.assertFalse(qs.qbittorrent_running())
            # When Windows can't say: better to ask the user to close it than to lose the change.
            with (
                mock.patch.object(qs, "_process_names", side_effect=OSError(5, "denied")),
                self.assertLogs(qs.log, "WARNING"),
            ):
                self.assertTrue(qs.qbittorrent_running())

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_the_real_process_list_can_be_read(self) -> None:
        names = qs._process_names()  # read-only; nothing is started or stopped
        self.assertTrue(any(name.casefold().startswith("python") for name in names))  # this one


class EntryTests(SetupTestCase):
    def test_exit_codes_for_the_installer(self) -> None:
        # install.ps1's Install-QBittorrentIfNeeded reads 0 (ready) and 3 (qBittorrent runs).
        self.assertEqual(run_setup(self.setup), 0)
        self.assertEqual(run_setup(self.setup), 0)  # already
        self.running.running = True
        self.assertEqual(run_setup(self.setup), 3)
        self.assertEqual(EXIT_CODES[QBittorrentSetupResult.FAILED], 1)

    def test_main_runs_only_the_setup(self) -> None:
        """main.py --setup-qbittorrent: no single-instance lock, no window; the setup's exit code.
        AirGrabber's data folder is a temporary one."""
        data = Path(self.folder.parent, "AirGrabberData")
        with (
            mock.patch.dict(os.environ, {"AIRGRABBER_DATA_DIR": str(data)}),
            mock.patch.object(sys, "argv", ["main.py", "--setup-qbittorrent"]),
            mock.patch.object(main, "InstanceLock", side_effect=AssertionError("no lock")),
            mock.patch.object(main, "ensure_requirements", return_value=True),
            mock.patch.object(main, "configure_logging"),
            mock.patch.object(qs, "run_setup", return_value=3) as setup,
            mock.patch.object(main, "_run", side_effect=AssertionError("no app")),
        ):
            self.assertEqual(main.main(), 3)
        setup.assert_called_once_with(data_dir=data)  # the theme's copy goes into the data folder
        self.assertEqual(main.parse_options(["--setup-qbittorrent"]).setup_qbittorrent, True)


class ServiceTests(SetupTestCase):
    def test_the_service_reads_and_sets_up_in_a_thread(self) -> None:
        self.write(fixture("settings_4.ini"))
        service = TorrentAppService(lambda: TorrentApp(name="qBittorrent"), qbittorrent=self.setup)

        async def run() -> tuple[QBittorrentState | None, QBittorrentSetupResult, QBittorrentState | None]:
            before = await service.qbittorrent_state()
            result = await service.set_up_qbittorrent()
            return before, result, await service.qbittorrent_state()

        before, result, after = asyncio.run(run())
        assert before is not None and after is not None
        self.assertFalse(before.quiet)
        self.assertIs(result, QBittorrentSetupResult.DONE)
        self.assertTrue(after.quiet)

    def test_an_unreadable_file_is_no_state(self) -> None:
        self.folder.mkdir(parents=True)
        self.path.mkdir()  # reading a folder fails
        service = TorrentAppService(lambda: None, qbittorrent=self.setup)
        self.assertIsNone(asyncio.run(service.qbittorrent_state()))


# -- AirGrabber's look ----------------------------------------------------------------------------


class ThemeTestCase(SetupTestCase):
    """A setup with AirGrabber's theme, its copy in a temporary data folder (non-ASCII, as a
    Windows user name can be)."""

    def setUp(self) -> None:
        super().setUp()
        self.data = self.folder.parent.parent / "Local" / "AirGrabber"
        self.theme = ThemeFolder.in_data_dir(self.data)
        self.setup = QBittorrentSetup(self.path, running=self.running, home=HOME, theme=self.theme)

    def text(self) -> str:
        return self.path.read_bytes().decode("utf-8")

    def theme_path(self) -> str | None:
        return raw_value(self.text(), THEME_PATH_KEY)

    def theme_on(self) -> str | None:
        return raw_value(self.text(), THEME_ON_KEY)

    def assert_copied(self) -> None:
        for relative in shipped_files():
            self.assertEqual(
                (self.theme.target / relative).read_bytes(), (SOURCE_DIR / relative).read_bytes()
            )


class ThemeSetupTests(ThemeTestCase):
    def test_a_new_qbittorrent_gets_airgrabbers_look(self) -> None:
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)
        self.assert_copied()
        self.assertEqual(self.theme_on(), "true")
        self.assertEqual(self.theme_path(), str(self.theme.config))  # unescaped: Zoë, backslashes
        self.assertIn("\\\\AirGrabber\\\\qbittorrent-theme\\\\config.json\r\n", self.text())
        self.assertIn("Zo\\xeb\\\\", self.text())  # non-ASCII as QSettings' \x escape
        self.assertTrue(self.setup.state().quiet)
        self.assertIs(self.setup.look(), True)
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.ALREADY)

    def test_every_other_byte_stays(self) -> None:
        original = fixture("settings_5.ini")
        self.write(original)
        self.assertIs(self.setup.look(), False)  # qBittorrent's own look
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)
        changed = self.path.read_bytes()
        added = (b"Enabled=false", b"Downloads\\NewAdditionDialog=false",
                 b"General\\UseCustomUITheme=true")  # fmt: skip
        lines = changed.split(b"\r\n")
        kept = [line for line in lines
                if line not in added and not line.startswith(b"General\\CustomUIThemePath=")]  # fmt: skip
        self.assertEqual(b"\r\n".join(kept), original)
        self.assertEqual(len(lines) - len(kept), 4)
        # The theme keys at the end of [Preferences], after the 4.3 dialog key.
        self.assertIn(
            b"WebUI\\Port=8080\r\nDownloads\\NewAdditionDialog=false\r\n"
            b"General\\UseCustomUITheme=true\r\nGeneral\\CustomUIThemePath=",
            changed,
        )
        self.assertTrue(changed.endswith(b"\\\\config.json\r\n"))
        self.assertEqual(self.backup().read_bytes(), original)
        self.assertEqual(raw_value(changed.decode(), IniKey("LegalNotice", "Accepted")), "true")

    def test_the_users_own_theme_stays(self) -> None:
        original = fixture("settings_5_own_theme.ini")
        self.write(original)
        self.assertIsNone(self.setup.look())  # in use: no switch
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)  # the pop-up only
        self.assertEqual(self.theme_path(), "C:\\Users\\Alex\\Themes\\dracula.qbtheme")
        self.assertEqual(self.theme_on(), "true")
        self.assertFalse(self.theme.target.exists())  # nothing copied
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.ALREADY)

    def test_the_users_own_theme_turned_off_is_kept_by_the_setup(self) -> None:
        self.write(fixture("settings_5_own_theme.ini").replace(
            b"UseCustomUITheme=true", b"UseCustomUITheme=false"
        ))  # fmt: skip
        self.assertIs(self.setup.look(), False)  # the switch shows, off
        self.setup.set_up()
        self.assertEqual(self.theme_path(), "C:\\Users\\Alex\\Themes\\dracula.qbtheme")
        # Switching it on in Settings is the user's choice: AirGrabber's look replaces it.
        self.assertIs(self.setup.set_look(True), QBittorrentSetupResult.DONE)
        self.assertEqual(self.theme_path(), str(self.theme.config))
        self.assertEqual(self.theme_on(), "true")
        self.assert_copied()

    def test_turned_off_stays_off(self) -> None:
        self.write(fixture("settings_5.ini"))
        self.setup.set_up()
        self.assertIs(self.setup.set_look(False), QBittorrentSetupResult.DONE)
        self.assertEqual(self.theme_on(), "false")
        self.assertEqual(self.theme_path(), str(self.theme.config))  # kept: "turned off"
        self.assertIs(self.setup.look(), False)
        self.assertIs(self.setup.set_look(False), QBittorrentSetupResult.ALREADY)
        # The user turns the pop-up on again in qBittorrent and sets it up again: still off.
        self.write(self.path.read_bytes().replace(b"Enabled=false", b"Enabled=true"))
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.DONE)
        self.assertEqual(self.theme_on(), "false")
        # On again from the switch.
        self.assertIs(self.setup.set_look(True), QBittorrentSetupResult.DONE)
        self.assertIs(self.setup.look(), True)
        self.assertIs(self.setup.set_look(True), QBittorrentSetupResult.ALREADY)
        self.assertEqual(self.text().count("UseCustomUITheme"), 1)  # replaced, not added

    def test_nothing_changes_while_qbittorrent_runs(self) -> None:
        original = fixture("settings_5.ini")
        self.write(original)
        self.running.running = True
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.RUNNING)
        self.assertIs(self.setup.set_look(True), QBittorrentSetupResult.RUNNING)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.theme.target.exists())
        self.running.running = False
        self.setup.set_up()
        after = self.path.read_bytes()
        self.running.running = True
        self.assertIs(self.setup.set_look(False), QBittorrentSetupResult.RUNNING)
        self.assertEqual(self.path.read_bytes(), after)
        self.assertIs(self.setup.look(), True)  # reading is fine while it runs

    def test_the_backup_is_the_file_before_airgrabber_changed_it(self) -> None:
        original = fixture("settings_5.ini")
        self.write(original)
        self.setup.set_up()
        self.setup.set_look(False)
        self.setup.set_look(True)
        self.assertEqual(self.backup().read_bytes(), original)

    def test_a_deleted_copy_comes_back(self) -> None:
        self.setup.set_up()
        (self.theme.target / "stylesheet.qss").unlink()
        self.assertIs(self.setup.set_up(), QBittorrentSetupResult.ALREADY)  # nothing in the file
        self.assert_copied()
        (self.theme.target / "config.json").write_bytes(b"{}")
        self.assertIs(self.setup.set_look(True), QBittorrentSetupResult.ALREADY)
        self.assert_copied()

    def test_a_failed_copy_doesnt_stop_the_setup(self) -> None:
        empty = Path(self.data, "empty-source")
        empty.mkdir(parents=True)
        setup = QBittorrentSetup(
            self.path, running=self.running, home=HOME, theme=ThemeFolder(self.theme.target, empty)
        )
        with self.assertLogs(qs.log, "WARNING"):
            self.assertIs(setup.set_up(), QBittorrentSetupResult.DONE)
        self.assertTrue(setup.state().quiet)
        self.assertIsNone(self.theme_path())  # no theme pointing at missing files
        with self.assertLogs(qs.log, "WARNING"):
            self.assertIs(setup.set_look(True), QBittorrentSetupResult.FAILED)

    def test_without_a_theme_there_is_no_look(self) -> None:
        plain = QBittorrentSetup(self.path, running=self.running, home=HOME)
        self.assertIsNone(plain.look())
        self.assertIs(plain.set_look(True), QBittorrentSetupResult.FAILED)
        plain.refresh_theme()  # nothing to do
        self.assertIs(plain.set_up(), QBittorrentSetupResult.DONE)
        self.assertIsNone(self.theme_path())

    def test_run_setup_with_the_data_folder(self) -> None:
        """main.py's way in, with the real defaults: only the settings file's place and the
        process list are patched."""
        with (
            mock.patch.object(qs, "settings_file", return_value=self.path),
            mock.patch.object(qs, "_process_names", return_value=["explorer.exe"]),
        ):
            self.assertEqual(run_setup(data_dir=self.data), 0)
        self.assertEqual(self.theme_path(), str(self.theme.config))
        self.assert_copied()


class LookTextTests(unittest.TestCase):
    CONFIG = Path(r"C:\Users\Zoë\AppData\Local\AirGrabber\qbittorrent-theme\config.json")

    def test_values_escape_like_qsettings(self) -> None:
        for value in (
            r"C:\Users\Zoë\AppData\Local\AirGrabber\qbittorrent-theme\config.json",
            r"D:\Mijn spullen, oud\theme\config.json",  # a comma: quoted
            " leading and trailing ",
            '@at "quoted" =',
            "Zoë1 café",
            "tab\there",
        ):
            with self.subTest(value=value):
                self.assertEqual(unescape(escape(value)), value)
        self.assertEqual(escape("C:\\Zoë1"), "C:\\\\Zo\\xeb\\x31")  # a hex digit after an escape
        self.assertEqual(escape("a,b"), '"a,b"')
        self.assertEqual(escape("@x"), "@@x")
        self.assertEqual(escape("\U0001F600"), "\\xd83d\\xde00")  # two UTF-16 units, as QString

    def test_the_look_from_the_settings(self) -> None:
        ours = escape(str(self.CONFIG))
        cases = {
            "": Look.NONE,
            "[Preferences]\nGeneral\\UseCustomUITheme=true\n": Look.NONE,  # no path: not used
            "[Preferences]\nGeneral\\UseCustomUITheme=true\nGeneral\\CustomUIThemePath=\n": Look.NONE,
            f"[Preferences]\nGeneral\\UseCustomUITheme=true\nGeneral\\CustomUIThemePath={ours}\n": Look.ON,
            f"[Preferences]\nGeneral\\CustomUIThemePath={ours}\n": Look.OFF,  # missing = off
            f"[Preferences]\nGeneral\\UseCustomUITheme=false\nGeneral\\CustomUIThemePath={ours}\n": Look.OFF,
            # "/" for "\" and another case: still ours (Windows paths)
            "[preferences]\ngeneral\\usecustomuitheme=true\nGeneral/CustomUIThemePath="
            "c:/users/zo\\xeb/appdata/local/airgrabber/qbittorrent-theme/CONFIG.JSON\n": Look.ON,
            "[Preferences]\nGeneral\\UseCustomUITheme=true\nGeneral\\CustomUIThemePath=D:/t/x.qbtheme\n": Look.OTHER_ON,
            "[Preferences]\nGeneral\\UseCustomUITheme=false\nGeneral\\CustomUIThemePath=D:/t/config.json\n": Look.OTHER_OFF,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIs(look_state(text, self.CONFIG), expected)
        self.assertIs(look_state(None, self.CONFIG), Look.NONE)

    def test_qt_reads_bools_like_this(self) -> None:
        for value, expected in {"true": True, "TRUE": True, "1": True, "yes": True,
                                "false": False, "0": False, "": False, None: False}.items():  # fmt: skip
            with self.subTest(value=value):
                self.assertIs(qt_bool(value), expected)

    def test_the_values_to_write(self) -> None:
        on = look_values(self.CONFIG, True)
        self.assertEqual(on[THEME_ON_KEY], "true")
        self.assertEqual(unescape(on[THEME_PATH_KEY]), str(self.CONFIG))
        self.assertEqual(look_values(self.CONFIG, False), {THEME_ON_KEY: "false"})


class ThemeServiceTests(ThemeTestCase):
    def test_the_service_switches_the_look_in_a_thread(self) -> None:
        self.write(fixture("settings_5.ini"))
        service = TorrentAppService(lambda: TorrentApp(name="qBittorrent"), qbittorrent=self.setup)

        async def run() -> list[object]:
            return [
                await service.qbittorrent_look(),
                await service.set_up_qbittorrent(),
                await service.qbittorrent_look(),
                await service.set_qbittorrent_look(False),
                await service.qbittorrent_look(),
            ]

        self.assertEqual(
            asyncio.run(run()),
            [False, QBittorrentSetupResult.DONE, True, QBittorrentSetupResult.DONE, False],
        )

    def test_an_unreadable_file_has_no_switch(self) -> None:
        self.folder.mkdir(parents=True)
        self.path.mkdir()  # reading a folder fails
        service = TorrentAppService(lambda: None, qbittorrent=self.setup)
        self.assertIsNone(asyncio.run(service.qbittorrent_look()))

    def test_refresh_brings_an_update_to_the_copy(self) -> None:
        service = TorrentAppService(lambda: None, qbittorrent=self.setup)
        asyncio.run(service.refresh_qbittorrent_theme())
        self.assertFalse(self.theme.target.exists())  # never given the look: nothing copied
        self.setup.set_up()
        (self.theme.target / "stylesheet.qss").write_bytes(b"/* an older version */")
        with self.assertLogs("app.services.qbittorrent_theme", "INFO"):
            asyncio.run(service.refresh_qbittorrent_theme())
        self.assert_copied()


if __name__ == "__main__":
    unittest.main()
