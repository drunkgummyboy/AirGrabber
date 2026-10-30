"""qBittorrent's Web UI as AirGrabber sets it up: on, bound to this PC only (127.0.0.1), with a
password of its own, so AirGrabber can send downloads to it and read their progress (the
Downloads page) instead of opening each one in qBittorrent.

Pure helpers for qBittorrent's settings file; app/services/qbittorrent_setup.py reads and writes
the file (`QBittorrentSetup.set_up_web_ui`). The keys, from qBittorrent's source:

- src/base/preferences.cpp, 4.2 – 5.2: "Preferences/WebUI/Enabled", ".../Address" (default "*"),
  ".../Port" (8080), ".../Username" ("admin"), ".../Password_PBKDF2", ".../LocalHostAuth"
  (true), ".../HTTPS/Enabled" (false); in the INI file `[Preferences] WebUI\\Enabled=…`;
- 5.3 and later: the same names under "WebUI/…" (`[WebUI] Enabled=…`); 5.3's src/app/upgrade.cpp
  `migrateWebUISettings` (MigrationVersion 11) moves the old keys there once, over the new ones.

Both sets are written with the same values (as the "Add new torrent" switch's two keys are):
each version reads its own, and the one-time move copies equal values. Which set a file's
qBittorrent reads: the new one once `[Meta] MigrationVersion` is 11 or more, else the old one
(the new one when the old one is missing).

The password (src/base/utils/password.cpp `PBKDF2::generate` / `verify`): PBKDF2-HMAC-SHA512,
100000 iterations, a 16-byte random salt, a 64-byte key, stored as "<base64 salt>:<base64 key>";
qBittorrent keeps it as a QByteArray, which QSettings writes as `@ByteArray(…)`, in quotes
because base64's "=" padding needs them (qsettings.cpp `iniEscapedString`).

A Web UI is AirGrabber's when it is on, bound to 127.0.0.1, with the user name `USERNAME`; it
is the user's own when it is on otherwise, when it serves HTTPS, or when it is off but keeps an
account of the user's (a password with a user name other than qBittorrent's default). AirGrabber
never changes the user's own. Its port: 8080 when free, else the next free one up to 8099, else
one Windows picks; never qBittorrent's torrent port.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import socket
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

# (section, key) -> the value, unescaped; None when the file doesn't have it.
Reader = Callable[[str, str], str | None]
PortCheck = Callable[[int], bool]  # True when the port can be listened on

ADDRESS = "127.0.0.1"  # this PC only
USERNAME = "airgrabber"  # marks the Web UI as AirGrabber's
DEFAULT_USERNAME = "admin"  # qBittorrent's
DEFAULT_PORT = 8080  # qBittorrent's
FALLBACK_PORTS = range(DEFAULT_PORT + 1, 8100)
PBKDF2_ITERATIONS = 100_000
PBKDF2_SALT_BYTES = 16
PBKDF2_KEY_BYTES = 64
PASSWORD_BYTES = 24  # secrets.token_urlsafe: 32 characters

# The Web UI's keys: (5.3+ section, key), (4.2 – 5.2 section, key).
NEW_SECTION = "WebUI"
OLD_SECTION = "Preferences"
OLD_PREFIX = "WebUI\\"
MIGRATION_KEY = ("Meta", "MigrationVersion")
MOVED_IN_MIGRATION = 11  # 5.3's upgrade.cpp: `if (version < 11) migrateWebUISettings()`
ENABLED = "Enabled"
ADDRESS_KEY = "Address"
PORT = "Port"
USERNAME_KEY = "Username"
PASSWORD = "Password_PBKDF2"
LOCAL_AUTH = "LocalHostAuth"
HTTPS = "HTTPS\\Enabled"
WRITTEN = (ENABLED, ADDRESS_KEY, PORT, USERNAME_KEY, PASSWORD, LOCAL_AUTH)
# qBittorrent's torrent port, which the Web UI can't share: 4.4+, then 4.3 and older.
TORRENT_PORT_KEYS = (("BitTorrent", "Session\\Port"), ("Preferences", "Connection\\PortRangeMin"))
LEGAL_NOTICE_KEY = ("LegalNotice", "Accepted")


class WebUi(Enum):
    """qBittorrent's Web UI as AirGrabber sees it."""

    OFF = "off"  # off (qBittorrent's default) without an account of the user's: may be set up
    AIRGRABBER = "airgrabber"  # AirGrabber's own: may be set up again
    USER = "user"  # the user's own: left alone


class WebUiResult(Enum):
    """How `QBittorrentSetup.set_up_web_ui` went."""

    CREATED = "created"  # turned on with a new password (the login in hand)
    KEPT = "kept"  # AirGrabber's already, with the login AirGrabber knows
    USER = "user"  # the user's own Web UI: nothing changed
    RUNNING = "running"  # qBittorrent runs: nothing changed
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WebUiLogin:
    """How AirGrabber signs in to the Web UI it set up. The password never shows in a repr."""

    port: int
    username: str
    password: str = field(repr=False)

    @property
    def url(self) -> str:
        return f"http://{ADDRESS}:{self.port}"


