"""The Forge over the phone: what the phone lists, and a tap that chooses and moves at once. The
real queue, resolver, renamer, executor and undo on a temporary forge.db (as the renamer's own
tests have them), a fake TMDB. Only temporary files are moved."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from app.config import AppPaths
from app.forge.clients.tmdb import TmdbError
from app.forge.core import duplicates as duplicates_module
from app.forge.core.auto import AutoRenamer, NamingChoice
from app.forge.core.ignored import IgnoreService
from app.forge.core.matcher import Resolution, Resolver
from app.forge.core.queue import ItemStatus, Queue, QueueItem
from app.forge.db.ignored import IgnoredRepo
from app.forge.db.matches import MatchRepo
from app.forge.models.history import RunSource
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.forge.runtime import ForgeRuntime
from app.i18n import t
from app.models import PairedPhone
from app.models.tmdb import TMDBTitle
from app.services import Services
from app.services.phone.forge import (
    KEPT,
    NO_ASIDE,
    NOT_REPLACED,
    REPLACED,
    REPLACED_TO_PLACE,
    PhoneForge,
    path_key,
)
from app.services.phone.server import Reply, Request
from app.services.profiles import ProfileService
from tests.forge.core.test_auto_renamer import Clock, RenamerTestCase, Tmdb
from tests.test_phone_coming_up import NOW

SHOW_NAME = "VisionQuest.S01E02.Theseus.1080p.WEB.mkv"
SHOW_NAME_2 = "VisionQuest.S01E01.No.Strings.1080p.WEB.mkv"
FILM_NAME = "Dune.2021.2160p.WEB-DL.mkv"
SHOW_GUESS = TMDBTitle(id=70, title="Vision Quest Again", date="2019-03-01", poster_path="/70.jpg")
SHOW_RIGHT = TMDBTitle(id=7, title="VisionQuest", date="2025-01-10", poster_path="/7.jpg")
FILM_GUESS = TMDBTitle(id=80, title="Dune Drifter", date="2021-02-01")
FILM_RIGHT = TMDBTitle(id=8, title="Dune", date="2021-09-15", poster_path="/8.jpg")
FAR_AWAY = TMDBTitle(id=99, title="Far Away", date="2001-05-05", poster_path="/99.jpg")
EPISODE_TARGET = Path("VisionQuest (2025)", "VisionQuest (2025) - Season 01")
SECOND_EPISODE = EPISODE_TARGET / "VisionQuest (2025) - S01E02 - Theseus.mkv"
FIRST_EPISODE = EPISODE_TARGET / "VisionQuest (2025) - S01E01 - No Strings.mkv"
FILM_TARGET = Path("Dune (2021)", "Dune (2021).mkv")
NOT_THERE = "Already sorted, or AirGrabber restarted."


class UnsureTmdb(Tmdb):
    """Every search gives more than one name unlike the file's, so the owner has to choose."""

    def __init__(self) -> None:
        super().__init__()
        self.searches: list[tuple[str, str, int | None]] = []
        self.search_down = False
        self.nothing_found = False
        self.details_down = False

    async def search_tv(self, query: str, year: int | None) -> list[TMDBTitle]:
        self.searches.append(("tv", query, year))
        if self.search_down:
            raise TmdbError("the router is restarting")
        return [] if self.nothing_found else [SHOW_GUESS, SHOW_RIGHT]

    async def search_movie(self, query: str, year: int | None) -> list[TMDBTitle]:
        self.searches.append(("movie", query, year))
        if self.search_down:
            raise TmdbError("the router is restarting")
        return [FILM_GUESS, TMDBTitle(id=81, title="Dune Part Zero", date="2021-01-01")]

    async def details(self, kind: MediaKind, tmdb_id: int) -> TMDBTitle | None:
        if self.details_down:
            raise TmdbError("the router is restarting")
        return {(MediaKind.EPISODE, 99): FAR_AWAY, (MediaKind.MOVIE, 8): FILM_RIGHT}.get(
            (kind, tmdb_id)
        )


class DisagreeingResolver(Resolver):
    """TMDB seems sure, but of a name that isn't the file's (`decide`: they don't agree)."""

    async def resolve(self, parsed: ParsedMedia) -> Resolution:
        media = ResolvedMedia(
            parsed=parsed,
            title=FILM_GUESS.title,
            year=parsed.year,
            episode_title=None,
            match_state=MatchState.CONFIDENT,
            tmdb_id=FILM_GUESS.id,
        )
        return Resolution(media=media, candidates=(FILM_GUESS,))


class Profiles:
    def __init__(self) -> None:
        self.gone = False

    def get(self, profile_id: int) -> object | None:
        return None if self.gone else object()


class ForgeOverPhoneCase(RenamerTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.unsure = UnsureTmdb()
        self.queue = Queue(Resolver(self.unsure, MatchRepo(self.db)), lambda event: None)
        self.parts = dataclasses.replace(self.parts, queue=self.queue, tmdb=self.unsure)
        await self.use("trial")
        self.clock = Clock()
        self.auto = AutoRenamer(self.parts, now=self.clock)
        self.addAsyncCleanup(self.auto.stop)
        self.ignored = IgnoreService(IgnoredRepo(self.db))
        self.forge_services = SimpleNamespace(
            queue=self.queue,
            auto=self.auto,
            run_lock=self.run_lock,
            settings=self.settings,
            tmdb=self.unsure,
            presets=self.parts.presets,
            ignored=self.ignored,
            aside_place=None,
            band_naming={},
        )
        self.runtime = SimpleNamespace(services=self.forge_services, reason="")
        self.profiles = Profiles()
        self.forge = PhoneForge(cast(ForgeRuntime, self.runtime), cast(ProfileService, self.profiles))
        self.routes = self.forge.routes()
        self.phone = PairedPhone(
            id="phone-1", name="Pixel", token_hash="0" * 64, profile_id=1, paired_at=NOW
        )

    async def watched(self, *names: str) -> list[QueueItem]:
        files = [(self.folders.download(name), self.folders.downloads) for name in names]
        ids = await self.queue.add_watched(files)
        await self.queue.wait_idle()
        items = [self.queue.get(i) for i in ids]
        return [i for i in items if i is not None]

    async def dropped(self, name: str) -> QueueItem:
        file = self.folders.downloads.parent / "elsewhere" / name
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b"video")
        await self.queue.add_paths([file], False)
        await self.queue.wait_idle()
        return next(i for i in self.queue.items if i.path == file)

    async def listing(self) -> dict[str, Any]:
        reply = await self.routes["GET", "/v1/forge"](
            Request("GET", "/v1/forge", {}, {}, self.phone)
        )
        self.assertEqual(reply.status, 200)
        return reply.body

    async def search(self, item_id: object, q: str = "Vision") -> Reply:
        path = f"/v1/forge/{item_id}/search"
        return await self.routes["GET", "/v1/forge/{id}/search"](
            Request("GET", path, {"id": str(item_id), "q": q}, {}, self.phone)
        )

    async def choose(self, item: QueueItem | str, **body: Any) -> Reply:
        if isinstance(item, str):
            item_id = item
        else:
            item_id = str(item.id)
            body.setdefault("path_key", path_key(item.path))
            body.setdefault("kind", item.parsed.kind.value)
        body.setdefault("tmdb_id", SHOW_RIGHT.id)
        path = f"/v1/forge/{item_id}/choose"
        return await self.routes["POST", "/v1/forge/{id}/choose"](
            Request("POST", path, {"id": item_id}, body, self.phone)
        )

    def in_series(self) -> list[Path]:
        return sorted(self.folders.series.rglob("*.mkv"))

    def in_movies(self) -> list[Path]:
        return sorted(self.folders.movies.rglob("*.mkv"))


