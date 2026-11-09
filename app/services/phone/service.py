"""The phone connection as a whole (`services.phone`): on or off, pairing, and being found.

For now it exists only on a PC whose data folder holds the file ENABLED_MARKER: without it there
is no Settings page, `run` returns at once and nothing starts, whatever the settings say. Setting
OPEN_TO_EVERYONE to True opens it up.

Off (the default) it does nothing: no port, no certificate. On, it runs the HTTPS server
(app/services/phone/server.py) and a small UDP finder, which tells a phone that knows this PC's id
where it is now, for when the PC's address changes. A start that fails never stops the app: it
says why in one plain sentence (`problem`), and `run` tries again now and then, for a port that a
closing AirGrabber still held.

No Plex, no phone app (the owner's decision): without a Plex token no code is made, and every route
but `/v1/pair` and `/v1/hello` answers 409 `no_plex` with one plain sentence (`/v1/hello` says
`"plex": null`, which the phone shows as that sentence). The page follows Plex (`plex_connected`).

A phone pairs by scanning a code (`new_pairing_payload`), and from then on follows the person who
was selected at that moment. The first phone is a library manager's, later ones aren't; the code
doesn't say and the phone has no say in it. Settings changes each phone's role, name and person
afterward (`change_phone`); the server reads the phone at every request, so that counts at once.
The Plex token goes out only in `/v1/pair`'s and `/v1/hello`'s answers: never in the code, never
in a log.

Nothing here logs a token, a code, an address or an exception's text.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import logging
import socket
import ssl
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from app import __version__
from app.config import AppPaths
from app.i18n import N_, t
from app.models import PairedPhone, Profile
from app.services.phone.certificate import ensure_certificate
from app.services.phone.copies import PhoneCopies
from app.services.phone.devices import DeviceStore, PairingCode
from app.services.phone.sends import PhoneSends
from app.services.phone.server import (
    PORT,
    FileReply,
    PhoneServer,
    Reply,
    Request,
    Route,
    error_reply,
    is_home_address,
)
from app.services.plex import plex_connected
from app.services.profiles import ProfileService
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

OPEN_TO_EVERYONE = False  # True: every copy has the phone connection, marker or not
ENABLED_MARKER = "phone-connection.enabled"  # a file directly in the data folder (AppPaths.root)

API_VERSION = 1
FINDER_PORT = 58761
MAX_PHONE_NAME = 60  # what a phone calls itself; the request isn't signed in yet
RETRY_S = 30.0  # between tries while it's on but not (all) running

PORT_BUSY_TEXT = N_(
    "Another program is using the port the phone connection needs, so close that program or "
    "restart your PC."
)
START_FAILED_TEXT = N_(
    "AirGrabber couldn't start the phone connection, so restart AirGrabber and try again."
)
FINDER_BUSY_TEXT = N_(
    "Another program is in the way of the phone connection, so your phone may not find this PC "
    "when its address changes: restart your PC to fix it."
)
CERTIFICATE_REMADE_TEXT = N_(
    "AirGrabber had to replace its security certificate, so phones connected earlier won't "
    "connect any more: connect each of them again."
)
PACKAGES_MISSING_TEXT = N_(
    "AirGrabber couldn't install what the phone connection needs, so check your internet "
    "connection: it tries again when you start it tomorrow."
)
OFF_TEXT = N_("The phone connection isn't running, so turn it on first.")
NO_NETWORK_TEXT = N_(
    "This PC isn't on a home network, so connect it to the same network as your phone and try "
    "again."
)
CODE_REFUSED_TEXT = N_(
    "That code has expired or was already used, so show a new code in AirGrabber's Settings and "
    "scan that one."
)
BAD_PAIRING_TEXT = N_("AirGrabber couldn't read that code, so scan it again.")
NEEDS_PLEX_TEXT = N_("Connect Plex first to use the phone app.")
NO_PLEX_TEXT = N_("AirGrabber isn't connected to Plex, so connect Plex in AirGrabber on your PC.")
PHONE_NAME_TEXT = N_("Phone")

# Windows says "access denied" for a port it keeps for itself (Hyper-V's ranges, for one).
_PORT_ERRORS = frozenset({errno.EADDRINUSE, errno.EACCES, 10013})


class PairingUnavailable(Exception):
    """No code can be made now; `str()` says why in one plain sentence."""


def home_only(addresses: Iterable[str]) -> list[str]:
    """The addresses a phone at home can reach this PC on, in order, without repeats."""
    found: list[str] = []
    for text in addresses:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if (
            is_home_address(text)
            and not address.is_loopback
            and not address.is_link_local
            and text not in found
        ):
            found.append(text)
    return found


def this_pcs_addresses() -> list[str]:
    """This PC's home-network addresses, the one its traffic leaves by first."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # Sends nothing: it only makes Windows pick the adapter. (An address reserved for
            # documentation, so it goes by the default route.)
            probe.connect(("192.0.2.1", 9))
            found.append(str(probe.getsockname()[0]))
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return home_only(found)


