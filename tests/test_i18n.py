"""The interface language (app/i18n.py) and its Dutch texts (app/i18n_nl.py)."""

from __future__ import annotations

import ast
import datetime as dt
import string
import unittest
from collections.abc import Iterator
from pathlib import Path

from app import i18n
from app.i18n import Language, N_, t, tn
from app.i18n_nl import NL

ROOT = Path(__file__).resolve().parent.parent
_CATALOG = ROOT / "app" / "i18n_nl.py"
_CALLS = {"t": 1, "tn": 2, "N_": 1}  # function -> how many leading arguments are texts


def _sources() -> Iterator[Path]:
    yield ROOT / "main.py"
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path != _CATALOG:
            yield path


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr if func.value.id == "i18n" else None
    return None


class _Texts:
    """Every text literal handed to t() / tn() / N_() in the app's source, and the f-strings
    handed to them (a mistake: the English text is the key)."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}  # text -> where first seen
        self.formatted: list[str] = []
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                if name not in _CALLS:
                    continue
                where = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                for arg in node.args[: _CALLS[name]]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        self.keys.setdefault(arg.value, where)
                    elif isinstance(arg, ast.JoinedStr):
                        self.formatted.append(where)


_TEXTS = _Texts()


def _fields(text: str) -> set[str]:
    return {name for _literal, name, _spec, _conv in string.Formatter().parse(text) if name}


class CatalogTests(unittest.TestCase):
    def test_every_text_has_a_dutch_entry(self) -> None:
        missing = {key: where for key, where in _TEXTS.keys.items() if key not in NL}
        self.assertEqual(missing, {}, "add these to app/i18n_nl.py")

    def test_no_dutch_entry_is_left_over(self) -> None:
        unused = sorted(key for key in NL if key not in _TEXTS.keys)
        self.assertEqual(unused, [], "no t()/tn()/N_() uses these any more")

    def test_no_f_strings_as_keys(self) -> None:
        self.assertEqual(_TEXTS.formatted, [], "use t('… {name} …', name=value)")

    def test_no_entry_twice(self) -> None:
        tree = ast.parse(_CATALOG.read_text(encoding="utf-8"))
        keys: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys += [
                    k.value
                    for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                ]
        self.assertEqual(sorted({k for k in keys if keys.count(k) > 1}), [])

    def test_placeholders_match(self) -> None:
        wrong = {key: value for key, value in NL.items() if _fields(key) != _fields(value)}
        self.assertEqual(wrong, {})

    def test_dutch_texts_are_filled_in_and_trimmed(self) -> None:
        bad = [key for key, value in NL.items() if not value.strip() or value != value.strip()]
        # A few keys start or end with a space on purpose; the Dutch text then does too.
        bad = [key for key in bad if not (key != key.strip() and NL[key].strip())]
        self.assertEqual(bad, [])


class TranslateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, i18n.current_language())

    def test_english_is_the_default_and_unchanged(self) -> None:
        i18n.set_language(Language.EN)
        self.assertEqual(t("Download"), "Download")
        self.assertEqual(t("Updated to v{version}", version="2.0.5"), "Updated to v2.0.5")

    def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        self.assertEqual(t("Download"), "Downloaden")
        self.assertEqual(t("Updated to v{version}", version="2.0.5"), "Bijgewerkt naar v2.0.5")

    def test_unknown_text_stays_english(self) -> None:
        i18n.set_language(Language.NL)
        self.assertEqual(t("Not a known text"), "Not a known text")

    def test_unknown_language_is_english(self) -> None:
        i18n.set_language("xx")
        self.assertIs(i18n.current_language(), Language.EN)

    def test_marker_does_not_translate(self) -> None:
        i18n.set_language(Language.NL)
        self.assertEqual(N_("Download"), "Download")

    def test_plurals(self) -> None:
        i18n.set_language(Language.EN)
        self.assertEqual(tn("{n} film", "{n} films", 1), "1 film")
        self.assertEqual(tn("{n} film", "{n} films", 3), "3 films")
        self.assertEqual(tn("{n} film", "{n} films", 0), "0 films")
        i18n.set_language(Language.NL)
        self.assertEqual(tn("{n} episode", "{n} episodes", 1), "1 aflevering")
        self.assertEqual(tn("{n} episode", "{n} episodes", 2), "2 afleveringen")

    def test_numbers(self) -> None:
        i18n.set_language(Language.EN)
        self.assertEqual(i18n.number(12345, ","), "12,345")
        self.assertEqual(i18n.number(2.5, "g"), "2.5")
        i18n.set_language(Language.NL)
        self.assertEqual(i18n.number(12345, ","), "12.345")
        self.assertEqual(i18n.number(2.5, "g"), "2,5")
        self.assertEqual(i18n.number(1234.5, ",.1f"), "1.234,5")

    def test_dates(self) -> None:
        day = dt.date(2026, 10, 19)  # a Monday
        i18n.set_language(Language.EN)
        self.assertEqual((i18n.day_short(day), i18n.day_name(day)), ("Mon", "Monday"))
        self.assertEqual((i18n.month_short(day), i18n.month_name(10)), ("Oct", "October"))
        self.assertEqual(i18n.day_short(6), "Sun")
        i18n.set_language(Language.NL)
        self.assertEqual((i18n.day_short(day), i18n.day_name(day)), ("ma", "maandag"))
        self.assertEqual((i18n.month_short(day), i18n.month_name(3)), ("okt", "maart"))
        self.assertEqual(i18n.capitalized(i18n.day_name(6)), "Zondag")

    def test_english_names_match_the_c_locale(self) -> None:
        """The English names are what strftime gave before (tests compare those texts)."""
        i18n.set_language(Language.EN)
        for offset in range(7):
            day = dt.date(2026, 10, 19) + dt.timedelta(days=offset)
            self.assertEqual(i18n.day_short(day), day.strftime("%a"))
            self.assertEqual(i18n.day_name(day), day.strftime("%A"))
        for month in range(1, 13):
            first = dt.date(2026, month, 1)
            self.assertEqual(i18n.month_short(first), first.strftime("%b"))
            self.assertEqual(i18n.month_name(first), first.strftime("%B"))

    def test_native_names(self) -> None:
        self.assertEqual(
            [language.native_name for language in Language], ["English", "Nederlands"]
        )


class LanguageSettingTests(unittest.TestCase):
    def test_english_by_default_also_for_older_settings(self) -> None:
        from app.models import Settings

        self.assertIs(Settings().language, Language.EN)
        self.assertIs(Settings.model_validate({"theme_mode": "dark"}).language, Language.EN)

    def test_stored_values(self) -> None:
        from app.models import Settings

        self.assertIs(Settings.model_validate({"language": "nl"}).language, Language.NL)
        self.assertIs(Settings.model_validate({"language": " NL "}).language, Language.NL)
        self.assertIs(Settings.model_validate({"language": "fr"}).language, Language.EN)

    def test_the_language_box(self) -> None:
        from app.ui.settings_dialog import language_from_name, needs_restart

        self.assertIs(language_from_name("Nederlands"), Language.NL)
        self.assertIs(language_from_name("English"), Language.EN)
        with self.assertRaises(ValueError):
            language_from_name("Deutsch")
        self.assertTrue(needs_restart(Language.NL, Language.EN))
        self.assertFalse(needs_restart(Language.EN, Language.EN))

    def test_dutch_dates_on_the_pages(self) -> None:
        from app.ui.views.calendar_view import day_label, range_label

        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.NL)
        self.assertEqual(range_label(dt.date(2026, 10, 19), dt.date(2026, 11, 1)), "19 okt – 1 nov")
        self.assertEqual(day_label(dt.date(2026, 10, 19)), "ma 19")
        self.assertEqual(day_label(dt.date(2026, 11, 1)), "zo 1 nov")
        i18n.set_language(Language.EN)
        self.assertEqual(range_label(dt.date(2026, 10, 19), dt.date(2026, 11, 1)), "19 Oct – 1 Nov")
        self.assertEqual(day_label(dt.date(2026, 11, 1)), "Sun 1 Nov")

    def test_weekday_names_follow_the_language(self) -> None:
        from app.models import Weekday
        from app.ui.settings_dialog import weekday_from_name, weekday_name

        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.assertEqual(weekday_name(Weekday.MONDAY), "Monday")  # the stored value
        i18n.set_language(Language.NL)
        self.assertEqual(weekday_name(Weekday.SUNDAY), "Zondag")
        self.assertIs(weekday_from_name("Maandag"), Weekday.MONDAY)


if __name__ == "__main__":
    unittest.main()
