"""Dialog for pasting a list of shows (names or TVMaze ids) to import."""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from app.ui import theme
from app.ui.branding import set_window_icon


class ImportShowsDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk | ctk.CTkToplevel,
        *,
        on_import: Callable[[list[str]], None],
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._on_import = on_import

        self.title("Import Shows")
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=25, pady=20)
        ctk.CTkLabel(
            body, text="Paste show names or TVMaze IDs, one per line:", text_color=theme.TEXT_MUTED
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
            text="Cancel",
            width=100,
            fg_color="transparent",
            border_width=1,
            border_color=theme.EDGE,
            hover_color=theme.SURFACE,
            command=self.destroy,
        ).pack(side="right")
        ctk.CTkButton(
            buttons,
            text="Import",
            width=100,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._import,
        ).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _event: self.destroy())
        self._center_on(master)
        # CTkToplevel can open behind its parent on Windows; raise and grab once it is mapped.
        self.after(150, self._activate)

    def _center_on(self, master: ctk.CTk | ctk.CTkToplevel) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_reqwidth()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _activate(self) -> None:
        if self.winfo_exists():
            self.lift()
            self.focus_force()
            self.grab_set()
            self._text.focus_set()

    def _import(self) -> None:
        lines = [line.strip() for line in self._text.get("1.0", "end").splitlines() if line.strip()]
        if not lines:
            self._hint.configure(text="Enter at least one show.")
            return
        self.destroy()
        self._on_import(lines)
