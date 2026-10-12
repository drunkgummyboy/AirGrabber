"""Bitsearch (formerly SolidTorrents): a DHT search engine with a JSON API."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from pydantic import Field

from app.indexers.base import BaseIndexer, WireModel, text_queries
from app.models.domain import SearchKind, SearchRequest, TorrentRelease
from app.models.types import Count

_BASE = "https://bitsearch.eu"
_PATH = "/api/v1/search"


class _BitsearchResult(WireModel):
    infohash: str
    title: str
    size: Count = 0
    seeders: Count = 0
    leechers: Count = 0
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class BitsearchIndexer(BaseIndexer):
    name = "bitsearch"
    label = "Bitsearch"
    kinds = frozenset(SearchKind)

    async def search(self, request: SearchRequest) -> list[TorrentRelease]:
        if not self.supports(request):
            return []
        return await self._first_hit(text_queries(request), self._search_query)

    def web_search_url(self, query: str) -> str | None:
        return f"{_BASE}/search?" + urlencode({"q": query.strip()})

    async def _search_query(self, query: str) -> list[TorrentRelease]:
        payload = await self._get_json(_BASE, _PATH, {"q": query})
        if not isinstance(payload, dict):
            raise self._unexpected(_PATH, f"expected an object, got {type(payload).__name__}")
        if payload.get("success") is False:
            raise self._unexpected(_PATH, "the search reported failure")
        return self._parse_releases(_PATH, payload.get("results"), self._release)

    def _release(self, raw: object) -> TorrentRelease:
        item = _BitsearchResult.model_validate(raw)
        # No magnet in the payload: TorrentRelease.magnet builds one from the info hash.
        return TorrentRelease(
            title=item.title.strip(),
            source=self.name,
            size_bytes=item.size,
            seeders=item.seeders,
            leechers=item.leechers,
            info_hash=item.infohash,
            published_at=item.updated_at,
        )