def keys(name: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """The setting's (section, key) for qBittorrent 5.3+, then for 4.2 – 5.2."""
    return (NEW_SECTION, name), (OLD_SECTION, OLD_PREFIX + name)


def _number(text: str | None) -> int | None:
    try:
        return int(text.strip()) if text is not None else None
    except ValueError:
        return None


def value(read: Reader, name: str) -> str | None:
    """The Web UI setting `name` as the file's qBittorrent reads it (see the module notes)."""
    new, old = keys(name)
    if (_number(read(*MIGRATION_KEY)) or 0) >= MOVED_IN_MIGRATION:
        return read(*new)
    found = read(*old)
    return found if found is not None else read(*new)


def _on(text: str | None) -> bool:
    """Qt's bool: true unless missing, empty, "0" or "false"."""
    return text is not None and text.strip().casefold() not in ("", "0", "false")


def owner(read: Reader) -> WebUi:
    """Whose Web UI the file describes (see the module notes)."""
    username = (value(read, USERNAME_KEY) or DEFAULT_USERNAME).strip()
    if _on(value(read, HTTPS)):
        return WebUi.USER
    if _on(value(read, ENABLED)):
        address = (value(read, ADDRESS_KEY) or "").strip()
        return WebUi.AIRGRABBER if username == USERNAME and address == ADDRESS else WebUi.USER
    has_password = bool((value(read, PASSWORD) or "").strip())
    if has_password and username not in ("", DEFAULT_USERNAME, USERNAME):
        return WebUi.USER
    return WebUi.OFF


def port_of(read: Reader) -> int | None:
    return _number(value(read, PORT))


def matches(read: Reader, login: WebUiLogin) -> bool:
    """The file's Web UI is AirGrabber's and takes `login` (port, user name, password)."""
    if owner(read) is not WebUi.AIRGRABBER or port_of(read) != login.port:
        return False
    if (value(read, USERNAME_KEY) or "").strip() != login.username:
        return False
    return password_matches(value(read, PASSWORD), login.password)


# -- the password ---------------------------------------------------------------------------------


def new_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)


def new_salt() -> bytes:
    return secrets.token_bytes(PBKDF2_SALT_BYTES)


def password_hash(password: str, salt: bytes) -> str:
    """"<base64 salt>:<base64 key>", as qBittorrent's `PBKDF2::generate` makes it."""
    key = hashlib.pbkdf2_hmac(
        "sha512", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, PBKDF2_KEY_BYTES
    )
    return f"{base64.b64encode(salt).decode('ascii')}:{base64.b64encode(key).decode('ascii')}"


def password_text(hashed: str) -> str:
    """The hash as QSettings writes a QByteArray into the INI file."""
    return f'"@ByteArray({hashed})"'


def password_matches(stored: str | None, password: str) -> bool:
    """Whether `stored` (the Password_PBKDF2 value, unescaped: "@ByteArray(salt:key)") is the
    hash of `password` (qBittorrent's `PBKDF2::verify`)."""
    if stored is None:
        return False
    text = stored.strip()
    if text.startswith("@ByteArray(") and text.endswith(")"):
        text = text[len("@ByteArray(") : -1]
    parts = text.split(":")
    if len(parts) != 2:
        return False
    try:
        salt = base64.b64decode(parts[0], validate=True)
        key = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return False
    computed = hashlib.pbkdf2_hmac(
        "sha512", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, len(key) or 1
    )
    return hmac.compare_digest(computed, key)


# -- the port -------------------------------------------------------------------------------------


def port_is_free(port: int) -> bool:
    """Nothing listens on `port` here: it can be bound on 127.0.0.1 and on every address (each
    probe closed at once). Blocking, but only briefly."""
    for host in (ADDRESS, "0.0.0.0"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if sys.platform == "win32":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((host, port))
            except OSError:
                return False
    return True


def spare_port() -> int:
    """A free port Windows picks."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((ADDRESS, 0))
        port: int = probe.getsockname()[1]
        return port


def torrent_ports(read: Reader) -> frozenset[int]:
    found = (_number(read(section, key)) for section, key in TORRENT_PORT_KEYS)
    return frozenset(port for port in found if port)


def choose_port(
    read: Reader,
    is_free: PortCheck = port_is_free,
    spare: Callable[[], int] = spare_port,
    *,
    preferred: int | None = None,
) -> int:
    """The Web UI's port: `preferred` (AirGrabber's earlier one), else 8080, else the next free
    one up to 8099, else one Windows picks; never qBittorrent's torrent port."""
    taken = torrent_ports(read)
    candidates: Iterable[int] = (
        *((preferred,) if preferred else ()),
        DEFAULT_PORT,
        *FALLBACK_PORTS,
    )
    for port in candidates:
        if 0 < port < 65536 and port not in taken and is_free(port):
            return port
    for _attempt in range(5):
        port = spare()
        if port not in taken:
            return port
    raise OSError("no free port for qBittorrent's Web UI")


# -- what is written ------------------------------------------------------------------------------


def entries(login: WebUiLogin, salt: bytes) -> Mapping[tuple[str, str], str]:
    """The Web UI's settings as INI value texts, for both key sets: on, 127.0.0.1, the port,
    the user name, the password's hash, and a password asked of this PC too (LocalHostAuth)."""
    texts = {
        ENABLED: "true",
        ADDRESS_KEY: ADDRESS,
        PORT: str(login.port),
        USERNAME_KEY: login.username,  # letters only: nothing to escape
        PASSWORD: password_text(password_hash(login.password, salt)),
        LOCAL_AUTH: "true",
    }
    written: dict[tuple[str, str], str] = {}
    for name in WRITTEN:
        for key in keys(name):
            written[key] = texts[name]
    return written


def legal_notice_accepted(read: Reader) -> bool:
    """qBittorrent's first-run notice was accepted (`[LegalNotice] Accepted`, src/app/main.cpp).
    Only read: the user accepts it in qBittorrent itself."""
    return _on(read(*LEGAL_NOTICE_KEY))
