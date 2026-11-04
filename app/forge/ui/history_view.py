"""History: the last 50 runs, grouped by day (ported from MediaForge's ui/history_view.py). Its
heading and muted line are on the main window's tabs row (app/forge/ui/page.py).

Any kept run can be undone, not only the latest and in any order: each run's band has its own
"Undo run", and "Show files" lets you tick single files of it ("Undo N selected"). A run reads
as partly or fully undone afterwards; files that can't be undone say why. Every run shows
(`kind` "all"); `set_kind("series" / "movies")` shows only the runs that renamed that kind (runs
from before MediaForge 1.3 have no kinds and show on both sides). `busy` is True while an undo
runs (the page asks it before AirGrabber quits).

A video's row has CC ▾ (app/forge/ui/subtitle_control.py), drawn on the list's canvas: greyed with
the reason as its tip while the video is no longer where this run put it or today's allowance is
used up. A subtitle got that way is journaled in the run, so it shows here and Undo removes it.
A subtitle the Subtitles extra couldn't get shows as a line saying why.

Everything goes through `ForgeServices`' `history` (read) and `undo` services. The journal's
notes are English on purpose and are shown through `t()` (an OS error's text passes unchanged).
Colours are the Forge look's (light, dark) pairs, so CTk repaints them on a light/dark switch;
the file lists (VirtualList) repaint their canvas themselves.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Hashable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.forge.core import fsops
from app.forge.core.parser import is_video
from app.forge.core.subtitles import file_identity
from app.forge.models.history import (
    Operation,
    OpKind,
    OpState,
    RunSource,
    RunSummary,
    UndoState,
)
from app.forge.models.media import MediaKind
from app.forge.ui.look import app_look
from app.forge.ui.subtitle_control import CC_ACTION, CC_LABEL, NO_VIDEO_TEXT, SubtitleDesk, VideoTarget
from app.forge.ui.widgets.dialogs import ask_choice, confirm
from app.forge.ui.widgets.virtual_list import Row, VirtualList
from app.i18n import N_, day_short, month_short, t, tn
from app.ui import tasks, theme
from app.ui.missing_control import popup_menu
from app.ui.scrollable import ScrollableFrame

if TYPE_CHECKING:
    from app.forge.core.undo import UndoResult
    from app.forge.services import ForgeServices

log = logging.getLogger(__name__)

NOTHING_YET = N_("Nothing here yet. Runs show up after you Forge some files.")
SKIPPED_SHOWN = 15  # the most skipped files listed in the report

_HIDDEN_KINDS = (OpKind.CREATE_DIR, OpKind.REMOVE_DIR)  # undone along with the files
_SOURCE_TAGS = {
    RunSource.RIGHT_CLICK: N_("right-click"),
    RunSource.IMPORT: N_("imported"),
    RunSource.AUTOMATIC: N_("automatic"),
}


def _short_date(dt: datetime, *, year: bool) -> str:
    """ "29 Oct" / "29 okt", with the year when asked."""
    text = f"{dt.day} {month_short(dt)}"
    return f"{text} {dt.year}" if year else text


def when_label(dt: datetime, now: datetime) -> str:
    """ "Today 14:02", "Yesterday 21:40", "19 Oct 22:23", "4 Mar 2025 08:05"."""
    time = f"{dt:%H:%M}"
    if dt.date() == now.date():
        return t("Today {time}", time=time)
    if dt.date() == (now - timedelta(days=1)).date():
        return t("Yesterday {time}", time=time)
    return f"{_short_date(dt, year=dt.year != now.year)} {time}"


def day_label(dt: datetime, now: datetime) -> str:
    """A day heading: "Today", "Yesterday", "Thu 29 Oct", "Tue 4 Mar 2025"."""
    if dt.date() == now.date():
        return t("Today")
    if dt.date() == (now - timedelta(days=1)).date():
        return t("Yesterday")
    return f"{day_short(dt)} {_short_date(dt, year=dt.year != now.year)}"


def _subtitle_not_there(op: Operation) -> bool:
    """A line of the Subtitles extra that got nothing (its `error` says why)."""
    return (
        op.kind is OpKind.CREATE_FILE
        and op.state is OpState.FAILED
        and bool(op.error)
        and Path(op.dst or "").suffix.casefold() == ".srt"
    )


def _describe(op: Operation) -> tuple[str, str]:
    """The name to show and a short note under it."""
    src = Path(op.src).name if op.src else ""
    dst = Path(op.dst).name if op.dst else ""
    match op.kind:
        case OpKind.RENAME:
            return dst, t("was {name}", name=src)
        case OpKind.CREATE_FILE:
            return dst, t("subtitle") if _subtitle_not_there(op) else t("downloaded")
        case OpKind.DELETE_FILE:
            return src, t("junk file, deleted")
        case OpKind.CREATE_DIR:
            return dst, t("new folder")
        case OpKind.REMOVE_DIR:
            return src, t("empty folder, removed")


def _not_undoable_reason(op: Operation) -> str | None:
    if op.kind is OpKind.DELETE_FILE:
        return t("can't be restored")
    if _subtitle_not_there(op):
        return t(op.error or "")  # the journal's English note
    if op.state is not OpState.DONE:
        return t("didn't happen")
    if op.undo_state is UndoState.DONE:
        return t("already undone")
    return None


def _has_video(op: Operation) -> bool:
    """A rename of a video that happened: its row has CC ▾."""
    return (
        op.kind is OpKind.RENAME
        and op.state is OpState.DONE
        and bool(op.dst)
        and is_video(Path(op.dst or ""))
    )


def _gone_videos(videos: Sequence[Operation]) -> set[int]:
    """The renames whose video is no longer where they put it. Blocking: run it in a thread."""
    return {op.id for op in videos if not fsops.exists(Path(op.dst or ""))}


class RunCard(ctk.CTkFrame):
    """One run's band: its time, what it did, a tag, Show files and Undo run."""

    def __init__(
        self, master: Any, view: HistoryView, run: RunSummary, undone_at: datetime | None
    ) -> None:
        look = view.look
        super().__init__(master, fg_color=look.SURFACE, corner_radius=8, border_width=0)
        self.view = view
        self.run = run
        self.undone_at = undone_at
        self.operations: list[Operation] = []
        self.checks: dict[int, bool] = {}
        self.files: VirtualList | None = None
        self.selected_button: ctk.CTkButton | None = None
        self.expanded = False
        self._gone: set[int] = set()  # the rows whose video is no longer where this run put it
        dim = run.status == "undone"
        text_colour = theme.TEXT_MUTED if dim else theme.TEXT

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=8)
        head.grid_columnconfigure(1, weight=1)
        # A band, as in the Inbox: an accent stripe, the time large (the day is the heading).
        ctk.CTkFrame(
            self, width=4, fg_color=look.PLACEHOLDER if dim else look.ACCENT, corner_radius=2
        ).place(x=3, rely=0.2, relheight=0.6)
        ctk.CTkLabel(
            head,
            text=f"{run.started_at:%H:%M}",
            width=70,
            anchor="w",
            font=theme.font(15, bold=True),
            text_color=text_colour,
        ).grid(row=0, column=0, sticky="w")
        self.summary_label = ctk.CTkLabel(
            head, text=self.summary_text(), anchor="w", font=theme.font(12), text_color=text_colour
        )
        self.summary_label.grid(row=0, column=1, sticky="ew")
        tag = self.tag_text()
        if tag:
            ctk.CTkLabel(
                head,
                text=tag,
                font=theme.font(10),
                text_color=theme.TEXT_MUTED,
                fg_color=look.CHIP,
                corner_radius=10,
            ).grid(row=0, column=2, padx=8, ipadx=6)
        self.toggle_button = ctk.CTkButton(
            head,
            text=t("Show files ▾"),
            width=100,
            height=26,
            fg_color="transparent",
            border_width=1,
            border_color=look.PLACEHOLDER,
            text_color=theme.TEXT_MUTED if dim else theme.BUTTON_TEXT,
            hover_color=look.CHIP,
            font=theme.font(11),
            command=lambda: view.spawn(self.toggle()),
        )
        self.toggle_button.grid(row=0, column=3, padx=(0, 8))
        can_undo = run.undoable_count > 0
        self.undo_button = ctk.CTkButton(
            head,
            text=t("Undo run"),
            width=86,
            height=26,
            fg_color=look.ACCENT if can_undo else look.CHIP,
            text_color=theme.ON_ACCENT if can_undo else theme.TEXT_MUTED,
            text_color_disabled=theme.TEXT_MUTED,
            hover_color=look.ACCENT_HOVER,
            font=theme.font(11, bold=True),
            command=lambda: view.spawn(view.undo_run(run.id)),
            state="normal" if can_undo else "disabled",
        )
        self.undo_button.grid(row=0, column=4)
        self.body = ctk.CTkFrame(self, fg_color="transparent")

    def summary_text(self) -> str:
        run = self.run
        parts = [
            tn("{n} file", "{n} files", run.file_count),
            run.preset_name or t("no preset"),
        ]
        if run.created_count:
            parts.append(t("{n} downloaded", n=run.created_count))
        return " · ".join(parts)

    def tag_text(self) -> str:
        if self.run.status == "undone":
            if self.undone_at:
                return t("Undone {date}", date=_short_date(self.undone_at, year=False))
            return t("Undone")
        if self.run.status == "partly-undone":
            return t("Partly undone")
        tag = _SOURCE_TAGS.get(self.run.source)
        return t(tag) if tag else ""

    async def toggle(self) -> None:
        if self.expanded:
            self.body.pack_forget()
            self.expanded = False
            self.toggle_button.configure(text=t("Show files ▾"))
        else:
            await self.expand()

    def _build_body(self) -> VirtualList:
        look = self.view.look
        files = VirtualList(self.body, row_height=40, look=look, removable=False, thumbnails=False)
        files.pack(fill="x")
        files.on_toggle = self.tick
        files.on_action = self.file_action
        footer = ctk.CTkFrame(self.body, fg_color="transparent")
        footer.pack(fill="x", pady=(6, 0))
        self.selected_button = ctk.CTkButton(
            footer,
            text="",
            height=26,
            fg_color="transparent",
            border_width=1,
            border_color=look.PLACEHOLDER,
            text_color=theme.BUTTON_TEXT,
            text_color_disabled=theme.TEXT_MUTED,
            hover_color=look.CHIP,
            font=theme.font(11),
            command=lambda: self.view.spawn(self.view.undo_selected(self.run.id)),
        )
        self.selected_button.pack(side="right")
        return files

    async def expand(self) -> None:
        operations = await self.view.services.history.operations(self.run.id)
        if not self.winfo_exists():
            return
        self.operations = operations
        videos = [op for op in operations if _has_video(op)]
        self._gone = set()
        if videos and self.view.services.subtitles.available:
            self._gone = await asyncio.to_thread(_gone_videos, videos)
            if not self.winfo_exists():
                return
        if self.files is None:
            self.files = self._build_body()
        self.checks = {
            op.id: _not_undoable_reason(op) is None
            for op in self.operations
            if op.kind not in _HIDDEN_KINDS
        }
        # Only the rows on screen are drawn, so a run with hundreds of files opens at once.
        self.files.set_rows(self._rows())
        self.files.fit_height(320)
        self._update_count()
        self.body.pack(fill="x", padx=(40, 12), pady=(0, 10))
        self.expanded = True
        self.toggle_button.configure(text=t("Hide files ▴"))

    def _rows(self) -> list[Row]:
        return [self._row(op) for op in self.operations if op.kind not in _HIDDEN_KINDS]

    def _row(self, op: Operation) -> Row:
        reason = _not_undoable_reason(op)
        title, note = _describe(op)
        subtitle = f"{note}  ·  {reason}" if reason else note
        actions: tuple[tuple[str, str], ...] = ()
        greyed: tuple[tuple[str, str], ...] = ()
        if _has_video(op):
            gone = t(NO_VIDEO_TEXT) if op.id in self._gone else ""
            look = self.view.subtitle_desk.look(op.id, gone=gone)
            if look is not None:
                actions = ((CC_ACTION, CC_LABEL),)
                greyed = ((CC_ACTION, look.tip),) if look.greyed else ()
                subtitle = f"{subtitle}  ·  {look.note}" if look.note else subtitle
        return Row(
            key=op.id,
            kind="item",
            title=title,
            subtitle=subtitle,
            checked=self.checks.get(op.id, False) if reason is None else False,
            check_disabled=reason is not None,
            actions=actions,
            greyed=greyed,
        )

    def refresh_rows(self) -> None:
        """The rows again (a subtitle request's state changed); nothing is drawn when they are
        the same."""
        if self.files is not None and self.expanded:
            self.files.set_rows(self._rows())

    def cc_items(self, op_id: int) -> list[tuple[str, Callable[[], None]]]:
        """The CC menu of a video's row."""
        op = next((o for o in self.operations if o.id == op_id), None)
        if op is None or not _has_video(op):
            return []

        async def targets() -> list[VideoTarget]:
            return [await self.view.video_target(op)]

        return self.view.subtitle_desk.menu(op_id, targets)

    def file_action(self, op_id: int, action: str) -> None:
        if action != CC_ACTION or self.files is None:
            return
        box = self.files.action_box(op_id, CC_ACTION)
        under = self.files.screen_box(box) if box is not None else None
        popup_menu(
            self.view,
            self.cc_items(op_id),
            under=under,
            point=None if under is not None else self.winfo_pointerxy(),
            look=self.view.look,
            align="left",
        )

    async def subtitles_changed(self) -> None:
        """A request of this run finished: what the journal holds now, its rows and its line."""
        if self.expanded:
            await self.expand()
        for run in await self.view.services.history.list_runs():
            if run.id == self.run.id and self.winfo_exists():
                self.run = run
                self.summary_label.configure(text=self.summary_text())

    def tick(self, op_id: int, ticked: bool) -> None:
        if op_id not in self.checks:
            return
        self.checks[op_id] = ticked
        if self.files is not None:
            rows = [
                row.model_copy(update={"checked": ticked})
                if row.key == op_id and not row.check_disabled
                else row
                for row in self.files.rows
            ]
            self.files.set_rows(rows)
        self._update_count()

    def selected_ids(self) -> list[int]:
        return [op_id for op_id, ticked in self.checks.items() if ticked]

    def _update_count(self) -> None:
        if self.selected_button is None:
            return
        n = len(self.selected_ids())
        self.selected_button.configure(
            text=t("Undo {n} selected", n=n), state="normal" if n else "disabled"
        )


