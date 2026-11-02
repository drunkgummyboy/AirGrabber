"""The old MediaForge's import step (app/forge/ui/import_step.py, F-26): the question, the
"replace what's here?" question, the result (with "Remove the old MediaForge" once), the
problems, Settings' "Bring over…" row, and the host showing it at an open of the Forge window
(D13: again at the next open after "Not now", never after an import).

The import itself is faked here (tests/forge/test_migration.py covers it); no old install is
real: tests/forge/base.py hides the owner's."""

from __future__ import annotations

import asyncio
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app.forge.migration import ImportProblem, ImportResult, ImportTarget, OldInstall, Problem
from app.forge.models.settings import ForgeSettings
from app.forge.ui.host import ForgeHost
from app.forge.ui.import_step import (
    ASK_HEADING,
    BOX_BUTTON,
    FAILED_TEXT,
    PROBLEM_HEADING,
    REPLACE_HEADING,
    RESULT_HEADING,
    BringOverBox,
    ImportStep,
    result_lines,
)
from app.i18n import Language, set_language, t
from tests.forge.tk import pump, pump_until
from tests.forge.ui.test_host import HostCase
from tests.forge.ui.test_window import TkCase

OLD = OldInstall(
    db_file=Path(r"C:\Users\Alex\AppData\Roaming\MediaForge\mediaforge.db"),
    install_location=r"C:\Users\Alex\AppData\Local\Programs\MediaForge",
    uninstall_string="powershell.exe -File uninstall.ps1",
    version="1.3.0",
)
RESULT = ImportResult(
    imported_from="MediaForge 1.3.0, 2026-11-02",
    presets=4,
    folders=3,
    renames=1,
    matches=12,
    tmdb_key=True,
    menu=False,
)


def labels(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found += labels(child)
    return found


class StepCase(TkCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.done: list[ImportResult | None] = []
        self.calls = 0
        self.outcomes: list[ImportResult | BaseException] = [RESULT]
        self.own = False
        self.removed = 0

    async def fake_bring_over(self, forge: Any, old: OldInstall) -> ImportResult:
        self.calls += 1
        await asyncio.sleep(0)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_has_own_data(self, forge: Any) -> bool:
        await asyncio.sleep(0)
        return self.own

    def remove(self) -> None:
        self.removed += 1

    def step(self, old: OldInstall = OLD, *, remover: bool = True) -> ImportStep:
        dialog = ImportStep(
            self.root,
            cast(ImportTarget, object()),
            old,
            on_remove_old=self.remove if remover else None,
            on_done=self.done.append,
            bring_over=self.fake_bring_over,
            has_own_data=self.fake_has_own_data,
        )
        dialog.attributes("-alpha", 0.0)
        self.addCleanup(self.gone, dialog)
        return dialog

    @staticmethod
    def gone(dialog: ImportStep) -> None:
        if dialog.winfo_exists():
            dialog.destroy()

    async def heading(self, dialog: ImportStep, text: str) -> None:
        await pump_until(dialog, lambda: dialog.heading.cget("text") == t(text))


class QuestionTests(StepCase):
    async def test_the_question(self) -> None:
        dialog = self.step()
        self.assertEqual(dialog.heading.cget("text"), t(ASK_HEADING))
        self.assertEqual(dialog.button_texts, ["Bring over", "Not now"])
        self.assertIsNotNone(dialog.borderless)

    async def test_not_now_closes_and_reports_nothing(self) -> None:
        dialog = self.step()
        dialog.close()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.done, [None])
        self.assertEqual(self.calls, 0)

    async def test_escape_is_not_now(self) -> None:
        dialog = self.step()
        await pump(self.root, 0.3)  # shown and focused
        dialog.focus_force()
        dialog.event_generate("<Escape>")
        await pump(self.root, 0.05)
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.done, [None])

    async def test_dutch(self) -> None:
        set_language(Language.NL)
        dialog = self.step()
        self.assertEqual(
            dialog.heading.cget("text"),
            "Je MediaForge-instellingen, voorinstellingen en geschiedenis overzetten?",
        )
        self.assertEqual(dialog.button_texts, ["Overzetten", "Nu niet"])


