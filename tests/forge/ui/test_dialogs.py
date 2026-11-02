"""Forge's awaitable question windows (app/forge/ui/widgets/dialogs.py) on AirGrabber's dialog kit,
and MediaForge's pop-up lesson (its tests/ui/test_popups.py): a pop-up destroyed before it was
ever shown must not break Tk, and no pop-up is transient."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk

import customtkinter as ctk

from app.forge.ui.look import forge_look
from app.forge.ui.widgets.chips import OptionButton
from app.forge.ui.widgets.dialogs import ask_choice, ask_text, confirm
from app.forge.ui.widgets.virtual_list import Row, VirtualList
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import press, pump, pump_until


class DialogCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def popups(self) -> list[ctk.CTkToplevel]:
        return [w for w in self.root.winfo_children() if isinstance(w, ctk.CTkToplevel)]

    async def opened(self) -> ctk.CTkToplevel:
        await pump_until(self.root, lambda: self.popups())
        [dialog] = self.popups()
        dialog.focus_force()  # keys go to the window with the focus
        await pump(self.root, 0.05)
        return dialog

    async def dismiss(self, question: asyncio.Task[str | None]) -> None:
        question.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await question

    async def answer(self, question: asyncio.Task[str | None]) -> str | None:
        return await asyncio.wait_for(question, 5)

    def buttons(self, dialog: tk.Misc) -> dict[str, ctk.CTkButton]:
        found: dict[str, ctk.CTkButton] = {}
        stack: list[tk.Misc] = [dialog]
        while stack:
            widget = stack.pop()
            if isinstance(widget, ctk.CTkButton):
                found[str(widget.cget("text"))] = widget
            stack.extend(widget.winfo_children())
        return found


class AskChoiceTests(DialogCase):
    async def test_returns_the_chosen_label_and_closes(self) -> None:
        question = asyncio.create_task(
            ask_choice(self.root, "Rename?", "3 files will be renamed.", ["Rename", "Later"])
        )
        dialog = await self.opened()
        self.assertEqual(dialog.title(), "Rename?")
        self.buttons(dialog)["Later"].invoke()
        self.assertEqual(await self.answer(question), "Later")
        await pump(self.root, 0.02)
        self.assertEqual(self.popups(), [])

    async def test_first_choice_is_the_copper_main_button(self) -> None:
        look = forge_look()
        question = asyncio.create_task(ask_choice(self.root, "T", "M", ["Yes", "No"]))
        dialog = await self.opened()
        buttons = self.buttons(dialog)
        self.assertEqual(tuple(buttons["Yes"].cget("fg_color")), look.ACCENT)
        self.assertEqual(tuple(buttons["No"].cget("border_color")), look.EDGE)
        # Shown in the order given, right-aligned.
        await pump(self.root, 0.05)
        self.assertLess(buttons["Yes"].winfo_x(), buttons["No"].winfo_x())
        await self.dismiss(question)

    async def test_escape_and_the_window_close_give_none(self) -> None:
        question = asyncio.create_task(ask_choice(self.root, "T", "M", ["OK"]))
        dialog = await self.opened()
        press(dialog, "<Escape>")
        self.assertIsNone(await self.answer(question))
        question = asyncio.create_task(ask_choice(self.root, "T", "M", ["OK"]))
        dialog = await self.opened()
        dialog.destroy()  # gone some other way
        self.assertIsNone(await self.answer(question))

    async def test_enter_picks_the_main_choice(self) -> None:
        question = asyncio.create_task(ask_choice(self.root, "T", "M", ["Go", "Stop"]))
        dialog = await self.opened()
        press(dialog, "<Return>")
        self.assertEqual(await self.answer(question), "Go")

    async def test_cancelling_the_wait_closes_the_window(self) -> None:
        question = asyncio.create_task(ask_choice(self.root, "T", "M", ["OK"]))
        await self.opened()
        question.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await question
        await pump(self.root, 0.02)
        self.assertEqual(self.popups(), [])

    async def test_never_transient(self) -> None:
        question = asyncio.create_task(ask_choice(self.root, "T", "M", ["OK"]))
        dialog = await self.opened()
        self.assertEqual(str(dialog.wm_transient()), "")
        await self.dismiss(question)

    async def test_confirm(self) -> None:
        for pressed, expected in (("Undo", True), ("Cancel", False)):
            answer = asyncio.create_task(confirm(self.root, "Undo this run?", "M", "Undo"))
            dialog = await self.opened()
            self.buttons(dialog)[pressed].invoke()
            with self.subTest(pressed=pressed):
                self.assertIs(await asyncio.wait_for(answer, 5), expected)
            await pump(self.root, 0.02)


class AskTextTests(DialogCase):
    def entry(self, dialog: tk.Misc) -> ctk.CTkEntry:
        stack: list[tk.Misc] = [dialog]
        while stack:
            widget = stack.pop()
            if isinstance(widget, ctk.CTkEntry):
                return widget
            stack.extend(widget.winfo_children())
        raise AssertionError("no entry")

    async def test_returns_the_text_typed_without_spaces_around_it(self) -> None:
        question = asyncio.create_task(
            ask_text(self.root, "Save as preset", "A name for the new preset:", "Save")
        )
        dialog = await self.opened()
        self.assertEqual(dialog.title(), "Save as preset")
        buttons = self.buttons(dialog)
        self.assertEqual(tuple(buttons["Save"].cget("fg_color")), forge_look().ACCENT)
        buttons["Save"].invoke()  # nothing typed: nothing happens
        await pump(self.root, 0.02)
        self.assertFalse(question.done())
        self.entry(dialog).insert(0, "  Films, short  ")
        press(dialog, "<Return>")
        self.assertEqual(await self.answer(question), "Films, short")

    async def test_cancel_escape_and_a_closed_window_give_none(self) -> None:
        question = asyncio.create_task(ask_text(self.root, "T", "M", "OK", initial="x"))
        dialog = await self.opened()
        self.assertEqual(self.entry(dialog).get(), "x")
        self.buttons(dialog)["Cancel"].invoke()
        self.assertIsNone(await self.answer(question))
        question = asyncio.create_task(ask_text(self.root, "T", "M", "OK"))
        dialog = await self.opened()
        press(dialog, "<Escape>")
        self.assertIsNone(await self.answer(question))
        question = asyncio.create_task(ask_text(self.root, "T", "M", "OK"))
        dialog = await self.opened()
        self.assertEqual(str(dialog.wm_transient()), "")
        dialog.destroy()
        self.assertIsNone(await self.answer(question))


class PopupLessonTests(DialogCase):
    async def test_popups_can_close_before_they_are_shown(self) -> None:
        # Regression (MediaForge): pop-ups marked `transient` with a size, destroyed before being
        # shown, left Tk in a state where building the next widget crashed the whole process.
        for _ in range(3):
            plain = ctk.CTkToplevel(self.root)
            plain.geometry("300x200")
            plain.destroy()
            question = asyncio.get_running_loop().create_task(
                ask_choice(self.root, "Title", "Message", ["OK"])
            )
            await asyncio.sleep(0)
            for child in self.root.winfo_children():
                if child.winfo_class() == "Toplevel":
                    child.destroy()
            await self.dismiss(question)
            holder = ctk.CTkFrame(self.root)
            holder.pack()
            OptionButton(holder, "Subfolder", lambda value: None).pack()
            vlist = VirtualList(holder)
            vlist.pack()
            vlist.set_rows([Row(key=1, kind="item", title="a")])
            await pump(self.root, 0.05)
            holder.destroy()
        self.assertEqual(self.popups(), [])
