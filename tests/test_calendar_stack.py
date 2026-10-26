"""The calendar's stack cards (several episodes of one show on one day): the grouping, the
range text, the episodes' states and what "Download all" fetches, the bottom row's looks, the
texts in English and Dutch, the batch run behind "Download all", the baked stack edges and
pill, and the wide and compact stack cards on a hidden Tk root."""

from __future__ import annotations

import asyncio
import tkinter as tk
import unittest
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk
from PIL import Image

from app import i18n
from app.i18n import Language
from app.models import CalendarEntry, CardSize, Episode, GrabOutcome, Show
from app.services import ImageCache
from app.ui import theme
from app.ui.calendar_cards import SLIM_ICON, SLIM_ICON_GAP, IconLook
from app.ui.calendar_face import (
    STACK_BEHIND,
    CapFace,
    PillFace,
    PosterFace,
    compose_cap,
    compose_pill,
    compose_poster,
    cover_size,
    episode_layout,
    poster_area,
    stack_edges,
)
from app.ui.calendar_stack import (
    CODE_BUTTONS_GAP,
    ROWS_GAP,
    SLIM_NAME_MIN,
    CompactStackCard,
    Stack,
    StackCard,
    StackControl,
    download_all_texts,
    entry_key,
    menu_label,
    more_fits,
    needed_entries,
    pill_texts,
    range_text,
    row_state,
    row_text,
    rows_height,
    slim_stack_rooms,
    stack_entries,
    stack_icon,
    stack_look,
)
from app.ui.card_scale import FULL, CardScale, line_height
from app.ui.download_control import (
    BatchKey,
    BatchState,
    BatchStep,
    DownloadActions,
    DownloadKey,
    DownloadStates,
    Phase,
    RunState,
    download_in_turn,
)

NOW = datetime.now(UTC)
DAY = NOW.date()
BG = (36, 36, 36)
EDGE = (78, 106, 133)
OUTSIDE = (18, 22, 32)
PLACEHOLDER = (51, 51, 51)
RED = (200, 0, 0)
NEAR, NEAR_EDGE = (33, 40, 48), (70, 96, 122)
FAR, FAR_EDGE = (30, 35, 41), (58, 78, 99)


def linespace(size: int, bold: bool) -> int:
    return line_height(size)


def close(actual: Any, expected: tuple[int, ...], tolerance: int = 3) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


def ep(
    show: int,
    season: int,
    number: int | None,
    *,
    aired: bool = True,
    done: bool = False,
    ident: int | None = None,
    day: date | None = None,
) -> CalendarEntry:
    stamp = NOW - timedelta(hours=2) if aired else NOW + timedelta(days=2)
    return CalendarEntry(
        show=Show(id=show, name=f"Made-up Show {show}", poster_url=f"https://img/{show}.jpg"),
        episode=Episode(
            id=ident if ident is not None else show * 1000 + season * 100 + (number or 99),
            show_id=show,
            season=season,
            number=number,
            air_date=day or stamp.date(),
            airstamp=stamp,
        ),
        is_tracked=True,
        is_downloaded=done,
    )


def no_states(_key: DownloadKey) -> RunState | None:
    return None


def states_of(states: dict[DownloadKey, RunState]) -> Callable[[DownloadKey], RunState | None]:
    return states.get


class LanguageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)


# -- the rules --------------------------------------------------------------------------------


class GroupingTests(unittest.TestCase):
    def test_one_card_per_show_in_the_order_the_shows_come(self) -> None:
        a7, a9, a8 = ep(1, 1, 7), ep(1, 1, 9), ep(1, 1, 8)
        b1 = ep(2, 3, 1)
        groups = stack_entries([a7, b1, a9, a8])
        self.assertEqual(groups, [(a7, a8, a9), (b1,)])  # episode order within a show

    def test_specials_go_last_and_seasons_in_order(self) -> None:
        special, s2e1, s1e10 = ep(1, 1, None), ep(1, 2, 1), ep(1, 1, 10)
        (group,) = stack_entries([special, s2e1, s1e10])
        self.assertEqual(group, (s1e10, special, s2e1))
        self.assertEqual(stack_entries([]), [])

    def test_the_stack_and_its_keys(self) -> None:
        stack = Stack((ep(4, 1, 7), ep(4, 1, 8)))
        self.assertEqual(stack.show.id, 4)
        self.assertEqual(stack.keys, tuple(entry_key(entry) for entry in stack.entries))
        self.assertEqual(stack.fold_key, (4, stack.entries[0].episode.air_date))
        self.assertEqual(stack.range, "S01E07 – E08")


