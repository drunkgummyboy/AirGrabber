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
from app.services.phone.firewall import NetworkCheck, NetworkFacts
from app.services.phone.companion_app import PHONE_APP_URL
from app.services.phone.server import PORTS, Handler, Reply, Request, is_home_address
from app.services.phone.service import (
    CERTIFICATE_REMADE_TEXT,
    FINDER_BUSY_TEXT,
    FINDER_PORTS,
    FIREWALL_BLOCKS_TEXT,
    PUBLIC_NETWORK_TEXT,
    ENABLED_MARKER,
    MAX_PHONE_NAME,
    NEEDS_LIBRARY_TEXT,
    NO_LIBRARY_TEXT,
    NO_PLEX_TEXT,
    PACKAGES_MISSING_TEXT,
    PORT_BUSY_TEXT,
    START_FAILED_TEXT,
    PairingUnavailable,
    PhoneService,
    home_only,
    ranked_addresses,
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
        self.network: NetworkFacts | None = None  # what the fake network check says
        self.network_checks = 0
        self.service = self.make_service()

    async def fake_network_check(self) -> NetworkFacts | None:
        self.network_checks += 1
        return self.network

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
            network_check=self.fake_network_check,
        )
        self.addAsyncCleanup(service.aclose)
        return service

    async def ask(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port, ssl=self.tls)
        data = b"" if body is None else json.dumps(body).encode()
        head = f"{method} {path} HTTP/1.1\r\nContent-Length: {len(data)}\r\n"
        if token is not None:
            head += f"Authorization: Bearer {token}\r\n"
        for name, value in (headers or {}).items():
            head += f"{name}: {value}\r\n"
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
                "library": "plex",
                "folders": False,  # the folders aren't on: no Downloaded shelf
                "phone_copies": False,  # no copy maker here
                "find": True,  # this PC can search (`/v1/find`)
                "person": True,  # and has the actor page (`/v1/person`) and `progress`
                "title": True,  # and the title page (`/v1/title`)
                "cast_sound": False,  # no Cast sound streams here
                # Where the phone checks for, and fetches, a newer Companion.
                "app_update": {
                    "version_url": (
                        "https://github.com/drunkgummyboy/AirGrabber/releases/download/"
                        "phone-app/version.json"
                    ),
                    "apk_url": PHONE_APP_URL,
                },
                # No "play_port": no play link server here (an older PC's answer: 58762).
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
        self.assertEqual(str(caught.exception), NEEDS_LIBRARY_TEXT)

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

    async def test_a_shared_servers_own_token_goes_to_the_phone(self) -> None:
        # A Plex Home or shared server takes its own token: the phone gets that one.
        await self.settings.update(plex_server_token="fake-shared-server-token")
        status, paired = await self.pair()
        self.assertEqual(status, 200)
        self.assertEqual(paired["plex"]["token"], "fake-shared-server-token")
        _, hello = await self.ask("GET", "/v1/hello", token=paired["token"])
        self.assertEqual(hello["plex"]["token"], "fake-shared-server-token")

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


CAN_FOLDERS = {"X-Companion-Can": "folder-library"}


