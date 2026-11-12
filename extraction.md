# How this package relates to the coevolution application

This package was extracted from
[the prompt-injection coevolution application](https://github.com/ncrispino/prompt-injection-coevolution/blob/main/src/coevolution/README.md).
The application still uses the package's shared parts. This page says what lives where, and why.

The rule is that mechanisms any experiment needs live here, and everything about
attacks, defenses and grading policy stays in the application. A new task then
inherits no assumptions from that application.

## What lives in this package

| Modules | What they do | Used by the application |
| --- | --- | --- |
| `persistence`, `source_tree`, `snapshots` | Atomic JSON writes, hashing, text-tree snapshots and diffs | Yes |
| `lifecycle` | Reserving, starting and recovering the evaluations of a candidate | Yes. `AutoresearchRunner` subclasses `EvaluationLifecycle`. |
| `validity` | Reading a verdict file and binding it to a candidate | Yes, with its own extra policy |
| `tool_server`, `mcp_proxy` | Serving `record_candidate` to an agent in a container | Yes |
| `viewer`, `traces`, `harbor` | The results page and ATIF trajectory handling | Yes, with its own page template |
| `programs`, `harnesses`, `providers`, `mcp_service`, `task_mcp`, `sandbox_exec` | Running a candidate solver program and coding-agent CLI sessions | Yes |
| `research`, `api`, `review`, `ledger`, `progress` | `ari.Research`, metrics, reviewer validity, the start ledger, the progress display | No |
| `task`, `launch`, `search`, `cli`, `coding_agent`, `harbor_filesystem` | `ari.Task`, run settings, the `ari` command and the researcher step | No |
| `harbor_task`, `harbor_harness`, `harbor_trial`, `harbor_agent`, `harbor_offline`, `harbor_build` | The two Harbor evaluators and the trial runner they share | No |
| `pilot`, `canary` | The benchmark harness study and its baseline-only calibration | No |

`ari.Research` and the application's runner are two controllers over the same
`lifecycle`, snapshot and verdict code. `Research` does not replace the
application's runner, which keeps its own state layout and candidate contract.

The solver image is built from [docker/solver](docker/solver/README.md) in this
package.

## What stays in the application

- Attacker, defender and environment roles, their goals and their write scopes.
- Attack and defense objectives, aggregation, fairness between tasks, and the
  rules for admitting a new environment.
- Domain seeds, prompts, policy and graders.
- The exact `record_candidate` schema it exposes, in `candidate-tool.json`, and
  the Docker mounts of its researchers.
- Extra verdict policy, such as the citations a novelty claim must carry. It
  passes this to the package's verdict reader.
- Provider selection. The package takes an explicit provider and has no policy
  for choosing one.
- Its own results page template and CLI.

## How the two are connected

- The package imports nothing from `coevolution`. It has no symlinks or paths
  that point back at the application.
- The application depends on the package as a local editable path, declared in
  `[tool.uv.sources]` of the root `pyproject.toml`. To move this directory to its
  own repository, install it from there and change that entry.
- The application hashes this package's source into its candidate contract and
  copies the source into its isolated candidate runtimes. A changed package
  therefore requires fresh evidence. Old evidence is not reinterpreted.

## Record of the extraction

The code was extracted from `ncrispino/prompt-injection-coevolution`, branch
`codex/environment-lifecycle-20261022`, commit `efca7e6` (Allow bounded Podman
worker cleanup). The extraction did not merge other branches or modify experiment
outputs.

The checks made at extraction time are fixture, reference and transport evidence.
None of them is a live model result.

- Fixture: the application's and the package's test suites passed. They cover
  recording, validity, metrics, snapshots, diffs, artifact tampering, budgets,
  reserving a candidate's evaluations, failed attempts, recovery and reuse of
  repeats, and the application's attack, defense, environment and harness
  boundaries. The package's tests ran on their own, with Harbor tests skipped
  when Harbor was absent.
- Fixture: a copy of this directory alone was built into an sdist and a wheel
  and installed into a fresh environment. The installed module imported under
  `python -I` with neither `coevolution` nor Harbor importable. The packing
  example ran from that copy and selected the feasible candidate with value 14.
- Fixture: Chromium opened the generated results page and checked candidate
  switching, comparison with the parent, metrics, an invalid candidate and
  metric bounds. The script is `tests/viewer-browser.cjs`.
- Fixture: the repository checks ran: frozen dependency synchronization, Ruff,
  the full pytest suite, `coevolution check`, `coevolution validate` and a fresh
  `coevolution offline` run. The offline run exercised attacker and
  common-defender sessions with 26 solver trial starts, and an environment
  proposal with its separate three clean trial starts.
- Transport: the solver program and CLI session checks used real disposable
  Docker containers and CLI processes with local fake providers.
- Reference: the Harbor reference controls used the upstream graders.

Live model results are recorded with each
[benchmark study](examples/task-bank/README.md).

## Limits carried over

- All evaluations of a candidate run from one controller process. The source
  branch tracked a follow-up to let long evaluations outlive the researcher
  process. That work is not done.
- Only the Codex and Claude Code harnesses are tested.