class RangeTextTests(unittest.TestCase):
    @staticmethod
    def codes(*episodes: tuple[int, int | None]) -> str:
        return range_text([ep(1, season, number).episode for season, number in episodes])

    def test_a_run_in_one_season(self) -> None:
        self.assertEqual(self.codes((1, 7), (1, 8), (1, 9)), "S01E07 – E09")
        self.assertEqual(self.codes((1, 7), (1, 8)), "S01E07 – E08")

    def test_gaps(self) -> None:
        self.assertEqual(self.codes((1, 7), (1, 9)), "S01E07, E09")
        self.assertEqual(self.codes((1, 7), (1, 8), (1, 9), (1, 11)), "S01E07 – E09, E11")
        self.assertEqual(self.codes((2, 1), (2, 3), (2, 4)), "S02E01, E03 – E04")

    def test_across_seasons_and_with_a_special(self) -> None:
        self.assertEqual(self.codes((1, 10), (2, 1)), "S01E10 – S02E01")
        self.assertEqual(self.codes((1, 9), (1, 10), (2, 1)), "S01E09 – S02E01")
        self.assertEqual(self.codes((1, 7), (1, None)), "S01E07 – S01 Special")

    def test_one_episode_or_none(self) -> None:
        self.assertEqual(self.codes((3, 4)), "S03E04")
        self.assertEqual(self.codes((3, 4), (3, 4)), "S03E04")  # the same number twice
        self.assertEqual(range_text([]), "")


class StateTests(unittest.TestCase):
    def test_row_states(self) -> None:
        self.assertEqual(row_state(ep(1, 1, 1, aired=False), None, ()), "not_aired")
        self.assertEqual(row_state(ep(1, 1, 1, done=True), None, ()), "downloaded")
        self.assertEqual(row_state(ep(1, 1, 1), RunState(Phase.DONE), ()), "downloaded")
        had = ep(1, 1, 1)
        self.assertEqual(row_state(had, None, {had.episode.id}), "have")
        self.assertEqual(row_state(ep(1, 1, 1), None, ()), "needed")
        self.assertEqual(row_state(ep(1, 1, 1), RunState(Phase.NOTHING), ()), "needed")
        self.assertEqual(row_state(ep(1, 1, 1), RunState(Phase.WORKING), ()), "needed")

    def test_download_all_fetches_the_aired_episodes_still_needed(self) -> None:
        done, needed, had, later, special, grabbed_now = (
            ep(1, 1, 1, done=True),
            ep(1, 1, 2),
            ep(1, 1, 3),
            ep(1, 1, 4, aired=False),
            ep(1, 1, None),
            ep(1, 1, 5),
        )
        entries = (done, needed, had, later, special, grabbed_now)
        state = states_of({entry_key(grabbed_now): RunState(Phase.DONE, GrabOutcome.SENT)})
        self.assertEqual(needed_entries(entries, state, {had.episode.id}), (needed,))
        self.assertEqual(needed_entries(entries, no_states, ()), (needed, had, grabbed_now))


