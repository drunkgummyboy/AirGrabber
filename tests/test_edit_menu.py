"""The right-click menu of text fields (app/ui/widgets.py `install_edit_menus`, `edit_actions`)
and where a right-click menu opens (app/ui/missing_control.py `point_position`); the widgets on
a real (hidden) Tk root. The system clipboard is used briefly."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest

import customtkinter as ctk

from app.ui.missing_control import PopupMenu, point_position
from app.ui.widgets import edit_actions, install_edit_menus


class PointTests(unittest.TestCase):
    def test_opens_at_the_pointer(self) -> None:
        window = (100, 100, 800, 600)
        self.assertEqual(point_position((300, 200), (200, 150), window), (300, 200))

    def test_moves_left_or_up_near_the_edges(self) -> None:
        window = (100, 100, 800, 600)  # right edge 900, bottom 700
        self.assertEqual(point_position((850, 650), (200, 150), window), (650, 500))
        # Bigger than the room either way: kept inside from the left / top.
        self.assertEqual(point_position((150, 150), (900, 700), window), (100, 100))


class EditMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        # Shown, but see-through: Tk only pastes into a field that is on screen.
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("400x300+40+40")
        self.addCleanup(self._destroy_root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        if PopupMenu._open is not None:
            PopupMenu._open.close()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.root.destroy()
        gc.collect()

    async def settle(self) -> None:
        for _ in range(4):
            await asyncio.sleep(0.01)
            self.root.update()

    def run_item(self, field: tk.Entry | tk.Text, label: str) -> None:
        actions = dict(edit_actions(field))
        actions[label]()

    async def test_cut_copy_paste_select_all(self) -> None:
        source = ctk.CTkEntry(self.root)
        target = ctk.CTkEntry(self.root)
        source.pack()
        target.pack()
        await self.settle()
        source.insert(0, "Severance Season 2")
        field = source._entry
        self.assertEqual([label for label, _ in edit_actions(field)], ["Cut", "Copy", "Paste", "Select all"])
        field.selection_range(0, 9)
        self.run_item(field, "Copy")
        await self.settle()
        self.assertEqual(self.root.clipboard_get(), "Severance")
        self.run_item(target._entry, "Paste")
        await self.settle()
        self.assertEqual(target.get(), "Severance")
        self.run_item(field, "Select all")
        await self.settle()
        self.assertEqual(field.selection_get(), "Severance Season 2")
        self.run_item(field, "Cut")
        await self.settle()
        self.assertEqual(source.get(), "")
        self.assertEqual(self.root.clipboard_get(), "Severance Season 2")

    async def test_a_text_box_too(self) -> None:
        box = ctk.CTkTextbox(self.root)
        box.pack()
        await self.settle()
        self.root.clipboard_clear()
        self.root.clipboard_append("6174=#ffcc00;")
        self.run_item(box._textbox, "Paste")
        await self.settle()
        self.assertEqual(box.get("1.0", "end").strip(), "6174=#ffcc00;")

    async def test_a_field_that_cant_be_edited_only_copies(self) -> None:
        entry = ctk.CTkEntry(self.root)
        entry.insert(0, "abc")
        entry.configure(state="disabled")
        self.assertEqual([label for label, _ in edit_actions(entry._entry)], ["Copy", "Select all"])
        box = ctk.CTkTextbox(self.root, state="disabled")
        self.assertEqual([label for label, _ in edit_actions(box._textbox)], ["Copy", "Select all"])

    async def test_a_right_click_opens_it_in_every_field(self) -> None:
        install_edit_menus(self.root)
        install_edit_menus(self.root)  # again: still one binding
        script = self.root.bind_class("Entry", "<Button-3>")
        self.assertEqual(len([line for line in script.splitlines() if line.strip()]), 1)
        self.assertTrue(self.root.bind_class("Text", "<Button-3>"))
        entry = ctk.CTkEntry(self.root)
        entry.pack()
        await self.settle()
        entry._entry.event_generate("<Button-3>", x=4, y=4)
        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(menu.labels, ("Cut", "Copy", "Paste", "Select all"))
        menu.close()


if __name__ == "__main__":
    unittest.main()
