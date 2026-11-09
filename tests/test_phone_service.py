"""The phone connection as a whole: pairing, the finder, turning it on and off (for real, on free
ports with a raw TLS client) and its Settings page (on a hidden Tk root)."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app import __version__
from app.config import AppPaths
from app.database import Database
from app.i18n import t
from app.models import ProfileColour, Settings
from app.services import SettingsService
from app.services.backup import settings_for_backup
from app.services.phone.certificate import ensure_certificate
from app.services.phone.server import Handler, Reply, Request
from app.services.phone.service import (
    CERTIFICATE_REMADE_TEXT,
    FINDER_BUSY_TEXT,
    ENABLED_MARKER,
    MAX_PHONE_NAME,
    NEEDS_PLEX_TEXT,
    NO_PLEX_TEXT,
    PACKAGES_MISSING_TEXT,
    PORT_BUSY_TEXT,
    START_FAILED_TEXT,
    PairingUnavailable,
    PhoneService,
    home_only,
)
from app.services.profiles import ProfileService
from app.ui.phone_settings import (
    CODE_EXPIRED_TEXT,
    CONNECT_TEXT,
    DISCONNECT_TEXT,
    FIREWALL_TEXT,
    NO_PHONES_TEXT,
    SCAN_TEXT,
    SWITCH_TEXT,
    PhoneBox,
    code_picture,
)
from app.ui.settings_layout import SettingsPage
from tests.test_backup_dialog import FakeProwlarr, FakeRepository, FakeShortcut, buttons, texts

HOME = "192.168.1.20"
PLEX_TOKEN = "made-up-plex-token"
PLEX = {
    "plex_token": PLEX_TOKEN,
    "plex_server_uri": "https://192-168-1-5.abc.plex.direct:32400",
    "plex_server_id": "a1b2",
    "plex_server_name": "Alex-NAS",
}


def _free_port(kind: socket.SocketKind = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = AppPaths(root=self.root)
        (self.root / ENABLED_MARKER).touch()
        self.db = await Database.open(self.root / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = await SettingsService.load(self.db.settings)
        await self.settings.update(plex_token=PLEX_TOKEN)  # no Plex, no phone app
        self.profiles = ProfileService(self.db.profiles, self.settings, mock.AsyncMock())
        await self.profiles.load()
        self.port = _free_port()
        self.finder_port = _free_port(socket.SOCK_DGRAM)
        self.tls = ssl.create_default_context()
        self.tls.check_hostname = False
        self.tls.verify_mode = ssl.CERT_NONE
        self.service = self.make_service()

    def make_service(
        self, routes: dict[tuple[str, str], Handler] | None = None
    ) -> PhoneService:
        service = PhoneService(
            self.settings,
            self.paths,
            self.profiles,
            routes_factory=lambda: routes or {},
            port=self.port,
            finder_port=self.finder_port,
            addresses=lambda: [HOME],
        )
        self.addAsyncCleanup(service.aclose)
        return service

    async def ask(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port, ssl=self.tls)
        data = b"" if body is None else json.dumps(body).encode()
        head = f"{method} {path} HTTP/1.1\r\nContent-Length: {len(data)}\r\n"
        if token is not None:
            head += f"Authorization: Bearer {token}\r\n"
        writer.write(head.encode() + b"\r\n" + data)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        first, _, rest = raw.partition(b"\r\n\r\n")
        return int(first.split(b" ")[1]), json.loads(rest)

    async def pair(self, name: Any = "Living room phone") -> tuple[int, dict[str, Any]]:
        code = json.loads(self.service.new_pairing_payload())["code"]
        return await self.ask("POST", "/v1/pair", {"code": code, "name": name})

    async def port_is_open(self) -> bool:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", self.port)
        except OSError:
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

    async def run_a_moment(self, service: PhoneService) -> None:
        task = asyncio.create_task(service.run())
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def find(self, question: bytes) -> bytes | None:
        """What the finder answers to `question`, or None when it stays quiet."""
        loop = asyncio.get_running_loop()
        answered: asyncio.Future[bytes] = loop.create_future()

        class Asking(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
                if not answered.done():
                    answered.set_result(data)

        transport, _ = await loop.create_datagram_endpoint(
            Asking, remote_addr=("127.0.0.1", self.finder_port)
        )
        try:
            transport.sendto(question)
            return await asyncio.wait_for(answered, 0.5)
        except TimeoutError:
            return None
        finally:
            transport.close()


class PairingTests(ServiceCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.settings.update(**PLEX)
        await self.service.set_on(True)

    async def test_pair_with_good_code_returns_token_and_plex(self) -> None:
        status, body = await self.pair()
        self.assertEqual(status, 200)
        phone = self.service.phones()[0]
        self.assertEqual(phone.name, "Living room phone")
        self.assertEqual(
            body,
            {
                "token": body["token"],
                "phone_id": phone.id,
                "manager": True,
                "api_version": 1,
                "app_version": __version__,
                "plex": {
                    "uri": PLEX["plex_server_uri"],
                    "token": PLEX_TOKEN,
                    "server_id": "a1b2",
                    "server_name": "Alex-NAS",
                },
            },
        )
        self.assertTrue(body["token"])

    async def test_pair_with_used_code_refused(self) -> None:
        code = json.loads(self.service.new_pairing_payload())["code"]
        first, _ = await self.ask("POST", "/v1/pair", {"code": code, "name": "One"})
        second, body = await self.ask("POST", "/v1/pair", {"code": code, "name": "Two"})
        self.assertEqual(first, 200)
        self.assertEqual(second, 401)
        self.assertEqual(body["error"], "not_paired")
        self.assertIn("new code", body["message"])
        self.assertEqual([phone.name for phone in self.service.phones()], ["One"])

    async def test_pair_with_a_made_up_code_refused(self) -> None:
        self.service.new_pairing_payload()
        status, _ = await self.ask("POST", "/v1/pair", {"code": "guess", "name": "Thief"})
        self.assertEqual(status, 401)
        self.assertEqual(self.service.phones(), ())

    async def test_pair_without_plex_says_null(self) -> None:
        # The code was made while Plex was connected; Plex went before the phone scanned it.
        code = json.loads(self.service.new_pairing_payload())["code"]
        await self.settings.update(plex_token="")
        status, body = await self.ask("POST", "/v1/pair", {"code": code, "name": "Phone"})
        self.assertEqual(status, 200)
        self.assertIsNone(body["plex"])

    async def test_no_code_without_plex(self) -> None:
        await self.settings.update(plex_token="")
        with self.assertRaises(PairingUnavailable) as caught:
            self.service.new_pairing_payload()
        self.assertEqual(str(caught.exception), NEEDS_PLEX_TEXT)

    async def test_pair_before_plex_found_its_server_says_null(self) -> None:
        await self.settings.update(plex_server_uri="")
        _, body = await self.pair()
        self.assertIsNone(body["plex"])

    async def test_pair_with_anything_but_text_is_a_bad_request(self) -> None:
        code = json.loads(self.service.new_pairing_payload())["code"]
        bodies: tuple[dict[str, Any], ...] = (
            {"code": 1, "name": "Phone"},
            {"code": code, "name": 1},
            {"code": [code], "name": "Phone"},
            {"code": code, "name": None},
            {"code": code},
            {"name": "Phone"},
            {},
        )
        for body in bodies:
            with self.subTest(body=body):
                status, answer = await self.ask("POST", "/v1/pair", body)
                self.assertEqual(status, 400)
                self.assertEqual(answer["error"], "bad_request")
        self.assertEqual(self.service.phones(), ())
        status, _ = await self.ask("POST", "/v1/pair", {"code": code, "name": "Phone"})
        self.assertEqual(status, 200)  # none of them used the code up

    async def test_a_phones_name_is_kept_to_a_sensible_size(self) -> None:
        await self.pair("  A\tvery\n" + "long " * 200)
        name = self.service.phones()[0].name
        self.assertLessEqual(len(name), MAX_PHONE_NAME)
        self.assertTrue(name.startswith("A very long"))
        self.assertEqual(name, name.strip())
        self.assertTrue(name.isprintable())

    async def test_a_phone_without_a_name_gets_one(self) -> None:
        await self.pair("  \n ")
        self.assertEqual(self.service.phones()[0].name, "Phone")

    async def test_phone_follows_the_current_profile(self) -> None:
        other = await self.profiles.add("Sam", ProfileColour.GREEN)
        first = self.profiles.current_id
        await self.profiles.switch(other.id)
        await self.pair()
        self.assertEqual(self.service.phones()[0].profile_id, other.id)
        await self.profiles.switch(first)  # the PC moves on; the phone stays with its person
        self.assertEqual(self.service.phones()[0].profile_id, other.id)

    async def test_hello_is_pairs_answer_without_the_token(self) -> None:
        _, paired = await self.pair()
        status, hello = await self.ask("GET", "/v1/hello", token=paired["token"])
        self.assertEqual(status, 200)
        self.assertEqual(hello, {k: v for k, v in paired.items() if k != "token"})

    async def test_hello_brings_the_current_plex_details(self) -> None:
        _, paired = await self.pair()
        await self.settings.update(plex_server_uri="https://new.plex.direct:32400")
        _, hello = await self.ask("GET", "/v1/hello", token=paired["token"])
        self.assertEqual(hello["plex"]["uri"], "https://new.plex.direct:32400")

    async def test_hello_needs_a_paired_phone(self) -> None:
        status, _ = await self.ask("GET", "/v1/hello")
        self.assertEqual(status, 401)

    async def test_disconnect_stops_the_phone_at_once(self) -> None:
        _, paired = await self.pair()
        await self.service.disconnect(paired["phone_id"])
        self.assertEqual(self.service.phones(), ())
        status, body = await self.ask("GET", "/v1/hello", token=paired["token"])
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "not_paired")

    async def test_the_code_holds_what_the_phone_needs_and_no_secret(self) -> None:
        text = self.service.new_pairing_payload()
        payload = json.loads(text)
        fingerprint = ensure_certificate(self.paths)[2]
        self.assertEqual(
            payload,
            {
                "v": 1,
                "id": self.settings.current.phone_pc_id,
                "hosts": [HOME],
                "port": self.port,
                "fp": fingerprint,
                "code": payload["code"],
            },
        )
        self.assertNotIn(PLEX_TOKEN, text)

    async def test_a_new_code_cancels_the_one_before(self) -> None:
        old = json.loads(self.service.new_pairing_payload())["code"]
        self.service.new_pairing_payload()
        status, _ = await self.ask("POST", "/v1/pair", {"code": old, "name": "Phone"})
        self.assertEqual(status, 401)

    async def test_nothing_secret_reaches_the_log(self) -> None:
        with self.assertLogs("app.services.phone", logging.DEBUG) as logs:
            logging.getLogger("app.services.phone").debug("start")
            code = json.loads(self.service.new_pairing_payload())["code"]
            _, body = await self.ask("POST", "/v1/pair", {"code": code, "name": "Phone"})
            await self.ask("GET", "/v1/hello", token=body["token"])
        everything = "\n".join(logs.output)
        for secret in (code, body["token"], PLEX_TOKEN):
            self.assertNotIn(secret, everything)

    async def test_later_tasks_routes_are_served(self) -> None:
        seen: list[Request] = []

        async def handler(request: Request) -> Reply:
            seen.append(request)
            return Reply(200, {"ok": True})

        await self.service.set_on(False)
        service = self.make_service({("GET", "/v1/thing"): handler})
        await service.set_on(True)
        self.service = service
        _, paired = await self.pair()
        status, _ = await self.ask("GET", "/v1/thing", token=paired["token"])
        self.assertEqual(status, 200)
        self.assertEqual(seen[0].phone, service.phones()[0])

    async def test_without_plex_every_route_but_hello_says_so(self) -> None:
        async def handler(request: Request) -> Reply:
            return Reply(200, {"ok": True})

        await self.service.set_on(False)
        service = self.make_service({("GET", "/v1/thing"): handler})
        await service.set_on(True)
        self.service = service
        _, paired = await self.pair()
        await self.settings.update(plex_token="")
        status, body = await self.ask("GET", "/v1/thing", token=paired["token"])
        self.assertEqual(
            (status, body), (409, {"error": "no_plex", "message": NO_PLEX_TEXT})
        )
        status, hello = await self.ask("GET", "/v1/hello", token=paired["token"])
        self.assertEqual(status, 200)
        self.assertIsNone(hello["plex"])
        await self.settings.update(plex_token=PLEX_TOKEN)
        status, _ = await self.ask("GET", "/v1/thing", token=paired["token"])
        self.assertEqual(status, 200)


class HomeOnlyTests(unittest.TestCase):
    def test_only_addresses_a_phone_at_home_can_reach(self) -> None:
        self.assertEqual(
            home_only(
                [
                    "192.168.1.5",
                    "127.0.0.1",
                    "8.8.8.8",
                    "169.254.10.10",
                    "10.0.0.2",
                    "172.16.3.4",
                    "172.32.0.1",
                    "192.168.1.5",
                    "not an address",
                ]
            ),
            ["192.168.1.5", "10.0.0.2", "172.16.3.4"],
        )


class FinderTests(ServiceCase):
    async def test_finder_answers_only_its_own_id(self) -> None:
        await self.service.set_on(True)
        pc_id = self.settings.current.phone_pc_id
        self.assertTrue(pc_id)
        self.assertEqual(
            await self.find(f"AGC? {pc_id}".encode()),
            f"AGC! {pc_id} {self.port}".encode(),
        )
        self.assertIsNone(await self.find(b"AGC? another-pc"))
        self.assertIsNone(await self.find(f"AGC? {pc_id} ".encode()))
        self.assertIsNone(await self.find(b"\xff\xfe"))

    async def test_the_finder_stops_with_the_connection(self) -> None:
        await self.service.set_on(True)
        pc_id = self.settings.current.phone_pc_id
        await self.service.set_on(False)
        with contextlib.suppress(ConnectionError):
            self.assertIsNone(await self.find(f"AGC? {pc_id}".encode()))

    async def test_pc_id_is_kept_over_a_restart(self) -> None:
        await self.service.set_on(True)
        pc_id = self.settings.current.phone_pc_id
        await self.service.set_on(False)
        await self.service.set_on(True)
        self.assertEqual(self.settings.current.phone_pc_id, pc_id)

    async def test_a_finder_port_in_use_is_said_and_the_connection_still_works(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
            taken.bind(("0.0.0.0", self.finder_port))
            await self.service.set_on(True)
            self.assertTrue(self.service.running)
            self.assertEqual(self.service.problem, FINDER_BUSY_TEXT)
        self.assertTrue(await self.port_is_open())


class OnOffTests(ServiceCase):
    async def test_off_means_port_closed(self) -> None:
        await self.run_a_moment(self.service)
        self.assertFalse(self.service.running)
        self.assertFalse(await self.port_is_open())
        await self.service.set_on(True)
        self.assertTrue(await self.port_is_open())
        await self.service.set_on(False)
        self.assertFalse(await self.port_is_open())
        self.assertFalse(self.settings.current.phone_connection_on)

    async def test_off_makes_nothing(self) -> None:
        await self.run_a_moment(self.service)
        self.assertFalse((self.root / "phone").exists())
        self.assertEqual(self.settings.current.phone_pc_id, "")

    async def test_port_in_use_sets_a_plain_problem_and_keeps_the_app_running(self) -> None:
        with socket.socket() as taken:
            taken.bind(("0.0.0.0", self.port))
            taken.listen()
            await self.service.set_on(True)  # doesn't raise
            self.assertEqual(self.service.problem, PORT_BUSY_TEXT)
            self.assertFalse(self.service.running)
            self.assertTrue(self.settings.current.phone_connection_on)
            with self.assertRaises(PairingUnavailable):
                self.service.new_pairing_payload()

    async def test_a_freed_port_is_taken_up_by_itself(self) -> None:
        with socket.socket() as taken:
            taken.bind(("0.0.0.0", self.port))
            taken.listen()
            await self.service.set_on(True)
            task = asyncio.create_task(self.service.run(retry_s=0.01))
            self.addCleanup(task.cancel)
            await asyncio.sleep(0.2)
            self.assertFalse(self.service.running)
        for _ in range(300):
            if self.service.problem is None:  # set once the finder is up too
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.service.running)
        self.assertIsNone(self.service.problem)

    async def test_a_certificate_that_cant_be_made_is_said(self) -> None:
        (self.root / "phone").write_text("a file where the folder goes")
        await self.service.set_on(True)
        self.assertEqual(self.service.problem, START_FAILED_TEXT)
        self.assertFalse(self.service.running)

    async def test_turning_off_clears_the_problem_and_the_code(self) -> None:
        with socket.socket() as taken:
            taken.bind(("0.0.0.0", self.port))
            taken.listen()
            await self.service.set_on(True)
            self.assertIsNotNone(self.service.problem)
            await self.service.set_on(False)
            self.assertIsNone(self.service.problem)
        await self.service.set_on(True)
        code = json.loads(self.service.new_pairing_payload())["code"]
        await self.service.set_on(False)
        with self.assertRaises(PairingUnavailable):
            self.service.new_pairing_payload()
        await self.service.set_on(True)
        status, _ = await self.ask("POST", "/v1/pair", {"code": code, "name": "Phone"})
        self.assertEqual(status, 401)  # a code shown before doesn't outlive the connection

    async def test_settings_off_stays_off_after_restart(self) -> None:
        await self.service.set_on(True)
        await self.service.set_on(False)
        await self.service.aclose()
        self.settings = await SettingsService.load(self.db.settings)
        self.assertFalse(self.settings.current.phone_connection_on)
        again = self.make_service()
        await self.run_a_moment(again)
        self.assertFalse(again.running)
        self.assertFalse(await self.port_is_open())

    async def test_settings_on_comes_back_on_after_restart(self) -> None:
        await self.service.set_on(True)
        await self.service.aclose()
        self.settings = await SettingsService.load(self.db.settings)
        again = self.make_service()
        await self.run_a_moment(again)
        self.assertTrue(again.running)
        self.assertTrue(await self.port_is_open())

    async def test_a_key_gone_bad_heals_on_the_next_try(self) -> None:
        await self.service.set_on(True)
        await self.pair_a_phone()
        await self.service.set_on(False)
        (self.root / "phone" / "key.pem").write_text("damaged")
        await self.service.set_on(True)
        self.assertEqual(self.service.problem, START_FAILED_TEXT)
        self.assertFalse(self.service.running)
        await self.service.set_on(True)  # what `run` does by itself
        self.assertIsNone(self.service.problem)
        self.assertTrue(self.service.running)
        self.assertEqual(self.service.notice, CERTIFICATE_REMADE_TEXT)

    async def pair_a_phone(self) -> None:
        status, _ = await self.pair()
        self.assertEqual(status, 200)

    async def test_identity_stays_out_of_a_backup(self) -> None:
        await self.service.set_on(True)
        saved = settings_for_backup(self.settings.current, include_secrets=True)
        self.assertNotIn("phone_pc_id", saved)
        self.assertNotIn("phone_fingerprint", saved)

    async def test_nothing_starts_after_closing(self) -> None:
        await self.service.aclose()
        await self.service.set_on(True)
        self.assertFalse(self.service.running)
        self.assertFalse(await self.port_is_open())

    async def test_no_home_network_says_so(self) -> None:
        service = PhoneService(
            self.settings,
            self.paths,
            self.profiles,
            routes_factory=dict,
            port=self.port,
            finder_port=self.finder_port,
            addresses=lambda: [],
        )
        self.addAsyncCleanup(service.aclose)
        await service.set_on(True)
        with self.assertRaises(PairingUnavailable) as caught:
            service.new_pairing_payload()
        self.assertIn("home network", str(caught.exception))


class ForTheOwnerOnlyTests(ServiceCase):
    """Without the marker in the data folder the connection doesn't exist; see OPEN_TO_EVERYONE."""

    def without_the_marker(self) -> PhoneService:
        (self.root / ENABLED_MARKER).unlink()
        return self.make_service()

    async def test_without_the_marker_nothing_listens_even_when_switched_on(self) -> None:
        await self.settings.update(phone_connection_on=True)
        service = self.without_the_marker()
        self.assertFalse(service.available)
        await asyncio.wait_for(service.run(), 1)  # it doesn't stay around either
        self.assertFalse(service.running)
        self.assertFalse(await self.port_is_open())
        self.assertIsNone(await self.find(b"AGC? anything"))
        self.assertFalse((self.root / "phone").exists())  # no certificate made
        self.assertEqual(self.settings.current.phone_pc_id, "")

    async def test_without_the_marker_switching_on_starts_nothing(self) -> None:
        service = self.without_the_marker()
        await service.set_on(True)
        self.assertFalse(service.running)
        self.assertFalse(await self.port_is_open())
        self.assertIsNone(service.problem)

    async def test_with_the_marker_it_starts_when_switched_on(self) -> None:
        await self.settings.update(phone_connection_on=True)
        self.assertTrue(self.service.available)
        await self.run_a_moment(self.service)
        self.assertTrue(self.service.running)
        self.assertTrue(await self.port_is_open())

    async def test_a_folder_of_that_name_is_no_marker(self) -> None:
        (self.root / ENABLED_MARKER).unlink()
        (self.root / ENABLED_MARKER).mkdir()
        self.assertFalse(self.make_service().available)

    async def test_one_constant_opens_it_to_everyone(self) -> None:
        (self.root / ENABLED_MARKER).unlink()
        with mock.patch("app.services.phone.service.OPEN_TO_EVERYONE", True):
            service = self.make_service()
        self.assertTrue(service.available)
        await service.set_on(True)
        self.assertTrue(service.running)


