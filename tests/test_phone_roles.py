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
from app.ui.phone_settings import CONNECT_TEXT, MANAGER_TEXT
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

    async def pair_as(self, manager: bool) -> tuple[int, dict[str, Any]]:
        code = json.loads(self.service.new_pairing_payload(manager))["code"]
        return await self.ask("POST", "/v1/pair", {"code": code, "name": "Sam's phone"})

    async def test_code_for_a_viewer_pairs_a_viewer(self) -> None:
        status, body = await self.pair_as(False)
        self.assertEqual(status, 200)
        self.assertIs(body["manager"], False)
        self.assertFalse(self.service.phones()[0].manager)

    async def test_code_for_a_manager_pairs_a_manager(self) -> None:
        status, body = await self.pair_as(True)
        self.assertEqual(status, 200)
        self.assertIs(body["manager"], True)
        self.assertTrue(self.service.phones()[0].manager)

    async def test_the_newest_code_decides_the_role(self) -> None:
        self.service.new_pairing_payload(True)
        _, body = await self.pair_as(False)
        self.assertIs(body["manager"], False)

    async def test_the_phone_cant_choose_its_own_role(self) -> None:
        payload = json.loads(self.service.new_pairing_payload(False))
        self.assertNotIn("manager", payload)
        _, body = await self.ask(
            "POST", "/v1/pair", {"code": payload["code"], "name": "Sam", "manager": True}
        )
        self.assertIs(body["manager"], False)

    async def test_hello_says_the_role(self) -> None:
        _, viewer = await self.pair_as(False)
        _, manager = await self.pair_as(True)
        status, body = await self.ask("GET", "/v1/hello", token=viewer["token"])
        self.assertEqual(status, 200)
        self.assertIs(body["manager"], False)
        status, body = await self.ask("GET", "/v1/hello", token=manager["token"])
        self.assertEqual(status, 200)
        self.assertIs(body["manager"], True)


class ForgeRouteTests(ServiceCase):
    async def serve_the_forge(self, runtime: Any, profiles: Any) -> None:
        routes = PhoneForge(cast(ForgeRuntime, runtime), cast(ProfileService, profiles)).routes()
        self.service = self.make_service(routes)
        await self.service.set_on(True)

    async def pair_as(self, manager: bool) -> str:
        code = json.loads(self.service.new_pairing_payload(manager))["code"]
        _, body = await self.ask("POST", "/v1/pair", {"code": code, "name": "Sam's phone"})
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


class TickTests(PageCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.service.set_on(True)

    async def pair_as(self, manager: bool) -> None:
        code = json.loads(self.service.new_pairing_payload(manager))["code"]
        await self.ask("POST", "/v1/pair", {"code": code, "name": "Sam's phone"})

    def click_connect(self, page: SettingsPage) -> mock.MagicMock:
        with mock.patch.object(
            self.service, "new_pairing_payload", wraps=self.service.new_pairing_payload
        ) as made:
            self.shown_buttons(page)[CONNECT_TEXT].invoke()
        return made

    async def test_the_first_phone_is_a_manager_by_default(self) -> None:
        box, page = self.box()
        self.assertEqual(box.manager_tick.get(), 1)
        self.assertEqual(str(box.manager_tick.cget("text")), MANAGER_TEXT)
        self.click_connect(page).assert_called_once_with(True)

    async def test_later_phones_are_viewers_by_default(self) -> None:
        await self.pair_as(True)
        box, page = self.box()
        self.assertEqual(box.manager_tick.get(), 0)
        self.click_connect(page).assert_called_once_with(False)

    async def test_the_tick_goes_off_once_the_first_phone_paired(self) -> None:
        box, _ = self.box()
        await self.pair_as(True)
        await self.settle()
        self.assertEqual(box.manager_tick.get(), 0)

    async def test_the_tick_comes_back_when_no_phone_is_left(self) -> None:
        await self.pair_as(True)
        box, _ = self.box()
        await self.service.disconnect(self.service.phones()[0].id)
        await self.settle()
        self.assertEqual(box.manager_tick.get(), 1)

    async def test_a_click_on_the_tick_decides_the_code(self) -> None:
        await self.pair_as(True)
        box, page = self.box()
        box.manager_tick.toggle()
        self.click_connect(page).assert_called_once_with(True)

    async def test_changing_the_tick_replaces_a_code_on_screen(self) -> None:
        box, page = self.box()
        self.click_connect(page)
        first = box.code_image
        with mock.patch.object(
            self.service, "new_pairing_payload", wraps=self.service.new_pairing_payload
        ) as made:
            box.manager_tick.toggle()
        made.assert_called_once_with(False)
        self.assertIsNot(box.code_image, first)

    async def test_changing_the_tick_without_a_code_makes_none(self) -> None:
        box, _ = self.box()
        with mock.patch.object(self.service, "new_pairing_payload") as made:
            box.manager_tick.toggle()
        made.assert_not_called()

    async def test_the_tick_is_only_there_when_a_phone_can_connect(self) -> None:
        box, _ = self.box()
        self.window.update_idletasks()
        self.assertNotEqual(box.manager_tick.winfo_manager(), "")
        await self.service.set_on(False)
        await self.settle()
        self.assertEqual(box.manager_tick.winfo_manager(), "")
