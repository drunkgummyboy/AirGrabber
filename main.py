"""AirGrabber entry point: one copy at a time, check the packages, build the services, open the root
window, run the async Tk loop.

`--tray` starts with only the tray icon showing (the Start-with-Windows shortcut passes it).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Stdlib-only imports up here: the third-party packages may not be installed until ensure_requirements().
from app.bootstrap import ensure_requirements
from app.config import AppPaths, configure_logging
from app.identity import set_process_app_id
from app.single_instance import InstanceLock

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

log = logging.getLogger("airgrabber")
_background_tasks: set[asyncio.Task[Any]] = set()  # strong references until they finish


@dataclass(frozen=True, slots=True)
class Options:
    tray: bool = False  # start hidden: only the tray icon shows


def parse_options(argv: Sequence[str]) -> Options:
    """The command line; anything unknown is ignored (pythonw.exe has no console to complain on)."""
    parser = argparse.ArgumentParser(prog="AirGrabber", add_help=False)
    parser.add_argument("--tray", action="store_true")
    namespace, _unknown = parser.parse_known_args(list(argv))
    return Options(tray=bool(namespace.tray))


def main() -> int:
    options = parse_options(sys.argv[1:])
    paths = AppPaths.default().ensure()
    configure_logging(paths)
    log.info("=== AirGrabber starting (data: %s) ===", paths.root)
    # Its own name and icon on notifications and its own taskbar group (app/identity.py), not
    # Python's: before any window (the package check's included) or the tray icon exists.
    set_process_app_id()
    instance = InstanceLock()
    if not instance.acquire():
        instance.signal_show()
        instance.release()
        log.info("AirGrabber is already running: asked it to show its window; exiting")
        return 0
    try:
        try:
            if not ensure_requirements(paths):
                return 1
        except Exception:
            log.exception("Package check failed")
            _show_fatal_error(paths)
            return 1
        return _run(paths, instance, start_hidden=options.tray)
    finally:
        instance.release()


def _run(paths: AppPaths, instance: InstanceLock, *, start_hidden: bool) -> int:
    from app.services import Services
    from app.ui import tasks
    from app.ui.main_window import MainWindow

    # One event loop for everything. aiosqlite/httpx awaitables and Tk callbacks all run on it,
    # so a coroutine can update widgets directly after an `await` — no thread pools, no queues.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    services: Services | None = None
    window: MainWindow | None = None
    restart = False
    try:
        services = loop.run_until_complete(Services.create(paths))
        window = MainWindow(services, start_hidden=start_hidden, show_requests=instance.show_requested)
        _fix_start_menu_shortcut_soon(loop)
        window.async_mainloop(event_loop=loop)  # returns once the window is closed for good (Quit)
        restart = window.restart_requested
        return 0
    except Exception:
        log.exception("Fatal error")
        _show_fatal_error(paths)
        return 1
    finally:
        loop.run_until_complete(tasks.cancel_all())
        if services is not None:
            # Must run: aiosqlite's worker thread is not a daemon and would keep the process alive.
            loop.run_until_complete(services.aclose())
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        _apply_staged_update()
        if window is not None:
            _stop_tray(window)  # last: the icon stays until everything else has shut down
        log.info("=== AirGrabber stopped ===")
        if restart:
            instance.release()  # else the new copy would find this one still running
            _relaunch()


def _fix_start_menu_shortcut_soon(loop: asyncio.AbstractEventLoop) -> None:
    """Once the loop runs, in the background: give this install's Start-menu shortcut AirGrabber's
    AppUserModelID if an older install left it without one (app/services/app_identity.py).

    A plain task on the loop, not app.ui.tasks.spawn: it is no UI work, so a failure must not
    reach the window; it logs its own failures and the done-callback logs anything unexpected.
    Like every task, tasks.cancel_all() stops it at exit.
    """
    from app.services.app_identity import ensure_start_menu_app_id

    def start() -> None:
        task = loop.create_task(ensure_start_menu_app_id(), name="start-menu-app-id")
        _background_tasks.add(task)
        task.add_done_callback(_background_done)

    loop.call_soon(start)  # nothing starts before the loop runs


def _background_done(task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        log.error("Background task %s failed", task.get_name(), exc_info=error)


def _apply_staged_update() -> None:
    """Swap in an update that was downloaded in the background, now that the app has shut down.

    Never raises: a failed update must not break closing (or restarting) AirGrabber.
    """
    try:
        # Third-party packages are importable by now (the package check passed earlier).
        from app.services.install_info import ensure_uninstall_entry
        from app.services.updates import apply_staged

        install_dir = Path(__file__).resolve().parent
        version = apply_staged(install_dir)  # logs what it did; None: nothing installed
        if version is not None:
            ensure_uninstall_entry(install_dir, version)  # "Installed apps" shows the new version
    except Exception:
        log.exception("Installing the downloaded update failed")


def _stop_tray(window: MainWindow) -> None:
    """Remove the tray icon (also when restarting). Never raises."""
    try:
        window.tray.stop()
    except Exception:
        log.exception("Stopping the tray icon failed")


def _relaunch() -> None:
    """Start a fresh copy (after an update), now that this one has released the database."""
    script = Path(__file__).resolve()
    log.info("Restarting")
    try:
        subprocess.Popen([sys.executable, str(script)], cwd=script.parent)
    except OSError:
        log.exception("Could not restart AirGrabber")


def _show_fatal_error(paths: AppPaths) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "AirGrabber",
            f"AirGrabber hit an unrecoverable error.\n\nDetails are in:\n{paths.logs / 'airgrabber.log'}",
        )
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
