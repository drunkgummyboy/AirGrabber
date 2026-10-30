"""airdates.tv import: export parsing, AirdatesClient on httpx.MockTransport (recorded answers in
tests/fixtures/airdates/), LibraryService.find_matches and AirdatesImportService.find."""

from __future__ import annotations

import asyncio
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from app.models import AirdatesPreview, ImportMatch
from app.services.airdates import (
    EXPORT_ERROR,
    MIN_INTERVAL,
    RETRY_DELAY,
    TIMEOUT,
    AirdatesClient,
    AirdatesError,
    AirdatesImportService,
    parse_export,
    parse_show_name,
)
from app.services.library import (
    NameHint,
    initialism_variants,
    name_hint,
    search_name,
    search_queries,
)
from tests.test_library import DRAGON, DUNE, LibraryTestCase, load, show_payload

FIXTURES = Path(__file__).parent / "fixtures" / "airdates"
LOGGER = "app.services"

DARK_MATTER = 6174  # "Dark Matter (2024) (all episodes)"
SHAKESPEARE = 3422  # "Shakespeare &amp; Hathaway: Private Investigators (all episodes)"
TRAITORS = 6193  # "The Traitors (US) (all episodes)"
UNKNOWN = 999999  # airdates.tv answers HTTP 500 "Application error"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def search_hit(
    show_id: int, name: str, *, premiered: str | None = None, country: str | None = None
) -> dict[str, Any]:
    show: dict[str, Any] = {"id": show_id, "name": name, "premiered": premiered}
    if country is not None:
        show["network"] = {"id": show_id, "name": f"Net {country}", "country": {"code": country}}
    return {"score": 1.0, "show": show}


