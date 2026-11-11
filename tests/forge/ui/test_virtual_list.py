"""The fast list (app/forge/ui/widgets/virtual_list.py), ported from MediaForge's
tests/ui/test_virtual_list.py: only rows on screen are drawn, scrolling, selection, tick boxes,
bands with and without pictures, the smoothness budget; plus the look in light and dark,
a switch between them and a change of Windows' scaling. The queue's bands (mockups/forge-chosen.html):
their status as a dot and a word, the destination in the link colour, buttons only under the
pointer, done bands faded. And the preset card's option buttons and the Forge progress bar
(app/forge/ui/widgets/chips.py)."""

from __future__ import annotations

import asyncio
import gc
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Literal
from unittest import mock

import customtkinter as ctk
from PIL import Image, ImageTk

from app.forge.ui.look import app_look
from app.forge.ui.widgets.chips import ForgeProgress, OptionButton, option_style
from app.forge.ui.widgets.virtual_list import (
    MONO_FAMILY,
    Row,
    Sizes,
    VirtualList,
    thumb_colours,
)
from app.ui import theme
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import SPEED_ROOM, pump
from tests.test_accent import contrast


def cget(canvas: tk.Canvas, item: int, option: str) -> str:
    """A canvas item's option (Canvas.itemcget is untyped)."""
    return str(canvas.tk.call(str(canvas), "itemcget", item, f"-{option}"))


def many_rows(n: int) -> list[Row]:
    rows: list[Row] = []
    for i in range(n):
        if i % 50 == 0:
            rows.append(Row(key=-(i + 1), kind="group", title=f"Group {i}"))
        rows.append(
            Row(
                key=i,
                kind="item",
                title=f"Show - S01E{i:02d} - Name.mkv",
                subtitle=f"raw{i}.mkv",
                badge="Show",
                status="ready",
                highlight=(7, 13),
            )
        )
    return rows


def band_rows(n_bands: int, per_band: int = 2) -> list[Row]:
    rows: list[Row] = [Row(key=-1, kind="section", title="Needs you", subtitle=str(n_bands))]
    key = 0
    for b in range(n_bands):
        rows.append(
            Row(
                key=-(b + 10),
                kind="band",
                title=f"Show {b}",
                subtitle=f"Season 1 · {per_band} episodes → Series › Show {b} › Season 01",
                badge=f"Show {b}",
                art=f"/p{b}.jpg",
                backdrop=f"/b{b}.jpg",
                actions=(("choose", "Choose…"), ("rename", "Rename")),
                note="12:30",
            )
        )
        for e in range(per_band):
            rows.append(
                Row(
                    key=key,
                    kind="item",
                    title=f"Episode {e}",
                    code=f"S01E{e + 1:02d}",
                    faint=f"show.{b}.s01e{e + 1:02d}.1080p.web.mkv",
                    status="ready",
                )
            )
            key += 1
    return rows


class TkCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        mode = ctk.get_appearance_mode()
        self.addCleanup(ctk.set_appearance_mode, mode)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def make_list(self, width: int = 700, height: int = 500) -> VirtualList:
        holder = ctk.CTkFrame(self.root, width=width, height=height)
        holder.place(x=0, y=0)
        holder.pack_propagate(False)
        self.addCleanup(holder.destroy)
        vlist = VirtualList(holder)
        vlist.pack(fill="both", expand=True)
        self.root.deiconify()  # mapped, so the canvas has its real size
        self.root.geometry(f"{width + 50}x{height + 50}+20+20")
        await pump(self.root, 0.05)
        return vlist

    def texts(self, vlist: VirtualList) -> dict[str, str]:
        """Every text item's text → its fill."""
        canvas = vlist.canvas
        return {
            cget(canvas, i, "text"): cget(canvas, i, "fill")
            for i in canvas.find_all()
            if str(canvas.type(i)) == "text"
        }


