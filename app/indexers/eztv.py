"""EZTV: TV torrents looked up by the show's IMDb id."""

from __future__ import annotations

import re
from urllib.parse import quote

from app.indexers.base import BaseIndexer, LenientInt, WireModel, from_unix, http_url, magnet_link
from app.models.domain import SearchKind, SearchRequest, TorrentRelease
from app.models.types import Count

_BASE = "https://eztvx.to"
_PATH = "/api/get-torrents"
_PAGE_SIZE = 100
_MAX_PAGES = 5
# Season packs are filed as episode 0; a few carry an episode number but say so in the title.
_WHOLE_SEASON = re.compile(
    r"(?<![a-z0-9])(?:complete|full[ ._-]*season)(?![a-z0-9])", re.IGNORECASE
)


class _EztvTorrent(WireModel):
    title: str
    hash: str | None = None
    magnet_url: str | None = None
    torrent_url: str | None = None
    seeds: Count = 0
    peers: Count = 0
    size_bytes: Count = 0
    season: LenientInt = None
    episode: LenientInt = None
    date_released_unix: LenientInt = None


class EztvIndexer(BaseIndexer):
    name = "eztv"
    label = "EZTV"
    kinds = frozenset({SearchKind.EPISODE, SearchKind.SEASON})

    def supports(self, request: SearchRequest) -> bool:
        """Needs the show's IMDb id and the season (and episode, for an episode search)."""
        if not super().supports(request) or request.season is None:
            return False
        if not (request.imdb_id and request.imdb_id.removeprefix("tt").isdigit()):
            return False
        return request.kind is not SearchKind.EPISODE or request.episode is not None

    async def search(self, request: SearchRequest) -> list[TorrentRelease]:
        if not self.supports(request) or request.imdb_id is None:
            return []
        imdb_digits = request.imdb_id.removeprefix("tt")  # EZTV keeps leading zeros: "0944947"
        releases: list[TorrentRelease] = []
        for page in range(1, _MAX_PAGES + 1):
            params = {"imdb_id": imdb_digits, "limit": str(_PAGE_SIZE), "page": str(page)}
            payload = await self._get_json(_BASE, _PATH, params)
            if not isinstance(payload, dict):
                raise self._unexpected(_PATH, f"expected an object, got {type(payload).__name__}")
            torrents = payload.get("torrents")
            if torrents is None:  # past the last page, or no torrents at all
                break
            releases.extend(
                self._parse_releases(_PATH, torrents, lambda raw: self._release(raw, request))
            )
            if len(torrents) < _PAGE_SIZE:
                break
        return releases

    def web_search_url(self, query: str) -> str | None:
        return f"{_BASE}/search/{quote(query.strip(), safe='')}"

    def _release(self, raw: object, request: SearchRequest) -> TorrentRelease | None:
        item = _EztvTorrent.model_validate(raw)
        if not _matches(item, request):
            return None
        return TorrentRelease(
            title=item.title,
            source=self.name,
            size_bytes=item.size_bytes,
            seeders=item.seeds,
            leechers=item.peers,
            info_hash=item.hash,
            magnet_uri=magnet_link(item.magnet_url),
            download_url=http_url(item.torrent_url),
            published_at=from_unix(item.date_released_unix),
        )


def _matches(item: _EztvTorrent, request: SearchRequest) -> bool:
    if item.season is None or item.season != request.season:
        return False
    if request.kind is SearchKind.EPISODE:
        return item.episode == request.episode
    return item.episode == 0 or bool(_WHOLE_SEASON.search(item.title))

