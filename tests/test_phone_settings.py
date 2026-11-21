"""The phone's Settings screen, the PC's side (`/v1/settings`, app/services/phone/settings.py):
asked over the real phone server (TLS), with a manager's phone and a viewer's."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.i18n import Language, set_language
from app.models import QualityProfile, Settings
from app.models.settings import QUALITY_NAMES
from app.services.auto_pause import longest_pause
from app.services.phone.server import Handler
from app.services.phone.settings import PhoneSettings
from tests.test_phone_service import ServiceCase

NOW = datetime(2026, 11, 21, 13, 30, tzinfo=UTC)
ENGLISH_LABELS = [
    {"id": "1080p", "label": "Full HD (1080p)"},
    {"id": "720p", "label": "HD (720p)"},
    {"id": "2160p", "label": "4K (2160p)"},
    {"id": "x265", "label": "Smaller files (1080p x265)"},
    {"id": "Any", "label": "Any quality"},
]


class SettingsCase(ServiceCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = NOW
        routes: dict[tuple[str, str], Handler] = {}
        self.service = self.make_service(routes)
        routes.update(
            PhoneSettings(
                self.settings, self.profiles, self.service.change_phone, now=lambda: self.now
            ).routes()
        )
        await self.service.set_on(True)
        _, manager = await self.pair("Sam's phone")  # the first phone pairs as a manager's
        _, viewer = await self.pair("Kim's phone")
        self.manager, self.manager_id = manager["token"], manager["phone_id"]
        self.viewer, self.viewer_id = viewer["token"], viewer["phone_id"]

    async def read(self, token: str) -> tuple[int, dict[str, Any]]:
        return await self.ask("GET", "/v1/settings", token=token)

    async def change(self, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return await self.ask("PATCH", "/v1/settings", body, token=token)

    def stored_phone(self, id: str) -> Any:
        return next(phone for phone in self.settings.current.phone_devices if phone.id == id)


class ReadTests(SettingsCase):
    async def test_a_managers_phone(self) -> None:
        status, body = await self.read(self.manager)
        self.assertEqual(status, 200)
        self.assertEqual(
            body["shared"],
            {
                "subtitle_languages": ["en"],
                "auto_download_new_shows": False,
                "auto_download_new_films": False,
                "auto_paused_until": None,
                "quality_tv": "1080p",
                "quality_movie": "1080p",
                "quality_choices": ENGLISH_LABELS,
            },
        )
        self.assertEqual(
            body["phone"],
            {
                "name": "Sam's phone",
                "profile_id": self.profiles.current_id,
                "profiles": [{"id": self.profiles.current_id, "name": "Me"}],
                "manager": True,
            },
        )

    async def test_a_viewers_phone_sees_the_same_and_every_person(self) -> None:
        kim = await self.profiles.add("Kim", self.profiles.current.colour)
        _, managers = await self.read(self.manager)
        status, body = await self.read(self.viewer)
        self.assertEqual(status, 200)
        self.assertEqual(body["shared"], managers["shared"])
        self.assertEqual(body["phone"]["name"], "Kim's phone")
        self.assertIs(body["phone"]["manager"], False)
        self.assertEqual(
            body["phone"]["profiles"],
            [{"id": self.profiles.current_id, "name": "Me"}, {"id": kim.id, "name": "Kim"}],
        )

    async def test_quality_labels_stay_english_on_a_dutch_pc(self) -> None:
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        _, body = await self.read(self.manager)
        self.assertEqual(body["shared"]["quality_choices"], ENGLISH_LABELS)
        self.assertEqual(
            [choice["id"] for choice in body["shared"]["quality_choices"]],
            [profile.value for profile in QUALITY_NAMES],
        )
        self.assertEqual({QualityProfile(c["id"]) for c in ENGLISH_LABELS}, set(QualityProfile))

    async def test_an_ended_pause_reads_as_none(self) -> None:
        await self.settings.update(auto_paused_until=NOW - timedelta(minutes=1))
        _, body = await self.read(self.manager)
        self.assertIsNone(body["shared"]["auto_paused_until"])

    async def test_hello_says_the_pc_has_settings(self) -> None:
        status, body = await self.ask("GET", "/v1/hello", token=self.viewer)
        self.assertEqual(status, 200)
        self.assertIs(body["settings"], True)


class SharedTests(SettingsCase):
    async def test_a_manager_changes_each_field(self) -> None:
        until = NOW + timedelta(hours=2)
        status, body = await self.change(
            self.manager,
            {
                "shared": {
                    "subtitle_languages": ["nl", "en"],
                    "auto_download_new_shows": True,
                    "auto_download_new_films": True,
                    "auto_paused_until": until.isoformat(),
                    "quality_tv": "2160p",
                    "quality_movie": "x265",
                }
            },
        )
        self.assertEqual(status, 200)
        current = self.settings.current
        self.assertEqual(current.subtitle_languages, ("nl", "en"))
        self.assertTrue(current.auto_download_new_shows and current.auto_download_new_films)
        self.assertEqual(current.auto_paused_until, until)
        self.assertEqual(
            (current.quality_tv, current.quality_movie), (QualityProfile.UHD, QualityProfile.HEVC)
        )
        # The answer is the whole GET body as it is now.
        _, read = await self.read(self.manager)
        self.assertEqual(body, read)
        shared = body["shared"]
        self.assertEqual(shared["subtitle_languages"], ["nl", "en"])
        self.assertEqual(datetime.fromisoformat(shared["auto_paused_until"]), until)
        self.assertIsNotNone(datetime.fromisoformat(shared["auto_paused_until"]).utcoffset())
        self.assertEqual((shared["quality_tv"], shared["quality_movie"]), ("2160p", "x265"))

    async def test_one_field_alone(self) -> None:
        status, body = await self.change(
            self.manager, {"shared": {"auto_download_new_films": True}}
        )
        self.assertEqual(status, 200)
        self.assertIs(body["shared"]["auto_download_new_films"], True)
        self.assertFalse(self.settings.current.auto_download_new_shows)

    async def test_a_viewer_is_refused_and_nothing_is_stored(self) -> None:
        before = self.settings.current
        status, body = await self.change(
            self.viewer,
            {"shared": {"auto_download_new_shows": True}, "phone": {"name": "Renamed"}},
        )
        self.assertEqual((status, body["error"]), (403, "not_manager"))
        self.assertTrue(body["message"])
        self.assertEqual(self.settings.current, before)
        self.assertEqual(self.stored_phone(self.viewer_id).name, "Kim's phone")

    async def test_every_quality_id_and_only_those(self) -> None:
        for profile in QualityProfile:
            with self.subTest(profile=profile):
                status, _ = await self.change(
                    self.manager, {"shared": {"quality_movie": profile.value}}
                )
                self.assertEqual(status, 200)
                self.assertIs(self.settings.current.quality_movie, profile)
        for wrong in ("x265/HEVC", "8K", "Full HD (1080p)", 1080, None):
            with self.subTest(wrong=wrong):
                status, body = await self.change(self.manager, {"shared": {"quality_tv": wrong}})
                self.assertEqual((status, body["error"]), (400, "bad_request"))
        self.assertIs(self.settings.current.quality_tv, QualityProfile.FULL_HD)

    async def test_switches_are_strictly_true_or_false(self) -> None:
        for wrong in ("true", 1, 0, None):
            with self.subTest(wrong=wrong):
                status, _ = await self.change(
                    self.manager, {"shared": {"auto_download_new_shows": wrong}}
                )
                self.assertEqual(status, 400)
        self.assertFalse(self.settings.current.auto_download_new_shows)

    async def test_subtitle_languages_through_the_models_rules(self) -> None:
        status, body = await self.change(
            self.manager, {"shared": {"subtitle_languages": ["NL", "pt-br", "xx", "nl"]}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["shared"]["subtitle_languages"], ["nl", "pt-BR"])
        for wrong in ([], ["xx"], "en,nl", [1], None):
            with self.subTest(wrong=wrong):
                status, body = await self.change(
                    self.manager, {"shared": {"subtitle_languages": wrong}}
                )
                self.assertEqual((status, body["error"]), (400, "bad_request"))
        self.assertEqual(self.settings.current.subtitle_languages, ("nl", "pt-BR"))

    async def test_all_or_nothing(self) -> None:
        before = self.settings.current
        status, body = await self.change(
            self.manager,
            {
                "shared": {
                    "auto_download_new_shows": True,
                    "subtitle_languages": ["nl"],
                    "quality_tv": "8K",
                },
                "phone": {"name": "Renamed"},
            },
        )
        self.assertEqual((status, body["error"]), (400, "bad_request"))
        self.assertEqual(
            body["message"], "AirGrabber doesn't offer that quality, so choose another one."
        )
        self.assertEqual(self.settings.current, before)
        self.assertEqual(self.stored_phone(self.manager_id).name, "Sam's phone")

    async def test_unknown_fields_are_refused_and_shown_ones_change_nothing(self) -> None:
        wrong: list[dict[str, Any]] = [
            {"shared": {"always_audio": "nl"}},
            {"phone": {"token_hash": "x"}},
            {"other": {}},
            {"shared": ["auto_download_new_shows"]},
        ]
        for body in wrong:
            with self.subTest(body=body):
                status, answer = await self.change(self.manager, body)
                self.assertEqual((status, answer["error"]), (400, "bad_request"))
        # What the phone can't change, sent back as it read it: nothing changes.
        status, answer = await self.change(
            self.viewer,
            {"shared": {"quality_choices": []}, "phone": {"manager": True, "profiles": []}},
        )
        self.assertEqual(status, 200)
        self.assertIs(answer["phone"]["manager"], False)
        self.assertFalse(self.stored_phone(self.viewer_id).manager)

    async def test_an_open_settings_window_hears_it_once(self) -> None:
        heard: list[Settings] = []
        self.addCleanup(self.settings.subscribe(heard.append))
        await self.change(
            self.manager,
            {"shared": {"auto_download_new_shows": True, "quality_tv": "720p"}},
        )
        self.assertEqual(len(heard), 1)  # one update: the window's listener hears it at once
        self.assertTrue(heard[0].auto_download_new_shows)
        self.assertIs(heard[0].quality_tv, QualityProfile.HD)


class PauseTests(SettingsCase):
    async def pause(self, value: Any) -> tuple[int, dict[str, Any]]:
        return await self.change(self.manager, {"shared": {"auto_paused_until": value}})

    async def test_a_pause_to_come(self) -> None:
        local = (NOW + timedelta(hours=1)).astimezone()
        status, body = await self.pause(local.isoformat())
        self.assertEqual(status, 200)
        self.assertEqual(self.settings.current.auto_paused_until, NOW + timedelta(hours=1))
        self.assertEqual(
            datetime.fromisoformat(body["shared"]["auto_paused_until"]), NOW + timedelta(hours=1)
        )
        status, _ = await self.pause("2026-11-21T14:00:00Z")  # Z is a time zone too
        self.assertEqual(status, 200)
        self.assertEqual(self.settings.current.auto_paused_until, NOW + timedelta(minutes=30))

    async def test_an_ended_time_and_null_resume(self) -> None:
        await self.pause((NOW + timedelta(hours=1)).isoformat())
        status, body = await self.pause((NOW - timedelta(seconds=1)).isoformat())
        self.assertEqual(status, 200)
        self.assertIsNone(self.settings.current.auto_paused_until)
        self.assertIsNone(body["shared"]["auto_paused_until"])
        await self.pause((NOW + timedelta(hours=1)).isoformat())
        status, body = await self.pause(None)
        self.assertEqual(status, 200)
        self.assertIsNone(self.settings.current.auto_paused_until)

    async def test_a_time_without_a_time_zone_is_refused(self) -> None:
        for wrong in ("2026-11-21T22:00:00", "2026-11-22", "tonight", 1763762400, True):
            with self.subTest(wrong=wrong):
                status, body = await self.pause(wrong)
                self.assertEqual((status, body["error"]), (400, "bad_request"))
        self.assertIsNone(self.settings.current.auto_paused_until)

    async def test_capped_as_the_tray_menus_longest(self) -> None:
        status, body = await self.pause((NOW + timedelta(days=5)).isoformat())
        self.assertEqual(status, 200)
        self.assertEqual(self.settings.current.auto_paused_until, longest_pause(NOW))
        self.assertLessEqual(longest_pause(NOW), NOW + timedelta(days=1))
        self.assertEqual(
            datetime.fromisoformat(body["shared"]["auto_paused_until"]), longest_pause(NOW)
        )

    def test_the_longest_pause(self) -> None:
        self.assertGreaterEqual(longest_pause(NOW), NOW + timedelta(hours=3))
        tomorrow = (NOW.astimezone() + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.assertGreaterEqual(longest_pause(NOW), tomorrow)


class PhoneTests(SettingsCase):
    async def test_any_phone_renames_itself(self) -> None:
        status, body = await self.change(self.viewer, {"phone": {"name": "  Kim's\nPixel  "}})
        self.assertEqual(status, 200)
        self.assertEqual(body["phone"]["name"], "Kim's Pixel")  # cleaned up as Settings does
        self.assertEqual(self.stored_phone(self.viewer_id).name, "Kim's Pixel")
        self.assertEqual(self.stored_phone(self.manager_id).name, "Sam's phone")
        status, _ = await self.change(self.viewer, {"phone": {"name": 5}})
        self.assertEqual(status, 400)

    async def test_a_manager_chooses_whose_lists_it_follows(self) -> None:
        kim = await self.profiles.add("Kim", self.profiles.current.colour)
        status, body = await self.change(self.manager, {"phone": {"profile_id": kim.id}})
        self.assertEqual(status, 200)
        self.assertEqual(body["phone"]["profile_id"], kim.id)
        self.assertEqual(self.stored_phone(self.manager_id).profile_id, kim.id)

    async def test_only_an_existing_person(self) -> None:
        own = self.stored_phone(self.manager_id).profile_id
        for wrong in (999, "1", True, 1.0):
            with self.subTest(wrong=wrong):
                status, body = await self.change(self.manager, {"phone": {"profile_id": wrong}})
                self.assertEqual((status, body["error"]), (400, "bad_request"))
        self.assertEqual(self.stored_phone(self.manager_id).profile_id, own)

    async def test_a_viewer_cant_choose_a_person(self) -> None:
        kim = await self.profiles.add("Kim", self.profiles.current.colour)
        own = self.stored_phone(self.viewer_id).profile_id
        status, body = await self.change(
            self.viewer, {"phone": {"profile_id": kim.id, "name": "Renamed"}}
        )
        self.assertEqual((status, body["error"]), (403, "not_manager"))
        self.assertEqual(self.stored_phone(self.viewer_id).profile_id, own)
        self.assertEqual(self.stored_phone(self.viewer_id).name, "Kim's phone")


class LogTests(SettingsCase):
    async def test_nothing_private_in_the_log(self) -> None:
        kim = await self.profiles.add("Grandma Kim", self.profiles.current.colour)
        until = (NOW + timedelta(hours=1)).isoformat()
        with self.assertLogs("app", level="DEBUG") as logs:
            await self.change(
                self.manager,
                {
                    "shared": {"subtitle_languages": ["pt-BR"], "auto_paused_until": until},
                    "phone": {"name": "Secret Pixel", "profile_id": kim.id},
                },
            )
            await self.change(self.manager, {"shared": {"quality_tv": "Sneaky value"}})
            await self.change(self.viewer, {"phone": {"name": "Another Secret"}})
        text = "\n".join(logs.output)
        for private in ("Secret Pixel", "Another Secret", "Grandma", "pt-BR", until, "Sneaky"):
            self.assertNotIn(private, text)
        self.assertIn("subtitle_languages", text)  # the fields' names are what it says
        self.assertNotIn(self.manager, text)  # nor a token
