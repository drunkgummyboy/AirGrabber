"""The rule editor's live check (app/forge/ui/rule_editor.py): what a rule would take from its
folder right now, and why it leaves the other files (docs/forge/2026-11-18-rules-design.md).

- `peek(folder, ignored)`: the folder's candidate videos (`watch.candidates`, the look's own
  reading: no temporary names, samples, extras or what the owner ignored) with their names
  parsed (`queue.parse_names`, no TMDB), read in a thread under the look's step limit
  (`auto.LOOK_STEP_TIMEOUT_S`); None when it can't be read (an OSError, past the limit), so a
  NAS asleep never holds the window.
- `explain(index, rules, settings, folder, files)`: the files counted per reason → CheckGroup
  (the rule at `index` counted as on; otherwise the rules in their order, as a look offers them:
  `watch_list`, `holds`, `fits`). It fits; a rule above takes it; or this rule's first test that
  fails (a folder inside, the kind, the quality) with the rule that takes it instead. Files in a
  destination (`rules.destinations`) aren't counted: a look never takes them.

Pure but for `peek`; no Tk. Logs nothing (a file name or folder never leaves here).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, NamedTuple, get_args

from app.forge.core.auto import LOOK_STEP_TIMEOUT_S
from app.forge.core.queue import parse_names
from app.forge.core.library import inside
from app.forge.core.rules import WatchedRule, destinations, fits, holds, is_4k, watch_list
from app.forge.core.watch import Ignored, candidates
from app.forge.models.media import MediaKind, ParsedMedia
from app.forge.models.rules import Rule
from app.forge.models.settings import ForgeSettings

type CheckReason = Literal[
    "fits", "taken_above", "in_subfolder", "a_show", "a_film", "not_4k", "is_4k"
]
_REASONS: tuple[str, ...] = get_args(CheckReason.__value__)
_KINDS: tuple[MediaKind, ...] = (MediaKind.EPISODE, MediaKind.MOVIE)


class CheckGroup(NamedTuple):
    """Files of one kind the check counts for one reason."""

    reason: CheckReason
    kind: MediaKind
    count: int  # type: ignore[assignment]  # shadows tuple.count, as the plan names it
    other: int | None  # the number (from 1) of the rule that takes them; None: no rule does


async def peek(
    folder: Path, ignored: Ignored | None, *, timeout_s: float = LOOK_STEP_TIMEOUT_S
) -> list[tuple[Path, ParsedMedia]] | None:
    """`folder`'s candidate videos with their parsed names; None when it can't be read within
    `timeout_s` (a thread past the limit goes on by itself, its answer dropped)."""
    try:
        files = await asyncio.wait_for(
            asyncio.to_thread(candidates, folder, ignored), timeout_s
        )
    except (OSError, TimeoutError):
        return None
    parsed = await parse_names([file.name for file in files])
    return list(zip(files, parsed, strict=True))


def _miss(rule: Rule, entry: WatchedRule, file: Path, parsed: ParsedMedia) -> CheckReason:
    """The first of `rule`'s tests `file` fails: a folder inside, the kind, then the quality."""
    if not holds(entry, file):
        return "in_subfolder"
    if parsed.kind not in rule.kinds():
        return "a_show" if parsed.kind is MediaKind.EPISODE else "a_film"
    return "not_4k" if rule.quality == "4k" and not is_4k(parsed) else "is_4k"


def explain(
    index: int,
    rules: Sequence[Rule],
    settings: ForgeSettings,
    folder: Path,
    files: Sequence[tuple[Path, ParsedMedia]],
) -> list[CheckGroup]:
    """Why each of `files` (in `folder`) does or doesn't go to the rule at `index` (counted as
    on, whatever its switch or refusals): per file the first rule that takes it, as a look
    offers them; groups ordered fits first, then by reason (CheckReason's order), kind and the
    other rule's number."""
    rule = rules[index].model_copy(update={"on": True})
    number = index + 1
    # A destination's files are never taken (a look doesn't walk there): not counted either.
    shown = [*rules[:index], rule, *rules[index + 1 :]]
    dests = destinations(shown, settings)
    within = [dest for dest in dests if inside(dest, folder)]  # a look never goes up
    files = [(f, p) for f, p in files if not any(inside(f, dest) for dest in within)]
    others = [entry for entry in watch_list(rules, settings) if entry.number != number]
    own = WatchedRule(rule, number, folder)
    above = [entry for entry in others if entry.number < number]
    below = [entry for entry in others if entry.number > number]
    entries = [*above, own, *below]
    counts: Counter[tuple[CheckReason, MediaKind, int | None]] = Counter()
    for file, parsed in files:
        taker = next((e for e in entries if holds(e, file) and fits(e.rule, parsed)), None)
        if taker is own:
            counts["fits", parsed.kind, None] += 1
        elif taker is not None and taker.number < number:
            counts["taken_above", parsed.kind, taker.number] += 1
        else:
            other = taker.number if taker is not None else None
            counts[_miss(rule, own, file, parsed), parsed.kind, other] += 1
    ordered = sorted(
        counts.items(),
        key=lambda item: (
            _REASONS.index(item[0][0]),
            _KINDS.index(item[0][1]),
            item[0][2] or 0,
        ),
    )
    return [CheckGroup(reason, kind, count, other) for (reason, kind, other), count in ordered]
