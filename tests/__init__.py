"""AirGrabber's tests (stdlib unittest: python -m unittest discover -s tests -t .).

No test changes a real window's frame: the pop-ups made borderless (app/ui/borderless.py) get
their close button and drag bindings, but the Win32 / DWM side is a stand-in that does
nothing (`InertNative`), so every window keeps the title bar Tk gave it."""

from __future__ import annotations

from app.ui import borderless


class InertNative:
    """Borderless windows' Win32 / DWM side, doing nothing."""

    def attach(self, window_id: int, *, dark: bool) -> bool:
        return True

    def start_drag(self, window_id: int) -> None:
        pass

    def set_dark(self, window_id: int, dark: bool) -> None:
        pass


borderless.set_native(InertNative())
