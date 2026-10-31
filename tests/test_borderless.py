"""Pop-ups without the Windows title bar (app/ui/borderless.py): the round close button and the
way out it takes, Escape, dragging (never from a control), the no-op path, the pure Win32
helpers, and the dialogs that show their title as a heading. The Win32 / DWM side is a fake:
no test touches a real window's frame."""

from __future__ import annotations

import gc
import tkinter as tk
import unittest
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.i18n import Language, set_language
from app.ui import borderless, theme
from app.ui.borderless import (
    CLOSE_INSET,
    CLOSE_ROOM,
    CLOSE_SIZE,
    Borderless,
    CloseButton,
    caption_free_top,
    clearance,
    fitted_height,
    make_borderless,
    point_lparam,
    starts_drag,
)
from app.ui.widgets import show_info


class FakeNative:
    """Records what a borderless window asks of Windows."""

    def __init__(self, *, attaches: bool = True) -> None:
        self.attaches = attaches
        self.attached: list[tuple[int, bool]] = []
        self.drags: list[int] = []
        self.dark: list[tuple[int, bool]] = []

    def attach(self, window_id: int, *, dark: bool) -> bool:
        self.attached.append((window_id, dark))
        return self.attaches

    def start_drag(self, window_id: int) -> None:
        self.drags.append(window_id)

    def set_dark(self, window_id: int, dark: bool) -> None:
        self.dark.append((window_id, dark))


class PureTests(unittest.TestCase):
    def test_the_client_area_starts_at_the_windows_top(self) -> None:
        self.assertEqual(caption_free_top(200, 300, 311, zoomed=False), 200)
        # Maximized: the border hangs over the screen's edge, the content starts under it.
        self.assertEqual(caption_free_top(-11, -11, 0, zoomed=True), 0)

    def test_a_point_as_lparam(self) -> None:
        self.assertEqual(point_lparam(10, 20), 10 | (20 << 16))
        self.assertEqual(point_lparam(-1, -2), 0xFFFF | (0xFFFE << 16))  # left of the main screen

    def test_a_pinned_height_without_the_title_bar(self) -> None:
        # Tk still counts the title bar (45) in the border: the dialog keeps its height.
        self.assertEqual(fitted_height(393 + 56, client=393, border=11, title_bar=45), 393 + 11)
        # Tk measured the window again: its number stands.
        self.assertEqual(fitted_height(393 + 11, client=393, border=11, title_bar=45), 393 + 11)
        # A new size Tk asks for (another scaling), worked out with the title bar.
        self.assertEqual(fitted_height(500 + 56, client=393, border=11, title_bar=45), 500 + 11)

    def test_room_for_the_close_button(self) -> None:
        self.assertEqual(CLOSE_ROOM, 46)
        self.assertEqual(clearance(32), CLOSE_ROOM + 8 - 32)
        self.assertEqual(clearance(100), 0)


