"""MediaForge's dock on every page of the main window (the owner, 2026-11-16: "display the same
loading bar from the forge tab on every page when something is going").

`RunDock` is the page's own `ForgeProgress` (app/forge/ui/widgets/chips.py), with the same look
and words ("Renaming 2 of 5 · Dune"), fed by Forge's `RunProgress`
(app/forge/core/run_progress.py) instead of the queue view: `set_state` shows a state's words,
how far, and a Cancel when the run has one (the page's run: the same `_cancel_clicked` as its own
Cancel; the renamer's and a confirm window's runs have none on the page either). A click on the
panel (not on Cancel) opens MediaForge's page (`on_open`). The main window builds it once Forge
has started and grids it in a row of its own under the page on screen (app/ui/main_window.py
`_show_run_dock`); it is imported only then, never at AirGrabber's start.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.forge.core.run_progress import IDLE, RunState
from app.forge.ui.look import app_look
from app.forge.ui.widgets.chips import ForgeProgress
from app.i18n import t

DOCK_PAD = (4, 8)  # CTk units above (under the page's own 5) and below it (above the status bar)


class RunDock(ForgeProgress):
    def __init__(self, master: Any, *, on_open: Callable[[], None]) -> None:
        look = app_look()
        super().__init__(master, look=look, ground=look.BG)
        self.state = IDLE
        self.cancel_button = self.action_button(t("Cancel"), self._cancel_clicked)
        self.canvas.configure(cursor="hand2")
        self.canvas.bind("<Button-1>", lambda _event: on_open())

    def set_state(self, state: RunState) -> None:
        self.state = state
        self.show(state.fraction, state.text)
        self.show_action(self.cancel_button, state.cancel is not None)

    def _cancel_clicked(self) -> None:
        cancel = self.state.cancel
        if cancel is not None:
            cancel()
