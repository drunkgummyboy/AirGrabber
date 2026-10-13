"""AirGrabber entry point: check the packages, build the services, open the root window, run the async Tk loop."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

# Stdlib-only imports up here: the third-party packages may not be installed until ensure_requirements().
from app.bootstrap import ensure_requirements
from app.config import AppPaths, configure_logging

log = logging.getLogger("airgrabber")


def main() -> int:
    paths = AppPaths.default().ensure()
    configure_logging(paths)
    log.info("=== AirGrabber starting (data: %s) ===", paths.root)
    try:
        if not ensure_requirements(paths):
            return 1
    except Exception:
        log.exception("Package check failed")
        _show_fatal_error(paths)
        return 1
    return _run(paths)


def _run(paths: AppPaths) -> int:
    from app.services import Services
    from app.ui import tasks
    from app.ui.main_window import MainWindow

    # One event loop for everything. aiosqlite/httpx awaitables and Tk callbacks all run on it,
    # so a coroutine can update widgets directly after an `await` — no thread pools, no queues.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    services: Services | None = None
    restart = False
    try:
        services = loop.run_until_complete(Services.create(paths))
        window = MainWindow(services)
        window.async_mainloop(event_loop=loop)  # returns once the window is closed
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
        log.info("=== AirGrabber stopped ===")
        if restart:
            _relaunch()


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
