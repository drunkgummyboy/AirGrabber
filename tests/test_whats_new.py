"""What's new after an update: which notes show, the dialog, and waiting for the window when
AirGrabber started hidden in the tray."""

from __future__ import annotations

import asyncio
import gc
import importlib
import logging
import tempfile
import threading
import tkinter as tk
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk
from PIL import Image

from app.i18n_nl import NL
from app.ui import whats_new_dialog
from app.ui.borderless import starts_drag
from app.ui.dialog_kit import CONTENT_WIDTH, PAD_X
from app.ui.main_window import MainWindow
from app.ui.whats_new_dialog import (
    FEATURES_WIDTH,
    MAX_SCREEN_SHARE,
    PICTURE_HEIGHT,
    PICTURES_DIR,
    WhatsNewDialog,
    load_picture,
)
from app.whats_new import FEATURES, NOTES, Feature, WhatsNew, dialog_title, whats_new

FAKE_NOTES = {
    "2.0.5": ("Five a", "Five b", "Five c"),
    "2.0.7": ("Seven a", "Seven b", "Seven c"),
    "2.1": ("Two one a", "Two one b", "Two one c"),
}
# tools/ is not a package; importing by name keeps mypy from seeing the module twice.
PICTURES = importlib.import_module("tools.whats_new_art").PICTURES

FAKE_FEATURE = Feature("Big thing", "It does a lot.", "library.png")
OTHER_FEATURE = Feature("Other thing", "It does more.", "library.png")
# Pictures that aren't there or don't read (the release has one real picture).
MISSING_FEATURE = Feature("Big thing", "It does a lot.", "missing.png")
BROKEN_FEATURE = Feature("Other thing", "It does more.", "broken.png")
LINES = ("One.", "Two.", "Three.")
TALL_SCREEN = (0, 0, 4000, 4000)
SHORT_SCREEN = (0, 0, 2000, 700)


