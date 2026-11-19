"""Split an imported task's instruction into ARI's parts, with a recorded mapping.

A split is a TOML file of ``[[part]]`` tables. Each part takes one span of the
source instruction, word for word, to one place:

``to = "objective"``
    The goal, the metric and the deliverable's interface. Objective parts are
    joined in the order the file lists them.
``to = "rule"``
    One constraint the candidate must obey, with a stable ``name``. Write it
    as a prohibition, so an empty starter passes review.
``to = "environment"``
    A fact that holds whatever the candidate does, with a stable ``name``.
``to = "dropped"``
    Text that ARI's loop replaces, such as a time limit or a submission tool,
    with the ``reason``.

Every part but a dropped one has a ``tag``:

``verbatim``
    ``source`` is used as it is.
``adapted``
    ``text`` replaces ``source``, and ``reason`` says why, such as "made a
    prohibition" or "heading demoted to fit under Objective".
``added``
    ``text`` is new, with no ``source``, and ``reason`` says why, such as "the
    importer's: the task states no rule against stored answers".

Coverage is checked: each ``source`` must occur exactly once in the instruction,
no two may overlap, and every non-blank character of the instruction must be in
one of them, except a list marker that starts a line: a bullet moved to Rules or
Environment becomes one of ARI's bullets. A task with a split fails to open otherwise, naming what is missing.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .instructions import Instructions, check_names

_DESTINATIONS = ("objective", "rule", "environment", "dropped")
_TAGS = ("verbatim", "adapted", "added")
_KEYS = {"to", "tag", "source", "text", "name", "reason", "enforced"}


@dataclass(frozen=True)
class Part:
    """One entry of a split: where a span of the source goes, and how its wording was kept."""

    to: str
    tag: str | None
    source: str | None
    text: str | None
    name: str | None
    reason: str | None
    enforced: str | None  # what besides the monitor enforces a rule, such as the grader

    @property
    def wording(self) -> str:
        return (self.source if self.tag == "verbatim" else self.text).strip()


def load(split: Path, instruction: Path, *, title: str = "") -> tuple[Instructions, dict]:
    """Read ``split`` against ``instruction`` and return the instructions and the mapping to record."""
    source = instruction.read_text(encoding="utf-8")
    table = tomllib.loads(split.read_text(encoding="utf-8"))
    if set(table) - {"part", "title"}:
        raise ValueError(f"{split} holds only [[part]] tables and an optional title")
    parts = [_part(entry, number) for number, entry in enumerate(table.get("part") or [], 1)]
    if not parts:
        raise ValueError(f"{split} has no [[part]] tables")
    positions = _check_coverage(parts, source, instruction)
    objective = _join_objective(parts, positions, source)
    for line in objective.splitlines():
        if re.match(r"#{1,2}\s", line):
            raise ValueError(f"The objective has the heading {line.strip()!r}. It is placed under ARI's "
                             "'## Objective', so adapt it to a '###' heading or lower")
    rules = [part for part in parts if part.to == "rule"]
    notes = [part for part in parts if part.to == "environment"]
    instructions = Instructions(
        objective=objective,
        rules=tuple(part.wording for part in rules),
        environment=tuple(part.wording for part in notes),
        title=table.get("title", title),
        rule_names=tuple(part.name for part in rules),
        environment_names=tuple(part.name for part in notes),
    )
    record = {
        "split": split.name,
        "split_sha256": hashlib.sha256(split.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "parts": [{key: value for key, value in asdict(part).items() if value is not None} for part in parts],
    }
    return instructions, record


def _part(entry, number: int) -> Part:
    label = f"part {number}"
    if not isinstance(entry, dict) or set(entry) - _KEYS:
        raise ValueError(f"{label}: a part has only the keys {sorted(_KEYS)}")
    values = {key: entry.get(key) for key in _KEYS}
    for key, value in values.items():
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{label}: {key} must be nonempty text")
    part = Part(**values)
    if part.to not in _DESTINATIONS:
        raise ValueError(f"{label}: to is one of {list(_DESTINATIONS)}")
    if part.to == "dropped":
        if part.tag is not None or part.text is not None or part.name is not None:
            raise ValueError(f"{label}: a dropped part has only source and reason")
        if part.source is None or part.reason is None:
            raise ValueError(f"{label}: a dropped part needs the source it drops and the reason")
        return part
    if part.tag not in _TAGS:
        raise ValueError(f"{label}: tag is one of {list(_TAGS)}")
    if part.tag == "verbatim" and (part.source is None or part.text is not None):
        raise ValueError(f"{label}: a verbatim part has a source and no text")
    if part.tag == "adapted" and (part.source is None or part.text is None or part.reason is None):
        raise ValueError(f"{label}: an adapted part has the source, the new text and the reason")
    if part.tag == "added" and (part.source is not None or part.text is None or part.reason is None):
        raise ValueError(f"{label}: an added part has new text and the reason, and no source")
    if part.to in ("rule", "environment"):
        if part.name is None:
            raise ValueError(f"{label}: a {part.to} needs a stable name")
        check_names("rules" if part.to == "rule" else "environment", [part.name], 1)
    elif part.name is not None:
        raise ValueError(f"{label}: an objective part has no name")
    if part.enforced is not None and part.to != "rule":
        raise ValueError(f"{label}: only a rule says what else enforces it")
    return part


def _join_objective(parts: list[Part], positions: dict, source: str) -> str:
    """The objective parts in order, keeping the list a moved bullet came from.

    A part that starts after a list marker keeps that marker, and parts on
    consecutive lines of the source stay on consecutive lines, so a list whose
    middle bullet half moved to Rules still renders as a list.
    """
    text, previous = "", None
    for number, part in enumerate(parts, 1):
        if part.to != "objective":
            continue
        wording = part.wording
        place = positions.get(number)
        if place is not None:
            line_start = source.rfind("\n", 0, place[0]) + 1
            lead = source[line_start:place[0]]
            if re.fullmatch(r"[ \t]*[-*+][ \t]+", lead) and not wording.lstrip().startswith(("-", "*", "+")):
                wording = lead.strip() + " " + wording
        # One line break apart in the source: whatever lies between went to another part.
        joined_by_line = (place is not None and previous is not None
                          and source.count("\n", previous[1], place[0]) == 1)
        text += ("" if not text else "\n" if joined_by_line else "\n\n") + wording
        previous = place
    return text


def _check_coverage(parts: list[Part], source: str, instruction: Path) -> dict:
    """Check that the parts cover the source once, and return where each sourced part sits in it."""
    covered = [None] * len(source)
    positions = {}
    for number, part in enumerate(parts, 1):
        if part.source is None:
            continue
        span = part.source.strip()
        found = [match.start() for match in re.finditer(re.escape(span), source)]
        if len(found) != 1:
            raise ValueError(f"part {number}: its source occurs {len(found)} times in {instruction.name}, not "
                             f"once: {_preview(span)}")
        start = found[0]
        positions[number] = (start, start + len(span))
        for index in range(start, start + len(span)):
            if covered[index] is not None:
                raise ValueError(f"part {number} overlaps part {covered[index]} at {_preview(source[index:])}")
            covered[index] = number
    # A list marker at the start of a line is structure, like whitespace: a bullet moved to Rules or
    # Environment is rendered as ARI's own bullet, so its source's "- " need not be assigned anywhere.
    structure = set()
    for marker in re.finditer(r"(?m)^[ \t]*([-*+])[ \t]", source):
        structure.add(marker.start(1))
    gaps, index = [], 0
    while index < len(source):
        if covered[index] is None and not source[index].isspace() and index not in structure:
            end = index
            while end < len(source) and covered[end] is None:
                end += 1
            line = source.count("\n", 0, index) + 1
            gaps.append(f"line {line}: {_preview(source[index:end])}")
            index = end
        else:
            index += 1
    if gaps:
        raise ValueError(f"{len(gaps)} span(s) of {instruction.name} are in no part. Give each a place, or "
                         "drop it with a reason: " + "; ".join(gaps[:4]) + ("; …" if len(gaps) > 4 else ""))
    names = {}
    for part in parts:
        if part.name is not None:
            if (part.to, part.name) in names:
                raise ValueError(f"Two {part.to} parts are named {part.name!r}")
            names[(part.to, part.name)] = True
    return positions


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return repr(text[:70] + ("…" if len(text) > 70 else ""))