class LookTests(LanguageTestCase):
    def test_download_all_counts_the_episodes_still_needed(self) -> None:
        entries = (ep(1, 1, 7, done=True), ep(1, 1, 8), ep(1, 1, 9))
        look = stack_look(entries, no_states, None, ())
        self.assertEqual((look.layout, look.action), ("button", "download"))
        self.assertEqual(look.texts, ("⬇ Download all (2)", "⬇ Download (2)", "⬇ All (2)", "⬇ 2"))
        self.assertEqual(look.color, theme.ACCENT)
        had = stack_look(entries, no_states, None, {entries[1].episode.id})
        self.assertEqual(had.text, "⬇ Download all (1)")

    def test_the_batch_at_work(self) -> None:
        entries = (ep(1, 1, 7), ep(1, 1, 8), ep(1, 1, 9))
        look = stack_look(entries, no_states, BatchState(2, 3), ())
        self.assertEqual(look.layout, "label")
        self.assertEqual(look.texts, ("Downloading 2 of 3…", "Getting 2 of 3…", "2 of 3…"))
        self.assertIsNone(look.action)

    def test_one_episode_fetched_on_its_own(self) -> None:
        entries = (ep(1, 1, 7), ep(1, 1, 8))
        state = states_of({entry_key(entries[1]): RunState(Phase.WORKING)})
        look = stack_look(entries, state, None, ())
        self.assertEqual((look.layout, look.text), ("label", "Finding the best one…"))

    def test_all_downloaded_is_a_check_like_a_single_card(self) -> None:
        entries = (ep(1, 1, 7, done=True), ep(1, 1, 8, done=True), ep(1, 1, 9, aired=False))
        look = stack_look(entries, no_states, None, ())
        self.assertEqual(
            (look.text, look.action, look.color), ("✓ Grabbed", "choose", theme.GRABBED)
        )
        # Grabbed by this session's runs, all sent the same way: their outcome.
        fresh = (ep(1, 1, 7), ep(1, 1, 8))
        sent = {entry_key(entry): RunState(Phase.DONE, GrabOutcome.SENT) for entry in fresh}
        self.assertEqual(stack_look(fresh, states_of(sent), None, ()).text, "✓ Sent")
        mixed = {**sent, entry_key(fresh[0]): RunState(Phase.DONE, GrabOutcome.SAVED)}
        self.assertEqual(stack_look(fresh, states_of(mixed), None, ()).text, "✓ Grabbed")

    def test_nothing_aired(self) -> None:
        entries = (ep(1, 1, 7, aired=False), ep(1, 1, 8, aired=False))
        look = stack_look(entries, no_states, None, ())
        self.assertEqual((look.text, look.action), ("Not Aired", None))

    def test_the_compact_cards_icon(self) -> None:
        entries = (ep(1, 1, 7), ep(1, 1, 8))
        download = stack_icon(stack_look(entries, no_states, None, ()))
        self.assertEqual((download.text, download.action), ("⬇", "download"))
        self.assertEqual(download.tip, "⬇ Download all (2)")
        working = stack_icon(stack_look(entries, no_states, BatchState(1, 2), ()))
        self.assertEqual(
            (working.text, working.action, working.tip), ("…", None, "Downloading 1 of 2…")
        )
        done = tuple(entry.model_copy(update={"is_downloaded": True}) for entry in entries)
        check = stack_icon(stack_look(done, no_states, None, ()))
        self.assertEqual((check.text, check.action, check.color), ("✓", "choose", theme.GRABBED))
        later = tuple(ep(1, 1, n, aired=False) for n in (7, 8))
        self.assertEqual(stack_icon(stack_look(later, no_states, None, ())).text, "")


class TextTests(LanguageTestCase):
    def test_english(self) -> None:
        self.assertEqual(pill_texts(3, False), ("3 episodes ▾", "3 ▾"))
        self.assertEqual(pill_texts(2, True), ("2 episodes ▴", "2 ▴"))
        self.assertEqual(
            [row_text(kind) for kind in ("downloaded", "needed", "not_aired", "have")],
            ["✓ downloaded", "still needed", "not aired", "✓ have it"],
        )
        entry = ep(1, 1, 8)
        self.assertEqual(menu_label(entry, None, ()), "S01E08 · still needed")

    def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        self.assertEqual(pill_texts(3, False), ("3 afleveringen ▾", "3 ▾"))
        self.assertEqual(
            download_all_texts(2),
            ("⬇ Alle downloaden (2)", "⬇ Download (2)", "⬇ Alle (2)", "⬇ 2"),
        )
        self.assertEqual(
            [row_text(kind) for kind in ("downloaded", "needed", "not_aired", "have")],
            ["✓ gedownload", "nog nodig", "niet uitgezonden", "✓ heb ik"],
        )
        look = stack_look((ep(1, 1, 7), ep(1, 1, 8)), no_states, BatchState(2, 2), ())
        self.assertEqual(look.text, "2 van 2 downloaden…")


