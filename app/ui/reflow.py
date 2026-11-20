"""Wrapping texts that follow a window made wider than it was built (Settings, a rule's editor).

    follow_width(root, extra)

A window builds its texts for its first width: each wrapping label gets a `wraplength` made
from it. Dragged wider by `extra` (logical units), every wrapping text inside `root` that is
at least MIN_FOLLOW wide gets that much more room: a label's `wraplength` (CTk scales it), and
a switch's, tick's or radio button's own text label (plain Tk: physical pixels). A narrower
text (a caption beside a field, a fixed column) keeps its width. What a label wrapped at when
first seen is kept on it, so the room is always its first plus `extra`: a label made later (a
page built at its first visit) follows on the next call.

A widget (or any frame above it) with a `reflow_share` attribute gets that part of `extra`:
two cards side by side each get half (`reflow_share = 0.5`), something in a column of fixed
width none (0).

The window itself never gets narrower than it was built (its `minsize`): fields and buttons
keep the widths they were made with.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

import customtkinter as ctk

MIN_FOLLOW = 200  # logical: a narrower wrap is a column's or a caption's, it keeps its width

_BASE = "_reflow_base"  # the wrap a widget had when first seen (its own units)
_TOGGLES = (ctk.CTkSwitch, ctk.CTkCheckBox, ctk.CTkRadioButton)


def follow_width(root: tk.Misc, extra: int) -> None:
    """Give every wrapping text inside `root` its first room plus `extra` (logical, ≥ 0; times
    its `reflow_share`)."""
    extra = max(0, int(extra))
    scaling = float(ctk.ScalingTracker.get_widget_scaling(root)) or 1.0
    stack: list[tuple[tk.Misc, float]] = [(root, _share(root, 1.0))]
    while stack:
        widget, share = stack.pop()
        for child in widget.winfo_children():
            child_share = _share(child, share)
            if isinstance(child, ctk.CTkLabel):
                _follow(child, round(extra * child_share), scale=1.0)
                continue  # its own canvas and label are CTk's
            if isinstance(child, _TOGGLES):
                label = getattr(child, "_text_label", None)
                if label is not None:
                    _follow(label, round(extra * child_share * scaling), scale=scaling)
                continue
            stack.append((child, child_share))


def _share(widget: tk.Misc, inherited: float) -> float:
    own = getattr(widget, "reflow_share", None)
    return inherited if own is None else inherited * float(own)


def _follow(label: Any, more: int, *, scale: float) -> None:
    """`label`'s wrap: its first plus `more` (its own units; `scale`: physical per logical)."""
    base: int | None = getattr(label, _BASE, None)
    if base is None:
        try:
            base = int(float(label.cget("wraplength") or 0))
        except (tk.TclError, ValueError):
            return
        setattr(label, _BASE, base)
    if base < MIN_FOLLOW * scale:
        return
    wanted = base + more
    try:
        if int(float(label.cget("wraplength") or 0)) != wanted:
            label.configure(wraplength=wanted)
    except tk.TclError:
        pass  # gone meanwhile