class WithoutItsPackagesTests(ServiceCase):
    """cryptography and segno are optional: the rest of AirGrabber never needs them."""

    async def test_airgrabber_loads_without_them(self) -> None:
        code = (
            "import sys; sys.modules['cryptography'] = None; sys.modules['segno'] = None; "
            "import app.services, app.ui.phone_settings, app.ui.settings_dialog, "
            "app.ui.main_window"
        )
        done = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, "AIRGRABBER_DATA_DIR": str(self.root)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr)

    async def test_without_cryptography_it_says_so(self) -> None:
        with mock.patch.dict(sys.modules, {"cryptography": None}):
            await self.service.set_on(True)
        self.assertFalse(self.service.running)
        self.assertFalse(await self.port_is_open())
        self.assertEqual(self.service.problem, t(PACKAGES_MISSING_TEXT))


class CertificateRemadeTests(ServiceCase):
    def remake(self) -> None:
        for name in ("certificate.pem", "key.pem"):
            (self.root / "phone" / name).unlink()

    async def test_no_notice_for_a_first_start_or_a_kept_certificate(self) -> None:
        await self.service.set_on(True)
        self.assertIsNone(self.service.notice)
        await self.pair()
        await self.service.set_on(False)
        await self.service.set_on(True)
        self.assertIsNone(self.service.notice)

    async def test_a_remade_certificate_is_said_until_a_phone_connects_again(self) -> None:
        await self.service.set_on(True)
        await self.pair("Old phone")
        await self.service.set_on(False)
        self.remake()
        await self.service.set_on(True)
        self.assertEqual(self.service.notice, CERTIFICATE_REMADE_TEXT)
        status, _ = await self.pair("Same phone again")
        self.assertEqual(status, 200)
        self.assertIsNone(self.service.notice)

    async def test_the_notice_survives_a_restart(self) -> None:
        await self.service.set_on(True)
        await self.pair()
        await self.service.set_on(False)
        self.remake()
        await self.service.set_on(True)
        await self.service.set_on(False)
        await self.service.set_on(True)
        self.assertEqual(self.service.notice, CERTIFICATE_REMADE_TEXT)

    async def test_disconnecting_the_old_phones_ends_the_notice(self) -> None:
        await self.service.set_on(True)
        _, paired = await self.pair()
        await self.service.set_on(False)
        self.remake()
        await self.service.set_on(True)
        await self.service.disconnect(paired["phone_id"])
        self.assertIsNone(self.service.notice)

    async def test_without_phones_a_new_certificate_needs_no_notice(self) -> None:
        await self.service.set_on(True)
        await self.service.set_on(False)
        self.remake()
        await self.service.set_on(True)
        self.assertIsNone(self.service.notice)


