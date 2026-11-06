"""The right-click confirm window (ported from MediaForge's ui/confirm_window.py): "Rename N
files?", every file's new name over its old one, nothing changes until Rename.

A row per file with a tick box (grouped by destination folder, `queue_rows.build_rows`); files
TMDB isn't sure about say so and "Change match…" opens the match picker for the chosen row; the
preset can be switched; "Open in MediaForge" hands the files to MediaForge's page instead; Cancel;
"Rename N files"; then the result with Undo and Close.

Built on AirGrabber's dialog kit in the Forge look: no title bar (app/ui/borderless.py, the
title as a heading), resizable by its edges, accent buttons. The list is a VirtualList, so
hundreds of files stay quick. Never `transient` and never grabbing: it must show on its own
while AirGrabber's window is in the tray (then it opens on the screen under the mouse pointer),
and several can be open at once (one per preset). The window has its own Queue, apart from the
Inbox's; a rename takes Forge's run lock, so it never runs alongside another run.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.forge.core.library import library_folder, route_inputs
from app.forge.core.planner import build_plan
from app.forge.core.queue import ItemStatus, Queue, QueueEvent, QueueItem
from app.forge.models.history import RunSource
from app.forge.models.preset import Preset, with_options
from app.forge.ui.queue_rows import build_rows
from app.forge.ui.look import app_look
from app.forge.ui.match_picker import MatchPicker
from app.forge.ui.widgets.virtual_list import VirtualList
from app.i18n import N_, t, tn
from app.ui import theme
from app.ui.borderless import clearance, make_borderless
from app.ui.branding import set_own_icon
from app.ui.dialog_kit import (
    BUTTON_RADIUS,
    LINE_GAP,
    WINDOW_FRAME,
    heading_row,
    on_screen,
    primary_button,
    screen_area,
    secondary_button,
)
from app.ui.tasks import spawn
from app.ui.tooltip import work_area

if TYPE_CHECKING:
    from app.forge.services import ForgeServices

log = logging.getLogger(__name__)

STAYS_HINT = N_("Nothing changes until you click Rename.")
MOVES_HINT = N_("These files move to your library folder. Nothing changes until you click Rename.")
NO_FOLDER = N_("Choose a library folder in Settings › Auto-rename first.")

ICON_FILE = Path(__file__).resolve().parents[2] / "assets" / "forge" / "MediaForge.ico"
WIDTH, HEIGHT = 860, 600  # CTk units
MIN_WIDTH, MIN_HEIGHT = 700, 380
_PAD_X = 25  # as Forge's other dialogs
_PAD_Y = 20
_BUTTON_GAP = 8
_DETAIL_SIZE = 13
_REFRESH_MS = 80  # queue events within this time are drawn once


class ConfirmWindow(ctk.CTkToplevel):
    """`ConfirmWindow(master, services, paths, preset_name)`; `await window.loaded()` waits for
    the file names and lookups. The host sets `on_closed` (the window went: D5's "quit when the
    last one closes") and `on_open_in_forge` (paths, preset name)."""

    def __init__(
        self,
        master: Any,
        services: ForgeServices,
        paths: Sequence[Path],
        preset_name: str | None,
        *,
        look: theme.Look | None = None,
    ) -> None:
        self._look: theme.Look = app_look() if look is None else look
        p = self._look
        super().__init__(master, fg_color=p.BG)
        self.services = services
        self.paths = list(paths)
        self.presets: list[Preset] = []
        self.preset: Preset | None = None
        self.run_id: int | None = None
        self.busy = False  # renaming or undoing; extra clicks are ignored
        self.on_closed: Callable[[ConfirmWindow], None] = lambda win: None
        self.on_open_in_forge: Callable[[list[Path], str | None], None] = lambda p, n: None
        self._closed = False
        self._ticks: dict[int, bool] = {}
        self._targets: dict[int, Path] = {}
        self._refresh_id: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.queue = Queue(services.resolver, self._on_event)

        self.title(t("MediaForge · Confirm rename"))
        set_own_icon(self, ICON_FILE)  # MediaForge's, in the taskbar and Alt+Tab
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _event: self.cancel())

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=_PAD_X, pady=_PAD_Y)
        self._heading_row = heading_row(body, t("Reading file names…"), right_room=clearance(_PAD_X))
        self._heading_row.pack(fill="x")
        self.heading: ctk.CTkLabel = self._heading_row.grid_slaves(row=0, column=0)[0]

        line = ctk.CTkFrame(body, fg_color="transparent")
        line.pack(fill="x", pady=(LINE_GAP, 12))
        self.hint = ctk.CTkLabel(
            line,
            text=t(STAYS_HINT),
            font=theme.font(_DETAIL_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        )
        self.hint.pack(side="left")
        self.preset_menu = ctk.CTkOptionMenu(
            line,
            values=["…"],
            width=200,
            height=34,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(_DETAIL_SIZE),
            dropdown_font=theme.font(_DETAIL_SIZE),
            fg_color=p.SURFACE,
            button_color=p.CHIP,  # the arrow keeps BUTTON_TEXT's contrast in both looks
            button_hover_color=p.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            dropdown_fg_color=p.SURFACE,
            dropdown_hover_color=p.MENU_HOVER,
            dropdown_text_color=theme.TEXT,
            command=self.select_preset,
        )
        self.preset_menu.pack(side="right")
        ctk.CTkLabel(
            line,
            text=t("Preset"),
            font=theme.font(_DETAIL_SIZE),
            text_color=theme.TEXT_MUTED,
        ).pack(side="right", padx=(0, 8))

        # Only the rows on screen are drawn, so hundreds of files stay quick.
        frame = ctk.CTkFrame(
            body, fg_color=p.BG, border_width=1, border_color=p.EDGE, corner_radius=BUTTON_RADIUS
        )
        frame.pack(fill="both", expand=True)
        self.list = VirtualList(frame, look=p, removable=False)
        self.list.pack(fill="both", expand=True, padx=2, pady=BUTTON_RADIUS // 2)
        self.list.on_toggle = self._on_toggle
        self.list.on_select = lambda key: self._update_counts()

        self.footer = ctk.CTkFrame(body, fg_color="transparent")
        self.footer.pack(fill="x", pady=(16, 0))
        self.selected_label = ctk.CTkLabel(
            self.footer, text="", font=theme.font(_DETAIL_SIZE), text_color=theme.TEXT_MUTED
        )
        self.selected_label.pack(side="left")
        self.result_label = ctk.CTkLabel(
            self.footer,
            text="",
            font=theme.font(15, bold=True),
            text_color=theme.SUCCESS,
            anchor="w",
        )
        self.rename_button = primary_button(
            self.footer, t("Rename"), self._command(self.rename), width=170, look=p
        )
        self.rename_button.pack(side="right")
        self.cancel_button = secondary_button(self.footer, t("Cancel"), self.cancel, width=100, look=p)
        self.cancel_button.pack(side="right", padx=(0, _BUTTON_GAP))
        self.open_button = secondary_button(
            self.footer, t("Open in MediaForge"), self.open_in_forge, width=180, look=p
        )
        self.open_button.pack(side="right", padx=(0, _BUTTON_GAP))
        self.change_button = secondary_button(
            self.footer,
            t("Change match…"),
            self._command(self.change_selected),
            width=150,
            look=p,
        )
        self.change_button.pack(side="right", padx=(0, _BUTTON_GAP))
        self.undo_button = secondary_button(
            self.footer, t("Undo"), self._command(self.undo), width=100, look=p
        )
        self.close_button = primary_button(self.footer, t("Close"), self.close, width=120, look=p)
        for button in (self.rename_button, self.change_button):
            button.configure(state="disabled")

        make_borderless(
            self, drag_widgets=(self, body, self._heading_row, line, self.footer), look=p
        )
        self._place(master)
        self.after(150, self._activate)
        self._load_task = self._spawn(self._load(preset_name))

    # --- plumbing --------------------------------------------------------------------------

    def _place(self, master: Any) -> None:
        """Over AirGrabber's window when it shows; else (in the tray) in the middle of the
        screen under the mouse pointer. Always fully on that screen."""
        scale = ctk.ScalingTracker.get_window_scaling(self)
        try:
            shown = bool(master.winfo_viewable())
        except tk.TclError:
            shown = False
        if shown:
            area = screen_area(master)
        else:
            area = work_area(*self.winfo_pointerxy()) or screen_area(self)
        left, top, right, bottom = area
        room_h = (bottom - top) / scale - WINDOW_FRAME - 32
        width, height = WIDTH, max(MIN_HEIGHT, min(HEIGHT, int(room_h)))
        w, h = round(width * scale), round(height * scale)
        if shown:
            x = master.winfo_rootx() + (master.winfo_width() - w) // 2
            y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        else:
            x, y = left + (right - left - w) // 2, top + (bottom - top - h) // 3
        frame = round(WINDOW_FRAME * scale)
        x, y = on_screen(x, y, w, h + frame, area)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _activate(self) -> None:
        # No grab: other confirm windows and AirGrabber's own stay usable.
        if not self._closed and self.winfo_exists():
            self.lift()
            self.focus_force()

    def _command(self, action: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
        """A button's command that starts `action` as one of this window's tasks."""

        def run() -> None:
            self._spawn(action())

        return run

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = spawn(coro, name="ConfirmWindow")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def loaded(self) -> None:
        """Wait until every file is read and looked up, and the rows are drawn."""
        await self._load_task
        await self.queue.wait_idle()
        self.refresh()

    async def _load(self, preset_name: str | None) -> None:
        self.presets = await self.services.presets.list()
        if self._closed:
            return
        names = [p.name for p in self.presets]
        self.preset_menu.configure(values=names or ["…"])
        settings = self.services.settings.current
        wanted = preset_name if preset_name in names else settings.last_preset
        if names:
            self.select_preset(wanted if wanted in names else names[0], reload=False)
        if self.preset is not None:
            await self.queue.set_hint(self.preset.type)
        await self.queue.add_paths(self.paths, settings.scan_subfolders)
        log.info("Confirm window opened for %d file(s)", len(self.queue.items))
        self._schedule_refresh()

    def select_preset(self, name: str, reload: bool = True) -> None:
        preset = next((p for p in self.presets if p.name == name), None)
        if preset is None or self.busy:
            if self.preset is not None:
                self.preset_menu.set(self.preset.name)
            return
        self.preset = preset
        self.preset_menu.set(preset.name)
        if reload:
            self._spawn(self.queue.set_hint(preset.type))
        self._schedule_refresh()

    def _on_event(self, event: QueueEvent) -> None:
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._closed:
            return
        if self._refresh_id is not None:
            self.after_cancel(self._refresh_id)
        self._refresh_id = self.after(_REFRESH_MS, self.refresh)

    # --- rows ------------------------------------------------------------------------------

    def refresh(self) -> None:
        self._refresh_id = None
        if self._closed or not self.winfo_exists() or self.preset is None:
            return
        items = self.queue.items
        rows, self._targets = build_rows(items, self.preset.rule, self._root_of)
        if self.run_id is None:  # after a run the hint says it's done
            self.hint.configure(
                text=t(MOVES_HINT if self.preset.options.move_to_library else STAYS_HINT)
            )
        by_id = {item.id: item for item in items}
        self._ticks = {item.id: self._ticks.get(item.id, True) for item in items}
        shown = []
        for row in rows:
            item = by_id.get(row.key)
            if item is None:
                shown.append(row)
                continue
            changes: dict[str, Any] = {"checked": self._ticks[item.id]}
            if item.status is ItemStatus.NEEDS_CHOICE:
                changes["subtitle"] = t(
                    "TMDB isn't sure, using {title}  ·  {old}", title=item.media.ny, old=row.subtitle
                )
            shown.append(row.model_copy(update=changes))
        self.list.set_rows(shown)
        self.heading.configure(
            text=tn("Rename {n} file?", "Rename {n} files?", len(items))
        )
        self._update_counts()

    def _root_of(self, item: QueueItem) -> Path:
        """Where a file's naming rule starts: its library folder when the preset moves it."""
        if self.preset is None or not self.preset.options.move_to_library:
            return item.root
        settings = self.services.settings.current
        return library_folder(item.media.kind, settings) or item.root

    def _on_toggle(self, item_id: int, ticked: bool) -> None:
        self._ticks[item_id] = ticked
        self._update_counts()

    def _update_counts(self) -> None:
        if self._closed:
            return
        chosen = len(self.chosen_ids())
        self.selected_label.configure(
            text=t("{chosen} of {total} selected", chosen=chosen, total=len(self._ticks))
        )
        self.rename_button.configure(
            text=tn("Rename {n} file", "Rename {n} files", chosen),
            state="normal" if chosen and not self.busy else "disabled",
        )
        picked = self.list.selected is not None and not self.busy
        self.change_button.configure(state="normal" if picked else "disabled")

    def chosen_ids(self) -> list[int]:
        return [item_id for item_id, ticked in self._ticks.items() if ticked]

    def new_names(self) -> list[str]:
        return [self._targets[i.id].name for i in self.queue.items if i.id in self._targets]

    def uncertain_ids(self) -> list[int]:
        return [i.id for i in self.queue.items if i.status is ItemStatus.NEEDS_CHOICE]

    def set_ticked(self, path: Path, ticked: bool) -> None:
        for item in self.queue.items:
            if item.path == path and item.id in self._ticks:
                self._ticks[item.id] = ticked
        self.refresh()

    async def change_selected(self) -> None:
        item = next((i for i in self.queue.items if i.id == self.list.selected), None)
        if item is not None and not self.busy:
            await self._change(item)

    async def _change(self, item: QueueItem) -> None:
        choice = await MatchPicker.ask(self, item, self.services, look=self._look)
        if choice != "cancel" and not self._closed:
            await self.queue.choose_match(item.id, choice)

    # --- actions ---------------------------------------------------------------------------

    async def rename(self) -> None:
        if self.busy or self.preset is None or self.run_id is not None:
            return
        self.busy = True
        try:
            await self._rename(self.preset)
        finally:
            self.busy = False
            if not self._closed and self.run_id is None:
                self._update_counts()

    def _set_buttons(self, state: str) -> None:
        for widget in (self.rename_button, self.cancel_button, self.open_button, self.change_button):
            widget.configure(state=state)
        self.preset_menu.configure(state=state)

    async def _rename(self, preset: Preset) -> None:
        self._set_buttons("disabled")
        await self.queue.wait_idle()
        ids = self.chosen_ids()
        if not ids or self._closed:
            self._set_buttons("normal")
            return
        chosen = set(ids)
        options = preset.options
        if options.trailer and not self.services.trailers.tools_present():
            options = options.model_copy(update={"trailer": False})
            log.info("Trailers skipped: the trailer tools aren't downloaded yet")
        inputs, left = route_inputs(
            self.queue.plan_inputs(ids), options, self.services.settings.current
        )
        if not inputs:  # left alone until the owner has chosen where they go
            self.result_label.configure(text=t(NO_FOLDER), text_color=theme.WARNING)
            self.result_label.pack(side="left", fill="x", expand=True)
            self._set_buttons("normal")
            return
        plan = await asyncio.to_thread(build_plan, inputs, with_options(preset.format, options), options)
        async with self.services.run_lock:  # never alongside the renamer's or the Inbox's run
            result = await self.services.executor.run(
                plan,
                source=RunSource.RIGHT_CLICK,
                preset_name=preset.name,
                on_progress=lambda event: None,
                cancel=asyncio.Event(),
                kinds={item.media.kind for item in self.queue.items if item.id in chosen},
                media={i.item_id: i.media for i in inputs},  # for the move records
            )
        self.run_id = result.run_id
        parts = [
            t("{n} moved", n=result.renamed)
            if options.move_to_library
            else t("{n} renamed", n=result.renamed)
        ]
        if left:
            parts.append(t("{n} left alone: choose a library folder first", n=len(left)))
        if result.skipped:
            parts.append(t("{n} already right", n=result.skipped))
        if result.failed:
            parts.append(t("{n} failed", n=result.failed))
        summary = ", ".join(parts)
        log.info(
            "Right-click rename: %d renamed, %d already right, %d failed",
            result.renamed,
            result.skipped,
            result.failed,
        )
        if self._closed or not self.winfo_exists():
            return
        self.result_label.configure(
            text=summary, text_color=theme.DANGER if result.failed else theme.SUCCESS
        )
        for widget in (
            self.selected_label,
            self.rename_button,
            self.cancel_button,
            self.open_button,
            self.change_button,
        ):
            widget.pack_forget()
        self.preset_menu.configure(state="disabled")
        self.hint.configure(text=t("Done. You can undo this here or later in History."))
        self.result_label.pack(side="left", fill="x", expand=True)
        self.close_button.pack(side="right")
        if self.run_id is not None:
            self.undo_button.pack(side="right", padx=(0, _BUTTON_GAP))

    async def undo(self) -> None:
        if self.busy or self.run_id is None:
            return
        run_id, self.run_id = self.run_id, None
        self.busy = True
        self.undo_button.configure(state="disabled")
        try:
            result = await self.services.undo.undo_run(run_id)
        finally:
            self.busy = False
        if self._closed or not self.winfo_exists():
            return
        self.undo_button.pack_forget()
        text = tn("Put back {n} change", "Put back {n} changes", result.undone)
        if result.skipped:
            text += " · " + tn(
                "{n} left as it is: {reason}",
                "{n} left as they are, such as: {reason}",
                len(result.skipped),
                reason=t(result.skipped[0][1]),  # the journal's English reason, translated
            )
        self.result_label.configure(text=text, text_color=theme.TEXT_MUTED)

    def open_in_forge(self) -> None:
        if self.busy:
            return
        self.on_open_in_forge(list(self.paths), self.preset.name if self.preset else None)
        self.close()

    def cancel(self) -> None:
        if self.busy:
            return  # renaming: closing now would cut the run short
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self.busy:
            return
        self._closed = True
        self.queue.clear()
        try:
            self.on_closed(self)
        except Exception:
            log.exception("The confirm window's close callback failed")
        try:
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass  # gone with its interpreter

    def destroy(self) -> None:
        self._closed = True
        if self._refresh_id is not None:
            try:
                self.after_cancel(self._refresh_id)
            except tk.TclError:
                pass
            self._refresh_id = None
        current = asyncio.current_task() if _loop_running() else None
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        super().destroy()


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
