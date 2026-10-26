"""The remote torrent programs' read-only status calls (the Downloads page): Download Station's
task list, qBittorrent's torrents/info + sync/maindata, Transmission's torrent-get + free-space,
on the fake servers of the client tests (httpx.MockTransport) with the answers in
tests/fixtures/<program>/ (built from each API's documentation; see their README.md)."""

from __future__ import annotations

import json
import logging
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.database import SettingsRepository
from app.models import DownloadState, RemoteKind, RemoteStatus, Settings
from app.services.qbittorrent import QBittorrentClient, qbittorrent_state
from app.services.remote import DiskSpace, RemoteError, magnet_hash, normal_hash
from app.services.remote_downloads import RemoteDownloads
from app.services.settings import SettingsService
from app.services.synology import DownloadStationClient, ds_state
from app.services.transmission import TransmissionClient, transmission_state
from tests import test_qbittorrent as qbt
from tests import test_synology as dsm
from tests import test_transmission as tr

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(path: str) -> Any:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


def states(status: RemoteStatus) -> dict[str, tuple[DownloadState, float]]:
    return {task.name: (task.state, round(task.progress, 4)) for task in status.tasks}


class HelperTests(unittest.TestCase):
    def test_magnet_hash_reads_hex_and_base32_and_nothing_else(self) -> None:
        self.assertEqual(
            magnet_hash("magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=x"),
            "abcdef0123456789abcdef0123456789abcdef01",
        )
        self.assertEqual(
            magnet_hash("magnet:?dn=x&xt=urn:btih:CEIRCEIRCEIRCEIRCEIRCEIRCEIRCEIR"), "1" * 40
        )
        self.assertIsNone(magnet_hash("magnet:?dn=no.hash"))
        self.assertIsNone(magnet_hash("Some.File.torrent"))
        self.assertIsNone(normal_hash("not a hash"))
        self.assertIsNone(normal_hash(42))

    def test_state_rules(self) -> None:
        self.assertIs(ds_state("paused", 1.0), DownloadState.READY)
        self.assertIs(ds_state("paused", 0.4), DownloadState.STOPPED)
        self.assertIs(ds_state("something_new", 0.4), DownloadState.WAITING)
        self.assertIs(qbittorrent_state("stoppedDL", 0.2), DownloadState.STOPPED)
        self.assertIs(qbittorrent_state("stalledDL", 0.2), DownloadState.DOWNLOADING)
        self.assertIs(qbittorrent_state("brandNewState", 1.0), DownloadState.READY)
        # A tracker error doesn't stop a download; a local error (disk full) does.
        self.assertIs(transmission_state(4, 2, 0.3), DownloadState.DOWNLOADING)
        self.assertIs(transmission_state(4, 3, 0.3), DownloadState.PROBLEM)
        self.assertIs(transmission_state(0, 0, 1.0), DownloadState.READY)
        self.assertIs(transmission_state(0, 0, 0.3), DownloadState.STOPPED)


# -- Download Station ----------------------------------------------------------------------------