class VirtualListTests(TkCase):
    async def test_draws_only_visible(self) -> None:
        vlist = await self.make_list()
        rows = many_rows(1000)
        start = time.perf_counter()
        vlist.set_rows(rows)
        self.assertLess(time.perf_counter() - start, 0.2 * SPEED_ROOM)
        await pump(self.root, 0.05)
        visible = vlist.visible_keys()
        self.assertTrue(0 < len(visible) < 40, len(visible))
        self.assertEqual(visible[0], rows[0].key)

    async def test_scroll_changes_visible(self) -> None:
        vlist = await self.make_list()
        vlist.set_rows(many_rows(1000))
        await pump(self.root, 0.05)
        before = vlist.visible_keys()
        vlist.scroll_to_fraction(0.5)
        await pump(self.root, 0.05)
        after = vlist.visible_keys()
        self.assertTrue(after and after != before)
        self.assertTrue(any(400 <= k <= 600 for k in after))

    async def test_select_notifies_and_survives_new_rows(self) -> None:
        vlist = await self.make_list()
        picked: list[int | None] = []
        vlist.on_select = picked.append
        vlist.set_rows(many_rows(10))
        vlist.select(3)
        self.assertEqual(picked, [3])
        self.assertEqual(vlist.selected, 3)
        vlist.set_rows(many_rows(10))
        self.assertEqual(vlist.selected, 3)
        vlist.set_rows(many_rows(2))
        self.assertIsNone(vlist.selected)

    async def test_item_keys_skip_groups(self) -> None:
        vlist = await self.make_list()
        vlist.set_rows(many_rows(3))
        self.assertEqual(vlist.item_keys(), [0, 1, 2])

    async def test_tick_boxes_toggle_on_click_and_respect_disabled(self) -> None:
        vlist = await self.make_list()
        toggled: list[tuple[int, bool]] = []
        vlist.on_toggle = lambda key, value: toggled.append((key, value))
        vlist.set_rows(
            [
                Row(key=1, kind="item", title="a", checked=True),
                Row(key=2, kind="item", title="b", checked=True, check_disabled=True),
            ]
        )
        await pump(self.root, 0.05)
        x, y_first, y_second = vlist.check_point(1), vlist.row_middle(1), vlist.row_middle(2)
        vlist.canvas.event_generate("<Button-1>", x=x, y=y_first)
        vlist.canvas.event_generate("<Button-1>", x=x, y=y_second)
        await pump(self.root, 0.05)
        self.assertEqual(toggled, [(1, False)])
        self.assertEqual([r.checked for r in vlist.rows], [False, True])

    async def test_rows_are_frozen(self) -> None:
        row = Row(key=1, kind="item", title="a", checked=True)
        with self.assertRaises(ValueError):
            row.checked = False  # type: ignore[misc]

    async def test_band_actions_are_clickable(self) -> None:
        vlist = await self.make_list()
        pressed: list[tuple[int, str]] = []
        vlist.on_action = lambda key, action: pressed.append((key, action))
        vlist.set_rows(band_rows(2))
        await pump(self.root, 0.05)
        for action in ("rename", "choose"):
            x, y = vlist.action_point(-10, action)
            vlist.canvas.event_generate("<Button-1>", x=x, y=y)
        await pump(self.root, 0.05)
        self.assertEqual(pressed, [(-10, "rename"), (-10, "choose")])

    async def test_a_band_button_keeps_the_focus_its_menu_took(self) -> None:
        # A button may open a menu, which takes the focus: CTk's own click on "all" (the focus
        # to the clicked widget) ran after it, took it back and the menu closed at once.
        vlist = await self.make_list()
        calls: list[str] = []
        vlist.on_action = lambda key, action: calls.append(action)
        vlist.set_rows(band_rows(2))
        await pump(self.root, 0.05)
        with mock.patch.object(vlist.canvas, "focus_set", side_effect=lambda: calls.append("focus")):
            x, y = vlist.action_point(-10, "rename")
            vlist.canvas.event_generate("<Button-1>", x=x, y=y)
            self.assertEqual(calls, ["focus", "rename"])  # nothing takes the focus after it
            calls.clear()
            first = next(r.key for r in vlist.rows if r.kind == "item")
            vlist.canvas.event_generate("<Button-1>", x=200, y=vlist.row_middle(first))
            self.assertIn("focus", calls)  # a line: the keys move from there
            self.assertEqual(vlist.selected, first)

    async def test_band_draws_without_images(self) -> None:
        # No TMDB key, offline or no picture: a tinted band with initials.
        vlist = await self.make_list()
        vlist.images = lambda row: None
        vlist.set_rows(band_rows(1))
        await pump(self.root, 0.05)
        canvas = vlist.canvas
        initials = [cget(canvas, i, "text") for i in canvas.find_withtag("band-initials")]
        self.assertEqual(initials, ["S0"])

    async def test_band_uses_the_poster_it_gets(self) -> None:
        vlist = await self.make_list()
        poster = ImageTk.PhotoImage(Image.new("RGB", (30, 45), "red"), master=self.root)
        vlist.images = lambda row: poster
        vlist.set_rows(band_rows(1))
        await pump(self.root, 0.05)
        canvas = vlist.canvas
        shown = {cget(canvas, i, "image") for i in canvas.find_withtag("band-image")}
        self.assertEqual(shown, {str(poster)})

    async def test_only_visible_bands_ask_for_images(self) -> None:
        # A thousand bands, pictures only for what is on screen.
        vlist = await self.make_list()
        asked: list[str] = []

        def images(row: Row) -> None:
            asked.append(row.title)

        vlist.images = images
        start = time.perf_counter()
        vlist.set_rows(band_rows(1000))
        self.assertLess(time.perf_counter() - start, 0.3 * SPEED_ROOM)
        await pump(self.root, 0.05)
        self.assertTrue(0 < len(set(asked)) < 15, len(set(asked)))

    async def test_episode_lines_show_code_name_and_old_name(self) -> None:
        vlist = await self.make_list()
        vlist.set_rows(band_rows(1))
        await pump(self.root, 0.05)
        texts = list(self.texts(vlist))
        self.assertIn("S01E01", texts)
        self.assertIn("Episode 0", texts)
        self.assertTrue(any(t.startswith("show.0.s01e01") for t in texts))
        self.assertIn("NEEDS YOU   ·   1", texts)

    async def test_fonts_airgrabbers_and_monospace_for_old_names(self) -> None:
        """D10: AirGrabber's font, monospace only for old file names."""
        vlist = await self.make_list()
        vlist.set_rows(band_rows(1))
        await pump(self.root, 0.05)
        canvas = vlist.canvas
        family = theme.font().cget("family")
        for item in canvas.find_all():
            if str(canvas.type(item)) != "text" or not cget(canvas, item, "text"):
                continue
            text = cget(canvas, item, "text")
            shown = canvas.tk.call("font", "actual", cget(canvas, item, "font"), "-family")
            wanted = MONO_FAMILY if text.startswith("show.") else family
            expected = canvas.tk.call("font", "actual", (wanted, -12), "-family")
            with self.subTest(text=text):
                self.assertEqual(shown, expected)

    async def test_coloured_codes_are_readable_in_both_looks(self) -> None:
        look = app_look()
        for mode, dark in (("dark", True), ("light", False)):
            ctk.set_appearance_mode(mode)
            vlist = await self.make_list()

            def readable(colour: str, dark: bool = dark) -> bool:
                grounds = (theme.pick(look.BG, dark=dark), theme.pick(look.SURFACE, dark=dark))
                return min(contrast(colour, ground) for ground in grounds) >= 4

            vlist.set_rows(band_rows(1))  # an episode line's code
            await pump(self.root, 0.05)
            with self.subTest(mode=mode, row="band"):
                self.assertTrue(readable(self.texts(vlist)["S01E01"]))
            vlist.set_rows(many_rows(1))  # the code inside a new name (the confirm window)
            await pump(self.root, 0.05)
            with self.subTest(mode=mode, row="item"):
                self.assertTrue(readable(self.texts(vlist)["S01E00"]))

    async def test_redraws_on_a_switch_between_light_and_dark(self) -> None:
        look = app_look()
        ctk.set_appearance_mode("light")
        vlist = await self.make_list()
        vlist.set_rows(band_rows(1))
        await pump(self.root, 0.05)
        self.assertEqual(vlist.canvas.cget("bg").upper(), theme.pick(look.BG, dark=False).upper())
        self.assertEqual(self.texts(vlist)["Show 0"].upper(), theme.pick(theme.TEXT, dark=False))
        ctk.set_appearance_mode("dark")
        await pump(self.root, 0.05)
        self.assertEqual(vlist.canvas.cget("bg").upper(), theme.pick(look.BG, dark=True).upper())
        self.assertEqual(self.texts(vlist)["Show 0"].upper(), theme.pick(theme.TEXT, dark=True))
        self.assertEqual(
            self.texts(vlist)["S01E01"].upper(), theme.pick(look.LINK, dark=True).upper()
        )

    async def test_follows_a_change_of_windows_scaling(self) -> None:
        vlist = await self.make_list()
        vlist.set_rows(band_rows(3))
        await pump(self.root, 0.05)
        scaling = vlist.scale
        band_before = vlist.row_middle(-11) - vlist.row_middle(-10)
        font_before = vlist._band_font.cget("size")
        vlist._set_scaling(scaling * 2, scaling * 2)  # what CTk's ScalingTracker calls
        try:
            await pump(self.root, 0.05)
            self.assertAlmostEqual(vlist.scale, scaling * 2)
            band_after = vlist.row_middle(-11) - vlist.row_middle(-10)
            self.assertAlmostEqual(band_after / band_before, 2, delta=0.1)
            self.assertAlmostEqual(vlist._band_font.cget("size") / font_before, 2, delta=0.15)
            self.assertTrue(vlist.visible_keys())
        finally:
            vlist._set_scaling(scaling, scaling)

    async def test_thumb_colours_carry_white_initials(self) -> None:
        self.assertEqual(len(thumb_colours()), 10)
        for colour in thumb_colours():
            with self.subTest(colour=colour):
                self.assertGreaterEqual(contrast(colour, theme.ON_COLOR), 3)

    async def test_bands_scroll_smoothly(self) -> None:
        # 2,000 rows of bands and lines redraw within the 1,000-file budget.
        vlist = await self.make_list()
        rows = band_rows(500, per_band=3)
        self.assertGreaterEqual(len(rows), 2000)
        vlist.set_rows(rows)
        await pump(self.root, 0.05)
        slowest = 0.0
        for fraction in (0.1, 0.35, 0.6, 0.85, 1.0):
            vlist.scroll_to_fraction(fraction)
            start = time.perf_counter()
            vlist._redraw()
            slowest = max(slowest, time.perf_counter() - start)
        self.assertLess(slowest, 0.1)


