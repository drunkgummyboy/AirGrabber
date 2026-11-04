"""People in the UI (3.0.1): names, avatars and the menu's entries; the band's name button
(its place, its click, the Series side asked for the selected person); the people menu; the
add / rename / remove dialogs; a switch closing the info windows and reloading the pages (and
the pages asking for the selected person); the welcome's "Who uses AirGrabber?" step."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.i18n import Language
from app.models import DownloadState, Profile, ProfileColour, Settings, SpotlightSide
from app.services import SettingsService
from app.services.profiles import ProfileChange, ProfileError
from app.ui import theme
from app.ui.main_window import MainWindow
from app.ui.missing_control import MENU_SEPARATOR, PopupMenu
from app.ui.people import (
    ColourSwatches,
    avatar_image,
    menu_entries,
    person_initial,
    person_name,
    switched_text,
)
from app.ui.people_dialogs import PeopleActions, PersonDialog, RemovePersonDialog
from app.ui.spotlight_band import SpotlightBand
from app.ui.spotlight import (
    BUTTON_SIZE,
    PERSON_NAME_MAX,
    HitKind,
    band_layout,
    controls,
    hit_target,
    person_width,
)
from app.ui.views import CalendarView, TrackedView
from app.ui.welcome_dialog import Page, WelcomeDialog, people_problem
from tests.people import CREATED, FakePeople
from tests.test_calendar_view import FakeLibrary as CalendarLibrary
from tests.test_calendar_view import FakeSettings
from tests.test_spotlight import NOW, FakeImages, FakeSpotlight, FakeStatus, downloading, option

ME = Profile(id=1, created_at=CREATED)
SANNE = Profile(id=2, name="Sanne", colour=ProfileColour.PINK, created_at=CREATED)


class TkCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building windows is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        if PopupMenu._open is not None:
            PopupMenu._open.close()
        self.root.destroy()
        gc.collect()

    async def settle(self, rounds: int = 8) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0.01)
            self.root.update()

    def keep(self, window: Any) -> Any:
        def close() -> None:
            if window.winfo_exists():
                window.destroy()

        self.addCleanup(close)
        return window


# -- the pure parts --------------------------------------------------------------------------------


class NameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, i18n.current_language())

    def test_the_first_person_is_me_until_named(self) -> None:
        i18n.set_language(Language.EN)
        self.assertEqual((person_name(ME), person_initial(ME)), ("Me", "M"))
        self.assertEqual((person_name(SANNE), person_initial(SANNE)), ("Sanne", "S"))
        self.assertEqual(switched_text(SANNE), "Now showing Sanne's list")
        i18n.set_language(Language.NL)
        self.assertEqual((person_name(ME), person_initial(ME)), ("Ik", "I"))
        self.assertEqual(switched_text(SANNE), "Je ziet nu de lijst van Sanne")

    def test_an_avatar_is_a_disc_with_the_letter(self) -> None:
        picture = avatar_image("S", (181, 86, 122), 40)
        self.assertEqual(picture.size, (40, 40))

        def pixel(x: int, y: int) -> tuple[int, ...]:
            value = picture.getpixel((x, y))
            assert isinstance(value, tuple)
            return value

        self.assertEqual(pixel(0, 0)[3], 0)  # see-through corner
        self.assertEqual(pixel(6, 20)[:3], (181, 86, 122))  # the disc
        middle = [pixel(x, 20)[:3] for x in range(14, 27)]
        self.assertIn((255, 255, 255), middle)  # the white letter

    def test_white_reads_on_every_person_colour(self) -> None:
        from tests.test_theme import contrast

        for colour in ProfileColour:
            pair = theme.person_colour(colour)
            for dark in (False, True):
                with self.subTest(colour=colour, dark=dark):
                    ratio = contrast(theme.ON_COLOR, theme.pick(pair, dark=dark))
                    self.assertGreaterEqual(ratio, 4.5)


class MenuEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.calls: list[Any] = []

    def entries(self, people: list[Profile], current: int) -> list[Any]:
        return menu_entries(
            people,
            current,
            on_switch=lambda person_id: self.calls.append(("switch", person_id)),
            on_add=lambda: self.calls.append("add"),
            on_edit=lambda: self.calls.append("edit"),
            on_remove=lambda: self.calls.append("remove"),
            image=lambda person: f"avatar {person.id}",
        )

    def test_everyone_a_line_then_the_actions(self) -> None:
        entries = self.entries([ME, SANNE], 2)
        self.assertEqual(
            [entry.label.strip() for entry in entries],
            ["Me", "Sanne", "", "＋ Add a person…", "✎ Rename or change colour…",
             "🗑 Remove a person…"],
        )  # fmt: skip
        self.assertEqual([entry.checked for entry in entries[:2]], [False, True])
        self.assertEqual([entry.image for entry in entries[:2]], ["avatar 1", "avatar 2"])
        self.assertIs(entries[2], MENU_SEPARATOR)
        for entry in entries:
            if entry.action is not None:
                entry.action()
        self.assertEqual(self.calls, [("switch", 1), ("switch", 2), "add", "edit", "remove"])

    def test_alone_no_remove(self) -> None:
        labels = [entry.label for entry in self.entries([ME], 1)]
        self.assertNotIn("🗑 Remove a person…", labels)
        self.assertIn("＋ Add a person…", labels)  # how a second person comes in

    def test_a_long_name_is_cut_short(self) -> None:
        long = Profile(id=3, name="W" * 30, created_at=CREATED)
        [entry, *_] = menu_entries(
            [long], 3, on_switch=print, on_add=print, on_edit=print, on_remove=print,
            measure=lambda text: 10.0 * len(text),
        )  # fmt: skip
        self.assertTrue(entry.label.endswith("…"))


class BandLayoutTests(unittest.TestCase):
    def test_the_name_button_at_the_right_end(self) -> None:
        width = person_width(60, 10)
        layout = band_layout(1220, word_width=110, person_width=width)
        self.assertEqual((layout.person.h, layout.person.right), (BUTTON_SIZE, 1200))
        self.assertLess(layout.settings.right, layout.person.x)
        self.assertEqual(hit_target(layout, layout.person.x + 5, layout.person.y + 5, items=0),
                         (HitKind.PERSON, 0))  # fmt: skip
        self.assertEqual(layout.avatar.x, layout.person.x + 5)

    def test_nothing_overlaps_with_the_longest_name_at_the_narrowest_window(self) -> None:
        widest = person_width(1000, 10)  # cut to PERSON_NAME_MAX
        self.assertEqual(widest, person_width(PERSON_NAME_MAX, 10))
        for width in range(1220, 2600, 37):
            for history in (False, True):  # 🕘 on MediaForge's page
                parts = controls(
                    band_layout(width, word_width=110, person_width=widest, history=history)
                )
                for index, part in enumerate(parts):
                    for other in parts[index + 1 :]:
                        self.assertFalse(part.overlaps(other), (width, part, other))

    def test_no_person_no_button(self) -> None:
        layout = band_layout(1220, word_width=110)
        self.assertEqual(layout.person.w, 0)
        self.assertEqual(layout.settings.right, 1200)
        self.assertEqual(len(controls(layout)), 4)


class WelcomeNamesTests(unittest.TestCase):
    def test_names(self) -> None:
        self.assertIsNone(people_problem(["", "", ""]))  # empty: "Me", the others skipped
        self.assertIsNone(people_problem(["Beau", "Sanne", ""]))
        self.assertEqual(people_problem(["Beau", "beau"]), "Someone is called Beau already")
        self.assertEqual(people_problem(["", "x" * 31]), "Use at most 30 characters")


# -- the band's name button ----------------------------------------------------------------------


class NameButtonTests(TkCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.status = FakeStatus()
        self.status.latest = downloading(DownloadState.DOWNLOADING)
        self.service = FakeSpotlight()
        self.events: list[Any] = []
        self.person = 1  # the selected person the band reads

    def band(self) -> SpotlightBand:
        band = SpotlightBand(
            self.root,
            spotlight=self.service,  # type: ignore[arg-type]
            downloads=self.status,  # type: ignore[arg-type]
            images=FakeImages(),  # type: ignore[arg-type]
            on_downloads=lambda: None,
            on_history=lambda: None,
            on_settings=lambda: None,
            on_search=lambda: None,
            on_open=lambda item: None,
            report=lambda text, ms: None,
            profile=lambda: self.person,
            on_person=lambda: self.events.append("person"),
            clock=lambda: NOW,
        )
        band.pack(fill="x")
        scale = float(ctk.ScalingTracker.get_widget_scaling(band))
        width = round(1220 * scale)
        self.root.geometry(f"{width}x{round(300 * scale)}")
        band._on_configure(SimpleNamespace(width=width))
        return band

    def text(self, band: SpotlightBand, item: int) -> str:
        return option(band, item, "text")

    async def test_the_name_button_shows_the_person_and_opens_the_menu(self) -> None:
        band = self.band()
        self.assertEqual(band.layout.person.w if band.layout else -1, 0)  # nobody yet
        band.set_person("Sanne", "S", theme.PERSON_PINK)
        layout = band.layout
        assert layout is not None
        self.assertGreater(layout.person.w, 0)
        self.assertEqual(self.text(band, band._person_name), "Sanne")
        self.assertEqual(self.text(band, band._person_arrow), "▾")
        rect = layout.person
        event = SimpleNamespace(
            x=band._px(rect.x + rect.w / 2), y=band._px(rect.y + rect.h / 2)
        )
        band._on_press(event)
        band._on_release(event)
        self.assertEqual(self.events, ["person"])
        box = band.person_box()
        assert box is not None
        self.assertEqual(box[2:], (band._px(rect.w), band._px(rect.h)))

    async def test_the_series_side_is_the_selected_persons(self) -> None:
        band = self.band()
        band.start()
        await self.settle(40)
        self.assertEqual(set(self.service.people), {1})
        self.person = 2
        band.reload()
        await self.settle(100)
        self.assertEqual(self.service.people[-1], 2)
        self.assertIn(SpotlightSide.SERIES, band._spotlights)


# -- the menu and the dialogs ----------------------------------------------------------------------


class PeopleMenuTests(TkCase):
    async def test_the_menu_under_the_button(self) -> None:
        people = FakePeople(ME, SANNE)
        actions = PeopleActions(self.root, cast(Any, people))
        anchor = ctk.CTkFrame(self.root, width=200, height=40)
        anchor.pack()
        self.root.deiconify()
        self.root.update()
        menu = actions.open_menu(anchor, (anchor.winfo_rootx(), anchor.winfo_rooty(), 200, 40))
        assert menu is not None
        self.assertEqual([label.strip() for label in menu.labels[:2]], ["Me", "Sanne"])
        self.assertEqual(len(menu.checks), 1)  # the ✓ on the selected one
        self.assertEqual(len(menu.buttons), 5)
        menu.buttons[1].invoke()  # Sanne
        await self.settle()
        self.assertEqual(people.calls, [("switch", 2)])
        self.assertFalse(menu.is_open)

    async def test_add_switches_to_the_new_person(self) -> None:
        people = FakePeople(ME)
        actions = PeopleActions(self.root, cast(Any, people))
        actions.add()
        dialog = self.keep(actions.dialog)
        assert isinstance(dialog, PersonDialog)
        dialog.name.insert(0, "  Sanne ")
        dialog.swatches.select(ProfileColour.GREEN)
        dialog.save()
        await self.settle()
        self.assertEqual(people.calls, [("add", "Sanne"), ("switch", 2)])
        self.assertEqual(people.people[1].colour, ProfileColour.GREEN)
        self.assertFalse(dialog.winfo_exists())


class PersonDialogTests(TkCase):
    def dialog(self, people: list[Profile], person: Profile | None = None) -> PersonDialog:
        self.saved: list[tuple[str, ProfileColour]] = []

        async def save(name: str, colour: ProfileColour) -> None:
            if name == "Boom":
                raise ProfileError("That person isn't there any more")
            self.saved.append((name, colour))

        dialog = PersonDialog(self.root, people=people, on_save=save, person=person)
        self.keep(dialog)
        return dialog

    async def test_add_a_person(self) -> None:
        dialog = self.dialog([ME, SANNE])
        texts = [str(w.cget("text")) for w in _descendants(dialog) if isinstance(w, ctk.CTkLabel)]
        self.assertIn("Add a person", texts)
        self.assertIn("They start with an empty list. Downloads and settings are shared.", texts)
        self.assertEqual(dialog.save_button.cget("text"), "Add")
        self.assertIs(dialog.swatches.selected, ProfileColour.GREEN)  # the first one free
        self.assertEqual(len(dialog.swatches.buttons), 5)
        dialog.save()
        self.assertEqual(dialog.problem.cget("text"), "Type a name")
        dialog.name.insert(0, "x" * 31)
        dialog.save()
        self.assertEqual(dialog.problem.cget("text"), "Use at most 30 characters")
        dialog.name.delete(0, "end")
        dialog.name.insert(0, "sanne")
        dialog.save()
        self.assertEqual(dialog.problem.cget("text"), "Someone is called Sanne already")
        self.assertEqual(self.saved, [])
        dialog.name.delete(0, "end")
        dialog.name.insert(0, "Oma")
        dialog.swatches.buttons[ProfileColour.COPPER].invoke()
        dialog.save()
        await self.settle()
        self.assertEqual(self.saved, [("Oma", ProfileColour.COPPER)])
        self.assertFalse(dialog.winfo_exists())

    async def test_rename_or_recolour_the_selected_person(self) -> None:
        dialog = self.dialog([ME, SANNE], SANNE)
        self.assertEqual(dialog.name.get(), "Sanne")
        self.assertIs(dialog.swatches.selected, ProfileColour.PINK)
        self.assertEqual(dialog.save_button.cget("text"), "Save")
        dialog.name.delete(0, "end")
        dialog.name.insert(0, "SANNE")  # her own name in other letters
        dialog.save()
        await self.settle()
        self.assertEqual(self.saved, [("SANNE", ProfileColour.PINK)])

    async def test_me_may_change_only_the_colour(self) -> None:
        dialog = self.dialog([ME, SANNE], ME)
        self.assertEqual(dialog.name.get(), "")
        self.assertEqual(dialog.name.cget("placeholder_text"), "Me")
        dialog.swatches.select(ProfileColour.GREEN)
        dialog.save()
        await self.settle()
        self.assertEqual(self.saved, [("", ProfileColour.GREEN)])

    async def test_a_refusal_shows_and_the_dialog_stays(self) -> None:
        dialog = self.dialog([ME])
        dialog.name.insert(0, "Boom")
        dialog.save()
        await self.settle()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.problem.cget("text"), "That person isn't there any more")
        self.assertFalse(dialog.busy)


class RemoveDialogTests(TkCase):
    async def test_choose_who_then_confirm(self) -> None:
        removed: list[int] = []

        async def remove(person_id: int) -> None:
            removed.append(person_id)

        dialog = self.keep(RemovePersonDialog(self.root, people=[ME, SANNE], on_remove=remove))
        self.assertEqual(sorted(dialog.rows), [1, 2])
        dialog.rows[2].invoke()
        self.assertIs(dialog.chosen, SANNE)
        self.assertEqual(
            dialog.question.cget("text"),
            "Remove Sanne? Their followed series and watchlist are removed. Downloads stay.",
        )
        dialog.remove_button.invoke()
        await self.settle()
        self.assertEqual(removed, [2])
        self.assertFalse(dialog.winfo_exists())

    async def test_the_last_person_is_refused(self) -> None:
        async def remove(person_id: int) -> None:
            raise ProfileError("The last person can't be removed")

        dialog = self.keep(RemovePersonDialog(self.root, people=[ME], on_remove=remove))
        dialog.rows[1].invoke()
        dialog.remove_button.invoke()
        await self.settle()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.problem.cget("text"), "The last person can't be removed")


class SwatchTests(TkCase):
    async def test_one_is_chosen(self) -> None:
        chosen: list[ProfileColour] = []
        swatches = ColourSwatches(self.root, selected=ProfileColour.BLUE, on_change=chosen.append)
        swatches.pack()
        swatches.buttons[ProfileColour.PURPLE].invoke()
        self.assertIs(swatches.selected, ProfileColour.PURPLE)
        self.assertEqual(chosen, [ProfileColour.PURPLE])
        rings = swatches._rings
        self.assertEqual(rings[ProfileColour.PURPLE].cget("border_width"), 2)
        self.assertEqual(rings[ProfileColour.BLUE].cget("border_width"), 0)


# -- a switch --------------------------------------------------------------------------------------


class SwitchTests(unittest.IsolatedAsyncioTestCase):
    """MainWindow._on_profiles_changed on a stand-in window."""

    def window(self) -> Any:
        calls: list[Any] = []

        async def summary() -> None:
            calls.append("summary")

        results = SimpleNamespace(
            winfo_exists=lambda: True, destroy=lambda: calls.append("results")
        )
        return SimpleNamespace(
            calls=calls,
            _services=SimpleNamespace(profiles=SimpleNamespace(current=SANNE)),
            _show_person=lambda: calls.append("button"),
            _show_windows=SimpleNamespace(close_windows=lambda: calls.append("show windows")),
            _movie_windows=SimpleNamespace(close_windows=lambda: calls.append("film windows")),
            _results_window=results,
            _suggestions=SimpleNamespace(hide=lambda: calls.append("suggestions")),
            _reload_pages=lambda: calls.append("pages"),
            _show_library_summary=summary,
            _report=lambda text, ms: calls.append(("status", text)),
        )

    async def test_a_switch_closes_the_info_windows_and_reloads_the_pages(self) -> None:
        window = self.window()
        MainWindow._on_profiles_changed(window, ProfileChange.SWITCHED)
        await asyncio.sleep(0)
        self.assertEqual(
            window.calls,
            [
                "button",
                "show windows",
                "film windows",
                "results",
                "suggestions",
                "pages",
                ("status", "Now showing Sanne's list"),
                "summary",
            ],
        )
        self.assertIsNone(window._results_window)

    async def test_a_new_name_only_repaints_the_button(self) -> None:
        window = self.window()
        MainWindow._on_profiles_changed(window, ProfileChange.PEOPLE)
        self.assertEqual(window.calls, ["button"])


class PagesAskForTheSelectedPersonTests(TkCase):
    async def test_the_calendar_and_my_shows_load_the_selected_persons(self) -> None:
        settings = FakeSettings(Settings(current_profile=2, show_suggestions=False))
        library = CalendarLibrary([], [])
        calendar = CalendarView(
            self.root,
            library=cast(Any, library),
            settings=cast(SettingsService, settings),
            images=cast(Any, mock.MagicMock()),
            status=lambda text, clear_after_ms=None: None,
            episode_actions=cast(Any, mock.MagicMock()),
        )
        calendar.refresh()
        await self.settle(20)
        self.assertEqual(set(library.people), {2})
        await settings.update(current_profile=1)  # another person: the page loads theirs
        calendar.refresh()
        await self.settle(20)
        self.assertEqual(library.people[-1], 1)

        from tests.test_tracked_view import _Library

        shows = _Library([], {})
        tracked = TrackedView(
            self.root,
            library=cast(Any, shows),
            settings=cast(SettingsService, FakeSettings(Settings(current_profile=2))),
            airdates=cast(Any, mock.MagicMock()),
            images=cast(Any, mock.MagicMock()),
            status=lambda text, clear_after_ms=None: None,
        )
        tracked.refresh()
        await self.settle(20)
        self.assertEqual(shows.people, [2])
        tracked._remove_ended()
        await self.settle(10)
        self.assertEqual(shows.removed_for, [2])  # "Cleanup Ended" for the person on the page


# -- the welcome's "Who uses AirGrabber?" ----------------------------------------------------------


class _Repository:
    async def save(self, settings: Settings) -> None:
        pass


class WelcomePeopleTests(TkCase):
    def welcome(self, people: FakePeople) -> WelcomeDialog:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        settings = SettingsService(
            _Repository(),  # type: ignore[arg-type]
            Settings(download_dir=Path(tmp.name)),
        )
        dialog = WelcomeDialog(
            self.root,
            settings=settings,
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            airdates=mock.MagicMock(),
            library=mock.MagicMock(),
            profiles=people,
        )
        dialog.attributes("-alpha", 0.0)
        return cast(WelcomeDialog, self.keep(dialog))

    async def next(self, dialog: WelcomeDialog) -> None:
        dialog._on_right()
        await self.settle()

    async def test_no_one_else_and_an_empty_name_stays_me(self) -> None:
        people = FakePeople()
        dialog = self.welcome(people)
        dialog._show_page(Page.PEOPLE)
        self.assertEqual(len(dialog.person_rows), 1)
        self.assertEqual(dialog.person_rows[0].entry.cget("placeholder_text"), "Me")
        await self.next(dialog)
        self.assertEqual(people.calls, [("update", 1)])
        self.assertIsNone(people.people[0].name)
        self.assertIs(dialog._page, Page.AIRDATES)

    async def test_two_more_people(self) -> None:
        people = FakePeople()
        dialog = self.welcome(people)
        dialog._show_page(Page.PEOPLE)
        dialog.person_rows[0].entry.insert(0, "Beau")
        first = dialog.add_person_row()
        second = dialog.add_person_row()
        assert first is not None and second is not None
        self.assertIs(first.colour, ProfileColour.PINK)  # the next free swatches
        self.assertIs(second.colour, ProfileColour.GREEN)
        first.entry.insert(0, "Sanne")
        second.entry.insert(0, "Kid")
        await self.next(dialog)
        self.assertEqual(
            [(p.id, p.name, p.colour) for p in people.people],
            [(1, "Beau", ProfileColour.BLUE), (2, "Sanne", ProfileColour.PINK),
             (3, "Kid", ProfileColour.GREEN)],
        )  # fmt: skip
        self.assertEqual(people.current_id, 1)  # the first person stays selected
        # Back and Next again: changed, not added twice; a name taken out: that person goes.
        dialog._show_page(Page.PEOPLE)
        second.entry.delete(0, "end")
        await self.next(dialog)
        self.assertEqual([p.name for p in people.people], ["Beau", "Sanne"])

    async def test_at_most_four_more_and_a_clash_keeps_the_step(self) -> None:
        dialog = self.welcome(FakePeople())
        dialog._show_page(Page.PEOPLE)
        rows = [dialog.add_person_row() for _ in range(5)]
        self.assertIsNone(rows[-1])
        self.assertEqual(len(dialog.person_rows), 5)
        self.assertFalse(dialog.add_person_link.winfo_ismapped())
        first = rows[0]
        assert first is not None
        dialog.person_rows[0].entry.insert(0, "Beau")
        first.entry.insert(0, "BEAU")
        await self.next(dialog)
        self.assertEqual(dialog.people_problem.cget("text"), "Someone is called Beau already")
        self.assertIs(dialog._page, Page.PEOPLE)


def _descendants(widget: Any) -> list[Any]:
    found: list[Any] = []
    stack = list(widget.winfo_children())
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(child.winfo_children())
    return found

