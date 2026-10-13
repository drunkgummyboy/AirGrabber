"""Automatic setup of a Prowlarr on this PC: connect to it, then add a few public sites to it once.

Connecting: when Settings has no Prowlarr yet, read the config.xml in Prowlarr's installer data
folder, wait for Prowlarr to answer, check the key, and save its URL and key in Settings.

Adding sites: once per Prowlarr, and only for the Prowlarr on this PC (never one on a NAS the
user typed in), add the curated public sites in `CURATED` that it lacks. Prowlarr tests every
site before saving it, so sites that are blocked on this network are simply not added. Existing
indexers, app profiles, proxies and tags are never changed or deleted. A run that could not finish
(Prowlarr gone, key rejected, site definitions not downloaded yet) is tried again next launch.

Secret hygiene: the API key travels only in the `X-Api-Key` header. Logs name API paths, status
codes and site names: never a URL, the key, a request or response body, or an httpx error text.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from app.models import ProwlarrSetupResult
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

# Where the Prowlarr installer (winget or the setup program, tray or service) keeps its data.
# Portable copies started with -data=<folder> are not looked for.
CONFIG_PATH = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "Prowlarr" / "config.xml"

# The public sites added to a Prowlarr on this PC: (definitionName as Prowlarr offers it,
# priority 1-50 where lower is preferred), best first.
CURATED: tuple[tuple[str, int], ...] = (
    ("Knaben", 10),
    ("limetorrents", 15),
    ("torrentdownload", 20),
    ("showrss", 25),
    ("nyaasi", 30),
)

_MAX_CONFIG_BYTES = 64 * 1024
_DEFAULT_PORT = 9696
_API_KEY = re.compile(r"[0-9a-fA-F]{32}")
_LOOPBACK = "127.0.0.1"
# Binds that answer on loopback. 127.0.0.1 rather than localhost: on Windows "localhost" tries
# ::1 first, which costs 2-4 s when Prowlarr listens on IPv4 only or not at all.
_LOOPBACK_BINDS = frozenset({"", "*", "0.0.0.0", "::", "[::]", "127.0.0.1", "localhost"})

_PING_PATH = "/ping"
_STATUS_PATH = "/api/v1/system/status"
_APP_PROFILE_PATH = "/api/v1/appprofile"
_INDEXER_PATH = "/api/v1/indexer"
_SCHEMA_PATH = "/api/v1/indexer/schema"

_PING_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
_PING_INTERVAL_S = 1.0
_PING_BUDGET_S = 60.0  # covers Prowlarr's first start right after it was installed
_SCHEMA_TIMEOUT = httpx.Timeout(60.0, connect=5.0)  # ~660 definitions, ~6 MB
_ADD_TIMEOUT = httpx.Timeout(90.0, connect=5.0)  # Prowlarr tests the site before saving it

_SEARCHED_CATEGORIES = frozenset({2000, 5000})  # Movies and TV: all AirGrabber ever asks for
_NEW_APP_PROFILE: dict[str, JsonValue] = {
    "name": "AirGrabber",
    "enableRss": True,
    "enableAutomaticSearch": True,
    "enableInteractiveSearch": True,
    "minimumSeeders": 1,
}
_IDENTIFIER = re.compile(r"[\w.\[\]]{1,80}")

Sleep = Callable[[float], Awaitable[None]]
WireT = TypeVar("WireT", bound="_Wire")


# --- config.xml -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalProwlarr:
    """Where the Prowlarr on this PC answers, as its config.xml says."""

    base_url: str  # "http://host:port[/url base]", in the form Settings.prowlarr_url stores
    api_key: str = field(repr=False)


def parse_config(text: str) -> LocalProwlarr | None:
    """The address and key in a Prowlarr config.xml; None when the file is not usable.

    Prowlarr always listens on http at <Port> (https is only added with a certificate), so the
    SSL settings are ignored. Wildcard and loopback binds become 127.0.0.1. Raises nothing.
    """
    if "<!doctype" in text.casefold():  # no DTDs, so no entity expansion
        return None
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        return None
    if root.tag != "Config":
        return None

    def value(tag: str) -> str:
        return (root.findtext(tag) or "").strip()

    key = value("ApiKey")
    if not _API_KEY.fullmatch(key):
        return None
    port_text = value("Port") or str(_DEFAULT_PORT)
    if not (port_text.isascii() and port_text.isdigit()):
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    url_base = value("UrlBase").strip("/")
    prefix = f"/{url_base}" if url_base else ""
    return LocalProwlarr(f"http://{_host(value('BindAddress') or '*')}:{port}{prefix}", key)


def _host(bind: str) -> str:
    """The host to reach a Prowlarr bound to `bind` (a BindAddress value) from this PC."""
    if bind.casefold() in _LOOPBACK_BINDS:
        return _LOOPBACK
    try:
        address = ipaddress.ip_address(bind.strip("[]"))
    except ValueError:
        return _LOOPBACK
    if address.is_unspecified:
        return _LOOPBACK
    if isinstance(address, ipaddress.IPv6Address):
        return _LOOPBACK if address.scope_id else f"[{address}]"
    return str(address)


def read_local_config(path: Path) -> LocalProwlarr | None:
    """`parse_config` for the file at `path`; None when it is missing or too big. Raises nothing."""
    try:
        with path.open("rb") as file:
            data = file.read(_MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        log.debug("No Prowlarr config on this PC (%s)", type(exc).__name__)
        return None
    if len(data) > _MAX_CONFIG_BYTES:
        log.debug("Ignoring the Prowlarr config on this PC: over %d bytes", _MAX_CONFIG_BYTES)
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = ""
    local = parse_config(text)
    if local is None:
        log.debug("Ignoring the Prowlarr config on this PC: not usable")
    return local


# --- Prowlarr payloads ------------------------------------------------------------------------


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _AppProfile(_Wire):
    id: int = Field(gt=0)


class _Indexer(_Wire):
    name: str | None = None
    definition_name: str | None = Field(default=None, alias="definitionName")


class _Category(_Wire):
    id: int


class _Capabilities(_Wire):
    categories: tuple[_Category, ...] = ()  # top-level categories; sub-categories are ignored


class _Field(_Wire):
    name: str
    type: str | None = None
    value: JsonValue = None


class _SchemaEntry(_Wire):
    """One addable indexer from GET /api/v1/indexer/schema (only what adding one needs)."""

    name: str = Field(min_length=1)
    definition_name: str = Field(alias="definitionName", min_length=1)
    implementation: str = Field(min_length=1)
    config_contract: str = Field(alias="configContract", min_length=1)
    protocol: str = ""
    privacy: str = ""
    capabilities: _Capabilities = _Capabilities()
    settings: tuple[_Field, ...] = Field(default=(), alias="fields")

    @property
    def suitable(self) -> bool:
        """A public torrent site that Prowlarr would ask in AirGrabber's TV or movie searches
        (it only queries sites whose categories cover the requested ones)."""
        categories = {category.id for category in self.capabilities.categories}
        return (
            self.protocol.casefold() == "torrent"
            and self.privacy.casefold() == "public"
            and bool(categories & _SEARCHED_CATEGORIES)
        )

    def payload(self, profile_id: int, priority: int) -> dict[str, JsonValue]:
        """The new indexer as the web UI would send it. Fields keep the definition's defaults
        (baseUrl stays empty, so the site follows future domain changes); info texts are left
        out. `enable` makes Prowlarr test the site before saving it."""
        return {
            "name": self.name,
            "definitionName": self.definition_name,
            "implementation": self.implementation,
            "configContract": self.config_contract,
            "protocol": self.protocol,
            "enable": True,
            "appProfileId": profile_id,
            "priority": priority,
            "tags": [],
            "fields": [
                {"name": setting.name, "value": setting.value}
                for setting in self.settings
                if setting.type != "info"
            ],
        }


class _Outcome(Enum):
    ADDED = "added"
    SKIPPED = "skipped"  # Prowlarr refused this one site; final
    STOPPED = "stopped"  # Prowlarr itself failed; the run is incomplete


@dataclass(frozen=True, slots=True)
class _Schema:
    entries: dict[str, dict[str, object]]  # case-folded definitionName -> raw entry
    has_definitions: bool  # the downloaded (Cardigann) site definitions are present


# --- the service ------------------------------------------------------------------------------


class ProwlarrSetupService:
    """Connects AirGrabber to a Prowlarr on this PC and adds the curated public sites to it once."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: SettingsService,
        *,
        config_path: Path | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`config_path` defaults to `CONFIG_PATH`; `sleep` and `clock` pace the wait for
        Prowlarr to come up (tests pass fakes)."""
        self._http = http
        self._settings = settings
        self._config_path = CONFIG_PATH if config_path is None else config_path
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()

    async def run(self) -> ProwlarrSetupResult:
        """Connect to the Prowlarr on this PC when none is configured, then add the curated
        sites to it unless that was done before. Problems with the network or Prowlarr are
        logged, never raised."""
        async with self._lock:
            return await self._run()

    async def _run(self) -> ProwlarrSetupResult:
        current = self._settings.current
        url = current.prowlarr_url
        key = current.prowlarr_api_key.get_secret_value()
        configured = bool(url or key)
        if configured and (not (url and key) or current.prowlarr_seeded_url == url):
            return ProwlarrSetupResult()
        local = await asyncio.to_thread(read_local_config, self._config_path)
        if local is None:
            return ProwlarrSetupResult()
        connected = False
        if not configured:
            if not await self._connect(local):
                return ProwlarrSetupResult()
            connected = True
            url, key = local.base_url, local.api_key
        elif url != local.base_url:
            return ProwlarrSetupResult()  # a Prowlarr elsewhere is the user's to set up
        elif not await self._is_prowlarr(url, key):
            return ProwlarrSetupResult()
        if self._settings.current.prowlarr_seeded_url == url:
            return ProwlarrSetupResult(connected=connected)
        added, complete = await self._add_sites(url, key)
        if complete:
            await self._settings.update(prowlarr_seeded_url=url)
            log.info("Prowlarr setup: public sites done for the Prowlarr on this PC")
        else:
            log.info("Prowlarr setup: adding public sites is unfinished; trying again next launch")
        return ProwlarrSetupResult(connected=connected, added=added)

    # -- connecting --------------------------------------------------------------------------

    async def _connect(self, local: LocalProwlarr) -> bool:
        """Wait for the Prowlarr on this PC, check its key and save both in Settings."""
        if not await self._wait_until_up(local.base_url):
            log.info("Prowlarr setup: the Prowlarr on this PC did not answer; will try next launch")
            return False
        if not await self._is_prowlarr(local.base_url, local.api_key):
            return False
        current = self._settings.current
        if current.prowlarr_url or current.prowlarr_api_key.get_secret_value():
            return False  # entered in Settings meanwhile: never overwrite the user's choice
        await self._settings.update(prowlarr_url=local.base_url, prowlarr_api_key=local.api_key)
        log.info("Prowlarr setup: connected to the Prowlarr on this PC")
        return True

    async def _wait_until_up(self, base: str) -> bool:
        """Poll /ping every second until Prowlarr answers, for up to a minute."""
        deadline = self._clock() + _PING_BUDGET_S
        while True:
            if await self._ping(base):
                return True
            if self._clock() + _PING_INTERVAL_S > deadline:
                return False
            await self._sleep(_PING_INTERVAL_S)

    async def _ping(self, base: str) -> bool:
        try:  # /ping needs no key, so none is sent
            response = await self._http.get(
                base + _PING_PATH, timeout=_PING_TIMEOUT, follow_redirects=False
            )
        except (httpx.HTTPError, httpx.InvalidURL):
            return False
        payload = _json(response) if response.status_code == 200 else None
        return isinstance(payload, dict) and str(payload.get("status", "")).casefold() == "ok"

    async def _is_prowlarr(self, base: str, key: str) -> bool:
        """Whether `base` answers the status call with `key`, as Prowlarr."""
        response = await self._request("GET", base, _STATUS_PATH, key)
        if response is None:
            return False
        if response.status_code != 200:
            log.info("Prowlarr setup: %s answered HTTP %d", _STATUS_PATH, response.status_code)
            return False
        payload = _json(response)
        if isinstance(payload, dict) and payload.get("appName") == "Prowlarr":
            return True
        log.info("Prowlarr setup: %s did not answer like Prowlarr", _STATUS_PATH)
        return False

    # -- adding sites ------------------------------------------------------------------------

    async def _add_sites(self, base: str, key: str) -> tuple[tuple[str, ...], bool]:
        """Add the curated sites Prowlarr lacks: (names added, whether the run was complete)."""
        profile_id = await self._app_profile(base, key)
        if profile_id is None:
            return (), False
        existing = await self._existing(base, key)
        if existing is None:
            return (), False
        names, definitions = existing
        schema = await self._schema(base, key)
        if schema is None:
            return (), False
        added: list[str] = []
        complete = True
        for definition_name, priority in CURATED:
            raw = schema.entries.get(definition_name.casefold())
            if raw is None:
                if schema.has_definitions:
                    log.info("Prowlarr setup: %s is not offered by Prowlarr", definition_name)
                else:  # downloaded daily; missing after an offline first start
                    log.info(
                        "Prowlarr setup: %s not offered yet (site definitions not downloaded)",
                        definition_name,
                    )
                    complete = False
                continue
            try:
                entry = _SchemaEntry.model_validate(raw)
            except ValidationError:
                log.warning("Prowlarr setup: odd definition for %s; skipped", definition_name)
                continue
            if definition_name.casefold() in definitions or entry.name.casefold() in names:
                log.info("Prowlarr setup: %s is already in Prowlarr", entry.name)
                continue
            if not entry.suitable:
                log.info("Prowlarr setup: %s is no public TV/movie site; skipped", entry.name)
                continue
            outcome = await self._add(base, key, entry, profile_id, priority)
            if outcome is _Outcome.ADDED:
                added.append(entry.name)
            elif outcome is _Outcome.STOPPED:
                complete = False
                break
        return tuple(added), complete

    async def _app_profile(self, base: str, key: str) -> int | None:
        """The lowest app profile id; one is created when there is none. None on failure."""
        payload = await self._get_list(base, _APP_PROFILE_PATH, key)
        if payload is None:
            return None
        profiles = _parse_each(_AppProfile, payload)
        if profiles:
            return min(profile.id for profile in profiles)
        if payload:  # profiles we cannot read: better not to add another
            log.warning("Prowlarr setup: unexpected answer from %s", _APP_PROFILE_PATH)
            return None
        response = await self._request("POST", base, _APP_PROFILE_PATH, key, body=_NEW_APP_PROFILE)
        if response is None:
            return None
        created = _parse_each(_AppProfile, [_json(response)]) if response.is_success else []
        if not created:
            log.warning(
                "Prowlarr setup: creating an app profile failed (HTTP %d)", response.status_code
            )
            return None
        log.info("Prowlarr setup: created an app profile for the new sites")
        return created[0].id

    async def _existing(self, base: str, key: str) -> tuple[set[str], set[str]] | None:
        """The case-folded (names, definition names) of the indexers Prowlarr already has."""
        payload = await self._get_list(base, _INDEXER_PATH, key)
        if payload is None:
            return None
        indexers = _parse_each(_Indexer, payload)
        names = {indexer.name.casefold() for indexer in indexers if indexer.name}
        definitions = {
            indexer.definition_name.casefold() for indexer in indexers if indexer.definition_name
        }
        return names, definitions

    async def _schema(self, base: str, key: str) -> _Schema | None:
        payload = await self._get_list(base, _SCHEMA_PATH, key, timeout=_SCHEMA_TIMEOUT)
        if payload is None:
            return None
        entries: dict[str, dict[str, object]] = {}
        has_definitions = False
        for item in payload:
            if not isinstance(item, dict):
                continue
            definition_name = item.get("definitionName")
            if isinstance(definition_name, str) and definition_name:
                entries.setdefault(definition_name.casefold(), item)
            has_definitions = has_definitions or item.get("implementation") == "Cardigann"
        return _Schema(entries, has_definitions)

    async def _add(
        self, base: str, key: str, entry: _SchemaEntry, profile_id: int, priority: int
    ) -> _Outcome:
        response = await self._request(
            "POST",
            base,
            _INDEXER_PATH,
            key,
            body=entry.payload(profile_id, priority),
            timeout=_ADD_TIMEOUT,
        )
        if response is None:
            return _Outcome.STOPPED
        status = response.status_code
        if response.is_success:  # 201 Created
            log.info("Prowlarr setup: added %s", entry.name)
            return _Outcome.ADDED
        if status == 400:
            log.info("Prowlarr setup: %s not added (%s)", entry.name, _refusal(response))
            return _Outcome.SKIPPED
        log.warning("Prowlarr setup: %s answered HTTP %d", _INDEXER_PATH, status)
        return _Outcome.STOPPED

    # -- HTTP --------------------------------------------------------------------------------

    async def _get_list(
        self,
        base: str,
        path: str,
        key: str,
        *,
        timeout: httpx.Timeout | None = None,
    ) -> list[object] | None:
        """The JSON list a successful GET answers with; None (logged) on any failure."""
        response = await self._request("GET", base, path, key, timeout=timeout)
        if response is None:
            return None
        if response.status_code != 200:
            log.warning("Prowlarr setup: %s answered HTTP %d", path, response.status_code)
            return None
        payload = _json(response)
        if not isinstance(payload, list):
            log.warning("Prowlarr setup: %s did not answer with a list", path)
            return None
        return payload

    async def _request(
        self,
        method: str,
        base: str,
        path: str,
        key: str,
        *,
        body: JsonValue = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response | None:
        """One request with the key in its header; None (logged) when nothing answered.

        Redirects are not followed, so the key never travels anywhere but `base`.
        """
        try:
            return await self._http.request(
                method,
                base + path,
                headers={"X-Api-Key": key, "Accept": "application/json"},
                json=body,
                timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
                follow_redirects=False,
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            log.warning("Prowlarr setup: no answer from %s (%s)", path, type(exc).__name__)
            return None


def _json(response: httpx.Response) -> object:
    """The response's JSON body; None when it has none."""
    try:
        payload: object = response.json()
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        return None
    return payload


def _parse_each(model: type[WireT], items: list[object]) -> list[WireT]:
    """The items that validate as `model`; the others are dropped."""
    parsed: list[WireT] = []
    for item in items:
        try:
            parsed.append(model.model_validate(item))
        except ValidationError:
            continue
    return parsed


def _refusal(response: httpx.Response) -> str:
    """Why Prowlarr refused a site, for the log. Its messages are never quoted: validation texts
    follow the Windows language and test failures can quote URLs."""
    payload = _json(response)
    items = payload if isinstance(payload, list) else []
    failures = [item for item in items if isinstance(item, dict)]
    problems = [
        f"{_identifier(item.get('errorCode'))} on {_identifier(item.get('propertyName'))}"
        for item in failures
        if item.get("errorCode")
    ]
    if problems:  # validation: a clash with an existing name, or a bad payload
        return "rejected: " + "; ".join(problems)
    if any("cloudflare" in str(item.get("errorMessage", "")).casefold() for item in failures):
        return "its test was blocked by Cloudflare"
    return "its test failed"


def _identifier(value: object) -> str:
    text = str(value) if value is not None else ""
    return text if _IDENTIFIER.fullmatch(text) else "?"