QUEUE_SIZES = Sizes(
    band_title=21, band_sub=14, button=14, line=16, code=16, mono=13, state=13,
    button_height=34, band_gap=10, band_gap_below=False, band_radius=10,
    band_inset=16, poster=(58, 86),
)  # fmt: skip


def queue_rows() -> list[Row]:
    """A band that needs the owner, a ready one and a done one, each with a line."""
    rows: list[Row] = []
    states: tuple[tuple[Literal["needs", "ready", "done"], str], ...] = (
        ("needs", "Needs you"),
        ("ready", "Ready"),
        ("done", "Renamed"),
    )
    for index, (state, word) in enumerate(states):
        band = -(index + 10)
        actions = (("undo", "Undo"),) if state == "done" else (("rename", "Rename"),)
        rows.append(
            Row(
                key=band,
                kind="band",
                title=f"Show {index}",
                subtitle="Season 1 · 1 episode",
                dest=f"→ Series › Show {index}",
                badge=f"Show {index}",
                actions=actions,
                state=state,
                state_text=word,
                faded=state == "done",
            )
        )
        rows.append(
            Row(
                key=index + 1,
                kind="item",
                title=f"Episode {index}" if index else "Film (2024).mkv",
                code="S01E01" if index else "",
                faint=f"show.{index}.s01e01.mkv",
                faint_label="Now:",
                new=index == 0,  # a film's line: its new name
                status="ready",
                band=band,
                faded=state == "done",
            )
        )
    return rows