# -- the page -----------------------------------------------------------------------------------


class PageCase(ServiceCase):
    def setUp(self) -> None:
        try:
            self.window = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.window.withdraw()
        self.addCleanup(self._destroy_window)

    def _destroy_window(self) -> None:
        for job in self.window.tk.splitlist(self.window.tk.call("after", "info")):
            self.window.after_cancel(job)
        self.window.destroy()
        gc.collect()

    async def settle(self) -> None:
        for _ in range(8):
            await asyncio.sleep(0.01)
            self.window.update()

    def box(self, **kwargs: Any) -> tuple[PhoneBox, SettingsPage]:
        page = SettingsPage(self.window, "Phone connection", "note")
        page.frame.pack()
        return PhoneBox(page, self.service, **kwargs), page

    def shown(self, page: SettingsPage) -> list[str]:
        """The texts on the page right now (what grid_remove hid doesn't count)."""
        self.window.update_idletasks()
        found: list[str] = []

        def walk(widget: Any) -> None:
            for child in widget.winfo_children():
                if child.winfo_manager() == "":
                    continue
                if isinstance(child, ctk.CTkLabel):
                    found.append(str(child.cget("text")))
                walk(child)

        walk(page.frame)
        return found

    def shown_buttons(self, page: SettingsPage) -> dict[str, ctk.CTkButton]:
        return {
            name: button
            for name, button in buttons(page.frame).items()
            if button.winfo_manager() != ""
        }