def _clean_name(name: str) -> str:
    """What a phone calls itself as shown in Settings: one line, of a sensible size."""
    printable = "".join(char if char.isprintable() else " " for char in name)
    cleaned = " ".join(printable.split())[:MAX_PHONE_NAME].rstrip()
    return cleaned or t(PHONE_NAME_TEXT)


class _Finder(asyncio.DatagramProtocol):
    """Answers `AGC? <pc id>` with `AGC! <pc id> <port>`, on the home network only."""

    def __init__(self, pc_id: str, port: int) -> None:
        self._question = f"AGC? {pc_id}".encode()
        self._answer = f"AGC! {pc_id} {port}".encode()
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        if data == self._question and is_home_address(str(addr[0])) and self._transport:
            self._transport.sendto(self._answer, addr)

    def error_received(self, exc: Exception) -> None:
        # Windows reports an earlier answer that found nobody home on the next read.
        log.debug("The phone finder got %s", type(exc).__name__)


class PhoneService:
    def __init__(
        self,
        settings: SettingsService,
        paths: AppPaths,
        profiles: ProfileService,
        routes_factory: Callable[[], Mapping[tuple[str, str], Route]],
        *,
        port: int = PORT,
        finder_port: int = FINDER_PORT,
        addresses: Callable[[], list[str]] = this_pcs_addresses,
        sends: PhoneSends | None = None,
        copies: PhoneCopies | None = None,
    ) -> None:
        """`routes_factory`: the routes of everything else the phone may ask (read at each
        start). `port`, `finder_port`, `addresses`: for tests."""
        self._settings = settings
        self._paths = paths
        self._profiles = profiles
        self._routes_factory = routes_factory
        self._port = port
        self._finder_port = finder_port
        self._addresses = addresses
        # What was sent to each phone and what it holds (its menu in Settings).
        self.sends = sends if sends is not None else PhoneSends(settings)
        # Phone-sized copies of 4K films (copies.py): started with the server, said in hello.
        self.copies = copies
        # Read once: the page and the service agree until the next start.
        self.available = OPEN_TO_EVERYONE or (paths.root / ENABLED_MARKER).is_file()
        self._devices = DeviceStore(settings)
        self._code = PairingCode()
        self._server: PhoneServer | None = None
        self._finder: asyncio.DatagramTransport | None = None
        self._fingerprint = ""  # of the certificate in use, once it runs
        self._lock = asyncio.Lock()  # one start or stop at a time
        self._closed = False
        self._listeners: list[Callable[[], None]] = []
        self.problem: str | None = None  # why it isn't (fully) working, in one plain sentence
        self._had_plex = plex_connected(settings.current)
        self._unsubscribe_settings = settings.subscribe(self._settings_changed)

    # -- what the page shows ---------------------------------------------------------------------

    @property
    def on(self) -> bool:
        """What the switch says, whether or not it managed to start."""
        return self._settings.current.phone_connection_on

    @property
    def running(self) -> bool:
        """Phones can connect now."""
        return self._server is not None

    @property
    def ever_started(self) -> bool:
        """It has been on on this PC before (so Windows has already asked about the network)."""
        return bool(self._settings.current.phone_fingerprint)

    @property
    def notice(self) -> str | None:
        """Phones connected earlier trust another certificate than the one in use, so they'll
        refuse this PC: said until one connects again, or they're all disconnected."""
        current = self._settings.current
        if (
            self._fingerprint
            and current.phone_devices
            and current.phone_fingerprint != self._fingerprint
        ):
            return t(CERTIFICATE_REMADE_TEXT)
        return None

    def phones(self) -> tuple[PairedPhone, ...]:
        return self._settings.current.phone_devices

    @property
    def people(self) -> tuple[Profile, ...]:
        """AirGrabber's people, whom a phone can belong to."""
        return self._profiles.people

    @property
    def plex_connected(self) -> bool:
        """Plex is connected: without it the phone app has nothing to show."""
        return plex_connected(self._settings.current)

    def _settings_changed(self, current: Any) -> None:
        """Plex connected or disconnected: the page follows."""
        now = plex_connected(current)
        if now != self._had_plex:
            self._had_plex = now
            self._notify()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` after every change the page shows. Returns what unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("Phone connection listener failed")

    # -- on and off ------------------------------------------------------------------------------

    async def set_on(self, on: bool) -> None:
        """Turn it on or off, also for the next start. A start that fails sets `problem`."""
        await self._settings.update(phone_connection_on=on)
        async with self._lock:
            if on:
                await self._start()
            else:
                await self._stop()
                self.problem = None
        self._notify()

    async def run(self, retry_s: float = RETRY_S) -> None:
        """For as long as the app runs: start it when the settings say on, and try again what
        failed. Does nothing while it's off, and ends at once on a PC without the connection."""
        if not self.available:
            return
        while not self._closed:
            if self.on and not self._fully_running:
                async with self._lock:
                    # It may have been turned off while waiting for the lock.
                    if self.on:
                        await self._start()
                self._notify()
            await asyncio.sleep(retry_s)

    async def aclose(self) -> None:
        """The app is closing: stop, and don't start again."""
        self._unsubscribe_settings()
        async with self._lock:
            self._closed = True
            await self._stop()
        if self.copies is not None:
            await self.copies.aclose()

    @property
    def _fully_running(self) -> bool:
        return self._server is not None and self._finder is not None

    async def _start(self) -> None:
        """Start what isn't running; `problem` says what didn't work out."""
        if self._closed or not self.available:
            return
        problem = None
        if self._server is None:
            problem = await self._start_server()
        if self._server is not None and self._finder is None:
            problem = await self._start_finder()
        self.problem = problem

    async def _start_server(self) -> str | None:
        try:
            cert, key, fingerprint = await asyncio.to_thread(ensure_certificate, self._paths)
        except ImportError as exc:  # the optional cryptography couldn't be installed
            self._log_start_failure("No package for the phone connection's certificate", exc)
            return t(PACKAGES_MISSING_TEXT)
        except (OSError, ValueError) as exc:
            self._log_start_failure("No certificate for the phone connection", exc)
            return t(START_FAILED_TEXT)
        await self._remember_identity(fingerprint)
        self._fingerprint = fingerprint
        others = {key: self._needs_plex(handler) for key, handler in self._routes_factory().items()}
        routes = {**others, **self._own_routes()}
        server = PhoneServer(routes, self._devices, cert, key)
        try:
            await server.start(self._port)
        except ssl.SSLError as exc:
            # The key doesn't go with the certificate. Without the certificate the next try makes
            # a new pair (`notice` then tells about the phones paired before).
            self._log_start_failure("The phone connection's key is unusable", exc)
            cert.unlink(missing_ok=True)
            return t(START_FAILED_TEXT)
        except OSError as exc:
            self._log_start_failure("The phone connection can't start", exc)
            return t(PORT_BUSY_TEXT if exc.errno in _PORT_ERRORS else START_FAILED_TEXT)
        self._server = server
        if self.copies is not None:
            await self.copies.start()  # tries the card once: hello says what it found
        return None

    async def _remember_identity(self, fingerprint: str) -> None:
        """This PC's id, made once, and the certificate the paired phones trust. With phones
        paired, another certificate than theirs stays unsaved: that's how `notice` knows."""
        current = self._settings.current
        changes: dict[str, str] = {}
        if not current.phone_pc_id:
            changes["phone_pc_id"] = uuid.uuid4().hex
        if not current.phone_fingerprint or not current.phone_devices:
            changes["phone_fingerprint"] = fingerprint
        if changes:
            await self._settings.update(**changes)

    async def _start_finder(self) -> str | None:
        pc_id = self._settings.current.phone_pc_id
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Finder(pc_id, self._port), local_addr=("0.0.0.0", self._finder_port)
            )
        except OSError as exc:
            self._log_start_failure("The phone finder can't start", exc)
            return t(FINDER_BUSY_TEXT)
        self._finder = transport
        return None

    def _log_start_failure(self, what: str, exc: Exception) -> None:
        """The first time as a warning, not every time `run` tries again; never the exception's
        text, which holds a file's path."""
        level = logging.WARNING if self.problem is None else logging.DEBUG
        log.log(level, "%s: %s %s", what, type(exc).__name__, getattr(exc, "errno", None))

    async def _stop(self) -> None:
        self._code = PairingCode()  # a code shown before doesn't outlive the connection
        finder, self._finder = self._finder, None
        if finder is not None:
            finder.close()
        server, self._server = self._server, None
        if server is not None:
            await server.stop()

    # -- pairing ---------------------------------------------------------------------------------

    def new_pairing_payload(self) -> str:
        """What the code on the screen holds; it cancels the code before it. Raises
        PairingUnavailable when a phone couldn't connect anyway."""
        if not self.plex_connected:
            raise PairingUnavailable(t(NEEDS_PLEX_TEXT))
        if self._server is None:
            raise PairingUnavailable(t(OFF_TEXT))
        hosts = self._addresses()
        if not hosts:
            raise PairingUnavailable(t(NO_NETWORK_TEXT))
        return json.dumps(
            {
                "v": 1,
                "id": self._settings.current.phone_pc_id,
                "hosts": hosts,
                "port": self._port,
                "fp": self._fingerprint,
                "code": self._code.new(datetime.now(UTC)),
            },
            separators=(",", ":"),
        )

    async def disconnect(self, id: str) -> None:
        """This phone's token stops working at once."""
        await self._devices.remove(id)
        self._notify()

    async def change_phone(
        self,
        id: str,
        *,
        name: str | None = None,
        manager: bool | None = None,
        profile_id: int | None = None,
    ) -> None:
        """A phone's new name (cleaned up as a phone's own is), role or person; None leaves
        that as it is. A person who isn't there any more changes nothing."""
        if profile_id is not None and self._profiles.get(profile_id) is None:
            profile_id = None
        await self._devices.change(
            id,
            name=_clean_name(name) if name is not None else None,
            manager=manager,
            profile_id=profile_id,
        )
        self._notify()

    def _needs_plex(self, handler: Route) -> Route:
        """`handler`, answering `no_plex` while Plex isn't connected."""

        async def guarded(request: Request) -> Reply | FileReply:
            if not self.plex_connected:
                return error_reply(409, "no_plex", t(NO_PLEX_TEXT))
            return await handler(request)

        return guarded

    def _own_routes(self) -> dict[tuple[str, str], Route]:
        return {("POST", "/v1/pair"): self._pair, ("GET", "/v1/hello"): self._hello}

    async def _pair(self, request: Request) -> Reply:
        code = request.body.get("code")
        name = request.body.get("name")
        if not isinstance(code, str) or not isinstance(name, str):
            return error_reply(400, "bad_request", t(BAD_PAIRING_TEXT))
        if not self._code.take(code, datetime.now(UTC)):
            return error_reply(401, "not_paired", t(CODE_REFUSED_TEXT))
        first = not self._settings.current.phone_devices
        phone, token = await self._devices.add(
            _clean_name(name), self._profiles.current_id, manager=first
        )
        # Someone is using this certificate now: the note about the earlier one has done its work.
        await self._settings.update(phone_fingerprint=self._fingerprint)
        self._notify()
        return Reply(200, {"token": token, **self._about(phone)})

    async def _hello(self, request: Request) -> Reply:
        assert request.phone is not None  # the server lets only paired phones through
        return Reply(200, self._about(request.phone))

    def _about(self, phone: PairedPhone) -> dict[str, Any]:
        return {
            "phone_id": phone.id,
            "manager": phone.manager,
            "api_version": API_VERSION,
            "app_version": __version__,
            "plex": self._plex_details(),
            "phone_copies": self.copies is not None and self.copies.available,
        }

    def _plex_details(self) -> dict[str, str] | None:
        """How the phone reaches Plex; None until AirGrabber has found its server itself."""
        current = self._settings.current
        if not plex_connected(current) or not current.plex_server_uri:
            return None
        return {
            "uri": current.plex_server_uri,
            "token": current.plex_token.get_secret_value(),
            "server_id": current.plex_server_id,
            "server_name": current.plex_server_name,
        }