class ListTest(ForgeOverPhoneCase):
    async def test_only_watched_items_are_listed(self) -> None:
        dropped = await self.dropped(SHOW_NAME)
        self.assertIs(dropped.status, ItemStatus.NEEDS_CHOICE)
        other = await self.dropped(SHOW_NAME_2)
        self.queue.hold(other.id, "held by hand")
        body = await self.listing()
        self.assertEqual(body, {"state": "ready", "waiting": [], "elsewhere": []})

    async def test_needs_choice_goes_in_waiting_and_held_or_error_in_elsewhere(self) -> None:
        waiting, held, failed, ready = await self.watched(
            SHOW_NAME, FILM_NAME, "Other.2020.mkv", "Fourth.2020.mkv"
        )
        self.queue.hold(held.id, "A taken name.")
        failed.status, failed.message = ItemStatus.ERROR, "It went wrong."
        ready.status = ItemStatus.READY
        body = await self.listing()
        self.assertEqual(body["state"], "ready")
        self.assertEqual(
            [(w["id"], w["file_name"]) for w in body["waiting"]], [(waiting.id, SHOW_NAME)]
        )
        self.assertEqual(
            body["elsewhere"],
            [
                {"file_name": FILM_NAME, "reason": "A taken name."},
                {"file_name": "Other.2020.mkv", "reason": "It went wrong."},
            ],
        )

    async def test_a_waiting_item_has_its_guess_and_candidates(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        [row] = (await self.listing())["waiting"]
        self.assertEqual(row["path_key"], path_key(item.path))
        self.assertEqual(row["same_show_count"], 1)
        guess = {
            "tmdb_id": 70,
            "kind": "episode",
            "title": "Vision Quest Again",
            "year": 2019,
            "poster_url": "https://image.tmdb.org/t/p/w342/70.jpg",
        }
        right = {
            "tmdb_id": 7,
            "kind": "episode",
            "title": "VisionQuest",
            "year": 2025,
            "poster_url": "https://image.tmdb.org/t/p/w342/7.jpg",
        }
        self.assertEqual(row["guess"], guess)
        self.assertEqual(row["candidates"], [guess, right])

    async def test_tab_count_is_only_waiting(self) -> None:
        waiting, held = await self.watched(SHOW_NAME, FILM_NAME)
        self.queue.hold(held.id, "A taken name.")
        self.assertEqual(self.auto._needing_owner(), {waiting.id, held.id})
        self.assertEqual(len((await self.listing())["waiting"]), 1)

    async def test_forge_not_started_says_starting(self) -> None:
        self.runtime.services = None
        self.assertEqual(
            await self.listing(), {"state": "starting", "waiting": [], "elsewhere": []}
        )
        for reply in (await self.search(1), await self.choose("1", kind="movie", path_key="x")):
            self.assertEqual(reply.status, 503)
            self.assertEqual(reply.body["error"], "forge_starting")
            self.assertTrue(reply.body["message"])

    async def test_a_forge_that_cant_start_says_why(self) -> None:
        self.runtime.services = None
        self.runtime.reason = "MediaForge couldn't start. The log has the details."
        body = await self.listing()
        self.assertEqual(body["state"], "starting")
        self.assertEqual(body["message"], "MediaForge couldn't start. The log has the details.")

    async def test_no_path_goes_to_the_phone_or_the_log(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        with self.assertLogs("app.services.phone.forge", logging.INFO) as logs:
            body = await self.listing()
            [row] = body["waiting"]
            reply = await self.choose(item)
        sent = json.dumps(body) + json.dumps(reply.body)
        self.assertNotIn(self.folders.downloads.name, sent)
        self.assertNotIn(str(self.tmp.name), sent)
        self.assertNotIn(self.folders.downloads.name, row["path_key"])
        written = "\n".join(logs.output)
        for secret in (SHOW_NAME, "VisionQuest", self.folders.downloads.name):
            self.assertNotIn(secret, written)

    async def test_a_file_without_an_episode_number_isnt_offered(self) -> None:
        # It would be called "E00" in the library: the PC's preview shows that, the phone doesn't.
        [pack] = await self.watched("VisionQuest.S01.Complete.1080p.mkv")
        self.assertIs(pack.status, ItemStatus.NEEDS_CHOICE)
        body = await self.listing()
        self.assertEqual(body["waiting"], [])
        self.assertEqual(
            body["elsewhere"],
            [
                {
                    "file_name": "VisionQuest.S01.Complete.1080p.mkv",
                    "reason": t("No season or episode number in the name."),
                }
            ],
        )
        reply = await self.choose(pack)
        self.assertEqual(reply.status, 410)
        self.assertEqual(self.in_series(), [])

    async def test_a_removed_person_gets_the_plain_answer(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        self.profiles.gone = True
        replies = [
            await self.routes["GET", "/v1/forge"](Request("GET", "/v1/forge", {}, {}, self.phone)),
            await self.search(item.id),
            await self.choose(item),
        ]
        for reply in replies:
            self.assertEqual(reply.status, 401)
            self.assertEqual(reply.body["error"], "not_paired")
        self.assertEqual(self.in_series(), [])


class SearchTest(ForgeOverPhoneCase):
    async def test_search_looks_for_the_kind_of_the_file(self) -> None:
        show, film = await self.watched(SHOW_NAME, FILM_NAME)
        self.unsure.searches.clear()
        reply = await self.search(show.id, "  Far  ")
        self.assertEqual(reply.status, 200)
        self.assertEqual([c["tmdb_id"] for c in reply.body["candidates"]], [70, 7])
        self.assertEqual(reply.body["candidates"][0]["kind"], "episode")
        reply = await self.search(film.id, "Dune")
        self.assertEqual(reply.body["candidates"][0]["kind"], "movie")
        self.assertEqual(self.unsure.searches, [("tv", "Far", None), ("movie", "Dune", None)])

    async def test_a_gone_or_hand_dropped_id_finds_nothing(self) -> None:
        dropped = await self.dropped(SHOW_NAME)
        self.unsure.searches.clear()
        for item_id in (dropped.id, 4321):
            reply = await self.search(item_id)
            self.assertEqual(reply.status, 410)
            self.assertEqual(reply.body["error"], "no_longer_there")
        self.assertEqual(self.unsure.searches, [])

    async def test_an_empty_or_huge_question_is_refused(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        for q in ("", "   ", "x" * 201):
            self.assertEqual((await self.search(item.id, q)).status, 400)

    async def test_tmdb_away_says_so(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        self.unsure.search_down = True
        with self.assertLogs("app.services.phone.forge", logging.WARNING) as logs:
            reply = await self.search(item.id)
        self.assertNotIn("router", "".join(logs.output))
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body["error"], "failed")
        self.assertNotIn("router", reply.body["message"])

    async def test_a_bad_id_is_refused(self) -> None:
        for bad in ("abc", "1_0", "+1", "-1", " 1", "01", "\u0663", "9" * 40):
            self.assertEqual((await self.search(bad)).status, 400, bad)


class ChooseTest(ForgeOverPhoneCase):
    async def test_choose_renames_at_once_with_the_phone_as_source(self) -> None:
        for automatic in ("trial", "on"):
            with self.subTest(automatic=automatic):
                await self.use(automatic)
                [item] = await self.watched(SHOW_NAME)
                reply = await self.choose(item)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.body["moved"], 1)
                self.assertTrue(reply.body["message"])
                self.assertTrue((self.folders.series / SECOND_EPISODE).exists())
                self.assertFalse(any(self.folders.downloads.rglob("*.mkv")))
                self.assertIs(item.status, ItemStatus.DONE)
                run = (await self.history.list_runs())[0]
                self.assertIs(run.source, RunSource.APP)
                (self.folders.series / SECOND_EPISODE).unlink()
                self.queue.remove(item.id)

    async def test_choose_applies_to_same_show_and_counts_them(self) -> None:
        first, second, film = await self.watched(SHOW_NAME, SHOW_NAME_2, FILM_NAME)
        rows = {w["id"]: w for w in (await self.listing())["waiting"]}
        self.assertEqual(rows[first.id]["same_show_count"], 2)
        self.assertEqual(rows[second.id]["same_show_count"], 2)
        self.assertEqual(rows[film.id]["same_show_count"], 1)
        reply = await self.choose(first)
        self.assertEqual(reply.body["moved"], rows[first.id]["same_show_count"])
        self.assertEqual(
            sorted(self.in_series()),
            sorted(self.folders.series / p for p in (FIRST_EPISODE, SECOND_EPISODE)),
        )
        self.assertIs(film.status, ItemStatus.NEEDS_CHOICE)
        self.assertEqual(self.in_movies(), [])
        self.assertIs(second.status, ItemStatus.DONE)

    async def test_a_hand_dropped_file_gets_the_choice_and_stays(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        dropped = await self.dropped(SHOW_NAME_2)
        where = dropped.path
        reply = await self.choose(item)
        self.assertEqual(reply.body["moved"], 1)
        self.assertEqual(self.in_series(), [self.folders.series / SECOND_EPISODE])
        self.assertTrue(where.exists())
        self.assertEqual(dropped.path, where)
        self.assertIs(dropped.status, ItemStatus.READY)
        self.assertEqual(dropped.media.tmdb_id, SHOW_RIGHT.id)

    async def test_a_held_file_of_the_same_show_stays_where_it_is(self) -> None:
        # "Sort this out on the PC": a taken name would become a numbered copy, an undone file
        # was put back on purpose. A tap on another episode doesn't touch them.
        item, held = await self.watched(SHOW_NAME, SHOW_NAME_2)
        self.queue.hold(held.id, "Put back by an undo.")
        where = held.path
        before = (await self.listing())["waiting"]
        self.assertEqual([w["same_show_count"] for w in before], [1])
        reply = await self.choose(item)
        self.assertEqual(reply.body["moved"], 1)
        self.assertEqual(self.in_series(), [self.folders.series / SECOND_EPISODE])
        self.assertTrue(where.exists())
        self.assertIs(held.status, ItemStatus.HELD)
        self.assertEqual(held.message, "Put back by an undo.")
        self.assertEqual(
            (await self.listing())["elsewhere"],
            [{"file_name": SHOW_NAME_2, "reason": "Put back by an undo."}],
        )

    async def test_a_held_file_in_the_library_keeps_its_replace(self) -> None:
        # Held because the episode is in the library already: its band offers "Replace the
        # one in the library", and a tap on another episode mustn't take that away.
        item, held = await self.watched(SHOW_NAME, SHOW_NAME_2)
        self.queue.hold(held.id, "In the library already.", in_library=True)
        reply = await self.choose(item)
        self.assertEqual(reply.body["moved"], 1)
        self.assertIs(held.status, ItemStatus.HELD)
        self.assertEqual(held.message, "In the library already.")
        self.assertTrue(held.in_library)

    async def test_a_same_show_file_without_a_number_stays_where_it_is(self) -> None:
        item, pack = await self.watched(SHOW_NAME, "VisionQuest.S01.Complete.1080p.mkv")
        where = pack.path
        self.assertEqual((await self.listing())["waiting"][0]["same_show_count"], 1)
        reply = await self.choose(item)
        self.assertEqual(reply.body["moved"], 1)
        self.assertTrue(where.exists())
        self.assertIs(pack.status, ItemStatus.HELD)
        self.assertEqual(pack.message, t("No season or episode number in the name."))

    async def test_rename_stays_undoable_in_history(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        source = item.path
        await self.choose(item)
        self.assertFalse(source.exists())
        run = (await self.history.list_runs())[0]
        self.assertIs(run.source, RunSource.APP)
        self.assertGreaterEqual(run.undoable_count, 1)
        await self.undo.undo_run(run.id)
        self.assertTrue(source.exists())
        self.assertEqual(self.in_series(), [])

    async def test_the_choice_is_taken_from_tmdb_when_it_came_from_a_search(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        reply = await self.choose(item, tmdb_id=FAR_AWAY.id)
        self.assertEqual(reply.body["moved"], 1)
        self.assertEqual(item.media.tmdb_id, FAR_AWAY.id)
        self.assertIn("Far Away", str(item.path))

    async def test_tmdb_away_or_an_unknown_title_changes_nothing(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        where = item.path
        self.unsure.details_down = True
        with self.assertLogs("app.services.phone.forge", logging.WARNING) as logs:
            reply = await self.choose(item, tmdb_id=FAR_AWAY.id)
        self.assertNotIn("router", "".join(logs.output))
        self.assertEqual((reply.status, reply.body["error"]), (502, "failed"))
        self.unsure.details_down = False
        reply = await self.choose(item, tmdb_id=12345)
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.body["error"], "no_results")
        self.assertIs(item.status, ItemStatus.NEEDS_CHOICE)
        self.assertEqual(item.path, where)
        self.assertTrue(where.exists())
        self.assertEqual(await self.history.list_runs(), [])

    async def test_nonsense_moves_nothing(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        key = path_key(item.path)
        for body in (
            {"tmdb_id": True},
            {"tmdb_id": "7"},
            {"tmdb_id": 0},
            {"tmdb_id": -7},
            {"tmdb_id": 7, "kind": "tv"},
            {"tmdb_id": 7, "kind": "movie"},  # a film's name on an episode
            {"tmdb_id": 7, "kind": 1},
            {"tmdb_id": 7, "path_key": 12},
        ):
            with self.subTest(body=body):
                self.assertEqual((await self.choose(item, **body)).status, 400)
        for bad in ("abc", "1_0", "+1", " 1", "01", "\u0663"):
            reply = await self.choose(bad, kind="episode", path_key=key)
            self.assertEqual(reply.status, 400, bad)
        self.assertEqual(self.in_series(), [])
        self.assertIs(item.status, ItemStatus.NEEDS_CHOICE)

    async def test_stale_id_says_no_longer_there_and_moves_nothing(self) -> None:
        item, other = await self.watched(SHOW_NAME, FILM_NAME)
        item_key = path_key(item.path)

        def refused(reply: Reply) -> None:
            self.assertEqual(reply.status, 410)
            self.assertEqual(reply.body["error"], "no_longer_there")
            self.assertEqual(reply.body["message"], t(NOT_THERE))
            self.assertEqual(self.in_series(), [])
            self.assertEqual(self.in_movies(), [])

        # An id gone.
        refused(await self.choose("4321", kind="episode", path_key=item_key))
        # An id reused for another file after a restart: the id is right, the key isn't.
        refused(await self.choose(str(other.id), kind="movie", path_key=item_key))
        self.assertIs(other.status, ItemStatus.NEEDS_CHOICE)
        # No longer waiting, or gone from the queue.
        self.queue.hold(item.id, "Sorted on the PC.")
        refused(await self.choose(item))
        self.assertEqual(item.message, "Sorted on the PC.")
        self.queue.remove(item.id)
        refused(await self.choose(item))
        # Hand-dropped: never moved from the phone, even with the right key.
        dropped = await self.dropped(SHOW_NAME_2)
        refused(await self.choose(dropped))
        self.assertEqual(dropped.media.tmdb_id, SHOW_GUESS.id)

    async def test_already_done_says_no_longer_there_and_moves_nothing(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        source_key = path_key(item.path)
        self.assertEqual((await self.choose(item)).body["moved"], 1)
        reply = await self.choose(str(item.id), kind="episode", path_key=source_key)
        self.assertEqual(reply.status, 410)
        reply = await self.choose(item, tmdb_id=FAR_AWAY.id)  # with the new path's key too
        self.assertEqual(reply.status, 410)
        self.assertEqual(self.in_series(), [self.folders.series / SECOND_EPISODE])
        self.assertEqual(len(await self.history.list_runs()), 1)

    async def test_two_taps_at_once_move_it_once(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        replies = await asyncio.gather(self.choose(item), self.choose(item, tmdb_id=FAR_AWAY.id))
        self.assertEqual([r.status for r in replies], [200, 410])
        self.assertEqual(replies[0].body["moved"], 1)
        self.assertEqual(self.in_series(), [self.folders.series / SECOND_EPISODE])
        self.assertEqual(len(await self.history.list_runs()), 1)

    async def test_two_taps_on_one_show_at_once_move_each_file_once(self) -> None:
        first, second = await self.watched(SHOW_NAME, SHOW_NAME_2)
        replies = await asyncio.gather(self.choose(first), self.choose(second))
        self.assertEqual([r.status for r in replies], [200, 410])
        self.assertEqual(replies[0].body["moved"], 2)
        self.assertEqual(len(self.in_series()), 2)
        self.assertEqual(len(await self.history.list_runs()), 1)

    async def test_a_running_run_is_waited_for_and_the_file_read_again_after(self) -> None:
        item, other = await self.watched(SHOW_NAME, FILM_NAME)
        await self.run_lock.acquire()
        tap = asyncio.create_task(self.choose(item))
        tap_other = asyncio.create_task(self.choose(other, tmdb_id=FILM_RIGHT.id))
        await asyncio.sleep(0.05)
        self.assertFalse(tap.done())
        self.assertEqual(self.in_series(), [])
        self.assertIs(item.status, ItemStatus.NEEDS_CHOICE)
        # The run that was going sorts the first file meanwhile.
        self.queue.hold(item.id, "Sorted by that run.")
        self.run_lock.release()
        reply, reply_other = await asyncio.gather(tap, tap_other)
        self.assertEqual(reply.status, 410)
        self.assertEqual(self.in_series(), [])
        self.assertEqual(reply_other.body["moved"], 1)
        self.assertEqual(self.in_movies(), [self.folders.movies / FILM_TARGET])

    async def test_a_file_gone_from_the_disk_says_so_and_lands_elsewhere(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        item.path.unlink()
        with self.assertLogs("app.forge.core.executor", logging.WARNING):
            reply = await self.choose(item)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["moved"], 0)
        self.assertTrue(reply.body["message"].endswith(t("Sort this out on the PC.")))
        self.assertEqual(self.in_series(), [])
        self.assertIs(item.status, ItemStatus.HELD)
        self.assertEqual(len((await self.listing())["elsewhere"]), 1)

    async def test_some_moved_says_how_many(self) -> None:
        first, second = await self.watched(SHOW_NAME, SHOW_NAME_2)
        second.path.unlink()
        with self.assertLogs("app.forge.core.executor", logging.WARNING):
            reply = await self.choose(first)
        self.assertEqual(reply.body["moved"], 1)
        self.assertTrue(reply.body["message"].endswith("Sort out the rest on the PC."))
        self.assertIs(second.status, ItemStatus.HELD)

    async def test_a_library_not_set_up_says_so_and_changes_nothing(self) -> None:
        [item] = await self.watched(SHOW_NAME)
        await self.settings.update(series_folder="")
        reply = await self.choose(item)
        self.assertEqual(reply.status, 409)
        self.assertEqual(reply.body["error"], "failed")
        self.assertEqual(reply.body["message"], t("Choose the Series library folder."))
        self.assertIs(item.status, ItemStatus.NEEDS_CHOICE)
        self.assertEqual(item.media.tmdb_id, SHOW_GUESS.id)

    async def test_a_renamer_that_fails_leaves_nothing_silently_waiting(self) -> None:
        [item] = await self.watched(SHOW_NAME)

        async def broken(*_args: object, **_kwargs: object) -> None:
            raise ValueError("a planned file is outside the library folder")

        self.forge_services.auto = SimpleNamespace(rename=broken)
        with self.assertRaises(ValueError):  # the server answers `failed` and logs it
            await self.choose(item)
        self.assertIs(item.status, ItemStatus.HELD)
        self.assertTrue(item.message)


class TakenNameTest(ForgeOverPhoneCase):
    """A taken name waits on the phone, never a "(1)" copy; the PC's Rename stays as it was."""

    SHOW_720 = "VisionQuest.S01E02.Theseus.720p.WEB.mkv"

    def taken(self) -> Path:
        there = self.folders.series / SECOND_EPISODE
        there.parent.mkdir(parents=True)
        there.write_bytes(b"already here")
        return there

    def copies(self) -> list[Path]:
        return [p for p in self.folders.series.rglob("*") if "(1)" in p.name]

    async def test_a_name_in_the_library_holds_the_file(self) -> None:
        there = self.taken()
        [item] = await self.watched(SHOW_NAME)
        source = item.path
        reply = await self.choose(item)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["moved"], 0)
        self.assertEqual(
            reply.body["message"],
            t("{reason} Sort this out on the PC.", reason=t("Already in the library.")),
        )
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.in_series(), [there])
        self.assertEqual(there.read_bytes(), b"already here")
        self.assertTrue(source.exists())
        self.assertIs(item.status, ItemStatus.HELD)
        body = await self.listing()
        self.assertEqual(body["waiting"], [])
        [entry] = body["elsewhere"]
        self.assertEqual(
            (entry["file_name"], entry["reason"], entry["can_replace"], entry["kind"]),
            (SHOW_NAME, t("Already in the library."), True, "episode"),
        )  # "Use this one instead" is offered there now
        await asyncio.gather(*self.forge._reads)
        self.assertEqual(await self.history.list_runs(), [])

    async def test_two_files_for_one_name_move_the_tapped_one_and_hold_the_other(self) -> None:
        better, other = await self.watched(SHOW_NAME, self.SHOW_720)
        reply = await self.choose(other)  # the owner tapped the 720p: that one goes
        self.assertEqual(reply.body["moved"], 1)
        self.assertEqual(
            reply.body["message"],
            t(
                "Moved {moved} of {total} files. {reason} Sort out the rest on the PC.",
                moved=1,
                total=2,
                reason=t("Two downloads would get the same name."),
            ),
        )
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.in_series(), [self.folders.series / SECOND_EPISODE])
        self.assertIs(other.status, ItemStatus.DONE)
        self.assertIs(better.status, ItemStatus.HELD)
        self.assertTrue(better.path.exists())
        self.assertEqual(
            (await self.listing())["elsewhere"],
            [
                {
                    "file_name": SHOW_NAME,
                    "reason": t("Two downloads would get the same name."),
                }
            ],
        )

    async def test_two_files_for_one_name_both_wait_when_neither_was_tapped(self) -> None:
        tapped, poor, good = await self.watched(
            "VisionQuest.S01E03.Third.1080p.WEB.mkv",
            "VisionQuest.S01E02.Theseus.480p.WEB.mkv",
            "VisionQuest.S01E02.Theseus.2160p.WEB.mkv",
        )
        reply = await self.choose(tapped)
        self.assertEqual(reply.body["moved"], 1)
        self.assertEqual(
            reply.body["message"],
            t(
                "Moved {moved} of {total} files. {reason} Sort out the rest on the PC.",
                moved=1,
                total=3,
                reason=t("Two downloads would get the same name."),
            ),
        )
        self.assertIs(tapped.status, ItemStatus.DONE)
        for held in (poor, good):
            self.assertIs(held.status, ItemStatus.HELD)
            self.assertEqual(held.message, t("Two downloads would get the same name."))
            self.assertTrue(held.path.exists())
        self.assertFalse((self.folders.series / SECOND_EPISODE).exists())
        self.assertEqual(len(self.in_series()), 1)
        self.assertEqual(self.copies(), [])

    async def test_two_files_for_a_taken_name_both_wait(self) -> None:
        self.taken()
        first, second = await self.watched(SHOW_NAME, self.SHOW_720)
        reply = await self.choose(first)
        self.assertEqual(reply.body["moved"], 0)
        self.assertEqual(
            reply.body["message"],
            t("{reason} Sort this out on the PC.", reason=t("Already in the library.")),
        )
        self.assertEqual(self.copies(), [])
        self.assertIs(first.status, ItemStatus.HELD)
        self.assertIs(second.status, ItemStatus.HELD)

    async def test_other_episodes_still_move_past_a_taken_name(self) -> None:
        self.taken()
        first, second = await self.watched(SHOW_NAME, SHOW_NAME_2)
        reply = await self.choose(first)
        self.assertEqual(reply.body["moved"], 1)
        self.assertIs(first.status, ItemStatus.HELD)
        self.assertIs(second.status, ItemStatus.DONE)
        self.assertEqual(self.copies(), [])

    async def test_the_pc_rename_still_makes_a_numbered_copy(self) -> None:
        there = self.taken()
        item, other = await self.watched(SHOW_NAME, self.SHOW_720)
        await self.queue.choose_match(item.id, SHOW_RIGHT)
        result = await self.auto.rename([item.id, other.id], source=RunSource.APP)
        self.assertEqual(result.renamed, 2)
        names = sorted(f.stem[-3:] for f in self.in_series() if f != there)
        self.assertEqual(names, ["(1)", "(2)"])  # numbered copies, as in 1.x
        self.assertTrue(there.exists())
        self.assertIs(item.status, ItemStatus.DONE)
        self.assertIs(other.status, ItemStatus.DONE)


class MatchHoldTest(ForgeOverPhoneCase):
    async def found_by_a_look(self, name: str) -> QueueItem:
        self.folders.download(name)
        await self.two_looks(self.auto, self.clock)
        [item] = self.queue.items
        return item

    async def test_not_found_goes_in_waiting_and_a_choice_moves_it(self) -> None:
        self.unsure.nothing_found = True
        item = await self.found_by_a_look(SHOW_NAME)
        self.assertIs(item.status, ItemStatus.HELD)
        self.assertEqual(item.message, t("Not found at TMDB."))
        body = await self.listing()
        self.assertEqual(body["elsewhere"], [])
        [row] = body["waiting"]
        self.assertEqual(row["id"], item.id)
        self.assertIsNone(row["guess"])
        self.assertEqual(row["candidates"], [])
        source = item.path
        reply = await self.choose(item, tmdb_id=FAR_AWAY.id)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body["moved"], 1)
        self.assertFalse(source.exists())
        self.assertIs(item.status, ItemStatus.DONE)
        self.assertEqual(item.message, "")
        self.assertIn("Far Away", str(item.path))
        self.assertEqual(item.media.tmdb_id, FAR_AWAY.id)

    async def test_a_wrong_match_goes_in_waiting_and_a_choice_moves_it(self) -> None:
        self.queue._resolver = DisagreeingResolver(self.unsure, MatchRepo(self.db))
        item = await self.found_by_a_look(FILM_NAME)
        self.assertIs(item.status, ItemStatus.HELD)
        self.assertEqual(item.message, t("The name and TMDB don't agree."))
        body = await self.listing()
        self.assertEqual(body["elsewhere"], [])
        [row] = body["waiting"]
        self.assertEqual(row["guess"]["tmdb_id"], FILM_GUESS.id)
        reply = await self.choose(item, tmdb_id=FILM_RIGHT.id)
        self.assertEqual(reply.body["moved"], 1)
        self.assertEqual(self.in_movies(), [self.folders.movies / FILM_TARGET])
        self.assertIs(item.status, ItemStatus.DONE)
        self.assertEqual(item.message, "")

    async def test_the_same_text_without_the_mark_stays_elsewhere(self) -> None:
        # The list goes by the mark, never by the translated text.
        [item] = await self.watched(SHOW_NAME)
        self.queue.hold(item.id, t("Not found at TMDB."))
        body = await self.listing()
        self.assertEqual(body["waiting"], [])
        self.assertEqual(len(body["elsewhere"]), 1)
        self.assertEqual((await self.choose(item)).status, 410)
        self.assertEqual(self.in_series(), [])

    async def test_a_later_hold_replaces_the_mark(self) -> None:
        self.unsure.nothing_found = True
        item = await self.found_by_a_look(SHOW_NAME)
        self.queue.hold(item.id, "Put back by an undo.")
        body = await self.listing()
        self.assertEqual(body["waiting"], [])
        self.assertEqual(
            body["elsewhere"], [{"file_name": SHOW_NAME, "reason": "Put back by an undo."}]
        )
        self.assertEqual((await self.choose(item, tmdb_id=FAR_AWAY.id)).status, 410)
        self.assertEqual(self.in_series(), [])

    async def test_a_held_match_counts_and_moves_with_its_show(self) -> None:
        first, second, other = await self.watched(
            SHOW_NAME, SHOW_NAME_2, "VisionQuest.S01E03.mkv"
        )
        self.queue.hold(second.id, t("Not found at TMDB."), match_problem=True)
        self.queue.hold(other.id, "A taken name.")
        rows = (await self.listing())["waiting"]
        self.assertEqual([r["same_show_count"] for r in rows], [2, 2])
        reply = await self.choose(second)
        self.assertEqual(reply.body["moved"], 2)
        self.assertEqual(len(self.in_series()), 2)
        self.assertIs(first.status, ItemStatus.DONE)
        self.assertIs(second.status, ItemStatus.DONE)
        self.assertIs(other.status, ItemStatus.HELD)
        self.assertTrue(other.path.exists())

    async def test_a_held_match_without_a_number_stays_elsewhere(self) -> None:
        self.unsure.nothing_found = True
        item = await self.found_by_a_look("VisionQuest.S01.Complete.1080p.mkv")
        self.assertTrue(item.match_problem)
        body = await self.listing()
        self.assertEqual(body["waiting"], [])
        self.assertEqual(len(body["elsewhere"]), 1)
        self.assertEqual((await self.choose(item, tmdb_id=FAR_AWAY.id)).status, 410)

    async def test_a_choice_that_fails_lands_elsewhere(self) -> None:
        self.unsure.nothing_found = True
        item = await self.found_by_a_look(SHOW_NAME)
        item.path.unlink()
        with self.assertLogs("app.forge.core.executor", logging.WARNING):
            reply = await self.choose(item, tmdb_id=FAR_AWAY.id)
        self.assertEqual(reply.body["moved"], 0)
        body = await self.listing()
        self.assertEqual(body["waiting"], [])
        self.assertEqual(len(body["elsewhere"]), 1)


class SourceNameTest(ForgeOverPhoneCase):
    async def test_a_phone_run_records_a_source_older_versions_know(self) -> None:
        # An older version raises on an unknown source: going back must survive a phone run.
        self.assertEqual(
            {s.value for s in RunSource}, {"app", "right-click", "import", "automatic"}
        )
        [item] = await self.watched(SHOW_NAME)
        await self.choose(item)
        self.assertEqual(await self.scalar("SELECT source FROM runs"), "app")

    async def test_path_key_is_the_same_for_one_file_and_differs_for_another(self) -> None:
        file = Path("C:/Downloads/Some.File.mkv")
        self.assertEqual(path_key(file), path_key(Path("c:\\downloads\\SOME.FILE.mkv")))
        self.assertNotEqual(path_key(file), path_key(Path("C:/Downloads/Other.File.mkv")))
        self.assertRegex(path_key(file), r"^[0-9a-f]{32}$")
        self.assertRegex(path_key(Path("C:/odd\udc80name.mkv")), r"^[0-9a-f]{32}$")

    async def test_the_routes_are_in_the_phone_connection(self) -> None:
        self.assertEqual(
            set(self.routes),
            {
                ("GET", "/v1/forge"),
                ("GET", "/v1/forge/{id}/search"),
                ("POST", "/v1/forge/{id}/choose"),
                ("POST", "/v1/forge/{id}/replace"),
                ("POST", "/v1/forge/{id}/keep"),
            },
        )
        (self.tmp / "app").mkdir()
        services = await Services.create(AppPaths(self.tmp / "app"))
        try:
            self.assertLessEqual(set(self.routes), set(services.phone._routes_factory()))
        finally:
            await services.aclose()


DUNE = ResolvedMedia(
    parsed=ParsedMedia(source_name=FILM_NAME, kind=MediaKind.MOVIE, title="Dune", year=2021,
                       quality="2160p"),
    title="Dune",
    year=2021,
    episode_title=None,
    match_state=MatchState.CONFIDENT,
    tmdb_id=8,
)  # fmt: skip


class ReplaceOverPhoneTest(ForgeOverPhoneCase):
    """"Use this one instead" and "Keep the one I have" from the phone: a download held because
    Dune is in the Movies folder already (made-up files in a temporary folder)."""

    async def in_library(self) -> tuple[QueueItem, Path]:
        old = self.folders.movies / FILM_TARGET
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old copy")
        [item] = await self.watched(FILM_NAME)
        item.resolution = Resolution(media=DUNE.model_copy(update={"parsed": item.parsed}))
        self.queue.hold(item.id, t("Already in the library."), in_library=True)
        return item, old

    async def post(self, action: str, item: QueueItem | str, **body: Any) -> Reply:
        item_id = item if isinstance(item, str) else str(item.id)
        if not isinstance(item, str):
            body.setdefault("path_key", path_key(item.path))
        path = f"/v1/forge/{item_id}/{action}"
        return await self.routes["POST", f"/v1/forge/{{id}}/{action}"](
            Request("POST", path, {"id": item_id}, body, self.phone)
        )

    async def test_the_list_says_what_replace_would_swap_without_waiting_for_the_disk(self) -> None:
        item, _old = await self.in_library()
        first = await self.listing()
        [entry] = first["elsewhere"]
        self.assertEqual(
            entry,
            {
                "file_name": FILM_NAME,
                "reason": t("Already in the library."),
                "id": item.id,
                "path_key": path_key(item.path),
                "can_replace": True,
                "kind": "movie",
                "size_bytes": None,  # not read yet: the request never waits
                "quality": "2160p",
                "replaces": {"file_name": None, "size_bytes": None, "quality": None},
            },
        )
        await asyncio.gather(*self.forge._reads)
        [entry] = (await self.listing())["elsewhere"]
        self.assertEqual(entry["size_bytes"], len(b"video"))
        self.assertEqual(
            entry["replaces"], {"file_name": "Dune (2021).mkv", "size_bytes": 8, "quality": None}
        )
        self.assertEqual(self.forge._reads, set())  # read once
        sent = json.dumps(entry)
        self.assertNotIn(self.tmp.name, sent)
        self.assertNotIn(self.folders.movies.name, sent)

    async def test_another_hold_isnt_offered(self) -> None:
        [item] = await self.watched(FILM_NAME)
        self.queue.hold(item.id, "Two downloads would get the same name.")
        [entry] = (await self.listing())["elsewhere"]
        self.assertEqual(set(entry), {"file_name", "reason"})
        reply = await self.post("replace", item)
        self.assertEqual((reply.status, reply.body["error"]), (410, "no_longer_there"))

    async def test_replace_puts_the_download_in_the_old_copys_place(self) -> None:
        item, old = await self.in_library()
        with self.assertLogs("app.services.phone.forge", logging.INFO) as logs:
            reply = await self.post("replace", item)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"moved": 1, "message": t(REPLACED)})
        self.assertEqual(old.read_bytes(), b"video")
        aside = self.folders.movies.parent / "Duplicates" / "Movies" / FILM_TARGET
        self.assertEqual(aside.read_bytes(), b"old copy")
        self.assertIs(item.status, ItemStatus.DONE)
        self.assertEqual(await self.scalar("SELECT source FROM runs"), "app")  # undo on the PC
        written = "\n".join(logs.output)
        for secret in (FILM_NAME, "Dune", self.tmp.name):
            self.assertNotIn(secret, written)
        again = await self.post("replace", item)
        self.assertEqual((again.status, again.body["error"]), (410, "no_longer_there"))

    async def test_a_wrong_key_or_id_moves_nothing(self) -> None:
        item, old = await self.in_library()
        for reply in (
            await self.post("replace", item, path_key="0" * 32),
            await self.post("replace", "999", path_key=path_key(item.path)),
            await self.post("keep", item, path_key="0" * 32),
        ):
            self.assertEqual((reply.status, reply.body["error"]), (410, "no_longer_there"))
        for reply in (await self.post("replace", "x1"), await self.post("keep", item, path_key=3)):
            self.assertEqual(reply.status, 400)
        self.assertEqual(old.read_bytes(), b"old copy")
        self.assertIs(item.status, ItemStatus.HELD)

    async def test_a_share_root_library_without_a_place_is_left_to_the_pc(self) -> None:
        item, old = await self.in_library()
        movies = self.folders.movies
        self.enterContext(  # as if the Movies folder were \\NAS\Movies: nothing beside it
            mock.patch.object(duplicates_module, "is_root", lambda folder: folder == movies)
        )
        [entry] = (await self.listing())["elsewhere"]
        self.assertIs(entry["can_replace"], False)  # a tap could only be refused
        reply = await self.post("replace", item)
        self.assertEqual(reply.status, 409)
        self.assertEqual(reply.body, {"error": "failed", "message": t(NO_ASIDE)})
        self.assertEqual(old.read_bytes(), b"old copy")
        self.forge_services.aside_place = self.tmp / "aside"  # chosen on the PC this session
        [entry] = (await self.listing())["elsewhere"]
        self.assertIs(entry["can_replace"], True)
        await asyncio.gather(*self.forge._reads)
        reply = await self.post("replace", item)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"moved": 1, "message": t(REPLACED_TO_PLACE)})
        self.assertEqual(
            (self.tmp / "aside" / "Movies" / FILM_TARGET).read_bytes(), b"old copy"
        )

    async def test_a_library_not_set_up_or_a_refused_replace_says_so(self) -> None:
        item, old = await self.in_library()
        await self.settings.update(movies_folder="")
        reply = await self.post("replace", item)
        self.assertEqual((reply.status, reply.body["error"]), (409, "failed"))
        await self.use("trial")

        async def refused(ids: Any, **kwargs: Any) -> Any:
            from app.forge.core.executor import RunResult

            return RunResult(failed=1)

        self.enterContext(mock.patch.object(self.auto, "replace", refused))
        reply = await self.post("replace", item)
        self.assertEqual(reply.status, 409)
        self.assertEqual(reply.body, {"error": "failed", "message": t(NOT_REPLACED)})
        self.assertEqual(old.read_bytes(), b"old copy")

    async def test_keep_leaves_the_download_alone_like_the_pc(self) -> None:
        item, old = await self.in_library()
        reply = await self.post("keep", item)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"message": t(KEPT)})
        self.assertIsNone(self.queue.get(item.id))
        self.assertTrue(self.ignored.snapshot().ignored(item.path))  # Auto-rename won't again
        self.assertEqual(old.read_bytes(), b"old copy")
        self.assertTrue(item.path.exists())  # nothing moved or deleted
        again = await self.post("keep", item)
        self.assertEqual(again.status, 410)

    async def test_a_forge_not_started_says_starting(self) -> None:
        self.runtime.services = None
        for action in ("replace", "keep"):
            reply = await self.post(action, "1", path_key="x")
            self.assertEqual((reply.status, reply.body["error"]), (503, "forge_starting"))

    async def test_two_taps_at_once_replace_once(self) -> None:
        item, old = await self.in_library()
        first, second = await asyncio.gather(
            self.post("replace", item), self.post("replace", item)
        )
        self.assertEqual(sorted([first.status, second.status]), [200, 410])
        self.assertEqual(old.read_bytes(), b"video")