class FakeClock:
    """A monotonic clock that only moves when the fake sleep is awaited."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeAirdates:
    """Serves `/s?q=info:<id>` from the recorded fixtures; unknown ids answer like the site (500).

    `errors[id]`: how many times that id first fails with a connection error (-1 = always).
    `statuses[id]`: a status to answer instead.
    """

    PAGES = {
        DARK_MATTER: "info_6174.html",
        SHAKESPEARE: "info_3422.html",
        TRAITORS: "info_6193.html",
    }

    def __init__(self) -> None:
        self.requests: Counter[int] = Counter()
        self.errors: dict[int, int] = {}
        self.statuses: dict[int, int] = {}
        self.timeouts: list[Any] = []
        self.user_agents: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.airdates.tv" and request.url.path == "/s"
        query = request.url.params["q"]
        assert query.startswith("info:")
        series_id = int(query.removeprefix("info:"))
        self.requests[series_id] += 1
        self.timeouts.append(request.extensions.get("timeout"))
        self.user_agents.append(request.headers.get("User-Agent", ""))
        remaining = self.errors.get(series_id, 0)
        if remaining:
            self.errors[series_id] = remaining - 1 if remaining > 0 else -1
            raise httpx.ConnectError("boom (secret detail)", request=request)
        if (status := self.statuses.get(series_id)) is not None:
            return httpx.Response(status, text="")
        page = self.PAGES.get(series_id)
        if page is None:
            return httpx.Response(500, text=fixture("unknown_999999.html"))
        return httpx.Response(200, text=fixture(page))


class ParseExportTests(unittest.TestCase):
    def test_color_selection(self) -> None:
        self.assertEqual(parse_export("1534=#ffcc00;5064=7fa2c4;2067=#e06c75"), [1534, 5064, 2067])

    def test_spaces_newlines_and_trailing_separator(self) -> None:
        text = " 1534 = #ffcc00 ;\n5064=7fa2c4;\r\n 2067=#E06C75 ; \n"
        self.assertEqual(parse_export(text), [1534, 5064, 2067])

    def test_duplicates_keep_first_position(self) -> None:
        self.assertEqual(parse_export("5=#fff;3=#000;5=#123456;3=abc"), [5, 3])

    def test_bare_ids(self) -> None:
        self.assertEqual(parse_export("1534;5064"), [1534, 5064])
        self.assertEqual(parse_export("1534, 5064,2067"), [1534, 5064, 2067])
        self.assertEqual(parse_export("1534\n5064  2067\t1534"), [1534, 5064, 2067])

    def test_junk_next_to_pairs_is_ignored(self) -> None:
        self.assertEqual(parse_export("colors: 1534=#ffcc00; 5064=7fa2c4;"), [1534, 5064])

    def test_unusable_text_raises(self) -> None:
        for text in ("", "   ;; ", "Futurama; Severance", "season 2", "hello=world", "0", "0=#fff"):
            with self.subTest(text=text), self.assertRaises(AirdatesError) as caught:
                parse_export(text)
            self.assertEqual(str(caught.exception), EXPORT_ERROR)


class ParseShowNameTests(unittest.TestCase):
    def test_recorded_answers(self) -> None:
        self.assertEqual(parse_show_name(fixture("info_6174.html")), "Dark Matter (2024)")
        self.assertEqual(
            parse_show_name(fixture("info_3422.html")),
            "Shakespeare & Hathaway: Private Investigators",
        )
        self.assertEqual(parse_show_name(fixture("info_6193.html")), "The Traitors (US)")

    def test_brief_layout_with_list_item(self) -> None:
        fragment = '<ul class="x"><li data-series-id="1534"> <b>Futurama  (All Episodes)</b></li>'
        self.assertEqual(parse_show_name(fragment), "Futurama")

    def test_no_name(self) -> None:
        self.assertIsNone(parse_show_name(fixture("unknown_999999.html")))
        self.assertIsNone(parse_show_name(""))
        self.assertIsNone(parse_show_name("<b> (all episodes) </b>"))


class AirdatesClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeAirdates()
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.server), headers={"User-Agent": "AirGrabber/test"}
        )
        self.clock = FakeClock()
        self.client = AirdatesClient(self.http, sleep=self.clock.sleep, clock=self.clock)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()

    async def test_found(self) -> None:
        self.assertEqual(await self.client.show_name(DARK_MATTER), "Dark Matter (2024)")
        self.assertEqual(self.server.user_agents, ["AirGrabber/test"])
        timeout = self.server.timeouts[0]
        self.assertEqual(
            (timeout["connect"], timeout["read"], timeout["write"], timeout["pool"]), (TIMEOUT,) * 4
        )

    async def test_unknown_id_is_none_without_retry(self) -> None:
        self.assertIsNone(await self.client.show_name(UNKNOWN))
        self.server.statuses[DARK_MATTER] = 404
        self.assertIsNone(await self.client.show_name(DARK_MATTER))
        self.assertEqual(self.server.requests, Counter({UNKNOWN: 1, DARK_MATTER: 1}))

    async def test_network_error_is_retried_once(self) -> None:
        self.server.errors[SHAKESPEARE] = 1
        with self.assertLogs(LOGGER, "WARNING") as logs:
            name = await self.client.show_name(SHAKESPEARE)
        self.assertEqual(name, "Shakespeare & Hathaway: Private Investigators")
        self.assertEqual(self.server.requests[SHAKESPEARE], 2)
        self.assertIn(RETRY_DELAY, self.clock.sleeps)
        self.assertNotIn("airdates.tv/s", "\n".join(logs.output))

    async def test_second_failure_raises_without_details(self) -> None:
        self.server.errors[SHAKESPEARE] = -1
        with self.assertLogs(LOGGER, "WARNING") as logs, self.assertRaises(AirdatesError) as caught:
            await self.client.show_name(SHAKESPEARE)
        self.assertEqual(self.server.requests[SHAKESPEARE], 2)
        message = str(caught.exception)
        self.assertIn("ConnectError", message)
        for text in (message, "\n".join(logs.output)):
            self.assertNotIn("secret detail", text)
            self.assertNotIn("info:", text)

    async def test_server_error_is_retried_then_raises(self) -> None:
        self.server.statuses[DARK_MATTER] = 503
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaisesRegex(AirdatesError, "503"):
            await self.client.show_name(DARK_MATTER)
        self.assertEqual(self.server.requests[DARK_MATTER], 2)

    async def test_client_error_is_not_retried(self) -> None:
        self.server.statuses[DARK_MATTER] = 403
        with self.assertRaisesRegex(AirdatesError, "403"):
            await self.client.show_name(DARK_MATTER)
        self.assertEqual(self.server.requests[DARK_MATTER], 1)

    async def test_requests_are_spaced(self) -> None:
        for series_id in (DARK_MATTER, UNKNOWN, TRAITORS):
            await self.client.show_name(series_id)
        self.assertEqual([round(s, 6) for s in self.clock.sleeps], [MIN_INTERVAL] * 2)

    async def test_no_wait_when_enough_time_passed(self) -> None:
        await self.client.show_name(DARK_MATTER)
        self.clock.now += 5
        await self.client.show_name(TRAITORS)
        self.assertEqual(self.clock.sleeps, [])

    async def test_concurrent_calls_take_turns(self) -> None:
        in_flight = 0
        peak = 0
        real = self.server.__call__

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return real(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            client = AirdatesClient(http, sleep=self.clock.sleep, clock=self.clock)
            names = await asyncio.gather(*(client.show_name(i) for i in (DARK_MATTER, TRAITORS)))
        finally:
            await http.aclose()
        self.assertEqual(names, ["Dark Matter (2024)", "The Traitors (US)"])
        self.assertEqual(peak, 1)
        self.assertEqual([round(s, 6) for s in self.clock.sleeps], [MIN_INTERVAL])


class FindMatchesTests(LibraryTestCase):
    def serve_search(self, query: str, *hits: dict[str, Any]) -> None:
        self.server.searches[query.lower()] = list(hits)

    async def test_exact_name(self) -> None:
        progress: list[tuple[int, int]] = []
        matches = await self.service.find_matches(
            ["Severance", " ", "Severance "], lambda d, t: progress.append((d, t))
        )
        self.assertEqual(
            matches,
            (
                ImportMatch(
                    source_name="Severance",
                    show_id=44933,
                    show_name="Severance",
                    year=2022,
                    network="Apple TV",
                ),
            ),
        )
        self.assertEqual(progress, [(1, 1)])
        self.assertEqual(self.notifications, 0)
        self.assertEqual(await self.service.tracked(), [])  # nothing tracked

    async def test_year_suffix_prefers_that_year(self) -> None:
        self.serve_search(
            "Dark Matter",
            search_hit(1, "Dark Matter", premiered="2015-06-12"),
            search_hit(2, "Dark Matter", premiered="2024-05-08"),
        )
        (match,) = await self.service.find_matches(["Dark Matter (2024)"])
        self.assertEqual(
            (match.source_name, match.show_id, match.year), ("Dark Matter (2024)", 2, 2024)
        )

    async def test_country_suffix_prefers_that_country(self) -> None:
        self.serve_search(
            "The Traitors",
            search_hit(10, "The Traitors", premiered="2022-11-29", country="GB"),
            search_hit(11, "The Traitors", premiered="2023-01-12", country="US"),
        )
        self.serve_search("The Office", search_hit(20, "The Office", country="US"))
        self.server.searches["the office"].append(search_hit(21, "The Office", country="GB"))
        matches = await self.service.find_matches(["The Traitors (US)", "The Office (UK)"])
        self.assertEqual([m.show_id for m in matches], [11, 21])
        self.assertEqual(matches[0].network, "Net US")

    async def test_suffix_without_a_matching_hit_takes_the_best_hit(self) -> None:
        self.serve_search("Ghosts", search_hit(30, "Ghosts", premiered="2019-04-15"))
        (match,) = await self.service.find_matches(["Ghosts (2021)"])
        self.assertEqual(match.show_id, 30)

    async def test_suffix_that_is_part_of_the_name(self) -> None:
        # Nothing for the bare name: the full name is searched as well.
        self.serve_search("Blink (1999)", search_hit(40, "Blink (1999)"))
        (match,) = await self.service.find_matches(["Blink (1999)"])
        self.assertEqual(match.show_id, 40)

    async def test_unicode_minus_and_quotes_are_searched_as_ascii(self) -> None:
        # airdates.tv writes this title with a minus sign (U+2212); TVMaze knows it with "-".
        source = "Re:Zero − Starting Life in Another World"
        self.serve_search(
            "Re:ZERO - Starting Life in Another World",
            search_hit(50, "Re:ZERO -Starting Life in Another World-", premiered="2016-04-04"),
        )
        self.serve_search("Grey's Anatomy", search_hit(51, "Grey's Anatomy"))
        matches = await self.service.find_matches(
            [source, "Grey’s  Anatomy", "“Grey’s Anatomy”"]
        )
        self.assertEqual([m.show_id for m in matches], [50, 51, None])
        self.assertEqual(matches[0].source_name, source)  # shown as airdates.tv wrote it
        self.assertEqual(matches[1].source_name, "Grey’s  Anatomy")

    async def test_the_names_the_vm_test_couldnt_find(self) -> None:
        """TVMaze's own answers (tests/fixtures/tvmaze/search_name_variants.json): nothing for
        the names as airdates.tv writes them, the show for the name without its bare country /
        year, or with the initialism dotted."""
        self.server.searches.update(load("search_name_variants.json")["searches"])
        names = ["Shameless US", "Agents of S H I E L D", "MacGyver 2016", "The Agency 2024"]
        matches = await self.service.find_matches(names)
        self.assertEqual(
            [(m.source_name, m.show_id, m.show_name, m.year) for m in matches],
            [
                ("Shameless US", 150, "Shameless", 2011),
                ("Agents of S H I E L D", 31, "Marvel's Agents of S.H.I.E.L.D.", 2013),
                ("MacGyver 2016", 5511, "MacGyver", 2016),
                ("The Agency 2024", 66840, "The Agency", 2024),
            ],
        )
        # Each: the name as written (nothing), then one more search that finds it.
        self.assertEqual(self.server.requests["/search/shows"], 8)

    async def test_a_bare_suffix_prefers_that_year_or_country(self) -> None:
        self.server.searches.update(load("search_name_variants.json")["searches"])
        matches = await self.service.find_matches(["Shameless UK", "MacGyver 1985", "Shameless AU"])
        # The UK one; the 1985 one; no Australian one: TVMaze's best hit for the name.
        self.assertEqual([m.show_id for m in matches], [746, 821, 150])

    async def test_a_year_that_is_part_of_the_name(self) -> None:
        self.serve_search("Space: 1999", search_hit(60, "Space: 1999", premiered="1975-09-04"))
        (match,) = await self.service.find_matches(["Space: 1999"])
        self.assertEqual(match.show_id, 60)
        self.assertEqual(self.server.requests["/search/shows"], 1)  # its own name: no more

    async def test_dotted_letters_are_also_tried_joined(self) -> None:
        self.serve_search("SWAT", search_hit(70, "S.W.A.T.", premiered="2017-11-02"))
        (match,) = await self.service.find_matches(["S.W.A.T."])
        self.assertEqual(match.show_id, 70)

    def test_name_hints(self) -> None:
        self.assertEqual(name_hint("Ghosts (2021)"), NameHint("Ghosts", 2021, None, True))
        self.assertEqual(name_hint("The Office (UK)"), NameHint("The Office", None, "GB", True))
        self.assertEqual(name_hint("MacGyver 2016"), NameHint("MacGyver", 2016, None, False))
        self.assertEqual(name_hint("Shameless US"), NameHint("Shameless", None, "US", False))
        self.assertEqual(name_hint("Utopia AU"), NameHint("Utopia", None, "AU", False))
        self.assertIsNone(name_hint("House MD"))  # not a country written bare
        self.assertIsNone(name_hint("1923"))  # the year is the whole name
        self.assertIsNone(name_hint("Archive 81"))
        self.assertIsNone(name_hint("This Is Us"))
        self.assertIsNone(name_hint("Countdown 1234"))  # no plausible year

    def test_initialism_variants(self) -> None:
        self.assertEqual(
            initialism_variants("Agents of S H I E L D"),
            ["Agents of S.H.I.E.L.D.", "Agents of SHIELD"],
        )
        self.assertEqual(
            initialism_variants("Agents of S. H. I. E. L. D."),
            ["Agents of S.H.I.E.L.D.", "Agents of SHIELD"],
        )
        self.assertEqual(initialism_variants("Agents of S.H.I.E.L.D."), ["Agents of SHIELD"])
        self.assertEqual(initialism_variants("A.P. Bio"), ["AP Bio"])
        for name in ("I Love Lucy", "Malcolm X", "Mr. Robot", "Severance", "Y: The Last Man"):
            self.assertEqual(initialism_variants(name), [], name)

    def test_search_queries(self) -> None:
        self.assertEqual(search_queries("Severance", None), ["Severance"])
        self.assertEqual(
            search_queries("Ghosts (2021)", name_hint("Ghosts (2021)")), ["Ghosts", "Ghosts (2021)"]
        )
        self.assertEqual(
            search_queries("S W A T 2017", name_hint("S W A T 2017")),
            ["S W A T 2017", "S W A T", "S.W.A.T.", "SWAT", "S.W.A.T. 2017", "SWAT 2017"],
        )

    def test_search_name(self) -> None:
        self.assertEqual(
            search_name(" Re:Zero − Starting	Life – in — Another‐World "),
            "Re:Zero - Starting Life - in - Another-World",
        )
        self.assertEqual(search_name("“It’s” ‘Always’"), "\"It's\" 'Always'")
        self.assertEqual(search_name("ＡＢ C"), "AB C")  # NFKC: full-width, nbsp

    async def test_not_found(self) -> None:
        (match,) = await self.service.find_matches(["Nothing Matches"])
        self.assertEqual(match, ImportMatch(source_name="Nothing Matches"))
        self.assertFalse(match.found)

    async def test_already_tracked(self) -> None:
        await self.store_tracked(load("web_show.json"))  # House of the Dragon
        dragon = load("web_show.json")
        self.serve_search("House of the Dragon", {"score": 1.0, "show": dragon})
        (match,) = await self.service.find_matches(["House of the Dragon"])
        self.assertEqual(match.show_id, DRAGON)
        self.assertTrue(match.already_tracked)

    async def test_failed_lookup(self) -> None:
        self.server.failures["/search/shows"] = 500
        with self.assertLogs(LOGGER, "WARNING"):
            (match,) = await self.service.find_matches(["Severance"])
        self.assertEqual(match, ImportMatch(source_name="Severance", failed=True))

    async def test_import_shows_by_tvmaze_ids(self) -> None:
        """The preview's ticked matches are imported by id through import_shows."""
        await self.store_tracked(load("web_show.json"))
        progress: list[tuple[int, int]] = []
        result = await self.service.import_shows(
            [str(DUNE), str(DRAGON)], lambda d, t: progress.append((d, t))
        )
        self.assertEqual(result.added, ("Dune: Prophecy",))
        self.assertEqual(result.already_tracked, ("House of the Dragon",))
        self.assertEqual(progress, [(1, 2), (2, 2)])
        self.assertEqual(self.server.requests["/search/shows"], 0)
        self.assertEqual({e.show.id for e in await self.service.tracked()}, {DUNE, DRAGON})

    async def test_import_shows_uses_the_suffix_rule_too(self) -> None:
        self.serve_search(
            "Dark Matter",
            search_hit(1, "Dark Matter", premiered="2015-06-12"),
            search_hit(2, "Dark Matter", premiered="2024-05-08"),
        )
        self.server.shows[2] = show_payload(2, "Dark Matter")
        result = await self.service.import_shows(["Dark Matter (2024)"])
        self.assertEqual(result.added, ("Dark Matter",))
        self.assertEqual(self.server.requests["/shows/1"], 0)