class TkCase(unittest.TestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.native = FakeNative()
        previous = borderless.set_native(self.native)
        self.addCleanup(borderless.set_native, previous)

    def _destroy_root(self) -> None:
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def window(self) -> ctk.CTkToplevel:
        window = ctk.CTkToplevel(self.root, fg_color=theme.BG)
        window.title("A window")
        return window

    def close_buttons(self, window: tk.Misc) -> list[CloseButton]:
        return [child for child in window.winfo_children() if isinstance(child, CloseButton)]

    def press(self, widget: tk.Misc) -> None:
        """A left-button press on `widget` itself (a CTk widget's inner canvas, a plain one)."""
        target: Any = getattr(widget, "_canvas", widget)
        target.event_generate("<ButtonPress-1>", x=2, y=2)


class CloseButtonTests(TkCase):
    def test_the_round_close_button_in_the_top_right_corner(self) -> None:
        window = self.window()
        window.bind("<Escape>", lambda _event: window.destroy())
        frame = make_borderless(window)
        assert frame is not None
        self.assertEqual(self.native.attached, [(window.winfo_id(), theme.is_dark())])
        (button,) = self.close_buttons(window)
        self.assertIs(frame.close_button, button)
        place = button.place_info()
        self.assertEqual(place["anchor"], "ne")
        self.assertEqual(float(place["relx"]), 1.0)
        scaling = ctk.ScalingTracker.get_widget_scaling(button)
        self.assertEqual(int(place["x"]), -round(CLOSE_INSET * scaling))
        self.assertEqual(int(place["y"]), round(CLOSE_INSET * scaling))
        # Round: as wide as it is tall (a CTkButton grows wider around its text).
        diameter = round(CLOSE_SIZE * scaling)
        self.assertEqual((button.winfo_reqwidth(), button.winfo_reqheight()), (diameter, diameter))
        # On top of whatever the window holds (Tk lists children lowest first).
        stacking = window.tk.splitlist(window.tk.call("winfo", "children", str(window)))
        self.assertEqual(stacking[-1], str(button))
        # CTk no longer hides and re-shows it to recolour a title bar it hasn't got.
        self.assertTrue(getattr(window, "_deactivate_windows_window_header_manipulation"))

    def test_red_with_a_white_cross_under_the_pointer(self) -> None:
        window = self.window()
        frame = make_borderless(window)
        assert frame is not None
        button = frame.close_button
        self.root.update()  # mapped: Tk sends no Enter / Leave to a hidden widget
        glyph = button.find_withtag("all")[-1]  # the ✕, over the disc

        def fill() -> str:
            return str(button.tk.call(str(button), "itemcget", glyph, "-fill"))

        self.assertEqual(fill(), theme.pick(theme.CLOSE_GLYPH))
        button.event_generate("<Enter>")
        self.assertTrue(button.hovered)
        self.assertEqual(fill(), theme.ON_COLOR)
        button.event_generate("<Leave>")
        self.assertFalse(button.hovered)
        self.assertEqual(fill(), theme.pick(theme.CLOSE_GLYPH))

    def test_a_click_is_a_press_let_go_on_it(self) -> None:
        window = self.window()
        closed: list[str] = []
        frame = make_borderless(window, on_close=lambda: closed.append("closed"))
        assert frame is not None
        button = frame.close_button
        self.root.update()
        button.event_generate("<ButtonPress-1>", x=3, y=3)
        button.event_generate("<ButtonRelease-1>", x=-50, y=3)  # let go elsewhere: nothing
        self.assertEqual(closed, [])
        button.event_generate("<ButtonPress-1>", x=3, y=3)
        button.event_generate("<ButtonRelease-1>", x=4, y=4)
        self.assertEqual(closed, ["closed"])

    def test_it_calls_on_close(self) -> None:
        window = self.window()
        closed: list[str] = []
        frame = make_borderless(window, on_close=lambda: closed.append("closed"))
        assert frame is not None
        frame.close_button.invoke()
        self.assertEqual(closed, ["closed"])
        self.assertTrue(window.winfo_exists())  # on_close decides

    def test_it_takes_the_windows_own_way_out(self) -> None:
        window = self.window()
        closed: list[str] = []
        window.protocol("WM_DELETE_WINDOW", lambda: closed.append("protocol"))
        frame = make_borderless(window)
        assert frame is not None
        frame.close_button.invoke()
        self.assertEqual(closed, ["protocol"])
        self.assertTrue(window.winfo_exists())

    def test_without_one_it_destroys_the_window(self) -> None:
        window = self.window()
        frame = make_borderless(window)
        assert frame is not None
        frame.close_button.invoke()
        self.root.update()
        self.assertFalse(window.winfo_exists())

    def test_the_tip_names_escape_only_when_it_closes_the_window(self) -> None:
        with_escape = self.window()
        with_escape.bind("<Escape>", lambda _event: with_escape.destroy())
        without = self.window()
        with mock.patch.object(borderless, "add_tooltip") as tooltip:
            make_borderless(with_escape)
            make_borderless(without)
        self.assertEqual([call.args[1] for call in tooltip.call_args_list], ["Close (Esc)", "Close"])
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        dutch = self.window()
        dutch.bind("<Escape>", lambda _event: dutch.destroy())
        with mock.patch.object(borderless, "add_tooltip") as tooltip:
            make_borderless(dutch)
        self.assertEqual(tooltip.call_args.args[1], "Sluiten (Esc)")

    def test_escape_still_closes_it(self) -> None:
        window = self.window()
        window.bind("<Escape>", lambda _event: window.destroy())
        make_borderless(window, drag_widgets=(window,))
        self.assertTrue(window.bind("<Escape>"))
        window.deiconify()
        window.focus_force()
        self.root.update()
        if window.focus_get() is None:
            self.skipTest("no keyboard focus for the test window")
        window.event_generate("<Escape>")
        self.root.update()
        self.assertFalse(window.winfo_exists())

    def test_the_border_follows_light_and_dark(self) -> None:
        window = self.window()
        before = list(ctk.AppearanceModeTracker.callback_list)
        make_borderless(window)
        added = [c for c in ctk.AppearanceModeTracker.callback_list if c not in before]
        with mock.patch.object(theme, "is_dark", return_value=False):
            for callback in added:
                callback("Light")
        self.assertIn((window.winfo_id(), False), self.native.dark)


class NoOpTests(TkCase):
    def test_off_windows_nothing_changes(self) -> None:
        borderless.set_native(None)
        window = self.window()
        self.assertIsNone(make_borderless(window, drag_widgets=(window,)))
        self.assertEqual(self.close_buttons(window), [])
        self.assertFalse(window._deactivate_windows_window_header_manipulation)
        self.assertEqual(self.native.attached, [])

    def test_a_window_windows_wont_change_keeps_its_title_bar(self) -> None:
        self.native.attaches = False
        window = self.window()
        with self.assertLogs("app.ui.borderless", "WARNING"):
            self.assertIsNone(make_borderless(window))
        self.assertEqual(self.close_buttons(window), [])
        self.assertFalse(window._deactivate_windows_window_header_manipulation)


class DragTests(TkCase):
    def setUp(self) -> None:
        super().setUp()
        self.top = self.window()
        self.area = ctk.CTkFrame(self.top, fg_color="transparent")
        self.area.pack(fill="both", expand=True)
        self.label = ctk.CTkLabel(self.area, text="Heading")
        self.label.pack()
        self.link = ctk.CTkLabel(self.area, text="A link", cursor="hand2")
        self.link.pack()
        self.button = ctk.CTkButton(self.area, text="OK")
        self.button.pack()
        self.entry = ctk.CTkEntry(self.area)
        self.entry.pack()
        self.plain = tk.Canvas(self.top, width=40, height=20)
        self.plain.pack()
        frame = make_borderless(self.top, drag_widgets=(self.top, self.area, self.plain))
        assert frame is not None
        self.frame: Borderless = frame
        self.root.update()  # mapped: Tk delivers no pointer events to a hidden widget
        self.window_id = self.top.winfo_id()

    def test_the_background_and_its_labels_drag_the_window(self) -> None:
        self.press(self.area)
        self.press(self.label)
        self.press(self.plain)
        self.top.event_generate("<ButtonPress-1>", x=1, y=1)  # the window's own background
        self.assertEqual(self.native.drags, [self.window_id] * 4)

    def test_a_button_an_entry_or_a_link_never_starts_a_drag(self) -> None:
        self.press(self.button)
        self.button._text_label.event_generate("<ButtonPress-1>", x=1, y=1)
        self.entry._entry.event_generate("<ButtonPress-1>", x=1, y=1)
        self.press(self.link)
        self.press(self.frame.close_button)
        self.assertEqual(self.native.drags, [])

    def test_more_widgets_later(self) -> None:
        later = ctk.CTkFrame(self.top)
        later.pack()
        self.root.update()
        self.press(later)
        self.assertEqual(self.native.drags, [])
        self.frame.add_drag(later)
        self.press(later)
        self.assertEqual(self.native.drags, [self.window_id])

    def test_which_presses_drag(self) -> None:
        self.assertTrue(starts_drag(self.area, self.area._canvas))
        self.assertTrue(starts_drag(self.top, self.top))
        self.assertFalse(starts_drag(self.top, self.area._canvas))  # inside the window: not its own
        self.assertFalse(starts_drag(self.area, self.button._canvas))  # a control's inner canvas
        self.assertFalse(starts_drag(self.area, self.entry._entry))
        self.assertFalse(starts_drag(self.area, ".gone"))  # a widget gone meanwhile

    def test_not_on_a_canvas_item_with_its_own_click(self) -> None:
        canvas = tk.Canvas(self.top)
        pill = canvas.create_rectangle(0, 0, 10, 10, tags=("pill",))
        canvas.tag_bind("pill", "<Button-1>", lambda _event: None)
        backdrop = canvas.create_rectangle(20, 20, 30, 30)
        with mock.patch.object(canvas, "find_withtag", return_value=(pill,)):
            self.assertFalse(starts_drag(canvas, canvas))
        with mock.patch.object(canvas, "find_withtag", return_value=(backdrop,)):
            self.assertTrue(starts_drag(canvas, canvas))
        with mock.patch.object(canvas, "find_withtag", return_value=()):
            self.assertTrue(starts_drag(canvas, canvas))


class DialogTests(TkCase):
    def texts(self, widget: tk.Misc) -> list[str]:
        found: list[str] = []
        for child in widget.winfo_children():
            if isinstance(child, ctk.CTkLabel):
                found.append(str(child.cget("text")))
            found.extend(self.texts(child))
        return found

    def test_show_info_shows_its_title_as_a_heading(self) -> None:
        show_info(self.root, "Import complete", "3 shows imported.")
        (dialog,) = [c for c in self.root.winfo_children() if isinstance(c, ctk.CTkToplevel)]
        self.assertEqual(dialog.title(), "Import complete")  # the taskbar and Alt+Tab
        texts = self.texts(dialog)
        self.assertIn("Import complete", texts)
        self.assertIn("3 shows imported.", texts)
        (button,) = self.close_buttons(dialog)
        button.invoke()
        self.root.update()
        self.assertFalse(dialog.winfo_exists())

    def test_the_tray_notice_closes_its_own_way(self) -> None:
        from app.ui.tray import TrayNotice

        done: list[str] = []
        notice = TrayNotice(self.root, on_done=lambda: done.append("done"))
        (button,) = self.close_buttons(notice)
        button.invoke()
        self.root.update()
        self.assertEqual(done, ["done"])
        self.assertFalse(notice.winfo_exists())


if __name__ == "__main__":
    unittest.main()
