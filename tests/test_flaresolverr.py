"""The FlareSolverr check (httpx.MockTransport, no network): running, not running, another
program answering, and no answer in time."""

from __future__ import annotations

import unittest
from collections.abc import Callable

import httpx

from app.services.flaresolverr import FLARESOLVERR_URL, FlareSolverrService

Handler = Callable[[httpx.Request], httpx.Response]

READY = {
    "msg": "FlareSolverr is ready!",
    "version": "3.3.21",
    "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


class FlareSolverrCheckTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, handler: Handler) -> tuple[bool, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def server(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
            running = await FlareSolverrService(http).is_running()
        return running, seen

    async def test_running(self) -> None:
        running, seen = await self.check(lambda _request: httpx.Response(200, json=READY))
        self.assertTrue(running)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].method, "GET")
        self.assertEqual(str(seen[0].url), FLARESOLVERR_URL)
        self.assertEqual(str(seen[0].url), "http://localhost:8191/")

    async def test_short_timeout(self) -> None:
        _running, seen = await self.check(lambda _request: httpx.Response(200, json=READY))
        timeout = seen[0].extensions["timeout"]
        self.assertLessEqual(timeout["read"], 5.0)
        self.assertLessEqual(timeout["connect"], 5.0)

    async def test_not_running(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        running, _seen = await self.check(refuse)
        self.assertFalse(running)

    async def test_timeout(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        running, _seen = await self.check(slow)
        self.assertFalse(running)

    async def test_wrong_answer(self) -> None:
        answers = [
            httpx.Response(200, json={"msg": "Hello from another program"}),
            httpx.Response(200, json={"status": "ok"}),
            httpx.Response(200, json=["FlareSolverr"]),
            httpx.Response(200, json={"msg": 42}),
            httpx.Response(200, text="<html>FlareSolverr</html>"),
            httpx.Response(404, json=READY),
            httpx.Response(500, text="error"),
        ]
        for answer in answers:
            with self.subTest(answer=answer):
                running, _seen = await self.check(_always(answer))
                self.assertFalse(running)

    async def test_message_case_does_not_matter(self) -> None:
        running, _seen = await self.check(
            lambda _request: httpx.Response(200, json={"msg": "flaresolverr is ready"})
        )
        self.assertTrue(running)


def _always(answer: httpx.Response) -> Handler:
    return lambda _request: answer


if __name__ == "__main__":
    unittest.main()
