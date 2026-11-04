"""AirGrabber entry point: one copy at a time, check the packages, build the services, open the root
window, run the async Tk loop.

`--tray` starts with only the tray icon showing (the Start-with-Windows shortcut passes it).
`--setup-qbittorrent` only sets qBittorrent up for AirGrabber (no "Add new torrent" pop-up for
each download, AirGrabber's look; app/services/qbittorrent_setup.py) and exits: install.ps1 runs it right after it
installed qBittorrent. On a new AirGrabber install it also turns qBittorrent's Web UI on (this PC only)
and points AirGrabber at it (app/services/qbittorrent_local.py). First it makes Windows open magnet
links and .torrent files with qBittorrent when no other torrent program is set (its installer
doesn't; app/services/torrent_app.py). No window; the exit code says how it went (`EXIT_CODES`
there).
`--forge [--confirm] [--preset NAME] [PATH …]` is a request for Forge (MediaForge inside AirGrabber):
the Explorer right-click menu starts one process per selected file with it. When AirGrabber already
runs, the request is handed over (app/single_instance.py's mailbox) before logging and the package
check, so many starts stay cheap; a first start keeps it in `Options.forge_request` and starts with
the main window hidden: a plain request then shows it on MediaForge's page, a confirm request only
its confirm window (app/ui/forge_link.py: when that closes it stays in the tray or quits, D5).
`--forge-menu on|off` only turns this copy's Explorer right-click menu on or off (D8: install.ps1
turns it on for a new install; app/forge/context_menu.py, which leaves another program's menu
alone) and exits: no window, no single-instance lock; the exit code says how it went (`EXIT_*`
there).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Stdlib-only imports up here: the third-party packages may not be installed until ensure_requirements().
from app.bootstrap import ensure_requirements
from app.config import AppPaths, configure_logging
from app.i18n import set_language, t
from app.identity import set_process_app_id
from app.single_instance import ForgeRequest, InstanceLock, hand_over

if TYPE_CHECKING:
    from app.services import Services
    from app.ui.main_window import MainWindow

log = logging.getLogger("airgrabber")
_background_tasks: set[asyncio.Task[Any]] = set()  # strong references until they finish


@dataclass(frozen=True, slots=True)
class Options:
    tray: bool = False  # start hidden: only the tray icon shows
    setup_qbittorrent: bool = False  # only set qBittorrent up for AirGrabber, then exit
    forge: bool = False  # a request for Forge: MediaForge's page, or with `confirm` the confirm window
    confirm: bool = False  # "Rename N files?" for `paths` (the right-click menu)
    preset: str | None = None  # the naming preset to use (the right-click menu's entry)
    paths: tuple[str, ...] = ()  # files and folders, as given
    forge_menu: str | None = None  # "on" / "off": only change the right-click menu, then exit

    @property
    def forge_request(self) -> ForgeRequest | None:
        """The Forge request this start carries (only with `--forge`), paths made absolute: the
        running copy that may take it has another working folder."""
        if not self.forge:
            return None
        return ForgeRequest(
            paths=tuple(os.path.abspath(path) for path in self.paths),
            preset=self.preset,
            confirm=self.confirm,
        )


def parse_options(argv: Sequence[str]) -> Options:
    """The command line; anything unknown is ignored (pythonw.exe has no console to complain on)."""
    parser = argparse.ArgumentParser(prog="AirGrabber", add_help=False)
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--setup-qbittorrent", action="store_true")
    parser.add_argument("--forge", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--preset")
    parser.add_argument("--forge-menu", nargs="?", const="")  # alone: "" (not on / off)
    parser.add_argument("paths", nargs="*")
    namespace, _unknown = parser.parse_known_intermixed_args(_joined_preset(argv))
    preset = namespace.preset
    return Options(
        tray=bool(namespace.tray),
        setup_qbittorrent=bool(namespace.setup_qbittorrent),
        forge=bool(namespace.forge),
        confirm=bool(namespace.confirm),
        preset=preset if preset else None,
        paths=tuple(str(path) for path in namespace.paths),
        forge_menu=namespace.forge_menu,
    )


def _joined_preset(argv: Sequence[str]) -> list[str]:
    """`--preset NAME` as `--preset=NAME`, so a name that starts with "-" is still a name; a
    `--preset` with nothing after it is dropped (argparse would exit on either)."""
    args: list[str] = []
    rest = iter(argv)
    for arg in rest:
        if arg == "--preset":
            name = next(rest, None)
            if name is not None:
                args.append(f"--preset={name}")
        else:
            args.append(arg)
    return args


def main() -> int:
    options = parse_options(sys.argv[1:])
    paths = AppPaths.default()
    instance: InstanceLock | None = None
    only_setup = options.setup_qbittorrent or options.forge_menu is not None
    request = None if only_setup else options.forge_request
    if request is not None:
        # Before logging and the package check: Explorer starts one process per selected file.
        try:
            instance = hand_over(paths.root, request)
        except OSError:
            instance = None  # couldn't write it: carry on as a plain start (shows the window)
        else:
            if instance is None:
                return 0  # the running AirGrabber took it, or will (it waits in the folder)
    paths.ensure()
    configure_logging(paths)
    if options.setup_qbittorrent:
        # Not the app: no single-instance lock (AirGrabber may be running) and no window.
        return _setup_qbittorrent(paths)
    if options.forge_menu is not None:
        # Not the app either: no lock (AirGrabber may be running) and no window.
        return _forge_menu(paths, options.forge_menu)
    log.info("=== AirGrabber starting (data: %s) ===", paths.root)
    # Its own name and icon on notifications and its own taskbar group (app/identity.py), not
    # Python's: before any window (the package check's included) or the tray icon exists.
    set_process_app_id()
    if instance is None:
        instance = InstanceLock(data_dir=paths.root)
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
        return _run(paths, instance, start_hidden=options.tray, forge_request=request)
    finally:
        instance.release()


def _run(
    paths: AppPaths,
    instance: InstanceLock,
    *,
    start_hidden: bool,
    forge_request: ForgeRequest | None = None,
) -> int:
    """The app. `forge_request`: the Forge request this first start carries (it starts hidden;
    the request decides what shows); later starts' requests arrive through `instance`."""
    from app.services import Services
    from app.ui import tasks, theme
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
        # The interface language for this run, before the first window builds its texts
        # (switching it in Settings takes a restart).
        set_language(services.settings.current.language)
        # The accent colour's palette, likewise before the first widget takes its colours
        # (app/ui/theme.py; a change in Settings takes a restart too).
        theme.apply_accent(services.settings.current.accent)
        window = MainWindow(
            services,
            start_hidden=start_hidden,
            show_requests=instance.show_requested,
            forge_mailbox=instance,  # later starts' Forge requests
            forge_request=forge_request,
        )
        _fix_start_menu_shortcut_soon(loop)
        window.async_mainloop(event_loop=loop)  # returns once the window is closed for good (Quit)
        restart = window.restart_requested
        return 0
    except Exception:
        log.exception("Fatal error")
        _show_fatal_error(paths)
        return 1
    finally:
        if services is not None:
            # Forge first, before every task is cancelled: its renamer finishes the file it is
            # renaming (bounded). Quit has normally closed it already; then this does nothing.
            loop.run_until_complete(_close_forge(services))
        loop.run_until_complete(tasks.cancel_all())
        if services is not None:
            # Must run: aiosqlite's worker thread is not a daemon and would keep the process alive.
            # Forge closes through it too (forge.db's thread is no daemon either).
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