class WhatsNewTests(unittest.TestCase):
    def test_after_an_update_the_newest_entry_up_to_this_version(self) -> None:
        self.assertEqual(
            whats_new("2.0.4", "2.0.5", FAKE_NOTES), WhatsNew("2.0.5", FAKE_NOTES["2.0.5"])
        )
        self.assertEqual(whats_new("2.0.4", "2.0.9", FAKE_NOTES), WhatsNew("2.0.7", FAKE_NOTES["2.0.7"]))
        self.assertEqual(whats_new("2.0.4", "2.1.0", FAKE_NOTES), WhatsNew("2.1", FAKE_NOTES["2.1"]))

    def test_nothing_without_a_new_entry(self) -> None:
        self.assertIsNone(whats_new("2.0.3", "2.0.4", FAKE_NOTES))  # no entry for it
        self.assertIsNone(whats_new("2.0.5", "2.0.6", FAKE_NOTES))  # 2.0.5's were shown already

    def test_nothing_on_a_first_start_or_without_a_newer_version(self) -> None:
        self.assertIsNone(whats_new("", "2.0.5", FAKE_NOTES))  # first start: the welcome shows
        self.assertIsNone(whats_new("2.0.5", "2.0.5", FAKE_NOTES))
        self.assertIsNone(whats_new("2.0.7", "2.0.5", FAKE_NOTES))  # went back a version
        self.assertIsNone(whats_new("rubbish", "2.0.5", FAKE_NOTES))

    def test_the_release_notes(self) -> None:
        for version, lines in NOTES.items():
            with self.subTest(version):
                # Fewer are fine beside a big feature.
                self.assertTrue((1 if FEATURES.get(version) else 3) <= len(lines) <= 6)
                for line in lines:
                    self.assertLessEqual(len(line), 120)
                    self.assertTrue(line.endswith("."), line)
        self.assertEqual(whats_new("2.0.4", "2.0.5"), WhatsNew("2.0.5", NOTES["2.0.5"]))

    def test_3_8_0_has_nothing_to_tell(self) -> None:
        # Its one change is the phone connection, which others don't get: no window coming from
        # 3.7.0, and 3.7.0's notes for whoever skipped that one.
        self.assertIsNone(whats_new("3.7.0", "3.8.0"))
        self.assertEqual(whats_new("3.6.0", "3.8.0"), WhatsNew("3.7.0", NOTES["3.7.0"]))

    def test_the_notes_never_mention_the_phone_connection(self) -> None:
        for version, lines in NOTES.items():
            for line in lines:
                with self.subTest(version, line=line):
                    self.assertNotIn("phone", line.lower())
                    self.assertNotIn("companion", line.lower())

    def test_an_entry_with_features_only_lines_only_or_both(self) -> None:
        features = {"2.2": (FAKE_FEATURE,), "2.3": (FAKE_FEATURE, OTHER_FEATURE), "2.4": ()}
        notes: dict[str, tuple[str, ...]] = {"2.1": ("A", "B", "C"), "2.3": ("D", "E", "F"), "2.4": ()}
        self.assertEqual(
            whats_new("2.0", "2.1", notes, features), WhatsNew("2.1", ("A", "B", "C"), ())
        )
        self.assertEqual(
            whats_new("2.0", "2.2", notes, features), WhatsNew("2.2", (), (FAKE_FEATURE,))
        )
        self.assertEqual(
            whats_new("2.0", "2.3", notes, features),
            WhatsNew("2.3", ("D", "E", "F"), (FAKE_FEATURE, OTHER_FEATURE)),
        )
        # 2.4 has neither: the newest with something counts, unless that was shown already.
        self.assertEqual(
            whats_new("2.0", "2.4", notes, features),
            WhatsNew("2.3", ("D", "E", "F"), (FAKE_FEATURE, OTHER_FEATURE)),
        )
        self.assertIsNone(whats_new("2.3", "2.4", notes, features))

    def test_a_release_with_features_has_short_lines_in_both_languages(self) -> None:
        for version in FEATURES:
            for line in NOTES.get(version, ()):
                self.assertIn(line, NL)
                self.assertLessEqual(len(NL[line]), 120, line)

    def test_the_features(self) -> None:
        for version, features in FEATURES.items():
            with self.subTest(version):
                self.assertTrue(1 <= len(features) <= 3)
                for feature in features:
                    self.assertIn(feature.title, NL)
                    self.assertIn(feature.text, NL)
                    self.assertTrue(feature.text.endswith("."), feature.text)

    def test_the_pictures(self) -> None:
        for features in FEATURES.values():
            for feature in features:
                path = PICTURES_DIR / feature.picture
                with self.subTest(feature.picture), Image.open(path) as image:
                    self.assertEqual(
                        (image.format, image.size, image.mode), ("PNG", (1464, 488), "RGBA")
                    )
                    alpha = image.getchannel("A")
                    right, bottom = image.width - 1, image.height - 1
                    self.assertEqual(alpha.getpixel((0, 0)), 0)  # the card's corners show there
                    self.assertEqual(alpha.getpixel((right, 0)), 0)
                    self.assertEqual(alpha.getpixel((0, bottom)), 255)  # against the words
                    self.assertEqual(alpha.getpixel((right, bottom)), 255)
                    self.assertEqual(alpha.getpixel((image.width // 2, 0)), 255)
                    self.assertLess(path.stat().st_size, 300 * 1024)
                    self.assertIn(feature.picture, PICTURES)  # tools/whats_new_art.py draws it

    def test_a_picture_is_rendered_at_the_size_asked(self) -> None:
        image = load_picture(PICTURES_DIR / FAKE_FEATURE.picture, (1098, 366))
        self.assertEqual((image.size, image.mode), ((1098, 366), "RGBA"))
        self.assertEqual(image.getchannel("A").getpixel((0, 0)), 0)

    def test_title(self) -> None:
        self.assertEqual(dialog_title("2.0.5"), "What's new in AirGrabber 2.0.5")


class WhatsNewWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for dialog in self.dialogs():  # first: their own timers belong to them
            dialog.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def dialogs(self) -> list[WhatsNewDialog]:
        return [child for child in self.root.winfo_children() if isinstance(child, WhatsNewDialog)]

    def texts(self, widget: Any) -> list[str]:
        found: list[str] = []
        for child in widget.winfo_children():
            if isinstance(child, ctk.CTkLabel):
                found.append(str(child.cget("text")))
            found.extend(self.texts(child))
        return found

    def test_the_dialog(self) -> None:
        dialog = WhatsNewDialog(self.root, version="2.0.5", lines=("One.", "Two.", "Three."))
        self.root.update()
        self.assertEqual(dialog.title(), "What's new in AirGrabber 2.0.5")
        texts = self.texts(dialog)
        self.assertIn("What's new in AirGrabber 2.0.5", texts)
        for line in ("One.", "Two.", "Three."):
            self.assertIn(line, texts)
        # Tk delivers a generated key press to the window with the keyboard focus, which Windows
        # withholds while another program is in front (a desktop in use while the tests run).
        self.assertTrue(dialog.bind("<Escape>"))
        dialog.focus_force()
        self.root.update()
        if dialog.focus_get() is None:
            self.skipTest("no keyboard focus for the test window")
        dialog.event_generate("<Escape>")
        self.root.update()
        self.assertEqual(self.dialogs(), [])

    def test_without_features_it_is_the_plain_dialog(self) -> None:
        # No running loop here: it starts nothing either.
        dialog = WhatsNewDialog(self.root, version="2.0.5", lines=LINES)
        self.root.update()
        scaling = ctk.ScalingTracker.get_widget_scaling(dialog)
        self.assertEqual(dialog.winfo_reqwidth(), round((CONTENT_WIDTH + 2 * PAD_X) * scaling))
        self.assertIsNone(dialog.scroll)
        self.assertEqual(dialog.cards, [])
        self.assertNotIn("ALSO NEW", self.texts(dialog))
        self.assertIsNone(dialog._load_task)

    def test_started_hidden_it_waits_for_the_window(self) -> None:
        window: Any = self.root
        window._whats_new_binding = None
        MainWindow._show_whats_new(window, WhatsNew("2.0.5", ("One.", "Two.", "Three.")))
        self.root.update()
        self.assertEqual(self.dialogs(), [])  # the window is still hidden in the tray
        self.assertIsNotNone(window._whats_new_binding)
        self.root.deiconify()  # opened from the tray
        self.root.update()
        self.assertEqual(len(self.dialogs()), 1)
        self.assertIsNone(window._whats_new_binding)
        self.root.withdraw()
        self.root.deiconify()  # only once
        self.root.update()
        self.assertEqual(len(self.dialogs()), 1)


class FeatureCardsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.geometry("300x200")
        self.root.update()
        self.addCleanup(self._destroy_root)
        self.area = TALL_SCREEN
        for patch in (
            mock.patch.object(whats_new_dialog, "activate_later", lambda _dialog: None),
            mock.patch.object(whats_new_dialog, "screen_area", lambda _master: self.area),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        for dialog in self.root.winfo_children():
            if isinstance(dialog, WhatsNewDialog):
                dialog.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.root.destroy()
        gc.collect()

    async def until(self, check: Callable[[], bool]) -> None:
        for _ in range(500):
            self.root.update()
            if check():
                return
            await asyncio.sleep(0.01)
        self.fail("never happened")

    def dialog(
        self,
        lines: tuple[str, ...] = LINES,
        features: tuple[Feature, ...] = (FAKE_FEATURE, OTHER_FEATURE),
    ) -> WhatsNewDialog:
        dialog = WhatsNewDialog(self.root, version="3.2.0", lines=lines, features=features)
        self.root.update()
        return dialog

    async def landed(self, dialog: WhatsNewDialog) -> None:
        task = dialog._load_task
        assert task is not None
        await self.until(task.done)
        self.root.update()

    def all_widgets(self, widget: Any) -> Iterator[Any]:
        for child in widget.winfo_children():
            yield child
            yield from self.all_widgets(child)

    def texts(self, widget: Any) -> list[str]:
        return [
            str(child.cget("text"))
            for child in self.all_widgets(widget)
            if isinstance(child, ctk.CTkLabel)
        ]

    def scroll_of(self, dialog: WhatsNewDialog) -> Any:
        self.assertIsNotNone(dialog.scroll)
        return dialog.scroll

    async def test_a_card_per_feature_with_its_picture_and_words(self) -> None:
        dialog = self.dialog()
        await self.landed(dialog)
        self.assertEqual([card.feature for card in dialog.cards], [FAKE_FEATURE, OTHER_FEATURE])
        texts = self.texts(dialog)
        for text in ("What's new in AirGrabber 3.2.0", "Big thing", "It does a lot.", "Other thing"):
            self.assertIn(text, texts)
        scaling = ctk.ScalingTracker.get_widget_scaling(dialog)
        for card in dialog.cards:
            self.assertTrue(card.has_picture)
            self.assertEqual((card.picture.winfo_x(), card.picture.winfo_y()), (0, 0))
            self.assertEqual(card.winfo_width(), round(FEATURES_WIDTH * scaling))
            photo = card.picture.cget("image").create_scaled_photo_image(scaling, "dark")
            self.assertEqual(photo.width(), round(FEATURES_WIDTH * scaling))
            self.assertEqual(photo.height(), round(PICTURE_HEIGHT * scaling))

    async def test_the_also_new_heading_only_with_lines(self) -> None:
        with_lines = self.dialog()
        self.assertIn("ALSO NEW", self.texts(with_lines))
        for line in LINES:
            self.assertIn(line, self.texts(with_lines))
        await self.landed(with_lines)
        without = self.dialog(lines=())
        self.assertNotIn("ALSO NEW", self.texts(without))
        await self.landed(without)

    async def test_nothing_moves_when_a_picture_lands(self) -> None:
        dialog = self.dialog()
        self.assertFalse(dialog.cards[0].has_picture)  # the loop hasn't run yet
        heights = [card.winfo_reqheight() for card in dialog.cards], dialog.winfo_reqheight()
        scaling = ctk.ScalingTracker.get_widget_scaling(dialog)
        self.assertGreater(heights[0][0], round(PICTURE_HEIGHT * scaling))
        await self.landed(dialog)
        self.assertTrue(dialog.cards[0].has_picture)
        self.assertEqual(
            ([card.winfo_reqheight() for card in dialog.cards], dialog.winfo_reqheight()), heights
        )

    async def test_it_fits_without_a_scrollbar_where_the_screen_has_room(self) -> None:
        dialog = self.dialog()
        await self.landed(dialog)
        scroll = self.scroll_of(dialog)
        self.assertFalse(scroll.scrolls)
        self.assertEqual(scroll._parent_canvas.winfo_height(), scroll.content_height())

    async def test_it_scrolls_when_too_tall_with_ok_in_view(self) -> None:
        self.area = SHORT_SCREEN
        dialog = self.dialog()
        await self.landed(dialog)
        scroll = self.scroll_of(dialog)
        self.assertTrue(scroll.scrolls)
        self.assertLessEqual(dialog.winfo_height(), SHORT_SCREEN[3] * MAX_SCREEN_SHARE)
        buttons = [
            widget
            for widget in self.all_widgets(dialog)
            if isinstance(widget, ctk.CTkButton) and widget.cget("text") == "OK"
        ]
        self.assertEqual(len(buttons), 1)
        bottom = buttons[0].winfo_rooty() + buttons[0].winfo_height()
        self.assertLess(bottom, dialog.winfo_rooty() + dialog.winfo_height())
        # The cards keep their width beside the scrollbar, and nothing scrolls sideways.
        scaling = ctk.ScalingTracker.get_widget_scaling(dialog)
        self.assertEqual(dialog.cards[0].winfo_width(), round(FEATURES_WIDTH * scaling))
        self.assertEqual(scroll._parent_canvas.xview(), (0.0, 1.0))

    async def test_the_mouse_wheel_scrolls_over_a_picture_and_over_the_words(self) -> None:
        self.area = SHORT_SCREEN
        dialog = self.dialog()
        await self.landed(dialog)
        scroll = self.scroll_of(dialog)
        card = dialog.cards[0]
        for widget in (card.picture._label, card._canvas, card._title._label):
            scroll.to_top()
            self.root.update()
            scroll._mouse_wheel_all(SimpleNamespace(widget=widget, delta=-120))
            self.root.update()
            self.assertGreater(scroll._parent_canvas.yview()[0], 0.0)

    async def test_a_missing_or_broken_picture_leaves_the_words(self) -> None:
        with tempfile.TemporaryDirectory(prefix="whats-new-") as folder:
            Path(folder, BROKEN_FEATURE.picture).write_bytes(b"not a picture")
            with self.assertLogs(whats_new_dialog.log, logging.WARNING) as logs:
                with mock.patch.object(whats_new_dialog, "PICTURES_DIR", Path(folder)):
                    dialog = self.dialog(features=(MISSING_FEATURE, BROKEN_FEATURE))
                    task = dialog._load_task
                    assert task is not None
                    await self.landed(dialog)
            output = "\n".join(logs.output)
            self.assertIn(f"{MISSING_FEATURE.picture} (FileNotFoundError)", output)
            self.assertIn(f"{BROKEN_FEATURE.picture} (UnidentifiedImageError)", output)
            self.assertNotIn("whats-new-", output)  # never the folder
        self.assertIsNone(task.exception())
        scaling = ctk.ScalingTracker.get_widget_scaling(dialog)
        for card in dialog.cards:
            self.assertFalse(card.has_picture)
            self.assertLess(card.winfo_reqheight(), round(PICTURE_HEIGHT * scaling))
        self.assertIn("Big thing", self.texts(dialog))
        self.assertTrue(dialog.winfo_exists())

    async def test_one_bad_picture_leaves_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            Image.new("RGBA", (30, 10)).save(Path(folder, BROKEN_FEATURE.picture))
            with mock.patch.object(whats_new_dialog, "PICTURES_DIR", Path(folder)):
                with self.assertLogs(whats_new_dialog.log, logging.WARNING):
                    dialog = self.dialog(features=(MISSING_FEATURE, BROKEN_FEATURE))
                    await self.landed(dialog)
        self.assertEqual([card.has_picture for card in dialog.cards], [False, True])

    async def test_escape_enter_and_the_close_button_close_it(self) -> None:
        for key in ("<Escape>", "<Return>"):
            with self.subTest(key):
                dialog = self.dialog()
                await self.landed(dialog)
                self.assertTrue(dialog.bind(key))
                dialog.focus_force()
                self.root.update()
                if dialog.focus_get() is None:  # Windows withholds it while another program is in front
                    dialog.destroy()
                    continue
                dialog.event_generate(key)
                self.root.update()
                self.assertFalse(dialog.winfo_exists())
        dialog = self.dialog()
        await self.landed(dialog)
        if dialog.borderless is not None:
            dialog.borderless.close_button.invoke()
            self.root.update()
            self.assertFalse(dialog.winfo_exists())

    async def test_it_drags_from_the_cards_and_pictures(self) -> None:
        dialog = self.dialog()
        if dialog.borderless is None:
            self.skipTest("no borderless windows here")
        await self.landed(dialog)
        card = dialog.cards[0]
        for widget in (card.picture._label, card._title._label, card._canvas):
            self.assertTrue(widget.bind("<ButtonPress-1>"))
            self.assertTrue(starts_drag(card, widget))

    async def test_destroyed_while_a_picture_loads(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def slow(_path: Path, _size: tuple[int, int]) -> Image.Image:
            started.set()
            release.wait(10)
            return Image.new("RGBA", (10, 10))

        self.addCleanup(release.set)
        with mock.patch.object(whats_new_dialog, "load_picture", slow):
            dialog = self.dialog()
            task = dialog._load_task
            assert task is not None
            await self.until(started.is_set)
            dialog.destroy()
            self.root.update()
            release.set()
            await asyncio.wait({task}, timeout=5)
            self.assertTrue(task.cancelled())
            self.root.update()
            self.assertFalse(dialog.winfo_exists())


if __name__ == "__main__":
    unittest.main()