class ReplaceRoundThreeTest(ReplaceOverPhoneTest):
    """Review 3: a replace judged by what moved, the PC's click first, the band's chip, a close."""

    async def test_a_replace_with_remove_after_forge_on_or_off_says_it_moved(self) -> None:
        for remove in (True, False):
            with self.subTest(remove_after_forge=remove):
                await self.settings.update(remove_after_forge=remove)
                item, old = await self.in_library()
                reply = await self.post("replace", item)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.body, {"moved": 1, "message": t(REPLACED)})
                self.assertEqual(self.queue.get(item.id) is None, remove)  # left the queue
                self.assertEqual(old.read_bytes(), b"video")
                await asyncio.gather(*self.forge._reads)
                for leftover in (self.folders.movies, self.folders.movies.parent / "Duplicates"):
                    for path in sorted(leftover.rglob("*"), reverse=True):
                        path.unlink() if path.is_file() else path.rmdir()
                self.queue.clear()

    async def test_the_pcs_own_click_first_answers_no_longer_there(self) -> None:
        real = self.auto.replace

        async def pc_first(ids: Any, **kwargs: Any) -> Any:
            await real(ids, source=RunSource.APP)  # the PC's "Use this one instead" landed first
            return await real(ids, **kwargs)

        for remove in (False, True):
            with self.subTest(remove_after_forge=remove):
                await self.settings.update(remove_after_forge=remove)
                item, old = await self.in_library()
                runs = await self.scalar("SELECT COUNT(*) FROM runs")
                with mock.patch.object(self.auto, "replace", pc_first):
                    reply = await self.post("replace", item)
                self.assertEqual((reply.status, reply.body["error"]), (410, "no_longer_there"))
                self.assertEqual(old.read_bytes(), b"video")  # moved once, by the PC
                self.assertEqual(await self.scalar("SELECT COUNT(*) FROM runs"), cast(int, runs) + 1)  # one
                for leftover in (self.folders.movies, self.folders.movies.parent / "Duplicates"):
                    for path in sorted(leftover.rglob("*"), reverse=True):
                        path.unlink() if path.is_file() else path.rmdir()
                self.queue.clear()

    async def test_the_band_chip_on_the_pc_names_the_copy_the_phone_replaces(self) -> None:
        flat = self.folders.movies / "Dune (2021).mkv"  # what Movie (Flat) names it
        flat.write_bytes(b"flat copy")
        item, folder_copy = await self.in_library()
        preset = next(p for p in await self.parts.presets.list() if p.name == "Movie (Flat)")
        self.forge_services.band_naming = {
            item.id: NamingChoice(preset, preset.rule, preset.options)
        }
        await self.listing()
        await asyncio.gather(*self.forge._reads)
        [entry] = (await self.listing())["elsewhere"]
        self.assertEqual(entry["replaces"]["size_bytes"], len(b"flat copy"))
        reply = await self.post("replace", item)
        self.assertEqual(reply.status, 200)
        self.assertEqual(flat.read_bytes(), b"video")  # the chip's name, as on the PC
        self.assertEqual(folder_copy.read_bytes(), b"old copy")  # the other copy untouched

    async def test_closing_stops_the_background_reads(self) -> None:
        await self.in_library()
        started = asyncio.Event()

        async def asleep(*args: Any, **kwargs: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

        with mock.patch("app.forge.core.replace_facts.replace_facts", asleep):
            await self.listing()
            await asyncio.wait_for(started.wait(), 5)
            [task] = self.forge._reads
            await self.forge.aclose()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.forge._reads, set())
