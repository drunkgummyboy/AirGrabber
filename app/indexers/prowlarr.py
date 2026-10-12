"""Prowlarr: the user's own indexer manager, searched through its v1 API.

Secret hygiene: the API key travels only in the `X-Api-Key` header, but the `downloadUrl` of
every release embeds it. Those links are stored on the release and never logged or quoted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx
from pydantic import Field

from app.indexers.base import (
    BaseIndexer,
    IndexerError,
    WireModel,
    http_url,
    magnet_link,
    text_queries,
)
from app.models.domain import SearchKind, SearchRequest, TorrentRelease
from app.models.types import Count

_SEARCH_PATH = "/api/v1/search"
_STATUS_PATH = "/api/v1/system/status"
_TV_CATEGORY = "5000"
_MOVIE_CATEGORY = "2000"
_MAX_QUERIES = 2
_LIMIT = "100"


class _ProwlarrRelease(WireModel):
    title: str
    protocol: str = "torrent"  # "torrent" or "usenet"
    indexer: str | None = None
    size: Count = 0
    seeders: Count = 0
    leechers: Count = 0
    info_hash: str | None = Field(default=None, alias="infoHash")
    magnet_url: str | None = Field(default=None, alias="magnetUrl")
    download_url: str | None = Field(default=None, alias="downloadUrl")
    info_url: str | None = Field(default=None, alias="infoUrl")
    publish_date: datetime | None = Field(default=None, alias="publishDate")


class ProwlarrIndexer(BaseIndexer):
    name = "prowlarr"
    label = "Prowlarr"
    kinds = frozenset(SearchKind)

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        url: Callable[[], str],
        api_key: Callable[[], str],
        max_concurrent: int = 2,
        retry_delay: float = 1.0,
    ) -> None:
        """`url` and `api_key` are read on every call, so changes in Settings apply at once."""
        super().__init__(http, max_concurrent=max_concurrent, retry_delay=retry_delay)
        self._url = url
        self._api_key = api_key

    def supports(self, request: SearchRequest) -> bool:
        return super().supports(request) and bool(self._base_url() and self._api_key().strip())

    async def search(self, request: SearchRequest) -> list[TorrentRelease]:
        if not self.supports(request):
            return []
        base, headers = self._connection()
        category = _MOVIE_CATEGORY if request.kind is SearchKind.MOVIE else _TV_CATEGORY

        async def search_query(query: str) -> list[TorrentRelease]:
            params = {"query": query, "type": "search", "categories": category, "limit": _LIMIT}
            payload = await self._get_json(base, _SEARCH_PATH, params, headers=headers)
            return self._parse_releases(_SEARCH_PATH, payload, self._release)

        return await self._first_hit(text_queries(request)[:_MAX_QUERIES], search_query)

    async def test(self) -> str:
        """'Prowlarr <version>' when the configured URL and key work; IndexerError otherwise."""
        base, headers = self._connection()
        # One attempt: a Settings "Test" button should fail fast, not retry for ~18 s.
        payload = await self._get_json(base, _STATUS_PATH, headers=headers, failed_attempts=1)
        if isinstance(payload, dict):
            version = payload.get("version")
            app_name = payload.get("appName", "Prowlarr")
            if isinstance(version, str) and version and app_name == "Prowlarr":
                return f"Prowlarr {version}"
        raise IndexerError("Prowlarr: the URL answered, but not like Prowlarr does; check the URL")

    def web_search_url(self, query: str) -> str | None:
        base = self._base_url()
        return f"{base}/search" if base else None

    def _base_url(self) -> str:
        """The configured URL without trailing slashes; http:// when it names no scheme."""
        url = self._url().strip().rstrip("/")
        if url and "://" not in url:
            url = f"http://{url}"
        return url

    def _connection(self) -> tuple[str, dict[str, str]]:
        base = self._base_url()
        key = self._api_key().strip()
        if not (base and key):
            raise IndexerError("Prowlarr is not set up: enter its URL and API key in Settings")
        if not (key.isascii() and key.isprintable()):  # e.g. a pasted zero-width space
            raise IndexerError("Prowlarr API key contains invalid characters")
        return base, {"X-Api-Key": key}

    def _status_error(self, path: str, status: int) -> IndexerError:
        if status == 401:
            return IndexerError("Prowlarr rejected the API key")
        if status == 404:
            return IndexerError(f"Prowlarr: {path} not found (HTTP 404); check the URL")
        return super()._status_error(path, status)

    def _release(self, raw: object) -> TorrentRelease | None:
        item = _ProwlarrRelease.model_validate(raw)
        if item.protocol.lower() != "torrent":  # usenet hits need a download client we lack
            return None
        # Prowlarr sometimes hands out a magnet as downloadUrl, or a proxied link as magnetUrl.
        magnet = magnet_link(item.magnet_url) or magnet_link(item.download_url)
        download = http_url(item.download_url) or http_url(item.magnet_url)
        source = f"prowlarr:{item.indexer}" if item.indexer else self.name
        return TorrentRelease(
            title=item.title.strip(),
            source=source,
            size_bytes=item.size,
            seeders=item.seeders,
            leechers=item.leechers,
            info_hash=item.info_hash,
            magnet_uri=magnet,
            download_url=download,
            info_url=http_url(item.info_url),
            published_at=item.publish_date,
        )
