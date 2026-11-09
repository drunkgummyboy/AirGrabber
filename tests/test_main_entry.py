"""main.py's command line: `--tray` starts with only the tray icon showing; `--setup-qbittorrent`
only sets qBittorrent up; `--forge [--confirm] [--preset NAME] PATH…` is a Forge request, handed to a
running AirGrabber before logging and the package check (app/single_instance.py's mailbox)."""

from __future__ import annotations

import asyncio
import functools
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import main
from app.config import DATA_DIR_ENV, AppPaths
from app.single_instance import ForgeRequest, InstanceLock, hand_over, take_requests
from main import Options, parse_options

FILE = "C:\\Media\\Alex.S01E01.mkv"


class ParseOptionsTests(unittest.TestCase):
    def test_options(self) -> None:
        cases: dict[tuple[str, ...], Options] = {
            (): Options(tray=False),
            ("--tray",): Options(tray=True),
            ("--tray", "--something-newer"): Options(tray=True),  # unknown ones are ignored
            ("stray",): Options(paths=("stray",)),  # kept, but no Forge request without --forge
            # install.ps1, right after installing qBittorrent (tests/test_qbittorrent_setup.py).
            ("--setup-qbittorrent",): Options(setup_qbittorrent=True),
            # The right-click menu (one process per selected file).
            ("--forge", "--confirm", "--preset", "Plex", FILE): Options(
                forge=True, confirm=True, preset="Plex", paths=(FILE,)
            ),
            # The "MediaForge" shortcut: MediaForge's page.
            ("--forge",): Options(forge=True),
            ("--forge", "a", "--tray", "b", "--something-newer"): Options(
                tray=True, forge=True, paths=("a", "b")
            ),
            ("--forge", "--preset", "-Kodi-", FILE): Options(forge=True, preset="-Kodi-", paths=(FILE,)),
            ("--forge", "--preset=Plex Movies"): Options(forge=True, preset="Plex Movies"),
            ("--forge", "--preset"): Options(forge=True),  # no name: none
            ("--forge", "--preset", ""): Options(forge=True),
            # install.ps1 on a new install (D8), and its opposite.
            ("--forge-menu", "on"): Options(forge_menu="on"),
            ("--forge-menu=off",): Options(forge_menu="off"),
            ("--forge-menu",): Options(forge_menu=""),  # no word: refused by the action itself
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(parse_options(argv), expected)

    def test_the_forge_request(self) -> None:
        self.assertIsNone(parse_options(()).forge_request)
        self.assertIsNone(parse_options(("--confirm", FILE)).forge_request)  # only with --forge
        self.assertEqual(
            parse_options(("--forge", "--confirm", "--preset", "Plex", FILE)).forge_request,
            ForgeRequest(paths=(os.path.abspath(FILE),), preset="Plex", confirm=True),
        )
        self.assertEqual(parse_options(("--forge",)).forge_request, ForgeRequest())

    def test_paths_are_made_absolute(self) -> None:
        # The running copy that takes the request has another working folder.
        request = parse_options(("--forge", "Alex.mkv")).forge_request
        assert request is not None
        self.assertEqual(request.paths, (os.path.join(os.getcwd(), "Alex.mkv"),))


class _FakeLock:
    def __init__(self, *, acquired: bool, answers: bool = True) -> None:
        self.acquired = acquired
        self.answers = answers
        self.shown = False
        self.released = False

    def acquire(self) -> bool:
        return self.acquired

    def signal_show(self) -> bool:
        self.shown = True
        return True

    def ask_to_show(self, answer_s: float = 5.0) -> bool:
        self.shown = True
        return self.answers

    def show_requested(self) -> bool:
        return False

    def release(self) -> None:
        self.released = True


class MainTests(unittest.TestCase):
    """main() up to the package check, against a temporary data folder; nothing real starts."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.patch(mock.patch.dict(os.environ, {DATA_DIR_ENV: str(self.data_dir)}))
        self.logging = self.patch(mock.patch.object(main, "configure_logging"))
        self.app_id = self.patch(mock.patch.object(main, "set_process_app_id"))
        self.requirements = self.patch(mock.patch.object(main, "ensure_requirements", return_value=False))
        self.run_app = self.patch(mock.patch.object(main, "_run", side_effect=AssertionError("no app")))

    def patch(self, patcher: Any) -> Any:
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def main(self, *argv: str) -> int:
        with mock.patch.object(sys, "argv", ["main.py", *argv]):
            return main.main()

    def test_a_start_that_went_back_starts_afresh_with_the_same_arguments(self) -> None:
        lock = mock.MagicMock()
        lock.acquire.return_value = True
        with (
            mock.patch.object(main, "InstanceLock", return_value=lock),
            mock.patch.object(main, "before_start", return_value=True) as guard,
            mock.patch.object(main, "_relaunch") as relaunch,
        ):
            self.assertEqual(self.main("--tray"), 0)
        guard.assert_called_once_with(
            Path(main.__file__).resolve().parent, AppPaths(self.data_dir).databases
        )
        relaunch.assert_called_once_with(["--tray"])
        lock.release.assert_called()
        self.requirements.assert_not_called()

    def test_the_guard_runs_before_the_package_check(self) -> None:
        lock = mock.MagicMock()
        lock.acquire.return_value = True
        order = mock.MagicMock()
        order.ensure_requirements.return_value = False
        order.before_start.return_value = False
        with (
            mock.patch.object(main, "InstanceLock", return_value=lock),
            mock.patch.object(main, "before_start", order.before_start),
            mock.patch.object(main, "ensure_requirements", order.ensure_requirements),
        ):
            self.main()
        self.assertEqual(
            [call[0] for call in order.mock_calls], ["before_start", "ensure_requirements"]
        )

    def test_a_handed_over_request_never_logs_or_checks_packages(self) -> None:
        with (
            mock.patch.object(main, "hand_over", return_value=None) as handed,
            mock.patch.object(main, "InstanceLock", side_effect=AssertionError("no second lock")),
        ):
            self.assertEqual(self.main("--forge", "--confirm", "--preset", "Plex", FILE), 0)
        self.assertEqual(handed.call_args.args, (self.data_dir, ForgeRequest((FILE,), "Plex", True)))
        self.logging.assert_not_called()
        self.requirements.assert_not_called()
        self.app_id.assert_not_called()
        self.assertFalse((self.data_dir / "logs").exists())  # not even the folders

    @unittest.skipUnless(sys.platform == "win32", "named kernel objects are Windows-only")
    def test_a_real_hand_over_to_a_copy_that_is_busy(self) -> None:
        names = f"Local\\AirGrabberTest-{uuid.uuid4().hex}"
        names_args = (names, f"{names}-show", f"{names}-forge")
        running = InstanceLock(*names_args, data_dir=self.data_dir)
        self.addCleanup(running.release)
        self.assertTrue(running.acquire())
        sender = functools.partial(
            hand_over,
            lock_factory=lambda: InstanceLock(*names_args, data_dir=self.data_dir),
            wait_s=0.2,
        )
        with mock.patch.object(main, "hand_over", sender):
            self.assertEqual(self.main("--forge", "--confirm", FILE), 0)
        self.logging.assert_not_called()
        self.assertTrue(running.forge_requested())
        self.assertEqual(running.take_requests(), [ForgeRequest((FILE,), None, True)])

    def test_a_start_that_became_airgrabber_goes_on_with_its_lock(self) -> None:
        lock = _FakeLock(acquired=True)
        with (
            mock.patch.object(main, "hand_over", return_value=lock),
            mock.patch.object(main, "InstanceLock", side_effect=AssertionError("its own lock")),
        ):
            self.assertEqual(self.main("--forge", FILE), 1)  # the (faked) package check fails
        self.logging.assert_called_once()
        self.app_id.assert_called_once()
        self.requirements.assert_called_once()
        self.assertTrue(lock.released)

    def test_a_first_start_keeps_the_request(self) -> None:
        lock = _FakeLock(acquired=True)
        self.requirements.return_value = True
        self.run_app.side_effect = None
        self.run_app.return_value = 0
        with mock.patch.object(main, "hand_over", return_value=lock):
            self.assertEqual(self.main("--forge", "--confirm", FILE), 0)
        self.assertIs(self.run_app.call_args.args[1], lock)
        # The app gets it: it starts hidden and shows only the confirm window (F-21).
        self.assertEqual(
            self.run_app.call_args.kwargs,
            {"start_hidden": False, "forge_request": ForgeRequest((FILE,), None, True)},
        )

    def test_a_plain_start_carries_no_request(self) -> None:
        lock = _FakeLock(acquired=True)
        self.requirements.return_value = True
        self.run_app.side_effect = None
        self.run_app.return_value = 0
        with (
            mock.patch.object(main, "hand_over", side_effect=AssertionError("no hand-over")),
            mock.patch.object(main, "InstanceLock", return_value=lock),
        ):
            self.assertEqual(self.main("--tray", "--confirm", FILE), 0)  # no --forge
        self.assertEqual(self.run_app.call_args.kwargs, {"start_hidden": True, "forge_request": None})

    def test_a_plain_second_start_still_shows_the_window(self) -> None:
        lock = _FakeLock(acquired=False)
        with (
            mock.patch.object(main, "hand_over", side_effect=AssertionError("no hand-over")),
            mock.patch.object(main, "InstanceLock", return_value=lock) as make_lock,
        ):
            self.assertEqual(self.main(), 0)
        self.assertEqual(make_lock.call_args.kwargs, {"data_dir": self.data_dir})
        self.assertTrue(lock.shown)
        self.assertTrue(lock.released)
        self.requirements.assert_not_called()

    def test_a_start_while_the_running_copy_closes_starts_after_it(self) -> None:
        # The running copy doesn't answer (it is closing); once it lets go this start runs.
        locks = iter([_FakeLock(acquired=False, answers=False), _FakeLock(acquired=True)])
        with (
            mock.patch.object(main, "hand_over", side_effect=AssertionError("no hand-over")),
            mock.patch.object(main, "InstanceLock", side_effect=lambda **_kw: next(locks)),
        ):  # one real 0.25 s poll
            self.assertEqual(self.main(), 1)  # the package check (patched to fail) was reached
        self.requirements.assert_called_once()

    def test_a_request_that_cannot_be_written_shows_the_window(self) -> None:
        lock = _FakeLock(acquired=False)
        with (
            mock.patch.object(main, "hand_over", side_effect=PermissionError),
            mock.patch.object(main, "InstanceLock", return_value=lock),
        ):
            self.assertEqual(self.main("--forge", FILE), 0)
        self.assertTrue(lock.shown)

    def test_setting_qbittorrent_up_hands_nothing_over(self) -> None:
        with (
            mock.patch.object(main, "hand_over", side_effect=AssertionError("no hand-over")),
            mock.patch.object(main, "_setup_qbittorrent", return_value=0) as setup,
        ):
            self.assertEqual(self.main("--setup-qbittorrent", "--forge", FILE), 0)
        setup.assert_called_once()
        self.assertEqual(take_requests(self.data_dir), [])

    def test_the_menu_action_needs_no_lock_and_hands_nothing_over(self) -> None:
        with (
            mock.patch.object(main, "hand_over", side_effect=AssertionError("no hand-over")),
            mock.patch.object(main, "InstanceLock", side_effect=AssertionError("no lock")),
            mock.patch.object(main, "_forge_menu", return_value=0) as menu,
        ):
            self.assertEqual(self.main("--forge-menu", "on", "--forge", FILE), 0)
        self.assertEqual(menu.call_args.args[1], "on")
        self.logging.assert_called_once()
        self.run_app.assert_not_called()
        self.assertEqual(take_requests(self.data_dir), [])

    def test_the_menu_action_runs_after_the_package_check(self) -> None:
        action = mock.AsyncMock(return_value=0)
        with mock.patch("app.forge.context_menu.menu_action", action):
            self.assertEqual(self.main("--forge-menu", "off"), 1)  # the (faked) check fails
            action.assert_not_called()
            self.requirements.return_value = True
            self.assertEqual(self.main("--forge-menu", "off"), 0)
        self.assertEqual(action.call_args.args[1:], ("off",))
        action.side_effect = OSError("broken")
        with mock.patch("app.forge.context_menu.menu_action", action), self.assertLogs("airgrabber", "ERROR"):
            self.assertEqual(self.main("--forge-menu", "on"), 1)


class CloseForgeTests(unittest.IsolatedAsyncioTestCase):
    """main.py's shutdown closes Forge first, bounded: a slow renamer can't hold quitting up."""

    def services(self, aclose: Any) -> Any:
        return SimpleNamespace(forge=SimpleNamespace(aclose=aclose))

    async def test_closes_forge(self) -> None:
        aclose = mock.AsyncMock()
        await main._close_forge(self.services(aclose))
        aclose.assert_awaited_once()

    async def test_a_slow_forge_is_given_up_on(self) -> None:
        async def slow() -> None:
            await asyncio.sleep(10)

        with (
            mock.patch("app.ui.forge_link.FORGE_CLOSE_S", 0.05),
            self.assertLogs("airgrabber", "WARNING"),
        ):
            await asyncio.wait_for(main._close_forge(self.services(slow)), 2.0)

    async def test_a_failing_close_never_raises(self) -> None:
        with self.assertLogs("airgrabber", "ERROR"):
            await main._close_forge(self.services(mock.AsyncMock(side_effect=OSError("disk"))))


if __name__ == "__main__":
    unittest.main()
