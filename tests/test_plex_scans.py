"""Plex scans the folders MediaForge moved files into: which library folder a Windows folder is
on the server, the scan call itself, and hearing Forge's moves."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

import httpx

from app.forge.models.media import MediaKind
from app.forge.models.moves import MoveRecord
from app.services.plex import plex_folder
from app.services.plex_scans import PlexScansAfterMoves
from tests.test_plex import LOCAL_HTTP, TOKEN, FakePlex, PlexTestCase

SERIES = "\\\\Alex-NAS\\video\\TV"
LOCATIONS = [("1", "/volume1/video/TV"), ("2", "/volume1/video/Movies")]


class PlexFolderTests(unittest.TestCase):
    def test_a_network_folder_is_found_on_the_server(self) -> None:
        folder = SERIES + "\\The Ark (2023)\\Season 02"
        self.assertEqual(
            plex_folder(folder, LOCATIONS), ("1", "/volume1/video/TV/The Ark (2023)/Season 02")
        )
        self.assertEqual(plex_folder(SERIES, LOCATIONS), ("1", "/volume1/video/TV"))

    def test_names_are_compared_without_case(self) -> None:
        found = plex_folder("\\\\Alex-NAS\\VIDEO\\tv\\Show", LOCATIONS)
        self.assertEqual(found, ("1", "/volume1/video/TV/Show"))

    def test_a_server_on_windows(self) -> None:
        found = plex_folder("\\\\Alex-PC\\Media\\TV\\Show", [("4", "D:\\Media\\TV")])
        self.assertEqual(found, ("4", "D:\\Media\\TV\\Show"))

    def test_a_docker_style_folder_with_only_its_last_name(self) -> None:
        found = plex_folder(SERIES + "\\Show", [("1", "/tv"), ("2", "/movies")])
        self.assertEqual(found, ("1", "/tv/Show"))

    def test_unsure_or_unknown_folders_are_left_alone(self) -> None:
        self.assertIsNone(plex_folder(SERIES + "\\Show", [("1", "/a/TV"), ("2", "/b/TV")]))
        self.assertIsNone(plex_folder("\\\\Alex-NAS\\video\\Films\\X", [("1", "/volume1/video/TV")]))


class ScanningPlex(FakePlex):
    def _server(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if len(parts) == 4 and parts[3] == "refresh" and self._authorised(request):
            return httpx.Response(200, content=b"")
        return super()._server(request)


class ScanFoldersTests(PlexTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.fake = ScanningPlex()
        self.http._transport = httpx.MockTransport(self.fake)
        await self.settings.update(plex_server_uri=LOCAL_HTTP)

    def scans(self) -> list[httpx.Request]:
        return [r for r in self.fake.requests if r.url.path.endswith("/refresh")]

    async def test_each_folder_is_scanned_once_with_the_token_in_its_header(self) -> None:
        folders = ["\\\\Alex-NAS\\video\\TV\\Show\\Season 01"] * 2 + ["\\\\Alex-NAS\\other\\X"]
        asked = await self.plex.scan_folders(folders)
        self.assertEqual(asked, 1)
        [scan] = self.scans()
        self.assertEqual(scan.url.path, "/library/sections/1/refresh")
        self.assertEqual(scan.url.params["path"], "/volume1/video/TV/Show/Season 01")
        self.assertEqual(scan.headers["X-Plex-Token"], TOKEN)
        self.assertNotIn(TOKEN, str(scan.url))

    async def test_nothing_without_plex_or_its_address(self) -> None:
        await self.settings.update(plex_server_uri="")
        self.assertEqual(await self.plex.scan_folders(["\\\\Alex-NAS\\video\\TV\\Show"]), 0)
        await self.settings.update(plex_server_uri=LOCAL_HTTP, plex_token="")
        self.assertEqual(await self.plex.scan_folders(["\\\\Alex-NAS\\video\\TV\\Show"]), 0)
        self.assertEqual(self.fake.requests, [])

    async def test_a_server_that_doesnt_answer_is_no_error(self) -> None:
        self.fake.answering = set()
        self.assertEqual(await self.plex.scan_folders(["\\\\Alex-NAS\\video\\TV\\Show"]), 0)


def record(target: str, when: float) -> MoveRecord:
    return MoveRecord(
        source=Path("C:/downloads/x.mkv"),
        target=Path(target),
        moved_at=datetime.fromtimestamp(when),
        kind=MediaKind.EPISODE,
        tmdb_id=None,
        run_id=1,
    )


class FakeMoves:
    def __init__(self) -> None:
        self.records: list[MoveRecord] = []
        self.listeners: list[Callable[[], None]] = []
        self.asked: list[float] = []

    async def moved_after(self, when: float) -> list[MoveRecord]:
        self.asked.append(when)
        return [r for r in self.records if r.moved_at.timestamp() > when]

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def moved(self, *targets: str, when: float) -> None:
        self.records += [record(target, when) for target in targets]
        for listener in list(self.listeners):
            listener()


class FakeScanner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def scan_folders(self, folders: Iterable[str]) -> int:
        self.calls.append(sorted(folders))
        return len(self.calls[-1])


class AfterMovesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.now = 1_800_000_000.0
        self.scanner = FakeScanner()
        self.scans = PlexScansAfterMoves(self.scanner, gather_s=0.01, clock=lambda: self.now)
        self.addAsyncCleanup(self.scans.aclose)
        self.moves = FakeMoves()

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0.01)

    async def test_a_runs_moves_bring_one_scan_of_their_folders(self) -> None:
        self.scans.attach(self.moves)
        self.moves.moved("D:/TV/Show/Season 01/e1.mkv", when=self.now + 1)
        self.moves.moved("D:/TV/Show/Season 01/e2.mkv", "D:/TV/Other/e1.mkv", when=self.now + 2)
        await self.settle()
        self.assertEqual(self.scanner.calls, [[str(Path("D:/TV/Other")), str(Path("D:/TV/Show/Season 01"))]])
        self.moves.moved("D:/TV/Late/e1.mkv", when=self.now + 3)
        await self.settle()
        self.assertEqual(self.scanner.calls[-1], [str(Path("D:/TV/Late"))])  # only what's new

    async def test_what_was_moved_before_is_left_to_plex(self) -> None:
        self.moves.records.append(record("D:/TV/Old/e1.mkv", self.now - 100))
        self.scans.attach(self.moves)
        self.moves.listeners[0]()
        await self.settle()
        self.assertEqual(self.scanner.calls, [])

    async def test_closing_stops_listening(self) -> None:
        self.scans.attach(self.moves)
        await self.scans.aclose()
        self.assertEqual(self.moves.listeners, [])

    async def test_a_failing_scan_is_logged_not_raised(self) -> None:
        class Broken:
            async def scan_folders(self, folders: Iterable[str]) -> int:
                raise OSError("NAS")

        scans = PlexScansAfterMoves(Broken(), gather_s=0.01, clock=lambda: self.now)
        self.addAsyncCleanup(scans.aclose)
        scans.attach(self.moves)
        with self.assertLogs("app.services.plex_scans", "WARNING"):
            self.moves.moved("D:/TV/Show/e1.mkv", when=self.now + 1)
            await self.settle()
