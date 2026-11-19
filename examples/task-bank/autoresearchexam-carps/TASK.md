# AutoResearchExam: low-discrepancy subset selection

This is a real benchmark task imported into ARI without editing it:
`carps-star-discrepancy-subset-select` from
[AutoResearchExam](https://github.com/bespokelabsai/AutoResearchExam) (Bespoke Labs),
at commit `23c0d92e11af1dea821f972b5f0a739060958002`. The researcher writes
`/app/output/select_subset.py`, which picks `k` of `n` points in the unit cube with
low star discrepancy. The task's own grader scores it.

The import was checked by grading, with no agent and no model. No researcher has
run on it yet. See [What was observed](#what-was-observed).

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
| `/app/output`, named by `instruction.md` | `deliverable="/app/output"` | The researcher also sees the candidate there, so the task's text is true as written. |
| `environment/are-grader-ctx/Dockerfile` | `grader="environment/are-grader-ctx"` | The grader's own image, with both hidden splits in its `/tests`. ARI builds it once and leaves the directory out of the agents' container. |
| `/tests/intermediate.sh` in that image | `feedback` | Scores the `intermediate` split. The researcher sees this score. |
| `/tests/test.sh` in that image | `sealed` | Scores the `final` split. Recorded privately. |
| The grader runs each call as uid 1001 with `runuser` | `capabilities=["CHOWN", "SETUID", "SETGID"]` | ARI's grader container has no capabilities otherwise. |
| `cap_add: SYS_ADMIN` in the compose file | Not needed | Only the benchmark's submit server uses it, for `unshare -n`. ARI's grader container has no network. |
| `docker-compose.yaml`, the `are` MCP server, `tests/finalize.py` | Not used | They are the benchmark's own submit loop, which `record_candidate` replaces. `tests/test.sh` only reads that loop's ledger. |
| `[agent]` network: an allowlist of `api.openai.com`, `api.anthropic.com` and `grader` | Kept, without `grader` | ARI applies it while the agents work, as Harbor does. `grader` is the benchmark's compose service, which is not started. The instructions say what the network reaches. |
| No rules in `instruction.md` | Two rules, written by the importer | The reviewer needs rules to judge. They are marked as the importer's in `task.py`. |
| `budget_seconds = 86400`, many submissions | `ari.Memory(conversation_mode="continue", conversations="all")` and `override_timeout_sec = 86400` in `run.toml` | One researcher session for 24 hours, as the benchmark runs an agent. |
| `hints/hint.md` | Not used | The benchmark keeps hints apart from the instruction. |

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
  `grader_cpus = 4`; without it ARI refuses to grade on fewer CPUs than the task
  asks for. This grader runs as many calls at a time as it has CPUs, each with
  its own 10-second budget, so fewer CPUs make grading slower here rather than
  changing the score.
- **Images.** In the sandbox where this was checked, `docker build` could only
  reach package mirrors through a TLS-inspecting proxy, so both images were built
  outside ARI with the proxy's CA mounted as a build secret, then given as
  `[task] grader_image` and `[task] image`. Their Dockerfiles were otherwise
  unchanged. On an ordinary machine, leave those out and ARI builds them.

## Not yet checked

- **A researcher in this container.** `ari check` with [run.toml](run.toml) opens the
  task and derives the agents' environment from the task's image. That
  environment holds only `environment/` without `are-grader-ctx/`. No agent has
  started in it.
- **The agent's network with a real agent.** The allowlist is carried over and its
  enforcement was tested through ARI's own agent path with a probe agent, not
  with this task's image or a model. Terminus-2, in [run.toml](run.toml), calls
  its model from the host. Codex or Claude Code would call it from inside the
  container, so a provider other than OpenAI or Anthropic needs
  `allowed_hosts` under `[task]`.
