"""The torrent program on a NAS or another computer that Settings names: DownloadMode.REMOTE
sends every grab there, FOLDER the releases without a .torrent file.

`client()` picks the client for `Settings.remote_kind` on every call, and each client reads the
address and account from the settings on every operation, so a change in Settings applies at
once. `status` / `free_space` are the Downloads page's read-only calls.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import httpx

from app.i18n import t
from app.models import RemoteKind, RemoteStatus
from app.services.qbittorrent import QBittorrentClient
from app.services.remote import (
    DiskSpace,
    HttpClients,
    RemoteClient,
    RemoteError,
    RemoteStatusClient,
)
from app.services.settings import SettingsService
from app.services.synology import DownloadStationClient
from app.services.transmission import TransmissionClient


def make_clients(
    settings: SettingsService, http: HttpClients
) -> dict[RemoteKind, RemoteClient]:
    """One client per kind of program, sharing `http` (its cached SSL contexts)."""
    return {
        RemoteKind.SYNOLOGY: DownloadStationClient(settings, http=http),
        RemoteKind.QBITTORRENT: QBittorrentClient(settings, http=http),
        RemoteKind.TRANSMISSION: TransmissionClient(settings, http=http),
    }


class RemoteDownloads:
    def __init__(
        self,
        settings: SettingsService,
        *,
        clients: Mapping[RemoteKind, RemoteClient] | None = None,  # tests: fakes
        transport: httpx.AsyncBaseTransport | None = None,  # tests only
    ) -> None:
        self._settings = settings
        if clients is None:
            clients = make_clients(settings, HttpClients(transport=transport))
        self._clients = dict(clients)

    @property
    def configured(self) -> bool:
        """The program in Settings has an address (and, for Download Station, an account)."""
        return self._settings.current.remote_configured

    @property
    def kind(self) -> RemoteKind:
        return self._settings.current.remote_kind

    def client(self) -> RemoteClient:
        """The client for the program Settings names now."""
        return self._clients[self._settings.current.remote_kind]

    async def test(self) -> str:
        """Settings' "Test connection": e.g. "qBittorrent v5.0.2". Raises RemoteError."""
        return await self.client().test()

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        """The tasks of the program Settings names now (see RemoteStatusClient.status).
        Raises RemoteError."""
        client = self.client()
        if not isinstance(client, RemoteStatusClient):
            raise RemoteError(
                t("AirGrabber can't read the downloads of {program}.", program=client.kind.program)
            )
        return await client.status(hashes)

    async def free_space(self) -> DiskSpace | None:
        """Free space on that program's download disk, when it reports it. Raises
        RemoteError."""
        client = self.client()
        if not isinstance(client, RemoteStatusClient):
            return None
        return await client.free_space()