class SizeTests(unittest.TestCase):
    def test_rows_height(self) -> None:
        self.assertEqual(rows_height(0, 16), 0)
        row = SLIM_ICON + 16
        self.assertEqual(rows_height(3, 16), ROWS_GAP + 3 * row + 2 * 4)
        small = CardScale.of(CardSize.XS)
        self.assertLess(rows_height(3, 13, small), rows_height(3, 16))

    def test_the_more_button_gives_way_before_the_code(self) -> None:
        both = IconLook("⬇", more=True)
        need = 42 + CODE_BUTTONS_GAP + 2 * SLIM_ICON + SLIM_ICON_GAP
        self.assertTrue(more_fits(need, 42, both))
        self.assertFalse(more_fits(need - 1, 42, both))
        self.assertTrue(more_fits(need - SLIM_ICON - SLIM_ICON_GAP, 42, IconLook("✓")))

    def test_compact_line_rooms(self) -> None:
        name, range_room = slim_stack_rooms(200, 50, 16, SLIM_ICON)
        self.assertEqual(range_room, 50)
        self.assertGreater(name, SLIM_NAME_MIN)
        # Narrow: the range gives way so the name keeps its least room.
        name, range_room = slim_stack_rooms(120, 50, 16, SLIM_ICON)
        self.assertEqual(name, SLIM_NAME_MIN)
        self.assertLess(range_room, 50)
        name, range_room = slim_stack_rooms(40, 50, 16, SLIM_ICON)
        self.assertEqual(range_room, 0)


# -- the batch behind "Download all" ------------------------------------------------------------


class Painted:
    """What DownloadStates keeps per key: counts its repaints."""

    def __init__(self, key: DownloadKey) -> None:
        self.key = key
        self.repaints = 0

    def repaint(self) -> None:
        self.repaints += 1

    def winfo_exists(self) -> bool:
        return True


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.states = DownloadStates()
        self.entries = (ep(1, 1, 7), ep(1, 1, 8), ep(1, 1, 9))
        self.batch: BatchKey = tuple(entry_key(entry) for entry in self.entries)
        self.seen: list[tuple[str, BatchState | None]] = []

    def step(self, entry: CalendarEntry, result: bool = True) -> BatchStep:
        key = entry_key(entry)

        async def run() -> bool:
            self.seen.append((entry.episode.code, self.states.batch(self.batch)))
            self.states.set(key, RunState(Phase.WORKING))
            await asyncio.sleep(0)
            self.states.set(key, RunState(Phase.DONE, GrabOutcome.SENT) if result else None)
            return result

        return key, run

    async def test_one_after_another_with_the_progress(self) -> None:
        steps = [self.step(entry) for entry in self.entries]
        await download_in_turn(self.states, self.batch, steps)
        self.assertEqual(
            self.seen,
            [
                ("S01E07", BatchState(1, 3)),
                ("S01E08", BatchState(2, 3)),
                ("S01E09", BatchState(3, 3)),
            ],
        )
        self.assertIsNone(self.states.batch(self.batch))  # over
        for key in self.batch:
            state = self.states.get(key)
            assert state is not None
            self.assertIs(state.phase, Phase.DONE)  # each episode's own state

    async def test_a_failure_stops_the_batch(self) -> None:
        first, second, third = self.entries
        steps = [self.step(first), self.step(second, result=False), self.step(third)]
        await download_in_turn(self.states, self.batch, steps)
        self.assertEqual([code for code, _ in self.seen], ["S01E07", "S01E08"])
        self.assertIsNone(self.states.get(entry_key(third)))
        self.assertIsNone(self.states.batch(self.batch))

    async def test_an_episode_already_being_fetched_is_left_to_its_run(self) -> None:
        busy = entry_key(self.entries[1])
        self.states.set(busy, RunState(Phase.WORKING))
        await download_in_turn(self.states, self.batch, [self.step(e) for e in self.entries])
        self.assertEqual([code for code, _ in self.seen], ["S01E07", "S01E09"])

    async def test_cancelled_it_still_ends(self) -> None:
        started = asyncio.Event()

        async def hang() -> bool:
            started.set()
            await asyncio.Event().wait()
            return True

        task = asyncio.create_task(
            download_in_turn(self.states, self.batch, [(self.batch[0], hang)])
        )
        await started.wait()
        self.assertEqual(self.states.batch(self.batch), BatchState(1, 1))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.states.batch(self.batch))

    def test_the_batch_repaints_its_titles_controls_once_each(self) -> None:
        first, second = Painted(self.batch[0]), Painted(self.batch[1])
        other = Painted(entry_key(ep(9, 1, 1)))
        for painted in (first, second, other):
            self.states.attach(cast(Any, painted))
        self.states.attach(cast(Any, first))  # attached twice: still painted once
        self.states.set_batch(self.batch, BatchState(1, 3))
        self.assertEqual((first.repaints, second.repaints, other.repaints), (1, 1, 0))