class QueueBandTests(TkCase):
    async def queue_list(self) -> VirtualList:
        holder = ctk.CTkFrame(self.root, width=900, height=600)
        holder.place(x=0, y=0)
        holder.pack_propagate(False)
        self.addCleanup(holder.destroy)
        vlist = VirtualList(
            holder, row_height=40, band_height=120, sizes=QUEUE_SIZES, hover_actions=True
        )
        vlist.pack(fill="both", expand=True)
        self.root.deiconify()
        self.root.geometry("950x650+20+20")
        vlist.set_rows(queue_rows())
        await pump(self.root, 0.05)
        return vlist

    def shown_actions(self, vlist: VirtualList) -> set[int]:
        return {key for *_box, key, _action in vlist._hits}

    async def test_buttons_show_under_the_pointer_and_always_when_it_needs_you(self) -> None:
        vlist = await self.queue_list()
        self.assertEqual(self.shown_actions(vlist), {-10})
        vlist.hover_band(-11)
        self.assertEqual(self.shown_actions(vlist), {-10, -11})
        # The pointer over a band's line counts as over the band.
        vlist.hover_band(None)
        vlist.canvas.event_generate("<Motion>", x=300, y=vlist.row_middle(3))
        await pump(self.root, 0.05)
        self.assertEqual(self.shown_actions(vlist), {-10, -12})
        vlist.canvas.event_generate("<Leave>")
        await pump(self.root, 0.05)
        self.assertEqual(self.shown_actions(vlist), {-10})

    async def test_the_status_is_a_coloured_dot_and_word_that_never_moves(self) -> None:
        vlist = await self.queue_list()
        canvas = vlist.canvas
        words = {cget(canvas, i, "text"): cget(canvas, i, "fill") for i in canvas.find_withtag("band-state")}
        self.assertEqual(list(words), ["Needs you", "Ready", "Renamed"])
        self.assertEqual(words["Needs you"].upper(), theme.pick(theme.WARNING).upper())
        self.assertEqual(words["Ready"].upper(), theme.pick(app_look().LINK).upper())
        dots = [cget(canvas, i, "fill") for i in canvas.find_withtag("band-dot")]
        self.assertEqual(dots[:2], [words["Needs you"], words["Ready"]])
        ready = [i for i in canvas.find_withtag("band-state") if cget(canvas, i, "text") == "Ready"]
        before = canvas.bbox(ready[0])
        vlist.hover_band(-11)  # its Rename shows: the word stays where it was
        ready = [i for i in canvas.find_withtag("band-state") if cget(canvas, i, "text") == "Ready"]
        self.assertEqual(canvas.bbox(ready[0]), before)

    async def test_the_destination_follows_the_subtitle_in_the_link_colour(self) -> None:
        vlist = await self.queue_list()
        canvas = vlist.canvas
        look = app_look()
        dests = {cget(canvas, i, "text"): cget(canvas, i, "fill") for i in canvas.find_withtag("band-dest")}
        self.assertEqual(dests["→ Series › Show 1"].upper(), theme.pick(look.LINK).upper())
        self.assertEqual(dests["→ Series › Show 2"], theme.pick(theme.TEXT_MUTED))  # faded

    async def test_a_done_band_is_faded(self) -> None:
        vlist = await self.queue_list()
        canvas = vlist.canvas
        titles = {cget(canvas, i, "text"): cget(canvas, i, "fill") for i in canvas.find_withtag("band-title")}
        self.assertEqual(titles["Show 2"], theme.pick(theme.TEXT_MUTED))

    async def test_no_coloured_edge_the_status_says_it(self) -> None:
        vlist = await self.queue_list()
        self.assertEqual(vlist.canvas.find_withtag("band-edge"), ())

    async def test_a_lines_current_name_is_labelled_and_muted(self) -> None:
        vlist = await self.queue_list()
        canvas = vlist.canvas
        labels = [cget(canvas, i, "text") for i in canvas.find_withtag("line-now")]
        self.assertEqual(labels, ["Now:", "Now:", "Now:"])
        for mode, dark in (("light", False), ("dark", True)):
            ctk.set_appearance_mode(mode)
            await pump(self.root, 0.05)
            olds = {cget(canvas, i, "fill") for i in canvas.find_withtag("line-old")}
            with self.subTest(mode=mode):
                self.assertEqual(olds, {theme.pick(theme.TEXT_MUTED, dark=dark)})
                [label, *_] = canvas.find_withtag("line-now")
                now_x = canvas.bbox(label)[2]
                old_x = min(canvas.bbox(i)[0] for i in canvas.find_withtag("line-old"))
                self.assertLessEqual(now_x, old_x + 2)  # in front of the name

    async def test_a_films_line_shows_its_new_name_after_a_link_coloured_arrow(self) -> None:
        vlist = await self.queue_list()
        canvas = vlist.canvas
        [arrow] = canvas.find_withtag("line-arrow")
        self.assertEqual(cget(canvas, arrow, "text"), "→ ")
        self.assertEqual(cget(canvas, arrow, "fill").upper(), theme.pick(app_look().LINK).upper())

    async def test_the_poster_has_the_designs_size(self) -> None:
        vlist = await self.queue_list()
        scale = vlist.scale
        self.assertEqual(vlist.poster_size, (round(58 * scale), round(86 * scale)))


