"""Send to phone (app/services/phone/sends.py): orders, `PUT /v1/on-phone`, and its windows
(app/ui/phone_contents.py, app/ui/send_to_phone.py). Made-up phones and titles only."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest import mock

import customtkinter as ctk
import httpx

from app.models import PairedPhone, PhoneContents, PhoneItem
from app.models.plex import PlexEpisode, PlexTitle
from app.services.phone.sends import MAX_ITEMS, PhoneSends, read_item
from app.services.phone.server import Request
from app.services.plex import PlexClient, PlexError, PlexUnreachable
from app.ui.phone_contents import (
    MADE_TEXT,
    MAKE_FAILED_TEXT,
    NOT_HEARD_TEXT,
    NOTHING_TEXT,
    PhoneContentsWindow,
    item_label,
    size_text,
)
from app.ui.send_to_phone import (
    HOW_FOLDERS_TEXT,
    HOW_TEXT,
    PHONE_SIZED_HOW_TEXT,
    PLEX_AWAY_TEXT,
    THERE_TEXT,
    SendToPhoneDialog,
    phones_by_person,
)
from tests.test_phone_service import PageCase, ServiceCase

NOW = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
FILM = PlexTitle(kind="movie", key="501", title="Dune", year=2021)
FILM_4K = PlexTitle(kind="movie", key="601", title="Arrival", year=2016, resolution="4K")
SHOW = PlexTitle(kind="show", key="700", title="VisionQuest", year=2026)
EPISODES = [
    PlexEpisode(key="701", season=1, episode=1, title="Pilot"),
    PlexEpisode(key="702", season=1, episode=2, title="Second"),
    PlexEpisode(key="711", season=2, episode=1, title="Back"),
]
# The folder library's titles (made-up TMDB ids): the order carries the video's key.
FOLDER_FILM = PlexTitle(
    kind="movie", key="folder:movie:603", title="Nightfall", year=1999, tmdb_id=603, resolution="4K"
)
FOLDER_SHOW = PlexTitle(kind="show", key="folder:show:1399", title="Harbour", tmdb_id=1399)
FOLDER_EPISODES = [
    PlexEpisode(key="episode:1399:1:1", season=1, episode=1, title="One"),
    PlexEpisode(key="episode:1399:1:2", season=1, episode=2, title="Two"),
]


class FakeCopies:
    """PhoneCopies as Send to phone sees it: what was made and dropped."""

    def __init__(self) -> None:
        self.available = True
        self.made: list[str] = []
        self.dropped: list[str] = []
        self.states: dict[str, tuple[str, int, str | None]] = {}

    def make(self, key: str) -> bool:
        self.made.append(key)
        self.states.setdefault(key, ("waiting", 0, None))
        return True

    def state_of(self, key: str) -> tuple[str, int, str | None] | None:
        return self.states.get(key)

    async def drop(self, key: str) -> None:
        self.dropped.append(key)
        self.states.pop(key, None)


def item(key: str, **fields: Any) -> dict[str, Any]:
    return {"key": key, "kind": "movie", "title": f"Film {key}", "state": "ready", **fields}


class SendsCase(ServiceCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sends = PhoneSends(self.settings, now=lambda: NOW)
        self.alex = await self.add_phone("a", "Alex's phone")
        self.kim = await self.add_phone("k", "Kim's phone")

    async def add_phone(self, id: str, name: str, person: int | None = None) -> PairedPhone:
        phone = PairedPhone(
            id=id,
            name=name,
            token_hash="h" + id,
            profile_id=person if person is not None else self.profiles.current_id,
            paired_at=NOW,
            manager=False,
        )
        await self.settings.update(phone_devices=(*self.settings.current.phone_devices, phone))
        return phone

    async def report(
        self, phone: PairedPhone, items: list[dict[str, Any]], *, knows_folders: bool = True
    ) -> tuple[int, dict[str, Any]]:
        """A report from a Companion that knows the folder library (`knows_folders`: it sends
        `X-Companion-Can: folder-library`), unless told otherwise."""
        handler = self.sends.routes()["PUT", "/v1/on-phone"]
        headers = {"x-companion-can": "folder-library"} if knows_folders else {}
        reply = await handler(Request("PUT", "/v1/on-phone", {}, {"items": items}, phone, headers))
        return reply.status, reply.body


class SendTests(SendsCase):
    async def test_a_film_waits_for_the_phone_and_goes_once(self) -> None:
        self.assertEqual(await self.sends.send_film(self.alex, FILM), 1)
        self.assertEqual(await self.sends.send_film(self.alex, FILM), 0)  # on its way already
        [order] = self.sends.orders_for("a")
        self.assertEqual((order.action, order.key, order.title), ("take", "501", "Dune"))
        self.assertEqual(self.sends.orders_for("k"), ())
        status, body = await self.report(self.alex, [])
        self.assertEqual(status, 200)
        self.assertEqual([one["key"] for one in body["take"]], ["501"])
        self.assertEqual(body["remove"], [])

    async def test_the_report_settles_what_it_shows(self) -> None:
        await self.sends.send_film(self.alex, FILM)
        _, body = await self.report(self.alex, [item("501", state="copying", progress=40)])
        self.assertEqual(body, {"take": [], "remove": []})
        self.assertEqual(self.sends.orders_for("a"), ())
        report = self.sends.contents_of("a")
        assert report is not None
        self.assertEqual((report.items[0].state, report.reported_at), ("copying", NOW))
        self.assertEqual(await self.sends.send_film(self.alex, FILM), 0)  # it's there now

    async def test_episodes_skip_what_is_there(self) -> None:
        await self.report(self.alex, [item("701", kind="episode", show="VisionQuest", season=1, episode=1)])
        self.assertEqual(await self.sends.send_episodes(self.alex, SHOW, EPISODES), 2)
        orders = self.sends.orders_for("a")
        self.assertEqual([o.key for o in orders], ["702", "711"])
        self.assertEqual((orders[0].show, orders[0].season, orders[0].episode), ("VisionQuest", 1, 2))
        self.assertEqual(self.sends.taken_keys("a"), {"701", "702", "711"})

    async def test_remove_and_cancel(self) -> None:
        await self.report(self.alex, [item("501")])
        await self.sends.remove("a", "501")
        [order] = self.sends.orders_for("a")
        self.assertEqual(order.action, "remove")
        await self.sends.remove("a", "501")  # asked once
        self.assertEqual(len(self.sends.orders_for("a")), 1)
        _, body = await self.report(self.alex, [item("501")])
        self.assertEqual([one["key"] for one in body["remove"]], ["501"])
        self.assertNotIn("501", self.sends.taken_keys("a"))
        _, body = await self.report(self.alex, [])  # deleted on the phone
        self.assertEqual(self.sends.orders_for("a"), ())
        await self.sends.send_film(self.alex, FILM)
        [waiting] = self.sends.orders_for("a")
        await self.sends.remove("a", "501")  # still waiting to go: simply not sent
        self.assertEqual(self.sends.orders_for("a"), ())
        await self.sends.send_film(self.alex, FILM)
        await self.sends.cancel(self.sends.orders_for("a")[0].id)
        self.assertEqual(self.sends.orders_for("a"), ())

    async def test_sending_what_is_being_removed_keeps_it(self) -> None:
        await self.report(self.alex, [item("501")])
        await self.sends.remove("a", "501")
        self.assertEqual(await self.sends.send_film(self.alex, FILM), 1)
        self.assertEqual(self.sends.orders_for("a"), ())  # the removal is called off

    async def test_a_phone_sees_only_its_own_orders(self) -> None:
        await self.sends.send_film(self.kim, FILM)
        _, body = await self.report(self.alex, [])
        self.assertEqual(body["take"], [])
        _, body = await self.report(self.kim, [])
        self.assertEqual(len(body["take"]), 1)

    async def test_disconnecting_drops_its_orders_and_report(self) -> None:
        await self.sends.send_film(self.alex, FILM)
        await self.report(self.alex, [item("9")])
        await self.service.disconnect("a")
        self.assertEqual(self.settings.current.phone_orders, ())
        self.assertEqual(self.settings.current.phone_contents, ())
        self.assertEqual(await self.sends.send_film(self.alex, FILM), 0)  # gone: nothing

    async def test_bad_reports_are_refused_whole(self) -> None:
        for items in (
            "nope",
            [item("a b")],  # not a key
            [item("1", kind="show")],
            [item("1", state="done")],
            [item("1", progress=101)],
            [item("1", title="")],
            [item("1", season=-1)],
            [item(str(n)) for n in range(MAX_ITEMS + 1)],
        ):
            with self.subTest(items=str(items)[:40]):
                handler = self.sends.routes()["PUT", "/v1/on-phone"]
                reply = await handler(Request("PUT", "/v1/on-phone", {}, {"items": items}, self.alex))
                self.assertEqual(reply.status, 400)
        self.assertIsNone(self.sends.contents_of("a"))

    async def test_a_folder_film_goes_as_its_video_key(self) -> None:
        self.assertEqual(await self.sends.send_film(self.alex, FOLDER_FILM), 1)
        self.assertEqual(await self.sends.send_film(self.alex, FOLDER_FILM), 0)  # on its way
        [order] = self.sends.orders_for("a")
        self.assertEqual((order.key, order.kind, order.title), ("movie:603", "movie", "Nightfall"))
        self.assertIn("movie:603", self.sends.taken_keys("a"))
        _, body = await self.report(self.alex, [])
        self.assertEqual([one["key"] for one in body["take"]], ["movie:603"])
        # A show's episodes go as their video keys.
        self.assertEqual(await self.sends.send_episodes(self.alex, FOLDER_SHOW, FOLDER_EPISODES), 2)
        keys = [o.key for o in self.sends.orders_for("a")]
        self.assertEqual(keys, ["movie:603", "episode:1399:1:1", "episode:1399:1:2"])

    async def test_an_old_companion_gets_no_folder_orders(self) -> None:
        """Two shelves: Plex connected and the folders on, a Downloaded film and a Plex film are
        sent. A Companion that doesn't know the folder library hears only the Plex one (it couldn't
        fetch "movie:603"); the folder order stays open on the PC for a newer Companion."""
        await self.settings.update(
            plex_token="made-up", folder_library_on=True, film_folders=(r"\\nas\films",)
        )
        await self.sends.send_film(self.alex, FOLDER_FILM)
        await self.sends.send_film(self.alex, FILM)
        status, body = await self.report(self.alex, [], knows_folders=False)
        self.assertEqual(status, 200)
        self.assertEqual([one["key"] for one in body["take"]], ["501"])
        self.assertEqual(body["remove"], [])
        self.assertEqual([o.key for o in self.sends.orders_for("a")], ["movie:603", "501"])
        # Nor a folder video's removal (from a report a newer Companion made).
        await self.report(self.alex, [item("movie:603"), item("501")])
        await self.sends.remove("a", "movie:603")
        _, body = await self.report(
            self.alex, [item("movie:603"), item("501")], knows_folders=False
        )
        self.assertEqual((body["take"], body["remove"]), ([], []))
        # A newer Companion hears both.
        _, body = await self.report(self.alex, [item("movie:603"), item("501")])
        self.assertEqual([one["key"] for one in body["remove"]], ["movie:603"])

    async def test_reports_with_folder_keys_are_read(self) -> None:
        await self.sends.send_film(self.alex, FOLDER_FILM)
        status, body = await self.report(self.alex, [
            item("movie:603", state="copying", progress=10),
            item("episode:1399:1:2", kind="episode", show="Harbour", season=1, episode=2),
            item("a" * 40),
        ])
        self.assertEqual((status, body), (200, {"take": [], "remove": []}))  # the film's settled
        report = self.sends.contents_of("a")
        assert report is not None
        self.assertEqual(
            [one.key for one in report.items], ["movie:603", "episode:1399:1:2", "a" * 40]
        )
        # Anything else is no key: the report is refused, as before.
        for key in ("movie/603", "a" * 41, "movie:6 3", "movie:603\n", "fïlm:1"):
            with self.subTest(key=key):
                status, _ = await self.report(self.alex, [item(key)])
                self.assertEqual(status, 400)
        self.assertEqual(len(self.sends.contents_of("a").items), 3)  # type: ignore[union-attr]

    async def test_a_folder_key_with_a_leading_zero_is_the_same_video(self) -> None:
        await self.sends.send_film(self.alex, FOLDER_FILM)
        status, body = await self.report(self.alex, [
            item("movie:0603"),
            item("episode:01399:01:02", kind="episode", show="Harbour", season=1, episode=2),
        ])
        self.assertEqual((status, body), (200, {"take": [], "remove": []}))  # the film's settled
        report = self.sends.contents_of("a")
        assert report is not None
        self.assertEqual([one.key for one in report.items], ["movie:603", "episode:1399:1:2"])

    async def test_an_episode_without_a_title_is_read(self) -> None:
        # The folder library names no episodes: the order goes out with "title": "", and a phone
        # that echoes it back must not have its whole report refused.
        nameless = [one.model_copy(update={"title": ""}) for one in FOLDER_EPISODES]
        self.assertEqual(await self.sends.send_episodes(self.alex, FOLDER_SHOW, nameless), 2)
        _, body = await self.report(self.alex, [])
        self.assertEqual([one["title"] for one in body["take"]], ["", ""])
        status, body = await self.report(self.alex, [
            {"key": "episode:1399:1:1", "kind": "episode", "title": "", "show": "Harbour",
             "season": 1, "episode": 1, "state": "copying"},
            {"key": "episode:1399:1:2", "kind": "episode", "show": "Harbour",
             "season": 1, "episode": 2, "state": "ready"},  # no title at all
        ])
        self.assertEqual((status, body), (200, {"take": [], "remove": []}))
        report = self.sends.contents_of("a")
        assert report is not None
        self.assertEqual([one.title for one in report.items], ["", ""])
        # A film still needs its title.
        status, _ = await self.report(self.alex, [item("1", title="")])
        self.assertEqual(status, 400)

    def test_a_film_drops_episode_fields_and_text_is_one_line(self) -> None:
        read = read_item(item("1", show="x", season=2, title="Two\nlines"))
        assert read is not None
        self.assertEqual((read.show, read.season, read.title), ("", None, "Two lines"))

    async def test_the_route_needs_plex_and_any_phone_may_use_it(self) -> None:
        self.service = self.make_service(self.sends.routes())
        await self.service.set_on(True)
        code = json.loads(self.service.new_pairing_payload())["code"]
        _, paired = await self.ask("POST", "/v1/pair", {"code": code, "name": "Sam"})
        status, body = await self.ask("PUT", "/v1/on-phone", {"items": []}, token=paired["token"])
        self.assertEqual((status, body), (200, {"take": [], "remove": []}))  # not a manager: fine
        await self.settings.update(plex_token="")
        status, body = await self.ask("PUT", "/v1/on-phone", {"items": []}, token=paired["token"])
        self.assertEqual((status, body["error"]), (409, "no_plex"))

    async def test_listeners_hear_changes(self) -> None:
        heard = mock.Mock()
        stop = self.sends.subscribe(heard)
        await self.sends.send_film(self.alex, FILM)
        self.assertEqual(heard.call_count, 1)
        await self.service.change_phone("a", name="Other")  # not what it shows
        self.assertEqual(heard.call_count, 1)
        stop()
        await self.sends.send_film(self.kim, FILM)
        self.assertEqual(heard.call_count, 1)


class PlexEpisodesTests(SendsCase):
    async def test_a_shows_episodes_by_season_and_number(self) -> None:
        asked: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            asked.append(request)
            leaves = [
                {"ratingKey": "711", "parentIndex": 2, "index": 1, "title": "Back"},
                {"ratingKey": "701", "parentIndex": 1, "index": 1, "title": " Pilot "},
                {"ratingKey": "x", "parentIndex": 1, "index": 3, "title": "Odd key"},
            ]
            return httpx.Response(200, json={"MediaContainer": {"Metadata": leaves}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            client = PlexClient(http, client_id=lambda: "made-up-id")
            found = await client.episodes("https://alex-nas.example:32400", "made-up", "700")
            self.assertEqual(len(found), 3)
            with self.assertRaises(PlexError):
                await client.episodes("https://alex-nas.example:32400", "made-up", "7/../1")
        self.assertEqual(asked[0].url.path, "/library/metadata/700/allLeaves")
        self.assertNotIn("made-up", str(asked[0].url))  # the token only in the header
        self.assertEqual(asked[0].headers["X-Plex-Token"], "made-up")


# -- the windows ---------------------------------------------------------------------------------


class WindowCase(PageCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sends = PhoneSends(self.settings, now=lambda: NOW)
        self.alex = PairedPhone(
            id="a", name="Alex's phone", token_hash="ha",
            profile_id=self.profiles.current_id, paired_at=NOW,
        )
        await self.settings.update(phone_devices=(self.alex,))

    async def settle(self) -> None:
        for _ in range(10):
            await asyncio.sleep(0.01)
            self.window.update()


class ContentsWindowTests(WindowCase):
    def contents(self) -> PhoneContentsWindow:
        window = PhoneContentsWindow(self.window, phone=self.alex, sends=self.sends, now=lambda: NOW)
        self.addCleanup(window.close)
        return window

    async def test_nothing_yet(self) -> None:
        window = self.contents()
        self.assertEqual(str(window.heard.cget("text")), NOT_HEARD_TEXT)
        self.assertEqual(window.rows, [NOTHING_TEXT])

    async def test_waiting_and_on_the_phone_follow_changes(self) -> None:
        window = self.contents()
        await self.sends.send_film(self.alex, FILM)
        await self.settle()
        self.assertEqual(window.rows, ["Dune"])
        handler = self.sends.routes()["PUT", "/v1/on-phone"]
        body = {"items": [
            item("501", title="Dune", size=2 * 1024**3),
            item("701", kind="episode", show="VisionQuest", season=1, episode=1, title="Pilot",
                 state="copying", progress=30),
            item("702", kind="episode", show="VisionQuest", season=1, episode=2, title="Second",
                 state="failed"),
        ]}
        await handler(Request("PUT", "/v1/on-phone", {}, body, self.alex))
        await self.settle()
        # A show is one folded row (the owner, 2026-11-09: no single episodes).
        self.assertEqual(window.rows, [
            "Dune | 2.0 GB",
            "▸ VisionQuest | 2 episodes · 1 copying · 1 couldn't copy",
        ])
        window.buttons["fold:visionquest"].invoke()  # unfolded: its episodes under it
        self.assertEqual(window.rows, [
            "Dune | 2.0 GB",
            "▾ VisionQuest | 2 episodes · 1 copying · 1 couldn't copy",
            "  S01E01 · Pilot | Copying 30%",
            "  S01E02 · Second | Couldn't copy",
        ])
        self.assertIn("Today", str(window.heard.cget("text")))
        window.buttons["501"].invoke()  # Remove
        await window.settled()
        await self.settle()
        self.assertEqual(window.rows[0], "Dune | Being removed")
        window.buttons["501"].invoke()  # Cancel the removal
        await window.settled()
        await self.settle()
        self.assertEqual(window.rows[0], "Dune | 2.0 GB")
        self.assertEqual(window.rows[1][0], "▾")  # still unfolded after the phone's news

    async def test_a_show_is_removed_and_taken_back_as_a_whole(self) -> None:
        window = self.contents()
        handler = self.sends.routes()["PUT", "/v1/on-phone"]
        body = {"items": [
            item("702", kind="episode", show="VisionQuest", season=1, episode=2, size=700 * 1024**2),
            item("701", kind="episode", show="VisionQuest", season=1, episode=1, size=1024**3),
            item("801", kind="episode", show="Alpha Line", season=3, episode=4),
        ]}
        await handler(Request("PUT", "/v1/on-phone", {}, body, self.alex))
        await self.settle()
        self.assertEqual(window.rows, [
            "▸ Alpha Line | 1 episode",
            "▸ VisionQuest | 2 episodes · 1.7 GB",
        ])
        self.assertEqual(window.buttons["show:visionquest"].cget("text"), "Remove all")
        window.buttons["show:visionquest"].invoke()
        await window.settled()
        await self.settle()
        removals = [o.key for o in self.sends.orders_for("a") if o.action == "remove"]
        self.assertEqual(sorted(removals), ["701", "702"])
        self.assertEqual(window.rows[1], "▸ VisionQuest | Being removed")
        self.assertEqual(window.buttons["show:visionquest"].cget("text"), "Cancel")
        window.buttons["show:visionquest"].invoke()  # takes the whole removal back
        await window.settled()
        await self.settle()
        self.assertEqual(self.sends.orders_for("a"), ())
        self.assertEqual(window.rows[1], "▸ VisionQuest | 2 episodes · 1.7 GB")

    async def test_waiting_episodes_are_grouped_too(self) -> None:
        window = self.contents()
        show = PlexTitle(kind="show", key="700", title="VisionQuest", year=2025)
        await self.sends.send_episodes(self.alex, show, list(EPISODES[:2]))
        await self.settle()
        self.assertEqual(window.rows, ["▸ VisionQuest | 2 episodes"])
        window.buttons["fold:visionquest"].invoke()
        self.assertEqual(len(window.rows), 3)
        window.buttons["show:visionquest"].invoke()  # Cancel all
        await window.settled()
        await self.settle()
        self.assertEqual(self.sends.orders_for("a"), ())
        self.assertEqual(window.rows, [NOTHING_TEXT])

    def test_labels(self) -> None:
        film = PhoneItem(key="1", kind="movie", title="Dune", state="ready")
        self.assertEqual(item_label(film), "Dune")
        # The phone sends its code as an episode's title: not shown twice.
        episode = PhoneItem(
            key="2", kind="episode", title="S02E05", show="VisionQuest", season=2, episode=5,
            state="ready",
        )
        self.assertEqual(item_label(episode), "VisionQuest · S02E05")
        two = episode.model_copy(update={"title": "S02E05-E06"})
        self.assertEqual(item_label(two), "VisionQuest · S02E05-E06")
        named = episode.model_copy(update={"title": "Back"})
        self.assertEqual(item_label(named), "VisionQuest · S02E05 · Back")
        self.assertEqual(size_text(0), "")
        self.assertEqual(size_text(700 * 1024**2), "700 MB")


class SendDialogTests(WindowCase):
    def dialog(self, title: PlexTitle, episodes: Any = None, **kwargs: Any) -> tuple[SendToPhoneDialog, mock.Mock]:
        sent = mock.Mock()
        loader = episodes or mock.AsyncMock(return_value=list(EPISODES))
        settings = {
            "phones": self.settings.current.phone_devices,
            "people": self.profiles.people,
            "current_person": self.profiles.current_id,
            **kwargs,
        }
        dialog = SendToPhoneDialog(
            self.window, title=title, sends=self.sends, episodes=loader, on_sent=sent, **settings
        )
        self.addCleanup(dialog.close)
        return dialog, sent

    async def test_a_film_goes_to_the_one_phone(self) -> None:
        dialog, sent = self.dialog(FILM)
        self.assertIn("Alex's phone", str(dialog.to_label.cget("text")))
        self.assertIsNone(dialog.person_menu)
        dialog.send()
        await dialog.settled()
        sent.assert_called_once_with("Sent Dune to Alex's phone.")
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual([o.key for o in self.sends.orders_for("a")], ["501"])
        again, _ = self.dialog(FILM)
        self.assertEqual(str(again.note.cget("text")), THERE_TEXT)
        self.assertEqual(str(again.send_button.cget("state")), "disabled")

    async def test_a_show_ticks_seasons_and_skips_what_is_there(self) -> None:
        await self.sends.send_episodes(self.alex, SHOW, EPISODES[:1])
        dialog, sent = self.dialog(SHOW)
        await dialog.settled()
        await self.settle()
        self.assertEqual(dialog.ticks["701"].get(), 1)
        self.assertEqual(str(dialog.ticks["701"].cget("state")), "disabled")
        self.assertEqual(dialog.chosen_episodes(), [])
        dialog.season_ticks[1].toggle()  # all of season 1 that isn't there
        self.assertEqual([e.key for e in dialog.chosen_episodes()], ["702"])
        dialog.ticks["711"].toggle()
        self.assertEqual(dialog.season_ticks[2].get(), 1)
        dialog.send()
        await dialog.settled()
        sent.assert_called_once_with("Sent 2 episodes to Alex's phone.")
        self.assertEqual({o.key for o in self.sends.orders_for("a")}, {"701", "702", "711"})

    async def test_a_folder_film_on_its_way_shows_so(self) -> None:
        dialog, sent = self.dialog(FOLDER_FILM)
        self.assertEqual(str(dialog.note.cget("text")), HOW_FOLDERS_TEXT)
        dialog.send()
        await dialog.settled()
        sent.assert_called_once_with("Sent Nightfall to Alex's phone.")
        again, _ = self.dialog(FOLDER_FILM)
        self.assertEqual(str(again.note.cget("text")), THERE_TEXT)

    async def test_plex_away_says_so(self) -> None:
        dialog, _ = self.dialog(SHOW, episodes=mock.AsyncMock(side_effect=PlexUnreachable("x")))
        await dialog.settled()
        self.assertEqual(str(dialog.note.cget("text")), PLEX_AWAY_TEXT)
        self.assertEqual(str(dialog.send_button.cget("state")), "disabled")

    async def test_first_the_person_then_their_phone(self) -> None:
        kim = await self.profiles.add("Kim", self.profiles.current.colour)
        tablet = self.alex.model_copy(update={"id": "t", "name": "Kim's tablet", "profile_id": kim.id})
        phone = self.alex.model_copy(update={"id": "p", "name": "Kim's phone", "profile_id": kim.id})
        await self.settings.update(phone_devices=(self.alex, tablet, phone))
        dialog, sent = self.dialog(FILM, people=self.profiles.people)
        assert dialog.person_menu is not None
        self.assertIsNone(dialog.phone_menu)  # Alex has one phone
        dialog.choose_person(kim.id)
        assert dialog.phone_menu is not None
        self.assertEqual(dialog.phone_menu.cget("values"), ["Kim's tablet", "Kim's phone"])
        dialog.choose_phone(phone)
        dialog.send()
        await dialog.settled()
        sent.assert_called_once_with("Sent Dune to Kim's phone.")
        self.assertEqual(len(self.sends.orders_for("p")), 1)

    def test_people_without_a_phone_are_left_out(self) -> None:
        people = self.profiles.people
        self.assertEqual(list(phones_by_person([self.alex], people)), [self.alex.profile_id])
        self.assertEqual(phones_by_person([], people), {})


class PhoneSizedTests(SendsCase):
    """A 4K film sent phone-sized (the owner, 2026-11-10): the PC makes the copy at once."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.copies = FakeCopies()
        self.sends = PhoneSends(self.settings, copies=self.copies, now=lambda: NOW)  # type: ignore[arg-type]

    async def test_only_a_4k_film_on_a_pc_that_can(self) -> None:
        self.assertTrue(self.sends.phone_sized_possible(FILM_4K))
        self.assertFalse(self.sends.phone_sized_possible(FILM))  # not 4K
        self.assertFalse(self.sends.phone_sized_possible(SHOW))
        self.copies.available = False
        self.assertFalse(self.sends.phone_sized_possible(FILM_4K))
        self.assertFalse(PhoneSends(self.settings).phone_sized_possible(FILM_4K))  # no copies

    async def test_the_copy_starts_and_the_phone_hears_it(self) -> None:
        self.assertEqual(await self.sends.send_film(self.alex, FILM_4K, phone_sized=True), 1)
        self.assertEqual(self.copies.made, ["601"])
        [order] = self.sends.orders_for("a")
        self.assertTrue(order.phone_sized)
        self.assertEqual(self.sends.copy_state(order), ("waiting", 0, None))
        _, body = await self.report(self.alex, [])
        self.assertTrue(body["take"][0]["phone_sized"])
        await self.sends.send_film(self.kim, FILM)
        _, body = await self.report(self.kim, [])
        self.assertFalse(body["take"][0]["phone_sized"])
        self.assertIsNone(self.sends.copy_state(self.sends.orders_for("k")[0]))
        self.assertEqual(self.copies.made, ["601"])  # a plain send makes nothing

    async def test_sent_again_makes_nothing_new(self) -> None:
        await self.sends.send_film(self.alex, FILM_4K, phone_sized=True)
        self.assertEqual(await self.sends.send_film(self.alex, FILM_4K, phone_sized=True), 0)
        await self.report(self.alex, [item("601", state="copying", progress=10)])
        self.assertEqual(await self.sends.send_film(self.alex, FILM_4K, phone_sized=True), 0)
        self.assertEqual(self.copies.made, ["601"])

    async def test_cancelling_drops_the_copy_nobody_wants(self) -> None:
        await self.sends.send_film(self.alex, FILM_4K, phone_sized=True)
        await self.sends.send_film(self.kim, FILM_4K, phone_sized=True)
        await self.sends.cancel(self.sends.orders_for("a")[0].id)
        self.assertEqual(self.copies.dropped, [])  # Kim's phone still wants it
        await self.sends.remove("k", "601")  # still waiting: simply not sent
        self.assertEqual(self.copies.dropped, ["601"])
        self.assertEqual(self.sends.orders_for("k"), ())

    async def test_a_4k_folder_film_goes_phone_sized_by_its_video_key(self) -> None:
        # Part 1 stores a folder film's best copy's quality as its resolution.
        self.assertTrue(self.sends.phone_sized_possible(FOLDER_FILM))
        self.assertFalse(
            self.sends.phone_sized_possible(FOLDER_FILM.model_copy(update={"resolution": "1080p"}))
        )
        self.assertEqual(await self.sends.send_film(self.alex, FOLDER_FILM, phone_sized=True), 1)
        self.assertEqual(self.copies.made, ["movie:603"])
        [order] = self.sends.orders_for("a")
        self.assertEqual(self.sends.copy_state(order), ("waiting", 0, None))
        await self.sends.cancel(order.id)
        self.assertEqual(self.copies.dropped, ["movie:603"])

    async def test_a_restart_makes_what_is_still_wanted(self) -> None:
        await self.sends.send_film(self.alex, FILM_4K, phone_sized=True)
        await self.sends.send_film(self.alex, FILM)
        again = FakeCopies()
        PhoneSends(self.settings, copies=again).resume_copies()  # type: ignore[arg-type]
        self.assertEqual(again.made, ["601"])


