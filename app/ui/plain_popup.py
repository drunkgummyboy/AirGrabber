"""PlainPopup: the plain Tk window under a borderless pop-up that holds CTk widgets (PopupMenu,
the hover tips, the spotlight band's tips).

customtkinter's ScalingTracker registers the window of every CTk widget, a plain Toplevel
included, and at a Windows scaling change makes it see-through (alpha 0.15), calls its
`block_update_dimensions_event`, rescales its widgets, calls `unblock_update_dimensions_event`
and makes it solid again. Only CTk's own windows had those two: on a plain Toplevel the
AttributeError left the pop-up see-through and ended CTk's scaling checks for the rest of the
run. Here they do nothing (a plain Toplevel has no CTk <Configure> handler to hold back).

Its own module, below app/ui/widgets.py: that one imports missing_control, which needs this."""

from __future__ import annotations

import tkinter as tk


class PlainPopup(tk.Toplevel):
    """A tk.Toplevel that customtkinter's scaling check can go through."""

    def block_update_dimensions_event(self) -> None:
        """Nothing to hold back (CTk's windows hold back their <Configure> handling here)."""

    def unblock_update_dimensions_event(self) -> None:
        """Nothing was held back."""
