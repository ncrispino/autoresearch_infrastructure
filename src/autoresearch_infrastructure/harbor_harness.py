"""Evaluate an edited solver harness on fixed Harbor tasks.

The candidate is a solver program: ``program.toml``, ``solve.py`` and any prompts,
skills or hooks. The Harbor tasks, their graders, the model and the limits stay fixed.
"""

from __future__ import annotations

from pathlib import Path
from string import Template
from uuid import uuid4

from .api import Metric
from .harbor_filesystem import immutable_image
from .harbor_trial import OFFLINE_ENVIRONMENT, SOLVER_TOKENS, reward_mapping, run_trial
from .ledger import StartLedger
from .providers import Provider, provider_for
from .source_tree import evidence_digest

_RESEARCHER_GOAL = Template(Path(__file__).with_name("harness-researcher.md").read_text())

# The rules every harness candidate must follow. The reviewer judges them.
HARNESS_RULES = {
    "scope": "Edit only the shared solver code, prompts and skills. Keep task definitions, scoring checks, model/provider settings and budgets unchanged.",
    "generality": "Use the instructions, files and tools available to the solver during a task. Do not hard-code answers, add exceptions for specific test cases, look up reference answers, inspect hidden tests or alter score files. Adapting the approach to the kind of task is allowed.",
    "accounting": "All model inference uses runtime.run_harness. No direct provider calls, native delegation, external dependencies or unaccounted helper/solver launches.",
}


def researcher_goal(*, sessions: int, advice: str = "") -> str:
    """Instructions for improving a solver harness, as Markdown. ``advice`` closes the objective."""
    advice = advice.strip()
    return _RESEARCHER_GOAL.substitute(sessions=sessions, advice=f"\n{advice}\n" if advice else "")


def write_researcher_context(directory: Path, harness: str, rules: dict[str, str]) -> Path:
    """Write a harness researcher's reference files: the solver interface, examples and rules."""
    import json

    from .harnesses.settings import write_guide
    from .programs import api, parsing, starter

    directory.mkdir(parents=True, exist_ok=True)
    for source in (api, parsing, starter):
        (directory / Path(source.__file__).name).write_text(Path(source.__file__).read_text())
    (directory / "usage.md").write_text(Path(starter.__file__).with_name("usage.md").read_text())
    (directory / "policy.md").write_text(json.dumps(rules, indent=2))
    write_guide(harness, directory)
    return directory