class AirdatesImportServiceTests(LibraryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.airdates = FakeAirdates()
        self.airdates_http = httpx.AsyncClient(transport=httpx.MockTransport(self.airdates))
        self.clock = FakeClock()
        client = AirdatesClient(self.airdates_http, sleep=self.clock.sleep, clock=self.clock)
        self.importer = AirdatesImportService(client, self.service)
        self.server.searches["dark matter"] = [
            search_hit(1, "Dark Matter", premiered="2015-06-12"),
            search_hit(2, "Dark Matter", premiered="2024-05-08", country="US"),
        ]
        self.server.searches["shakespeare & hathaway: private investigators"] = [
            search_hit(3, "Shakespeare & Hathaway: Private Investigators", premiered="2018-02-26")
        ]

    async def asyncTearDown(self) -> None:
        await self.airdates_http.aclose()
        await super().asyncTearDown()

    async def find(self, text: str) -> tuple[AirdatesPreview, list[tuple[int, int]], list[str]]:
        progress: list[tuple[int, int]] = []
        with self.assertLogs(LOGGER, "DEBUG") as logs:
            preview = await self.importer.find(text, lambda d, t: progress.append((d, t)))
        return preview, progress, logs.output

    async def test_end_to_end(self) -> None:
        self.airdates.errors[777] = -1  # airdates.tv never answers for this one
        text = (
            f"{DARK_MATTER}=#ffcc00; {UNKNOWN}=7fa2c4;\n"
            f"{SHAKESPEARE}=e06c75;777=#123;{DARK_MATTER}=#000;"
        )
        preview, progress, output = await self.find(text)

        self.assertEqual(
            [(m.source_name, m.show_id, m.year) for m in preview.matches],
            [
                ("Dark Matter (2024)", 2, 2024),
                ("Shakespeare & Hathaway: Private Investigators", 3, 2018),
            ],
        )
        self.assertEqual(preview.unknown_ids, (UNKNOWN,))
        self.assertEqual(preview.failed, 1)
        # 4 ids: 4 lookups, then 2 names to match (the 2 others count as done at once).
        self.assertEqual(progress, [(1, 8), (2, 8), (3, 8), (4, 8), (6, 8), (7, 8), (8, 8)])
        self.assertEqual(await self.service.tracked(), [])
        self.assertEqual(self.notifications, 0)
        logged = "\n".join(output)
        for secret in ("airdates.tv/s", "info:", "#ffcc00", "7fa2c4"):
            self.assertNotIn(secret, logged)
        self.assertTrue(any("4 ids" in line for line in output))

        # Importing the ticked matches goes through import_shows with their TVMaze ids.
        self.server.shows[2] = show_payload(2, "Dark Matter")
        result = await self.service.import_shows([str(m.show_id) for m in preview.matches[:1]])
        self.assertEqual(result.added, ("Dark Matter",))

    async def test_every_id_named_and_found(self) -> None:
        preview, progress, _ = await self.find(f"{DARK_MATTER};{SHAKESPEARE}")
        self.assertEqual(len(preview.matches), 2)
        self.assertEqual((preview.unknown_ids, preview.failed), ((), 0))
        self.assertEqual(progress, [(1, 4), (2, 4), (3, 4), (4, 4)])

    async def test_tvmaze_failures_are_counted_not_listed(self) -> None:
        self.server.failures["/search/shows"] = 500
        preview, progress, _ = await self.find(f"{DARK_MATTER}=#fff;{UNKNOWN}=#000")
        self.assertEqual(preview.matches, ())
        self.assertEqual(preview.unknown_ids, (UNKNOWN,))
        self.assertEqual(preview.failed, 1)
        self.assertEqual(progress[-1], (4, 4))

    async def test_bad_export_raises_before_any_request(self) -> None:
        with self.assertRaises(AirdatesError):
            await self.importer.find("not an export")
        self.assertEqual(sum(self.airdates.requests.values()), 0)


if __name__ == "__main__":
    unittest.main()