class LibraryGateTests(ServiceCase):
    """A library (Plex or the folders) lets the phone in; an old Companion sees "no Plex"."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.settings.update(**PLEX)

        async def handler(request: Request) -> Reply:
            return Reply(200, {"ok": True})

        self.service = self.make_service({("GET", "/v1/thing"): handler})
        await self.service.set_on(True)
        _, paired = await self.pair()
        self.token = paired["token"]

    async def use_folders(self) -> None:
        await self.settings.update(
            plex_token="", folder_library_on=True, film_folders=(str(self.root / "Films"),)
        )

    async def thing(self, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        return await self.ask("GET", "/v1/thing", token=self.token, headers=headers)

    async def test_an_old_companion_still_sees_no_plex(self) -> None:
        await self.use_folders()
        status, hello = await self.ask("GET", "/v1/hello", token=self.token)
        self.assertEqual(status, 200)
        self.assertIsNone(hello["plex"])
        self.assertEqual(hello["library"], "folders")
        self.assertEqual(hello["api_version"], 1)
        self.assertEqual(
            await self.thing(), (409, {"error": "no_plex", "message": t(NO_PLEX_TEXT)})
        )

    async def test_a_new_companion_reaches_the_folder_library(self) -> None:
        await self.use_folders()
        self.assertEqual(await self.thing(CAN_FOLDERS), (200, {"ok": True}))
        # Any case, among other capabilities.
        status, _ = await self.thing({"X-Companion-Can": "later-thing, Folder-Library"})
        self.assertEqual(status, 200)

    async def test_no_library_at_all(self) -> None:
        await self.settings.update(plex_token="", folder_library_on=False)
        _, hello = await self.ask("GET", "/v1/hello", token=self.token, headers=CAN_FOLDERS)
        self.assertIsNone(hello["library"])
        self.assertEqual(
            await self.thing(CAN_FOLDERS),
            (409, {"error": "no_library", "message": t(NO_LIBRARY_TEXT)}),
        )
        self.assertEqual(
            await self.thing(), (409, {"error": "no_plex", "message": t(NO_PLEX_TEXT)})
        )

    async def test_hello_says_folders(self) -> None:
        # Plex alone: "folders" false.
        _, hello = await self.ask("GET", "/v1/hello", token=self.token, headers=CAN_FOLDERS)
        self.assertEqual((hello["library"], hello["folders"]), ("plex", False))
        # The folders on beside Plex: the main source stays Plex, and "folders" says so.
        await self.settings.update(folder_library_on=True, film_folders=(str(self.root),))
        _, hello = await self.ask("GET", "/v1/hello", token=self.token, headers=CAN_FOLDERS)
        self.assertEqual((hello["library"], hello["folders"]), ("plex", True))
        self.assertEqual(hello["plex"]["token"], PLEX_TOKEN)
        # An old Companion gets the same answer (an extra key it doesn't read).
        _, old = await self.ask("GET", "/v1/hello", token=self.token)
        self.assertEqual(old, hello)
        # The folders only, and none at all.
        await self.use_folders()
        _, hello = await self.ask("GET", "/v1/hello", token=self.token, headers=CAN_FOLDERS)
        self.assertEqual((hello["library"], hello["folders"]), ("folders", True))
        await self.settings.update(folder_library_on=False)
        _, hello = await self.ask("GET", "/v1/hello", token=self.token, headers=CAN_FOLDERS)
        self.assertEqual((hello["library"], hello["folders"]), (None, False))
        # Switched on with no folder set counts as off.
        await self.settings.update(folder_library_on=True, film_folders=(), series_folders=())
        _, hello = await self.ask("GET", "/v1/hello", token=self.token, headers=CAN_FOLDERS)
        self.assertFalse(hello["folders"])

    async def test_with_plex_nothing_changes(self) -> None:
        await self.settings.update(folder_library_on=True, film_folders=(str(self.root),))
        _, hello = await self.ask("GET", "/v1/hello", token=self.token)
        self.assertEqual(hello["library"], "plex")
        self.assertEqual(hello["plex"]["token"], PLEX_TOKEN)
        self.assertEqual(await self.thing(), (200, {"ok": True}))
        self.assertEqual(await self.thing(CAN_FOLDERS), (200, {"ok": True}))

    async def test_pairing_works_with_the_folder_library(self) -> None:
        await self.use_folders()
        self.assertTrue(self.service.has_library)
        self.assertEqual(self.service.library_source, "folders")
        status, body = await self.pair("Second phone")
        self.assertEqual(status, 200)
        self.assertEqual(body["library"], "folders")
        self.assertIsNone(body["plex"])
        await self.settings.update(folder_library_on=False)
        self.assertFalse(self.service.has_library)
        self.assertIsNone(self.service.library_source)
        with self.assertRaises(PairingUnavailable) as caught:
            self.service.new_pairing_payload()
        self.assertEqual(str(caught.exception), t(NEEDS_LIBRARY_TEXT))

    async def test_the_page_follows_the_library(self) -> None:
        await self.settings.update(plex_token="")
        heard: list[None] = []
        self.service.subscribe(lambda: heard.append(None))
        await self.settings.update(film_folders=(str(self.root / "Films"),))
        self.assertEqual(heard, [])  # still no library: switched off
        await self.settings.update(folder_library_on=True)
        self.assertEqual(len(heard), 1)  # None -> "folders"
        await self.settings.update(series_folders=(str(self.root / "Series"),))
        self.assertEqual(len(heard), 1)  # still "folders"
        await self.settings.update(plex_token=PLEX_TOKEN)
        self.assertEqual(len(heard), 2)  # "folders" -> "plex"


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

    def test_home_ranges_include_tailscale_and_ipv6_unique_local(self) -> None:
        for address in ("100.64.0.1", "100.101.102.103", "100.127.255.254", "fd12:3456::1",
                        "fc00::1", "::ffff:100.64.0.7"):
            with self.subTest(address):
                self.assertTrue(is_home_address(address))
        for address in ("100.63.255.255", "100.128.0.1", "2001:db8::1", "2a02:a46::1", "fe00::1"):
            with self.subTest(address):
                self.assertFalse(is_home_address(address))

    def test_the_codes_hosts_put_the_home_network_first(self) -> None:
        # A full-tunnel VPN (CGNAT address) holds the default route: the Wi-Fi's address first,
        # every one kept, IPv6 unique local ones after the IPv4 ones.
        self.assertEqual(
            ranked_addresses(
                "100.96.0.5",
                ["fd00:1::20", "100.96.0.5", "192.168.1.20", "fe80::1", "2a02:a46::20",
                 "10.8.0.2", "8.8.8.8"],
            ),
            ["192.168.1.20", "10.8.0.2", "100.96.0.5", "fd00:1::20"],
        )  # fmt: skip
        # A private default route stays first; without one, the PC's own order.
        self.assertEqual(
            ranked_addresses("10.0.0.7", ["192.168.56.1", "10.0.0.7", "fd00::7"]),
            ["10.0.0.7", "192.168.56.1", "fd00::7"],
        )
        self.assertEqual(
            ranked_addresses(None, ["fd00::7", "172.20.1.2"]), ["172.20.1.2", "fd00::7"]
        )
        self.assertEqual(ranked_addresses(None, []), [])


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


class PortTests(ServiceCase):
    """58760 / 58761 / 58762 sit in Windows' dynamic range, which Hyper-V, WSL2 and Docker keep
    pieces of (the bind fails with "access denied"): each has fallbacks."""

    def taken(self, kind: socket.SocketKind = socket.SOCK_STREAM) -> int:
        """A port in use for the whole test (as one Windows keeps)."""
        holder = socket.socket(socket.AF_INET, kind)
        self.addCleanup(holder.close)
        holder.bind(("0.0.0.0", 0))
        if kind == socket.SOCK_STREAM:
            holder.listen()
        return int(holder.getsockname()[1])

    def service_on(
        self, ports: tuple[int, ...], finder_ports: tuple[int, ...] | None = None
    ) -> PhoneService:
        service = PhoneService(
            self.settings,
            self.paths,
            self.profiles,
            routes_factory=dict,
            port=ports,
            finder_port=finder_ports or (self.finder_port,),
            addresses=lambda: [HOME],
        )
        self.addAsyncCleanup(service.aclose)
        return service

    async def finder_says(self) -> bytes | None:
        pc_id = self.settings.current.phone_pc_id
        return await self.find(f"AGC? {pc_id}".encode())

    def finder_answer(self, port: int) -> bytes:
        return f"AGC! {self.settings.current.phone_pc_id} {port}".encode()

    def test_the_ports_tried(self) -> None:
        self.assertEqual(PORTS, (58760, 47760, 0))  # 58760 first: what older phones know
        self.assertEqual(FINDER_PORTS, (58761, 47761))

    async def test_a_kept_port_falls_back_and_is_remembered(self) -> None:
        await self.settings.update(**PLEX)
        kept = self.taken()
        service = self.service_on((kept, self.port, 0))
        await service.set_on(True)
        self.assertIsNone(service.problem)
        self.assertEqual(service.port, self.port)
        self.assertEqual(self.settings.current.phone_port, self.port)
        payload = json.loads(service.new_pairing_payload())
        self.assertEqual(payload["port"], self.port)  # the code names the port it got
        self.assertEqual(await self.finder_says(), self.finder_answer(self.port))
        await service.aclose()
        # Next start: the one it got last time first, also when the list's first is free.
        again = self.service_on((_free_port(), self.port, 0))
        await again.set_on(True)
        self.assertEqual(again.port, self.port)

    async def test_the_last_resort_is_a_free_port_windows_picks(self) -> None:
        service = self.service_on((self.taken(), self.taken(), 0))
        await service.set_on(True)
        self.assertIsNone(service.problem)
        port = service.port
        assert port is not None
        self.assertNotEqual(port, 0)
        self.assertEqual(self.settings.current.phone_port, port)
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        self.assertEqual(await self.finder_says(), self.finder_answer(port))

    async def test_the_port_problem_is_said_only_when_nothing_could_be_bound(self) -> None:
        service = self.service_on((self.taken(), self.taken()))
        await service.set_on(True)
        self.assertEqual(service.problem, PORT_BUSY_TEXT)
        self.assertIsNone(service.port)

    async def test_the_finder_listens_on_each_port_it_can(self) -> None:
        kept = self.taken(socket.SOCK_DGRAM)
        service = self.service_on((self.port,), (kept, self.finder_port))
        await service.set_on(True)
        self.assertIsNone(service.problem)  # one is enough
        self.assertEqual(await self.finder_says(), self.finder_answer(self.port))
        await service.set_on(False)
        both = self.service_on((self.port,), (self.finder_port, _free_port(socket.SOCK_DGRAM)))
        await both.set_on(True)
        self.assertEqual(len(both._finders), 2)


class NetworkHintTests(ServiceCase):
    def service_checking(self, check: NetworkCheck | None) -> PhoneService:
        service = PhoneService(
            self.settings, self.paths, self.profiles, routes_factory=dict, port=self.port,
            finder_port=self.finder_port, network_check=check,
        )  # fmt: skip
        self.addAsyncCleanup(service.aclose)
        return service

    async def test_the_hint_says_what_windows_keeps_out(self) -> None:
        await self.service.check_network()
        self.assertIsNone(self.service.network_hint)  # can't tell: nothing said
        heard: list[None] = []
        self.service.subscribe(lambda: heard.append(None))
        self.network = NetworkFacts(public=True, blocked=False)
        await self.service.check_network()
        self.assertEqual(self.service.network_hint, PUBLIC_NETWORK_TEXT)
        self.assertEqual(len(heard), 1)
        self.network = NetworkFacts(public=False, blocked=True)
        await self.service.check_network()
        self.assertEqual(self.service.network_hint, FIREWALL_BLOCKS_TEXT)
        self.network = NetworkFacts(public=True, blocked=True)
        await self.service.check_network()
        self.assertEqual(
            self.service.network_hint, PUBLIC_NETWORK_TEXT + "\n" + FIREWALL_BLOCKS_TEXT
        )
        self.network = NetworkFacts(public=False, blocked=False)
        await self.service.check_network()
        self.assertIsNone(self.service.network_hint)
        # Windows' own question calls AirGrabber "Python": the hint and the first line say so.
        for text in (FIREWALL_BLOCKS_TEXT, FIREWALL_TEXT):
            self.assertIn("\u201cPython\u201d", text)
        self.assertIn("Private network", PUBLIC_NETWORK_TEXT)

    async def test_one_question_at_a_time(self) -> None:
        asked = asyncio.Event()
        answer: asyncio.Future[NetworkFacts | None] = asyncio.get_running_loop().create_future()

        async def slow() -> NetworkFacts | None:
            self.network_checks += 1
            asked.set()
            return await answer

        service = self.service_checking(slow)
        first = asyncio.create_task(service.check_network())
        await asyncio.wait_for(asked.wait(), 5)
        second = asyncio.create_task(service.check_network())
        answer.set_result(NetworkFacts(public=True, blocked=False))
        await asyncio.gather(first, second)
        self.assertEqual(self.network_checks, 1)
        self.assertEqual(service.network_hint, PUBLIC_NETWORK_TEXT)

    async def test_a_failing_check_says_nothing(self) -> None:
        async def broken() -> NetworkFacts | None:
            raise RuntimeError("C:\\secret\\python.exe")

        service = self.service_checking(broken)
        with self.assertLogs("app.services.phone.service", "WARNING") as logs:
            await service.check_network()
        self.assertIsNone(service.network_hint)
        self.assertNotIn("secret", "\n".join(logs.output))

    async def test_without_a_check_nothing_is_asked(self) -> None:
        service = self.service_checking(None)
        await service.check_network()
        self.assertIsNone(service.network_hint)


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
    """With OPEN_TO_EVERYONE off (as before 3.23.0), the connection exists only with the marker."""

    async def asyncSetUp(self) -> None:
        closed = mock.patch("app.services.phone.service.OPEN_TO_EVERYONE", False)
        closed.start()
        self.addCleanup(closed.stop)
        await super().asyncSetUp()

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
    async def test_without_a_library_the_page_is_one_line_and_follows_it(self) -> None:
        await self.service.set_on(True)
        await self.settings.update(plex_token="")
        box, page = self.box()
        heading = self.shown(page)[:2]  # the page's own title and note
        self.assertEqual(self.shown(page)[2:], [NEEDS_LIBRARY_TEXT])
        self.assertEqual(self.shown_buttons(page), {})
        await self.settings.update(plex_token=PLEX_TOKEN)
        self.assertNotIn(NEEDS_LIBRARY_TEXT, self.shown(page))
        self.assertIn(CONNECT_TEXT, self.shown_buttons(page))
        box.show_code()
        await self.settings.update(plex_token="")  # disconnected with a code on screen
        self.assertEqual(self.shown(page), [*heading, NEEDS_LIBRARY_TEXT])  # the code too is gone
        await self.settings.update(folder_library_on=True, film_folders=(str(self.root),))
        self.assertNotIn(NEEDS_LIBRARY_TEXT, self.shown(page))  # the folders are a library too
        self.assertIn(CONNECT_TEXT, self.shown_buttons(page))

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

    async def test_the_network_hint_shows_while_it_is_on(self) -> None:
        self.network = NetworkFacts(public=True, blocked=False)
        _, page = self.box()
        await self.settle()
        self.assertEqual(self.network_checks, 0)  # off: Windows isn't asked
        self.assertNotIn(PUBLIC_NETWORK_TEXT, self.shown(page))
        await self.service.set_on(True)
        box, page = self.box()  # the page opened again: Windows is asked again
        await box.settled()
        self.assertEqual(self.network_checks, 1)
        self.assertIn(PUBLIC_NETWORK_TEXT, self.shown(page))
        self.network = NetworkFacts(public=False, blocked=True)  # now Private, but blocked
        await self.service.check_network()
        self.assertNotIn(PUBLIC_NETWORK_TEXT, self.shown(page))
        self.assertIn(FIREWALL_BLOCKS_TEXT, self.shown(page))
        await self.service.set_on(False)
        self.assertNotIn(FIREWALL_BLOCKS_TEXT, self.shown(page))

    async def test_turning_it_on_asks_windows(self) -> None:
        self.network = NetworkFacts(public=False, blocked=True)
        box, page = self.box()
        box.switch.toggle()
        await box.settled()
        self.assertEqual(self.network_checks, 1)
        self.assertIn(FIREWALL_BLOCKS_TEXT, self.shown(page))

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
        from app.ui.settings_pages_registry import PAGE_PHONE

        dialog = self.settings_window(self.service)
        self.assertIn(PAGE_PHONE, dialog.sections)
        self.assertIn(FIREWALL_TEXT, texts(dialog.page(PAGE_PHONE)))

    async def test_open_to_everyone_without_the_marker_the_page_is_there(self) -> None:
        from app.services.phone import service as phone_service
        from app.ui.settings_pages_registry import PAGE_PHONE

        self.assertTrue(phone_service.OPEN_TO_EVERYONE)  # since 3.23.0
        (self.root / ENABLED_MARKER).unlink()
        service = self.make_service()
        self.assertFalse(service.can_switch)  # the hidden switch has nothing to switch
        dialog = self.settings_window(service)
        self.assertIn(PAGE_PHONE, dialog.sections)

    @mock.patch("app.services.phone.service.OPEN_TO_EVERYONE", False)
    async def test_without_the_marker_the_settings_window_has_no_page(self) -> None:
        from app.ui.settings_pages_registry import PAGE_PHONE

        (self.root / ENABLED_MARKER).unlink()
        dialog = self.settings_window(self.make_service())
        self.assertNotIn(PAGE_PHONE, dialog.sections)
        self.assertNotIn(PAGE_PHONE, dialog._items)
        self.assertNotIn(PAGE_PHONE, texts(dialog._sidebar))
        self.assertIsNone(dialog.phone_box)

    async def test_seven_quick_clicks_on_the_version_switch_the_phone_features(self) -> None:
        from app.ui.settings_dialog import PHONE_OFF_TITLE, PHONE_ON_TITLE
        from app.ui.settings_pages_registry import PAGE_UPDATES

        marker = self.root / ENABLED_MARKER
        dialog = self.settings_window(self.service)
        dialog.page(PAGE_UPDATES)
        for n in range(6):
            dialog.version_clicked(now=100.0 + n)
        await self.settle()
        self.assertTrue(marker.is_file())  # six: nothing
        dialog.version_clicked(now=110.0)  # too slow: counting starts again
        for n in range(6):
            dialog.version_clicked(now=111.0 + n)
        await self.settle()
        self.assertFalse(marker.exists())
        self.assertTrue(self.service.available)  # this start keeps what it had
        off = dialog.phone_switch_dialog
        assert off is not None
        self.assertEqual(off.title(), t(PHONE_OFF_TITLE))
        self.assertIsNone(off.code_image)  # off: no download code
        self.assertIsNone(off.restart_button)  # no restart to offer here
        off.close()
        for n in range(7):
            dialog.version_clicked(now=200.0 + n)
        await self.settle()
        self.assertTrue(marker.is_file())
        on = dialog.phone_switch_dialog
        assert on is not None
        self.assertEqual(on.title(), t(PHONE_ON_TITLE))
        self.assertIsNotNone(on.code_image)  # the phone app's download code
        on.close()

    async def test_the_page_offers_the_phone_app(self) -> None:
        from app.ui.phone_app import GET_APP_TITLE, PHONE_APP_URL

        self.assertTrue(PHONE_APP_URL.startswith("https://github.com/drunkgummyboy/AirGrabber/"))
        box, _ = self.box()
        box.app_button.invoke()
        first = box.app_dialog
        assert first is not None
        self.addCleanup(first.close)
        self.assertEqual(first.title(), t(GET_APP_TITLE))
        self.assertIsNotNone(first.code_image)
        box.app_button.invoke()
        self.assertIs(box.app_dialog, first)  # one at a time

    async def test_the_restart_button(self) -> None:
        from app.ui.phone_app import PhoneAppDialog

        restarted = mock.Mock()
        dialog = PhoneAppDialog(self.window, title="x", show_code=False, on_restart=restarted)
        assert dialog.restart_button is not None
        dialog.restart_button.invoke()
        restarted.assert_called_once_with()
        self.assertFalse(dialog.winfo_exists())


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