# -- the baked images ---------------------------------------------------------------------------


def face(**changes: Any) -> PosterFace:
    values: dict[str, Any] = {
        "size": (138, 168),
        "border": 2,
        "radius": 12,
        "fade_from": 21,
        "solid_left": 90,
        "solid_top": 100,
        "row_fade": 15,
        "bg": BG,
        "edge": EDGE,
        "outside": OUTSIDE,
        "placeholder": PLACEHOLDER,
    }
    values.update(changes)
    return PosterFace(**values)


class StackFaceTests(unittest.TestCase):
    def edges(self, step: int = 4) -> Any:
        layout = episode_layout(FULL, linespace=linespace, stack=True)
        made = stack_edges(
            layout, step / layout.stack_step, fills=(NEAR, FAR), edges=(NEAR_EDGE, FAR_EDGE)
        )
        assert made is not None
        self.assertEqual(made.step, step)
        return made

    def test_layout_makes_room_for_the_pill_and_the_cards_behind(self) -> None:
        for size in CardSize:
            scale = CardScale.of(size)
            with self.subTest(size=size):
                plain = episode_layout(scale, linespace=linespace)
                stacked = episode_layout(scale, linespace=linespace, stack=True)
                self.assertEqual(
                    (plain.stack_step, plain.stack_depth, plain.pill_height), (0, 0, 0)
                )
                self.assertGreaterEqual(stacked.stack_step, 2)
                self.assertEqual(stacked.stack_depth, STACK_BEHIND * stacked.stack_step)
                self.assertGreater(stacked.height, plain.height)
                needed = (
                    stacked.pad_top
                    + 2 * line_height(stacked.title_size)
                    + stacked.code_gap
                    + line_height(stacked.code_size)
                    + stacked.pill_gap
                    + stacked.pill_height
                    + stacked.row_height
                    + stacked.pad_bottom
                )
                self.assertGreaterEqual(stacked.height, needed)
                # The same poster area: the texts of a day's cards start at one x.
                self.assertEqual(stacked.poster_max, plain.poster_max)
                self.assertFalse(stacked.note)
        self.assertIsNone(
            stack_edges(
                episode_layout(FULL, linespace=linespace),
                1.5,
                fills=(NEAR, FAR),
                edges=(NEAR_EDGE, FAR_EDGE),
            )
        )
        with self.assertRaises(ValueError):
            stack_edges(
                episode_layout(FULL, linespace=linespace, stack=True),
                1.5,
                fills=(NEAR,),
                edges=(NEAR_EDGE,),
            )

    def test_poster_piece_with_the_cards_behind_it(self) -> None:
        cover = Image.new("RGB", cover_size((138, 168)), RED)
        image = compose_poster(cover, face(stack=self.edges()))
        self.assertEqual(image.size, (138, 168 + 8))
        pixel = image.getpixel
        self.assertTrue(close(pixel((10, 50)), RED))  # the front card as before
        self.assertTrue(close(pixel((0, 0)), OUTSIDE))
        # Below it at x = 100: the near card (fill, then its edge), then the far card.
        self.assertTrue(close(pixel((100, 168)), NEAR))
        self.assertTrue(close(pixel((100, 170)), NEAR_EDGE))
        self.assertTrue(close(pixel((100, 172)), FAR))
        self.assertTrue(close(pixel((100, 175)), FAR_EDGE))
        self.assertTrue(close(pixel((1, 174)), OUTSIDE))  # left of the cards behind
        plain = compose_poster(cover, face())
        self.assertEqual(plain.size, (138, 168))

    def test_right_cap_with_the_cards_behind_it(self) -> None:
        cap = CapFace(
            size=(15, 168), border=2, radius=12, bg=BG, edge=EDGE, outside=OUTSIDE,
            stack=self.edges(),
        )
        image = compose_cap(cap)
        self.assertEqual(image.size, (15 + 8, 168 + 8))
        pixel = image.getpixel
        self.assertTrue(close(pixel((14, 84)), EDGE))  # the front card's right border
        self.assertTrue(close(pixel((15, 84)), NEAR))
        self.assertTrue(close(pixel((18, 84)), NEAR_EDGE))
        self.assertTrue(close(pixel((19, 84)), FAR))
        self.assertTrue(close(pixel((22, 84)), FAR_EDGE))
        self.assertTrue(close(pixel((20, 1)), OUTSIDE))  # above the cards behind

    def test_pill(self) -> None:
        image = compose_pill(PillFace((60, 20), (42, 58, 77)))
        self.assertEqual((image.mode, image.size), ("RGBA", (60, 20)))
        alpha = image.getchannel("A").getpixel
        self.assertEqual(alpha((0, 0)), 0)  # see-through around the round ends
        self.assertEqual(image.getpixel((30, 10)), (42, 58, 77, 255))
        self.assertGreater(cast(int, alpha((30, 0))), 200)  # the top edge is solid between


