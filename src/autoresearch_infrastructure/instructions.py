"""Task instructions: one fixed layout, filled partly by the author and partly from the task.

The author writes three things: an objective, a list of rules, and notes about
the environment. ``render`` lays them out the same way for every task and adds
the facts the task already knows, such as the metric, its direction and the
starting files, so the text cannot disagree with what is measured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

from .api import Metric, Objective

_LAYOUT = Template(Path(__file__).with_name("instructions-layout.md").read_text())
_SECTIONS = ("objective", "rules", "environment")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Names given to unnamed items: the reviewer's criteria have always been rule_1, rule_2 and so on.
DEFAULT_PREFIX = {"rules": "rule_", "environment": "note_"}


def default_names(kind: str, count: int) -> tuple[str, ...]:
    return tuple(f"{DEFAULT_PREFIX[kind]}{number}" for number in range(1, count + 1))


def check_names(kind: str, names, count: int) -> tuple[str, ...]:
    """Names for ``count`` items of ``kind``: given ones checked, or the defaults when there are none."""
    names = tuple(names)
    if not names:
        return default_names(kind, count)
    if len(names) != count:
        raise ValueError(f"Give a name for every one of the {count} {kind} entries, or none")
    for name in names:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a usable name: use lowercase letters, digits and underscores")
    if len(set(names)) != len(names):
        raise ValueError(f"The {kind} names repeat: {sorted(n for n in names if names.count(n) > 1)}")
    return names


@dataclass(frozen=True)
class Instructions:
    """What a researcher is told about a task.

    ``objective``
        Prose: what to change and what should get better.
    ``rules``
        What a candidate must and must not do. The reviewer judges each one.
    ``environment``
        What the sandbox enforces or provides whatever the researcher does.
    ``title``
        An optional one-line name for the task.
    ``rule_names``, ``environment_names``
        Optional stable names, one for each rule or environment note, used by
        the reviewer's checks and by transforms. Without them the names are
        ``rule_1``, ``rule_2``, … and ``note_1``, ``note_2``, ….
    """

    objective: str
    rules: tuple[str, ...] = ()
    environment: tuple[str, ...] = ()
    title: str = ""
    rule_names: tuple[str, ...] = ()
    environment_names: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("Instructions need an objective: what to change and what should get better")
        for name in ("rules", "environment"):
            items = tuple(getattr(self, name))
            if any(not isinstance(item, str) or not item.strip() for item in items):
                raise ValueError(f"Each entry under {name} must be nonempty text")
            object.__setattr__(self, name, tuple(item.strip() for item in items))
        check_names("rules", self.rule_names, len(self.rules))
        check_names("environment", self.environment_names, len(self.environment))
        object.__setattr__(self, "rule_names", tuple(self.rule_names))
        object.__setattr__(self, "environment_names", tuple(self.environment_names))

    @classmethod
    def parse(cls, text: str) -> Instructions:
        """Read instructions written as Markdown.

        The text has ``## Objective`` and ``## Constraints``. Under Constraints,
        ``### Rules`` and ``### Environment`` each hold a bullet list. A leading
        ``# Title`` is optional. Any other heading at those levels is an error,
        so a misplaced section cannot be silently ignored.
        """
        title, section, blocks = "", None, {name: [] for name in _SECTIONS}
        for line in text.splitlines():
            heading = re.fullmatch(r"(#{1,3})\s+(.*\S)\s*", line)
            if heading and len(heading.group(1)) == 1 and section is None and not title:
                title = heading.group(2)
            elif heading and len(heading.group(1)) <= 3:
                name = heading.group(2).lower()
                if (len(heading.group(1)), name) == (2, "constraints"):
                    section = "constraints"
                elif (len(heading.group(1)), name) in ((2, "objective"), (3, "rules"), (3, "environment")):
                    section = name
                elif section == "objective" and len(heading.group(1)) == 3:
                    blocks["objective"].append(line)  # an Objective may have sub-headings of its own
                else:
                    raise ValueError(
                        f"Unexpected heading {line.strip()!r}. Instructions have '## Objective' and "
                        "'## Constraints' with '### Rules' and '### Environment'"
                    )
            elif section in blocks:
                blocks[section].append(line)
            elif line.strip():
                raise ValueError(f"Text outside a section: {line.strip()!r}. Start with '## Objective'")
        return cls(
            objective="\n".join(blocks["objective"]).strip(),
            rules=_bullets(blocks["rules"], "Rules"),
            environment=_bullets(blocks["environment"], "Environment"),
            title=title,
        )

    def render(self, facts: list[str] | tuple[str, ...] = ()) -> str:
        """The instructions in the standard layout, with ``facts`` listed under the objective."""

        def bullets(items, empty):
            return "\n".join(f"- {item}" for item in items) or empty

        return _LAYOUT.substitute(
            title=f"# {self.title}\n\n" if self.title else "",
            objective=self.objective,
            facts="\n\n### What is measured\n\n" + bullets(facts, "") if facts else "",
            rules=bullets(self.rules, "None."),
            environment=bullets(self.environment, "Nothing beyond the file layout below."),
        )


def _bullets(lines: list[str], section: str) -> tuple[str, ...]:
    items: list[str] = []
    for line in lines:
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif line.strip() and items and line.startswith(" "):
            items[-1] += " " + line.strip()
        elif line.strip():
            raise ValueError(f"{section} is a bullet list; found {line.strip()!r}")
    return tuple(items)


def task_facts(objective: Metric | Objective, seed_files: list[str], *, cases: int = 1, repeats: int = 1) -> list[str]:
    """What the task itself knows: the starting files, the score and how candidates are compared."""
    objective = objective if isinstance(objective, Objective) else Objective(objective)

    def better(direction):
        return "Higher is better" if direction == "max" else "Lower is better"

    shown = ", ".join(f"`{name}`" for name in seed_files[:12])
    if len(seed_files) > 12:
        shown += f" and {len(seed_files) - 12} more"
    score = objective.score.name if isinstance(objective.score, Metric) else objective.label
    facts = [
        f"Starting files: {shown}." if seed_files else "Starting files: none. The candidate starts empty.",
        f"Score: `{score}`. {better(objective.direction)}.",
    ]
    if objective.tie_breakers:
        ties = ", then ".join(f"`{m.name}` ({better(m.direction).lower()})" for m in objective.tie_breakers)
        facts.append(f"When scores tie: {ties}.")
    declared = objective.metrics or objective.referenced_metrics
    for metric in declared:
        bounds = [f"at least {metric.minimum}" if metric.minimum is not None else "",
                  f"at most {metric.maximum}" if metric.maximum is not None else ""]
        if any(bounds):
            facts.append(f"A candidate can be selected only if `{metric.name}` is {' and '.join(filter(None, bounds))}.")
    recorded = [m.name for m in declared if m not in objective.referenced_metrics]
    if recorded:
        facts.append("Also recorded, without affecting selection: " + ", ".join(f"`{name}`" for name in recorded) + ".")
    facts.append(f"Each candidate is measured on {cases} case(s), {repeats} time(s) each. "
                 "A crashed or unfinished evaluation produces no score.")
    return facts
