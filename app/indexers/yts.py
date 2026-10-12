"""YTS: small encodes of films, with real .torrent links."""

from __future__ import annotations

import logging
from urllib.parse import quote

from app.indexers.base import (
    FAILED_ATTEMPTS,
    BaseIndexer,
    IndexerError,
    LenientInt,
    WireModel,
    from_unix,
    http_url,
    text_queries,
)
from app.models.domain import SearchKind, SearchRequest, TorrentRelease
from app.models.types import Count

log = logging.getLogger(__name__)

# Tried in order. YTS announced the move to the first one in its responses' `@meta.migration`;
# the old base still answers for now and serves as the fallback.
BASE_URLS = ("https://movies-api.accel.li/api/v2", "https://yts.gg/api/v2")
_PATH = "/list_movies.json"


class _YtsTorrent(WireModel):
    url: str | None = None
    hash: str | None = None
    quality: str | None = None  # "720p", "1080p", "2160p", "3D"
    type: str | None = None  # "bluray", "web"
    video_codec: str | None = None  # "x264", "x265"
    seeds: Count = 0
    peers: Count = 0
    size_bytes: Count = 0
    date_uploaded_unix: LenientInt = None


class _YtsMovie(WireModel):
    title: str
    title_long: str | None = None  # "The Shawshank Redemption (1994)"
    year: LenientInt = None
    imdb_code: str | None = None
    url: str | None = None  # the film's page on YTS
    torrents: list[object] = []


class YtsIndexer(BaseIndexer):
    name = "yts"
    label = "YTS"
    kinds = frozenset({SearchKind.MOVIE})

    async def search(self, request: SearchRequest) -> list[TorrentRelease]:
        if not self.supports(request):
            return []
        imdb = request.imdb_id
        if imdb:
            term = imdb
        elif queries := text_queries(request):
            term = queries[0]
        else:
            return []
        movies = self._parse_items(_PATH, await self._list_movies(term), _YtsMovie.model_validate)
        releases: list[TorrentRelease] = []
        for movie in movies:
            if imdb:
                if movie.imdb_code and movie.imdb_code.lower() != imdb.lower():
                    continue
            elif request.year is not None and movie.year != request.year:
                continue
            # Called at once, so the lambda sees this iteration's movie.
            releases.extend(
                self._parse_releases(_PATH, movie.torrents, lambda raw: self._release(raw, movie))
            )
        return releases

    def web_search_url(self, query: str) -> str | None:
        term = quote(query.strip(), safe="")
        return f"https://yts.gg/browse-movies/{term}/all/all/0/latest/0/all"

    async def _list_movies(self, term: str) -> object:
        """The raw `data.movies` list for `term` ([] if absent), from the first base that answers.

        A base that fails (unreachable, 5xx, not JSON, unexpected shape) gets one attempt before
        the next base is tried; the last base gets the usual retries.
        """
        last = len(BASE_URLS) - 1
        for index, base in enumerate(BASE_URLS):
            attempts = FAILED_ATTEMPTS if index == last else 1
            try:
                payload = await self._get_json(
                    base, _PATH, {"query_term": term}, failed_attempts=attempts
                )
                return self._movies(payload)
            except IndexerError as exc:
                if index == last:
                    raise
                log.warning("%s; trying the next YTS API base", exc)
        raise AssertionError("BASE_URLS is empty")

    def _movies(self, payload: object) -> object:
        if not isinstance(payload, dict):
            raise self._unexpected(_PATH, f"expected an object, got {type(payload).__name__}")
        if payload.get("status", "ok") != "ok":
            raise self._unexpected(_PATH, f"status {str(payload.get('status'))[:40]!r}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise self._unexpected(_PATH, "no 'data' object")
        movies = data.get("movies")
        return [] if movies is None else movies  # no `movies` key when nothing matched

    def _release(self, raw: object, movie: _YtsMovie) -> TorrentRelease:
        item = _YtsTorrent.model_validate(raw)
        # Bracketed tags keep TorrentRelease's resolution and HEVC detection working.
        tags = [tag for tag in (item.quality, item.type, item.video_codec) if tag] + ["YTS"]
        title = f"{movie.title_long or movie.title} " + " ".join(f"[{tag}]" for tag in tags)
        return TorrentRelease(
            title=title,
            source=self.name,
            size_bytes=item.size_bytes,
            seeders=item.seeds,
            leechers=item.peers,
            info_hash=item.hash,
            download_url=http_url(item.url),
            info_url=http_url(movie.url),
            published_at=from_unix(item.date_uploaded_unix),
        )
