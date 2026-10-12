"""Torrent indexers: one strategy per source, each turning a SearchRequest into TorrentReleases."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from app.indexers.apibay import ApibayIndexer
from app.indexers.base import USER_AGENT, BaseIndexer, IndexerError
from app.indexers.bitsearch import BitsearchIndexer
from app.indexers.eztv import EztvIndexer
from app.indexers.prowlarr import ProwlarrIndexer
from app.indexers.yts import YtsIndexer


def default_indexers(
    http: httpx.AsyncClient,
    *,
    prowlarr_url: Callable[[], str],
    prowlarr_api_key: Callable[[], str],
) -> list[BaseIndexer]:
    """Every indexer, built-in sources first. The Prowlarr settings are read on every call."""
    return [
        ApibayIndexer(http),
        EztvIndexer(http),
        BitsearchIndexer(http),
        YtsIndexer(http),
        ProwlarrIndexer(http, url=prowlarr_url, api_key=prowlarr_api_key),
    ]


__all__ = [
    "USER_AGENT",
    "ApibayIndexer",
    "BaseIndexer",
    "BitsearchIndexer",
    "EztvIndexer",
    "IndexerError",
    "ProwlarrIndexer",
    "YtsIndexer",
    "default_indexers",
]
