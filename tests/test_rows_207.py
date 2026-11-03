"""2.0.7's show and film window controls: ⬇ Auto / ☰ Choose… (app/ui/row_actions.py; since
3.1.0 a title downloaded before shows the same, no ✓ Downloaded / ↻ Again), the subtitles'
CC ▾ (app/ui/subtitle_control.py), and the info window's size (app/ui/title_info.py)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

import customtkinter as ctk

from app.models import (
    CalendarEntry,
    Episode,
    GrabOutcome,
    MediaType,
    Show,
    SubtitleFetch,
    SubtitleOffer,
    SubtitleFetchOutcome,
)
from app.ui import theme
from app.ui.download_control import (
    Base,
    DownloadActions,
    DownloadStates,
    Phase,
    RunState,
    episode_key,
)
from app.ui.missing_control import PopupMenu
from app.ui.row_actions import (
    MIN_WIDTH,
    NOTHING_TIP,
    RowActions,
    again_items,
    button_width,
    row_look,
    slot_width,
    status_tip,
)
from app.ui.subtitle_control import (
    WORKING,
    CCControl,
    RunPhase,
    SubtitleActions,
    SubtitleRun,
    SubtitleRuns,
    cc_menu,
    cc_text,
    fetch_run,
    language_name,
)
from app.ui.title_info import MIN_HEIGHT, MIN_WIDTH as WINDOW_MIN_WIDTH, window_size

SHOW = Show(id=7, name="Severance")
EPISODE = Episode(
    id=74, show_id=7, season=2, number=4, airstamp=datetime(2026, 10, 1, tzinfo=UTC)
)
ENTRY = CalendarEntry(show=SHOW, episode=EPISODE, is_tracked=True)
KEY = episode_key(7, 2, 4)


class RowLookTests(unittest.TestCase):
    def test_idle_is_auto_and_choose(self) -> None:
        look = row_look(Base("download"), None)
        self.assertEqual((look.kind, look.buttons, look.status), ("download", ("auto", "choose"), ()))

    def test_a_special_only_chooses(self) -> None:
        self.assertEqual(row_look(Base("search"), None).buttons, ("choose",))

    def test_downloaded_before_looks_like_any_other(self) -> None:
        # Found in the download history meanwhile: no "✓ Downloaded", no "↻ Again" (3.1.0).
        look = row_look(Base("download"), RunState(Phase.DONE))
        self.assertEqual((look.kind, look.buttons, look.status), ("download", ("auto", "choose"), ()))

    def test_working_wins(self) -> None:
        look = row_look(Base("download"), RunState(Phase.WORKING))
        self.assertEqual((look.kind, look.status, look.buttons), ("working", ("Finding the best one…", "Finding…"), ()))

    def test_a_grab_shows_its_outcome_beside_choose(self) -> None:
        look = row_look(Base("download"), RunState(Phase.DONE, GrabOutcome.SENT))
        self.assertEqual((look.kind, look.status, look.buttons), ("done", ("✓ Sent",), ("choose",)))
        self.assertEqual(look.color, theme.SUCCESS)

    def test_nothing_found_is_amber_with_choose(self) -> None:
        look = row_look(Base("download"), RunState(Phase.NOTHING))
        self.assertEqual(look.kind, "nothing")
        self.assertEqual(look.status, ("No good release found", "None found"))
        self.assertEqual((look.buttons, look.alert, look.color), (("choose",), True, theme.PENDING))

    def test_not_released_is_a_plain_text(self) -> None:
        look = row_look(Base("unavailable", "Digital 15 Oct"), None)
        self.assertEqual((look.kind, look.status, look.buttons), ("unavailable", ("Digital 15 Oct",), ()))

    def test_tips(self) -> None:
        done = RunState(Phase.DONE, GrabOutcome.SAVED, quality="1080p", size_bytes=3 * 1024**3)
        self.assertEqual(status_tip(row_look(Base("download"), done), done), "Picked 1080p · 3 GB")
        nothing = RunState(Phase.NOTHING)
        self.assertEqual(status_tip(row_look(Base("download"), nothing), nothing), NOTHING_TIP)
        self.assertEqual(status_tip(row_look(Base("download"), None), None), "")

    def test_the_downloads_pages_again_menu(self) -> None:
        calls: list[str] = []
        items = again_items(lambda: calls.append("auto"), lambda: calls.append("choose"))
        self.assertEqual([label for label, _ in items], ["⬇ Download again automatically", "☰ Choose a release…"])
        for _label, action in items:
            action()
        self.assertEqual(calls, ["auto", "choose"])
        self.assertEqual([label for label, _ in again_items(None, lambda: None)], ["☰ Choose a release…"])

    def test_widths(self) -> None:
        def measure(text: str) -> float:
            return 8.0 * len(text)

        self.assertEqual(button_width("⬇ Auto", measure), 8 * 6 + 24)
        pair = button_width("⬇ Auto", measure) + 6 + button_width("☰ Choose…", measure)
        expected = max(
            MIN_WIDTH,
            pair,
            8 * len("Finding the best one…"),
            8 * len("✓ Opened") + 16 + 6 + button_width("☰ Choose…", measure),
            8 * len("None found") + 16 + 6 + button_width("☰ Choose…", measure),
        )
        self.assertEqual(slot_width(measure), expected + 2)
        self.assertEqual(slot_width(lambda text: 1.0), MIN_WIDTH + 2)


class SubtitleTextTests(unittest.TestCase):
    def test_menu(self) -> None:
        asked: list[tuple[str, ...]] = []
        items = cc_menu(("en", "nl"), asked.append)
        self.assertEqual(
            [label for label, _ in items],
            ["Get English subtitles", "Get Dutch subtitles", "Get all my languages"],
        )
        for _label, action in items:
            action()
        self.assertEqual(asked, [("en",), ("nl",), ("en", "nl")])
        self.assertEqual([label for label, _ in cc_menu(("pt-BR",), asked.append)], ["Get Portuguese (Brazil) subtitles"])
        self.assertEqual(language_name("xx"), "XX")

    def test_button_text(self) -> None:
        self.assertEqual(cc_text("CC EN NL"), "CC EN NL ▾")
        self.assertEqual(cc_text(""), "CC ▾")

    def test_outcomes_in_plain_words(self) -> None:
        def run(outcome: SubtitleFetchOutcome, **fields: Any) -> SubtitleRun | None:
            return fetch_run(SubtitleFetch(outcome=outcome, **fields))

        self.assertIsNone(run(SubtitleFetchOutcome.SAVED, saved=("en",)))  # the mark says it
        self.assertEqual(run(SubtitleFetchOutcome.ALREADY).text, "Already there")  # type: ignore[union-attr]
        partly = run(SubtitleFetchOutcome.PARTLY, saved=("en",), not_found=("nl", "de"))
        self.assertEqual(partly.text, "Not found yet: NL DE")  # type: ignore[union-attr]
        self.assertEqual(run(SubtitleFetchOutcome.NOT_FOUND).text, "Not found yet")  # type: ignore[union-attr]
        no_video = run(SubtitleFetchOutcome.NO_VIDEO)
        self.assertEqual((no_video.text, no_video.warn), ("Video not in the folder", True))  # type: ignore[union-attr]
        self.assertEqual(run(SubtitleFetchOutcome.DOWNLOADING).text, "Still downloading")  # type: ignore[union-attr]
        quota = run(SubtitleFetchOutcome.QUOTA, message="Today's subtitle downloads are used up")
        self.assertEqual((quota.text, quota.tip), ("Today's limit reached", "Today's subtitle downloads are used up"))  # type: ignore[union-attr]
        problem = run(SubtitleFetchOutcome.PROBLEM, message="OpenSubtitles can't be reached")
        self.assertEqual((problem.text, problem.tip), ("Couldn't get subtitles", "OpenSubtitles can't be reached"))  # type: ignore[union-attr]
        self.assertEqual(run(SubtitleFetchOutcome.UNAVAILABLE).text, "Choose the downloads folder first")  # type: ignore[union-attr]
        self.assertEqual(run(SubtitleFetchOutcome.NO_GRAB).text, "No download found")  # type: ignore[union-attr]
        self.assertEqual(run(SubtitleFetchOutcome.WAIT).text, "Tried recently")  # type: ignore[union-attr]

    def test_runs_expire_only_when_unchanged(self) -> None:
        runs = SubtitleRuns()
        first = SubtitleRun(RunPhase.DONE, "Not found yet")
        runs.set(KEY, first)
        runs.set(KEY, WORKING)
        runs.expire(KEY, first)
        self.assertIs(runs.get(KEY), WORKING)
        self.assertTrue(runs.is_working(KEY))
        runs.set(KEY, first)
        runs.expire(KEY, first)
        self.assertIsNone(runs.get(KEY))


class WindowSizeTests(unittest.TestCase):
    def test_tall_and_up_to_1400_wide(self) -> None:
        self.assertEqual(window_size(2560 / 1.5, 1392 / 1.5), (1400, round(1392 / 1.5 * 0.85)))
        self.assertEqual(window_size(1280, 680), (1200, 578))  # 1920 × 1080 at 150 %

    def test_never_larger_than_the_screen_and_the_minimum_where_it_fits(self) -> None:
        self.assertEqual(window_size(910, 470), (830, 400))  # 1366 × 768 at 150 %
        self.assertEqual(window_size(600, 400), (WINDOW_MIN_WIDTH, MIN_HEIGHT))
        self.assertEqual(window_size(500, 300), (500, 300))


class WidgetTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.geometry("900x200+0+0")
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        if PopupMenu._open is not None:
            PopupMenu._open.close()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    async def settle(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0.01)
            self.root.update()


class RowActionsWidgetTests(WidgetTestCase):
    async def test_buttons_follow_the_run(self) -> None:
        calls: list[str] = []
        states = DownloadStates()
        actions = DownloadActions[CalendarEntry](
            download=lambda item: calls.append("auto"),
            choose=lambda item: calls.append("choose"),
            states=states,
        )
        row = RowActions(self.root, item=ENTRY, key=KEY, base=Base("download"), actions=actions)
        row.pack(fill="x")
        await self.settle()
        self.assertTrue(row.auto.winfo_ismapped() and row.choose.winfo_ismapped())
        self.assertFalse(hasattr(row, "again"))  # no ↻ Again (3.1.0)
        row.auto.invoke()
        row.choose.invoke()
        self.assertEqual(calls, ["auto", "choose"])
        states.set(KEY, RunState(Phase.WORKING))
        await self.settle()
        self.assertEqual(row.look.kind, "working")  # type: ignore[union-attr]
        self.assertFalse(row.auto.winfo_ismapped())
        states.set(KEY, RunState(Phase.DONE, GrabOutcome.SENT))
        await self.settle()
        self.assertEqual(str(row.status.cget("text")), "✓ Sent")
        self.assertFalse(row.auto.winfo_ismapped())
        self.assertTrue(row.choose.winfo_ismapped())
        row.status._label.event_generate("<Button-1>")  # the outcome opens nothing
        self.assertIsNone(PopupMenu._open)
        row.choose.invoke()
        self.assertEqual(calls, ["auto", "choose", "choose"])
        row.destroy()
        states.set(KEY, None)  # a gone row is no longer painted

    async def test_a_film_window_row_sits_left_and_is_as_wide_as_its_content(self) -> None:
        actions = DownloadActions[CalendarEntry](
            download=lambda item: None, choose=lambda item: None, states=DownloadStates()
        )
        row = RowActions(
            self.root, item=ENTRY, key=KEY, base=Base("download"), actions=actions, align="left"
        )
        row.pack(side="left")
        await self.settle()
        self.assertLess(row.auto.winfo_x(), row.choose.winfo_x())
        self.assertLessEqual(row.winfo_width(), row.auto.winfo_width() + row.choose.winfo_width() + 30)
        row._actions.states.set(KEY, RunState(Phase.DONE, GrabOutcome.SAVED))
        await self.settle()
        self.assertEqual(str(row.status.cget("text")), "✓ Saved")  # never squeezed out
        self.assertGreater(row.status.winfo_width(), 30)
        row._actions.states.set(KEY, None)


class CCControlWidgetTests(WidgetTestCase):
    async def test_button_menu_and_the_requests_state(self) -> None:
        asked: list[tuple[Any, tuple[str, ...]]] = []
        offer = [SubtitleOffer.ON]
        runs = SubtitleRuns()
        actions = SubtitleActions(
            offer=lambda: offer[0],
            languages=lambda: ("en", "nl"),
            fetch=lambda key, languages: asked.append((key, languages)),
            runs=runs,
        )
        changes: list[int] = []
        cc = CCControl(self.root, key=KEY, actions=actions, on_change=lambda: changes.append(1))
        cc.pack()
        cc.show("CC EN", shown=True)
        await self.settle()
        self.assertTrue(cc.has_content)
        self.assertEqual(str(cc.button.cget("text")), "CC EN ▾")
        cc.open_menu()
        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(menu.labels, ("Get English subtitles", "Get Dutch subtitles", "Get all my languages"))
        menu.buttons[1].invoke()
        self.assertEqual(asked, [(KEY, ("nl",))])
        runs.set(KEY, WORKING)
        await self.settle()
        self.assertEqual(str(cc.state_label.cget("text")), "Looking for subtitles…")
        self.assertEqual(str(cc.button.cget("state")), "disabled")
        cc.open_menu()  # not while it works
        self.assertIsNone(PopupMenu._open)
        runs.set(KEY, SubtitleRun(RunPhase.DONE, "Video not in the folder", warn=True))
        await self.settle()
        self.assertEqual(str(cc.state_label.cget("text")), "Video not in the folder")
        # Without a folder (or a key): only the plain mark.
        runs.set(KEY, None)
        offer[0] = SubtitleOffer.NONE
        cc.show("CC EN")
        await self.settle()
        self.assertFalse(cc.button.winfo_ismapped())
        self.assertEqual(str(cc.mark_label.cget("text")), "CC EN")
        cc.show("", shown=True)
        self.assertFalse(cc.has_content)
        self.assertTrue(changes)

    async def test_greyed_while_subtitles_are_off(self) -> None:
        offer = [SubtitleOffer.OFF]
        asked: list[Any] = []
        runs = SubtitleRuns()
        actions = SubtitleActions(
            offer=lambda: offer[0],
            languages=lambda: ("en",),
            fetch=lambda key, languages: asked.append(languages),
            runs=runs,
        )
        cc = CCControl(self.root, key=KEY, actions=actions)
        cc.pack()
        cc.show("CC EN", shown=True)
        await self.settle()
        self.assertTrue(cc.button.winfo_ismapped())
        self.assertEqual(str(cc.button.cget("state")), "disabled")
        self.assertEqual(cc._button_tip.text, "Turn on subtitles in Settings → Subtitles")
        cc.open_menu()
        self.assertIsNone(PopupMenu._open)
        self.assertEqual(asked, [])
        offer[0] = SubtitleOffer.ON  # turned on in Settings: every button follows
        runs.refresh()
        self.assertEqual(str(cc.button.cget("state")), "normal")
        self.assertEqual(cc._button_tip.text, "Get subtitles")

    async def test_a_row_that_isnt_downloaded_shows_nothing(self) -> None:
        cc = CCControl(self.root, key=KEY, actions=None)
        cc.show("CC EN", shown=False)
        self.assertFalse(cc.has_content)
        cc.show("CC EN", shown=True)
        self.assertTrue(cc.has_content)  # the plain mark
        self.assertFalse(cc.offered)


class ShowWindowRowTests(WidgetTestCase):
    """The show window's episode row: the controls by state, a grabbed episode's subtitles."""

    async def test_row_states(self) -> None:
        from app.ui.show_details import _EpisodeRow

        calls: list[str] = []
        actions = DownloadActions[CalendarEntry](
            download=lambda item: calls.append("auto"),
            choose=lambda item: calls.append("choose"),
            states=DownloadStates(),
        )
        now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        subtitles = SubtitleActions(
            offer=lambda: SubtitleOffer.ON, languages=lambda: ("en",), fetch=lambda k, l: None, runs=SubtitleRuns()
        )
        row = _EpisodeRow(self.root, ENTRY, state="downloadable", now=now, actions=actions, subtitles=subtitles)
        row.pack(fill="x")
        await self.settle()
        assert row.row_actions is not None
        self.assertEqual(row.row_actions.look.kind, "download")  # type: ignore[union-attr]
        self.assertFalse(row.cc_control.has_content)
        # Grabbed before: the same controls (no ✓ Downloaded), and its subtitles.
        row.set_state(ENTRY, "downloadable", now, grabbed=True, cc="CC EN")
        await self.settle()
        self.assertEqual(row.row_actions.look.kind, "download")  # type: ignore[union-attr]
        self.assertEqual(row.cc, "CC EN")
        self.assertTrue(row.cc_control.winfo_manager())
        self.assertIsNone(PopupMenu._open)
        later = EPISODE.model_copy(update={"airstamp": now + timedelta(days=3), "air_date": (now + timedelta(days=3)).date()})
        row.set_state(ENTRY.model_copy(update={"episode": later}), "upcoming", now)
        await self.settle()
        self.assertIsNone(row.row_actions)
        self.assertEqual(str(row._upcoming.cget("text")), "airs in 3 days")  # type: ignore[union-attr]
        self.assertEqual(str(row._date.cget("text")), "21 Oct")
        self.assertFalse(row.cc_control.has_content)


if __name__ == "__main__":
    unittest.main()


class CalendarNewTests(unittest.TestCase):
    """The calendar's quiet blue "New" for an episode aired less than a day ago."""

    def test_stack_row_state_and_colour(self) -> None:
        from app.ui.calendar_stack import needed_entries, row_color, row_state, row_text

        aired = CalendarEntry(show=SHOW, episode=EPISODE, is_tracked=True)
        self.assertEqual(row_state(aired, None, frozenset(), marks=True), "needed")
        new = frozenset({EPISODE.id})
        self.assertEqual(row_state(aired, None, frozenset(), new, marks=True), "new")
        self.assertEqual(row_state(aired, None, new, new, marks=True), "plex")
        self.assertEqual(row_state(aired, None, frozenset(), new), "quiet")  # without Plex
        self.assertEqual(row_text("new"), "new")
        self.assertEqual(row_color("new"), theme.LINK)
        self.assertEqual(row_color("needed"), theme.WARNING)
        self.assertEqual(row_color("plex"), theme.TEXT_MUTED)
        # "Download all" still fetches a new one: the user asks for it.
        self.assertEqual(needed_entries([aired], lambda key: None, frozenset()), (aired,))

    def test_note(self) -> None:
        from app.ui.calendar_cards import compact_height, new_note

        self.assertEqual(new_note(500, lambda text: 7.0 * len(text)), "New")
        self.assertGreater(compact_height(True), compact_height(False))


class CalendarCardWidgetTests(WidgetTestCase):
    async def test_a_new_episode_gets_the_blue_note_not_the_amber_one(self) -> None:
        from app.ui.calendar_cards import CompactEpisodeCard

        actions = DownloadActions[CalendarEntry](
            download=lambda item: None, choose=lambda item: None, states=DownloadStates()
        )
        new = CompactEpisodeCard(self.root, ENTRY, actions=actions, new=True)
        new.pack()
        missing = CompactEpisodeCard(self.root, ENTRY, actions=actions, missing=True, new=True)
        missing.pack()
        await self.settle()
        assert new.note is not None and missing.note is not None
        self.assertEqual(str(new.note.cget("text")), "New")
        self.assertEqual(new.note.cget("text_color"), theme.LINK)
        self.assertEqual(str(missing.note.cget("text")), "Not downloaded yet")  # missing wins
        self.assertEqual(missing.note.cget("text_color"), theme.WARNING)
