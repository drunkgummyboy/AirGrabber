"""Paired phones, the one-time pairing code and the phone connection's certificate."""

from __future__ import annotations

import asyncio
import json
import ssl
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509

from app.config import AppPaths
from app.database import Database
from app.models import PairedPhone, Settings
from app.services import SettingsService
from app.services.backup import replace_changes, settings_for_backup
from app.services.phone.certificate import ensure_certificate
from app.services.phone.devices import DeviceStore, PairingCode

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class DeviceStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = await Database.open(Path(self.tmp.name) / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = await SettingsService.load(self.db.settings)
        self.store = DeviceStore(self.settings)

    async def _saved_text(self) -> str:
        rows = await self.db.fetch_all("SELECT key, value FROM settings")
        return json.dumps({row["key"]: row["value"] for row in rows})

    async def test_token_is_stored_only_as_a_hash(self) -> None:
        phone, token = await self.store.add("Living room phone", 2)
        self.assertGreaterEqual(len(token), 32)
        saved = await self._saved_text()
        self.assertNotIn(token, saved)
        self.assertNotIn(token, repr(phone))
        self.assertIn(phone.token_hash, saved)

    async def test_a_paired_phone_is_saved_with_its_details(self) -> None:
        phone, token = await self.store.add("Living room phone", 2)
        self.assertEqual((phone.name, phone.profile_id), ("Living room phone", 2))
        self.assertEqual(self.store.find(token), phone)
        loaded = await self.db.settings.load()
        self.assertEqual(loaded.phone_devices, (phone,))

    async def test_each_phone_gets_its_own_token_and_id(self) -> None:
        first, first_token = await self.store.add("One", 1)
        second, second_token = await self.store.add("Two", 1)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(self.store.find(first_token), first)
        self.assertEqual(self.store.find(second_token), second)

    async def test_find_refuses_unknown_and_removed(self) -> None:
        kept, kept_token = await self.store.add("Kept", 1)
        phone, token = await self.store.add("Gone", 1)
        self.assertIsNone(self.store.find("not-a-token"))
        self.assertIsNone(self.store.find(""))
        self.assertIsNone(self.store.find(phone.token_hash))  # the hash is not the key
        await self.store.remove(phone.id)
        self.assertIsNone(self.store.find(token))
        self.assertEqual(self.store.find(kept_token), kept)
        self.assertEqual((await self.db.settings.load()).phone_devices, (kept,))

    async def test_removing_an_unknown_phone_changes_nothing(self) -> None:
        phone, _ = await self.store.add("Kept", 1)
        await self.store.remove("nobody")
        self.assertEqual(self.settings.current.phone_devices, (phone,))

    async def test_changes_at_the_same_moment_all_count(self) -> None:
        first, _ = await self.store.add("First", 1)
        await asyncio.gather(
            self.store.add("Second", 1), self.store.remove(first.id), self.store.add("Third", 1)
        )
        names = {phone.name for phone in self.settings.current.phone_devices}
        self.assertEqual(names, {"Second", "Third"})

    async def test_older_settings_load_without_phone_fields(self) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM settings")
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)", ("language", '"nl"')
            )
        loaded = await self.db.settings.load()
        self.assertEqual(loaded.language, "nl")
        self.assertFalse(loaded.phone_connection_on)
        self.assertEqual(loaded.phone_devices, ())


class BackupTests(unittest.TestCase):
    def test_paired_phones_stay_on_this_pc(self) -> None:
        phone = PairedPhone(id="a", name="Phone", token_hash="h", profile_id=1, paired_at=NOW)
        mine = Settings(phone_connection_on=True, phone_devices=(phone,))
        for include_secrets in (False, True):
            kept = settings_for_backup(mine, include_secrets=include_secrets)
            self.assertNotIn("phone_devices", kept)
            self.assertNotIn("phone_connection_on", kept)
        changes = replace_changes(Settings(), mine)
        self.assertNotIn("phone_devices", changes)
        self.assertNotIn("phone_connection_on", changes)


class PairingCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pairing = PairingCode()

    def test_no_code_yet_refuses(self) -> None:
        self.assertFalse(self.pairing.take("anything", NOW))

    def test_code_works_once(self) -> None:
        code = self.pairing.new(NOW)
        self.assertTrue(self.pairing.take(code, NOW))
        self.assertFalse(self.pairing.take(code, NOW))

    def test_a_wrong_code_leaves_the_right_one_working(self) -> None:
        code = self.pairing.new(NOW)
        self.assertFalse(self.pairing.take(code + "x", NOW))
        self.assertFalse(self.pairing.take("", NOW))
        self.assertTrue(self.pairing.take(code, NOW))

    def test_code_expires_after_5_minutes(self) -> None:
        code = self.pairing.new(NOW)
        self.assertTrue(self.pairing.take(code, NOW + timedelta(minutes=4, seconds=59)))
        code = self.pairing.new(NOW)
        self.assertFalse(self.pairing.take(code, NOW + timedelta(minutes=5)))

    def test_new_code_cancels_the_old_one(self) -> None:
        old = self.pairing.new(NOW)
        new = self.pairing.new(NOW)
        self.assertNotEqual(old, new)
        self.assertFalse(self.pairing.take(old, NOW))
        self.assertTrue(self.pairing.take(new, NOW))


class CertificateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = AppPaths(Path(tmp.name))

    def test_certificate_stays_the_same_on_second_call(self) -> None:
        first = ensure_certificate(self.paths)
        second = ensure_certificate(self.paths)
        self.assertEqual(first, second)

    def test_it_lives_in_the_phone_folder_and_a_server_can_use_it(self) -> None:
        cert, key, _ = ensure_certificate(self.paths)
        self.assertEqual(cert.parent, self.paths.root / "phone")
        self.assertEqual(key.parent, self.paths.root / "phone")
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
        self.assertEqual({path.name for path in cert.parent.iterdir()}, {cert.name, key.name})

    def test_it_is_self_signed_for_20_years_with_its_fingerprint(self) -> None:
        cert, _, fingerprint = ensure_certificate(self.paths)
        loaded = x509.load_pem_x509_certificate(cert.read_bytes())
        self.assertEqual(loaded.issuer, loaded.subject)
        lasts = loaded.not_valid_after_utc - loaded.not_valid_before_utc
        self.assertGreaterEqual(lasts, timedelta(days=20 * 365))
        self.assertLess(lasts, timedelta(days=21 * 365))
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

    def test_a_half_made_pair_is_made_again(self) -> None:
        cert, _, fingerprint = ensure_certificate(self.paths)
        cert.unlink()
        new_cert, new_key, new_fingerprint = ensure_certificate(self.paths)
        self.assertNotEqual(new_fingerprint, fingerprint)
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(new_cert, new_key)

    def test_a_damaged_certificate_is_made_again(self) -> None:
        cert, _, _ = ensure_certificate(self.paths)
        cert.write_text("not a certificate", encoding="ascii")
        with self.assertLogs("app.services.phone.certificate", "WARNING"):
            new_cert, new_key, _ = ensure_certificate(self.paths)
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(new_cert, new_key)


if __name__ == "__main__":
    unittest.main()
