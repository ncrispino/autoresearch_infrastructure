"""Recorded changes to what a researcher is told, for studies that perturb a task's inputs.

Pass them as ``Task(transforms=[...])``. Each is applied in order to the
resolved ``ResearcherInputs``; the plan records every transformation, with its
arguments, and the original task text. Scoring never changes: ``omit_measured``
hides the "What is measured" list, it does not change what is measured.

Any function ``ResearcherInputs -> ResearcherInputs`` works as a transformation.
These cover the common cases and record their arguments in their identity.

A rule or an environment note is named by its stable name, such as
``"time_budget"``, or by its number counted from 1. Unnamed rules are
``rule_1``, ``rule_2``, … and unnamed notes ``note_1``, ``note_2``, …, as the
reviewer's checks are. Names stay with their text: omitting ``rule_2`` leaves
the next rule named ``rule_3``, so a later transform or a verdict still finds it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .inputs import ResearcherInputs
from .instructions import DEFAULT_PREFIX, check_names

_FIELDS = {"rules": ("rules", "rule_names"), "environment": ("environment_notes", "environment_names")}


class _Transform:
    @property
    def identity(self) -> dict:
        return {"transform": type(self).__name__, **asdict(self)}

    def _items(self, inputs: ResearcherInputs, kind: str) -> tuple[list[str], list[str]]:
        texts_field, names_field = _FIELDS[kind]
        texts = list(getattr(inputs, texts_field))
        return texts, list(check_names(kind, getattr(inputs, names_field), len(texts)))

    def _index(self, names: list[str], which, kind: str) -> int:
        noun = "rule" if kind == "rules" else "environment note"
        if type(which) is int:
            if not 1 <= which <= len(names):
                raise ValueError(f"{type(self).__name__}: there is no {noun} {which}")
            return which - 1
        if isinstance(which, str) and which in names:
            return names.index(which)
        raise ValueError(f"{type(self).__name__}: there is no {noun} {which!r}; the names are {names}")

    def _with(self, inputs: ResearcherInputs, kind: str, texts: list[str], names: list[str]) -> ResearcherInputs:
        texts_field, names_field = _FIELDS[kind]
        return replace(inputs, **{texts_field: tuple(texts), names_field: tuple(names)})

    def _new_name(self, names: list[str], kind: str, name: str | None) -> str:
        if name is None:  # the next default number that is free
            number = len(names) + 1
            while f"{DEFAULT_PREFIX[kind]}{number}" in names:
                number += 1
            return f"{DEFAULT_PREFIX[kind]}{number}"
        check_names(kind, [name], 1)
        if name in names:
            raise ValueError(f"{type(self).__name__}: the name {name!r} is taken")
        return name


@dataclass(frozen=True)
class omit_rule(_Transform):
    """Leave a rule, by name or number, out of what the researcher is told."""

    rule: int | str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        texts, names = self._items(inputs, "rules")
        index = self._index(names, self.rule, "rules")
        return self._with(inputs, "rules", texts[:index] + texts[index + 1:], names[:index] + names[index + 1:])


@dataclass(frozen=True)
class reword_rule(_Transform):
    """Replace the text of a rule, by name or number. It keeps its name."""

    rule: int | str
    text: str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        texts, names = self._items(inputs, "rules")
        texts[self._index(names, self.rule, "rules")] = _text(self.text)
        return self._with(inputs, "rules", texts, names)


@dataclass(frozen=True)
class add_rule(_Transform):
    """Add a rule at the end, under ``name`` or the next free ``rule_N``."""

    text: str
    name: str | None = None

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        texts, names = self._items(inputs, "rules")
        return self._with(inputs, "rules", texts + [_text(self.text)], names + [self._new_name(names, "rules", self.name)])


@dataclass(frozen=True)
class replace_goal(_Transform):
    """Replace the written objective."""

    text: str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        return replace(inputs, goal=_text(self.text))


@dataclass(frozen=True)
class add_environment_note(_Transform):
    """Add a line to the environment description, true or not, under ``name`` or the next free ``note_N``."""

    text: str
    name: str | None = None

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        texts, names = self._items(inputs, "environment")
        return self._with(inputs, "environment", texts + [_text(self.text)],
                          names + [self._new_name(names, "environment", self.name)])


@dataclass(frozen=True)
class omit_environment_note(_Transform):
    """Leave an environment note, by name or number, out of what the researcher is told."""

    note: int | str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        texts, names = self._items(inputs, "environment")
        index = self._index(names, self.note, "environment")
        return self._with(inputs, "environment", texts[:index] + texts[index + 1:], names[:index] + names[index + 1:])


@dataclass(frozen=True)
class reword_environment_note(_Transform):
    """Replace the text of an environment note, by name or number, such as blurring what grading allows."""

    note: int | str
    text: str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        texts, names = self._items(inputs, "environment")
        texts[self._index(names, self.note, "environment")] = _text(self.text)
        return self._with(inputs, "environment", texts, names)


@dataclass(frozen=True)
class omit_measured(_Transform):
    """Hide the generated "What is measured" list. Scoring is unchanged."""

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        return replace(inputs, measured=())


def _text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A transformation's text must be nonempty")
    return value.strip()
