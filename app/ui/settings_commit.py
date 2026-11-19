"""Instant saving for the Settings window (docs/settings/2026-11-19-settings-redesign.md §3): every
change stores itself when it is made, through one `Committer`.

- `Committer.commit(apply, undo, *, box)` runs `apply` (the store: `SettingsService.update(**its
  fields)`, the Forge settings service the same, or the service call a part makes). A refusal
  (pydantic's ValidationError, or `Refused(text)` that a part raises for its own checks) is said
  in red inside `box` (`SettingsBox.refuse`, per control: `source`) and nothing is stored; a
  stored change clears its own control's refusal (another control's stays) and shows the "✓ Saved · Undo" bar (`UndoBar`) for UNDO_SECONDS, replaced by
  the next one. `apply` may return False for "nothing changed" (no bar then). Undo runs `undo`
  through the same path (so it is checked too) and shows no new bar: Undo can't be undone.
- `commit_fields(committer, service, box, **fields)`: the usual change, a few fields of a
  settings service (AirGrabber's or MediaForge's: `current` + `update` + `subscribe`); Undo
  stores the values those fields had before. Changes of one service go one at a time (quick
  A → B → C undoes to B), and a store from elsewhere (a setup, a phone, a guide) that changes
  those fields while Undo is offered takes the Undo away: it would put a value back over it.
- `TextCommit` (`text_commit` for one field): a text field stores on Enter, or when the
  keyboard leaves it for another control of the window (`defocus_on_click`: a click anywhere
  takes it), and only when its text differs from what is stored. Focus leaving for another
  program stores nothing and keeps the text. While it has the keyboard or holds unstored text
  it is "editing": a value stored elsewhere meanwhile doesn't overwrite it. Fields that belong
  together (a size's min and max, an account's name and password) are one TextCommit: moving
  between them isn't leaving. The window closing stores what the fields hold (`flush`).

Actions (Install, Check, Test, Sign in, Connect, Backup, Restart …) aren't changes: they show
their own results and never the bar. A window closed while a commit's `await` runs is fine:
nothing of it is touched after the store (`winfo_exists`).
"""

from __future__ import annotations

import asyncio
import functools
import tkinter as tk
from collections.abc import Awaitable, Callable, Hashable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

import customtkinter as ctk
from pydantic import ValidationError

from app.i18n import N_, t
from app.ui import theme
from app.ui.dialog_kit import DETAIL_SIZE
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.ui.settings_layout import SettingsBox

SAVED_TEXT = N_("✓ Saved")
UNDO_TEXT = N_("Undo")
UNDO_SECONDS = 5

# What a commit stores and what Undo stores back. `apply` returning False: nothing changed.
Apply = Callable[[], Awaitable[bool | None]]
Undo = Callable[[], Awaitable[bool | None]]


