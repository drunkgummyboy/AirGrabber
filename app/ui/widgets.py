"""Reusable widgets."""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.services.images import ImageCache


def destroy_tree(widget: Any) -> None:
    """Destroy `widget` and its descendants with one Tk call.

    tkinter's destroy() walks the tree in Python and destroys every descendant with its own Tk
    call, child first: ~0.5 ms per widget on Windows, ~600 ms for a three-week calendar grid.
    A single Tk `destroy` of the root takes the whole subtree down at once (~40 ms); destroy()
    then only releases the Python side (CTk callbacks, poster loads), its own Tk calls being
    no-ops.
    """
    widget.tk.call("destroy", str(widget))
    widget.destroy()


class MessagePanel(ctk.CTkFrame):
    """A centred title + explanation, for empty, error and not-yet-built states.

    `action` adds a button under the text: (label, callback).
    """

    def __init__(
        self,
        master: Any,
        title: str,
        body: str = "",
        *,
        action: tuple[str, Callable[[], None]] | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.place(relx=0.5, rely=0.45, anchor="center")
        ctk.CTkLabel(inner, text=title, font=theme.font(20, bold=True), text_color=theme.TEXT).pack()
        if body:
            ctk.CTkLabel(
                inner,
                text=body,
                font=theme.font(13),
                text_color=theme.TEXT_MUTED,
                justify="center",
                wraplength=560,
            ).pack(pady=(8, 0))
        if action is not None:
            label, command = action
            ctk.CTkButton(
                inner,
                text=label,
                width=120,
                height=30,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                command=command,
            ).pack(pady=(16, 0))


class FlexGrid(ctk.CTkFrame):
    """Grids its children row-major into as many columns as fit its width.

    Children are re-flowed on resize only when the column count changes. Call `relayout()`
    after adding or removing children.
    """

    def __init__(
        self, master: Any, *, item_width: int, pad_x: int = 8, pad_y: int = 10, **kwargs: Any
    ) -> None:
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        self.item_width = item_width
        self.pad_x = pad_x
        self.pad_y = pad_y
        self._current_cols = 0
        self.bind("<Configure>", self._on_resize, add="+")

    def _columns(self, width: int) -> int:
        # Widths from Tk are physical pixels; item_width/pad_x are unscaled CTk units.
        slot = (self.item_width + self.pad_x * 2) * ctk.ScalingTracker.get_widget_scaling(self)
        return max(1, int(width // slot))

    def _on_resize(self, event: tk.Event[Any]) -> None:
        if event.width < 10:
            return
        cols = self._columns(event.width)
        if cols == self._current_cols:
            return
        self._current_cols = cols
        self._regrid(cols)

    def _regrid(self, cols: int) -> None:
        for idx, child in enumerate(self.winfo_children()):
            child.grid(
                row=idx // cols, column=idx % cols, padx=self.pad_x, pady=self.pad_y, sticky="nw"
            )

    def relayout(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        if width < 10:
            width = self.master.winfo_width()
        cols = self._columns(width) if width > 10 else 1
        self._current_cols = cols
        self._regrid(cols)


class PosterImage(ctk.CTkFrame):
    """A fixed-size poster slot: a placeholder until `show()` has loaded the image.
    `set_on_click` makes it clickable."""

    def __init__(self, master: Any, *, width: int = 68, height: int = 100, **kwargs: Any) -> None:
        kwargs.setdefault("fg_color", theme.PLACEHOLDER)
        kwargs.setdefault("corner_radius", 5)
        super().__init__(master, width=width, height=height, **kwargs)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self._size = (width, height)
        self._image: ctk.CTkImage | None = None
        self._load: asyncio.Task[None] | None = None
        self._on_click: Callable[[], object] | None = None
        self.bind("<Button-1>", self._clicked, add="+")
        self._label = self._new_label()

    def _new_label(self) -> ctk.CTkLabel:
        label = ctk.CTkLabel(self, text="", cursor=self._cursor())
        label.place(relx=0.5, rely=0.5, anchor="center")
        label.bind("<Button-1>", self._clicked, add="+")
        return label

    def set_on_click(self, command: Callable[[], object] | None) -> None:
        """What a click on the poster does (None: nothing); a hand cursor shows while it does."""
        self._on_click = command
        self.configure(cursor=self._cursor())
        self._label.configure(cursor=self._cursor())

    def _cursor(self) -> str:
        return "hand2" if self._on_click is not None else ""

    def _clicked(self, _event: Any) -> None:
        if self._on_click is not None:
            self._on_click()

    def show(self, images: ImageCache, url: str | None) -> None:
        """Load `url` (None: keep the placeholder), replacing any load still in flight."""
        self._cancel_load()
        if self._image is not None:
            # CTkLabel cannot drop an image once set; start over with a bare label.
            self._label.destroy()
            self._label = self._new_label()
            self._image = None
        if url is None:
            return
        self._load = spawn(self._fetch(images, url), name="PosterImage.show")

    async def _fetch(self, images: ImageCache, url: str) -> None:
        width, height = self._size
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        img = await images.get(url, (round(width * scaling), round(height * scaling)))
        if img is None or not self.winfo_exists():
            return
        self._image = ctk.CTkImage(light_image=img, dark_image=img, size=(width, height))
        self._label.configure(image=self._image)

    def _cancel_load(self) -> None:
        if self._load is not None and not self._load.done():
            self._load.cancel()
        self._load = None

    def destroy(self) -> None:
        self._cancel_load()
        super().destroy()


class LoadingBar(ctk.CTkProgressBar):
    """A thin indeterminate progress bar that animates for as long as it exists."""

    def __init__(self, master: Any, **kwargs: Any) -> None:
        kwargs.setdefault("height", 4)
        kwargs.setdefault("progress_color", theme.ACCENT)
        super().__init__(master, mode="indeterminate", **kwargs)
        self.start()

    def destroy(self) -> None:
        self.stop()
        super().destroy()


def show_info(master: ctk.CTk | ctk.CTkToplevel, title: str, message: str) -> None:
    """A small modal message box with an OK button, centred over `master`."""
    dialog = ctk.CTkToplevel(master, fg_color=theme.BG)
    dialog.title(title)
    set_window_icon(dialog)
    dialog.resizable(False, False)
    dialog.transient(master)

    body = ctk.CTkFrame(dialog, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=25, pady=20)
    ctk.CTkLabel(
        body,
        text=message,
        font=theme.font(13),
        text_color=theme.TEXT,
        wraplength=380,
        justify="left",
    ).pack(anchor="w")
    ctk.CTkButton(
        body,
        text="OK",
        width=100,
        fg_color=theme.ACCENT,
        hover_color=theme.ACCENT_HOVER,
        command=dialog.destroy,
    ).pack(anchor="e", pady=(16, 0))

    dialog.bind("<Escape>", lambda _event: dialog.destroy())
    dialog.bind("<Return>", lambda _event: dialog.destroy())

    dialog.update_idletasks()
    x = master.winfo_rootx() + (master.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = master.winfo_rooty() + (master.winfo_height() - dialog.winfo_reqheight()) // 3
    dialog.geometry(f"+{max(0, x)}+{max(0, y)}")

    def activate() -> None:
        # CTkToplevel can open behind its parent on Windows; raise and grab once it is mapped.
        if dialog.winfo_exists():
            dialog.lift()
            dialog.focus_force()
            dialog.grab_set()

    dialog.after(150, activate)
