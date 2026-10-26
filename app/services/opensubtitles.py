"""Async client for the OpenSubtitles.com REST API (https://api.opensubtitles.com/api/v1), and the
OpenSubtitles "moviehash" of a video file.

What the API asks of a client (opensubtitles.stoplight.io, "Getting started" and "Best
practices"): every request carries `Api-Key` and a `User-Agent` naming the app and its version
("AirGrabber v2.3.0"); GET parameters sorted, lowercase, without defaults; at most 5 requests a
second per IP (this client keeps to 4); `/login` at most once a second, and never again with
credentials it rejected (401); after a login every request goes to the `base_url` host it named
(api. or vip-api.opensubtitles.com) with `Authorization: Bearer <token>`; the token is valid 24
hours. `/download` returns a temporary link plus `remaining` (downloads left today) and
`reset_time_utc`; 406 means today's downloads are used up.

AirGrabber's own API key (APP_API_KEY) is shipped with the app, as OpenSubtitles' terms ask
("one key per application"). A user may enter their own key in Settings
(`Settings.opensubtitles_own_key`); it then replaces the shipped one (`effective_api_key`).
Signing in with a free account is optional; downloads then count against that account. Neither
key is ever logged or shown in a message.

Secret hygiene: the API key, the password and the token travel in headers or the POST body only,
never in a URL. Error messages and log records name the endpoint path at most; no httpx
exception (which may carry a URL) is ever chained to, or left as the context of, an
OpenSubtitlesError. The temporary download links are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from app import APP_NAME, __version__
from app.i18n import N_, t
from app.models import SubtitleAccount
from app.models.opensubtitles import (
    OSDownload,
    OSLogin,
    OSQuotaAnswer,
    OSSearch,
    OSUserInfo,
    SubtitleCandidate,
    SubtitleLink,
    candidates,
)

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

# AirGrabber's OpenSubtitles API consumer key (opensubtitles.com -> profile -> API consumers),
# the one key for every copy of the app (public in the source, still never logged). Empty:
# subtitles aren't offered in this version.
APP_API_KEY = "FSyx3KOQGUtxJKG3oVhrbcytV5hA3KkU"

DEFAULT_HOST = "api.opensubtitles.com"
API_HOSTS = frozenset({DEFAULT_HOST, "vip-api.opensubtitles.com"})  # what /login may name
USER_AGENT = f"{APP_NAME} v{__version__}"
REQUEST_INTERVAL_S = 0.25  # at most 4 requests a second (the limit is 5 per IP)
LOGIN_INTERVAL_S = 1.0  # /login: at most one a second
SESSION_TTL_S = 23 * 3600.0  # the token is valid 24 hours; log in again a bit before
MAX_WAIT_S = 10.0  # the longest a 429 answer is waited out
MAX_SUBTITLE_BYTES = 5_000_000  # a subtitle file is a few hundred KB at most
_ATTEMPTS = 3  # for 429, 5xx and transport errors

# OpenSubtitles' hash: file size + the 64-bit little-endian words of the first and last 64 KiB.
HASH_CHUNK = 65536
HASH_MIN_SIZE = 131072  # "9000000000 > filesize > 131072" (Moviehash source codes page)
HASH_MAX_SIZE = 9_000_000_000

# Plain-words messages (the status bar and Settings show them as they are), raised through t().
KEY_REJECTED_TEXT = N_(
    "OpenSubtitles refused AirGrabber's key. An update of AirGrabber may fix this"
)
LOGIN_REJECTED_TEXT = N_("Wrong OpenSubtitles user name or password")
UNREACHABLE_TEXT = N_("Couldn't reach OpenSubtitles. Check your internet connection")
QUOTA_TEXT = N_("Today's subtitle downloads are used up")
NO_KEY_TEXT = N_("Subtitles aren't available in this version yet")
OWN_KEY_REJECTED_TEXT = N_(
    "OpenSubtitles refused your own API key. Check it, or leave it empty to use AirGrabber's key"
)


def effective_api_key(own_key: str, shipped_key: str) -> str:
    """The key requests use: the user's own key when one is filled in, else the shipped one."""
    return own_key.strip() or shipped_key.strip()


class OpenSubtitlesError(Exception):
    """OpenSubtitles refused or failed a request. The message is plain words for the UI and
    never holds a URL, the key, the password or the token."""


class KeyRejected(OpenSubtitlesError):
    """The API key was refused (401/403 without a signed-in user)."""


class LoginRejected(OpenSubtitlesError):
    """/login refused the user name or password (401)."""