class ResultTests(StepCase):
    async def test_bring_over_reports_what_came(self) -> None:
        dialog = self.step()
        dialog.bring_over()
        await self.heading(dialog, RESULT_HEADING)
        shown = labels(dialog.content)
        for line in ("•  4 presets", "•  3 folders", "•  1 past rename", "•  12 remembered matches",
                     "•  Your TMDB key"):
            self.assertIn(line, shown)
        self.assertEqual(dialog.button_texts, ["Done"])
        self.assertIsNotNone(dialog.remove_button)
        dialog.close()
        self.assertEqual(self.done, [RESULT])

    async def test_a_renamer_waiting_for_the_old_install_is_said(self) -> None:
        for waits in (True, False):
            with self.subTest(waits=waits):
                self.outcomes = [
                    ImportResult("x", 1, 1, 1, 1, tmdb_key=False, menu=False,
                                 waits_for_removal=waits)
                ]
                dialog = self.step()
                dialog.bring_over()
                await self.heading(dialog, RESULT_HEADING)
                shown = "Automatic renaming starts once the old MediaForge is removed."
                self.assertEqual(shown in labels(dialog.content), waits)
                dialog.close()

    async def test_result_lines_name_the_key_and_menu_only_when_they_came(self) -> None:
        plain = ImportResult("x", presets=1, folders=0, renames=0, matches=0, tmdb_key=False,
                             menu=False)
        self.assertEqual(
            result_lines(plain),
            ["1 preset", "0 folders", "0 past renames", "0 remembered matches"],
        )
        both = ImportResult("x", 2, 1, 2, 1, tmdb_key=True, menu=True)
        self.assertEqual(
            result_lines(both)[-2:],
            ["Your TMDB key", "The right-click menu now opens MediaForge in AirGrabber"],
        )

    async def test_remove_the_old_mediaforge_once(self) -> None:
        dialog = self.step()
        dialog.bring_over()
        await self.heading(dialog, RESULT_HEADING)
        assert dialog.remove_button is not None
        dialog.remove_button.invoke()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.removed, 1)
        self.assertEqual(self.done, [RESULT])
        dialog.remove_old()  # gone: never twice
        self.assertEqual(self.removed, 1)

    async def test_no_remove_without_a_registered_install_or_a_remover(self) -> None:
        for old, remover in ((OldInstall(db_file=OLD.db_file), True), (OLD, False)):
            with self.subTest(registered=old.registered, remover=remover):
                dialog = self.step(old, remover=remover)
                dialog.bring_over()
                await self.heading(dialog, RESULT_HEADING)
                self.assertIsNone(dialog.remove_button)
                dialog.close()

    async def test_own_data_asks_before_replacing(self) -> None:
        self.own = True
        dialog = self.step()
        dialog.bring_over()
        await self.heading(dialog, REPLACE_HEADING)
        self.assertEqual(self.calls, 0)
        self.assertEqual(dialog.button_texts, ["Replace", "Not now"])
        dialog.replace()
        await self.heading(dialog, RESULT_HEADING)
        self.assertEqual(self.calls, 1)

    async def test_own_data_not_now_replaces_nothing(self) -> None:
        self.own = True
        dialog = self.step()
        dialog.bring_over()
        await self.heading(dialog, REPLACE_HEADING)
        dialog.close()
        self.assertEqual((self.calls, self.done), (0, [None]))

    async def test_busy_ignores_closing(self) -> None:
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def slow(forge: Any, old: OldInstall) -> ImportResult:
            await gate
            return RESULT

        dialog = self.step()
        dialog._bring_over = slow
        dialog.bring_over()
        await pump_until(dialog, lambda: dialog.busy and not dialog.button_texts)
        dialog.close()
        dialog.event_generate("<Escape>")
        self.assertTrue(dialog.winfo_exists())
        gate.set_result(None)
        await self.heading(dialog, RESULT_HEADING)


