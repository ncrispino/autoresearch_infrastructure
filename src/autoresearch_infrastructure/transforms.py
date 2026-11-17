"""Recorded changes to what a researcher is told, for studies that perturb a task's inputs.

Pass them as ``Task(transforms=[...])``. Each is applied in order to the
resolved ``ResearcherInputs``; the plan records every transformation, with its
arguments, and the original task text. Scoring never changes: ``omit_measured``
hides the "What is measured" list, it does not change what is measured.

Any function ``ResearcherInputs -> ResearcherInputs`` works as a transformation.
These cover the common cases and record their arguments in their identity.
Rules are numbered from 1, as the reviewer's ``rule_N`` names are.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .inputs import ResearcherInputs


class _Transform:
    @property
    def identity(self) -> dict:
        return {"transform": type(self).__name__, **asdict(self)}

    def _rule_index(self, inputs: ResearcherInputs, number: int) -> int:
        if type(number) is not int or not 1 <= number <= len(inputs.rules):
            raise ValueError(f"{type(self).__name__}: there is no rule {number}")
        return number - 1


@dataclass(frozen=True)
class omit_rule(_Transform):
    """Leave rule ``number`` out of what the researcher is told."""

    number: int

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        index = self._rule_index(inputs, self.number)
        return replace(inputs, rules=inputs.rules[:index] + inputs.rules[index + 1:])


@dataclass(frozen=True)
class reword_rule(_Transform):
    """Replace the text of rule ``number``."""

    number: int
    text: str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        index = self._rule_index(inputs, self.number)
        return replace(inputs, rules=inputs.rules[:index] + (_text(self.text),) + inputs.rules[index + 1:])


@dataclass(frozen=True)
class add_rule(_Transform):
    """Add a rule at the end."""

    text: str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        return replace(inputs, rules=inputs.rules + (_text(self.text),))


@dataclass(frozen=True)
class replace_goal(_Transform):
    """Replace the written objective."""

    text: str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        return replace(inputs, goal=_text(self.text))


@dataclass(frozen=True)
class add_environment_note(_Transform):
    """Add a line to the environment description, true or not."""

    text: str

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        return replace(inputs, environment_notes=inputs.environment_notes + (_text(self.text),))


@dataclass(frozen=True)
class omit_measured(_Transform):
    """Hide the generated "What is measured" list. Scoring is unchanged."""

    def __call__(self, inputs: ResearcherInputs) -> ResearcherInputs:
        return replace(inputs, measured=())


def _text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A transformation's text must be nonempty")
    return value.strip()