class HistoryView(ctk.CTkFrame):
    """History on MediaForge's page."""

    def __init__(
        self, master: Any, services: ForgeServices, *, look: theme.Look | None = None
    ) -> None:
        self.look: theme.Look = app_look() if look is None else look
        super().__init__(master, fg_color=self.look.BG, corner_radius=0)
        self.services = services
        self.cards: list[RunCard] = []
        self.kind = "all"  # "all", or only the runs that renamed "series" or "movies"
        self._headings: list[tuple[str, ctk.CTkLabel]] = []
        self._card_days: list[str] = []
        self._generation = 0  # bumped by each reload; an older reload stops building
        self.busy = False  # an undo is going; further clicks are ignored
        self._tasks: set[asyncio.Task[Any]] = set()
        # The questions, replaceable in tests.
        self.confirm: Callable[[str, str], Awaitable[bool]] = lambda title, message: confirm(
            self, title, message, t("Undo"), look=self.look
        )
        self.inform: Callable[[str, str], Awaitable[None]] = self._inform
        self.subtitle_desk = SubtitleDesk(
            services.subtitles,
            spawn=self.spawn,
            after=self.after,
            on_change=self._cc_changed,
            on_done=self._cc_done,
        )

        self.list = ScrollableFrame(
            self,
            fg_color=self.look.BG,
            corner_radius=0,
            scrollbar_button_color=self.look.SCROLLBAR,
            scrollbar_button_hover_color=self.look.EDGE,
        )
        self.list.pack(fill="both", expand=True, pady=(4, 0))
        self.empty_label = ctk.CTkLabel(
            self.list, text=t(NOTHING_YET), font=theme.font(12), text_color=theme.TEXT_MUTED
        )

    async def _inform(self, title: str, message: str) -> None:
        await ask_choice(self, title, message, [t("OK")], look=self.look)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Start `coro` (logged and reported by app.ui.tasks); cancelled when the view goes."""
        task = tasks.spawn(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- subtitles by hand (CC ▾) ------------------------------------------------------------

    async def video_target(self, op: Operation) -> VideoTarget:
        """Who a row's video is: its move record's kind and TMDB id, the rest from its name, and
        its name before this rename."""
        record = await self.services.moves.of_operation(op.id)
        self.services.warm_up()  # the name reader's first parse (nothing when it has been)
        path = Path(op.dst or "")
        original = Path(op.src).name if op.src else ""
        return VideoTarget(path, file_identity(path.name, record, original), op.run_id)

    def _cc_changed(self) -> None:
        if self.winfo_exists():
            for card in self.cards:
                card.refresh_rows()

    def _cc_done(self, key: Hashable) -> None:
        for card in self.cards:
            if any(op.id == key for op in card.operations):
                self.spawn(card.subtitles_changed())

    def set_kind(self, kind: str) -> None:
        """Every run ("all"), or only the runs that renamed this kind (old runs on both sides)."""
        if kind != self.kind:
            self.kind = kind
            self.spawn(self.reload())

    def on_show(self) -> None:
        self.spawn(self.reload())

    async def reload(self) -> None:
        self._generation += 1
        generation = self._generation
        every = await self.services.history.list_runs()
        if generation != self._generation or not self.winfo_exists():
            return
        wanted = MediaKind.MOVIE if self.kind == "movies" else MediaKind.EPISODE
        runs = [r for r in every if self.kind == "all" or not r.kinds or wanted in r.kinds]
        for card in self.cards:
            card.destroy()
        for _, heading in self._headings:
            heading.destroy()
        self.cards = []
        self._headings = []
        self._card_days = []
        if not runs:
            if not every:
                text = t(NOTHING_YET)
            elif self.kind == "movies":
                text = t("No renamed movies yet")
            else:
                text = t("No renamed shows yet")
            self.empty_label.configure(text=text)
            self.empty_label.pack(pady=40)
            return
        self.empty_label.pack_forget()
        now = datetime.now()
        for run in runs:
            undone_at = None
            if run.status == "undone":
                ops = await self.services.history.operations(run.id)
                dates = [o.undone_at for o in ops if o.undone_at]
                undone_at = max(dates) if dates else None
            if generation != self._generation or not self.winfo_exists():
                return  # a newer reload took over, or the view is gone
            day = day_label(run.started_at, now)
            if not self._headings or self._headings[-1][0] != day:
                heading = ctk.CTkLabel(
                    self.list,
                    text=day.upper(),
                    anchor="w",
                    font=theme.font(11, bold=True),
                    text_color=theme.TEXT_MUTED,
                )
                heading.pack(fill="x", padx=10, pady=(12, 2))
                self._headings.append((day, heading))
            card = RunCard(self.list, self, run, undone_at)
            card.pack(fill="x", padx=6, pady=4)
            self.cards.append(card)
            self._card_days.append(day)
            await asyncio.sleep(0)  # one card at a time, so the window keeps responding

    def day_labels(self) -> list[str]:
        return [day for day, _ in self._headings]

    def cards_by_day(self) -> dict[str, list[RunCard]]:
        out: dict[str, list[RunCard]] = {}
        for card, day in zip(self.cards, self._card_days, strict=True):
            out.setdefault(day, []).append(card)
        return out

    def _card(self, run_id: int) -> RunCard | None:
        return next((c for c in self.cards if c.run.id == run_id), None)

    async def _report(self, result: UndoResult) -> None:
        log.info("Undo finished: %d change(s) put back", result.undone, extra={"success": True})
        if result.skipped:
            lines = [
                f"• {Path(path).name} ({t(reason)})"  # the journal's English reason, translated
                for path, reason in result.skipped
            ]
            shown = "\n".join(lines[:SKIPPED_SHOWN]) + ("\n…" if len(lines) > SKIPPED_SHOWN else "")
            message = tn(
                "{n} change reversed. These were left as they are:",
                "{n} changes reversed. These were left as they are:",
                result.undone,
            )
            await self.inform(t("Some items weren't undone"), f"{message}\n\n{shown}")
        if self.winfo_exists():
            await self.reload()

    async def undo_run(self, run_id: int) -> None:
        if self.busy:
            return
        self.busy = True
        try:
            await self._undo_run(run_id)
        finally:
            self.busy = False

    async def undo_selected(self, run_id: int) -> None:
        if self.busy:
            return
        self.busy = True
        try:
            await self._undo_selected(run_id)
        finally:
            self.busy = False

    async def _undo_run(self, run_id: int) -> None:
        card = self._card(run_id)
        if card is None:
            return
        run = card.run
        files = tn("{n} renamed file", "{n} renamed files", run.file_count)
        created = tn(
            "{n} downloaded poster, trailer or subtitle",
            "{n} downloaded posters, trailers or subtitles",
            run.created_count,
        )
        message = t(
            "This puts back {files} and removes {created} from this run.",
            files=files,
            created=created,
        )
        if not await self.confirm(t("Undo this run?"), message):
            return
        await self._report(await self.services.undo.undo_run(run_id))

    async def _undo_selected(self, run_id: int) -> None:
        card = self._card(run_id)
        if card is None:
            return
        if not card.expanded:
            await card.expand()
        chosen = card.selected_ids()
        if not chosen:
            return
        # Folders created by the run go too, but only if undoing leaves them empty.
        folders = [o.id for o in card.operations if o.kind is OpKind.CREATE_DIR and o.undoable]
        message = tn("This puts back {n} item.", "This puts back {n} items.", len(chosen))
        if not await self.confirm(t("Undo the selected items?"), message):
            return
        await self._report(await self.services.undo.undo_operations([*chosen, *folders]))

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        super().destroy()