class PageTests(PageCase):
    async def test_without_plex_the_page_is_one_line_and_follows_plex(self) -> None:
        await self.service.set_on(True)
        await self.settings.update(plex_token="")
        box, page = self.box()
        heading = self.shown(page)[:2]  # the page's own title and note
        self.assertEqual(self.shown(page)[2:], [NEEDS_PLEX_TEXT])
        self.assertEqual(self.shown_buttons(page), {})
        await self.settings.update(plex_token=PLEX_TOKEN)
        self.assertNotIn(NEEDS_PLEX_TEXT, self.shown(page))
        self.assertIn(CONNECT_TEXT, self.shown_buttons(page))
        box.show_code()
        await self.settings.update(plex_token="")  # disconnected with a code on screen
        self.assertEqual(self.shown(page), [*heading, NEEDS_PLEX_TEXT])  # the code too is gone

    async def test_off_shows_the_switch_and_the_firewall_line(self) -> None:
        box, page = self.box()
        self.assertEqual(str(box.switch.cget("text")), SWITCH_TEXT)
        self.assertEqual(box.switch.get(), 0)
        self.assertIn(FIREWALL_TEXT, self.shown(page))
        self.assertNotIn(CONNECT_TEXT, self.shown_buttons(page))

    async def test_the_firewall_line_is_only_for_the_first_time(self) -> None:
        await self.service.set_on(True)
        await self.service.set_on(False)
        _, page = self.box()
        self.assertNotIn(FIREWALL_TEXT, self.shown(page))

    async def test_the_switch_turns_it_on_and_off(self) -> None:
        box, page = self.box()
        box.switch.toggle()
        await box.settled()
        self.assertTrue(self.settings.current.phone_connection_on)
        self.assertTrue(self.service.running)
        self.assertIn(CONNECT_TEXT, self.shown_buttons(page))
        box.switch.toggle()
        await box.settled()
        self.assertFalse(self.service.running)
        self.assertNotIn(CONNECT_TEXT, self.shown_buttons(page))

    async def test_connect_a_phone_shows_the_code_until_the_phone_pairs(self) -> None:
        await self.service.set_on(True)
        box, page = self.box()
        self.assertNotIn(SCAN_TEXT, self.shown(page))
        self.assertIn(NO_PHONES_TEXT, self.shown(page))
        self.shown_buttons(page)[CONNECT_TEXT].invoke()
        self.assertIn(SCAN_TEXT, self.shown(page))
        self.assertIsNotNone(box.code_image)

        payload = self.service.new_pairing_payload()
        await self.ask(
            "POST", "/v1/pair", {"code": json.loads(payload)["code"], "name": "Sam's phone"}
        )
        await self.settle()
        self.assertNotIn(SCAN_TEXT, self.shown(page))
        self.assertIn("Sam's phone", self.shown(page))
        self.assertNotIn(NO_PHONES_TEXT, self.shown(page))

    async def test_a_code_goes_away_when_it_expires(self) -> None:
        await self.service.set_on(True)
        _, page = self.box(code_seconds=0.01)
        self.shown_buttons(page)[CONNECT_TEXT].invoke()
        self.assertIn(SCAN_TEXT, self.shown(page))
        await asyncio.sleep(0.05)
        self.assertNotIn(SCAN_TEXT, self.shown(page))
        self.assertIn(CODE_EXPIRED_TEXT, self.shown(page))

    async def test_disconnect_takes_a_phone_off_the_list(self) -> None:
        await self.service.set_on(True)
        await self.pair("Sam's phone")
        box, page = self.box()
        self.assertIn("Sam's phone", self.shown(page))
        self.shown_buttons(page)[DISCONNECT_TEXT].invoke()
        await box.settled()
        await self.settle()
        self.assertEqual(self.service.phones(), ())
        self.assertNotIn("Sam's phone", self.shown(page))

    async def test_a_problem_shows_plainly(self) -> None:
        with socket.socket() as taken:
            taken.bind(("0.0.0.0", self.port))
            taken.listen()
            await self.service.set_on(True)
            _, page = self.box()
            self.assertIn(PORT_BUSY_TEXT, self.shown(page))
            self.assertNotIn(CONNECT_TEXT, self.shown_buttons(page))

    async def test_a_remade_certificate_shows_plainly(self) -> None:
        await self.service.set_on(True)
        await self.pair()
        await self.service.set_on(False)
        for name in ("certificate.pem", "key.pem"):
            (self.root / "phone" / name).unlink()
        await self.service.set_on(True)
        _, page = self.box()
        self.assertIn(CERTIFICATE_REMADE_TEXT, self.shown(page))

    async def test_a_code_that_cant_be_made_says_why(self) -> None:
        await self.service.set_on(True)
        _, page = self.box()
        with mock.patch.object(
            self.service, "new_pairing_payload", side_effect=PairingUnavailable("Why not.")
        ):
            self.shown_buttons(page)[CONNECT_TEXT].invoke()
        self.assertIn("Why not.", self.shown(page))
        self.assertNotIn(SCAN_TEXT, self.shown(page))

    async def test_the_page_says_so_when_cryptography_is_missing(self) -> None:
        with mock.patch.dict(sys.modules, {"cryptography": None}):
            await self.service.set_on(True)
        _, page = self.box()
        self.assertIn(PACKAGES_MISSING_TEXT, self.shown(page))
        self.assertNotIn(CONNECT_TEXT, self.shown_buttons(page))

    async def test_the_page_says_so_when_segno_is_missing(self) -> None:
        await self.service.set_on(True)
        _, page = self.box()
        with mock.patch.dict(sys.modules, {"segno": None}):
            self.shown_buttons(page)[CONNECT_TEXT].invoke()
        self.assertIn(PACKAGES_MISSING_TEXT, self.shown(page))
        self.assertNotIn(SCAN_TEXT, self.shown(page))

    async def test_the_box_stops_listening_when_it_goes(self) -> None:
        box, page = self.box()
        self.assertTrue(box.listening)
        page.frame.destroy()
        await self.settle()
        self.assertFalse(box.listening)
        await self.service.set_on(True)  # would touch a dead widget

    def settings_window(self, phone: PhoneService) -> Any:
        from app.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(
            self.window,
            SettingsService(FakeRepository(), Settings()),  # type: ignore[arg-type]
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=cast(Any, SimpleNamespace(current="3.1.0", previous_version=None)),
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=cast(Any, FakeProwlarr()),
            startup_shortcut=FakeShortcut(),
            phone=phone,
        )
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        return dialog

    async def test_the_settings_window_has_the_page(self) -> None:
        from app.ui.settings_dialog import SECTION_PHONE

        dialog = self.settings_window(self.service)
        self.assertIn(SECTION_PHONE, dialog.sections)
        self.assertIn(FIREWALL_TEXT, texts(dialog.page(SECTION_PHONE)))

    async def test_without_the_marker_the_settings_window_has_no_page(self) -> None:
        from app.ui.settings_dialog import SECTION_PHONE

        (self.root / ENABLED_MARKER).unlink()
        dialog = self.settings_window(self.make_service())
        self.assertNotIn(SECTION_PHONE, dialog.sections)
        self.assertNotIn(SECTION_PHONE, dialog._items)
        self.assertNotIn(SECTION_PHONE, texts(dialog._sidebar))
        self.assertIsNone(dialog.phone_box)


class CodePictureTests(unittest.TestCase):
    def test_the_picture_is_square_black_on_white_with_a_quiet_edge(self) -> None:
        picture = code_picture('{"v":1,"code":"abc"}', 300)
        width, height = picture.size
        self.assertEqual(width, height)
        self.assertLessEqual(width, 300)
        self.assertGreater(width, 150)
        self.assertEqual(picture.getpixel((0, 0)), 255)  # white: the edge a scanner needs
        self.assertEqual(set(picture.tobytes()), {0, 255})  # black too, nothing between


if __name__ == "__main__":
    unittest.main()
