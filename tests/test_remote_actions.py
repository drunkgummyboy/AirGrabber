"""The Downloads page's ⋯ menu on the remote torrent programs (3.0.2): pause, resume and remove
by task id, on the status calls' kept sign-in, never deleting files (Download Station's
`force_complete=false`, qBittorrent's `deleteFiles=false`, Transmission's `delete-local-data:
false`), and the task id, size and added time each program's task list gives. On the fake
servers of the client tests (httpx.MockTransport) with the answers in tests/fixtures/<program>/."""

from __future__ import annotations

import json
import logging
import unittest
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.database import SettingsRepository
from app.models import RemoteKind, RemoteStatus, Settings
from app.services.qbittorrent import QBittorrentClient
from app.services.qbittorrent_local import LocalQBittorrent
from app.services.remote import RemoteError, RemoteTaskActions, byte_count, epoch_time
from app.services.remote_downloads import RemoteDownloads
from app.services.settings import SettingsService
from app.services.synology import DownloadStationClient
from app.services.transmission import TransmissionClient
from tests import test_qbittorrent as qbt
from tests import test_synology as dsm
from tests import test_transmission as tr
from tests.test_remote_status import FakeQBittorrentStatus, OnlyAdds

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(path: str) -> Any:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


def at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


class HelperTests(unittest.TestCase):
    def test_sizes_and_times(self) -> None:
        self.assertEqual(byte_count("1500000000"), 1500000000)
        self.assertEqual(byte_count(2.0), 2)
        self.assertIsNone(byte_count(-1))
        self.assertIsNone(byte_count("lots"))
        self.assertEqual(epoch_time(1760950000), at(1760950000))
        self.assertEqual(epoch_time("1760950000"), at(1760950000))
        for nothing in (0, -1, None, "soon", 10**20):
            self.assertIsNone(epoch_time(nothing))

    def test_every_client_has_the_actions(self) -> None:
        settings = SettingsService(mock.Mock(spec=SettingsRepository), Settings())
        for client in (
            DownloadStationClient(settings),
            QBittorrentClient(settings),
            TransmissionClient(settings),
        ):
            self.assertIsInstance(client, RemoteTaskActions)


# -- Download Station ----------------------------------------------------------------------------