class PhoneSizedWindowTests(WindowCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.copies = FakeCopies()
        self.sends = PhoneSends(self.settings, copies=self.copies, now=lambda: NOW)  # type: ignore[arg-type]

    def dialog(self, title: PlexTitle) -> tuple[SendToPhoneDialog, mock.Mock]:
        sent = mock.Mock()
        dialog = SendToPhoneDialog(
            self.window, title=title, phones=self.settings.current.phone_devices,
            people=self.profiles.people, current_person=self.profiles.current_id,
            sends=self.sends, episodes=mock.AsyncMock(return_value=[]), on_sent=sent,
        )
        self.addCleanup(dialog.close)
        return dialog, sent

    async def test_the_tick_is_on_for_a_4k_film(self) -> None:
        dialog, sent = self.dialog(FILM_4K)
        assert dialog.phone_sized is not None
        self.assertEqual(dialog.phone_sized.get(), 1)
        self.assertEqual(str(dialog.note.cget("text")), PHONE_SIZED_HOW_TEXT)
        dialog.send()
        await dialog.settled()
        sent.assert_called_once_with("Sent Arrival to Alex's phone.")
        self.assertTrue(self.sends.orders_for("a")[0].phone_sized)
        self.assertEqual(self.copies.made, ["601"])
        again, _ = self.dialog(FILM_4K)
        assert again.phone_sized is not None
        self.assertEqual(str(again.phone_sized.cget("state")), "disabled")  # on its way

    async def test_unticked_it_goes_as_plex_has_it(self) -> None:
        dialog, _ = self.dialog(FILM_4K)
        assert dialog.phone_sized is not None
        dialog.phone_sized.toggle()
        self.assertEqual(str(dialog.note.cget("text")), HOW_TEXT)
        dialog.send()
        await dialog.settled()
        self.assertFalse(self.sends.orders_for("a")[0].phone_sized)
        self.assertEqual(self.copies.made, [])

    def test_no_tick_for_a_film_that_isnt_4k(self) -> None:
        dialog, _ = self.dialog(FILM)
        self.assertIsNone(dialog.phone_sized)

    async def test_the_phones_menu_says_how_far_the_copy_is(self) -> None:
        await self.sends.send_film(self.alex, FILM_4K, phone_sized=True)
        window = PhoneContentsWindow(self.window, phone=self.alex, sends=self.sends, now=lambda: NOW)
        self.addCleanup(window.close)
        self.assertEqual(window.rows, ["Arrival | Phone-sized"])
        self.assertIsNotNone(window._look_again)  # looked at again while it is being made
        self.copies.states["601"] = ("converting", 40, None)
        window.refresh()
        self.assertEqual(window.rows, ["Arrival | Making it phone-sized 40%"])
        self.copies.states["601"] = ("ready", 100, None)
        window.refresh()
        self.assertEqual(window.rows, [f"Arrival | {MADE_TEXT}"])
        self.assertIsNone(window._look_again)
        self.copies.states["601"] = ("failed", 0, "Plex doesn't have this film any more.")
        window.refresh()
        self.assertEqual(window.rows, [f"Arrival | {MAKE_FAILED_TEXT}"])
