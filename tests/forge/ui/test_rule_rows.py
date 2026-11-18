"""A rule's words (app/forge/ui/rule_rows.py): the name made from its choices, its own name first,
the live check's sentences, a row of the rules list (look A) and the line for the files no rule
fits, in English and Dutch."""

from __future__ import annotations

from pathlib import Path

from app import i18n
from app.forge.core.auto import Unfit
from app.forge.core.rule_check import CheckGroup
from app.forge.models.media import MediaKind
from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import ForgeSettings
from app.forge.ui.rule_rows import (
    RowLook,
    check_lines,
    last_name,
    made_name,
    name_of,
    row_look,
    unfit_line,
)
from app.i18n import Language
from tests.forge.base import ForgeTestCase

D = r"D:\Downloads"
MOVIE, EPISODE = MediaKind.MOVIE, MediaKind.EPISODE


class _Case(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    def dutch(self) -> None:
        i18n.set_language(Language.NL)


class MadeNameTests(_Case):
    def test_made_names(self) -> None:
        self.assertEqual(made_name(Rule(folder=D, take="shows")), "Shows from Downloads")
        self.assertEqual(
            made_name(Rule(folder=D, take="films", quality="4k")), "4K films from Downloads"
        )
        self.assertEqual(made_name(Rule(folder=D)), "Everything from Downloads")
        self.assertEqual(
            made_name(Rule(folder=D, quality="not_4k")), "Everything not in 4K from Downloads"
        )
        self.assertEqual(made_name(Rule(airgrabber=True, take="films")), "AirGrabber's downloads")
        self.assertEqual(made_name(Rule(folder="E:\\", take="films")), "Films from E:\\")

    def test_without_a_folder_only_what_it_takes(self) -> None:
        self.assertEqual(made_name(Rule()), "Everything")
        self.assertEqual(made_name(Rule(take="films", quality="4k")), "4K films")
        self.assertEqual(made_name(Rule(take="shows", quality="not_4k")), "Shows not in 4K")
        self.assertEqual(name_of(Rule(quality="4k")), "Everything in 4K")
        self.dutch()
        self.assertEqual(made_name(Rule(take="films", quality="4k")), "4K-films")

    def test_last_name(self) -> None:
        self.assertEqual(last_name(r"\\NAS\Downloads\New"), "New")
        self.assertEqual(last_name("E:\\"), "E:\\")

    def test_a_rules_own_name_comes_first(self) -> None:
        self.assertEqual(name_of(Rule(folder=D, name="  4K films ")), "4K films")
        self.assertEqual(name_of(Rule(folder=D, name="  ", take="shows")), "Shows from Downloads")

    def test_made_names_in_dutch(self) -> None:
        self.dutch()
        self.assertEqual(made_name(Rule(folder=D, take="shows")), "Series uit Downloads")


class CheckLineTests(_Case):
    def test_check_lines_read_as_the_mockup(self) -> None:
        self.assertEqual(
            check_lines(
                [
                    CheckGroup("fits", MOVIE, 2, None),
                    CheckGroup("a_show", EPISODE, 4, 3),
                    CheckGroup("not_4k", MOVIE, 3, 2),
                ]
            ),
            [
                "2 films fit this rule.",
                "4 episodes don't: they're shows. Rule 3 takes them.",
                "3 films don't: they're not 4K. Rule 2 takes them.",
            ],
        )

    def test_one_file_and_no_rule(self) -> None:
        self.assertEqual(
            check_lines(
                [
                    CheckGroup("fits", EPISODE, 1, None),
                    CheckGroup("a_film", MOVIE, 1, None),
                    CheckGroup("is_4k", EPISODE, 2, None),
                ]
            ),
            [
                "1 episode fits this rule.",
                "1 film doesn't: it's a film. No rule takes it.",
                "2 episodes don't: they're 4K. No rule takes them.",
            ],
        )

    def test_a_rule_above_says_it_once_for_both_kinds(self) -> None:
        self.assertEqual(
            check_lines(
                [
                    CheckGroup("taken_above", EPISODE, 1, 1),
                    CheckGroup("taken_above", MOVIE, 2, 1),
                    CheckGroup("in_subfolder", MOVIE, 1, 2),
                ]
            ),
            [
                "3 files don't: rule 1 above takes them.",
                "1 file doesn't: it's in a folder inside this one. Rule 2 takes it.",
            ],
        )

    def test_in_dutch(self) -> None:
        self.dutch()
        self.assertEqual(
            check_lines([CheckGroup("a_show", EPISODE, 4, 3)]),
            ["4 afleveringen vallen er niet onder: het zijn series. Regel 3 neemt ze."],
        )


class RowLookTests(_Case):
    S = ForgeSettings(series_folder=r"\\nas\video\Series", movies_folder=r"\\nas\video\Movies")

    def test_a_rule_moving_4k_films_to_its_own_folder(self) -> None:
        rule = Rule(
            name="4K films",
            folder=D,
            take="films",
            quality="4k",
            films=RuleTarget(preset="Movie (Folder)", to="folder", folder=r"\\NAS\Movies 4K"),
            mode="auto",
        )
        self.assertEqual(
            row_look(rule, self.S),
            RowLook(
                False, "Downloads", ("Films", "4K"), "Movie (Folder)", r"\\NAS\Movies 4K",
                "Automatic", "4K films", False,
            ),
        )

    def test_airgrabbers_rule_goes_to_both_library_folders(self) -> None:
        look = row_look(Rule(airgrabber=True), self.S)
        self.assertTrue(look.locked)
        self.assertEqual(look.folder, "AirGrabber's downloads")
        self.assertEqual(look.chips, ("Anything",))
        self.assertEqual(look.preset, "Series (Folder) / Movie (Folder)")
        self.assertEqual(look.destination, r"\\nas\video\Series / \\nas\video\Movies")
        self.assertEqual(look.mode, "Manual")
        self.assertEqual(look.tip, "AirGrabber's downloads")
        unchosen = row_look(Rule(airgrabber=True), ForgeSettings())
        self.assertEqual(unchosen.destination, "your Series folder / your Movies folder")

    def test_a_top_folder_only_rule_sorted_in_place_and_off(self) -> None:
        look = row_look(
            Rule(folder=r"D:\Courses", subfolders=False, stays=True, quality="not_4k", on=False),
            self.S,
        )
        self.assertEqual(look.chips, ("Anything", "Not 4K", "Top folder only"))
        self.assertEqual(look.destination, "sorted in this folder")
        self.assertEqual(look.tip, "Everything not in 4K from Courses")
        self.assertTrue(look.dim)

    def test_the_same_preset_and_folder_once(self) -> None:
        both = RuleTarget(preset="Plain", to="folder", folder=r"E:\Sorted")
        look = row_look(Rule(folder=D, shows=both, films=both), self.S)
        self.assertEqual((look.preset, look.destination), ("Plain", r"E:\Sorted"))

    def test_in_dutch(self) -> None:
        self.dutch()
        look = row_look(Rule(folder=D, subfolders=False, stays=True, mode="auto"), self.S)
        self.assertEqual(look.chips, ("Alles", "Alleen de bovenste map"))
        self.assertEqual((look.destination, look.mode), ("gesorteerd in deze map", "Automatisch"))


class UnfitLineTests(_Case):
    def test_one_and_more(self) -> None:
        folder = Path(D)
        self.assertEqual(
            unfit_line(Unfit(folder, (folder / "a.mkv",))),
            "1 file in Downloads fits no rule and is left alone.",
        )
        self.assertEqual(
            unfit_line(Unfit(folder, (folder / "a.mkv", folder / "b.mkv"))),
            "2 files in Downloads fit no rule and are left alone.",
        )
        self.dutch()
        self.assertEqual(
            unfit_line(Unfit(folder, (folder / "a.mkv", folder / "b.mkv"))),
            "2 bestanden in Downloads passen bij geen regel en blijven met rust.",
        )
