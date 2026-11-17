"""MediaForge's run progress (app/forge/core/run_progress.py): what the page's dock shows, kept
outside the page so the main window can show the same dock on every page, also for a run that
started before the page was built (an automatic run, a right-click rename)."""

from __future__ import annotations

import unittest

from app.forge.core.executor import ProgressEvent
from app.forge.core.run_progress import AUTO, PAGE, RunProgress, RunState, RunSteps
from app.i18n import Language, current_language, set_language


def working(item_id: int | None, message: str = "Renaming", fraction: float = 0.0) -> ProgressEvent:
    return ProgressEvent(item_id=item_id, status="working", message=message, fraction=fraction)


def done(item_id: int) -> ProgressEvent:
    return ProgressEvent(item_id=item_id, status="done", message="", fraction=0.0)


class RunProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.progress = RunProgress()
        self.heard: list[RunState] = []
        self.unsubscribe = self.progress.subscribe(self.heard.append)

    def test_idle_at_first(self) -> None:
        self.assertFalse(self.progress.state.running)
        self.assertFalse(self.progress.busy(PAGE))

    def test_a_report_then_idle(self) -> None:
        cancel = lambda: None  # noqa: E731
        self.progress.report("Renaming 1 of 2 · Dune", 0.5, source=PAGE, cancel=cancel)
        state = self.heard[-1]
        self.assertEqual(
            (state.running, state.text, state.fraction, state.cancel), (True, "Renaming 1 of 2 · Dune", 0.5, cancel)
        )
        self.assertTrue(self.progress.busy(PAGE))
        self.progress.idle(PAGE)
        self.assertFalse(self.heard[-1].running)
        self.assertFalse(self.progress.state.running)

    def test_the_same_report_twice_is_heard_once(self) -> None:
        self.progress.report("Renaming 1 of 2", 0.0, source=AUTO)
        self.progress.report("Renaming 1 of 2", 0.0, source=AUTO)
        self.assertEqual(len(self.heard), 1)

    def test_two_runs_the_newest_report_wins_and_idle_only_when_neither_runs(self) -> None:
        self.progress.report("Waiting for Auto-rename to finish…", 0.0, source=PAGE)
        self.progress.report("Renaming 1 of 3", 0.0, source=AUTO)
        self.assertEqual(self.progress.state.text, "Renaming 1 of 3")
        self.progress.report("Renaming 2 of 3", 0.33, source=AUTO)
        self.progress.idle(AUTO)
        state = self.progress.state
        self.assertTrue(state.running)  # the page's run still waits
        self.assertEqual((state.text, state.source), ("Waiting for Auto-rename to finish…", PAGE))
        self.progress.idle(PAGE)
        self.assertFalse(self.progress.state.running)

    def test_idle_for_a_run_that_never_reported_changes_nothing(self) -> None:
        self.progress.idle(AUTO)
        self.assertEqual(self.heard, [])

    def test_unsubscribed_and_failing_listeners(self) -> None:
        def broken(_state: RunState) -> None:
            raise RuntimeError("listener")

        self.progress.subscribe(broken)
        self.unsubscribe()
        with self.assertLogs("app.forge.core.run_progress", "ERROR"):
            self.progress.report("Renaming 1 of 1", 0.0, source=PAGE)
        self.assertEqual(self.heard, [])
        self.assertTrue(self.progress.state.running)  # the failure didn't stop the report

    def test_follow_reports_each_step_and_idles(self) -> None:
        names = {1: "Dune", 2: "Heat"}
        heard = self.progress.follow(AUTO, 2, names.get)
        self.assertEqual(self.progress.state.text, "Renaming 1 of 2")
        heard(working(1))
        self.assertEqual((self.progress.state.text, self.progress.state.fraction), ("Renaming 1 of 2 · Dune", 0.0))
        heard(done(1))
        heard(working(2))
        self.assertEqual((self.progress.state.text, self.progress.state.fraction), ("Renaming 2 of 2 · Heat", 0.5))
        self.assertIsNone(self.progress.state.cancel)
        self.progress.idle(AUTO)
        self.assertFalse(self.progress.state.running)


class RunStepsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(set_language, current_language())
        set_language(Language.EN)

    def test_renames_copies_and_steps(self) -> None:
        steps = RunSteps(4)
        self.assertEqual(steps.now(), ("Renaming 1 of 4", 0.0))
        steps.heard(working(7), "Dune")
        self.assertEqual(steps.now(), ("Renaming 1 of 4 · Dune", 0.0))
        steps.heard(
            ProgressEvent(item_id=7, status="working", message="Copying 1 of 2 GB", fraction=0.1, copying=True),
            "Dune",
        )
        self.assertEqual(steps.now(), ("Copying 1 of 2 GB · Dune", 0.1))
        steps.heard(done(7), "Dune")  # the copy is over
        self.assertEqual(steps.now(), ("Renaming 2 of 4 · Dune", 0.25))
        steps.heard(working(None, "Downloading trailer 1 of 1 · Dune", 0.75), None)
        self.assertEqual(steps.now(), ("Downloading trailer 1 of 1 · Dune", 0.75))

    def test_a_files_own_step_after_a_trailer_step_shows_the_rename_again(self) -> None:
        # An automatic run of several plans: the first plan's trailer, then the next one's files.
        steps = RunSteps(2)
        steps.heard(working(1), "Dune")
        steps.heard(done(1), "Dune")
        steps.heard(working(None, "Downloading trailer 1 of 1 · Dune", 0.5), None)
        self.assertEqual(steps.now(), ("Downloading trailer 1 of 1 · Dune", 0.5))
        steps.heard(working(2), "Heat")
        self.assertEqual(steps.now(), ("Renaming 2 of 2 · Heat", 0.5))

    def test_dutch(self) -> None:
        set_language(Language.NL)
        self.assertEqual(RunSteps(2).now()[0], "Hernoemen 1 van 2")


if __name__ == "__main__":
    unittest.main()
