"""ScrollableFrame: customtkinter's CTkScrollableFrame that lets go of what it binds app-wide.

CTkScrollableFrame binds the mouse wheel and the Shift keys on "all" (every widget of the
app) and never takes those bindings back. Tk holds their callbacks, out of the garbage
collector's sight, and through them the frame and the window it sits in: every closed window
with a scrolling part (Settings, an info window, the search results, …) stayed in memory for
the rest of the run, and every turn of the mouse wheel went past all of them. This one notes
each binding and takes it back when it is destroyed. Use it (or a subclass) wherever app code
would use CTkScrollableFrame; tests/test_closed_windows.py checks that nothing uses the
original."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

import customtkinter as ctk


class ScrollableFrame(ctk.CTkScrollableFrame):
    def bind_all(
        self, sequence: str | None = None, func: Callable[..., object] | None = None, add: Any = None
    ) -> Any:
        """CTkScrollableFrame's own bindings on "all" go through here (its __init__ calls it)."""
        funcid = super().bind_all(sequence, func, add)
        if sequence is not None and isinstance(funcid, str):
            self.__dict__.setdefault("_all_bindings", []).append((sequence, funcid))
        return funcid

    def destroy(self) -> None:
        bindings: list[tuple[str, str]] = self.__dict__.pop("_all_bindings", [])
        if bindings:
            try:
                root = self.nametowidget(".")
                for sequence, funcid in bindings:
                    _unbind_all(root, sequence, funcid)
            except tk.TclError:
                pass  # the app is going (its interpreter takes the bindings along)
        super().destroy()


def _unbind_all(root: tk.Misc, sequence: str, funcid: str) -> None:
    """Take one callback (`funcid`, from bind_all) off `sequence` on "all", leaving the others."""
    script = str(root.tk.call("bind", "all", sequence))
    kept = "\n".join(line for line in script.split("\n") if funcid not in line)
    root.tk.call("bind", "all", sequence, kept if kept.strip() else "")
    root.deletecommand(funcid)