class OptionButtonTests(TkCase):
    async def test_toggles_and_tells_every_click(self) -> None:
        heard: list[bool] = []
        button = OptionButton(self.root, "Poster", heard.append)
        button.pack()
        self.root.deiconify()
        await pump(self.root, 0.05)
        self.assertEqual(button.text, "Poster")
        self.assertFalse(button.value)
        self.assertEqual(button.canvas.find_withtag("glow"), ())
        button.canvas.event_generate("<Button-1>", x=10, y=10)
        self.assertEqual(heard, [True])
        self.assertTrue(button.canvas.find_withtag("glow"))  # on: the light glows
        button.set(False)  # quietly: no call
        self.assertEqual(heard, [True])
        self.assertFalse(button.value)
        button.invoke()
        self.assertEqual(heard, [True, True])

    async def test_style_d_in_both_looks(self) -> None:
        look = app_look()
        button = OptionButton(self.root, "Trailer", lambda value: None, look=look)
        button.pack()
        self.root.deiconify()
        await pump(self.root, 0.05)  # shown before a switch (CTk hides a window still hidden)
        for mode, dark in (("light", False), ("dark", True)):
            ctk.set_appearance_mode(mode)
            await pump(self.root, 0.05)
            off = option_style(look, on=False, hover=False)
            on = option_style(look, on=True, hover=False)
            hover = option_style(look, on=False, hover=True)
            with self.subTest(mode=mode):
                self.assertEqual(off.surface, on.surface)  # the same raised button either way
                self.assertNotEqual(hover.surface, off.surface)
                self.assertEqual(on.light, theme.pick(look.BRAND, dark=dark))
                self.assertEqual(on.text, theme.pick(theme.TEXT, dark=dark))
                self.assertEqual(off.text, theme.pick(theme.TEXT_MUTED, dark=dark))
                self.assertNotEqual(on.border, off.border)
                self.assertGreaterEqual(contrast(on.text, on.surface), 4.5)
                self.assertGreaterEqual(contrast(off.text, off.surface), 4.5)
                labels = [cget(button.canvas, i, "fill") for i in button.canvas.find_withtag("label")]
                self.assertEqual(labels, [off.text])

    async def test_sized_for_its_label(self) -> None:
        short = OptionButton(self.root, "Poster", lambda value: None)
        long = OptionButton(self.root, "Remove empty folders", lambda value: None)
        for button in (short, long):
            button.pack(side="left")
        self.root.deiconify()
        await pump(self.root, 0.05)
        self.assertLess(short.winfo_width(), long.winfo_width())
        self.assertEqual(short.winfo_height(), round(34 * short.scale))