class ProblemTests(StepCase):
    async def test_still_running_offers_try_again(self) -> None:
        self.outcomes = [ImportProblem(Problem.RUNNING), RESULT]
        dialog = self.step()
        dialog.bring_over()
        await self.heading(dialog, PROBLEM_HEADING)
        self.assertIn(ImportProblem(Problem.RUNNING).text, labels(dialog.content))
        self.assertEqual(dialog.button_texts, ["Try again", "Not now"])
        dialog.replace()
        await self.heading(dialog, RESULT_HEADING)
        self.assertEqual(self.calls, 2)

    async def test_other_problems_say_why_and_close(self) -> None:
        for problem in (Problem.TOO_NEW, Problem.UNREADABLE, Problem.NO_DATA, Problem.FAILED):
            with self.subTest(problem=problem):
                self.outcomes = [ImportProblem(problem)]
                dialog = self.step()
                dialog.bring_over()
                await self.heading(dialog, PROBLEM_HEADING)
                self.assertIn(ImportProblem(problem).text, labels(dialog.content))
                self.assertEqual(dialog.button_texts, ["Close"])
                dialog.close()
                self.assertEqual(self.done[-1], None)

    async def test_an_unexpected_error_is_a_plain_failure(self) -> None:
        self.outcomes = [ValueError("C:\\Users\\Alex\\secret path")]
        dialog = self.step()
        with self.assertLogs("app.forge.ui.import_step", "ERROR") as logged:
            dialog.bring_over()
            await self.heading(dialog, PROBLEM_HEADING)
        self.assertIn(t(FAILED_TEXT), labels(dialog.content))
        self.assertNotIn("Alex", "\n".join(logged.output))

    async def test_the_grab_goes_back_to_who_had_it(self) -> None:
        holder = ctk.CTkToplevel(self.root)
        holder.attributes("-alpha", 0.0)
        self.addCleanup(holder.destroy)
        await pump(holder, 0.05)
        holder.grab_set()
        dialog = self.step()
        await pump(self.root, 0.3)  # it takes the grab once shown
        self.assertEqual(str(self.root.grab_current()), str(dialog))
        dialog.close()
        self.assertEqual(str(self.root.grab_current()), str(holder))


def fake_services(current: ForgeSettings | None = None) -> SimpleNamespace:
    """Everything ImportTarget names (the import itself is faked)."""
    settings = SimpleNamespace(current=current or ForgeSettings())
    return SimpleNamespace(
        paths=None,
        settings=settings,
        presets=None,
        history=None,
        matches=None,
        airgrabber_settings=None,
        run_lock=asyncio.Lock(),
    )