async def _close_forge(services: Services) -> None:
    """Close Forge within FORGE_CLOSE_S (a slow TMDB look by its renamer can't hold quitting
    up). Never raises."""
    from app.ui.forge_link import FORGE_CLOSE_S

    try:
        async with asyncio.timeout(FORGE_CLOSE_S):
            await services.forge.aclose()
    except TimeoutError:
        log.warning("MediaForge didn't close within %.0f s: quitting anyway", FORGE_CLOSE_S)
    except Exception:
        log.exception("Closing MediaForge failed")


def _setup_qbittorrent(paths: AppPaths) -> int:
    """`--setup-qbittorrent` (install.ps1): make Windows open magnet links and .torrent files with
    qBittorrent when no other torrent program is set (its installer doesn't), turn its "Add new
    torrent" pop-up off and give it AirGrabber's look (its theme copied into the data folder)
    unless qBittorrent runs; on a new install also its Web UI for AirGrabber. Never raises; the
    exit code says how the setup went (linking never changes it)."""
    log.info("=== AirGrabber: setting qBittorrent up ===")
    try:
        if not ensure_requirements(paths):
            return 1
        from app.services.qbittorrent_setup import run_setup
        from app.services.torrent_app import link_after_install

        link_after_install()  # never raises
        return run_setup(data_dir=paths.root)
    except Exception:
        log.exception("Setting qBittorrent up failed")
        return 1


def _forge_menu(paths: AppPaths, action: str) -> int:
    """`--forge-menu on|off` (install.ps1 on a new install, D8): turn this copy's Explorer
    right-click menu on or off (app/forge/context_menu.py). Never raises; the exit code is one of
    its EXIT_ codes."""
    log.info("=== AirGrabber: right-click menu %s ===", action or "?")
    try:
        if not ensure_requirements(paths):
            return 1
        from app.forge.context_menu import menu_action

        return asyncio.run(menu_action(paths, action))
    except Exception:
        log.exception("Changing the right-click menu failed")
        return 1


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
            t(
                "AirGrabber hit an unrecoverable error.\n\nDetails are in:\n{path}",
                path=paths.logs / "airgrabber.log",
            ),
        )
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
