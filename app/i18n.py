"""The interface language: English (the default) or Dutch (Settings → Looks → Language).

Every text the user sees is written in English in the code and goes through `t()` where it is
shown: `t("Download")` is "Downloaden" while Dutch is on. The Dutch texts live in
app/i18n_nl.py, keyed by the English text; tests/test_i18n.py checks that every `t()` /
`tn()` / `N_()` text has its Dutch entry and that no entry is left over.

- `t("Updated to v{version}", version=v)`: placeholders are filled in after the lookup (the
  English text is the key, so never an f-string inside `t()`).
- `tn("{n} film", "{n} films", count)`: one / more (`{n}` is the count).
- `N_("text")` marks a text kept in a constant or table at import time; it is translated where
  it is shown (`t(CONSTANT)`), so the language only has to be known before the first window.
- Day and month names (`day_short`, `month_short`, …) and numbers (`number`) without the
  operating system's locale.

The language is chosen once per run, at startup (main.py, from the saved settings): switching
it in Settings takes a restart. Logs stay English. Stdlib only.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from enum import StrEnum


class Language(StrEnum):
    EN = "en"
    NL = "nl"

    @property
    def native_name(self) -> str:
        """Its name in its own language, as the Language box shows it (never translated)."""
        return _NATIVE_NAMES[self]


_NATIVE_NAMES = {Language.EN: "English", Language.NL: "Nederlands"}

_current = Language.EN
_catalog: Mapping[str, str] | None = None


def set_language(language: Language | str) -> None:
    """Use `language` from now on (an unknown value: English)."""
    global _current
    try:
        _current = Language(language)
    except ValueError:
        _current = Language.EN


def current_language() -> Language:
    return _current


def is_dutch() -> bool:
    return _current is Language.NL


def _dutch() -> Mapping[str, str]:
    global _catalog
    if _catalog is None:
        from app.i18n_nl import NL

        _catalog = NL
    return _catalog


def N_(text: str) -> str:
    """Mark `text` for translation without translating it (constants, tables): `t()` it where
    it is shown."""
    return text


def t(text: str, /, **fmt: object) -> str:
    """`text` in the current language; `fmt` fills its {placeholders}."""
    shown = _dutch().get(text, text) if _current is Language.NL else text
    return shown.format(**fmt) if fmt else shown


def tn(singular: str, plural: str, count: int, /, **fmt: object) -> str:
    """The singular text for 1, else the plural one (English and Dutch agree), translated, with
    `{n}` = `count` and `fmt` filled in."""
    return t(singular if count == 1 else plural, n=count, **fmt)


# -- numbers ----------------------------------------------------------------------------------


def number(value: float, spec: str = "") -> str:
    """`format(value, spec)` with Dutch separators while Dutch is on: "1,234" → "1.234",
    "2.5" → "2,5"."""
    text = format(value, spec)
    if _current is Language.NL:
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


# -- dates --------------------------------------------------------------------------------------

_DAYS = {
    Language.EN: ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    Language.NL: ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"),
}
_DAYS_SHORT = {
    Language.EN: ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    Language.NL: ("ma", "di", "wo", "do", "vr", "za", "zo"),
}
_MONTHS = {
    Language.EN: (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    Language.NL: (
        "januari", "februari", "maart", "april", "mei", "juni",
        "juli", "augustus", "september", "oktober", "november", "december",
    ),
}  # fmt: skip
_MONTHS_SHORT = {
    Language.EN: (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ),
    Language.NL: (
        "jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec",
    ),
}  # fmt: skip


def _weekday(day: dt.date | int) -> int:
    return day.weekday() if isinstance(day, dt.date) else day


def day_name(day: dt.date | int) -> str:
    """ "Monday" / "maandag" (`day`: a date, or 0 = Monday)."""
    return _DAYS[_current][_weekday(day)]


def day_short(day: dt.date | int) -> str:
    """ "Mon" / "ma"."""
    return _DAYS_SHORT[_current][_weekday(day)]


def month_name(month: dt.date | int) -> str:
    """ "October" / "oktober" (`month`: a date, or 1 = January)."""
    index = month.month if isinstance(month, dt.date) else month
    return _MONTHS[_current][index - 1]


def month_short(month: dt.date | int) -> str:
    """ "Oct" / "okt"."""
    index = month.month if isinstance(month, dt.date) else month
    return _MONTHS_SHORT[_current][index - 1]


def capitalized(text: str) -> str:
    """The first letter upper case ("maandag" → "Maandag"), the rest as it is."""
    return text[:1].upper() + text[1:]
