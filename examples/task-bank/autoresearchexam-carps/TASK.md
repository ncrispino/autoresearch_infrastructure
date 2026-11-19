# AutoResearchExam: low-discrepancy subset selection

This is a real benchmark task imported into ARI without editing it:
`carps-star-discrepancy-subset-select` from
[AutoResearchExam](https://github.com/bespokelabsai/AutoResearchExam) (Bespoke Labs),
at commit `23c0d92e11af1dea821f972b5f0a739060958002`. The researcher writes
`/app/output/select_subset.py`, which picks `k` of `n` points in the unit cube with
low star discrepancy. The task's own grader scores it.

The import was checked by grading alone, then with one short Codex researcher run.
See [What was observed](#what-was-observed).

## Fetch the task

The benchmark's files are not stored here. Copy the task directory in unchanged:

```bash
git clone https://github.com/bespokelabsai/AutoResearchExam /tmp/are
```

```bash
git -C /tmp/are checkout 23c0d92e11af1dea821f972b5f0a739060958002
```

```bash
cp -r /tmp/are/carps-star-discrepancy-subset-select examples/task-bank/autoresearchexam-carps/harbor-task
```

```bash
ari extract-seed examples/task-bank/autoresearchexam-carps/harbor-task --deliverable /app/output --output examples/task-bank/autoresearchexam-carps/seed
```

The last command builds the task's image and copies out what it ships at
`/app/output`. For this task that is nothing, so `seed/` is empty and the baseline
is "no submission".

## How the source maps to ARI

| Source | In `task.py` | Why |
| --- | --- | --- |
| `/app/output`, named by `instruction.md` | `deliverable="/app/output"` | The researcher sees the candidate there and nowhere else, so the task's text is true as written. `/workspace` is its scratch directory. |
| `environment/are-grader-ctx/Dockerfile` | `grader="environment/are-grader-ctx"` | The grader's own image, with both hidden splits in its `/tests`. ARI builds it once and leaves the directory out of the agents' container. |
| `/tests/intermediate.sh` in that image | `feedback` | Scores the `intermediate` split. The researcher sees this score. |
| `/tests/test.sh` in that image | `sealed` | Scores the `final` split. Recorded privately. |
| The grader runs each call as uid 1001 with `runuser` | ARI's default: Docker's default capabilities | The benchmark's grader runs as root with them, so `task.py` names none. |
| `cap_add: SYS_ADMIN` in the compose file | Not needed | Only the benchmark's submit server uses it, for `unshare -n`. ARI's grader container has no network. |
| `docker-compose.yaml`, the `are` MCP server, `tests/finalize.py` | Not used | They are the benchmark's own submit loop, which `record_candidate` replaces. `tests/test.sh` only reads that loop's ledger. |
| `[agent]` network: an allowlist of `api.openai.com`, `api.anthropic.com` and `grader` | Kept, without `grader` | ARI applies it while the agents work, as Harbor does. `grader` is the benchmark's compose service, which is not started. The instructions say what the network reaches. |
| `instruction.md` | `split="instruction-split.toml"` | Each span goes to the objective, a named rule, a named environment note, or is dropped with a reason. See [How instruction.md is split](#how-instructionmd-is-split). |
| `[agent] timeout_sec = 86400`, many submissions | `ari.Memory(conversation_mode="continue", conversations="all")`; the researcher's timeout defaults to the task's 86400 seconds | One researcher session for 24 hours, as the benchmark runs an agent. `override_timeout_sec` in `run.toml` replaces it. |
| `hints/hint.md` | Not used | The benchmark keeps hints apart from the instruction. |

## How instruction.md is split

[instruction-split.toml](instruction-split.toml) divides the benchmark's
`instruction.md` into ARI's parts. `ari check` and every run refuse the task if a
span of the source is in no part, is in two, or no longer occurs exactly once, so
an upstream edit to the instruction cannot slip through unnoticed. The whole
mapping, with the hashes of the split and the source, is recorded in the run's
`plan.json` under `inputs.instruction_split`.

Each part is tagged:

- **verbatim**: the source's own words.
- **adapted**: new words for a source span, with the reason.
- **added**: new words with no source, with the reason. Here only `no_lookup`.

A rule's last column says what enforces it besides the reviewer. A rule the grader
already enforces is still listed, so the researcher knows of it before a submission
fails. A rule only the reviewer can judge says so.

| # | Goes to | Tag | Name | Source in `instruction.md` | Text the researcher sees instead | Reason | What enforces it besides the reviewer |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | dropped |  |  | # Low-discrepancy subset selection | (dropped) | The title; ARI shows it as the task's title, set in this file. |  |
| 2 | objective | verbatim |  | You get a cloud of `n` points in the unit cube `[0,1]^3` and a subset … | (the source, unchanged) |  |  |
| 3 | objective | adapted |  | ## Submission format | ### Submission format | Heading demoted to fit under ARI's Objective section. |  |
| 4 | objective | verbatim |  | Write `/app/output/select_subset.py`, defining exactly one entry point … | (the source, unchanged) |  |  |
| 5 | objective | verbatim |  | `seed`: int. | (the source, unchanged) |  |  |
| 6 | rule | adapted | `seeded` | Use it for any randomness so a call is reproducible. | Do not use randomness that `seed` does not determine: the same call must return the same subset. | Made a prohibition, so the empty starter passes review. | Nothing besides the reviewer: the grader does not rerun a call. |
| 7 | objective | verbatim |  | `time_budget_s`: float, `10.0` at grade time. | (the source, unchanged) |  |  |
| 8 | rule | adapted | `time_budget` | `select` must return within it. | Do not let `select` run past `time_budget_s`. | Made a prohibition, so the empty starter passes review. | The grader kills a call `time_budget_s + 5` seconds after it starts and scores that cloud as no subset. |
| 9 | objective | verbatim |  | - Return value: any iterable of exactly `k` distinct integer indices i … | (the source, unchanged) |  |  |
| 10 | dropped |  |  | ## Data notes | (dropped) | Section heading; its lines are environment notes. |  |
| 11 | environment | verbatim | `example_clouds` | Example clouds live in `/app/data/examples/`. | (the source, unchanged) |  |  |
| 12 | environment | verbatim | `manifest` | `/app/data/examples/manifest.json` lists each cloud's file name, `n`, … | (the source, unchanged) |  |  |
| 13 | environment | verbatim | `sealed_clouds` | Grading uses sealed clouds drawn the same way; your code sees them for … | (the source, unchanged) |  |  |
| 14 | objective | adapted |  | ## Metric and scoring | ### Metric and scoring | Heading demoted to fit under ARI's Objective section. |  |
| 15 | objective | verbatim |  | - `select` is called once per (cloud, seed) pair. - You're scored on t … | (the source, unchanged) |  |  |
| 16 | dropped |  |  | ## Execution environment | (dropped) | Section heading; its lines are environment notes and rules. |  |
| 17 | environment | verbatim | `graded_separately` | Grading runs in a separate environment from this one, and only … | (the source, unchanged) |  |  |
| 18 | rule | adapted | `self_contained` | So your solution must be fully self-contained there: if `select_subset.py` imports a helper module you wrote, that module must also live under `/app/output`. | Do not make the solution depend on files you wrote outside `/app/output`: a helper module it imports must be there too. | Made a prohibition, about the files you write, not the installed packages. | Grading: only `/app/output` carries over, so anything else is missing there. |
| 19 | environment | verbatim | `fresh_process` | Each call runs in a fresh process pinned to a single CPU. | (the source, unchanged) |  |  |
| 20 | environment | verbatim | `read_only_output` | `/app/output` is mounted read-only at grade time. A scratch working di … | (the source, unchanged) |  |  |
| 21 | environment | verbatim | `kill_deadline` | The process is SIGKILLed `time_budget_s + 5` seconds after it starts. … | (the source, unchanged) |  |  |
| 22 | environment | verbatim | `packages` | Available packages: the Python standard library, numpy, and scipy. No … | (the source, unchanged) |  |  |
| 23 | dropped |  |  | `/app/output` is writable now; everything else is read-only. | (dropped) | Not true in ARI's container, where `/workspace` and `/tmp` are writable too; ARI's ari_container note states the writable paths. |  |
| 24 | rule | adapted | `no_links` | Everything under `/app/output` must be a regular file. A symbolic or hard link anywhere in that tree makes the whole submission unscoreable. | Do not put a symbolic or hard link anywhere under `/app/output`: everything there must be a regular file. | Two sentences made one prohibition. | The grader refuses the whole submission, and ARI does not save links in a candidate. |
| 25 | objective | adapted |  | ## Invalid submissions | ### Invalid submissions | Heading demoted to fit under ARI's Objective section. |  |
| 26 | objective | verbatim |  | - If a call is killed, raises, or returns anything other than exactly … | (the source, unchanged) |  |  |
| 27 | rule | added | `no_lookup` | (none) | Do not store subsets, indices or results chosen for particular clouds, or return them in place of a subset computed from the points `select` is given. | The importer's: the task states no rule against stored answers, and only a reviewer can judge it. | Nothing besides the reviewer: graded clouds are sealed, so a stored answer for them cannot exist, but one for the example clouds could. |

Long sources are cut short in this table; the TOML has them in full. To see the
same mapping as a diff, with every source in full, run
`ari instructions examples/task-bank/autoresearchexam-carps`; add `--changes` to
leave out the verbatim parts, or `--rendered` to print the whole prompt text.

ARI adds its own environment notes after the task's, also by name, so a study can
omit or reword any of them like the task's own: `ari_submission`, `ari_grader`,
`ari_network`, `ari_container` and `ari_budget`. A split may not reuse those names.

A study that perturbs the inputs names what it changes:

```python
transforms=[ari.transforms.omit_rule("no_lookup"),
            ari.transforms.reword_environment_note("packages", "Some scientific Python packages are installed.")]
```

The names stay with their text, so omitting one rule does not rename the others,
and the reviewer's verdicts are keyed by the same names.

## What the researcher reads

The instructions as `ari instructions examples/task-bank/autoresearchexam-carps --rendered`
prints them with `run.toml`. ARI's file layout and submission protocol follow them in the
prompt.

<details>
<summary>The full instructions</summary>

````markdown
# Low-discrepancy subset selection

## Objective

You get a cloud of `n` points in the unit cube `[0,1]^3` and a subset size `k`. Return the `k`
points that fill the cube most evenly, in the L-infinity star discrepancy sense.

For a set `P` of `k` points, the star discrepancy is:

$$d^*(P) = \sup_{q \in [0,1]^3} \left| \frac{\#\{p \in P : p < q\}}{k} - q_1 q_2 q_3 \right|$$

This is the worst gap, over every box `[0,q)` anchored at the origin, between the fraction of `P`
inside the box and the box's volume. Lower is better.

### Submission format

Write `/app/output/select_subset.py`, defining exactly one entry point:

```python
select(points, k, seed, time_budget_s) -> sequence of k ints
```

- `points`: C-contiguous float64 array, shape `(n, 3)`, values in `[0, 1]`, `800 <= n <= 4000`.
- `k`: int, one of `30`, `50`, `65`; always `k < n`.
- `seed`: int.
- `time_budget_s`: float, `10.0` at grade time.
- Return value: any iterable of exactly `k` distinct integer indices into `points`, each in
  `[0, n)`. Values can be Python ints, numpy integers, or floats with an integral value.

### Metric and scoring

- `select` is called once per (cloud, seed) pair.
- You're scored on the mean exact `d*` of the returned subsets across the whole panel.
- A subset no better than an unguided, uniformly random `k`-subset scores zero.
- Lower discrepancy always scores higher, with no cutoff where further improvement stops
  counting.

### Invalid submissions

- If a call is killed, raises, or returns anything other than exactly `k` distinct in-range
  integral indices, that cloud is scored as if no subset had been selected, the worst credit
  available.
- If your module fails to import, you score zero overall.

### What is measured

- Starting files: none. The candidate starts empty.
- Score: `reward`. Higher is better.
- Each candidate is measured on 1 case(s), 1 time(s) each. A crashed or unfinished evaluation produces no score.

## Constraints

### Rules

Your candidate must obey each of these.

- Do not use randomness that `seed` does not determine: the same call must return the same subset.
- Do not let `select` run past `time_budget_s`.
- Do not make the solution depend on files you wrote outside `/app/output`: a helper module it imports must be there too.
- Do not put a symbolic or hard link anywhere under `/app/output`: everything there must be a regular file.
- Do not store subsets, indices or results chosen for particular clouds, or return them in place of a subset computed from the points `select` is given.

### Environment

Facts about where you work and how grading runs. Nothing you do changes them.

- Example clouds live in `/app/data/examples/`.
- `/app/data/examples/manifest.json` lists each cloud's file name, `n`, and `k`.
- Grading uses sealed clouds drawn the same way; your code sees them for the first time at grade
  time.
- Grading runs in a separate environment from this one, and only `/app/output` carries over.
- Each call runs in a fresh process pinned to a single CPU.
- `/app/output` is mounted read-only at grade time. A scratch working directory is provided and
  deleted after each call, so nothing written during one call is visible to any other.
- The process is SIGKILLed `time_budget_s + 5` seconds after it starts. Interpreter startup and
  importing your module count against that extra 5 seconds.
- Available packages: the Python standard library, numpy, and scipy. No network access.
- Submit each candidate with the `record_candidate` tool, described below. The time and the number of proposals this run allows are given in the last note.
- The grader runs in a separate container that you cannot reach. Its score is returned to you after each submission.
- While you work, this container's network reaches only: `api.openai.com`, `api.anthropic.com`.
- You work in the task's main container, so its other paths are as the text above says. Only `/app/output`, `/workspace` and `/tmp` are writable.
- You have up to 24 hours in this session. A session started after this one ends gets the same time again. Across the whole run, up to 40 proposals can be submitted and scored.
````

</details>

## What was observed

Grading only, on a 4-CPU Linux machine, with `local.toml`. No agent, no model.

| Candidate | Feedback (`intermediate`) | Sealed (`final`) | Grader calls valid |
| --- | --- | --- | --- |
| `candidate-0001`: the empty starter | 0 | 0 | none to run |
| `candidate-0002`: [reference-solution.py](reference-solution.py) | 0.616 | 0.665 | 45 of 45 on each split |

The empty starter scores 0 because every call falls back to the random baseline.
The reference solution is a short heuristic written for this check: candidates
nearest a scrambled Sobol set, then single-point swaps while the exact
discrepancy falls, for half the time budget. Each candidate took about 2 minutes
20 seconds to grade, for the two scripts together. The sealed score appears in no
public file of the run.

Two differences from a native run apply to these numbers:

- **CPUs.** The task asks for 8 and the machine had 4, so the run settings said
  `cpus = 4` under `[task]`; without it ARI refuses to grade on fewer CPUs than the task
  asks for. This grader runs as many calls at a time as it has CPUs, each with
  its own 10-second budget, so fewer CPUs make grading slower here rather than
  changing the score.
- **Images.** In the sandbox where this was checked, `docker build` could only
  reach package mirrors through a TLS-inspecting proxy, so both images were built
  outside ARI with the proxy's CA mounted as a build secret, then given as
  `[task] grader_image` and `[task] image`. Their Dockerfiles were otherwise
  unchanged. On an ordinary machine, leave those out and ARI builds them.

### A researcher run

One live run with Codex 0.170.0 and `gpt-6-luna` as researcher and reviewer, on
the baseline plus two proposals, with review on. The agents ran in the task's own
image with Node, ripgrep and the Codex CLI added (the `[task] image`), under the
task's `[agent]` network allowlist; Codex called the model from inside the
container. The sandbox where this ran also needed its TLS proxy's CA in that
image; an ordinary machine does not.

| Candidate | Parent | Review | Feedback (`intermediate`) | Sealed (`final`) |
| --- | --- | --- | --- | --- |
| `candidate-0001`: the empty starter | none | valid | 0 | 0 |
| `candidate-0002`: greedy balancing of anchored boxes on a 16³ grid, then exchanges | `candidate-0001` | valid | 0.525 | 0.560 |
| `candidate-0003` | `candidate-0002` | valid | 0.586 | 0.643 |

The run took about 6½ minutes and reported $0.027 of model cost. The sealed score
rose with the feedback score, so the selected candidate did not overfit the split
the researcher was shown. It used two researcher starts, not one continuing
session: after the first submission the reply said to submit again, but also
carried a null remaining count, and Codex took that as nothing left. That reply
has since been made explicit.

Getting here found three problems, all fixed in ARI:

- The reviewer's container asked Docker for the task's 8 CPUs on a 4-CPU machine.
  `[task] cpus` now applies to the agents' container as well as the grader.
- An empty starter could never pass review: a pass had to cite a file, and the
  empty candidate had none. A pass may now cite the candidate directory itself
  when it holds no files, for a rule it cannot break.
- The importer's first rules were written as requirements, which an empty
  starter fails. They are now prohibitions; what the deliverable must contain is
  the grader's to score.

## Not yet checked

- **A longer run.** Two proposals show the loop works; they say nothing about how
  far a researcher can take this task in the benchmark's 24 hours.
- **Codex with another provider.** A provider other than OpenAI or Anthropic needs
  its host under `[task] allowed_hosts`, which no run has used yet.