# -- the cards ----------------------------------------------------------------------------------


class FakeImages:
    def __init__(self) -> None:
        self.faces: list[PosterFace] = []

    async def get(
        self, url: str | None, size: tuple[int, int], *, finish: Any = None
    ) -> Image.Image | None:
        if isinstance(finish, PosterFace):
            self.faces.append(finish)
        cover = Image.new("RGB", size, RED)
        return cover if finish is None else cast(Image.Image, finish(cover))


class CardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.asked: list[str] = []
        self.batches: list[tuple[BatchKey, Sequence[CalendarEntry]]] = []
        self.actions: DownloadActions[CalendarEntry] = DownloadActions(
            download=lambda entry: self.asked.append(f"download {entry.episode.code}"),
            choose=lambda entry: self.asked.append(f"choose {entry.episode.code}"),
            states=DownloadStates(),
            download_all=lambda key, entries: self.batches.append((key, entries)),
        )

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    def stack(self, *entries: CalendarEntry) -> Stack:
        return Stack(entries or (ep(5, 1, 7, done=True), ep(5, 1, 8), ep(5, 1, 9)))

    def card(self, stack: Stack, images: FakeImages | None = None, **options: Any) -> StackCard:
        return StackCard(
            self.root,
            stack,
            images=cast(ImageCache, images or FakeImages()),
            actions=self.actions,
            **options,
        )

    async def lay_out(self, card: StackCard, width: int = 200) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(card)
        height = round(card.cget("height") * scaling)
        card._on_resize(SimpleNamespace(width=round(width * scaling), height=height))
        if card._rebake_job is not None:
            card._bake()  # as the timer does once the width settles
        assert card._art_task is not None
        await card._art_task

    def text(self, card: StackCard, item: int) -> str:
        canvas: Any = card._art
        return str(canvas.itemcget(item, "text"))

    async def test_wide_stack_card(self) -> None:
        opened: list[Show] = []
        images = FakeImages()
        stack = self.stack()
        card = self.card(stack, images, missing=True, on_open_show=opened.append)
        await self.lay_out(card)
        self.assertEqual(self.text(card, card._code), "S01E07 – E09")
        self.assertEqual(self.text(card, card._pill_text), "3 episodes ▾")
        self.assertEqual(self.text(card, card._note), "")  # the amber edge only
        (used,) = images.faces
        self.assertIsNotNone(used.mark)
        assert used.stack is not None
        self.assertEqual(len(used.stack.fills), STACK_BEHIND)
        # The cards behind it reach past the front card's bottom.
        canvas: Any = card._art
        bottom = card._front_size()[1]
        self.assertEqual(canvas.coords(card._behind[0])[1], bottom)
        self.assertGreater(canvas.coords(card._behind[0])[3], canvas.coords(card._behind[2])[3])
        control = card._control
        assert isinstance(control, StackControl)
        self.assertEqual(control.current_look().text, "⬇ Download all (2)")
        control._click()  # as a click on it does
        ((key, entries),) = self.batches
        self.assertEqual(key, stack.keys)
        self.assertEqual([entry.episode.code for entry in entries], ["S01E08", "S01E09"])
        card._open()  # the poster or the name
        self.assertEqual(opened, [stack.show])

    async def test_pill_unfolds_and_folds_the_rows(self) -> None:
        folds: list[bool] = []
        card = self.card(self.stack(), on_fold=folds.append)
        await self.lay_out(card)
        folded = int(card.cget("height"))
        self.assertEqual(card.rows, [])
        card.toggle()  # as a click on the pill does
        self.assertTrue(card.unfolded)
        self.assertEqual(len(card.rows), 3)
        self.assertGreater(int(card.cget("height")), folded)
        await self.lay_out(card)
        self.assertEqual(self.text(card, card._pill_text), "3 episodes ▴")
        codes = [self.text(card, row.code) for row in card._rows]
        states = [self.text(card, row.state) for row in card._rows]
        self.assertEqual(codes, ["S01E07", "S01E08", "S01E09"])
        self.assertEqual(states, ["✓ downloaded", "still needed", "still needed"])
        canvas: Any = card._art
        self.assertEqual(canvas.itemcget(card._rows[1].state, "fill"), theme.pick(theme.WARNING))
        looks = [row.look.text if row.look else "" for row in card.rows]
        self.assertEqual(looks, ["✓", "⬇", "⬇"])
        # Rows sit under the pill.
        self.assertGreater(canvas.coords(card._rows[0].code)[1], canvas.coords(card._pill)[1])
        # A right-click on a row's buttons: its search window; its ⬇: that episode.
        card.rows[1].choose()
        card.rows[2].main.invoke()
        self.assertEqual(self.asked, ["choose S01E08", "download S01E09"])
        # A run's outcome shows in the row.
        self.actions.states.set(entry_key(card._rows[2].entry), RunState(Phase.DONE))
        self.assertEqual(self.text(card, card._rows[2].state), "✓ downloaded")
        card.toggle()
        self.assertEqual(card.rows, [])
        self.assertEqual(int(card.cget("height")), folded)
        self.assertEqual(folds, [True, False])
        self.assertEqual(canvas.find_withtag("rows"), ())

    async def test_starts_unfolded_when_remembered(self) -> None:
        card = self.card(self.stack(), unfolded=True)
        self.assertEqual(len(card.rows), 3)
        await self.lay_out(card)
        self.assertEqual(self.text(card, card._pill_text), "3 episodes ▴")

    async def test_a_narrow_row_drops_the_more_button_before_cutting_the_code(self) -> None:
        card = self.card(self.stack(), unfolded=True)
        await self.lay_out(card, 260)
        self.assertTrue(card.rows[1].shows_more)
        await self.lay_out(card, 150)  # about the narrowest window
        self.assertFalse(card.rows[1].shows_more)
        self.assertEqual(self.text(card, card._rows[1].code), "S01E08")
        await self.lay_out(card, 260)
        self.assertTrue(card.rows[1].shows_more)

    async def test_download_all_follows_the_batch_and_the_episodes(self) -> None:
        stack = self.stack(ep(6, 2, 1), ep(6, 2, 2))
        card = self.card(stack)
        await self.lay_out(card)
        control = card._control
        assert control is not None
        self.actions.states.set_batch(stack.keys, BatchState(1, 2))
        self.assertEqual(control.current_look().text, "Downloading 1 of 2…")
        self.actions.states.set_batch(stack.keys, None)
        for key in stack.keys:
            self.actions.states.set(key, RunState(Phase.DONE, GrabOutcome.SAVED))
        look = control.current_look()
        self.assertEqual((look.text, look.action), ("✓ Saved", "choose"))
        control._click()  # the ✓ unfolds the rows
        self.assertTrue(card.unfolded)
        card.destroy()
        self.actions.states.set(stack.keys[0], None)  # gone: nothing to repaint

    async def test_nothing_aired_is_a_muted_line(self) -> None:
        card = self.card(self.stack(ep(7, 1, 1, aired=False), ep(7, 1, 2, aired=False)))
        await self.lay_out(card)
        self.assertIsNone(card._control)
        self.assertEqual(self.text(card, card._status), "Not aired")

    async def test_follows_the_card_size(self) -> None:
        small = self.card(self.stack(), scale=CardScale.of(CardSize.XS))
        large = self.card(self.stack())
        self.assertLess(small.cget("height"), large.cget("height"))
        # The texts start where a single card's do, the cards behind it counted in.
        await self.lay_out(large, 200)
        layout = episode_layout(FULL, linespace=linespace)
        self.assertEqual(large._area, poster_area(layout, 200))

    async def test_compact_stack_card(self) -> None:
        opened: list[Show] = []
        stack = self.stack(ep(8, 1, 7), ep(8, 1, 8), ep(8, 1, 9, done=True))
        card = CompactStackCard(
            self.root, stack, actions=self.actions, missing=True, on_open_show=opened.append
        )
        self.assertEqual(card.badge.cget("text"), "3")
        self.assertEqual(card.range_label.cget("text"), "S01E07 – E09")
        assert card.note is not None and card.mark is not None
        assert card.open_show is not None
        card.open_show()
        self.assertEqual(opened, [stack.show])
        assert card.look is not None
        self.assertEqual((card.look.text, card.look.tip), ("⬇", "⬇ Download all (2)"))
        card.main.invoke()  # download all
        ((key, entries),) = self.batches
        self.assertEqual([entry.episode.code for entry in entries], ["S01E07", "S01E08"])
        # A right-click lists the episodes; each opens its search window.
        self.assertEqual(
            [label for label, _ in card.menu_items()],
            ["S01E07 · still needed", "S01E08 · still needed", "S01E09 · ✓ downloaded"],
        )
        with mock.patch("app.ui.calendar_stack.popup_menu") as popup:
            card._right_click()
        ((anchor, items), _kwargs) = popup.call_args
        self.assertIs(anchor, card)
        items[1][1]()
        self.assertEqual(self.asked, ["choose S01E08"])
        # The batch at work, then all there: … and ✓ (which lists the episodes).
        self.actions.states.set_batch(stack.keys, BatchState(2, 2))
        self.assertEqual(card.look.text, "…")
        self.actions.states.set_batch(stack.keys, None)
        for entry in entries:
            self.actions.states.set(entry_key(entry), RunState(Phase.DONE))
        self.assertEqual(card.look.text, "✓")
        with mock.patch("app.ui.calendar_stack.popup_menu") as popup:
            card.main.invoke()
        popup.assert_called_once()
        self.assertNotEqual(card.name_label._label.bind("<Button-3>"), "")


if __name__ == "__main__":
    unittest.main()