class ForgeProgressTests(TkCase):
    async def test_fills_up_and_says_where_it_is(self) -> None:
        bar = ForgeProgress(self.root)
        bar.pack()
        self.root.deiconify()
        await pump(self.root, 0.05)
        bar.show(0.5, "Renaming 6 of 12 · Mind The Gap")
        canvas = bar.canvas

        def texts(tag: str) -> list[str]:
            return [cget(canvas, i, "text") for i in canvas.find_withtag(tag)]

        self.assertEqual(texts("title"), ["Renaming 6 of 12"])  # the step in bold
        self.assertEqual(texts("detail"), ["Mind The Gap"])  # its file beside it
        self.assertEqual(texts("percent"), ["50%"])
        [track] = canvas.find_withtag("track")
        [fill, _head] = canvas.find_withtag("fill")
        tx0, _y0, tx1, _y1 = canvas.bbox(track)
        x0, _y0, x1, _y1 = canvas.bbox(fill)
        self.assertAlmostEqual((x1 - x0) / (tx1 - tx0), 0.5, delta=0.05)
        bar.show(0, "Renaming 1 of 12")
        self.assertEqual(canvas.find_withtag("fill"), ())
        self.assertEqual(texts("detail"), [])
        bar.show(1, "x" * 200)  # too long: cut to fit
        [label] = texts("title")
        self.assertTrue(label.endswith("…"))
        # Room kept for a button at its right end: the bar ends before it.
        bar.set_action_room(100)
        [track] = canvas.find_withtag("track")
        self.assertLess(canvas.bbox(track)[2], tx1 - 90 * bar.scale)


