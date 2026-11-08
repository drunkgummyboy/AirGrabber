"""Tonight's shortlist on the PC: real settings in a real database, so that what two phones do at
once and what a restart keeps are the real thing."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest import mock

from app.database import Database
from app.i18n import t
from app.models import Settings
from app.services.backup import settings_for_backup
from app.services.phone.server import PhoneServer, Reply, Request
from app.services.phone.shortlist import MAX_ITEMS, Shortlist
from app.services.phone.suggestions import Suggestions
from app.services.settings import SettingsService
from tests.test_phone_coming_up import NOW, PhoneCase

DUNE = {
    "kind": "movie",
    "title": "Dune",
    "year": 2021,
    "poster_url": "https://image.tmdb.org/t/p/w342/dune.jpg",
    "plex_key": None,
    "suggestion_id": 438631,
}
SEVERANCE = {
    "kind": "series",
    "title": "Severance",
    "year": 2022,
    "poster_url": "/library/metadata/77/thumb/1700000000",
    "plex_key": "/library/metadata/77",
    "suggestion_id": None,
}


class ShortlistCase(PhoneCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.suggestions = mock.Mock(spec=Suggestions)
        self.suggestions.offered = mock.AsyncMock(return_value=True)
        self.service = self.new_service(self.settings)

    def new_service(self, settings: SettingsService) -> Shortlist:
        return Shortlist(settings, self.profiles, self.suggestions, now=lambda: NOW)

    async def ask(
        self, method: str, key: str | None = None, body: dict[str, Any] | None = None
    ) -> Reply:
        pattern = "/v1/shortlist" if key is None else "/v1/shortlist/{key}"
        query = {} if key is None else {"key": key}
        request = Request(method, "/v1/shortlist", query, body or {}, self.phone)
        reply = await self.service.routes()[(method, pattern)](request)
        json.dumps(reply.body)  # what the server sends
        return reply

    async def keys(self) -> list[str]:
        return [item["key"] for item in (await self.ask("GET")).body["items"]]


class ChangeTests(ShortlistCase):
    async def test_empty_at_first(self) -> None:
        reply = await self.ask("GET")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"version": 0, "items": []})

    async def test_an_item_has_every_field_the_app_reads(self) -> None:
        reply = await self.ask("PUT", "movie-438631", DUNE)
        self.assertEqual(reply.status, 200, reply.body)
        item = reply.body["items"][0]
        added_at = item.pop("added_at")
        self.assertEqual(item, {"key": "movie-438631", **DUNE, "added_by": "Pixel"})
        self.assertTrue(added_at.startswith("2026-11-07T12:00:00"), added_at)

    async def test_who_added_it_is_the_persons_name_once_they_have_one(self) -> None:
        sam = await self.profiles.add("Sam", self.profiles.current.colour)
        self.phone = self.phone_of(sam.id)
        reply = await self.ask("PUT", "movie-438631", {**DUNE, "added_by": "someone else"})
        self.assertEqual(reply.body["items"][0]["added_by"], "Sam")

    async def test_same_item_added_twice_is_one_row(self) -> None:
        first = await self.ask("PUT", "movie-438631", DUNE)
        again = await self.ask("PUT", "movie-438631", {**DUNE, "title": "Dune, a tap later"})
        self.assertEqual(again.body, first.body)
        self.assertEqual(await self.keys(), ["movie-438631"])

    async def test_a_plex_address_stays_but_nothing_that_could_hold_a_token(self) -> None:
        reply = await self.ask("PUT", "show-77", SEVERANCE)
        self.assertEqual(reply.body["items"][0]["poster_url"], SEVERANCE["poster_url"])
        for poster in (
            "/library/metadata/77/thumb/1?X-Plex-Token=SECRET",
            "http://nas:32400/thumb?X-Plex-Token=SECRET",
            "https://user:SECRET@nas/thumb",
            "//nas/thumb",
            "file:///C:/thumb.jpg",
        ):
            with self.subTest(poster):
                reply = await self.ask("PUT", poster, {**DUNE, "poster_url": poster})
                row = next(item for item in reply.body["items"] if item["key"] == poster)
                self.assertIsNone(row["poster_url"])

    async def test_taking_one_off_leaves_the_rest(self) -> None:
        await self.ask("PUT", "movie-438631", DUNE)
        await self.ask("PUT", "show-77", SEVERANCE)
        reply = await self.ask("DELETE", "movie-438631")
        self.assertEqual(reply.status, 200)
        self.assertEqual([item["key"] for item in reply.body["items"]], ["show-77"])

    async def test_taking_off_what_isnt_there_changes_nothing(self) -> None:
        await self.ask("PUT", "movie-438631", DUNE)
        before = await self.ask("GET")
        reply = await self.ask("DELETE", "gone")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, before.body)

    async def test_clearing_empties_it(self) -> None:
        await self.ask("PUT", "movie-438631", DUNE)
        await self.ask("PUT", "show-77", SEVERANCE)
        reply = await self.ask("DELETE")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["items"], [])

    async def test_the_version_moves_only_when_something_changed(self) -> None:
        seen = [(await self.ask("GET")).body["version"]]
        for method, key, body in (
            ("PUT", "movie-438631", DUNE),
            ("PUT", "movie-438631", DUNE),
            ("DELETE", "gone", None),
            ("PUT", "show-77", SEVERANCE),
            ("DELETE", "show-77", None),
            ("DELETE", None, None),
            ("DELETE", None, None),
        ):
            seen.append((await self.ask(method, key, body)).body["version"])
        self.assertEqual(seen, [0, 1, 1, 1, 2, 3, 4, 4])

    async def test_two_changes_at_once_lose_neither(self) -> None:
        await self.ask("PUT", "first", DUNE)
        await asyncio.gather(
            self.ask("PUT", "movie-438631", DUNE),
            self.ask("PUT", "show-77", SEVERANCE),
            self.ask("DELETE", "first"),
            self.ask("PUT", "movie-438631", DUNE),
        )
        self.assertEqual(await self.keys(), ["movie-438631", "show-77"])
        self.assertEqual((await self.ask("GET")).body["version"], 4)

    async def test_it_has_an_end_and_says_what_to_do(self) -> None:
        for number in range(MAX_ITEMS):
            await self.ask("PUT", f"key-{number}", DUNE)
        reply = await self.ask("PUT", "one-more", DUNE)
        self.assertEqual(reply.status, 400)
        self.assertEqual(reply.body["message"], t("The shortlist is full, so clear it first."))
        self.assertEqual(len(await self.keys()), MAX_ITEMS)
        self.assertEqual((await self.ask("PUT", "key-0", DUNE)).status, 200)

    async def test_what_cant_be_read_is_a_bad_request(self) -> None:
        for change in (
            {"kind": "song"},
            {"kind": None},
            {"title": ""},
            {"title": "   "},
            {"title": 12},
            {"title": "x" * 501},
            {"year": "2021"},
            {"year": True},
            {"year": 12},
            {"poster_url": 12},
            {"plex_key": 12},
            {"plex_key": "x" * 501},
            {"suggestion_id": "1"},
            {"suggestion_id": True},
        ):
            with self.subTest(change):
                reply = await self.ask("PUT", "key", {**DUNE, **change})
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.body["error"], "bad_request")
        reply = await self.ask("PUT", "x" * 501, DUNE)
        self.assertEqual(reply.status, 400)
        self.assertEqual(await self.keys(), [])

    async def test_a_deleted_person_gets_a_plain_answer_everywhere(self) -> None:
        self.phone = self.phone_of(12345)
        for method, key, body in (
            ("GET", None, None),
            ("PUT", "movie-438631", DUNE),
            ("DELETE", "movie-438631", None),
            ("DELETE", None, None),
        ):
            with self.subTest(method=method, key=key):
                reply = await self.ask(method, key, body)
                self.assertEqual(reply.status, 401)
                self.assertEqual(reply.body["error"], "not_paired")

    async def test_a_viewers_phone_may_use_it(self) -> None:
        self.phone = self.phone.model_copy(update={"manager": False})
        self.assertEqual((await self.ask("PUT", "movie-438631", DUNE)).status, 200)
        self.assertEqual((await self.ask("GET")).status, 200)
        self.assertEqual((await self.ask("DELETE")).status, 200)

    def test_the_server_tells_the_routes_apart(self) -> None:
        server = PhoneServer(self.service.routes(), mock.Mock(), mock.Mock(), mock.Mock())
        found = server._find_route("DELETE", "/v1/shortlist/movie%2F1%20x")
        assert found is not None
        self.assertEqual((found[0], found[2]), ("/v1/shortlist/{key}", {"key": "movie/1 x"}))
        found = server._find_route("DELETE", "/v1/shortlist")
        assert found is not None
        self.assertEqual(found[0], "/v1/shortlist")

    async def test_a_suggestion_id_stays_only_if_it_was_offered(self) -> None:
        self.suggestions.offered.return_value = False
        reply = await self.ask("PUT", "movie-438631", DUNE)
        self.assertEqual(reply.status, 200)
        self.assertIsNone(reply.body["items"][0]["suggestion_id"])
        self.assertEqual(reply.body["items"][0]["title"], "Dune")
        self.suggestions.offered.assert_awaited_once_with("movie", 438631, self.person)

    async def test_without_a_suggestion_id_nothing_is_asked(self) -> None:
        await self.ask("PUT", "show-77", SEVERANCE)
        self.suggestions.offered.assert_not_awaited()



class KeptTests(ShortlistCase):
    async def test_shortlist_survives_a_restart(self) -> None:
        await self.ask("PUT", "movie-438631", DUNE)
        await self.ask("PUT", "show-77", SEVERANCE)
        before = await self.ask("GET")
        reopened = await Database.open(self.database_file)
        self.addAsyncCleanup(reopened.close)
        self.service = self.new_service(await SettingsService.load(reopened.settings))
        self.assertEqual((await self.ask("GET")).body, before.body)
        self.assertEqual(before.body["version"], 2)

    async def test_it_stays_out_of_a_backup_even_with_the_keys_and_passwords(self) -> None:
        await self.ask("PUT", "movie-438631", DUNE)
        for include in (False, True):
            kept = settings_for_backup(self.settings.current, include_secrets=include)
            self.assertFalse({"phone_shortlist", "phone_shortlist_version"} & set(kept))

    async def test_settings_from_before_the_shortlist_read_as_empty(self) -> None:
        self.assertEqual(Settings().phone_shortlist, ())
        self.assertEqual(Settings().phone_shortlist_version, 0)
