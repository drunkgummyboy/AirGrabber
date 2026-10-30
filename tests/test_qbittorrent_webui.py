"""qBittorrent's Web UI for AirGrabber (app/services/qbittorrent_webui.py and
QBittorrentSetup.set_up_web_ui): the password's PBKDF2 hash exactly as qBittorrent makes and
checks it, which of the two key sets a file's qBittorrent reads, whose Web UI it is, the port, and
the settings file's edits (both key sets, every other byte kept, one backup, never while
qBittorrent runs, never over the user's own Web UI). Temporary files, fixture files and fake port
checks only: nothing is started and no real settings file is read."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from app.services import qbittorrent_webui as webui
from app.services.qbittorrent_setup import IniKey, QBittorrentSetup, raw_value, set_values
from app.services.qbittorrent_webui import WebUi, WebUiLogin, WebUiResult

FIXTURES = Path(__file__).parent / "fixtures" / "qbittorrent"
HOME = Path(r"C:\Users\Alex")
# qBittorrent 4.2 / 4.3's built-in hash of "adminadmin" (src/base/preferences.cpp
# getWebUIPassword): a hash qBittorrent itself made.
ADMINADMIN = (
    "ARQ77eY1NUZaQsuDHbIMCA==:0WMRkYTUWVT9wVvdDtHAjU9b3b7uB8NR1Gur2hmQCvCDpm39Q+PsJRJPaCU51dEiz+"
    "dTzh8qbPsL8WkFljQYFQ=="
)
SALT = bytes(range(16))
PASSWORD = "s3cret-Pass_word"


def reader(text: str | None) -> webui.Reader:
    def read(section: str, key: str) -> str | None:
        return raw_value(text, IniKey(section, key)) if text is not None else None

    return read


def ini(*lines: str) -> str:
    return "\r\n".join(lines) + "\r\n"


class FakeRunning:
    def __init__(self, running: bool = False) -> None:
        self.running = running

    def __call__(self) -> bool:
        return self.running


class PasswordTests(unittest.TestCase):
    def test_the_hash_is_qbittorrents_own(self) -> None:
        salt = base64.b64decode(ADMINADMIN.split(":")[0])
        self.assertEqual(webui.password_hash("adminadmin", salt), ADMINADMIN)
        self.assertTrue(webui.password_matches(f"@ByteArray({ADMINADMIN})", "adminadmin"))
        self.assertFalse(webui.password_matches(f"@ByteArray({ADMINADMIN})", "adminadmiN"))

    def test_the_format(self) -> None:
        hashed = webui.password_hash(PASSWORD, SALT)
        salt_text, key_text = hashed.split(":")
        self.assertEqual(base64.b64decode(salt_text), SALT)  # 16 bytes
        self.assertEqual(len(base64.b64decode(key_text)), 64)  # PBKDF2-HMAC-SHA512, 64 bytes
        self.assertEqual(webui.PBKDF2_ITERATIONS, 100_000)
        # As QSettings writes a QByteArray; quoted, because base64 ends in "=".
        text = webui.password_text(hashed)
        self.assertEqual(text, f'"@ByteArray({hashed})"')
        stored = raw_value(set_values("", {IniKey("WebUI", "Password_PBKDF2"): text}), IniKey("WebUI", "Password_PBKDF2"))
        self.assertEqual(stored, f"@ByteArray({hashed})")
        self.assertTrue(webui.password_matches(stored, PASSWORD))

    def test_broken_values_never_match(self) -> None:
        for stored in (None, "", "@ByteArray()", "abc", "@ByteArray(a:b:c)", "@ByteArray(!!:??)"):
            with self.subTest(stored=stored):
                self.assertFalse(webui.password_matches(stored, PASSWORD))

    def test_new_passwords_and_salts_are_random(self) -> None:
        self.assertNotEqual(webui.new_password(), webui.new_password())
        self.assertGreaterEqual(len(webui.new_password()), 32)
        self.assertEqual(len(webui.new_salt()), 16)

    def test_the_login_never_shows_its_password(self) -> None:
        login = WebUiLogin(8080, "airgrabber", PASSWORD)
        self.assertNotIn(PASSWORD, repr(login))
        self.assertEqual(login.url, "http://127.0.0.1:8080")


class KeySetTests(unittest.TestCase):
    def test_both_key_sets(self) -> None:
        self.assertEqual(
            webui.keys("Enabled"), (("WebUI", "Enabled"), ("Preferences", "WebUI\\Enabled"))
        )

    def test_before_53s_move_the_old_keys_count(self) -> None:
        text = ini("[Meta]", "MigrationVersion=8", "[Preferences]", "WebUI\\Port=9000", "[WebUI]", "Port=9100")
        self.assertEqual(webui.value(reader(text), "Port"), "9000")
        # Without the old key, the new one (what 5.3's move leaves).
        self.assertEqual(webui.value(reader(ini("[WebUI]", "Port=9100")), "Port"), "9100")

    def test_after_53s_move_the_new_keys_count(self) -> None:
        text = ini("[Meta]", "MigrationVersion=12", "[Preferences]", "WebUI\\Port=9000", "[WebUI]", "Port=9100")
        self.assertEqual(webui.value(reader(text), "Port"), "9100")
        self.assertIsNone(webui.value(reader(ini("[Meta]", "MigrationVersion=11")), "Port"))


class OwnerTests(unittest.TestCase):
    def test_off_is_qbittorrents_default(self) -> None:
        self.assertIs(webui.owner(reader(None)), WebUi.OFF)
        self.assertIs(webui.owner(reader(ini("[Preferences]", "WebUI\\Port=8080"))), WebUi.OFF)
        # Saved by qBittorrent's options with its default account: still off.
        text = ini("[Preferences]", "WebUI\\Enabled=false", "WebUI\\Username=admin",
                   f'WebUI\\Password_PBKDF2="@ByteArray({ADMINADMIN})"')
        self.assertIs(webui.owner(reader(text)), WebUi.OFF)

    def test_the_users_own(self) -> None:
        cases = {
            "on": ini("[Preferences]", "WebUI\\Enabled=true"),
            "on, 5.3": (FIXTURES / "settings_5_3_webui.ini").read_text("utf-8"),
            "on for the network": ini(
                "[Preferences]", "WebUI\\Enabled=true", "WebUI\\Username=airgrabber", "WebUI\\Address=*"
            ),
            "https": ini("[Preferences]", "WebUI\\HTTPS\\Enabled=true"),
            "off with an account": ini(
                "[Preferences]", "WebUI\\Enabled=false", "WebUI\\Username=alex",
                f'WebUI\\Password_PBKDF2="@ByteArray({ADMINADMIN})"',
            ),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertIs(webui.owner(reader(text)), WebUi.USER)

    def test_airgrabbers(self) -> None:
        text = ini("[Preferences]", "WebUI\\Enabled=true", "WebUI\\Address=127.0.0.1", "WebUI\\Username=airgrabber")
        self.assertIs(webui.owner(reader(text)), WebUi.AIRGRABBER)

    def test_the_legal_notice_is_only_read(self) -> None:
        self.assertTrue(webui.legal_notice_accepted(reader(ini("[LegalNotice]", "Accepted=true"))))
        self.assertFalse(webui.legal_notice_accepted(reader(ini("[LegalNotice]", "Accepted=false"))))
        self.assertFalse(webui.legal_notice_accepted(reader(None)))


class PortTests(unittest.TestCase):
    def choose(self, free: set[int], text: str = "", **kwargs: int) -> int:
        return webui.choose_port(reader(text), free.__contains__, lambda: 50123, **kwargs)

    def test_8080_when_free(self) -> None:
        self.assertEqual(self.choose({8080, 8081}), 8080)

    def test_the_next_free_one(self) -> None:
        self.assertEqual(self.choose({8082, 8090}), 8082)

    def test_never_the_torrent_port(self) -> None:
        text = ini("[BitTorrent]", "Session\\Port=8080")
        self.assertEqual(self.choose({8080, 8081}, text), 8081)
        old = ini("[Preferences]", "Connection\\PortRangeMin=8080")
        self.assertEqual(self.choose({8080, 8081}, old), 8081)

    def test_airgrabbers_earlier_port_first(self) -> None:
        self.assertEqual(self.choose({8080, 8095}, preferred=8095), 8095)
        self.assertEqual(self.choose({8080}, preferred=8095), 8080)  # taken now

    def test_else_one_windows_picks(self) -> None:
        self.assertEqual(self.choose(set()), 50123)

    def test_the_real_probe_sees_a_listener(self) -> None:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(webui.port_is_free(port))
        spare = webui.spare_port()
        self.assertTrue(0 < spare < 65536)


class SetUpWebUiTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "Zoë" / "AppData" / "Roaming" / "qBittorrent"
        self.path = self.folder / "qBittorrent.ini"
        self.running = FakeRunning()
        self.setup = QBittorrentSetup(self.path, running=self.running, home=HOME)
        self.free = {8080, 8081}
        self.passwords = iter(["first-password", "second-password"])

    def write(self, data: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)

    def text(self) -> str:
        return self.path.read_bytes().decode("utf-8")

    def set_up(self, known: WebUiLogin | None = None) -> tuple[WebUiResult, WebUiLogin | None]:
        return self.setup.set_up_web_ui(
            known,
            port_free=self.free.__contains__,
            spare_port=lambda: 50123,
            new_password=lambda: next(self.passwords),
            new_salt=lambda: SALT,
        )

    def assert_web_ui(self, text: str, port: int, password: str) -> None:
        read = reader(text)
        for new, old in (webui.keys(name) for name in webui.WRITTEN):
            self.assertEqual(read(*new), read(*old), new)  # both key sets agree
        self.assertEqual(read("WebUI", "Enabled"), "true")
        self.assertEqual(read("WebUI", "Address"), "127.0.0.1")
        self.assertEqual(read("WebUI", "Port"), str(port))
        self.assertEqual(read("WebUI", "Username"), "airgrabber")
        self.assertEqual(read("WebUI", "LocalHostAuth"), "true")
        self.assertTrue(webui.password_matches(read("WebUI", "Password_PBKDF2"), password))
        self.assertIs(webui.owner(read), WebUi.AIRGRABBER)

    def test_a_50_file_gets_its_web_ui_and_keeps_every_other_line(self) -> None:
        original = (FIXTURES / "settings_5.ini").read_bytes()
        self.write(original)
        result, login = self.set_up()
        self.assertIs(result, WebUiResult.CREATED)
        assert login is not None
        self.assertEqual((login.port, login.username, login.password), (8080, "airgrabber", "first-password"))
        text = self.text()
        self.assert_web_ui(text, 8080, "first-password")
        # The existing `WebUI\Port=8080` line changed in place; every other line is as it was.
        before = original.decode("utf-8").split("\r\n")
        written_old = {f"WebUI\\{name}" for name in webui.WRITTEN}
        kept = [line for line in before if line and line.split("=", 1)[0] not in written_old]
        after = text.split("\r\n")
        self.assertEqual([line for line in after if line in kept], kept)
        written = written_old | set(webui.WRITTEN)
        for line in after:
            if line not in before:  # only the Web UI's keys, its section, a blank line
                self.assertTrue(line in ("", "[WebUI]") or line.split("=", 1)[0] in written, line)
        self.assertIn("[WebUI]", after)
        self.assertEqual(self.setup.backup_path.read_bytes(), original)
        self.assertNotIn("first-password", text)  # only its hash

    def test_a_43_file_too(self) -> None:
        self.write((FIXTURES / "settings_4.ini").read_bytes())
        result, login = self.set_up()
        self.assertIs(result, WebUiResult.CREATED)
        assert login is not None
        self.assert_web_ui(self.text(), 8080, login.password)

    def test_a_missing_file_is_made(self) -> None:
        result, login = self.set_up()
        self.assertIs(result, WebUiResult.CREATED)
        assert login is not None
        self.assert_web_ui(self.text(), 8080, login.password)
        self.assertFalse(self.setup.backup_path.exists())  # nothing to back up

    def test_the_port_avoids_8080_when_taken(self) -> None:
        self.free = {8081}
        _result, login = self.set_up()
        assert login is not None
        self.assertEqual(login.port, 8081)
        self.assertEqual(login.url, "http://127.0.0.1:8081")

    def test_kept_when_airgrabber_knows_it(self) -> None:
        _result, login = self.set_up()
        assert login is not None
        before = self.path.read_bytes()
        result, kept = self.set_up(login)
        self.assertIs(result, WebUiResult.KEPT)
        self.assertEqual(kept, login)
        self.assertEqual(self.path.read_bytes(), before)

    def test_airgrabbers_own_without_its_password_is_made_again_on_its_port(self) -> None:
        self.free = {8081}
        _result, first = self.set_up()
        assert first is not None
        self.assertEqual(first.port, 8081)
        self.free = {8080, 8081}
        result, again = self.set_up(None)  # AirGrabber's settings lost the password
        self.assertIs(result, WebUiResult.CREATED)
        assert again is not None
        self.assertEqual(again.port, 8081)  # its earlier port, not 8080
        self.assert_web_ui(self.text(), first.port, "second-password")

    def test_the_users_own_web_ui_is_left_alone(self) -> None:
        original = (FIXTURES / "settings_5_3_webui.ini").read_bytes()
        self.write(original)
        self.assertEqual(self.set_up(), (WebUiResult.USER, None))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.setup.backup_path.exists())

    def test_nothing_changes_while_qbittorrent_runs(self) -> None:
        original = (FIXTURES / "settings_5.ini").read_bytes()
        self.write(original)
        self.running.running = True
        self.assertEqual(self.set_up(), (WebUiResult.RUNNING, None))
        self.assertEqual(self.path.read_bytes(), original)

    def test_a_file_it_cant_write_is_a_failure_not_a_crash(self) -> None:
        self.folder.mkdir(parents=True)
        self.path.mkdir()  # reading a folder fails
        with self.assertLogs("app.services.qbittorrent_setup", "WARNING"):
            self.assertEqual(self.set_up(), (WebUiResult.FAILED, None))

    def test_the_legal_notice_is_read_never_written(self) -> None:
        self.assertFalse(self.setup.legal_notice_accepted())  # no file
        self.write((FIXTURES / "settings_5.ini").read_bytes())
        self.assertTrue(self.setup.legal_notice_accepted())
        self.set_up()
        self.assertEqual(raw_value(self.text(), IniKey("LegalNotice", "Accepted")), "true")
        self.write(b"[Preferences]\r\nGeneral\\Locale=en\r\n")
        self.set_up(None)
        self.assertIsNone(raw_value(self.text(), IniKey("LegalNotice", "Accepted")))


if __name__ == "__main__":
    unittest.main()
