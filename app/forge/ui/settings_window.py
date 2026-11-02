"""MediaForge's own Settings window (docs/forge/INTEGRATION.md 3.7, the owner's decision of
2026-11-02: the two windows feel separate; what both share — language, theme, the TMDB key,
updates, tray, Start with Windows — stays only in AirGrabber's Settings).

ForgeSettingsWindow(master = the Forge window, forge = AirGrabber's ForgeRuntime, section=,
on_open_airgrabber=): laid out as AirGrabber's Settings (app/ui/settings_layout.py, shared with
app/ui/settings_dialog.py): a sidebar with MediaForge's sections (FORGE_SECTIONS of
app/forge/ui/settings_pages.py, their icons and order: Library & watching, Naming, Right-click
menu) and one scrolling page per section, built by each section's `build(page, forge)`. In the
Forge look's copper (the page carries it: `SettingsPage.look`), borderless with the copper close
button like Forge's other dialogs, MediaForge's icon, transient to the Forge window and placed
over it, sized as AirGrabber's Settings (the pages scroll on a short screen).

Save (D9): every section's `problem()` first — the first reason shows with its section and
nothing is stored; then each section's `save()` in order; a reason that comes back shows and the
window stays; else it closes. Cancel, ✕ and Escape change nothing. Each section waits for
`forge.start()` itself and says so while Forge is NOT_READY or FAILED.

Left of Cancel / Save: a muted line saying what is shared with AirGrabber, and a link-style
button "Open AirGrabber's Settings" (`on_open_airgrabber`, the host's: AirGrabber's window
shown from the tray first, its Settings at their default page). One at a time: ForgeHost
(app/forge/ui/host.py) keeps it and raises an open one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.forge.ui.look import forge_look
from app.forge.ui.settings_pages import FORGE_GROUP, FORGE_SECTIONS, ForgeSectionPart
from app.forge.ui.window import set_forge_icon
from app.i18n import N_, t
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.dialog_kit import (
    PAD_X,
    SMALL_SIZE,
    activate_later,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.settings_layout import (
    MAX_PAGE_HEIGHT,
    PAGE_BOTTOM,
    PAGE_PAD_LEFT,
    PAGE_WIDTH,
    SettingsPage,
    build_layout,
    fill_sidebar,
    fit_window,
    sidebar_width,
)
from app.ui.tasks import async_command

if TYPE_CHECKING:
    from app.forge.runtime import ForgeRuntime

log = logging.getLogger(__name__)

TITLE = N_("MediaForge Settings")
SHARED_NOTE = N_("Language, theme, TMDB key and updates are shared with AirGrabber.")
OPEN_AIRGRABBER = N_("Open AirGrabber's Settings")

_BUTTON_WIDTH = 140  # Cancel / Save, as AirGrabber's Settings
_BUTTON_GAP = 12
_FOOT_GAP = 16  # between the shared line and Cancel
# The shared line beside Cancel / Save (CTk units): the page's width less the buttons.
_SHARED_WRAP = PAGE_WIDTH - 2 * _BUTTON_WIDTH - _BUTTON_GAP - _FOOT_GAP - PAD_X
_LINK_SIZE = 12  # as the Auto-rename page's "More in Settings…"
_LINK_HEIGHT = 26


class ForgeSettingsWindow(ctk.CTkToplevel):
    """`ForgeSettingsWindow(master, forge, section=, on_open_airgrabber=, look=)`; build it
    while the event loop runs (the sections start their own tasks)."""

    def __init__(
        self,
        master: Any,
        forge: ForgeRuntime,
        *,
        section: str | None = None,
        on_open_airgrabber: Callable[[], None] | None = None,
        look: theme.Look | None = None,
    ) -> None:
        self.look: theme.Look = forge_look() if look is None else look
        look = self.look
        super().__init__(master, fg_color=look.BG)
        self.parts: dict[str, ForgeSectionPart] = {}  # each section's Save side, in order
        self._pages: dict[str, SettingsPage] = {}
        self._on_open_airgrabber = on_open_airgrabber or (lambda: None)

        self.title(t(TITLE))
        set_forge_icon(self)  # MediaForge's .ico, read once per run (not per window)
        self.resizable(False, False)
        self.transient(master)

        layout = build_layout(self, sidebar_width(FORGE_SECTIONS), look=look)
        body = layout.body
        self._sidebar = layout.sidebar
        self._scroll = layout.scroll

        # Under the page: the error line, then the shared line and its link, Cancel and Save.
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.grid(row=1, column=2, sticky="ew", padx=(PAGE_PAD_LEFT, PAD_X), pady=(10, 20))
        self._error = ctk.CTkLabel(
            footer,
            text="",
            font=theme.font(14),
            text_color=theme.DANGER,
            justify="left",
            anchor="w",
            wraplength=PAGE_WIDTH,
        )
        self._error.pack(anchor="w")
        row = ctk.CTkFrame(footer, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        row.grid_columnconfigure(0, weight=1)
        shared = ctk.CTkFrame(row, fg_color="transparent")
        shared.grid(row=0, column=0, sticky="w", padx=(0, _FOOT_GAP))
        self.shared_label = text_label(
            shared, t(SHARED_NOTE), size=SMALL_SIZE, color=theme.TEXT_MUTED, wraplength=_SHARED_WRAP
        )
        self.shared_label.pack(anchor="w")
        self.airgrabber_button = ctk.CTkButton(
            shared,
            text=t(OPEN_AIRGRABBER),
            width=10,
            height=_LINK_HEIGHT,
            fg_color="transparent",
            hover_color=look.CHIP,
            text_color=look.LINK,
            font=theme.font(_LINK_SIZE, underline=True),
            command=self.open_airgrabber_settings,
        )
        self.airgrabber_button.pack(anchor="w")
        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.grid(row=0, column=1, sticky="e")
        self.cancel_button = secondary_button(
            buttons, t("Cancel"), self.cancel, width=_BUTTON_WIDTH, look=look
        )
        self.cancel_button.pack(side="right")
        self._save_button = primary_button(
            buttons, t("Save"), async_command(self._save), width=_BUTTON_WIDTH, look=look
        )
        self._save_button.pack(side="right", padx=(0, _BUTTON_GAP))

        for name, forge_section in FORGE_SECTIONS.items():
            page = SettingsPage(self._scroll, t(name), t(forge_section.note), look=look)
            self._pages[name] = page
            self.parts[name] = forge_section.build(page, forge)
        icons = {name: forge_section.icon for name, forge_section in FORGE_SECTIONS.items()}
        groups = [(FORGE_GROUP, list(FORGE_SECTIONS))]
        self._items = fill_sidebar(self._sidebar, groups, icons, self.show_section, look=look)
        first = next(iter(FORGE_SECTIONS))
        self._section = section if section in self._pages else first
        self.show_section(self._section)

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _event: self.cancel())
        self.borderless = make_borderless(
            self, drag_widgets=(self, body, self._sidebar), on_close=self.cancel, look=look
        )
        # As AirGrabber's Settings: its pages as tall as its tallest (MediaForge's fill once
        # Forge has started, so they are given that room from the start), less on a short screen.
        fit_window(self, self._scroll, MAX_PAGE_HEIGHT)
        place_over(self, master)
        activate_later(self)

    # -- sections ------------------------------------------------------------------------------

    @property
    def section(self) -> str:
        """The section whose page shows."""
        return self._section

    @property
    def sections(self) -> list[str]:
        """The sidebar's sections, in order."""
        return list(self._items)

    def page(self, section: str) -> ctk.CTkFrame:
        """A section's page frame (tests look inside it)."""
        return self._pages[section].frame

    def show_section(self, section: str) -> None:
        """Show `section`'s page; one that isn't here changes nothing."""
        if section not in self._pages:
            return
        self._section = section
        for name, page in self._pages.items():
            if name != section:
                page.frame.grid_remove()
        self._pages[section].frame.grid(row=0, column=0, sticky="ew", pady=(0, PAGE_BOTTOM))
        self._scroll._parent_canvas.yview_moveto(0)  # each page starts at its top
        for name, item in self._items.items():
            item.set_selected(name == section)

    # -- the buttons ---------------------------------------------------------------------------

    def open_airgrabber_settings(self) -> None:
        """The link: AirGrabber's Settings (the host's callback)."""
        try:
            self._on_open_airgrabber()
        except Exception:
            log.exception("Opening AirGrabber's Settings failed")

    def cancel(self) -> None:
        """Cancel, ✕ and Escape: nothing is stored."""
        if self.winfo_exists():
            self.destroy()

    def _show_problem(self, text: str, section: str) -> None:
        """The error line, and the page with the problem."""
        self._error.configure(text=text)
        self.show_section(section)

    async def _save(self) -> None:
        if await self._save_all() and self.winfo_exists():
            self.destroy()

    async def _save_all(self) -> bool:
        """Check every section, then store them in order (D9). False (the window shows why)
        when a section had a problem or couldn't be stored."""
        self._error.configure(text="")
        for name, part in self.parts.items():  # asked of every section before anything is stored
            problem = part.problem()
            if problem is not None:
                self._show_problem(problem, name)
                return False
        self._save_button.configure(state="disabled")
        for name, part in self.parts.items():
            problem = await part.save()
            if problem is not None:
                if self.winfo_exists():
                    self._show_problem(problem, name)
                    self._save_button.configure(state="normal")
                return False
        if self.winfo_exists():
            self._save_button.configure(state="normal")
        return True
