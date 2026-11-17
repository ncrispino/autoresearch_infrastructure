"""The researcher's four inputs, resolved once per run, and how one start is shown them.

A task names what the researcher is given: an objective, rules, an environment
and a memory of the experiment. ``resolve`` turns a task into one
``ResearcherInputs`` value. A presenter turns those inputs, plus the facts of one
start, into the prompt that start reads. The default presenter produces the
standard layout.

Content, policy and presentation stay separate:

- ``ResearcherInputs`` holds the content: the written goal, what is measured,
  the rules, the environment and the memory policy.
- ``Memory`` and ``Environment`` hold policy: which records a start may see and
  what survives between starts.
- A presenter decides wording and order. A custom presenter is recorded by its
  code identity, and each start saves the exact prompt it produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .instructions import Instructions

_HISTORY = ("all", "none")
_CONVERSATION_MODES = ("files", "native")
_WORKSPACE = ("keep", "reset")


@dataclass(frozen=True)
class Memory:
    """What a researcher may see of the experiment's own records.

    ``history``
        ``"all"`` shows every earlier candidate (the default), ``"none"`` shows
        none, and a positive integer ``n`` shows the ``n`` most recently recorded.
    ``preload``
        A directory of earlier experiment records, such as another run's
        ``history`` directory or a curated set. It is shown read-only under
        ``/history/preloaded/``. Its contents are researcher input only: they
        are never treated as results, candidates or spent budget of this run.
    ``conversations``
        How many earlier researcher conversations of this run each start
        receives: ``0`` (the default) for none, a positive integer ``k`` for the
        conversations of the last ``k`` starts that saved one, or ``"all"``. The
        window slides in time order over every earlier start, including failed
        ones and ones whose candidate was not selected; a start that saved no
        conversation is skipped.
    ``conversation_mode``
        ``"files"`` mounts the selected conversations as ATIF JSON under
        ``/history/conversations/``. ``"native"`` loads the previous start's
        conversation into the agent through Harbor's ``load_trajectory``, so the
        start continues it; it needs ``conversations=1`` and an agent with ATIF
        loading support.
    """

    history: str | int = "all"
    preload: Path | str | None = None
    conversations: int | str = 0
    conversation_mode: str = "files"

    def __post_init__(self):
        if self.history not in _HISTORY and not (type(self.history) is int and self.history > 0):
            raise ValueError('Memory history is "all", "none" or a positive number of candidates')
        if self.conversations != "all" and not (type(self.conversations) is int and self.conversations >= 0):
            raise ValueError('Memory conversations is 0, a positive number of starts, or "all"')
        if self.conversation_mode not in _CONVERSATION_MODES:
            raise ValueError('Memory conversation_mode is "files" or "native"')
        if self.conversation_mode == "native" and self.conversations != 1:
            raise ValueError("Native conversation loading carries exactly the previous start: use conversations=1")
        if self.preload is not None and not isinstance(self.preload, (str, Path)):
            raise ValueError("Memory preload is a directory path")
        if isinstance(self.preload, str):
            object.__setattr__(self, "preload", Path(self.preload))

    @property
    def is_default(self) -> bool:
        """Whether a start sees exactly the canonical history, with nothing added."""
        return self.history == "all" and self.preload is None and self.conversations == 0

    def identity(self, preload_digest: str | None = None) -> dict:
        return {
            "history": self.history,
            "preload": preload_digest,
            "conversations": self.conversations,
            "conversation_mode": self.conversation_mode,
        }


@dataclass(frozen=True)
class Environment:
    """The researcher's runtime, beyond its starting files and context.

    ``harbor``
        A Harbor task directory whose ``task.toml`` ``[environment]`` table and
        ``environment/`` directory (a Dockerfile, and optionally a
        ``docker-compose.yaml`` for services) define the researcher's container.
        Only the environment is used: the Harbor task's instruction, tests and
        solution are ignored. Without it, the researcher runs in the ``--image``
        container.
    ``workspace``
        ``"keep"`` (the default) leaves files a researcher wrote under
        ``/workspace/`` outside ``candidate/`` for later starts. ``"reset"``
        removes them before every start, so only the parent candidate and the
        memory carry over.
    """

    harbor: Path | str | None = None
    workspace: str = "keep"

    def __post_init__(self):
        if self.workspace not in _WORKSPACE:
            raise ValueError('Environment workspace is "keep" or "reset"')
        if self.harbor is not None and not isinstance(self.harbor, (str, Path)):
            raise ValueError("Environment harbor is the path of a Harbor task directory")
        if isinstance(self.harbor, str):
            object.__setattr__(self, "harbor", Path(self.harbor))


@dataclass(frozen=True)
class ResearcherInputs:
    """Everything a researcher is given, resolved from a task.

    ``goal`` is the written objective. ``measured`` is what the task's metric
    definition says about scoring: the generated "What is measured" list. The
    two together are the objective a researcher reads; only ``measured`` is
    derived from what the evaluator and objective actually do.
    """

    goal: str
    measured: tuple[str, ...]
    rules: tuple[str, ...]
    environment_notes: tuple[str, ...]
    environment: Environment = field(default_factory=Environment)
    memory: Memory = field(default_factory=Memory)
    title: str = ""

    @classmethod
    def resolve(cls, instructions: Instructions, measured, *, environment=None, memory=None) -> ResearcherInputs:
        """The one path from a task's parts to its researcher inputs."""
        return cls(
            goal=instructions.objective,
            measured=tuple(measured),
            rules=instructions.rules,
            environment_notes=instructions.environment,
            environment=environment or Environment(),
            memory=memory or Memory(),
            title=instructions.title,
        )

    @property
    def instructions(self) -> Instructions:
        return Instructions(self.goal, self.rules, self.environment_notes, self.title)

    def specification(self) -> str:
        """The task text in the standard layout: also what the reviewer reads."""
        return self.instructions.render(self.measured)


@dataclass(frozen=True)
class Start:
    """The facts of one researcher start that a presenter may use.

    ``visible`` lists the candidates shown under ``/history/``; ``None`` means
    every candidate. ``preloaded`` says whether ``/history/preloaded/`` exists,
    and ``conversations`` names the earlier starts whose conversations are
    mounted or loaded.
    """

    number: int
    parent_id: str
    repeats: int
    visible: tuple[str, ...] | None = None
    preloaded: bool = False
    conversations: tuple[str, ...] = ()
    conversation_mode: str = "files"


Presenter = Callable[[ResearcherInputs, Start], str]


def present(inputs: ResearcherInputs, start: Start) -> str:
    """The standard presentation: the task text, then the files and how to submit."""
    from .search import researcher_prompt

    return researcher_prompt(inputs.specification(), parent_id=start.parent_id, repeats=start.repeats,
                             start=start)
