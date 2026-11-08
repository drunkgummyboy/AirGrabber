"""Import Shows → From Sonarr: Sonarr's series read with its API key (MockTransport only)."""

from __future__ import annotations

import unittest
from unittest import mock

import httpx

from app.services.sonarr import SonarrClient, SonarrError, SonarrSeries, import_lines, series_url
from app.ui.import_dialog import ImportShowsDialog
from tests.test_backup_dialog import DialogTestCase

KEY = "0123456789abcdef0123456789abcdef"
SERIES = [
    {"title": "Severance", "tvdbId": 371980, "imdbId": "tt11280740", "monitored": True},
    {"title": "Some  Local Show", "tvdbId": 0},
    {"title": "Lost", "tvdbId": 73739},
]


class SonarrTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.payload: object = SERIES

        def answer(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.headers.get("X-Api-Key") != KEY:
                return httpx.Response(401)
            return httpx.Response(self.status, json=self.payload)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        self.addAsyncCleanup(self.http.aclose)
        self.sonarr = SonarrClient(self.http)

    async def test_the_series_as_import_lines(self) -> None:
        with self.assertLogs("app.services.sonarr", "INFO") as logs:
            lines = await self.sonarr.import_lines("192.168.1.20:8989/", KEY)
        self.assertEqual(lines, ["tvdb:371980 Severance", "Some Local Show", "tvdb:73739 Lost"])
        [request] = self.requests
        self.assertEqual(str(request.url), "http://192.168.1.20:8989/api/v3/series")
        self.assertNotIn(KEY, str(request.url))  # the key travels in the header only
        text = "\n".join(logs.output)
        self.assertNotIn(KEY, text)
        self.assertNotIn("192.168.1.20", text)

    async def test_problems_in_plain_words(self) -> None:
        with self.assertRaisesRegex(SonarrError, "didn't accept"):
            await self.sonarr.series("http://localhost:8989", "wrong")
        with self.assertRaisesRegex(SonarrError, "API key"):
            await self.sonarr.series("http://localhost:8989", "  ")
        with self.assertRaisesRegex(SonarrError, "address"):
            await self.sonarr.series("", KEY)
        self.payload = {"not": "a list"}
        with self.assertRaisesRegex(SonarrError, "like Sonarr"):
            await self.sonarr.series("http://localhost:8989", KEY)
        self.status = 500
        with self.assertRaisesRegex(SonarrError, "like Sonarr"):
            await self.sonarr.series("http://localhost:8989", KEY)

    async def test_unreachable(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
            with self.assertRaises(SonarrError) as caught:
                await SonarrClient(http).series("http://localhost:8989", KEY)
        self.assertEqual(str(caught.exception), "Couldn't reach Sonarr at that address.")


class TabTests(DialogTestCase):
    async def test_the_tab_reads_sonarr_and_hands_the_lines_on(self) -> None:
        imported: list[list[str]] = []
        asked: list[tuple[str, str]] = []

        async def sonarr(address: str, key: str) -> list[str]:
            asked.append((address, key))
            if key != KEY:
                raise SonarrError("Sonarr didn't accept that API key.")
            return ["tvdb:73739 Lost"]

        dialog = ImportShowsDialog(
            self.root,
            on_import=imported.append,
            airdates=mock.Mock(),
            library=mock.Mock(),
            profile_id=1,
            sonarr=sonarr,
        )
        self.keep(dialog)
        dialog.sonarr_key.insert(0, "wrong")
        dialog.import_sonarr()
        await self.settle()
        self.assertEqual(dialog.sonarr_hint.cget("text"), "Sonarr didn't accept that API key.")
        self.assertEqual(dialog.sonarr_button.cget("state"), "normal")
        dialog.sonarr_key.delete(0, "end")
        dialog.sonarr_key.insert(0, KEY)
        dialog.import_sonarr()
        await self.settle()
        self.assertEqual(asked[-1], ("http://localhost:8989", KEY))
        self.assertEqual(imported, [["tvdb:73739 Lost"]])
        self.assertFalse(dialog.winfo_exists())

    async def test_no_tab_without_sonarr(self) -> None:
        dialog = ImportShowsDialog(
            self.root, on_import=lambda lines: None, airdates=mock.Mock(), library=mock.Mock(), profile_id=1
        )
        self.keep(dialog)
        self.assertFalse(hasattr(dialog, "sonarr_button"))


class PureTests(unittest.TestCase):
    def test_the_address(self) -> None:
        self.assertEqual(series_url("https://nas.local/sonarr"), "https://nas.local/sonarr/api/v3/series")
        with self.assertRaises(SonarrError):
            series_url("ftp://nas")

    def test_lines(self) -> None:
        self.assertEqual(import_lines([SonarrSeries(title="", tvdbId=0)]), [])


if __name__ == "__main__":
    unittest.main()