class SynologyActionTests(dsm.SynologyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dsm.overrides[(dsm.TASK, "list")] = lambda: dsm.ok(
            fixture("synology/task_list.json")["data"]
        )
        for method in ("pause", "resume", "delete"):
            self.dsm.overrides[(dsm.TASK, method)] = lambda: dsm.ok(
                fixture("synology/task_action.json")["data"]
            )

    async def test_the_task_list_gives_id_size_and_added_time(self) -> None:
        status = await self.client.status()
        fallout = status.tasks[0]
        self.assertEqual(
            (fallout.task_id, fallout.size, fallout.added_at),
            ("dbid_101", 1500000000, at(1760950000)),
        )
        self.assertNotIn("magnet:", repr(status))

    async def test_pause_resume_and_remove_on_the_kept_sign_in(self) -> None:
        await self.client.status()
        await self.client.pause(["dbid_101", "dbid_102"])
        await self.client.resume(["dbid_101"])
        await self.client.remove(["dbid_102"])
        self.assertEqual(
            self.dsm.methods(),
            [
                (dsm.AUTH, "login"),
                (dsm.TASK, "list"),
                (dsm.TASK, "pause"),
                (dsm.TASK, "resume"),
                (dsm.TASK, "delete"),
            ],
        )
        self.assertEqual(self.dsm.form(dsm.TASK, "pause")["id"], "dbid_101,dbid_102")
        self.assertEqual(self.dsm.form(dsm.TASK, "resume")["id"], "dbid_101")
        delete = self.dsm.form(dsm.TASK, "delete")
        # Never deletes files: a finished task's stay; force_complete is sent, and false.
        self.assertEqual((delete["id"], delete["force_complete"]), ("dbid_102", "false"))
        self.assertEqual(delete["_sid"], dsm.SID)
        for request in self.dsm.requests:
            self.assertNotIn(dsm.SID, str(request.url))

    async def test_an_action_signs_in_first_and_again_when_the_session_ended(self) -> None:
        await self.client.pause(["dbid_101"])
        self.assertEqual(self.dsm.methods(), [(dsm.AUTH, "login"), (dsm.TASK, "pause")])
        answers = [dsm.failure(106), dsm.ok(fixture("synology/task_action.json")["data"])]
        self.dsm.overrides[(dsm.TASK, "resume")] = lambda: answers.pop(0)
        await self.client.resume(["dbid_101"])
        self.assertEqual(
            self.dsm.methods()[2:],
            [(dsm.TASK, "resume"), (dsm.AUTH, "login"), (dsm.TASK, "resume")],
        )

    async def test_a_refused_task_is_one_plain_line(self) -> None:
        self.dsm.overrides[(dsm.TASK, "pause")] = lambda: dsm.ok(
            fixture("synology/task_action_refused.json")["data"]
        )
        error = await self.error_of(self.client.pause(["dbid_101", "dbid_102"]))
        self.assertEqual(
            str(error), "Download Station can't do that with this download right now."
        )
        self.assertNotIn("dbid", str(error))

    async def test_removing_a_task_that_is_gone_is_no_error(self) -> None:
        self.dsm.overrides[(dsm.TASK, "delete")] = lambda: dsm.ok(
            [{"error": 0, "id": "dbid_101"}, {"error": 404, "id": "dbid_999"}]
        )
        await self.client.remove(["dbid_101", "dbid_999"])
        self.dsm.overrides[(dsm.TASK, "delete")] = lambda: dsm.failure(404)
        await self.client.remove(["dbid_999"])
        # Pausing one that is gone says so.
        self.dsm.overrides[(dsm.TASK, "pause")] = lambda: dsm.failure(404)
        error = await self.error_of(self.client.pause(["dbid_999"]))
        self.assertEqual(str(error), "Download Station no longer has this download.")

    async def test_a_box_with_only_downloadstation2_uses_it(self) -> None:
        info = dsm.api_info()
        del info["data"][dsm.TASK]
        self.dsm.info = info
        for method in ("pause", "delete"):
            self.dsm.overrides[(dsm.TASK2, method)] = lambda: dsm.ok(
                fixture("synology/task2_action.json")["data"]
            )
        await self.client.pause(["dbid_201"])
        await self.client.remove(["dbid_201", "dbid_202"])
        pause = self.dsm.form(dsm.TASK2, "pause")
        self.assertEqual((pause["version"], json.loads(pause["id"])), ("2", ["dbid_201"]))
        delete = self.dsm.form(dsm.TASK2, "delete")
        self.assertEqual(json.loads(delete["id"]), ["dbid_201", "dbid_202"])
        self.assertIs(json.loads(delete["force_complete"]), False)

    async def test_nothing_to_do_asks_nothing(self) -> None:
        await self.client.remove([])
        self.assertEqual(self.dsm.requests, [])


# -- qBittorrent ---------------------------------------------------------------------------------


class FakeQBittorrentActions(FakeQBittorrentStatus):
    """Adds torrents/stop, start, delete (v5); `old = True` answers as 4.x (pause, resume)."""

    NEW = ("torrents/stop", "torrents/start", "torrents/delete")
    OLD = ("torrents/pause", "torrents/resume", "torrents/delete")

    def __init__(self) -> None:
        super().__init__()
        self.old = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.partition("/api/v2/")[2]
        known = self.OLD if self.old else self.NEW
        if path in self.NEW or path in self.OLD:
            self.requests.append(request)
            self.calls.append(path)
            if request.headers.get("Cookie") != f"SID={self.sid}":
                return httpx.Response(403, text="Forbidden")
            if self._cross_site(request):
                return httpx.Response(401, text="Unauthorized")
            if path not in known:
                return httpx.Response(404, text="Not Found")
            self.forms[path] = dict(httpx.QueryParams(request.content.decode()))
            return httpx.Response(200)
        return super().__call__(request)


class QBittorrentActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server = FakeQBittorrentActions()
        self.settings = qbt.settings_service()
        self.client = QBittorrentClient(self.settings, transport=httpx.MockTransport(self.server))

    async def test_the_torrent_list_gives_id_size_and_added_time(self) -> None:
        status = await self.client.status()
        fallout = status.tasks[0]
        self.assertEqual(
            (fallout.task_id, fallout.size, fallout.added_at),
            ("2" * 40, 1500000000, at(1760950000)),
        )
        self.assertIsNone(status.tasks[4].added_at)  # Andor: no added_on

    async def test_stop_start_and_delete_keeping_the_files(self) -> None:
        await self.client.pause(["A" * 40, "b" * 40])
        await self.client.resume(["a" * 40])
        await self.client.remove(["b" * 40])
        self.assertEqual(
            self.server.calls,
            ["auth/login", "torrents/stop", "torrents/start", "torrents/delete"],
        )
        self.assertEqual(self.server.logins, 1)  # one kept session
        self.assertEqual(self.server.forms["torrents/stop"], {"hashes": f"{'a' * 40}|{'b' * 40}"})
        self.assertEqual(
            self.server.forms["torrents/delete"], {"hashes": "b" * 40, "deleteFiles": "false"}
        )
        for request in self.server.requests:
            self.assertEqual(request.method, "POST")
            self.assertNotIn(qbt.PASSWORD, str(request.url))

    async def test_an_older_qbittorrent_pauses_and_resumes(self) -> None:
        self.server.old = True
        await self.client.pause(["a" * 40])
        await self.client.resume(["a" * 40])
        self.assertEqual(
            self.server.calls,
            ["auth/login", "torrents/stop", "torrents/pause", "torrents/start", "torrents/resume"],
        )
        self.assertEqual(self.server.forms["torrents/resume"], {"hashes": "a" * 40})

    async def test_signs_in_again_when_the_session_ended(self) -> None:
        await self.client.status()
        self.server.expire()
        await self.client.pause(["a" * 40])
        self.assertEqual(self.server.logins, 2)
        self.assertEqual(self.server.calls[-3:], ["torrents/stop", "auth/login", "torrents/stop"])

    async def test_a_refusal_is_plain(self) -> None:
        await self.settings.update(remote_password="wrong")
        with self.assertRaises(RemoteError) as caught:
            await self.client.remove(["a" * 40])
        self.assertEqual(str(caught.exception), "qBittorrent: wrong user name or password.")
        self.assertNotIn("torrents/delete", self.server.calls)


# -- Transmission --------------------------------------------------------------------------------


class TransmissionActionTests(tr.TransmissionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server.answers["torrent-get"] = lambda body: fixture("transmission/torrent_get.json")
        for method in ("torrent-stop", "torrent-start", "torrent-remove"):
            self.server.answers[method] = lambda body: fixture("transmission/torrent_action.json")

    async def test_the_torrent_list_gives_id_size_and_added_time(self) -> None:
        status = await self.client.status()
        fallout = status.tasks[0]
        self.assertEqual(
            (fallout.task_id, fallout.size, fallout.added_at),
            ("7" * 40, 1500000000, at(1760950000)),
        )
        fields = self.server.bodies[-1]["arguments"]["fields"]
        self.assertIn("totalSize", fields)
        self.assertIn("addedDate", fields)

    async def test_stop_start_and_remove_keeping_the_files(self) -> None:
        await self.client.pause(["A" * 40])
        await self.client.resume(["a" * 40, "b" * 40])
        await self.client.remove(["b" * 40])
        self.assertEqual(
            [(body["method"], body["arguments"]) for body in self.server.bodies],
            [
                ("torrent-stop", {"ids": ["a" * 40]}),
                ("torrent-start", {"ids": ["a" * 40, "b" * 40]}),
                ("torrent-remove", {"ids": ["b" * 40], "delete-local-data": False}),
            ],
        )

    async def test_reads_transmission_41_snake_case_fields(self) -> None:
        self.server.answers["torrent-get"] = lambda body: {
            "arguments": {
                "torrents": [
                    {"hash_string": "d" * 40, "name": "X", "percent_done": 0.5, "status": 4,
                     "error": 0, "total_size": 2048, "added_date": 1760950000}
                ]
            },
            "result": "success",
        }
        [task] = (await self.client.status()).tasks
        self.assertEqual((task.task_id, task.size, task.added_at), ("d" * 40, 2048, at(1760950000)))

    async def test_a_refusal_is_plain(self) -> None:
        self.server.answers["torrent-remove"] = lambda body: {
            "arguments": {}, "result": "torrent not found"
        }
        error = await self.error_of(self.client.remove(["c" * 40]))
        self.assertEqual(str(error), "Transmission refused it: torrent not found.")


# -- RemoteDownloads and the qBittorrent on this PC ----------------------------------------------


class Recorder:
    """A client with the actions; the first call fails when `fail_once`."""

    kind = RemoteKind.QBITTORRENT

    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail_once = fail_once

    async def test(self) -> str:
        return "x"

    async def add_magnet(self, magnet: str, *, movie: bool = False) -> None:
        pass

    async def add_torrent(self, content: bytes, filename: str, *, movie: bool = False) -> None:
        pass

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        return RemoteStatus(program="qBittorrent")

    async def free_space(self) -> None:
        return None

    async def _record(self, name: str, task_ids: Collection[str]) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RemoteError("Couldn't reach 127.0.0.1:8085.")
        self.calls.append((name, list(task_ids)))

    async def pause(self, task_ids: Collection[str]) -> None:
        await self._record("pause", task_ids)

    async def resume(self, task_ids: Collection[str]) -> None:
        await self._record("resume", task_ids)

    async def remove(self, task_ids: Collection[str]) -> None:
        await self._record("remove", task_ids)


class Starter:
    def __init__(self) -> None:
        self.started: list[int] = []

    async def start(self, port: int) -> bool:
        self.started.append(port)
        return True


class RemoteDownloadsActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_client_without_actions_says_so(self) -> None:
        settings = SettingsService(mock.Mock(spec=SettingsRepository), Settings())
        remote = RemoteDownloads(settings, clients={kind: OnlyAdds() for kind in RemoteKind})
        with self.assertRaises(RemoteError) as caught:
            await remote.remove(["x"])
        self.assertEqual(
            str(caught.exception), "AirGrabber can't change the downloads of Download Station."
        )

    async def test_the_program_in_settings_does_them(self) -> None:
        settings = SettingsService(
            mock.Mock(spec=SettingsRepository),
            Settings(remote_kind=RemoteKind.QBITTORRENT, remote_url="http://192.168.1.20:8080"),
        )
        recorder = Recorder()
        clients: dict[RemoteKind, Any] = {kind: OnlyAdds() for kind in RemoteKind}
        clients[RemoteKind.QBITTORRENT] = recorder
        remote = RemoteDownloads(settings, clients=clients)
        await remote.pause(["a"])
        await remote.resume(["a"])
        await remote.remove(["a", "b"])
        self.assertEqual(
            recorder.calls, [("pause", ["a"]), ("resume", ["a"]), ("remove", ["a", "b"])]
        )
        self.assertFalse(remote.on_this_pc)
        await settings.update(remote_url="http://127.0.0.1:8085")
        self.assertTrue(remote.on_this_pc)

    async def test_the_qbittorrent_on_this_pc_is_started_for_an_action(self) -> None:
        recorder, starter = Recorder(fail_once=True), Starter()
        local = LocalQBittorrent(recorder, starter, 8085)  # type: ignore[arg-type]
        self.assertIsInstance(local, RemoteTaskActions)
        await local.remove(["a"])
        self.assertEqual((starter.started, recorder.calls), ([8085], [("remove", ["a"])]))
        with self.assertRaises(RemoteError):
            await LocalQBittorrent(OnlyAdds(), starter, 8085).pause(["a"])  # type: ignore[arg-type]


class NoSecretsInLogsTests(QBittorrentActionTests):
    async def test_actions_log_counts_only(self) -> None:
        with self.assertLogs("app", level=logging.DEBUG) as logs:
            logging.getLogger("app").debug("start")
            await self.client.remove(["a" * 40])
        text = "\n".join(logs.output)
        self.assertNotIn("a" * 40, text)
        self.assertNotIn(qbt.SID, text)
        self.assertIn("1 torrent(s)", text)


if __name__ == "__main__":
    unittest.main()