class SparedWorkTests(TkCase):
    """F-33: what a refresh of the queue (1,000 files, lookups landing) doesn't do again."""

    async def test_the_same_rows_again_draw_nothing(self) -> None:
        vlist = await self.make_list()
        vlist.set_rows(many_rows(50))
        await pump(self.root, 0.05)
        with mock.patch.object(vlist, "_redraw", wraps=vlist._redraw) as redraw:
            vlist.set_rows(many_rows(50))  # equal rows, new objects
            redraw.assert_not_called()
            vlist.set_rows(many_rows(51))
            redraw.assert_called_once()
        self.assertEqual(vlist.rows, many_rows(51))

    async def test_the_scroll_bar_is_set_only_when_the_view_changes(self) -> None:
        # CTk's scroll bar redraws itself and runs the window's pending idle work on every set.
        vlist = await self.make_list()
        rows = many_rows(100)
        vlist.set_rows(rows)
        await pump(self.root, 0.05)
        with mock.patch.object(vlist.scrollbar, "set") as set_bar:
            renamed = [r.model_copy(update={"title": r.title + " (new)"}) for r in rows]
            vlist.set_rows(renamed)  # other texts, the same heights
            await pump(self.root, 0.05)
            set_bar.assert_not_called()
            vlist.scroll_to_fraction(0.5)
            await pump(self.root, 0.05)
            set_bar.assert_called()
            calls = set_bar.call_count
            vlist._on_yscroll(*vlist.canvas.yview())  # Tk telling the same view again
            self.assertEqual(set_bar.call_count, calls)

    async def test_texts_are_measured_once_until_the_fonts_change(self) -> None:
        vlist = await self.make_list()
        vlist.set_rows(band_rows(3))
        await pump(self.root, 0.05)
        real = tkfont.Font.measure
        measured: list[str] = []

        def measure(font: tkfont.Font, text: str, *args: object) -> int:
            measured.append(text)
            return real(font, text)

        with mock.patch.object(tkfont.Font, "measure", measure):
            vlist._redraw()
            self.assertEqual(measured, [])
            vlist._apply_scale()  # Windows' scaling changed: new fonts
            vlist._redraw()
            self.assertNotEqual(measured, [])
