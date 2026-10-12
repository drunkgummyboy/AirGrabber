"""APIBay: The Pirate Bay's JSON search API."""

from __future__ import annotations

from urllib.parse import urlencode

from app.indexers.base import BaseIndexer, LenientInt, WireModel, from_unix, text_queries
from app.models.domain import SearchKind, SearchRequest, TorrentRelease
from app.models.types import Count

_BASE = "https://apibay.org"
_PATH = "/q.php"
# The Pirate Bay's "Video" top-level category: 201 Movies, 205 TV shows, 208 HD TV, 212 UHD...
_VIDEO_CATEGORIES = range(200, 300)


class _ApibayItem(WireModel):
    id: int
    name: str
    info_hash: str
    size: Count = 0
    seeders: Count = 0
    leechers: Count = 0
    added: LenientInt = None  # unix seconds
    category: LenientInt = None


class ApibayIndexer(BaseIndexer):
    name = "apibay"
    label = "APIBay"
    kinds = frozenset(SearchKind)

    async def search(self, request: SearchRequest) -> list[TorrentRelease]:
        if not self.supports(request):
            return []
        return await self._first_hit(text_queries(request), self._search_query)

    def web_search_url(self, query: str) -> str | None:
        return "https://thepiratebay.org/search.php?" + urlencode({"q": query.strip()})

    async def _search_query(self, query: str) -> list[TorrentRelease]:
        payload = await self._get_json(_BASE, _PATH, {"q": query})
        return self._parse_releases(_PATH, payload, self._release)

    def _release(self, raw: object) -> TorrentRelease | None:
        item = _ApibayItem.model_validate(raw)
        if item.id == 0:  # the lone "No results returned" placeholder
            return None
        if item.category not in _VIDEO_CATEGORIES:
            return None
        return TorrentRelease(
            title=item.name,
            source=self.name,
            size_bytes=item.size,
            seeders=item.seeders,
            leechers=item.leechers,
            info_hash=item.info_hash,
            info_url=f"https://thepiratebay.org/description.php?id={item.id}",
            published_at=from_unix(item.added),
        )
