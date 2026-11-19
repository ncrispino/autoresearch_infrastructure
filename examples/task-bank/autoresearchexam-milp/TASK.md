# AutoResearchExam: MILP branching policy

The second real benchmark task imported into ARI without editing it:
`tgat-milp-branching-node-count` from
[AutoResearchExam](https://github.com/bespokelabsai/AutoResearchExam) (Bespoke Labs),
at commit `23c0d92e11af1dea821f972b5f0a739060958002`. The researcher writes
`/app/submission/policy.py`, a branching policy that the SCIP solver consults at
every branch-and-bound node on set covering instances. The task's own grader counts
the nodes the solver needs to prove optimality.

It differs from the [first import](../autoresearchexam-carps/TASK.md) in three ways
that the import had to handle:

- The deliverable is `/app/submission`, and the image ships a starter there.
- Its grading limit is in the benchmark's compose file, not in `[verifier]`.
- Its score depends on wall-clock time, so the number of CPUs changes it.

The import was checked by grading the shipped starter, with no agent and no model.
No researcher has run on it. See [What was observed](#what-was-observed).

## Fetch the task

The benchmark's files are not stored here. Copy the task directory in unchanged, then
write the starting deliverable its image ships:

```bash
git clone https://github.com/bespokelabsai/AutoResearchExam /tmp/are
```

```bash
git -C /tmp/are checkout 23c0d92e11af1dea821f972b5f0a739060958002
```

```bash
cp -r /tmp/are/tgat-milp-branching-node-count examples/task-bank/autoresearchexam-milp/harbor-task
```

```bash
ari extract-seed examples/task-bank/autoresearchexam-milp/harbor-task --deliverable /app/submission --output examples/task-bank/autoresearchexam-milp/seed
```

`extract-seed` writes one file, `policy.py`, which always branches on the first
candidate. It is the same file as the task's `environment/app/submission/policy.py`,
and every run checks that the seed still matches the image.

## How the source maps to ARI

| Source | In `task.py` | Why |
| --- | --- | --- |
| `/app/submission/policy.py`, named by `instruction.md` | `deliverable="/app/submission"` | The researcher also sees the candidate there, so `/app/rollout.py` and the task's text work unchanged. |
| The starter the image ships at `/app/submission` | `seed/`, from `ari extract-seed` | The candidate hides the image's files at that path, so the seed must be them. |
| `environment/are-grader-ctx/Dockerfile` | `grader="environment/are-grader-ctx"` | The grader's own image, with the two sealed panels in its `/tests`. |
| `/tests/intermediate.sh` and `/tests/test.sh` in that image | `feedback` and `sealed` | The `intermediate` and `final` panels, each 40 instances solved twice. |
| The grader drops each solve to uid 1001 with `setgroups`, `setgid` and `setuid` | ARI's default: Docker's default capabilities, as the benchmark's grader has | Its solver import blocking is a Python import hook and audit hook, which needs no capabilities. `SYS_ADMIN` is only for the benchmark's server. |
| `ARE_GRADE_TIMEOUT_SEC: "2700"` in the compose file | `timeout=2700` | The grader has a 1900-second panel budget of its own. `[verifier] timeout_sec = 600` belongs to the benchmark's ledger-reading step and would cut grading short. |
| `[agent]` allowlist: `api.openai.com`, `api.anthropic.com`, `grader` | Kept, without `grader` | As in the first import. |
| Rules enforced by the grader, such as no MILP solver inside the policy | Two rules restating what a reviewer can judge | Marked as the importer's in `task.py`. |

## What was observed

Grading only, no agent, on a 4-CPU machine with `cpus = 4` in the run settings:

| Candidate | Feedback panel (`intermediate`) | Sealed panel (`final`) |
| --- | --- | --- |
| The shipped starter, which always branches on the first candidate | reward 0; 3,198 nodes, 35 of 80 runs charged the cap | reward 0; 5,977 nodes, 49 of 80 runs charged the cap |

All three of the grader's own tests passed on both panels: the deliverable
contract, the graded panel and the reward bounds. The grader dropped each solve
to uid 1001 inside ARI's container as it does in the benchmark's. Grading took
14 and 17 minutes for the two panels.

These numbers are not comparable with the benchmark's. The grader runs 8 solves
at once, each with a 120-second wall-clock cap that counts toward the score, and
it was built for 8 CPUs. On 4, most runs that hit the cap hit it for time, not for
nodes: one hard instance alone took 114 seconds for 3,759 decisions. That is why
ARI refuses to grade a task on fewer CPUs than it asks for unless the settings
say so.

The grading of [reference-policy.py](reference-policy.py), "most fractional", was
interrupted twice when the machine restarted, and has not been run to the end. On
the task's own practice instances it needed fewer nodes than the starter (a
geometric mean of 3,058 against 4,203 on eight instances), but both are well
above the task's trivial floor of about 400, so on this machine it would also
score 0. ARI recorded each interrupted evaluation as incomplete, with no score,
and now removes the grader container an interrupted process leaves behind.
