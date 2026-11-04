"""The one-time import of the old MediaForge (D4, D13; app/forge/migration.py does the work).

ImportStep: a borderless dialog in the dialog kit's style with the Forge look. It asks "Bring
over your MediaForge settings, presets and history?" (Bring over / Not now). When Forge already
has data of its own it asks again before replacing it. Then it reports what came over (presets,
folders, past renames, remembered matches; the TMDB key only when it was taken over), or why
nothing did. "Remove the old MediaForge" is offered once in the result, while the old install
is still registered and a remover is wired (`on_remove_old`, F-27's action).

MediaForge's host (app/forge/ui/host.py) shows it over the main window at the first visit to the
page in a run while the old data exists and nothing was imported; "Not now" means it asks again at
AirGrabber's next start (D13). BringOverBox is Settings' "Bring over…" row (in the Auto-rename section's foot)
for the same case. RemoveOldBox, below it in that foot, is the guard's notice with "Remove the
old MediaForge" while the old install is registered (F-27, app/forge/old_install.py).

Never `transient()` (MediaForge's lesson, see app/forge/ui/widgets/dialogs.py); it takes the
grab while it shows and gives it back to whoever held it before (Settings). Esc and the round ✕
mean Not now / Close, except while it is busy.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol

import customtkinter as ctk

from app.forge.ui.look import app_look
from app.i18n import N_, t, tn
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    BUTTON_HEIGHT,
    CLOSE_CLEARANCE,
    DETAIL_SIZE,
    HEADING_SIZE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.forge.migration import ImportProblem, ImportResult, ImportTarget, OldInstall
    from app.forge.old_install import OldInstallRemover, Outcome

log = logging.getLogger(__name__)

WIDTH = 500  # the content's width (CTk units)
_GAP = 14
_BUTTON_GAP = 12
_WIDE_BUTTON = 230  # "Remove the old MediaForge"
FOOT_GAP = 12  # between the rows in Settings' foot
_GUARD_WRAP = 320  # the guard's notice beside the wide button

ASK_HEADING = N_("Bring over your MediaForge settings, presets and history?")
ASK_TEXT = N_(
    "MediaForge is part of AirGrabber now. Your presets, folders, past renames and remembered "
    "matches can come along. The old MediaForge itself isn't changed."
)
BRING_OVER = N_("Bring over")
NOT_NOW = N_("Not now")
REPLACE_HEADING = N_("Replace what's here?")
REPLACE_TEXT = N_(
    "MediaForge in AirGrabber already has presets, folders or history of its own. Bringing "
    "your old MediaForge's over replaces them."
)
REPLACE = N_("Replace")
WORKING = N_("Bringing your MediaForge settings over…")
PROBLEM_HEADING = N_("Nothing was brought over")
TRY_AGAIN = N_("Try again")
FAILED_TEXT = N_("MediaForge's settings couldn't be written here. The log has the details.")
RESULT_HEADING = N_("Your MediaForge settings are here")
RESULT_INTRO = N_("Brought over from your old MediaForge:")
TMDB_KEY_LINE = N_("Your TMDB key")
MENU_LINE = N_("The right-click menu now opens MediaForge in AirGrabber")
WAITS_TEXT = N_("Automatic renaming starts once the old MediaForge is removed.")
REMOVE_TEXT = N_(
    "The old MediaForge isn't needed any more. Its own uninstaller asks before it deletes "
    "anything."
)
REMOVE_OLD = N_("Remove the old MediaForge")
DONE = N_("Done")
BOX_TEXT = N_("Your old MediaForge's settings, presets and history can still be brought over.")
BOX_BUTTON = N_("Bring over…")

BringOver = Callable[["ImportTarget", "OldInstall"], Awaitable["ImportResult"]]
HasOwnData = Callable[["ImportTarget"], Awaitable[bool]]
FindOldInstall = Callable[[], "OldInstall | None"]


class OldInstallRuntime(Protocol):
    """What RemoveOldBox needs of ForgeRuntime: its services (None before Forge started)."""

    @property
    def services(self) -> Any: ...


def result_lines(result: ImportResult) -> list[str]:
    """What came over, one line each (the TMDB key and the menu only when they did)."""
    lines = [
        tn("{n} preset", "{n} presets", result.presets),
        tn("{n} folder", "{n} folders", result.folders),
        tn("{n} past rename", "{n} past renames", result.renames),
        tn("{n} remembered match", "{n} remembered matches", result.matches),
    ]
    if result.tmdb_key:
        lines.append(t(TMDB_KEY_LINE))
    if result.menu:
        lines.append(t(MENU_LINE))
    return lines


def _default_bring_over(forge: ImportTarget, old: OldInstall) -> Awaitable[ImportResult]:
    from app.forge.migration import bring_over

    return bring_over(forge, old)


def _default_has_own_data(forge: ImportTarget) -> Awaitable[bool]:
    from app.forge.migration import has_own_data

    return has_own_data(forge)


def _default_find() -> OldInstall | None:
    from app.forge.migration import find_old_install

    return find_old_install()


class ImportStep(ctk.CTkToplevel):
    """The import's question, its work and its result, over `master` (the main window or
    Settings). `on_done(result)` after it closed: what came over, or None (Not now, a
    problem). `bring_over` / `has_own_data`: the migration's (tests pass fakes)."""

    def __init__(
        self,
        master: Any,
        forge: ImportTarget,
        old: OldInstall,
        *,
        look: theme.Look | None = None,
        on_remove_old: Callable[[], None] | None = None,
        on_done: Callable[[ImportResult | None], None] | None = None,
        bring_over: BringOver | None = None,
        has_own_data: HasOwnData | None = None,
    ) -> None:
        self.look: theme.Look = app_look() if look is None else look
        super().__init__(master, fg_color=self.look.BG)
        self._forge = forge
        self._old = old
        self._on_remove_old = on_remove_old
        self._on_done = on_done
        self._bring_over: BringOver = bring_over or _default_bring_over
        self._has_own_data: HasOwnData = has_own_data or _default_has_own_data
        self.result: ImportResult | None = None
        self.remove_button: ctk.CTkButton | None = None
        self.busy = False
        self._closed = False
        self._tasks: set[asyncio.Task[None]] = set()
        # Settings holds the grab while it shows: it gets it back when this closes.
        self._grab_before: tk.Misc | None = self.grab_current()

        self.title(t(ASK_HEADING))
        set_window_icon(self)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()  # the width
        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=1)
        self.heading = ctk.CTkLabel(
            top,
            text="",
            font=theme.font(HEADING_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
            justify="left",
            wraplength=WIDTH - CLOSE_CLEARANCE,
        )
        self.heading.grid(row=0, column=0, sticky="w")
        ctk.CTkFrame(top, width=CLOSE_CLEARANCE, height=1, fg_color="transparent").grid(
            row=0, column=1
        )  # the close button's corner
        self.content = ctk.CTkFrame(body, fg_color="transparent")
        self.content.pack(fill="x")
        self.buttons = ctk.CTkFrame(body, fg_color="transparent", height=BUTTON_HEIGHT)
        self.buttons.pack(fill="x", pady=(NAV_GAP, 0))

        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(
            self,
            drag_widgets=(self, body, top, self.heading, self.content, self.buttons),
            on_close=self.close,
            look=self.look,
        )
        self.show_question()
        place_over(self, master)
        activate_later(self)

    # --- building a step ------------------------------------------------------------------------

    def step(self, heading: str) -> None:
        """Start a step: its heading; the previous step's content and buttons go."""
        self.heading.configure(text=heading)
        for child in (*self.content.winfo_children(), *self.buttons.winfo_children()):
            child.destroy()

    def line(
        self,
        text: str,
        *,
        size: int = BODY_SIZE,
        color: theme.Color = theme.TEXT,
        gap: int = LINE_GAP,
    ) -> ctk.CTkLabel:
        label = text_label(self.content, text, size=size, color=color, wraplength=WIDTH)
        first = len(self.content.winfo_children()) == 1
        label.pack(anchor="w", pady=(LINE_GAP if first else gap, 0))
        return label

    def button(
        self,
        text: str,
        command: Callable[[], None],
        *,
        primary: bool = False,
        width: int = 140,
        master: Any = None,
    ) -> ctk.CTkButton:
        """A button at the right of the row (add the rightmost first)."""
        make = primary_button if primary else secondary_button
        button = make(master or self.buttons, text, command, width=width, look=self.look)
        button.pack(side="right", padx=(_BUTTON_GAP, 0))
        return button

    @property
    def button_texts(self) -> list[str]:
        """The button row's texts, left to right (they are packed from the right)."""
        return [
            str(child.cget("text"))
            for child in reversed(self.buttons.winfo_children())
            if isinstance(child, ctk.CTkButton)
        ]

    def run(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        """Start `coro`; cancelled when the dialog goes."""
        task: asyncio.Task[None] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the steps ------------------------------------------------------------------------------

    def show_question(self) -> None:
        self.step(t(ASK_HEADING))
        self.line(t(ASK_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED)
        self.button(t(NOT_NOW), self.close)
        self.button(t(BRING_OVER), self.bring_over, primary=True)

    def bring_over(self) -> None:
        """Bring over: ask first when Forge has data of its own, else import."""
        if not self.busy:
            self.busy = True
            self.run(self._check_then_import(), "forge-import-check")

    async def _check_then_import(self) -> None:
        self.show_working()
        try:
            own = await self._has_own_data(self._forge)
        except Exception as exc:
            log.error("Couldn't tell whether MediaForge has data (%s)", type(exc).__name__)
            own = True  # ask rather than replace without a word
        if not self.winfo_exists():
            return
        if own:
            self.busy = False
            self.show_replace_question()
            return
        await self._import()

    def show_replace_question(self) -> None:
        self.step(t(REPLACE_HEADING))
        self.line(t(REPLACE_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED)
        self.button(t(NOT_NOW), self.close)
        self.button(t(REPLACE), self.replace, primary=True)

    def replace(self) -> None:
        if not self.busy:
            self.busy = True
            self.run(self._import(), "forge-import")

    def show_working(self) -> None:
        self.step(t(ASK_HEADING))
        self.line(t(WORKING), size=DETAIL_SIZE, color=theme.TEXT_MUTED)

    async def _import(self) -> None:
        from app.forge.migration import ImportProblem

        self.busy = True
        if self.winfo_exists():
            self.show_working()
        try:
            result = await self._bring_over(self._forge, self._old)
        except ImportProblem as problem:
            self.busy = False
            if self.winfo_exists():
                self.show_problem(problem)
            return
        except Exception as exc:
            # The type only: a message or traceback could name the user's folders.
            log.error("Bringing the old MediaForge over failed (%s)", type(exc).__name__)
            self.busy = False
            if self.winfo_exists():
                self.show_problem(None)
            return
        self.busy = False
        self.result = result
        if self.winfo_exists():
            self.show_result(result)

    def show_problem(self, problem: ImportProblem | None) -> None:
        from app.forge.migration import Problem

        self.step(t(PROBLEM_HEADING))
        self.line(problem.text if problem is not None else t(FAILED_TEXT))
        if problem is not None and problem.problem is Problem.RUNNING:
            self.button(t(NOT_NOW), self.close)
            self.button(t(TRY_AGAIN), self.replace, primary=True)
        else:
            self.button(t("Close"), self.close, primary=True)

    def show_result(self, result: ImportResult) -> None:
        self.step(t(RESULT_HEADING))
        self.line(t(RESULT_INTRO), size=DETAIL_SIZE, color=theme.TEXT_MUTED)
        for index, text in enumerate(result_lines(result)):
            self.line("•  " + text, gap=_GAP if index == 0 else 2)
        if result.waits_for_removal:
            self.line(t(WAITS_TEXT), size=DETAIL_SIZE, color=theme.WARNING, gap=_GAP + 6)
        if self._on_remove_old is not None and self._old.registered:
            self.line(t(REMOVE_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED, gap=_GAP + 6)
            row = ctk.CTkFrame(self.content, fg_color="transparent")
            row.pack(fill="x", pady=(_GAP, 0))
            self.remove_button = secondary_button(
                row, t(REMOVE_OLD), self.remove_old, width=_WIDE_BUTTON, look=self.look
            )
            self.remove_button.pack(side="left")
        self.button(t(DONE), self.close, primary=True)

    def remove_old(self) -> None:
        """The result's "Remove the old MediaForge" (once): close, then F-27's remover."""
        remove = self._on_remove_old
        self._on_remove_old = None
        self.close()
        if remove is not None:
            try:
                remove()
            except Exception:
                log.exception("Removing the old MediaForge couldn't start")

    # --- closing --------------------------------------------------------------------------------

    def close(self) -> None:
        """Not now / Close / Done / Esc / ✕ (ignored while busy)."""
        if self.busy or self._closed:
            return
        self._closed = True
        grab = self._grab_before
        if self.winfo_exists():
            self.destroy()
        try:
            if grab is not None and grab.winfo_exists():
                grab.grab_set()  # Settings, modal again
                grab.focus_force()
        except tk.TclError:  # gone meanwhile: only the focus is lost
            pass
        if self._on_done is not None:
            try:
                self._on_done(self.result)
            except Exception:
                log.exception("The import's done callback failed")

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        super().destroy()


class BringOverBox:
    """Settings › Auto-rename: "Bring over…" in the section's foot while
    the old MediaForge's data exists and nothing was imported (D13). Shows nothing for a
    services object that can't import (a test's fake) or when there is no old data."""

    def __init__(
        self,
        foot: ctk.CTkFrame,
        services: object,
        *,
        find_old_install: FindOldInstall | None = None,
        bring_over: BringOver | None = None,
        has_own_data: HasOwnData | None = None,
    ) -> None:
        from app.forge.migration import ImportTarget

        self._foot = foot
        self._find: FindOldInstall = find_old_install or _default_find
        self._bring_over = bring_over
        self._has_own_data = has_own_data
        self.row: ctk.CTkFrame | None = None
        self.label: ctk.CTkLabel | None = None
        self.button: ctk.CTkButton | None = None
        self.dialog: ImportStep | None = None
        self._old: OldInstall | None = None
        self._task: asyncio.Task[None] | None = None
        self._forge: ImportTarget | None = None
        if not isinstance(services, ImportTarget) or services.settings.current.imported_from:
            return
        self._forge = services
        self._task = spawn(self._look_for_old(), name="forge-import-look")
        tk.Misc.bind(foot, "<Destroy>", self._destroyed, "+")

    @property
    def done(self) -> asyncio.Task[None] | None:
        """The look for the old install (tests await it)."""
        return self._task

    async def _look_for_old(self) -> None:
        old = await asyncio.to_thread(self._find)
        if old is None or not old.has_data or not self._foot.winfo_exists():
            return
        forge = self._forge
        if forge is None or forge.settings.current.imported_from:
            return
        self._old = old
        row = ctk.CTkFrame(self._foot, fg_color="transparent")
        others = self._foot.pack_slaves()
        if others:  # "Bring over…" sits above "Remove the old MediaForge" (RemoveOldBox)
            row.pack(fill="x", before=others[0], pady=(0, FOOT_GAP))
        else:
            row.pack(fill="x")
        row.grid_columnconfigure(0, weight=1)
        self.label = text_label(
            row,
            t(BOX_TEXT),
            size=DETAIL_SIZE,
            color=theme.TEXT_MUTED,
            wraplength=WIDTH - 140,
        )
        self.label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        button = secondary_button(row, t(BOX_BUTTON), self.open, width=140)
        button.configure(text_color=theme.BUTTON_TEXT)
        button.grid(row=0, column=1, sticky="e")
        self.button = button
        self.row = row
        self._foot.grid()

    def open(self) -> None:
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        if self._forge is None or self._old is None:
            return
        self.dialog = ImportStep(
            self._foot.winfo_toplevel(),
            self._forge,
            self._old,
            on_done=self._finished,
            bring_over=self._bring_over,
            has_own_data=self._has_own_data,
        )

    def _finished(self, result: ImportResult | None) -> None:
        self.dialog = None
        if result is None or self.row is None:
            return
        try:
            self.row.destroy()
            self.row = None
            if not self._foot.winfo_children():
                self._foot.grid_remove()
        except tk.TclError:  # Settings closed meanwhile
            pass

    def _destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is self._foot and self._task is not None:
            self._task.cancel()
            self._task = None


class RemoveOldBox:
    """Settings › Auto-rename: the guard's notice and "Remove the old
    MediaForge" in the section's foot while the old install is still registered (D4, F-27).
    The removal is app/forge/old_install.py's shared remover: it carries on (and lifts the
    guard) when Settings closes. Finding the old install already gone lifts the guard too.
    Data is never lost by removing first: while the old MediaForge's data exists and nothing
    was imported (`imported_from`), the box says to bring the settings over first and has no
    remove button ("Bring over…" sits above it); an import while Settings shows brings the
    button in."""

    def __init__(
        self,
        foot: ctk.CTkFrame,
        runtime: OldInstallRuntime,
        *,
        remover: OldInstallRemover | None = None,
        find_old_install: FindOldInstall | None = None,
    ) -> None:
        from app.forge.old_install import remover_for

        self._foot = foot
        self._runtime = runtime
        self.remover: OldInstallRemover = remover or remover_for(runtime)
        self._find: FindOldInstall = find_old_install or _default_find
        self.row: ctk.CTkFrame | None = None
        self.label: ctk.CTkLabel | None = None
        self.outcome_label: ctk.CTkLabel | None = None
        self.button: ctk.CTkButton | None = None
        self.first_label: ctk.CTkLabel | None = None  # "Bring your settings over first; …"
        self._has_data = True  # the old install's data waits to be brought over (until looked)
        self._unsubscribe: Callable[[], None] | None = None
        self._unsubscribe_settings: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = spawn(self._look(), name="forge-old-install-look")
        tk.Misc.bind(foot, "<Destroy>", self._destroyed, "+")

    @property
    def done(self) -> asyncio.Task[None] | None:
        """The look for the old install (tests await it)."""
        return self._task

    async def _look(self) -> None:
        if self.remover.running:
            old_registered = True
        else:
            try:
                old = await asyncio.to_thread(self._find)
            except Exception as exc:
                log.warning("Couldn't look for the old MediaForge (%s)", type(exc).__name__)
                return
            old_registered = old is not None and old.registered
            self._has_data = old is not None and old.has_data
            if not old_registered:
                self.remover.lift_guard()  # removed some other way meanwhile
                return
        if old_registered and self._foot.winfo_exists():
            self._show()

    def _imported(self) -> bool:
        services = self._runtime.services
        try:
            return services is not None and bool(services.settings.current.imported_from)
        except AttributeError:  # a test's fake services
            return False

    @property
    def may_remove(self) -> bool:
        """Whether "Remove the old MediaForge" is offered: its data was brought over, or it has
        none to bring (or a removal is under way already)."""
        return self.remover.running or not self._has_data or self._imported()

    def _guard_colour(self) -> theme.Color:
        """Amber while the guard holds a stored Manual / On back, else muted."""
        services = self._runtime.services
        try:
            waiting = services is not None and services.settings.current.automatic != "off"
        except AttributeError:  # a test's fake services
            waiting = False
        return theme.WARNING if waiting else theme.TEXT_MUTED

    def _show(self) -> None:
        from app.forge.old_install import BRING_FIRST_TEXT, GUARD_TEXT, REMOVE_OLD

        row = ctk.CTkFrame(self._foot, fg_color="transparent")
        row.pack(fill="x", pady=(FOOT_GAP, 0) if self._foot.pack_slaves() else 0)
        row.grid_columnconfigure(0, weight=1)
        self.label = text_label(
            row, t(GUARD_TEXT), size=DETAIL_SIZE, color=self._guard_colour(),
            wraplength=_GUARD_WRAP,
        )
        self.label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        button = secondary_button(row, t(REMOVE_OLD), self.remove, width=_WIDE_BUTTON)
        button.configure(text_color=theme.BUTTON_TEXT)
        button.grid(row=0, column=1, sticky="e")
        self.button = button
        self.first_label = text_label(
            row, t(BRING_FIRST_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH
        )
        self.outcome_label = text_label(row, "", size=DETAIL_SIZE, wraplength=WIDTH)
        self.row = row
        self._foot.grid()
        self._offer(self.may_remove)
        self._unsubscribe = self.remover.subscribe(self._heard)
        services = self._runtime.services
        subscribe = getattr(getattr(services, "settings", None), "subscribe", None)
        if callable(subscribe) and not self.may_remove:
            self._unsubscribe_settings = subscribe(self._settings_saved)
        if self.remover.running:
            self._heard(None)

    def _offer(self, offered: bool) -> None:
        """The remove button, or (before the data came over) the "bring it over first" line."""
        assert self.label is not None and self.button is not None
        assert self.first_label is not None
        if offered:
            self.label.configure(wraplength=_GUARD_WRAP)
            self.button.grid()
            self.first_label.grid_remove()
        else:
            self.label.configure(wraplength=WIDTH)
            self.button.grid_remove()
            self.first_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(LINE_GAP, 0))

    def _settings_saved(self, settings: Any) -> None:
        """An import while Settings shows ("Bring over…"): now the old one may go."""
        if not getattr(settings, "imported_from", ""):
            return
        if self._unsubscribe_settings is not None:
            self._unsubscribe_settings()
            self._unsubscribe_settings = None
        try:
            if self.row is not None and self.row.winfo_exists():
                self._offer(True)
        except tk.TclError:  # Settings closed meanwhile
            pass

    def remove(self) -> None:
        """The button: start the old uninstaller (it asks the owner itself)."""
        if not self.remover.running and self.may_remove:
            spawn(self.remover.remove(), name="forge-remove-old")

    def _heard(self, outcome: Outcome | None) -> None:
        from app.forge.old_install import WAITING_TEXT, Outcome, outcome_text

        if self.row is None or self.label is None or self.button is None:
            return
        try:
            if not self.row.winfo_exists():
                return
            assert self.outcome_label is not None
            if outcome is None:  # started: the old uninstaller may be asking
                self.button.configure(state="disabled")
                self._say("")
                self.label.configure(text=t(WAITING_TEXT), text_color=theme.TEXT_MUTED)
                return
            if outcome is Outcome.REMOVED:
                self.button.grid_remove()
                self.label.configure(text=outcome_text(outcome), text_color=theme.SUCCESS)
                self._say("")
                return
            from app.forge.old_install import GUARD_TEXT

            self.button.configure(state="normal")
            self.label.configure(text=t(GUARD_TEXT), text_color=self._guard_colour())
            colour = theme.TEXT_MUTED if outcome is Outcome.KEPT else theme.WARNING
            self._say(outcome_text(outcome), colour)
        except tk.TclError:  # Settings closed meanwhile
            pass

    def _say(self, text: str, colour: theme.Color = theme.TEXT_MUTED) -> None:
        label = self.outcome_label
        if label is None:
            return
        label.configure(text=text, text_color=colour)
        if text:
            label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(LINE_GAP, 0))
        else:
            label.grid_remove()

    def _destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is not self._foot:
            return
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._unsubscribe_settings is not None:
            self._unsubscribe_settings()
            self._unsubscribe_settings = None
