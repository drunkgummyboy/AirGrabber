"""Who may do what over the phone connection: only a library manager's phone gets the Forge. The
server enforces it, so these ask the real routes (over TLS where it matters)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from app.forge.runtime import ForgeRuntime
from app.models import PairedPhone, Settings
from app.services.phone.coming_up import ComingUp
from app.services.phone.forge import PhoneForge
from app.services.phone.server import Reply, Request, manager_only
from app.services.profiles import ProfileService
from app.ui.phone_settings import (
    CONNECT_TEXT,
    CONTENTS_TEXT,
    MANAGER_TEXT,
    RENAME_TEXT,
    SAVE_NAME_TEXT,
)
from app.ui.settings_layout import SettingsPage
from tests.test_phone_downloads import EPISODE_BODY, DownloadsCase
from tests.test_phone_service import PageCase, ServiceCase

NOW = datetime(2026, 11, 7, tzinfo=UTC)
FORGE_ROUTES = (
    ("GET", "/v1/forge"),
    ("GET", "/v1/forge/1/search?query=Dune"),
    ("POST", "/v1/forge/1/choose"),
)


class SavedPhoneTests(ServiceCase):
    def test_old_saved_phones_count_as_manager(self) -> None:
        saved = {
            "id": "a",
            "name": "Phone",
            "token_hash": "h",
            "profile_id": 1,
            "paired_at": NOW.isoformat(),
        }
        settings = Settings.model_validate({"phone_devices": [saved]})
        self.assertTrue(settings.phone_devices[0].manager)

    def test_the_role_survives_saving(self) -> None:
        phone = PairedPhone(
            id="a", name="Phone", token_hash="h", profile_id=1, paired_at=NOW, manager=False
        )
        again = PairedPhone.model_validate_json(phone.model_dump_json())
        self.assertFalse(again.manager)


class PairingTests(ServiceCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.service.set_on(True)

    async def pair_one(self, name: str = "Sam's phone") -> dict[str, Any]:
        code = json.loads(self.service.new_pairing_payload())["code"]
        status, body = await self.ask("POST", "/v1/pair", {"code": code, "name": name})
        self.assertEqual(status, 200)
        return body

    async def test_the_first_phone_pairs_as_a_manager(self) -> None:
        body = await self.pair_one()
        self.assertIs(body["manager"], True)
        self.assertTrue(self.service.phones()[0].manager)

    async def test_later_phones_pair_as_viewers(self) -> None:
        await self.pair_one()
        body = await self.pair_one("Kim's phone")
        self.assertIs(body["manager"], False)
        self.assertFalse(self.service.phones()[1].manager)

    async def test_the_phone_cant_choose_its_own_role(self) -> None:
        await self.pair_one()
        payload = json.loads(self.service.new_pairing_payload())
        self.assertNotIn("manager", payload)
        _, body = await self.ask(
            "POST", "/v1/pair", {"code": payload["code"], "name": "Sam", "manager": True}
        )
        self.assertIs(body["manager"], False)

    async def test_hello_says_the_role_and_a_change_counts_at_once(self) -> None:
        manager = await self.pair_one()
        viewer = await self.pair_one("Kim's phone")
        status, body = await self.ask("GET", "/v1/hello", token=viewer["token"])
        self.assertEqual((status, body["manager"]), (200, False))
        status, body = await self.ask("GET", "/v1/hello", token=manager["token"])
        self.assertEqual((status, body["manager"]), (200, True))
        await self.service.change_phone(viewer["phone_id"], manager=True)
        _, body = await self.ask("GET", "/v1/hello", token=viewer["token"])
        self.assertIs(body["manager"], True)

    async def test_rename_and_person(self) -> None:
        body = await self.pair_one()
        other = await self.profiles.add("Kim", self.profiles.current.colour)
        await self.service.change_phone(body["phone_id"], name="  Kim's\nphone  ")
        await self.service.change_phone(body["phone_id"], profile_id=other.id)
        [phone] = self.service.phones()
        self.assertEqual((phone.name, phone.profile_id), ("Kim's phone", other.id))
        await self.service.change_phone(body["phone_id"], profile_id=999)  # nobody: no change
        self.assertEqual(self.service.phones()[0].profile_id, other.id)
        await self.service.change_phone("unknown", name="x")  # an unknown phone: nothing
        self.assertEqual(len(self.service.phones()), 1)


class ForgeRouteTests(ServiceCase):
    async def serve_the_forge(self, runtime: Any, profiles: Any) -> None:
        routes = PhoneForge(cast(ForgeRuntime, runtime), cast(ProfileService, profiles)).routes()
        self.service = self.make_service(routes)
        await self.service.set_on(True)

    async def pair_as(self, manager: bool) -> str:
        code = json.loads(self.service.new_pairing_payload())["code"]
        _, body = await self.ask("POST", "/v1/pair", {"code": code, "name": "Sam's phone"})
        await self.service.change_phone(body["phone_id"], manager=manager)
        return cast(str, body["token"])

    async def test_a_viewer_gets_403_on_every_forge_route(self) -> None:
        runtime, profiles = mock.MagicMock(), mock.MagicMock()
        await self.serve_the_forge(runtime, profiles)
        token = await self.pair_as(False)
        for method, path in FORGE_ROUTES:
            with self.subTest(path=path):
                body = {"path_key": "k", "tmdb_id": 1} if method == "POST" else None
                status, reply = await self.ask(method, path, body, token=token)
                self.assertEqual(status, 403)
                self.assertEqual(reply["error"], "not_manager")
                self.assertTrue(reply["message"])
        self.assertEqual(runtime.mock_calls, [])  # the Forge never heard of it
        self.assertEqual(profiles.mock_calls, [])

    async def test_a_manager_gets_through_on_every_forge_route(self) -> None:
        await self.serve_the_forge(SimpleNamespace(services=None, reason=""), mock.MagicMock())
        token = await self.pair_as(True)
        for method, path in FORGE_ROUTES:
            with self.subTest(path=path):
                body = {"path_key": "k", "tmdb_id": 1} if method == "POST" else None
                status, reply = await self.ask(method, path, body, token=token)
                self.assertNotEqual(status, 403, reply)
                self.assertNotEqual(reply.get("error"), "not_manager")

    async def test_no_phone_at_all_is_no_manager(self) -> None:
        handler = mock.AsyncMock(return_value=Reply(200, {}))
        reply = await manager_only(handler)(Request("GET", "/v1/forge", {}, {}, None))
        self.assertEqual(reply.status, 403)
        handler.assert_not_awaited()


class EveryPhoneTests(DownloadsCase):
    async def test_a_viewer_may_download_and_see_coming_up_data(self) -> None:
        viewer = self.phone.model_copy(update={"manager": False})
        reply = await self.post("/v1/download", EPISODE_BODY, viewer)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.body["outcome"], "grabbed")
        reply = await self.post("/v1/releases", EPISODE_BODY, viewer)
        self.assertEqual(reply.status, 200, reply.body)

        coming_up = ComingUp(
            self.library,
            self.watchlist,
            cast(Any, self.status),
            self.grabs,
            self.quick,
            self.queue,
            self.profiles,
            self.films,
        )
        request = Request("GET", "/v1/coming-up", {}, {}, viewer)
        reply = await coming_up.routes()["GET", "/v1/coming-up"](request)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertIn("items", reply.body)


class PhoneListTests(PageCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.service.set_on(True)

    async def pair_one(self, name: str = "Sam's phone") -> str:
        code = json.loads(self.service.new_pairing_payload())["code"]
        _, body = await self.ask("POST", "/v1/pair", {"code": code, "name": name})
        return cast(str, body["phone_id"])

    async def test_there_is_no_tick_before_connecting(self) -> None:
        _box, page = self.box()
        self.assertNotIn(MANAGER_TEXT, self.shown(page))
        self.assertIn(CONNECT_TEXT, self.shown_buttons(page))

    async def test_each_phone_has_its_own_tick(self) -> None:
        first = await self.pair_one()
        second = await self.pair_one("Kim's phone")
        box, _ = self.box()
        self.assertEqual(box.manager_ticks[first].get(), 1)
        self.assertEqual(box.manager_ticks[second].get(), 0)
        self.assertEqual(str(box.manager_ticks[first].cget("text")), MANAGER_TEXT)
        box.manager_ticks[second].toggle()
        await box.settled()
        await self.settle()
        self.assertTrue(self.service.phones()[1].manager)
        self.assertEqual(box.manager_ticks[second].get(), 1)  # the list was redrawn as it is
        box.manager_ticks[first].toggle()
        await box.settled()
        self.assertFalse(self.service.phones()[0].manager)

    async def test_rename_in_place(self) -> None:
        id = await self.pair_one()
        box, page = self.box()
        box.rename(id)
        assert box.name_entry is not None
        self.assertEqual(box.name_entry.get(), "Sam's phone")
        self.assertEqual(str(box.rename_buttons[id].cget("text")), SAVE_NAME_TEXT)
        box.name_entry.delete(0, "end")
        box.name_entry.insert(0, "Sam's Galaxy")
        box.rename(id)  # the button, now Save
        await box.settled()
        await self.settle()
        self.assertEqual(self.service.phones()[0].name, "Sam's Galaxy")
        self.assertIn("Sam's Galaxy", self.shown(page))
        self.assertIsNone(box.name_entry)

    async def test_escape_or_an_empty_name_changes_nothing(self) -> None:
        id = await self.pair_one()
        box, _ = self.box()
        box.rename(id)
        assert box.name_entry is not None
        box.name_entry.delete(0, "end")
        box.rename(id)  # saved empty: nothing
        await box.settled()
        self.assertEqual(self.service.phones()[0].name, "Sam's phone")
        box.rename(id)
        box._stop_renaming()  # Escape
        self.assertIsNone(box.name_entry)
        self.assertEqual(str(box.rename_buttons[id].cget("text")), RENAME_TEXT)

    async def test_the_person_is_a_choice_only_with_two_people(self) -> None:
        id = await self.pair_one()
        box, _ = self.box()
        self.assertEqual(box.person_menus, {})
        kim = await self.profiles.add("Kim", self.profiles.current.colour)
        await self.settle()
        box, _ = self.box()
        self.assertIn(id, box.person_menus)
        box.choose_person(id, kim.id)
        await box.settled()
        self.assertEqual(self.service.phones()[0].profile_id, kim.id)

    async def test_whats_on_it_opens_the_phones_menu(self) -> None:
        id = await self.pair_one()
        box, _ = self.box()
        self.assertEqual(str(box.contents_buttons[id].cget("text")), CONTENTS_TEXT)
        box.show_contents(id)
        window = box._open_contents
        self.assertIsNotNone(window)
        self.assertTrue(window.winfo_exists())
        box.show_contents(id)  # one at a time
        self.assertFalse(window.winfo_exists())
        box._close_contents()