class Refused(Exception):
    """A change a part won't store, with why (translated): said in red in its box."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def plain_describe(error: ValidationError) -> str:
    """A validation error's messages, one per line, without pydantic's "Value error, "."""
    return "\n".join(item["msg"].removeprefix("Value error, ") for item in error.errors())


def _alive(widget: tk.Misc | None) -> bool:
    if widget is None:
        return False
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole application is gone
        return False


class UndoBar(ctk.CTkFrame):
    """"✓ Saved · Undo" at the foot of the window, over the page (placed, so nothing moves),
    for UNDO_SECONDS after a stored change."""

    def __init__(
        self,
        master: Any,
        on_undo: Callable[[], None],
        *,
        on_gone: Callable[[], None] | None = None,
    ) -> None:
        self._on_gone = on_gone
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_width=1,
            border_color=theme.PANEL_EDGE,
            corner_radius=10,
        )
        self.saved_label = ctk.CTkLabel(
            self, text=t(SAVED_TEXT), font=theme.font(DETAIL_SIZE), text_color=theme.SUCCESS
        )
        self.saved_label.pack(side="left", padx=(14, 6), pady=6)
        ctk.CTkLabel(
            self, text="·", font=theme.font(DETAIL_SIZE), text_color=theme.TEXT_MUTED
        ).pack(side="left", pady=6)
        self.undo_link = ctk.CTkLabel(
            self,
            text=t(UNDO_TEXT),
            font=theme.font(DETAIL_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
        )
        self.undo_link.pack(side="left", padx=(6, 14), pady=6)
        self.undo_link.bind("<Button-1>", lambda _event: on_undo())
        self._job: str | None = None

    @property
    def shown(self) -> bool:
        return bool(self.winfo_manager())

    def show(self) -> None:
        """Show it (again) for UNDO_SECONDS."""
        self._cancel()
        self.place(relx=0.5, rely=1.0, anchor="s", y=-14)
        self.lift()
        self._job = self.after(UNDO_SECONDS * 1000, self.hide)

    def hide(self) -> None:
        self._cancel()
        if _alive(self):
            self.place_forget()

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def destroy(self) -> None:
        self._cancel()
        if self._on_gone is not None:
            self._on_gone()
        super().destroy()


class _Last:
    """What Undo would store, for `box` (its refusal said as `source`'s); `stop`: ends the
    watch on the fields it touched."""

    def __init__(
        self, undo: Undo, box: SettingsBox, source: Hashable, stop: Callable[[], None] | None
    ) -> None:
        self.undo = undo
        self.box = box
        self.source = source
        self.stop = stop


class Committer:
    """Stores the changes of one Settings window (see the module's text). `window`: where the
    Undo bar shows (None: no bar, e.g. a part tested on its own)."""

    def __init__(
        self,
        window: Any = None,
        *,
        describe: Callable[[ValidationError], str] = plain_describe,
    ) -> None:
        self._describe = describe
        self.bar: UndoBar | None = (
            UndoBar(window, self.undo, on_gone=self._forget) if window is not None else None
        )
        self._last: _Last | None = None  # what Undo would store
        self._fields: list[TextCommit] = []
        # One field change of a settings service at a time (`commit_fields`): each reads its
        # "before" once the previous one has stored, so A → B → C quickly undoes to B.
        self._locks: dict[int, tuple[SettingsStore, asyncio.Lock]] = {}

    @property
    def can_undo(self) -> bool:
        """Whether the bar's Undo has something to store back."""
        return self._last is not None

    def lock(self, service: SettingsStore) -> asyncio.Lock:
        """The lock `commit_fields` holds for `service` while it reads "before" and stores."""
        held = self._locks.get(id(service))
        if held is None or held[0] is not service:
            held = (service, asyncio.Lock())
            self._locks[id(service)] = held
        return held[1]

    async def commit(
        self,
        apply: Apply,
        undo: Undo | None,
        *,
        box: SettingsBox,
        source: Hashable = None,
        watch: tuple[SettingsStore, Sequence[str]] | None = None,
    ) -> bool:
        """Store one change: True when it was stored (or there was nothing to store), False
        when it was refused (`box` says why, nothing stored). `source`: the control it comes
        from (its fields): storing clears that one's refusal only, not another control's in the
        same box. `watch`: the service and fields the change stored; when a store from
        elsewhere changes one of them while Undo is offered, Undo goes (it would put back a
        value over that one)."""
        return await self._run(apply, undo, box, source, undoing=False, watch=watch)

    async def _run(
        self,
        apply: Apply,
        undo: Undo | None,
        box: SettingsBox,
        source: Hashable,
        *,
        undoing: bool,
        watch: tuple[SettingsStore, Sequence[str]] | None = None,
    ) -> bool:
        try:
            changed = await apply()
        except Refused as exc:
            self._refuse(box, exc.text, source)
            return False
        except ValidationError as exc:
            self._refuse(box, self._describe(exc), source)
            return False
        if _alive(box.frame):
            box.refuse(None, source=source)
        if changed is False:
            return True
        bar = self.bar
        if undoing:
            if _alive(bar):
                assert bar is not None
                bar.hide()
            return True
        self._forget()
        if undo is not None:
            stop = self._watch(*watch) if watch is not None else None
            self._last = _Last(undo, box, source, stop)
            if _alive(bar):
                assert bar is not None
                bar.show()
        return True

    def _watch(self, service: SettingsStore, names: Sequence[str]) -> Callable[[], None]:
        """Hear `service`'s stores: one that changes what this change stored ends its Undo.
        (Read at once, after the store: nothing else ran since it landed.)"""
        stored = {name: getattr(service.current, name) for name in names}

        def heard(updated: Any) -> None:
            if any(getattr(updated, name) != value for name, value in stored.items()):
                self.stored_elsewhere()

        return service.subscribe(heard)

    def stored_elsewhere(self) -> None:
        """A value the last change stored was changed by another store: no Undo over it."""
        self._forget()
        bar = self.bar
        if _alive(bar):
            assert bar is not None
            bar.hide()

    def _forget(self) -> None:
        last, self._last = self._last, None
        if last is not None and last.stop is not None:
            last.stop()

    def _refuse(self, box: SettingsBox, text: str, source: Hashable) -> None:
        if _alive(box.frame):
            box.refuse(text, source=source)

    def undo(self) -> None:
        """The bar's Undo: the last change's values from before, stored the same way."""
        if self._last is not None:
            spawn(self.run_undo(), name="settings-undo")

    async def run_undo(self) -> bool:
        last = self._last
        if last is None:
            return False
        self._forget()
        return await self._run(last.undo, None, last.box, last.source, undoing=True)

    # -- text fields ---------------------------------------------------------------------------

    def watch(self, field: TextCommit) -> TextCommit:
        """Keep `field` for `flush` (the window closing stores what it holds)."""
        self._fields.append(field)
        return field

    def flush(self) -> None:
        """Store each text field that holds unstored text (the window closes: nothing is left
        pending)."""
        for field in self._fields:
            field.commit_now()
        self._fields.clear()


class SettingsStore(Protocol):
    """A settings service: AirGrabber's SettingsService or MediaForge's ForgeSettingsService."""

    @property
    def current(self) -> Any: ...

    async def update(self, **changes: Any) -> Any: ...

    def subscribe(self, listener: Callable[[Any], None]) -> Callable[[], None]: ...


async def commit_fields(
    committer: Committer,
    service: SettingsStore,
    box: SettingsBox,
    *,
    check: Callable[[], Awaitable[None]] | None = None,
    **fields: Any,
) -> bool:
    """Store `fields` of `service` as one change; Undo stores the values they had before (read
    once the change before it, on the same service, has stored: `Committer.lock`). A store
    from elsewhere that changes these fields while Undo is offered ends the Undo.
    `check`: a part's own check first (raises Refused)."""
    lock = committer.lock(service)
    before: dict[str, Any] = {}

    async def apply() -> bool:
        before.update({name: getattr(service.current, name) for name in fields})
        if check is not None:
            await check()
        old = service.current
        await service.update(**fields)
        return service.current is not old

    async def undo() -> bool:
        async with lock:
            old = service.current
            await service.update(**before)
            return service.current is not old

    async with lock:
        return await committer.commit(
            apply, undo, box=box, source=tuple(sorted(fields)), watch=(service, tuple(fields))
        )


class TextCommit:
    """Stores the text of `entries` (one field, or the few of one change: a size's min and max,
    an account's name and password) on Enter or when the keyboard leaves them inside the window,
    only when it differs from what is stored (`stored()`, one text per entry). `commit(texts)`
    starts the store (it reads what else it needs at once: the window may close right after).
    `strip`: spaces around a text don't count (not for a password). When `commit` returns the
    store's task (True: stored), the field then shows the stored form (`_landed`)."""

    def __init__(
        self,
        entries: Sequence[ctk.CTkEntry],
        *,
        stored: Callable[[], Sequence[str]],
        commit: Callable[[list[str]], object],
        strip: bool = True,
    ) -> None:
        self.entries = list(entries)
        self._stored = stored
        self._commit = commit
        self._strip = strip
        self._seen = list(stored())  # what was stored when last looked (`show`)
        for entry in self.entries:
            entry.bind("<Return>", lambda _event: self.commit_now(), add="+")
            entry.bind("<KP_Enter>", lambda _event: self.commit_now(), add="+")
            entry.bind("<FocusOut>", self._focus_out, add="+")

    @property
    def texts(self) -> list[str]:
        return [str(entry.get()) for entry in self.entries]

    @property
    def text(self) -> str:
        """The first entry's text (a field of its own: its text)."""
        return self.texts[0]

    def _same(self, a: str, b: str) -> bool:
        return a.strip() == b.strip() if self._strip else a == b

    @property
    def pending(self) -> bool:
        """It holds text that isn't stored."""
        stored = list(self._stored())
        return any(not self._same(a, b) for a, b in zip(self.texts, stored, strict=True))

    @property
    def editing(self) -> bool:
        """It has the keyboard or holds text typed over what was stored: a value stored
        elsewhere waits."""
        return self._typed() or self._has_focus()

    def _typed(self) -> bool:
        return any(not self._same(a, b) for a, b in zip(self.texts, self._seen, strict=True))

    def _has_focus(self) -> bool:
        try:
            focused = self.entries[0].focus_get()
        except (KeyError, tk.TclError):  # a popdown's name Tk can't resolve
            return False
        return focused is not None and self._ours(focused)

    def _ours(self, widget: tk.Misc) -> bool:
        return any(
            widget is entry or widget is getattr(entry, "_entry", None) for entry in self.entries
        )

    def commit_now(self) -> None:
        if not all(_alive(entry) for entry in self.entries) or not self.pending:
            return
        sent = self.texts
        started = self._commit(sent)
        if isinstance(started, asyncio.Future):
            started.add_done_callback(functools.partial(self._landed, sent))

    def _landed(self, sent: list[str], task: asyncio.Future[Any]) -> None:
        """Its own store landed: the field shows what is stored, in the stored form (the
        model may have rewritten it: "CAM" → "cam", a path's "/" as "\\", "2.50" → "2.5"), unless
        something was typed since or the store was refused."""
        if task.cancelled() or task.exception() is not None or task.result() is not True:
            return
        if not all(_alive(entry) for entry in self.entries) or self.texts != sent:
            return
        values = list(self._stored())
        self._seen = values
        self._write(values)

    def _write(self, values: Sequence[str]) -> None:
        for entry, value in zip(self.entries, values, strict=True):
            if str(entry.get()) == value:
                continue
            entry.delete(0, "end")  # an entry with a hint shows it again when empty
            if value:
                entry.insert(0, value)

    def show(self, values: Sequence[str]) -> None:
        """What is stored now (after any change of the settings): shown when it changed and
        the field isn't being edited (its own store landing shows nothing new; text typed over
        an older value, or refused, stays)."""
        values = list(values)
        if values == self._seen or not all(_alive(entry) for entry in self.entries):
            return
        keep = self._has_focus() or (
            self._typed() and any(not self._same(a, b) for a, b in zip(self.texts, values))
        )
        self._seen = values
        if not keep:
            self._write(values)

    def _focus_out(self, _event: object) -> None:
        entry = self.entries[0]
        if _alive(entry):
            entry.after_idle(self._after_focus_out)

    def _after_focus_out(self) -> None:
        entry = self.entries[0]
        if not _alive(entry):
            return
        try:
            focused = entry.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        self.left(focused)

    def left(self, focused: tk.Misc | None) -> None:
        """The keyboard left for `focused`: stored, unless it went to another program (None:
        nothing of this window has it) or to another entry of the same change."""
        if focused is None or self._ours(focused):
            return
        self.commit_now()


def text_commit(
    entry: ctk.CTkEntry,
    *,
    stored: Callable[[], str],
    commit: Callable[[str], object],
    strip: bool = True,
) -> TextCommit:
    """A TextCommit for one field: `stored()` its stored text, `commit(text)`."""
    return TextCommit(
        [entry], stored=lambda: [stored()], commit=lambda texts: commit(texts[0]), strip=strip
    )


def command[**P](fn: Callable[P, object]) -> Callable[P, None]:
    """`fn` as a widget's command: what it returns (a change's task) isn't the widget's."""

    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        fn(*args, **kwargs)

    return run


def defocus_on_click(window: tk.Misc) -> None:
    """A click anywhere in `window` outside a text field takes the keyboard from the field that
    has it (CTk's buttons, switches and the page don't), so leaving a field stores it."""

    def clicked(event: tk.Event[Any]) -> None:
        widget = event.widget
        if isinstance(widget, (tk.Entry, tk.Text)) or not isinstance(widget, tk.Misc):
            return
        try:
            focused = window.focus_get()
        except (KeyError, tk.TclError):
            return
        if isinstance(focused, (tk.Entry, tk.Text)):
            window.focus_set()

    window.bind("<Button-1>", clicked, add="+")
