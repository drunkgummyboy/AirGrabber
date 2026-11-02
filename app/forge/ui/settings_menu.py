"""MediaForge Settings › Right-click menu: MediaForge in Explorer's right-click menu, on or off,
which presets it shows, and whose menu Explorer has now (MediaForge's Settings › Right-click
menu, `ui/settings_view.py`'s `_build_menu`).

Like the other Forge sections (app/forge/ui/settings_pages.py) its changes wait for Settings'
Save (D9): Save stores the chosen presets (`ForgeSettings.context_menu_presets`, [] = every
preset, so presets added later appear too) and writes or removes the menu
(app/forge/context_menu.py); Cancel and ✕ change nothing. The box starts ticked only when the menu
starts this copy. A menu that starts another copy or the old MediaForge is never removed here;
ticking the box and saving replaces it (the owner's own choice). Two .reg files can be saved: the
menu as ticked, and, while MediaForge's very first (HKLM) menu is still there, the file that
removes it (an administrator runs it).

A Save in Naming that renamed or deleted presets (F-24's `on_presets_saved`, told before this
section saves) carries the chosen presets along (`context_menu.follow_presets`) and rewrites the
menu when it starts this copy; the menu also catches up at every Forge start
(`context_menu.refresh_from`).

The registry is injectable (tests pass a fake; tests/forge/base.py guards the real one).
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Any, Protocol

import customtkinter as ctk

from app.forge import context_menu
from app.forge.context_menu import MAIN_PY, MenuRegistry, MenuState
from app.forge.ui.settings_naming import PresetsChange, on_presets_saved
from app.i18n import N_, t
from app.ui import dialog_kit, theme
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.forge.models.preset import Preset
    from app.forge.models.settings import ForgeSettings
    from app.ui.settings_layout import SettingsPage

log = logging.getLogger(__name__)

# -- its place in MediaForge's Settings (FORGE_SECTIONS in app/forge/ui/settings_pages.py) ------

SECTION_MENU = N_("Right-click menu")
MENU_ICON = "🖱"
MENU_NOTE = N_("Start MediaForge from Explorer: right-click files or folders.")

# -- texts ----------------------------------------------------------------------------------------

GETTING_READY = N_("Getting MediaForge ready…")
MENU_TITLE = N_("In the right-click menu")
MENU_TEXT = N_(
    "Adds a MediaForge submenu to Explorer's right-click menu for files and folders (on "
    "Windows 11 it's under \"Show more options\"). No administrator rights needed."
)
SHOW_MENU = N_("Show MediaForge in the right-click menu")
PRESETS_TITLE = N_("Presets in the menu")
PRESETS_TEXT = N_(
    "One \"Forge: …\" entry for each ticked preset. With every preset ticked, presets you "
    "add later appear too."
)
FILES_TITLE = N_("Registry files")
FILES_TEXT = N_("The menu as ticked above, as a .reg file to add by hand.")
SAVE_REG = N_("Save as .reg file…")
SAVE_REG_TITLE = N_("Save .reg file")
REG_FILE_NAME = "MediaForge menu.reg"  # a file name, kept English like the menu's keys
REG_FILE_KIND = N_("Registry file")
LEGACY_TEXT = N_(
    "An old MediaForge menu was found. Save the removal file and double-click it (Windows asks "
    "for administrator approval) to remove the old entries."
)
SAVE_LEGACY = N_("Save removal file for the old menu…")
SAVE_LEGACY_TITLE = N_("Save removal file")
LEGACY_FILE_NAME = "Remove old MediaForge menu.reg"
FILE_FAILED = N_("The file couldn't be saved.")
PICK_ONE = N_("Tick at least one preset for the right-click menu.")
SAVE_FAILED = N_("The right-click menu couldn't be changed. The log has the details.")
READ_FAILED = N_("MediaForge's presets couldn't be read. The log has the details.")

STATUS: dict[MenuState, str] = {
    MenuState.ON: N_("✓ MediaForge is in the right-click menu."),
    MenuState.OFF: N_("MediaForge isn't in the right-click menu."),
    MenuState.STALE: N_(
        "The menu points to an old location of Python. Save puts it right."
    ),
    MenuState.OTHER_COPY: N_(
        "The menu starts another copy of AirGrabber. Tick the box and Save to point it at "
        "this one."
    ),
    MenuState.OLD_MEDIAFORGE: N_(
        "The menu starts the old MediaForge. Tick the box and Save to point it at AirGrabber."
    ),
}

_ROW_GAP = 8
_COLUMNS = 2  # the presets' tick boxes
_BUTTON_WIDTH = 230


class _Presets(Protocol):
    async def list(self) -> list[Preset]: ...


class _Settings(Protocol):
    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...


class _Parts(Protocol):
    @property
    def presets(self) -> _Presets: ...

    @property
    def settings(self) -> _Settings: ...


class _Starter(Protocol):
    """What the section needs of the ForgeRuntime (tests pass a fake)."""

    @property
    def reason(self) -> str: ...

    @property
    def services(self) -> _Parts | None: ...

    async def start(self) -> _Parts | None: ...


AskSave = Callable[[str, str], str]  # (title, file name) → the chosen path, or ""


class MenuSection:
    """The Right-click menu page's contents; changes wait for Save (`problem` / `save`)."""

    def __init__(
        self,
        page: SettingsPage,
        forge: _Starter,
        *,
        registry: MenuRegistry | None = None,
        main_py: Path = MAIN_PY,
        ask_save: AskSave | None = None,
    ) -> None:
        from app.forge.ui.settings_pages import section_look  # it imports this module

        self._page = page
        self._forge = forge
        self._look = section_look(page)
        self._registry = registry
        self._main_py = main_py
        self.ask_save: AskSave = ask_save if ask_save is not None else self._ask_save
        self._parts: _Parts | None = None
        self._task: asyncio.Task[None] | None = None
        self.state = MenuState.OFF  # whose menu Explorer has (read when the page is ready)
        self.legacy = False  # MediaForge's very first (HKLM) menu is still there
        self._names: list[str] = []  # the presets, in their order
        self._stored: list[str] = []  # context_menu_presets as stored
        self._ticks: dict[str, tk.BooleanVar] = {}
        frame = page.frame

        # Shown until Forge has started and the menu was read (or why it can't).
        self.waiting = page.box()
        self.waiting_label = self.waiting.text(t(GETTING_READY))
        self.waiting.add(self.waiting_label, gap=0, sticky="w")

        menu = page.box(t(MENU_TITLE), t(MENU_TEXT))
        self._on = tk.BooleanVar(master=frame, value=False)
        self.on_box = self._checkbox(menu.body, t(SHOW_MENU), self._on)
        menu.add(self.on_box, gap=_ROW_GAP + 2, sticky="w")
        self.status_label = menu.text("")
        menu.add(self.status_label, gap=_ROW_GAP, sticky="w")

        presets = page.box(t(PRESETS_TITLE), t(PRESETS_TEXT))
        self.presets_frame = ctk.CTkFrame(presets.body, fg_color="transparent")
        presets.add(self.presets_frame, gap=_ROW_GAP + 2, sticky="w")

        files = page.box(t(FILES_TITLE), t(FILES_TEXT))
        self.reg_button = self._button(files.body, t(SAVE_REG), self.save_reg_file)
        files.add(self.reg_button, gap=_ROW_GAP + 2, sticky="w")
        self.legacy_label = files.text(t(LEGACY_TEXT))
        self.legacy_label.configure(text_color=theme.WARNING)
        self._legacy_row = files.add(self.legacy_label, gap=_ROW_GAP + 4, sticky="w")
        self.legacy_button = self._button(files.body, t(SAVE_LEGACY), self.save_legacy_file)
        files.add(self.legacy_button, gap=_ROW_GAP, sticky="w")
        self.file_label = files.text("")
        self.file_label.configure(text_color=theme.WARNING)
        self._file_row = files.take_row()

        self._content = [menu, presets, files]
        for box in self._content:
            box.frame.grid_remove()
        self.legacy_label.grid_remove()
        self.legacy_button.grid_remove()

        tk.Misc.bind(frame, "<Destroy>", self._destroyed, "+")
        self._disconnect: Callable[[], None] | None = on_presets_saved(self._presets_saved)
        self._task = spawn(self._load(), name="forge-menu-settings")

    # --- widgets ---------------------------------------------------------------------------------

    def _button(self, master: Any, text: str, command: Callable[[], None]) -> ctk.CTkButton:
        button = dialog_kit.secondary_button(
            master, text, command, width=_BUTTON_WIDTH, look=self._look
        )
        button.configure(text_color=theme.BUTTON_TEXT)
        return button

    def _checkbox(self, master: Any, text: str, variable: tk.BooleanVar) -> ctk.CTkCheckBox:
        # As Library & watching's options (app/forge/ui/settings_pages.py).
        return ctk.CTkCheckBox(
            master,
            text=text,
            variable=variable,
            width=0,
            height=dialog_kit.BUTTON_HEIGHT,
            checkbox_width=22,
            checkbox_height=22,
            corner_radius=6,
            font=theme.font(dialog_kit.DETAIL_SIZE),
            fg_color=self._look.ACCENT,
            hover_color=self._look.ACCENT_HOVER,
            border_color=self._look.EDGE,
        )

    def _ask_save(self, title: str, name: str) -> str:
        # Native modal dialog: the event loop pauses until it closes (as Settings' Browse).
        return filedialog.asksaveasfilename(
            parent=self._page.frame.winfo_toplevel(),
            title=title,
            defaultextension=".reg",
            initialfile=name,
            filetypes=[(t(REG_FILE_KIND), "*.reg")],
        )

    # --- starting --------------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Whether the menu's settings are showing (Forge started and the menu was read)."""
        return self._parts is not None

    def _alive(self) -> bool:
        return bool(self.waiting_label.winfo_exists())

    async def _load(self) -> None:
        parts = self._forge.services or await self._forge.start()
        if not self._alive():
            return
        if parts is None:
            # NOT_READY (guessit missing) or FAILED: say why; nothing here can be changed.
            self.waiting_label.configure(text=self._forge.reason or t(GETTING_READY))
            return
        try:
            names = [preset.name for preset in await parts.presets.list()]
            state, legacy = await self._read_menu()
        except Exception as exc:
            # The type only: a message could name a path with the user's name in it.
            log.error("The right-click menu's settings couldn't be read (%s)", type(exc).__name__)
            if self._alive():
                self.waiting_label.configure(text=t(READ_FAILED))
            return
        if not self._alive():
            return
        self._parts = parts
        self._names = names
        self._stored = list(parts.settings.current.context_menu_presets)
        self._show(state, legacy)
        self._fill_presets(self._stored)
        self._on.set(state.ours)
        first_gap = self.waiting.frame.grid_info().get("pady")
        self.waiting.frame.grid_remove()
        for box in self._content:
            box.frame.grid()
        if first_gap is not None:  # the first box sits where the waiting box sat
            self._content[0].frame.grid_configure(pady=first_gap)

    async def _read_menu(self) -> tuple[MenuState, bool]:
        registry, main_py = self._registry, self._main_py
        state = await asyncio.to_thread(context_menu.menu_state, registry, main_py=main_py)
        legacy = await asyncio.to_thread(context_menu.legacy_installed, registry)
        return state, legacy

    def _destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is not self._page.frame:
            return
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._disconnect is not None:
            self._disconnect()
            self._disconnect = None

    async def _presets_saved(self, change: PresetsChange) -> None:
        """Naming's Save stored changed presets (before this section's Save runs): the chosen
        ones follow a rename and drop a deleted one, and a menu that starts this copy is
        rewritten. The form keeps the ticks not saved yet. Raises on a failure (Naming's Save
        logs it; the menu catches up at the next Forge start)."""
        parts = self._parts
        if parts is None:
            return
        stored = list(parts.settings.current.context_menu_presets)
        chosen = context_menu.follow_presets(stored, change.names, change.renamed, change.removed)
        if chosen != stored:
            await parts.settings.update(context_menu_presets=chosen)
        ticks = {
            change.renamed.get(name, name): bool(variable.get())
            for name, variable in self._ticks.items()
            if name not in change.removed
        }
        self._names, self._stored = list(change.names), chosen
        state, legacy = self.state, self.legacy
        if state.ours:
            registry, main_py = self._registry, self._main_py
            await asyncio.to_thread(
                context_menu.refresh, self._names, chosen, registry, main_py=main_py
            )
            state, legacy = await self._read_menu()
        if not self._alive():
            return
        self._show(state, legacy)
        self._fill_presets(chosen)
        for name, ticked in ticks.items():
            if name in self._ticks:
                self._ticks[name].set(ticked)

    # --- the form --------------------------------------------------------------------------------

    def _show(self, state: MenuState, legacy: bool) -> None:
        """Whose menu Explorer has, and the old HKLM menu's part."""
        self.state, self.legacy = state, legacy
        self.status_label.configure(
            text=t(STATUS[state]),
            text_color=theme.TEXT_MUTED if state in (MenuState.ON, MenuState.OFF) else theme.WARNING,
        )
        for widget in (self.legacy_label, self.legacy_button):
            if legacy:
                widget.grid()
            else:
                widget.grid_remove()

    def _fill_presets(self, chosen: list[str]) -> None:
        for child in self.presets_frame.winfo_children():
            child.destroy()
        self._ticks.clear()
        shown = context_menu.menu_presets(self._names, chosen)
        for index, name in enumerate(self._names):
            variable = tk.BooleanVar(master=self.presets_frame, value=name in shown)
            self._ticks[name] = variable
            self._checkbox(self.presets_frame, name, variable).grid(
                row=index // _COLUMNS,
                column=index % _COLUMNS,
                sticky="w",
                padx=(0, 24),
                pady=(0 if index < _COLUMNS else _ROW_GAP // 2, 0),
            )

    @property
    def on(self) -> bool:
        return bool(self._on.get())

    def set_on(self, on: bool) -> None:
        self._on.set(on)

    def ticked(self) -> list[str]:
        """The ticked presets, in the presets' order."""
        return [name for name, variable in self._ticks.items() if variable.get()]

    def set_ticked(self, names: list[str]) -> None:
        for name, variable in self._ticks.items():
            variable.set(name in names)

    @staticmethod
    def _chosen(ticked: list[str], names: list[str]) -> list[str]:
        """What context_menu_presets stores: [] when every preset is ticked (presets added
        later appear too), else the ticked ones that still exist."""
        kept = [name for name in names if name in ticked]
        return [] if len(kept) == len(names) else kept

    def changes(self) -> dict[str, Any]:
        """What Save would change: "on" (the menu) and / or "context_menu_presets"."""
        if self._parts is None:
            return {}
        found: dict[str, Any] = {}
        if self.on != self.state.ours:
            found["on"] = self.on
        ticked = self.ticked()
        shown = context_menu.menu_presets(self._names, self._stored)
        if ticked and ticked != shown:  # none ticked is no choice (`problem` says so when on)
            found["context_menu_presets"] = self._chosen(ticked, self._names)
        return found

    # --- the .reg files -----------------------------------------------------------------------------

    def save_reg_file(self) -> None:
        names = context_menu.menu_presets(self._names, self.ticked())
        entries = context_menu.build_entries(
            names, context_menu.command_prefix(main_py=self._main_py), context_menu.default_icon()
        )
        self._write_file(t(SAVE_REG_TITLE), REG_FILE_NAME, context_menu.to_reg_file(entries))

    def save_legacy_file(self) -> None:
        text = context_menu.legacy_removal_reg(self._registry)
        self._write_file(t(SAVE_LEGACY_TITLE), LEGACY_FILE_NAME, text)

    def _write_file(self, title: str, name: str, text: str) -> None:
        path = self.ask_save(title, name)
        if not path or not self._alive():
            return
        try:
            # UTF-16 as regedit writes them; the text has its own \r\n line ends.
            Path(path).write_text(text, encoding="utf-16", newline="")
        except OSError as exc:
            log.warning("A .reg file couldn't be saved (%s)", type(exc).__name__)
            self.file_label.configure(text=t(FILE_FAILED))
            self.file_label.grid(row=self._file_row, column=0, sticky="w", pady=(_ROW_GAP, 0))
            return
        self.file_label.grid_remove()

    # --- Save -------------------------------------------------------------------------------------

    def problem(self) -> str | None:
        """A menu that is to be on with no preset ticked can't be saved."""
        if self._parts is None or not self._names or not self.on:
            return None
        return t(PICK_ONE) if not self.ticked() else None

    async def save(self) -> str | None:
        """Store the chosen presets and put the menu right: written when it is to be on and was
        off, another program's, stale or showing other presets; removed when it is to be off
        and starts this copy."""
        parts = self._parts
        if parts is None:
            return None
        changes = self.changes()
        if not changes and self.state is not MenuState.STALE:
            return None
        on, ticked = self.on, self.ticked()
        registry, main_py = self._registry, self._main_py
        try:
            # The presets as they are now (a Naming change saved first may have renamed one).
            names = [preset.name for preset in await parts.presets.list()]
            chosen = self._chosen(ticked, names)
            if "context_menu_presets" in changes:
                await parts.settings.update(context_menu_presets=chosen)
            if on:
                await asyncio.to_thread(
                    context_menu.write_menu, names, chosen, registry, main_py=main_py
                )
            elif self.state.ours:
                await asyncio.to_thread(context_menu.remove_own, registry, main_py=main_py)
            state, legacy = await self._read_menu()
        except Exception as exc:
            # The type only: a message could name a path with the user's name in it.
            log.error("The right-click menu couldn't be changed (%s)", type(exc).__name__)
            return t(SAVE_FAILED)
        log.info("Right-click menu saved (%s)", state)
        if self._alive():
            self._names = names
            self._stored = list(parts.settings.current.context_menu_presets)
            self._show(state, legacy)
            self._fill_presets(self._stored)
            self._on.set(state.ours)
        return None


def build_menu(page: SettingsPage, forge: Any) -> MenuSection:
    """FORGE_SECTIONS' builder (app/forge/ui/settings_pages.py)."""
    return MenuSection(page, forge)