class SynologyStatusTests(dsm.SynologyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.answers = [dsm.ok(fixture("synology/task_list.json")["data"])]
        self.dsm.overrides[(dsm.TASK, "list")] = self.next_answer

    def next_answer(self) -> httpx.Response:
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    async def test_lists_the_tasks_with_state_progress_hash_and_file_name(self) -> None:
        status = await self.client.status()
        self.assertEqual(status.program, "Download Station")
        self.assertEqual(
            states(status),
            {
                "Fallout.S02E03.1080p.WEB.h264": (DownloadState.DOWNLOADING, 0.42),
                "Gladiator.II.2024.1080p.WEB-DL": (DownloadState.READY, 1.0),
                "The.Bear.S04E01.720p": (DownloadState.WAITING, 0.0),
                "Severance.S02E01.1080p": (DownloadState.PROBLEM, 0.1),
            },
        )
        fallout, gladiator = status.tasks[0], status.tasks[1]
        self.assertEqual(fallout.info_hash, "1" * 40)  # from its magnet
        self.assertIsNone(fallout.file_name)
        self.assertEqual(gladiator.file_name, "Gladiator II 2024 1080p WEB-DL.torrent")
        self.assertIsNone(gladiator.info_hash)
        form = self.dsm.form(dsm.TASK, "list")
        self.assertEqual(
            (form["version"], form["additional"], form["limit"], form["offset"]),
            ("1", "detail,transfer", "-1", "0"),
        )
        self.assertEqual(form["_sid"], dsm.SID)
        # The links themselves are never kept.
        self.assertNotIn("magnet:", repr(status))
        self.assertNotIn("dn=", repr(status))

    async def test_keeps_its_sign_in_between_calls(self) -> None:
        await self.client.status()
        await self.client.status()
        self.assertEqual(
            self.dsm.methods(),
            [(dsm.AUTH, "login"), (dsm.TASK, "list"), (dsm.TASK, "list")],
        )
        self.assertEqual(sum(1 for r in self.dsm.requests if r.url.path.endswith("query.cgi")), 1)

    async def test_signs_in_again_when_dsm_ended_the_session(self) -> None:
        await self.client.status()
        self.answers[:0] = [dsm.failure(106)]
        status = await self.client.status()
        self.assertEqual(len(status.tasks), 4)
        self.assertEqual(
            self.dsm.methods(),
            [
                (dsm.AUTH, "login"),
                (dsm.TASK, "list"),
                (dsm.TASK, "list"),  # 106: session timed out
                (dsm.AUTH, "login"),
                (dsm.TASK, "list"),
            ],
        )

    async def test_other_errors_are_not_retried(self) -> None:
        await self.client.status()
        self.answers[:0] = [dsm.failure(102)]
        error = await self.error_of(self.client.status())
        self.assertIn("doesn't offer the Download Station API", str(error))
        self.assertEqual(self.dsm.methods().count((dsm.AUTH, "login")), 1)

    async def test_a_changed_password_signs_in_again(self) -> None:
        await self.client.status()
        await self.configure(remote_password="NEWPASS456")
        await self.client.status()
        self.assertEqual(self.dsm.methods().count((dsm.AUTH, "login")), 2)
        self.assertEqual(self.dsm.form(dsm.AUTH, "login")["passwd"], dsm.PASSWORD)

    async def test_a_box_with_only_downloadstation2_lists_through_it(self) -> None:
        info = dsm.api_info()
        del info["data"][dsm.TASK]
        self.dsm.info = info
        self.dsm.overrides[(dsm.TASK2, "list")] = lambda: dsm.ok(
            fixture("synology/task2_list.json")["data"]
        )
        status = await self.client.status()
        self.assertEqual(
            states(status),
            {
                "Fallout.S02E03.1080p.WEB.h264": (DownloadState.DOWNLOADING, 0.25),
                "Some.Film.2023.1080p": (DownloadState.READY, 1.0),  # unknown number, all there
                "Broken": (DownloadState.PROBLEM, 0.01),  # 100 and up: an error
            },
        )
        self.assertEqual(status.tasks[0].info_hash, "1" * 40)  # base32 in the magnet
        form = self.dsm.form(dsm.TASK2, "list")
        self.assertEqual(json.loads(form["additional"]), ["detail", "transfer"])

    async def test_unreachable_nas_is_one_plain_line(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno 111] http://192.168.1.20:5000 refused", request=request)

        client = self.make_client(refuse)
        error = await self.error_of(client.status())
        self.assertEqual(
            str(error),
            f"Can't reach the NAS at {dsm.HOST}. Check the address in Settings and that the NAS "
            "is on.",
        )
        self.assertIsNone(error.__context__)

    async def test_not_set_up(self) -> None:
        await self.configure(remote_password="")
        error = await self.error_of(self.client.status())
        self.assertIn("isn't set up", str(error))
        self.assertEqual(self.dsm.requests, [])

    async def test_reports_no_free_space(self) -> None:
        self.assertIsNone(await self.client.free_space())


# -- qBittorrent ---------------------------------------------------------------------------------


class FakeQBittorrentStatus(qbt.FakeQBittorrent):
    """Adds torrents/info and sync/maindata; `expire()` ends the session (a new SID)."""

    def __init__(self) -> None:
        super().__init__()
        self.sid = qbt.SID
        self.logins = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.partition("/api/v2/")[2]
        if path == "auth/login":
            self.logins += 1
            response = super().__call__(request)
            if "Set-Cookie" in response.headers:
                response.headers["Set-Cookie"] = f"SID={self.sid}; path=/"
            return response
        if path in ("torrents/info", "sync/maindata"):
            self.requests.append(request)
            self.calls.append(path)
            if not self.bypass_auth and request.headers.get("Cookie") != f"SID={self.sid}":
                return httpx.Response(403, text="Forbidden")
            name = "torrents_info.json" if path == "torrents/info" else "sync_maindata.json"
            return httpx.Response(200, json=fixture(f"qbittorrent/{name}"))
        return super().__call__(request)

    def expire(self) -> None:
        self.sid = "NewSessionId0000000000000000000"


class QBittorrentStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server = FakeQBittorrentStatus()
        self.settings = qbt.settings_service()
        self.client = QBittorrentClient(self.settings, transport=httpx.MockTransport(self.server))

    async def test_lists_the_torrents_with_state_and_progress(self) -> None:
        status = await self.client.status()
        self.assertEqual(status.program, "qBittorrent")
        self.assertEqual(
            states(status),
            {
                "Fallout.S02E03.1080p.WEB.h264": (DownloadState.DOWNLOADING, 0.4216),
                "Gladiator.II.2024.1080p.WEB-DL": (DownloadState.READY, 1.0),
                "The.Bear.S04E01.720p": (DownloadState.WAITING, 0.0),
                "Severance.S02E01.1080p": (DownloadState.STOPPED, 0.1),
                "Andor.S02E01.1080p": (DownloadState.PROBLEM, 0.5),
            },
        )
        self.assertEqual(status.tasks[0].info_hash, "2" * 40)

    async def test_asks_for_just_the_hashes_it_needs(self) -> None:
        await self.client.status({"b" * 40, "a" * 40})
        info = next(r for r in self.server.requests if r.url.path.endswith("torrents/info"))
        self.assertEqual(info.url.params["hashes"], f"{'a' * 40}|{'b' * 40}")

    async def test_keeps_the_session_and_signs_in_again_when_it_ended(self) -> None:
        await self.client.status()
        await self.client.status()
        self.assertEqual(self.server.logins, 1)
        self.assertNotIn("auth/logout", self.server.calls)
        self.server.expire()
        status = await self.client.status()
        self.assertEqual(len(status.tasks), 5)
        self.assertEqual(self.server.logins, 2)
        self.assertEqual(
            self.server.calls,
            [
                "auth/login",
                "torrents/info",
                "torrents/info",
                "torrents/info",  # 403: the session ended
                "auth/login",
                "torrents/info",
            ],
        )

    async def test_without_a_user_name_it_doesnt_sign_in(self) -> None:
        await self.settings.update(remote_username="", remote_password="")
        self.server.bypass_auth = True
        await self.client.status()
        await self.client.status()
        self.assertEqual(self.server.logins, 0)

    async def test_a_wrong_password_is_plain(self) -> None:
        await self.settings.update(remote_password="wrong")
        with self.assertRaises(RemoteError) as caught:
            await self.client.status()
        self.assertEqual(str(caught.exception), "qBittorrent: wrong user name or password.")

    async def test_free_space_from_maindata(self) -> None:
        self.assertEqual(await self.client.free_space(), DiskSpace(12884901888, None))

    async def test_secrets_stay_out_of_urls_and_logs(self) -> None:
        with self.assertLogs("app", level=logging.DEBUG) as logs:
            logging.getLogger("app").debug("start")
            await self.client.status()
            self.server.expire()
            await self.client.status()
        for request in self.server.requests:
            self.assertNotIn(qbt.PASSWORD, str(request.url))
            self.assertNotIn(qbt.SID, str(request.url))
        self.assertFalse(any(qbt.SID in line or qbt.PASSWORD in line for line in logs.output))


# -- Transmission --------------------------------------------------------------------------------


class TransmissionStatusTests(tr.TransmissionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server.answers["torrent-get"] = lambda body: fixture("transmission/torrent_get.json")
        self.server.answers["free-space"] = lambda body: fixture("transmission/free_space.json")
        self.server.answers["session-get"] = lambda body: {
            "arguments": {"download-dir": "/downloads/complete"},
            "result": "success",
        }

    async def test_lists_the_torrents_with_state_and_progress(self) -> None:
        status = await self.client.status()
        self.assertEqual(status.program, "Transmission")
        self.assertEqual(
            states(status),
            {
                "Fallout.S02E03.1080p.WEB.h264": (DownloadState.DOWNLOADING, 0.4216),
                "The.Bear.S04E01.720p": (DownloadState.DOWNLOADING, 0.25),  # tracker error
                "Gladiator.II.2024.1080p.WEB-DL": (DownloadState.READY, 1.0),
                "Severance.S02E01.1080p": (DownloadState.STOPPED, 0.1),
                "Andor.S02E01.1080p": (DownloadState.PROBLEM, 0.5),
                "Shogun.S01E01": (DownloadState.WAITING, 0.0),
            },
        )
        self.assertEqual(status.tasks[0].info_hash, "7" * 40)
        body = self.server.bodies[-1]
        self.assertEqual(body["method"], "torrent-get")
        self.assertIn("hashString", body["arguments"]["fields"])
        self.assertNotIn("ids", body["arguments"])

    async def test_asks_for_just_the_hashes_it_needs(self) -> None:
        await self.client.status({"b" * 40, "a" * 40})
        self.assertEqual(self.server.bodies[-1]["arguments"]["ids"], ["a" * 40, "b" * 40])

    async def test_keeps_the_session_id_between_status_calls(self) -> None:
        await self.client.status()
        before = len(self.server.requests)
        await self.client.status()
        self.assertEqual(len(self.server.requests) - before, 1)  # no 409 handshake again

    async def test_reads_transmission_41_snake_case_fields(self) -> None:
        self.server.answers["torrent-get"] = lambda body: {
            "arguments": {
                "torrents": [
                    {"hash_string": "d" * 40, "name": "X", "percent_done": 0.5, "status": 4,
                     "error": 0, "is_finished": False}
                ]
            },
            "result": "success",
        }
        status = await self.client.status()
        self.assertEqual(states(status), {"X": (DownloadState.DOWNLOADING, 0.5)})
        self.assertEqual(status.tasks[0].info_hash, "d" * 40)

    async def test_free_space_of_its_download_folder(self) -> None:
        self.assertEqual(
            await self.client.free_space(), DiskSpace(12884901888, 4000787030016)
        )
        methods = [body["method"] for body in self.server.bodies]
        self.assertEqual(methods, ["session-get", "free-space"])
        self.assertEqual(self.server.bodies[-1]["arguments"], {"path": "/downloads/complete"})

    async def test_free_space_of_the_destination_in_settings(self) -> None:
        await self.settings.update(remote_destination="/volume1/video")
        await self.client.free_space()
        self.assertEqual([body["method"] for body in self.server.bodies], ["free-space"])
        self.assertEqual(self.server.bodies[-1]["arguments"], {"path": "/volume1/video"})

    async def test_refused_is_plain(self) -> None:
        self.server.whitelisted = False
        error = await self.error_of(self.client.status())
        self.assertIn("RPC whitelist", str(error))


# -- RemoteDownloads -----------------------------------------------------------------------------


class OnlyAdds:
    """A remote client without status calls."""

    kind = RemoteKind.SYNOLOGY

    async def test(self) -> str:
        return "x"

    async def add_magnet(self, magnet: str) -> None:
        pass

    async def add_torrent(self, content: bytes, filename: str) -> None:
        pass


class RemoteDownloadsStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_client_without_status_calls(self) -> None:
        settings = SettingsService(mock.Mock(spec=SettingsRepository), Settings())
        remote = RemoteDownloads(settings, clients={kind: OnlyAdds() for kind in RemoteKind})
        with self.assertRaises(RemoteError):
            await remote.status()
        self.assertIsNone(await remote.free_space())

    async def test_uses_the_program_in_settings(self) -> None:
        server = FakeQBittorrentStatus()
        settings = qbt.settings_service()
        remote = RemoteDownloads(settings, transport=httpx.MockTransport(server))
        self.assertEqual((await remote.status()).program, "qBittorrent")
        self.assertIsInstance(remote.client(), QBittorrentClient)
        await settings.update(remote_kind=RemoteKind.TRANSMISSION)
        self.assertIsInstance(remote.client(), TransmissionClient)
        await settings.update(remote_kind=RemoteKind.SYNOLOGY)
        self.assertIsInstance(remote.client(), DownloadStationClient)


if __name__ == "__main__":
    unittest.main()
