"""FlexGrid: how many columns fit, where each card goes, and the block of columns centred in the
page's width (the maths pure, the grid set-up on a real hidden Tk root)."""

from __future__ import annotations

import tkinter as tk
import unittest
from typing import Any

import customtkinter as ctk

from app.ui.widgets import FlexGrid, flex_cell, flex_columns, flex_margin


class FlexMathTests(unittest.TestCase):
    def test_columns_that_fit(self) -> None:
        slot = (192 + 16) * 1.5  # an XL poster card and its gaps, at 150 %
        self.assertEqual(flex_columns(1100 * 1.5, slot), 5)
        self.assertEqual(flex_columns(5 * slot, slot), 5)
        self.assertEqual(flex_columns(5 * slot - 1, slot), 4)
        self.assertEqual(flex_columns(100, slot), 1)  # never none
        self.assertEqual(flex_columns(100, 0), 1)

    def test_cells_row_by_row_between_the_margins(self) -> None:
        cells = [flex_cell(index, 3) for index in range(7)]
        self.assertEqual(
            cells, [(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (1, 3), (2, 1)]
        )  # the short last row starts under the first column

    def test_cells_from_the_left_when_not_centred(self) -> None:
        self.assertEqual([flex_cell(index, 2, center=False) for index in range(3)], [(0, 0), (0, 1), (1, 0)])

    def test_equal_margins(self) -> None:
        slot = 208.0
        width = 1100.0
        columns = flex_columns(width, slot)
        margin = flex_margin(width, columns, slot)
        self.assertEqual(columns, 5)
        self.assertEqual(margin * 2 + columns * slot, width)
        self.assertEqual(margin, 30.0)
        self.assertEqual(flex_margin(100, 1, 208), 0.0)  # one card wider than the page: none


class FlexGridTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    def grid_with(self, count: int, *, center: bool = True) -> FlexGrid:
        grid = FlexGrid(self.root, item_width=100, pad_x=8, pad_y=10, center=center)
        for _ in range(count):
            ctk.CTkFrame(grid, width=100, height=150)
        return grid

    def column_of(self, widget: Any) -> int:
        return int(widget.grid_info()["column"])

    def test_centred_block_with_a_short_last_row(self) -> None:
        grid = self.grid_with(7)
        grid._regrid(3)
        children = grid.winfo_children()
        self.assertEqual([self.column_of(child) for child in children], [1, 2, 3, 1, 2, 3, 1])
        slot = round((100 + 16) * ctk.ScalingTracker.get_widget_scaling(grid))
        for column in (0, 4):  # the margins share the spare width equally
            self.assertEqual(int(grid.grid_columnconfigure(column)["weight"]), 1)
        for column in (1, 2, 3):  # every card column a whole slot, also where row 3 is empty
            options = grid.grid_columnconfigure(column)
            self.assertEqual((int(options["weight"]), int(options["minsize"])), (0, slot))

    def test_fewer_columns_after_a_resize_moves_the_right_margin(self) -> None:
        grid = self.grid_with(7)
        grid._regrid(5)
        grid._regrid(3)
        self.assertEqual(int(grid.grid_columnconfigure(4)["weight"]), 1)  # the new margin
        for column in (5, 6):  # the old card column and margin are gone
            options = grid.grid_columnconfigure(column)
            self.assertEqual((int(options["weight"]), int(options["minsize"])), (0, 0))
        self.assertEqual(max(self.column_of(child) for child in grid.winfo_children()), 3)

    def test_not_centred_keeps_the_columns_on_the_left(self) -> None:
        grid = self.grid_with(3, center=False)
        grid._regrid(2)
        self.assertEqual([self.column_of(child) for child in grid.winfo_children()], [0, 1, 0])
        self.assertEqual(int(grid.grid_columnconfigure(0)["weight"]), 0)

    def test_set_order_shows_some_children_in_another_order(self) -> None:
        grid = self.grid_with(3)
        first, second, third = grid.winfo_children()
        grid.set_order([third, first])
        grid._regrid(2)  # as a resize does: the order and the hidden child stay
        self.assertEqual([self.column_of(child) for child in (third, first)], [1, 2])
        self.assertEqual(second.winfo_manager(), "")  # hidden
        grid.set_order(None)  # all of them again, as added
        grid._regrid(2)
        cells = [(int(child.grid_info()["row"]), self.column_of(child)) for child in (first, second, third)]
        self.assertEqual(cells, [(0, 1), (0, 2), (1, 1)])

if __name__ == "__main__":
    unittest.main()
