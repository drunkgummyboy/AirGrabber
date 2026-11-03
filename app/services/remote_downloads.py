"""The torrent program on a NAS or another computer that Settings names: DownloadMode.REMOTE
sends every grab there, FOLDER the releases without a .torrent file.

`client()` picks the client for `Settings.remote_kind` on every call, and each client reads the
address and account from the settings on every operation, so a change in Settings applies at
once. `status` / `free_space` are the Downloads page's read-only calls; `pause` / `resume` /
`remove` its ⋯ menu's (by `RemoteTask.task_id`, files never deleted). The qBittorrent on this
PC that AirGrabber set up is started when it isn't running (app/services/qbittorrent_local.py).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import httpx

from app.i18n import t
from app.models import RemoteKind, RemoteStatus
from app.services.qbittorrent import QBittorrentClient
from app.services.qbittorrent_local import (
    LocalQBittorrent,
    QBittorrentStarter,
    local_port,
    set_up_by_airgrabber,
)
from app.services.remote import (
    DiskSpace,
    HttpClients,
    RemoteClient,
    RemoteError,
    RemoteStatusClient,
    RemoteTaskActions,
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
        starter: QBittorrentStarter | None = None,  # starts AirGrabber's qBittorrent on this PC
    ) -> None:
        self._settings = settings
        if clients is None:
            clients = make_clients(settings, HttpClients(transport=transport))
        self._clients = dict(clients)
        self._starter = starter

    @property
    def configured(self) -> bool:
        """The program in Settings has an address (and, for Download Station, an account)."""
        return self._settings.current.remote_configured

    @property
    def kind(self) -> RemoteKind:
        return self._settings.current.remote_kind

    def client(self) -> RemoteClient:
        """The client for the program Settings names now; for the qBittorrent on this PC that
        AirGrabber set up (with a `starter`) one that starts qBittorrent when it isn't running."""
        settings = self._settings.current
        client = self._clients[settings.remote_kind]
        port = local_port(settings.remote_url)
        if self._starter is not None and port is not None and set_up_by_airgrabber(settings):
            return LocalQBittorrent(client, self._starter, port)
        return client

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

    @property
    def on_this_pc(self) -> bool:
        """The program in Settings runs on this PC (its address is 127.0.0.1 / localhost)."""
        return local_port(self._settings.current.remote_url) is not None

    async def pause(self, task_ids: Collection[str]) -> None:
        """Pause these tasks (by `RemoteTask.task_id`). Raises RemoteError."""
        await self._actions().pause(task_ids)

    async def resume(self, task_ids: Collection[str]) -> None:
        """Resume these tasks. Raises RemoteError."""
        await self._actions().resume(task_ids)

    async def remove(self, task_ids: Collection[str]) -> None:
        """Remove these tasks from the program; their files stay (see each client). Raises
        RemoteError."""
        await self._actions().remove(task_ids)

    def _actions(self) -> RemoteTaskActions:
        client = self.client()
        if not isinstance(client, RemoteTaskActions):
            raise RemoteError(
                t("AirGrabber can't change the downloads of {program}.", program=client.kind.program)
            )
        return client
