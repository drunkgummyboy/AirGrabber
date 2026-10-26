"""Dialog for importing shows: paste a list of names / TVMaze ids, or bring them over from
airdates.tv (the shared AirdatesImportPanel)."""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.airdates_import import AirdatesImportPanel, PreviewFinder, ShowImporter
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import CONTENT_WIDTH, PAD_X, PAD_Y, activate_later, place_over
from app.ui.widgets import Tabview

# The tabs' texts (CTkTabview names a tab by its text: always through t()).
TAB_NAMES = N_("Show names")
TAB_AIRDATES = N_("From airdates.tv")

_TAB_PAD = 24  # around the content inside a tab
_TABS_WIDTH = CONTENT_WIDTH + 2 * _TAB_PAD
_TABS_HEIGHT = 560


class ImportShowsDialog(ctk.CTkToplevel):
    """Two tabs: "Show names" (paste names or TVMaze ids; Import hands them to `on_import`) and
    "From airdates.tv" (the panel imports by itself)."""

    def __init__(
        self,
        master: ctk.CTk | ctk.CTkToplevel,
        *,
        on_import: Callable[[list[str]], None],
        airdates: PreviewFinder,
        library: ShowImporter,
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._on_import = on_import

        self.title(t("Import Shows"))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        self._tabs = Tabview(
            self,
            width=_TABS_WIDTH,
            height=_TABS_HEIGHT,
            corner_radius=15,
            border_width=1,
            fg_color=theme.TAB_BG,
            border_color=theme.EDGE,
            segmented_button_fg_color=theme.SURFACE,
            segmented_button_selected_color=theme.SELECTED,
            segmented_button_selected_hover_color=theme.SELECTED_HOVER,
            segmented_button_unselected_color=theme.SURFACE,
            segmented_button_font=theme.font(15, bold=True),
            text_color=theme.BUTTON_TEXT,
            command=self._focus_tab,
        )
        self._tabs.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        self._build_names(self._tabs.add(t(TAB_NAMES)))
        self._panel = AirdatesImportPanel(
            self._tabs.add(t(TAB_AIRDATES)), airdates=airdates, library=library
        )
        self._panel.pack(fill="x", padx=_TAB_PAD, pady=(_TAB_PAD - 8, 0))
        self._tabs.set(t(TAB_NAMES))

        self.bind("<Escape>", lambda _event: self.destroy())
        place_over(self, master)
        activate_later(self)
        self.after(200, self._focus_tab)

    def _build_names(self, tab: ctk.CTkFrame) -> None:
        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=25, pady=20)
        ctk.CTkLabel(
            body,
            text=t("Paste show names or TVMaze IDs, one per line:"),
            text_color=theme.TEXT_MUTED,
        ).pack(anchor="w", pady=(0, 6))
        self._text = ctk.CTkTextbox(
            body, width=460, height=250, fg_color=theme.SURFACE, border_color=theme.EDGE, border_width=1
        )
        self._text.pack(fill="both", expand=True)

        self._hint = ctk.CTkLabel(body, text="", text_color=theme.DANGER)
        self._hint.pack(anchor="w", pady=(6, 0))

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(anchor="e", pady=(6, 0))
        ctk.CTkButton(
            buttons,
            text=t("Cancel"),
            width=100,
            fg_color="transparent",
            border_width=1,
            border_color=theme.EDGE,
            hover_color=theme.SURFACE,
            text_color=theme.BUTTON_TEXT,
            command=self.destroy,
        ).pack(side="right")
        ctk.CTkButton(
            buttons,
            text=t("Import"),
            width=100,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._import,
        ).pack(side="right", padx=(0, 8))

    def show_airdates(self) -> None:
        self._tabs.set(t(TAB_AIRDATES))
        self._focus_tab()

    def _focus_tab(self) -> None:
        if not self.winfo_exists():
            return
        if self._tabs.get() == t(TAB_AIRDATES):
            self._panel.focus_paste()
        else:
            self._text.focus_set()

    def _import(self) -> None:
        lines = [line.strip() for line in self._text.get("1.0", "end").splitlines() if line.strip()]
        if not lines:
            self._hint.configure(text=t("Enter at least one show."))
            return
        self.destroy()
        self._on_import(lines)