class QuotaExceeded(OpenSubtitlesError):
    """Today's downloads are used up (406 on /download); `reset_at` = when they start again."""

    def __init__(self, message: str, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class Unreachable(OpenSubtitlesError):
    """No answer (network, timeout) or a server error that retrying didn't cure."""


class _SessionExpired(Exception):
    """The token was refused: log in again (internal)."""


def movie_hash(path: Path | str) -> str | None:
    """The OpenSubtitles hash of a video file: 16 lowercase hex digits, or None when the file is
    outside the sizes OpenSubtitles hashes. Reads 128 KiB; blocking (run it in a thread)."""
    size = os.path.getsize(path)
    if not HASH_MIN_SIZE < size < HASH_MAX_SIZE:
        return None
    total = size
    with open(path, "rb") as file:
        head = file.read(HASH_CHUNK)
        file.seek(size - HASH_CHUNK)
        tail = file.read(HASH_CHUNK)
    for chunk in (head, tail):
        for (word,) in struct.iter_unpack("<Q", chunk[: len(chunk) - len(chunk) % 8]):
            total = (total + word) & 0xFFFF_FFFF_FFFF_FFFF
    return f"{total:016x}"


def sorted_params(params: Mapping[str, str | int]) -> list[tuple[str, str]]:
    """GET parameters as OpenSubtitles wants them (no redirect): sorted, lowercase, no empties."""
    return sorted(
        (name.lower(), str(value).lower())
        for name, value in params.items()
        if str(value).strip() != ""
    )


def imdb_number(imdb_id: str | None) -> int | None:
    """"tt0903747" -> 903747 (OpenSubtitles wants the number without "tt" and leading zeros)."""
    if not imdb_id:
        return None
    digits = imdb_id.strip().lower().removeprefix("tt")
    return int(digits) if digits.isdigit() and int(digits) > 0 else None


@dataclass(slots=True)
class _Session:
    token: str
    host: str
    fingerprint: str  # of the key, user name and password it was made with
    started: float  # monotonic


def _fingerprint(key: str, username: str, password: str) -> str:
    """Tells whether credentials changed, without keeping them (memory only)."""
    return hashlib.sha256(f"{key}\0{username}\0{password}".encode()).hexdigest()


class OpenSubtitlesClient:
    """One request at a time, spaced REQUEST_INTERVAL_S apart.

    `api_key` is called for every request (the user's own key, else AirGrabber's shipped
    APP_API_KEY: `effective_api_key`); `own_key` tells whether that is the user's own key, so a
    refusal names the right one. The user's session (token) lives in this object only.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: Callable[[], str],
        *,
        own_key: Callable[[], bool] = lambda: False,
        user_agent: str = USER_AGENT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delay: float = 1.0,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._own_key = own_key
        self._user_agent = user_agent
        self._clock = clock
        self._sleep = sleep
        self._retry_delay = retry_delay
        self._lock = asyncio.Lock()
        self._next_request = 0.0
        self._next_login = 0.0
        self._session: _Session | None = None
        self._rejected: str | None = None  # fingerprint of credentials /login refused

    # -- public --------------------------------------------------------------------------------

    @property
    def signed_in(self) -> bool:
        return self._session is not None

    @property
    def configured(self) -> bool:
        """There is an API key to use (this version ships one)."""
        return bool(self._api_key().strip())

    def forget_session(self) -> None:
        self._session = None

    async def check_key(self, key: str) -> None:
        """Whether OpenSubtitles accepts `key` (the user's own, not saved yet): one GET
        /infos/formats with it, without a session. Raises KeyRejected (in plain words, without
        the key), Unreachable or OpenSubtitlesError."""
        await self._request("GET", "/infos/formats", key=key.strip(), use_session=False, own=True)

    async def sign_in(
        self, username: str, password: str, *, retry: bool = True
    ) -> SubtitleAccount:
        """Log in (POST /login, the password in the body only) and read the account's downloads
        left today (/infos/user). The session is kept for later requests. `retry` False:
        credentials refused before are refused again without asking."""
        key = self._api_key().strip()
        fingerprint = _fingerprint(key, username, password)
        if not retry and fingerprint == self._rejected:
            raise LoginRejected(t(LOGIN_REJECTED_TEXT))
        self._session = None
        login = await self._login(key, username, password, fingerprint)
        account = SubtitleAccount(
            level=login.user.level,
            allowed_downloads=login.user.allowed_downloads,
            vip=login.user.vip,
        )
        try:
            info = await self._get_model("/infos/user", {}, OSUserInfo, key=key)
        except OpenSubtitlesError as exc:  # the login stands; the count is only nice to have
            log.info("OpenSubtitles /infos/user: %s", exc)
            return account
        return account.model_copy(update={"remaining_downloads": info.data.remaining_downloads})

    async def ensure_session(self, username: str, password: str) -> bool:
        """A session for the saved account: kept while the key, user name and password stay the
        same and it is younger than SESSION_TTL_S, else a new login. False without an account.
        Credentials refused before raise LoginRejected without asking again."""
        if not username or not password:
            self._session = None
            return False
        fingerprint = _fingerprint(self._api_key().strip(), username, password)
        session = self._session
        if (
            session is not None
            and session.fingerprint == fingerprint
            and self._clock() - session.started < SESSION_TTL_S
        ):
            return True
        await self.sign_in(username, password, retry=False)
        return True

    async def search(self, params: Mapping[str, str | int]) -> list[SubtitleCandidate]:
        """GET /subtitles (first page): the usable results (one file each, not machine- or
        AI-translated)."""
        return candidates(await self._get_model("/subtitles", params, OSSearch))

    async def download_link(self, file_id: int) -> SubtitleLink:
        """POST /download: a temporary link to the file (counts against today's downloads)."""
        payload = await self._request("POST", "/download", json_body={"file_id": file_id})
        answer = _validate(OSDownload, payload, "/download")
        return SubtitleLink(
            link=answer.link, remaining=answer.remaining, reset_at=answer.reset_time_utc
        )

    async def fetch(self, link: str) -> bytes:
        """The subtitle file behind a temporary download link (no API key or token sent)."""
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (
            host == "opensubtitles.com" or host.endswith(".opensubtitles.com")
        ):
            raise OpenSubtitlesError(t("OpenSubtitles sent an unexpected download link"))
        response: httpx.Response | None = None
        for attempt in range(1, _ATTEMPTS + 1):
            error_name = ""
            try:
                response = await self._http.get(link, headers={"User-Agent": self._user_agent})
            except httpx.HTTPError as exc:
                error_name = type(exc).__name__
            if response is not None:
                if response.status_code < 500:
                    break
                error_name = f"HTTP {response.status_code}"
            log.info("OpenSubtitles file download: %s (attempt %d)", error_name, attempt)
            if attempt < _ATTEMPTS:
                await self._sleep(self._retry_delay * attempt)
        if response is None or response.status_code >= 500:
            raise Unreachable(t(UNREACHABLE_TEXT))
        if response.status_code != 200:
            raise OpenSubtitlesError(
                t(
                    "OpenSubtitles didn't hand over the subtitle file (HTTP {status})",
                    status=response.status_code,
                )
            )
        content = response.content
        if not content or len(content) > MAX_SUBTITLE_BYTES:
            raise OpenSubtitlesError(t("OpenSubtitles sent an empty or oversized subtitle file"))
        return content

    # -- internals -------------------------------------------------------------------------------

    async def _login(self, key: str, username: str, password: str, fingerprint: str) -> OSLogin:
        wait = self._next_login - self._clock()
        if wait > 0:
            await self._sleep(wait)
        self._next_login = self._clock() + LOGIN_INTERVAL_S
        try:
            payload = await self._request(
                "POST",
                "/login",
                key=key,
                json_body={"username": username, "password": password},
                use_session=False,
            )
        except LoginRejected:
            self._rejected = fingerprint  # never sent again unless the user asks (Sign in)
            raise
        login = _validate(OSLogin, payload, "/login")
        host = login.base_url.strip().lower().removeprefix("https://").rstrip("/")
        if host not in API_HOSTS:  # never send the key or token anywhere else
            host = DEFAULT_HOST
        self._rejected = None
        self._session = _Session(
            token=login.token, host=host, fingerprint=fingerprint, started=self._clock()
        )
        log.info("OpenSubtitles: signed in (%s)", login.user.level or "member")
        return login

    async def _get_model(
        self, path: str, params: Mapping[str, str | int], model: type[M], *, key: str | None = None
    ) -> M:
        payload = await self._request("GET", path, params=params, key=key)
        return _validate(model, payload, path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
        key: str | None = None,
        use_session: bool = True,
        own: bool | None = None,
    ) -> object:
        """One API call with retries; returns the decoded JSON. A refused token is dropped and
        the call made once more without it. `own`: whether the key is the user's own (None:
        as `own_key` says), for the refusal's wording."""
        rejected = self._rejected_text(own)
        try:
            return await self._call(method, path, params, json_body, key, use_session, rejected)
        except _SessionExpired:
            pass
        log.info("OpenSubtitles %s: the session expired", path)
        self._session = None
        try:
            return await self._call(method, path, params, json_body, key, False, rejected)
        except _SessionExpired:
            raise KeyRejected(t(rejected)) from None

    def _rejected_text(self, own: bool | None) -> str:
        if own is None:
            own = self._own_key()
        return OWN_KEY_REJECTED_TEXT if own else KEY_REJECTED_TEXT

    async def _call(
        self,
        method: str,
        path: str,
        params: Mapping[str, str | int] | None,
        json_body: Mapping[str, Any] | None,
        key: str | None,
        use_session: bool,
        rejected: str = KEY_REJECTED_TEXT,
    ) -> object:
        api_key = (key if key is not None else self._api_key()).strip()
        if not api_key:
            raise KeyRejected(t(NO_KEY_TEXT))
        if not (api_key.isascii() and api_key.isprintable()):
            raise KeyRejected(t(rejected))
        session = self._session if use_session else None
        host = session.host if session is not None else DEFAULT_HOST
        headers = {
            "Api-Key": api_key,
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        if session is not None:
            headers["Authorization"] = f"Bearer {session.token}"
        url = f"https://{host}/api/v1{path}"
        query = sorted_params(params or {})
        for attempt in range(1, _ATTEMPTS + 1):
            response, error_name = await self._send(method, url, headers, query, json_body)
            if response is None:
                reason = error_name
                wait = self._retry_delay * attempt
            else:
                status = response.status_code
                if status == 200:
                    return _json(response, path)
                if session is not None and _token_refused(response):
                    raise _SessionExpired
                if status == 401 and path == "/login":
                    raise LoginRejected(t(LOGIN_REJECTED_TEXT))
                if status in (401, 403):
                    raise KeyRejected(t(rejected))
                if status == 406 and path == "/download":
                    raise _quota_exceeded(response)
                if status != 429 and status < 500:
                    raise OpenSubtitlesError(
                        t("OpenSubtitles {path}: HTTP {status}", path=path, status=status)
                    )
                reason = f"HTTP {status}"
                wait = _retry_after(response) if status == 429 else self._retry_delay * attempt
            log.info("OpenSubtitles %s: %s (attempt %d of %d)", path, reason, attempt, _ATTEMPTS)
            if attempt < _ATTEMPTS:
                await self._sleep(wait)
        raise Unreachable(t(UNREACHABLE_TEXT))

    async def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        query: list[tuple[str, str]],
        json_body: Mapping[str, Any] | None,
    ) -> tuple[httpx.Response | None, str]:
        """(response, "") or (None, the transport error's type name): no exception escapes."""
        async with self._lock:
            wait = self._next_request - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._next_request = self._clock() + REQUEST_INTERVAL_S
            try:
                response = await self._http.request(
                    method,
                    url,
                    headers=headers,
                    params=tuple(query) if query else None,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                return None, type(exc).__name__
        return response, ""


def _json(response: httpx.Response, path: str) -> object:
    try:
        return response.json()
    except ValueError:
        pass
    raise OpenSubtitlesError(t("OpenSubtitles {path}: the answer wasn't readable", path=path))


def _validate(model: type[M], payload: object, path: str) -> M:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        count = exc.error_count()
    raise OpenSubtitlesError(
        t("OpenSubtitles {path}: unexpected answer ({n} invalid field(s))", path=path, n=count)
    )


def _token_refused(response: httpx.Response) -> bool:
    """401, or the "invalid token" 500 the docs describe."""
    if response.status_code == 401:
        return True
    if response.status_code == 500:
        try:
            text = response.text.lower()
        except (UnicodeDecodeError, httpx.HTTPError):
            return False
        return "invalid" in text and "token" in text
    return False


def _retry_after(response: httpx.Response) -> float:
    for name in ("retry-after", "ratelimit-reset", "x-ratelimit-reset"):
        value = response.headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value)
        except ValueError:
            continue
        if seconds > 1e9:  # an epoch timestamp
            seconds -= time.time()
        return min(max(seconds, 1.0), MAX_WAIT_S)
    return 1.0


def _quota_exceeded(response: httpx.Response) -> QuotaExceeded:
    reset_at: datetime | None = None
    try:
        reset_at = OSQuotaAnswer.model_validate(response.json()).reset_time_utc
    except (ValueError, ValidationError):
        pass
    return QuotaExceeded(t(QUOTA_TEXT), reset_at)
