"""The phone connection as a whole (`services.phone`): on or off, pairing, and being found.

For now it exists only on a PC whose data folder holds the file ENABLED_MARKER: without it there
is no Settings page, `run` returns at once and nothing starts, whatever the settings say. Setting
OPEN_TO_EVERYONE to True opens it up. The hidden switch (the owner, 2026-11-10): seven quick clicks on
"Version …" in Settings → Updates write or delete that file (`switch_here`); it counts from the next
start, and the paired phones stay in the settings either way.

Off (the default) it does nothing: no port, no certificate. On, it runs the HTTPS server
(app/services/phone/server.py) and a small UDP finder, which tells a phone that knows this PC's id
where it is now, for when the PC's address changes. After the HTTPS server it starts the play
link server (app/services/phone/play_links.py, plain http for the phone's player and TVs; a port
in use there leaves the rest running). A start that fails never stops the app: it says why in one
plain sentence (`problem`), and `run` tries again now and then, for a port that a closing
AirGrabber still held.

No library, no phone app (the owner's decision): without one (Plex or the user's own folders,
app/services/library_source.py) no code is made, and every route but `/v1/pair` and `/v1/hello`
answers 409 with one plain sentence. `/v1/hello` says where the library is (`"library": "plex" |
"folders" | null`, the main source: "plex" whenever Plex is connected), whether the user's own
folders are on, also beside Plex (`"folders"`, `folders_on`: the two shelves,
docs/folder-library/2026-11-16-alongside-plex-design.md, the phone's Downloaded section), and keeps
`"plex": null` without Plex. A Companion that knows the folder library
says so on every request (`X-Companion-Can: folder-library`, `knows_folders`); one that doesn't
sees what it always saw: `no_plex` unless Plex is the library, so an older Companion sees "no Plex"
and nothing else. With the header, no library at all is `no_library`
(docs/folder-library/2026-11-16-companion-contract.md). The page follows the library
(`has_library`).

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
from app.services.phone.library import PLEX_LIBRARY_TEXT as PLEX_LIBRARY_TEXT  # re-exported
from app.services.phone.play_links import PLAY_PORT, PlayLinks
from app.services.phone.sends import PhoneSends
from app.services.phone.server import (
    PORT,
    AnyReply,
    PhoneServer,
    Reply,
    Request,
    Route,
    error_reply,
    is_home_address,
    knows_folders,
)
from app.services.library_source import LibrarySource, folders_on, library_source
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
NEEDS_LIBRARY_TEXT = N_(
    "Connect Plex or choose your own folders in Settings → Library first to use the phone app."
)
# For a Companion that doesn't know the folder library: what it has always been told.
NO_PLEX_TEXT = N_("AirGrabber isn't connected to Plex, so connect Plex in AirGrabber on your PC.")
NO_LIBRARY_TEXT = N_(
    "AirGrabber has no library yet, so connect Plex or choose your folders in AirGrabber's "
    "Settings → Library."
)
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
        play_links: PlayLinks | None = None,
        play_port: int = PLAY_PORT,
    ) -> None:
        """`routes_factory`: the routes of everything else the phone may ask (read at each
        start). `play_links`: the play link server, run with the connection (the routes make
        its links). `port`, `finder_port`, `play_port`, `addresses`: for tests."""
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
        self._play_links = play_links
        self._play_port = play_port
        # Read once: the page and the service agree until the next start.
        self.available = OPEN_TO_EVERYONE or (paths.root / ENABLED_MARKER).is_file()
        # The hidden switch has something to switch while it isn't open to everyone.
        self.can_switch = not OPEN_TO_EVERYONE
        self._devices = DeviceStore(settings)
        self._code = PairingCode()
        self._server: PhoneServer | None = None
        self._finder: asyncio.DatagramTransport | None = None
        self._fingerprint = ""  # of the certificate in use, once it runs
        self._lock = asyncio.Lock()  # one start or stop at a time
        self._closed = False
        self._listeners: list[Callable[[], None]] = []
        self.problem: str | None = None  # why it isn't (fully) working, in one plain sentence
        self._had_library = library_source(settings.current)
        self._unsubscribe_settings = settings.subscribe(self._settings_changed)

    def switched_on_here(self) -> bool:
        """Whether this PC's data folder holds ENABLED_MARKER now (the next start's say)."""
        return (self._paths.root / ENABLED_MARKER).is_file()

    async def switch_here(self, on: bool) -> None:
        """The hidden switch: write or delete ENABLED_MARKER. Counts from the next start; raises
        OSError when the file can't be written or deleted."""
        marker = self._paths.root / ENABLED_MARKER
        if on:
            await asyncio.to_thread(marker.write_text, "", encoding="utf-8")
        else:
            await asyncio.to_thread(marker.unlink, missing_ok=True)
        log.info("Phone features switched %s here (from the next start)", "on" if on else "off")

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
    def library_source(self) -> LibrarySource | None:
        """Where the library is: Plex, the user's own folders, or None."""
        return library_source(self._settings.current)

    @property
    def has_library(self) -> bool:
        """There is a library (Plex or the folders): without one the phone app has nothing to
        show."""
        return self.library_source is not None

    def _settings_changed(self, current: Any) -> None:
        """The library came, went or moved between Plex and the folders: the page follows."""
        now = library_source(current)
        if now != self._had_library:
            self._had_library = now
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
        return (
            self._server is not None
            and self._finder is not None
            and (self._play_links is None or self._play_links.running)
        )

    async def _start(self) -> None:
        """Start what isn't running; `problem` says what didn't work out."""
        if self._closed or not self.available:
            return
        problem = None
        if self._server is None:
            problem = await self._start_server()
        if self._server is not None and self._finder is None:
            problem = await self._start_finder()
        if self._server is not None:
            # After the phone server; its problem is said when the rest has none.
            links_problem = await self._start_play_links()
            problem = problem or links_problem
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
        others = {
            key: self._needs_library(handler) for key, handler in self._routes_factory().items()
        }
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
            self.sends.resume_copies()  # sent phone-sized from the PC before a restart
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

    async def _start_play_links(self) -> str | None:
        """The play link server, once the phone server runs; a port in use is said, and leaves
        the phone server running."""
        if self._play_links is None or self._play_links.running:
            return None
        try:
            await self._play_links.start(self._play_port)
        except OSError as exc:
            self._log_start_failure("The play link server can't start", exc)
            return t(PORT_BUSY_TEXT if exc.errno in _PORT_ERRORS else START_FAILED_TEXT)
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
        if self._play_links is not None:
            await self._play_links.stop()
        server, self._server = self._server, None
        if server is not None:
            await server.stop()

    # -- pairing ---------------------------------------------------------------------------------

    def new_pairing_payload(self) -> str:
        """What the code on the screen holds; it cancels the code before it. Raises
        PairingUnavailable when a phone couldn't connect anyway."""
        if not self.has_library:
            raise PairingUnavailable(t(NEEDS_LIBRARY_TEXT))
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

    def _needs_library(self, handler: Route) -> Route:
        """`handler` while there is a library the asking Companion knows: Plex for any, the
        folders only for one that says it knows them. Otherwise 409: `no_library` for one that
        knows the folders, `no_plex` (what it has always been told) for one that doesn't."""

        async def guarded(request: Request) -> AnyReply:
            source = self.library_source
            knows = knows_folders(request)
            if source == "plex" or (source == "folders" and knows):
                return await handler(request)
            if source is None and knows:
                return error_reply(409, "no_library", t(NO_LIBRARY_TEXT))
            return error_reply(409, "no_plex", t(NO_PLEX_TEXT))

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
            "library": self.library_source,
            "folders": folders_on(self._settings.current),
            "phone_copies": self.copies is not None and self.copies.available,
            "find": True,  # `/v1/find` (find.py); an older PC has no such key
            "person": True,  # `/v1/person` (person.py), and `progress` on what's downloading
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
