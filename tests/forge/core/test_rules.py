"""The rules for watched folders, pure (app/forge/core/rules.py): 4K from the name, the first rule
that fits from the top, the subfolder option, nested folders, one listing per folder, the
refusals and warnings on a rule's row, AirGrabber's rule per folder, where files go, and the
texts in Dutch."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app import i18n
from app.forge.core.parser import parse_filename
from app.forge.core.rules import (
    MOVED_INTO,
    NEVER_REACHED,
    NO_MOVIES_FOLDER,
    PRESET_GONE,
    REFUSE_HOLDS_DESTINATION,
    REFUSE_IN_DESTINATION,
    REFUSE_IN_LIBRARY,
    REFUSE_NO_DESTINATION,
    REFUSE_NO_FOLDER,
    UNREACHABLE_FOLDER,
    RuleNote,
    WatchedRule,
    destinations,
    fits,
    holds,
    is_4k,
    listing_roots,
    newly_watched,
    record_roots,
    refusal,
    rule_for,
    rule_notes,
    uncovered,
    watch_list,
    watched_folders,
)
from app.forge.models.media import MediaKind
from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import AirGrabberDownloads, AirGrabberFolder, ForgeSettings
from app.i18n import Language
from app.i18n_nl_forge import NL
from tests.forge.base import ForgeTestCase

D = r"D:\Downloads"
E = r"E:\Torrents"
SERIES = r"\\NAS\Series"
MOVIES = r"\\NAS\Movies"
MOVIES_4K = r"\\NAS\Movies 4K"
SETTINGS = ForgeSettings(series_folder=SERIES, movies_folder=MOVIES)
NAMES = {"Series (Folder)", "Movie (Folder)"}


def to_4k(folder: str = MOVIES_4K) -> RuleTarget:
    return RuleTarget(preset="Movie (Folder)", to="folder", folder=folder)


def with_airgrabber(*paths: str, **changes: Any) -> ForgeSettings:
    folders = [AirGrabberFolder(path=p, why="finished") for p in paths]
    return SETTINGS.model_copy(
        update={"airgrabber": AirGrabberDownloads(folders=folders), **changes}
    )


def found(file: str, watch: list[WatchedRule]) -> WatchedRule | None:
    path = Path(file)
    return rule_for(path, parse_filename(path.name), watch)


def taken_by(file: str, watch: list[WatchedRule]) -> int | None:
    entry = found(file, watch)
    return None if entry is None else entry.rule.id


class MatchingTests(ForgeTestCase):
    def test_4k_is_read_from_the_name(self) -> None:
        self.assertTrue(is_4k(parse_filename("Dune.2021.2160p.WEB-DL.mkv")))
        self.assertTrue(is_4k(parse_filename("Dune 2021 UHD.mkv")))
        self.assertTrue(is_4k(parse_filename("Dune.2021.4K.HDR.mkv")))
        self.assertFalse(is_4k(parse_filename("Dune.2021.1080p.mkv")))
        self.assertFalse(is_4k(parse_filename("Dune.2021.mkv")))  # silent on it: not 4K
        self.assertFalse(is_4k(parse_filename("Dune.2021.x2160px.mkv")))  # only a whole word

    def test_fits_by_kind_then_quality(self) -> None:
        film_4k, show = parse_filename("Dune.2021.2160p.mkv"), parse_filename("Show.S01E02.mkv")
        self.assertTrue(fits(Rule(folder=D), film_4k) and fits(Rule(folder=D), show))
        self.assertFalse(fits(Rule(folder=D, take="films"), show))
        self.assertFalse(fits(Rule(folder=D, take="shows"), film_4k))
        self.assertTrue(fits(Rule(folder=D, quality="4k"), film_4k))
        self.assertFalse(fits(Rule(folder=D, quality="not_4k"), film_4k))
        self.assertTrue(fits(Rule(folder=D, quality="not_4k"), show))

    def test_first_fit_from_the_top(self) -> None:
        rules = [
            Rule(id=1, folder=D, take="films", quality="4k", films=to_4k()),
            Rule(id=2, folder=D, take="films"),
            Rule(id=3, folder=D, take="shows"),
        ]
        watch = watch_list(rules, SETTINGS)
        self.assertEqual(taken_by(D + r"\Dune.2021.2160p.mkv", watch), 1)
        self.assertEqual(taken_by(D + r"\Dune.2021.1080p.mkv", watch), 2)
        show = found(D + r"\VisionQuest.S01E02.mkv", watch)
        assert show is not None
        self.assertEqual((show.number, show.rule.id), (3, 3))

    def test_top_folder_only_skips_the_folders_inside(self) -> None:
        [entry] = watch_list([Rule(folder=D, subfolders=False)], SETTINGS)
        self.assertTrue(holds(entry, Path(D, "x.mkv")))
        self.assertTrue(holds(entry, Path(r"d:\downloads\X.mkv")))  # Windows' comparison
        self.assertFalse(holds(entry, Path(D, "rel", "x.mkv")))
        self.assertFalse(holds(entry, Path(E, "x.mkv")))
        [deep] = watch_list([Rule(folder=D)], SETTINGS)
        self.assertTrue(holds(deep, Path(D, "rel", "x.mkv")))

    def test_nested_folders_are_offered_to_each_rule(self) -> None:
        courses = D + r"\Courses"
        rules = [Rule(id=1, folder=D, take="shows"), Rule(id=2, folder=courses)]
        watch = watch_list(rules, SETTINGS)
        self.assertEqual(taken_by(courses + r"\Dune.2021.mkv", watch), 2)
        self.assertEqual(taken_by(courses + r"\Show.S01E01.mkv", watch), 1)
        self.assertIsNone(found(D + r"\Dune.2021.mkv", watch))

    def test_listing_roots_list_a_nested_folder_once(self) -> None:
        rules = [
            Rule(folder=D, take="shows"),
            Rule(folder=D + r"\Courses"),
            Rule(folder=r"d:\downloads", take="films"),
            Rule(folder=E),
        ]
        self.assertEqual(listing_roots(watch_list(rules, SETTINGS)), [Path(D), Path(E)])
        self.assertEqual(watched_folders(rules, SETTINGS), [D, D + r"\Courses", E])

    def test_a_file_no_rule_fits(self) -> None:
        watch = watch_list([Rule(folder=D, take="films", quality="4k")], SETTINGS)
        self.assertIsNone(found(D + r"\Dune.2021.1080p.mkv", watch))
        self.assertIsNone(found(D + r"\Show.S01E01.2160p.mkv", watch))
        self.assertIsNone(found(E + r"\Dune.2021.2160p.mkv", watch))

    def test_an_off_rule_watches_nothing(self) -> None:
        self.assertEqual(watch_list([Rule(folder=D, on=False)], SETTINGS), [])


class CheckTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    def refusals(self, rule: Rule, settings: ForgeSettings = SETTINGS) -> list[str]:
        [notes] = rule_notes([rule], settings, presets=NAMES)
        return [note.text for note in notes if note.level == "refuse"]

    def test_refusals(self) -> None:
        moves_to = RuleTarget(preset="Movie (Folder)", to="folder", folder=r"D:\Movies")
        same_as_destination = Rule(folder=r"D:\Movies", take="films", films=moves_to)
        self.assertEqual(self.refusals(same_as_destination), [REFUSE_IN_DESTINATION])
        # Inside the folder it moves to is fine: a look never goes up (the owner, 2026-11-20).
        inside_destination = Rule(folder=r"D:\Movies\New", take="films", films=moves_to)
        self.assertEqual(self.refusals(inside_destination), [])
        sorted_ = RuleTarget(preset="Movie (Folder)", to="folder", folder=D + r"\Sorted")
        holding = Rule(folder=D, take="films", films=sorted_)
        self.assertEqual(self.refusals(holding), [REFUSE_HOLDS_DESTINATION])
        a_library = Rule(folder=SERIES, stays=True)
        self.assertEqual(self.refusals(a_library), [REFUSE_IN_LIBRARY])
        in_library = Rule(folder=SERIES + r"\Incoming", stays=True)
        self.assertEqual(self.refusals(in_library), [])
        self.assertEqual(self.refusals(Rule(folder="")), [REFUSE_NO_FOLDER])
        no_destination = Rule(folder=D, films=RuleTarget(preset="Movie (Folder)", to="folder"))
        self.assertEqual(self.refusals(no_destination), [REFUSE_NO_DESTINATION])
        # A library folder inside the watched folder its rule moves to: refused too.
        series_inside = SETTINGS.model_copy(update={"series_folder": D + r"\Series"})
        self.assertEqual(
            self.refusals(Rule(folder=D, take="shows"), series_inside), [REFUSE_HOLDS_DESTINATION]
        )
        # A Stays rule moves nothing; a films rule's missing shows folder doesn't count.
        self.assertEqual(self.refusals(Rule(folder=D, stays=True)), [])
        films_only = Rule(folder=D, take="films", shows=RuleTarget(preset="x", to="folder"))
        self.assertEqual(self.refusals(films_only), [])

        rules = [Rule(id=1, folder=""), Rule(id=2, folder=D)]
        self.assertEqual(refusal(rules, SETTINGS), "Rule 1 can't be saved yet. Its row says why.")
        self.assertEqual([w.rule.id for w in watch_list(rules, SETTINGS)], [2])
        self.assertIsNone(refusal(rules[1:], SETTINGS))
        # Off, it still can't be saved.
        self.assertIsNotNone(refusal([Rule(folder="", on=False)], SETTINGS))

    def test_airgrabbers_rule_is_refused_only_without_its_destination(self) -> None:
        settings = with_airgrabber(SERIES + r"\in", D)
        self.assertEqual(self.refusals(Rule(airgrabber=True), settings), [])
        self.assertIsNone(refusal([Rule(airgrabber=True)], settings))
        no_folder = Rule(airgrabber=True, films=RuleTarget(preset="Movie (Folder)", to="folder"))
        self.assertEqual(self.refusals(no_folder, settings), [REFUSE_NO_DESTINATION])
        self.assertEqual(
            refusal([Rule(folder=E), no_folder], settings),
            "Rule 2 can't be saved yet. Its row says why.",
        )
        self.assertEqual(watch_list([no_folder], settings), [])
        # Its films' target isn't used by a shows-only or a Stays rule.
        shows_only = no_folder.model_copy(update={"take": "shows"})
        self.assertEqual(self.refusals(shows_only, settings), [])
        self.assertEqual(self.refusals(no_folder.model_copy(update={"stays": True}), settings), [])

    def test_a_rule_below_a_wider_one_never_gets_a_file(self) -> None:
        notes = rule_notes(
            [Rule(folder=D, take="films"), Rule(folder=D, take="films", quality="4k")],
            SETTINGS,
            presets=NAMES,
        )
        self.assertEqual(
            notes[1],
            [
                RuleNote(
                    "warn",
                    "⚠ This rule never gets a file: rule 1 above takes the same files first. "
                    "Drag it higher, or change one of them.",
                )
            ],
        )
        self.assertEqual(notes[0], [])
        narrower_first = [Rule(folder=D, take="films", quality="4k"), Rule(folder=D, take="films")]
        self.assertEqual(rule_notes(narrower_first, SETTINGS, presets=NAMES), [[], []])
        off_above = [Rule(folder=D, on=False), Rule(folder=D, take="films")]
        self.assertEqual(rule_notes(off_above, SETTINGS, presets=NAMES), [[], []])
        # Top folder only above doesn't cover a rule looking in subfolders; a holder does.
        top_only = [Rule(folder=D, subfolders=False), Rule(folder=D)]
        self.assertEqual(rule_notes(top_only, SETTINGS, presets=NAMES)[1], [])
        holder = [Rule(folder=D), Rule(folder=D + r"\Courses", subfolders=False, take="shows")]
        [note] = rule_notes(holder, SETTINGS, presets=NAMES)[1]
        self.assertEqual(note.text, i18n.t(NEVER_REACHED, n=1))
        top_holder = [Rule(folder=D, subfolders=False), Rule(folder=D + r"\Courses")]
        self.assertEqual(rule_notes(top_holder, SETTINGS, presets=NAMES)[1], [])

    def test_airgrabbers_rule_never_reached_only_when_every_folder_is(self) -> None:
        settings = with_airgrabber(D, E)
        one = [Rule(folder=D), Rule(airgrabber=True)]
        self.assertEqual(rule_notes(one, settings, presets=NAMES)[1], [])
        both = [Rule(folder=D), Rule(folder=E), Rule(airgrabber=True)]
        [note] = rule_notes(both, settings, presets=NAMES)[2]
        self.assertEqual(note.text, i18n.t(NEVER_REACHED, n=1))
        # AirGrabber's rule above covers an owner's rule on one of its folders.
        below = [Rule(airgrabber=True), Rule(folder=E, take="shows")]
        [note] = rule_notes(below, settings, presets=NAMES)[1]
        self.assertEqual(note.text, i18n.t(NEVER_REACHED, n=1))

    def test_a_gone_preset_warns(self) -> None:
        rule = Rule(folder=D, films=RuleTarget(preset="Gone"))
        [notes] = rule_notes([rule], SETTINGS, presets=NAMES)
        self.assertEqual(notes, [RuleNote("warn", "⚠ Its preset is gone: choose another.")])
        shows_only = rule.model_copy(update={"take": "shows"})  # its films' preset isn't used
        self.assertEqual(rule_notes([shows_only], SETTINGS, presets=NAMES), [[]])

    def test_a_library_folder_not_chosen_warns_only_its_rules(self) -> None:
        settings = SETTINGS.model_copy(update={"movies_folder": ""})
        rules = [
            Rule(folder=D, take="films"),
            Rule(folder=E, take="shows"),
            Rule(folder=r"F:\New", take="films", films=to_4k()),
            Rule(folder=r"G:\New", take="films", stays=True),
        ]
        notes = rule_notes(rules, settings, presets=NAMES)
        self.assertEqual(notes[0], [RuleNote("warn", i18n.t(NO_MOVIES_FOLDER))])
        self.assertEqual(notes[1:], [[], [], []])
        [series] = rule_notes(
            [Rule(folder=D, take="shows")],
            SETTINGS.model_copy(update={"series_folder": ""}),
            presets=NAMES,
        )
        self.assertEqual(series, [RuleNote("warn", "⚠ Choose your Series folder under Folders.")])

    def test_unreachable_folders_warn(self) -> None:
        away = {os.path.normcase(MOVIES)}
        [notes] = rule_notes([Rule(folder=D)], SETTINGS, presets=NAMES, unreachable=away)
        self.assertEqual(
            notes,
            [
                RuleNote(
                    "warn",
                    "⚠ Can't reach \\\\NAS\\Movies right now. Films wait in the queue until "
                    "it's back.",
                )
            ],
        )
        shows = rule_notes(
            [Rule(folder=D, take="shows")],
            SETTINGS,
            presets=NAMES,
            unreachable={os.path.normcase(SERIES)},
        )
        self.assertEqual(
            shows[0][0].text,
            "⚠ Can't reach \\\\NAS\\Series right now. Shows wait in the queue until it's back.",
        )
        one_place = SETTINGS.model_copy(update={"series_folder": MOVIES})
        [both] = rule_notes([Rule(folder=D)], one_place, presets=NAMES, unreachable=away)
        self.assertEqual(
            both[0].text,
            "⚠ Can't reach \\\\NAS\\Movies right now. Files wait in the queue until it's back.",
        )
        nas = r"\\NAS\Downloads"
        [watched] = rule_notes(
            [Rule(folder=nas)], SETTINGS, presets=NAMES, unreachable={os.path.normcase(nas)}
        )
        self.assertEqual(watched, [RuleNote("warn", "⚠ Can't reach \\\\NAS\\Downloads right now.")])
        # A trailing separator or another case is the same folder.
        [again] = rule_notes(
            [Rule(folder=nas + "\\")], SETTINGS, presets=NAMES, unreachable={nas.upper()}
        )
        self.assertEqual(len(again), 1)

    def test_moving_into_a_watched_folder_is_said(self) -> None:
        sorted_ = RuleTarget(preset="Movie (Folder)", to="folder", folder=D + r"\Sorted")
        rules = [Rule(folder=D), Rule(folder=E, take="films", films=sorted_)]
        notes = rule_notes(rules, SETTINGS, presets=NAMES)
        self.assertIn(
            RuleNote(
                "info",
                "Rule 2 moves files into this folder. MediaForge won't pick them up again here.",
            ),
            notes[0],
        )
        self.assertEqual(notes[1], [])
        # An off rule moves nothing.
        off = [rules[0], rules[1].model_copy(update={"on": False})]
        self.assertEqual(rule_notes(off, SETTINGS, presets=NAMES)[0], [])
        self.assertEqual(i18n.t(MOVED_INTO, n=2), notes[0][-1].text)

    def test_watching_inside_what_another_rule_moves_to_is_said(self) -> None:
        moved_into = RuleNote("info", i18n.t(MOVED_INTO, n=2))
        to_e = RuleTarget(preset="Movie (Folder)", to="folder", folder=E)
        for watched in (E, E.lower() + "\\"):
            rules = [Rule(folder=watched), Rule(folder=D, take="films", films=to_e)]
            self.assertEqual(rule_notes(rules, SETTINGS, presets=NAMES)[0], [moved_into], watched)
        # Watching inside what another rule moves to: nothing to say, a look never goes up (the
        # owner's ruling, 2026-11-20); a library folder's too.
        inside = [Rule(folder=E + r"\New"), Rule(folder=D, take="films", films=to_e)]
        self.assertEqual(rule_notes(inside, SETTINGS, presets=NAMES)[0], [])
        in_series = [Rule(folder=SERIES + r"\in"), Rule(folder=D, take="shows")]
        self.assertEqual(rule_notes(in_series, SETTINGS, presets=NAMES)[0], [])
        self.assertEqual(rule_notes(in_series[:1], SETTINGS, presets=NAMES)[0], [])
        # A Stays rule moves nothing, whatever its targets still say; an off rule neither.
        stays = Rule(folder=D, stays=True, films=to_e)
        self.assertEqual(rule_notes([Rule(folder=E), stays], SETTINGS, presets=NAMES), [[], []])
        off = Rule(folder=D, take="films", films=to_e, on=False)
        self.assertEqual(rule_notes([Rule(folder=E), off], SETTINGS, presets=NAMES), [[], []])

    def test_note_order(self) -> None:
        rule = Rule(
            folder=SERIES,
            take="films",
            films=RuleTarget(preset="Gone", to="folder"),
        )
        into = RuleTarget(preset="Movie (Folder)", to="folder", folder=SERIES + r"\Films")
        mover = Rule(folder=D, take="films", films=into)  # moves its films into the rule's folder
        notes = rule_notes(
            [rule, mover],
            SETTINGS,
            presets=NAMES,
            unreachable={os.path.normcase(SERIES)},
        )[0]
        self.assertEqual(
            notes,
            [
                RuleNote("refuse", i18n.t(REFUSE_NO_DESTINATION)),
                RuleNote("refuse", i18n.t(REFUSE_IN_LIBRARY)),
                RuleNote("warn", i18n.t(UNREACHABLE_FOLDER, folder=SERIES)),
                RuleNote("warn", i18n.t(PRESET_GONE)),
                RuleNote("info", i18n.t(MOVED_INTO, n=2)),
            ],
        )

    def test_airgrabbers_rule_watches_each_of_its_folders(self) -> None:
        settings = with_airgrabber(r"D:\Done", r"F:\Films", MOVIES_4K + r"\in", SERIES + r"\x")
        rules = [
            Rule(id=1, folder=r"D:\Done\Mine"),  # nesting between rules is allowed
            Rule(id=2, folder=E, take="films", quality="4k", films=to_4k()),
            Rule(id=9, airgrabber=True),
        ]
        watch = watch_list(rules, settings)
        airgrabbers = [w for w in watch if w.rule.airgrabber]
        self.assertEqual([w.folder for w in airgrabbers], [Path(r"D:\Done"), Path(r"F:\Films")])
        self.assertEqual({w.number for w in airgrabbers}, {3})
        self.assertEqual(
            watched_folders(rules, settings), [r"D:\Done\Mine", E, r"D:\Done", r"F:\Films"]
        )
        self.assertEqual(listing_roots(watch), [Path(E), Path(r"D:\Done"), Path(r"F:\Films")])
        off = rules[:2] + [rules[2].model_copy(update={"on": False})]
        self.assertFalse(any(w.rule.airgrabber for w in watch_list(off, settings)))

    def test_newly_watched_is_airgrabbers_rules_new_folders(self) -> None:
        rules = [Rule(id=1, folder=E), Rule(id=9, airgrabber=True)]
        before = with_airgrabber(r"D:\Done")
        after = with_airgrabber(r"d:\done" + "\\", r"F:\Films", E)
        # E is another rule's folder: AirGrabber's rule watches it now all the same.
        self.assertEqual(newly_watched(before, after, rules), [r"F:\Films", E])
        self.assertEqual(newly_watched(after, before, rules), [])
        # AirGrabber's rule's on / off is each side's setting, whatever the rules say.
        off = before.model_copy(update={"use_airgrabber_downloads": False})
        self.assertEqual(newly_watched(off, before, rules), [r"D:\Done"])
        self.assertEqual(newly_watched(before, off, rules), [])
        # The owner's own rules never count: only AirGrabber's rule is watched behind their back.
        mine = [Rule(id=1, folder=E), Rule(id=2, folder=r"G:\New"), Rule(id=9, airgrabber=True)]
        self.assertEqual(newly_watched(before, before, mine), [])

    def test_uncovered_are_the_rules_a_gone_destination_or_a_new_folder_exposes(self) -> None:
        sorted_ = r"D:\Downloads\Sorted"
        watching = Rule(id=1, folder=D, take="films")
        sorter = Rule(id=2, folder=E, take="films", films=to_4k(sorted_))
        flat = Rule(id=3, folder=D, take="films", subfolders=False)
        rules = [watching, sorter, flat]
        self.assertEqual(uncovered(rules, SETTINGS, rules, SETTINGS), [])
        # Deleted, switched off or pointed elsewhere: its folder's files are found from now on
        # by the rule watching above it (not by one that doesn't look in subfolders).
        self.assertEqual(uncovered(rules, SETTINGS, [watching, flat], SETTINGS), [1])
        off = sorter.model_copy(update={"on": False})
        self.assertEqual(uncovered(rules, SETTINGS, [watching, off, flat], SETTINGS), [1])
        elsewhere = sorter.model_copy(update={"films": to_4k(MOVIES_4K)})
        self.assertEqual(uncovered(rules, SETTINGS, [watching, elsewhere, flat], SETTINGS), [1])
        # A watched folder inside a destination was looked in all along: nothing new to see.
        incoming = Rule(id=5, folder=D + r"\Incoming", take="films")
        to_d = Rule(id=6, folder=E, take="films", films=to_4k(D))
        self.assertEqual(uncovered([incoming, to_d], SETTINGS, [incoming], SETTINGS), [])
        # Still inside another destination: nothing is exposed.
        wider = sorter.model_copy(update={"films": to_4k(r"D:\Downloads")})
        deeper = [watching.model_copy(update={"folder": r"D:\Other"}), wider]
        self.assertEqual(uncovered([*deeper, sorter], SETTINGS, deeper, SETTINGS), [])
        # A library folder chosen elsewhere: the rule watching where it was.
        inside_dl = SETTINGS.model_copy(update={"movies_folder": r"D:\Downloads\Films"})
        shows = [Rule(id=4, folder=D, take="shows")]
        self.assertEqual(uncovered(shows, inside_dl, shows, SETTINGS), [4])
        self.assertEqual(uncovered(shows, SETTINGS, shows, inside_dl), [])
        # AirGrabber's folder from "overlap" (inside a library folder) to watched.
        ag = [Rule(id=9, airgrabber=True)]
        overlap = with_airgrabber(MOVIES + r"\Incoming")
        moved = overlap.model_copy(update={"movies_folder": r"\\NAS\Films"})
        self.assertEqual(uncovered(ag, overlap, ag, moved), [9])
        # New rules (no id yet) are fresh anyway.
        self.assertEqual(uncovered([], SETTINGS, [Rule(folder=D)], SETTINGS), [])

    def test_destinations_and_record_roots(self) -> None:
        c = r"C:\Courses"
        rules = [
            Rule(folder=E, take="films", quality="4k", films=to_4k()),
            Rule(folder=c, stays=True, shows=RuleTarget(preset="x", to="folder", folder=r"Z:\X")),
            Rule(folder=D, take="shows", films=to_4k(folder=r"Y:\Unused")),
            Rule(folder=r"F:\Off", on=False, films=to_4k(folder=r"W:\Off")),
        ]
        self.assertEqual(destinations(rules, SETTINGS), [Path(SERIES), Path(MOVIES), Path(MOVIES_4K)])
        roots = record_roots(rules, SETTINGS)
        self.assertEqual(
            roots,
            [
                (Path(SERIES), frozenset({MediaKind.EPISODE})),
                (Path(MOVIES), frozenset({MediaKind.MOVIE})),
                (Path(MOVIES_4K), frozenset({MediaKind.MOVIE})),
                (Path(c), frozenset({MediaKind.EPISODE, MediaKind.MOVIE})),
            ],
        )
        # Merged per folder: one library folder for both kinds.
        one = SETTINGS.model_copy(update={"movies_folder": SERIES.lower()})
        self.assertEqual(
            record_roots([], one), [(Path(SERIES), frozenset({MediaKind.EPISODE, MediaKind.MOVIE}))]
        )

    def test_notes_follow_the_language(self) -> None:
        i18n.set_language(Language.NL)
        [notes] = rule_notes([Rule(folder="")], SETTINGS, presets=NAMES)
        self.assertEqual(notes, [RuleNote("refuse", NL[REFUSE_NO_FOLDER])])
        self.assertEqual(
            refusal([Rule(folder="")], SETTINGS),
            "Regel 1 kan nog niet worden opgeslagen. Zijn rij zegt waarom.",
        )
        below = rule_notes([Rule(folder=D), Rule(folder=D, take="films")], SETTINGS, presets=NAMES)
        self.assertTrue(below[1][0].text.startswith("⚠ Deze regel krijgt nooit een bestand: regel 1"))
