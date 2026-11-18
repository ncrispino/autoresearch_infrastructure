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
_CONVERSATION_MODES = ("files", "continue")
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
        How much conversation carries over: ``0`` (the default) for none, a
        positive integer ``k``, or ``"all"``. What ``k`` counts depends on
        ``conversation_mode``.
    ``conversation_mode``
        ``"files"`` (the default): every start is a fresh conversation that can
        read the conversations of the last ``k`` starts that saved one, as ATIF
        JSON under ``/history/conversations/``. Starts are counted in time
        order, including failed ones and ones whose candidate was not selected.

        ``"continue"``: one researcher session makes up to ``k`` proposals in
        a row, so it keeps its own conversation: it submits, reads the result,
        and proposes again. ``"all"`` sets no limit of its own. After each
        submission ARI puts a fresh copy of the selected candidate in the
        workspace and updates ``/history``. The session ends early when a
        budget runs out, and the next start is a fresh conversation. It works
        with any agent, needs ``k`` of at least 2, and counts one researcher
        start for the whole session, so give the agent a time limit that
        covers ``k`` rounds of review and evaluation.
    ``log``
        ``True`` adds ``/history/log.md``, a research log ARI writes from its
        own records before each start: every visible candidate's parent,
        hypothesis, mechanism, verdict and score, oldest first.
    ``summarize``
        A function ``(index) -> str`` given the visible ``/history/index.json``
        content. Its text is shown as ``/history/summary.md``. It is trusted
        code, recorded by its identity, and its output is saved with each start.
    """

    history: str | int = "all"
    preload: Path | str | None = None
    conversations: int | str = 0
    conversation_mode: str = "files"
    log: bool = False
    summarize: Callable | None = None

    def __post_init__(self):
        if self.history not in _HISTORY and not (type(self.history) is int and self.history > 0):
            raise ValueError('Memory history is "all", "none" or a positive number of candidates')
        if self.conversations != "all" and not (type(self.conversations) is int and self.conversations >= 0):
            raise ValueError('Memory conversations is 0, a positive number of starts, or "all"')
        if self.conversation_mode not in _CONVERSATION_MODES:
            raise ValueError('Memory conversation_mode is "files" or "continue"')
        if self.conversation_mode == "continue" and self.conversations in (0, 1):
            raise ValueError('A session continues for 2 or more proposals, or "all": set conversations')
        if type(self.log) is not bool:
            raise ValueError("Memory log is True or False")
        if self.summarize is not None and not callable(self.summarize):
            raise ValueError("Memory summarize is a function of the history index")
        if self.preload is not None and not isinstance(self.preload, (str, Path)):
            raise ValueError("Memory preload is a directory path")
        if isinstance(self.preload, str):
            object.__setattr__(self, "preload", Path(self.preload))

    def identity(self, preload_digest: str | None = None, summarize: str | None = None) -> dict:
        return {
            "history": self.history,
            "preload": preload_digest,
            "conversations": self.conversations,
            "conversation_mode": self.conversation_mode,
            "log": self.log,
            "summarize": summarize,
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
        What survives a start besides the candidate and the memory. Containers,
        services and their volumes never do: each start's environment is torn
        down when it ends. With ``"keep"`` (the default), ARI exports the files
        a researcher left under ``/workspace/`` outside ``candidate/`` after
        each start, seals them, and restores exactly that export before the
        next start. With ``"reset"``, nothing is exported and every start
        begins with only the parent candidate.
    ``candidate_at``
        A second container path at which the researcher sees the candidate
        directory, such as ``/app/output``. It is the same directory as
        ``/workspace/candidate/``, not a copy. Use it when the files are
        written to run from a fixed place, so that a relative path such as
        ``../data`` means the same to the researcher as it does to the grader.
    """

    harbor: Path | str | None = None
    workspace: str = "keep"
    candidate_at: str | None = None

    def __post_init__(self):
        if self.workspace not in _WORKSPACE:
            raise ValueError('Environment workspace is "keep" or "reset"')
        if self.candidate_at is not None:
            from .harbor_filesystem import candidate_mount

            candidate_mount(self.candidate_at)
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

    def specification(self) -> str:
        """The task text in the standard layout: also what the reviewer reads."""
        return Instructions(self.goal, self.rules, self.environment_notes, self.title).render(self.measured)


@dataclass(frozen=True)
class Start:
    """The facts of one researcher start that a presenter may use.

    ``visible`` lists the candidates shown under ``/history/``; ``None`` means
    every candidate. ``preloaded`` says whether ``/history/preloaded/`` exists,
    and ``conversations`` names the earlier starts whose conversations are
    mounted as files. ``submissions`` is how many candidates this session may
    record: one, a larger number, or ``None`` for no limit of its own.
    ``context`` says whether the task has context files, mounted at ``/context``.
    ``candidate_at`` is a second path at which the candidate directory is mounted.
    """

    number: int
    parent_id: str
    repeats: int
    visible: tuple[str, ...] | None = None
    preloaded: bool = False
    conversations: tuple[str, ...] = ()
    log: bool = False
    summary: bool = False
    submissions: int | None = 1
    context: bool = True
    candidate_at: str | None = None


def present(inputs: ResearcherInputs, start: Start) -> str:
    """The standard presentation: the task text, then the files and how to submit."""
    from .search import researcher_prompt

    return researcher_prompt(inputs.specification(), parent_id=start.parent_id, repeats=start.repeats,
                             start=start)
