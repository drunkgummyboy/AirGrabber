"""Launch-time package check: requirement parsing, version comparison, the installer, the overall flow."""

from __future__ import annotations

import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import bootstrap
from app.bootstrap import (
    REQUIREMENTS_FILE,
    Installer,
    Requirement,
    ensure_requirements,
    parse_requirements,
    unmet,
    version_tuple,
)
from app.config import AppPaths


class ParseRequirementsTests(unittest.TestCase):
    def test_names_and_clauses(self) -> None:
        text = "customtkinter>=5.2\nhttpx >= 0.27, <1.0\nPillow\n"
        self.assertEqual(
            parse_requirements(text),
            [
                Requirement("customtkinter", ((">=", "5.2"),)),
                Requirement("httpx", ((">=", "0.27"), ("<", "1.0"))),
                Requirement("Pillow"),
            ],
        )

    def test_skips_comments_blank_lines_and_pip_options(self) -> None:
        text = "# GUI\n\n-r other.txt\n--index-url https://example.invalid/simple\naiosqlite>=0.20  # db\n"
        self.assertEqual(parse_requirements(text), [Requirement("aiosqlite", ((">=", "0.20"),))])

    def test_drops_extras_markers_and_unreadable_clauses(self) -> None:
        text = 'pydantic[email]>=2.9 ; python_version >= "3.10"\nfoo==2.*\nbar @ https://example.invalid/bar.whl\n'
        self.assertEqual(
            parse_requirements(text),
            [Requirement("pydantic", ((">=", "2.9"),)), Requirement("foo"), Requirement("bar")],
        )

    def test_the_project_requirements_parse_with_a_version_each(self) -> None:
        requirements = parse_requirements(REQUIREMENTS_FILE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(requirements), 6)
        for requirement in requirements:
            self.assertTrue(requirement.clauses, requirement.name)


class VersionTests(unittest.TestCase):
    def test_release_numbers_without_trailing_zeros(self) -> None:
        self.assertEqual(version_tuple("10.0"), (10,))
        self.assertEqual(version_tuple("2.14.0b1"), (2, 14))
        self.assertEqual(version_tuple("0.10.4"), (0, 10, 4))
        self.assertEqual(version_tuple("0"), (0,))
        self.assertIsNone(version_tuple("unknown"))

    def test_unmet_reports_missing_and_outdated(self) -> None:
        requirements = [
            Requirement("present", ((">=", "2.9"),)),
            Requirement("old", ((">=", "2.9"),)),
            Requirement("absent", ((">=", "1.0"),)),
            Requirement("pinned", (("==", "1.0"),)),
            Requirement("odd", ((">=", "1.0"),)),
        ]
        installed = {"present": "2.10.1", "old": "1.10.17", "pinned": "1.0.0", "odd": "nightly"}
        problems = unmet(requirements, installed.get)
        self.assertEqual([str(problem) for problem in problems], ["old 1.10.17 (needs >=2.9)", "absent (not installed)"])

    def test_the_project_requirements_are_met_here(self) -> None:
        # The test suite itself imports every package, so the real check must agree they are installed.
        self.assertEqual(unmet(parse_requirements(REQUIREMENTS_FILE.read_text(encoding="utf-8"))), [])


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.log_file = Path(self._dir.name) / "logs" / "install.log"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def run_to_end(self, installer: Installer) -> list[str]:
        lines: list[str] = []
        deadline = time.monotonic() + 30
        while not installer.finished:
            self.assertLess(time.monotonic(), deadline, "installer never finished")
            lines += installer.poll()
            time.sleep(0.02)
        installer.close()
        return lines

    def test_runs_commands_in_order_and_collects_output(self) -> None:
        installer = Installer(
            [[sys.executable, "-c", "print('one')"], [sys.executable, "-c", "print('two')"]], self.log_file
        )
        lines = self.run_to_end(installer)
        self.assertEqual(installer.returncode, 0)
        self.assertEqual([line for line in lines if not line.startswith(">")], ["one", "two"])

    def test_stops_at_the_first_failure(self) -> None:
        installer = Installer(
            [
                [sys.executable, "-c", "import sys; print('broken'); sys.exit(3)"],
                [sys.executable, "-c", "print('never')"],
            ],
            self.log_file,
        )
        lines = self.run_to_end(installer)
        self.assertEqual(installer.returncode, 3)
        self.assertIn("broken", lines)
        self.assertNotIn("never", self.log_file.read_text(encoding="utf-8"))

    def test_a_command_that_cannot_start_fails(self) -> None:
        installer = Installer([[str(Path(self._dir.name) / "no-such-program.exe")]], self.log_file)
        lines = self.run_to_end(installer)
        self.assertEqual(installer.returncode, 1)
        self.assertTrue(any(line.startswith("Could not start it") for line in lines))

    def test_cancel_stops_a_running_command(self) -> None:
        installer = Installer([[sys.executable, "-c", "import time; time.sleep(30)"]], self.log_file)
        installer.poll()
        started = time.monotonic()
        installer.close()
        self.assertLess(time.monotonic() - started, 10)


class EnsureRequirementsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.paths = AppPaths(root).ensure()
        self.requirements_file = root / "requirements.txt"
        self.requirements_file.write_text("alpha>=1.0\nbeta>=2.0\n", encoding="utf-8")
        self.installed: dict[str, str] = {}
        logging.disable(logging.CRITICAL)  # the flow logs warnings by design
        self.addCleanup(logging.disable, logging.NOTSET)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(bootstrap, "installed_version", side_effect=lambda name: self.installed.get(name)).start()
        mock.patch.object(bootstrap, "refresh_import_paths").start()
        self.report = mock.patch.object(bootstrap, "_report_failure").start()
        self.install = mock.patch.object(bootstrap, "_install").start()

    def tearDown(self) -> None:
        self._dir.cleanup()

    def ensure(self) -> bool:
        return ensure_requirements(self.paths, self.requirements_file)

    def test_nothing_to_do_when_everything_is_installed(self) -> None:
        self.installed.update(alpha="1.2", beta="2.0.1")
        self.assertTrue(self.ensure())
        self.install.assert_not_called()

    def test_installs_from_the_requirements_file_when_something_is_missing(self) -> None:
        self.installed.update(alpha="1.2")

        def pip(commands: list[list[str]], log_file: Path, problems: object) -> int:
            self.assertEqual(commands[-1][-2:], ["-r", str(self.requirements_file)])
            self.assertEqual(log_file, self.paths.logs / "install.log")
            self.installed.update(beta="2.1")
            return 0

        self.install.side_effect = pip
        self.assertTrue(self.ensure())
        self.report.assert_not_called()

    def test_reports_packages_that_are_still_missing(self) -> None:
        self.install.return_value = 1
        self.assertFalse(self.ensure())
        problems = self.report.call_args.args[0]
        self.assertEqual([str(problem) for problem in problems], ["alpha (not installed)", "beta (not installed)"])

    def test_trusts_pip_when_only_the_version_reading_disagrees(self) -> None:
        self.installed.update(alpha="1.2", beta="1.9")
        self.install.return_value = 0  # pip ran fine, yet beta still reads as 1.9
        self.assertTrue(self.ensure())

    def test_cancelling_stops_the_launch_without_an_error_dialog(self) -> None:
        self.install.return_value = None
        self.assertFalse(self.ensure())
        self.report.assert_not_called()

    def test_unreadable_requirements_file_skips_the_check(self) -> None:
        self.requirements_file.unlink()
        self.assertTrue(self.ensure())
        self.install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