class BoxTests(StepCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.page = ctk.CTkFrame(self.root)
        self.page.pack()
        self.foot = ctk.CTkFrame(self.page, fg_color="transparent")
        self.foot.grid()
        self.foot.grid_remove()
        self.found: OldInstall | None = OLD

    def box(self, services: object) -> BringOverBox:
        return BringOverBox(
            self.foot,
            services,
            find_old_install=lambda: self.found,
            bring_over=self.fake_bring_over,
            has_own_data=self.fake_has_own_data,
        )

    async def test_shown_while_the_old_data_exists(self) -> None:
        box = self.box(fake_services())
        assert box.done is not None
        await box.done
        self.assertIsNotNone(box.row)
        self.assertEqual(self.foot.winfo_manager(), "grid")
        assert box.button is not None
        self.assertEqual(box.button.cget("text"), t(BOX_BUTTON))

    async def test_hidden_otherwise(self) -> None:
        cases: dict[str, tuple[object, OldInstall | None]] = {
            "imported": (fake_services(ForgeSettings(imported_from="MediaForge 1.3.0")), OLD),
            "no old install": (fake_services(), None),
            "no old data": (fake_services(), OldInstall(db_file=None, uninstall_string="x")),
            "a fake without the parts": (SimpleNamespace(settings=SimpleNamespace()), OLD),
        }
        for name, (services, found) in cases.items():
            with self.subTest(name):
                self.found = found
                box = self.box(services)
                if box.done is not None:
                    await box.done
                self.assertIsNone(box.row)
                self.assertEqual(self.foot.winfo_manager(), "")

    async def test_bring_over_from_settings_hides_the_row(self) -> None:
        box = self.box(fake_services())
        assert box.done is not None and box.button is None
        await box.done
        assert box.button is not None
        box.button.invoke()
        dialog = box.dialog
        assert dialog is not None
        dialog.attributes("-alpha", 0.0)
        self.assertEqual(dialog.master, self.root)  # over Settings (here the root)
        dialog.bring_over()
        await self.heading(dialog, RESULT_HEADING)
        dialog.close()
        self.assertIsNone(box.row)
        self.assertEqual(self.foot.winfo_manager(), "")

    async def test_not_now_from_settings_keeps_the_row(self) -> None:
        box = self.box(fake_services())
        assert box.done is not None
        await box.done
        box.open()
        assert box.dialog is not None
        box.dialog.close()
        self.assertIsNotNone(box.row)


class HostOfferTests(HostCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.lookups = 0
        self.found: OldInstall | None = OLD
        self.offering = ForgeHost(self.root, self.runtime, find_old_install=self.find)
        self.addAsyncCleanup(self.offering.shutdown)

    def find(self) -> OldInstall | None:
        self.lookups += 1
        return self.found

    async def opened(self) -> ImportStep:
        window = await self.offering.open()
        assert window is not None
        window.attributes("-alpha", 0.0)
        await pump_until(window, lambda: self.offering.import_step is not None)
        step = self.offering.import_step
        assert step is not None
        step.attributes("-alpha", 0.0)
        self.assertEqual(step.master, window)
        return step

    async def test_asked_at_the_first_open_and_again_after_not_now(self) -> None:
        step = await self.opened()
        self.assertEqual(step.heading.cget("text"), t(ASK_HEADING))
        await self.offering.open()  # already showing: not asked twice
        await pump(self.root, 0.1)
        self.assertIs(self.offering.import_step, step)
        self.assertEqual(self.lookups, 1)
        step.close()  # Not now
        self.assertIsNone(self.offering.import_step)
        await self.offering.open()  # still showing: no new question
        await pump(self.root, 0.1)
        self.assertIsNone(self.offering.import_step)
        self.offering.close()  # hidden, then opened again: asked again (D13)
        again = await self.opened()
        self.assertIsNot(again, step)

    async def test_never_after_an_import(self) -> None:
        services = await self.runtime.start()
        assert services is not None
        await services.settings.update(imported_from="MediaForge 1.3.0, 2026-11-02")
        window = await self.offering.open()
        assert window is not None
        await pump(window, 0.2)
        self.assertIsNone(self.offering.import_step)
        self.assertEqual(self.lookups, 0)

    async def test_not_without_old_data(self) -> None:
        self.found = OldInstall(db_file=None, uninstall_string="x")
        window = await self.offering.open()
        assert window is not None
        await pump_until(window, lambda: self.lookups == 1)
        await pump(window, 0.1)
        self.assertIsNone(self.offering.import_step)

    async def test_an_import_reloads_the_presets_and_the_page(self) -> None:
        """From the window's step: the import's last step stores `imported_from`."""
        step = await self.opened()
        window = self.offering.window
        assert window is not None and window.queue is not None
        services = self.services()

        async def fake_import(forge: Any, old: OldInstall) -> ImportResult:
            await services.settings.update(imported_from=RESULT.imported_from)
            return RESULT

        step._bring_over = fake_import
        with (
            mock.patch.object(window.queue, "reload_presets") as reload,
            mock.patch.object(window, "refresh_page") as refresh,
        ):
            step.bring_over()
            await pump_until(step, lambda: step.result is not None)
            step.close()
            await services.settings.update(window_geometry="1000x700+5+5")  # not an import
        reload.assert_called_once_with()
        refresh.assert_called_once_with()

    async def test_an_import_from_settings_refreshes_the_open_window(self) -> None:
        """Settings' "Bring over…" (BringOverBox) runs the import on the same services: the
        open Forge window is refreshed the same way."""
        step = await self.opened()
        step.close()  # Not now in the window
        window = self.offering.window
        assert window is not None and window.queue is not None
        services = self.services()

        async def fake_import(forge: Any, old: OldInstall) -> ImportResult:
            await forge.settings.update(imported_from=RESULT.imported_from)
            return RESULT

        async def no_own_data(forge: Any) -> bool:
            return False

        settings_page = ctk.CTkToplevel(self.root)
        settings_page.attributes("-alpha", 0.0)
        self.addCleanup(settings_page.destroy)
        foot = ctk.CTkFrame(settings_page)
        foot.grid()
        foot.grid_remove()
        box = BringOverBox(
            foot,
            services,
            find_old_install=lambda: OLD,
            bring_over=fake_import,
            has_own_data=no_own_data,
        )
        assert box.done is not None
        await box.done
        box.open()
        dialog = box.dialog
        assert dialog is not None
        dialog.attributes("-alpha", 0.0)
        with (
            mock.patch.object(window.queue, "reload_presets") as reload,
            mock.patch.object(window, "refresh_page") as refresh,
        ):
            dialog.bring_over()
            await pump_until(dialog, lambda: dialog.result is not None)
            dialog.close()
        reload.assert_called_once_with()
        refresh.assert_called_once_with()
        self.assertIsNone(box.row)

    async def test_the_remover_is_handed_to_the_step(self) -> None:
        removed: list[int] = []
        self.offering.on_remove_old = lambda: removed.append(1)
        step = await self.opened()
        step.result = RESULT
        step.show_result(RESULT)
        assert step.remove_button is not None
        step.remove_button.invoke()
        self.assertEqual(removed, [1])

    async def test_the_default_host_finds_nothing_in_tests(self) -> None:
        """The plain host (test_host.py's) looks with the real finder, hidden by the base."""
        window = await self.host.open()
        assert window is not None
        await pump(window, 0.3)
        self.assertIsNone(self.host.import_step)