class HarborHarnessEvaluator:
    """Run a candidate solver program on each fixed Harbor task.

    Each evaluation costs one ``solver_trial`` start and up to
    ``max_harness_sessions`` ``solver_session`` starts from ``ledger``.
    """

    def __init__(
        self,
        *,
        tasks: dict[str, Path],
        solver_image: str,
        model: str,
        ledger: StartLedger,
        harness: str = "codex",
        provider: Provider | None = None,
        timeout_seconds=120,
        max_harness_sessions=2,
        compose_overlays: dict[str, Path] | None = None,
        include_solver_tokens: bool = False,
        metrics: dict[Metric, str] | None = None,
    ):
        immutable_image(solver_image, "Harbor harness evaluation")
        if not tasks:
            raise ValueError("A frozen Harbor task mapping is required")
        if not isinstance(ledger, StartLedger) or not {"solver_trial", "solver_session"} <= ledger.limits.keys():
            raise ValueError("A StartLedger with solver_trial and solver_session ceilings is required")
        if type(max_harness_sessions) is not int or not 1 <= max_harness_sessions <= 128:
            raise ValueError("Sessions per trial must be between 1 and 128")
        self.metrics = reward_mapping(metrics)
        if self.metrics and include_solver_tokens and any(m.name == SOLVER_TOKENS.name for m in self.metrics):
            raise ValueError("solver_tokens is already supplied by native usage")
        self.include_solver_tokens = include_solver_tokens
        self.compose_overlays = compose_overlays or {}
        self.tasks = {name: Path(path) for name, path in tasks.items()}
        self.task_hashes = {
            name: evidence_digest(path, max_total_bytes=100_000_000) for name, path in self.tasks.items()
        }
        self.solver_image = solver_image
        self.model = model
        self.harness = harness
        self.provider = provider or provider_for(harness, "openrouter")
        provider_for(harness, self.provider.name)
        self.ledger = ledger
        self.timeout_seconds = timeout_seconds
        self.max_harness_sessions = max_harness_sessions

    @property
    def identity(self):
        return {
            "task_hashes": self.task_hashes,
            "include_solver_tokens": self.include_solver_tokens,
            "image": self.solver_image,
            "model": self.model,
            "harness": self.harness,
            "provider": self.provider.metadata(),
            "credential_variable": self.provider.key_variable,
            "timeout": self.timeout_seconds,
            "sessions_per_trial": self.max_harness_sessions,
            "build_overlays": {k: evidence_digest(p.parent) for k, p in self.compose_overlays.items()},
            "allocation": self.ledger.identity_hash,
            "limits": self.ledger.limits,
            "metrics": None if self.metrics is None else {m.name: key for m, key in self.metrics.items()},
        }

    def reserve_evaluations(self, count):
        self.ledger.require_capacity({
            "solver_trial": count, "solver_session": count * self.max_harness_sessions,
        })

    def __call__(self, candidate, context):
        from harbor.models.task.config import TaskConfig as TaskDefinition
        from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig

        from .programs.package import program_manifest

        if context.task not in self.tasks:
            raise ValueError("Task is outside the frozen benchmark panel")
        task = self.tasks[context.task]
        if evidence_digest(task, max_total_bytes=100_000_000) != self.task_hashes[context.task]:
            raise ValueError("Benchmark task bytes changed")
        definition = TaskDefinition.model_validate_toml((task / "task.toml").read_text())
        offline = definition.environment.resolve_baseline().network_mode == "no-network"
        overlay = self.compose_overlays.get(context.task)
        config = TrialConfig(
            task=TaskConfig(path=task),
            trial_name="evaluation-" + uuid4().hex[:12],
            trials_dir=context.artifacts / "harbor",
            environment=EnvironmentConfig(
                # A no-network task keeps that policy even where the host cannot
                # enforce it dynamically.
                import_path=OFFLINE_ENVIRONMENT if offline else None,
                extra_docker_compose=[overlay] if overlay else [],
            ),
            agent=AgentConfig(
                import_path="autoresearch_infrastructure.harbor_agent:HarnessProgramAgent",
                model_name=self.model,
                override_timeout_sec=self.timeout_seconds + 120,
                kwargs={
                    "program_path": str(candidate),
                    "program_id": program_manifest(candidate)["id"],
                    "solver_image": self.solver_image,
                    "harness": self.harness,
                    "provider_name": self.provider.name,
                    "provider_base_url": self.provider.base_url,
                    "provider_key_variable": self.provider.key_variable,
                    "timeout_seconds": self.timeout_seconds,
                    "max_harness_sessions": self.max_harness_sessions,
                    "ledger_path": str(self.ledger.path),
                    "ledger_limits": self.ledger.limits,
                    "ledger_identity": self.ledger.identity_hash,
                },
            ),
        )
        return run_trial(
            config, context.artifacts, self.ledger, mapping=self.metrics,
            extra_metrics=self._token_metric if self.include_solver_tokens else None,
            metadata={"task_hash": self.task_hashes[context.task], "fidelity": context.condition},
        )

    @staticmethod
    def _token_metric(result) -> dict[Metric, float]:
        counts = [getattr(result.agent_result, key, None) for key in ("n_input_tokens", "n_output_tokens")]
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("Native token usage is missing; cannot compare efficiency")
        return {SOLVER_TOKENS: float(sum(counts))}
