"""Launch-time package check: requirement parsing, version comparison, the installer, the overall flow."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import bootstrap
from app.bootstrap import (
    OPTIONAL_RETRY_S,
    OPTIONAL_STATE_FILE,
    REQUIREMENTS_FILE,
    Installer,
    OptionalFailures,
    Requirement,
    ensure_requirements,
    install_commands,
    parse_requirements,
    pip_install,
    retry_optional,
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

    def test_requirements_below_the_optional_line_are_optional(self) -> None:
        text = (
            "# GUI\ncustomtkinter>=5.2\nhttpx>=0.27  # http\n\n# Optional: AirGrabber starts without these\n"
            'pywebview>=6.0 ; sys_platform == "win32"  # trailers\nextra[fast]>=1.0\n'
        )
        requirements = parse_requirements(text)
        self.assertEqual([(r.name, r.optional) for r in requirements], [
            ("customtkinter", False), ("httpx", False), ("pywebview", True), ("extra", True),
        ])  # fmt: skip
        # pip gets each line as written, without its comment
        self.assertEqual(
            [r.spec for r in requirements],
            ["customtkinter>=5.2", "httpx>=0.27", 'pywebview>=6.0 ; sys_platform == "win32"', "extra[fast]>=1.0"],
        )
        self.assertEqual(Requirement("plain", ((">=", "1.0"),)).spec, "plain>=1.0")

    def test_in_the_project_only_pywebview_and_guessit_are_optional(self) -> None:
        requirements = parse_requirements(REQUIREMENTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual([r.name for r in requirements if r.optional], ["pywebview", "guessit"])
        self.assertIn("customtkinter", [r.name for r in requirements if not r.optional])


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

    def test_optional_commands_may_fail(self) -> None:
        installer = Installer(
            [[sys.executable, "-c", "print('required')"]],
            self.log_file,
            optional=[
                [sys.executable, "-c", "import sys; print('optional one'); sys.exit(1)"],
                [str(Path(self._dir.name) / "no-such-program.exe")],
                [sys.executable, "-c", "print('optional three')"],
            ],
        )
        lines = self.run_to_end(installer)
        self.assertEqual(installer.returncode, 0)  # the required one decides
        self.assertEqual(installer.optional_failures, 2)
        self.assertIn("optional one", lines)
        self.assertIn("optional three", lines)  # a failed optional one doesn't stop the rest

    def test_no_optional_command_after_a_failed_required_one(self) -> None:
        installer = Installer(
            [[sys.executable, "-c", "import sys; sys.exit(2)"]],
            self.log_file,
            optional=[[sys.executable, "-c", "print('optional')"]],
        )
        self.run_to_end(installer)
        self.assertEqual(installer.returncode, 2)
        self.assertNotIn("print('optional')", self.log_file.read_text(encoding="utf-8"))


class InstallCommandsTests(unittest.TestCase):
    required = [Requirement("alpha", ((">=", "1.0"),), line="alpha>=1.0"), Requirement("beta", line="beta")]
    optional = [Requirement("gamma", ((">=", "2.0"),), True, "gamma>=2.0"), Requirement("delta", (), True, "delta")]

    def test_required_in_one_go_optional_one_each(self) -> None:
        with mock.patch("importlib.util.find_spec", return_value=object()):
            commands, optional = install_commands(self.required, self.optional)
        self.assertEqual(commands, [pip_install("alpha>=1.0", "beta")])
        self.assertEqual(optional, [pip_install("gamma>=2.0"), pip_install("delta")])
        self.assertEqual(pip_install("x")[:4], [sys.executable, "-m", "pip", "install"])

    def test_ensurepip_first_and_only_tolerated_without_required_ones(self) -> None:
        ensure = [sys.executable, "-m", "ensurepip", "--upgrade"]
        with mock.patch("importlib.util.find_spec", return_value=None):
            commands, optional = install_commands(self.required, self.optional)
            self.assertEqual(commands[0], ensure)
            self.assertNotIn(ensure, optional)
            commands, optional = install_commands([], self.optional)
        self.assertEqual(commands, [])
        self.assertEqual(optional[0], ensure)


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

        def pip(commands: list[list[str]], log_file: Path, problems: object, *, optional: object) -> int:
            self.assertEqual(commands[-1][-2:], ["alpha>=1.0", "beta>=2.0"])  # every required one
            self.assertEqual(optional, [])
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


class OptionalRequirementsTests(unittest.TestCase):
    """Packages below "# optional": tried after the required ones, never blocking the launch, and
    after a failure not tried again for OPTIONAL_RETRY_S (unless their line changes)."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.paths = AppPaths(root).ensure()
        self.requirements_file = root / "requirements.txt"
        self.write_requirements("viewer>=6.0")
        self.installed: dict[str, str] = {"alpha": "1.2"}
        self.now = 1_000_000.0
        self.calls: list[tuple[list[list[str]], list[list[str]]]] = []
        self.result: dict[str, object] = {"returncode": 0, "installs": {}}
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(bootstrap, "installed_version", side_effect=lambda name: self.installed.get(name)).start()
        mock.patch.object(bootstrap, "refresh_import_paths").start()
        mock.patch("importlib.util.find_spec", return_value=object()).start()  # pip is there
        self.report = mock.patch.object(bootstrap, "_report_failure").start()
        mock.patch.object(bootstrap, "_install", side_effect=self.pip).start()

    def write_requirements(self, optional_line: str) -> None:
        self.requirements_file.write_text(
            f"alpha>=1.0\n# optional: AirGrabber starts without these\n{optional_line}\n", encoding="utf-8"
        )

    def pip(
        self, commands: list[list[str]], log_file: Path, problems: object, *, optional: list[list[str]]
    ) -> int | None:
        self.calls.append((commands, optional))
        installs = self.result["installs"]
        assert isinstance(installs, dict)
        self.installed.update(installs)
        returncode = self.result["returncode"]
        assert returncode is None or isinstance(returncode, int)
        return returncode

    def ensure(self) -> bool:
        return ensure_requirements(self.paths, self.requirements_file, clock=lambda: self.now)

    def test_a_failed_optional_package_never_stops_the_launch(self) -> None:
        self.assertTrue(self.ensure())  # pip ran, the package is still missing
        self.assertEqual(self.calls, [([], [pip_install("viewer>=6.0")])])
        self.report.assert_not_called()
        state = json.loads((self.paths.root / OPTIONAL_STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state, {"viewer>=6.0": self.now})

    def test_not_tried_again_until_the_retry_time(self) -> None:
        self.ensure()
        self.now += OPTIONAL_RETRY_S - 60
        self.assertTrue(self.ensure())
        self.assertEqual(len(self.calls), 1)  # no pip, no window: straight on
        self.now += 120
        self.assertTrue(self.ensure())
        self.assertEqual(len(self.calls), 2)

    def test_a_changed_line_is_tried_at_once(self) -> None:
        self.ensure()
        self.write_requirements("viewer>=7.0")
        self.ensure()
        self.assertEqual(self.calls[-1], ([], [pip_install("viewer>=7.0")]))

    def test_installed_at_last_clears_the_failure(self) -> None:
        self.ensure()
        self.now += OPTIONAL_RETRY_S
        self.result["installs"] = {"viewer": "6.1"}
        self.assertTrue(self.ensure())
        self.assertEqual(json.loads((self.paths.root / OPTIONAL_STATE_FILE).read_text(encoding="utf-8")), {})
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.ensure())  # nothing missing any more
        self.assertEqual(len(self.calls), 2)

    def test_required_and_optional_together(self) -> None:
        self.installed.clear()
        self.result["installs"] = {"alpha": "1.5"}  # the optional one still fails
        self.assertTrue(self.ensure())
        self.assertEqual(self.calls, [([pip_install("alpha>=1.0")], [pip_install("viewer>=6.0")])])
        self.report.assert_not_called()

    def test_a_failed_required_package_still_stops_it(self) -> None:
        self.installed.clear()
        self.result["returncode"] = 1
        self.assertFalse(self.ensure())
        problems = self.report.call_args.args[0]
        self.assertEqual([str(problem) for problem in problems], ["alpha (not installed)"])  # not the optional one

    def test_cancelling_an_optional_install_starts_without_it(self) -> None:
        self.result["returncode"] = None
        self.assertTrue(self.ensure())
        self.assertIn("viewer>=6.0", json.loads((self.paths.root / OPTIONAL_STATE_FILE).read_text(encoding="utf-8")))
        self.installed.clear()
        self.assertFalse(self.ensure())  # a required one missing: cancelling stops the launch as before

    def test_a_broken_state_file_is_ignored(self) -> None:
        (self.paths.root / OPTIONAL_STATE_FILE).write_text("{not json", encoding="utf-8")
        self.assertTrue(self.ensure())
        self.assertEqual(len(self.calls), 1)
        failures = OptionalFailures(self.paths.root / OPTIONAL_STATE_FILE)
        self.assertFalse(failures.due(Requirement("viewer", line="viewer>=6.0"), self.now + 1))
        self.assertTrue(failures.due(Requirement("viewer", line="viewer>=6.0"), self.now - 10))  # the clock went back

    def test_retry_optional_tries_it_again_at_the_next_start(self) -> None:
        self.ensure()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.ensure())
        self.assertEqual(len(self.calls), 1)  # remembered: not tried again yet
        state_file = self.paths.root / OPTIONAL_STATE_FILE
        self.assertTrue(retry_optional("viewer", state_file))
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8")), {})
        self.assertTrue(self.ensure())
        self.assertEqual(self.calls[-1], ([], [pip_install("viewer>=6.0")]))  # tried again at once

    def test_retry_optional_matches_the_name_only(self) -> None:
        state_file = self.paths.root / OPTIONAL_STATE_FILE
        state_file.write_text(
            json.dumps({"Viewer_Pkg>=6.0": self.now, "viewer-pkg-extra>=1.0": self.now, "other>=2.0": self.now}),
            encoding="utf-8",
        )
        self.assertTrue(retry_optional("viewer.pkg", state_file))  # pip's name rules: case, - _ . alike
        self.assertEqual(
            sorted(json.loads(state_file.read_text(encoding="utf-8"))), ["other>=2.0", "viewer-pkg-extra>=1.0"]
        )
        self.assertFalse(retry_optional("viewer-pkg", state_file))  # nothing left to forget

    def test_retry_optional_without_a_state_file(self) -> None:
        missing = self.paths.root / "nowhere" / OPTIONAL_STATE_FILE
        self.assertFalse(retry_optional("viewer", missing))
        self.assertFalse(missing.exists())

    def test_retry_optional_uses_the_data_folder_by_default(self) -> None:
        (self.paths.root / OPTIONAL_STATE_FILE).write_text(json.dumps({"viewer>=6.0": self.now}), encoding="utf-8")
        with mock.patch.dict("os.environ", {"AIRGRABBER_DATA_DIR": str(self.paths.root)}):
            self.assertTrue(retry_optional("viewer"))


if __name__ == "__main__":
    unittest.main()
