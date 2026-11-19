# Import AutoResearchExam tasks

[AutoResearchExam](https://github.com/bespokelabsai/AutoResearchExam) (Bespoke Labs)
ships 31 Harbor tasks with one shared layout, so one script imports any of them
without editing it:

```bash
python examples/task-bank/autoresearchexam/import_task.py /tmp/are/<task> my-task
```

[import_task.py](import_task.py) copies the task, unchanged, to `my-task/harbor-task/`,
and writes `my-task/task.py` and `my-task/local.toml`. Everything it writes is read
from the task's own files; nothing in the task is run:

| `task.py` | Read from |
| --- | --- |
| `deliverable` | `ARE_ARTIFACT_PATHS` of the grader service in `environment/docker-compose.yaml` |
| `timeout` | `ARE_GRADE_TIMEOUT_SEC` there. The task's `[verifier] timeout_sec` belongs to the benchmark's ledger-reading step. |
| `grader` | `environment/are-grader-ctx`, whose Dockerfile copies both scripts and both hidden splits into the image's `/tests` |
| `feedback`, `sealed` | `/tests/intermediate.sh` and `/tests/test.sh` in that image |
| `capabilities` | `CHOWN`, `SETUID`, `SETGID`: every CPU task's grader drops to the agent's uid with `runuser`, `setuid` or `chown` |

It writes no rules, because the tasks state none that a reviewer could judge, so
`local.toml` turns review off. Add rules to `task.py` before a run with a reviewer.
On a machine with fewer CPUs than the task asks for, `local.toml` also says
`cpus` under `[task]`, for the grader and the agents; scores graded that way are not
comparable with the benchmark's.

Then write the starting deliverable the task's image ships, which `task.py` finds
as `seed/`:

```bash
ari extract-seed my-task/harbor-task --deliverable <path> --output my-task/seed
```

The two worked imports, [subset selection](../autoresearchexam-carps/TASK.md) and
[MILP branching](../autoresearchexam-milp/TASK.md), were written by hand before this
script and add rules of their own.

## All tasks

The script was run on every task at commit `23c0d92e11af1dea821f972b5f0a739060958002`,
and each import was opened with `ari check`.

| Task | Deliverable | Grading timeout (s) | Result |
| --- | --- | --- | --- |
| act-tensor-sparse-panel-imputation-r2 | `/app/solution` | 1800 | Imported and opened |
| activeprune-al-unlabeled-pool-pruning | `/app/output` | 1800 | Imported and opened |
| budgeted-covtype-dual-market-open | `/app/output` | 2000 | Imported and opened |
| budgeted-imputation-mcar50 | `/app/output` | 1800 | Imported and opened |
| carps-star-discrepancy-subset-select | `/app/output` | 1200 | Imported and opened |
| causalpfn-cate-pehe-ihdp-surfaceb | `/app/output` | 1800 | Imported and opened |
| causalrivers-heldout-station-graph-auroc | `/app/output` | 2700 | Imported and opened |
| coreset-selection-group-robust-waterbirds | `/app/output` | 2400 | Imported and opened |
| cpu-decoder-graph-executor | `/app/output` | 1800 | Imported and opened |
| cpu-llm-decode-throughput | `/app/output` | 2400 | Imported and opened |
| dctabeval-aeac-pooled-cat-statistics | `/app/output` | 1800 | Imported and opened |
| grpo-rl-halfcheetah-advantage-estimator | `/app/output` | 15000 | Imported and opened |
| hicard-latent-encoder | `/app/output` | 1800 | Imported and opened |
| label-efficient-risk-estimator | `/app/output` | 2700 | Imported and opened |
| mlh-coco-16bit-hash-head-map5000 | `/app/output` | 1800 | Imported and opened |
| reppo-reliable-onpolicy-control-trainer | `/app/solution` | 2400 | Imported and opened |
| shortest-valid-ci-l2-ece | `/app/output` | 1800 | Imported and opened |
| sketched-newton-cov-estimator | `/app/output` | 1500 | Imported and opened |
| sopcc-online-chance-constrained-policy | `/app/solution` | 1800 | Imported and opened |
| sparse-elsa-item-embeddings-8nnz | `/app/output` | 3300 | Imported and opened |
| tgat-milp-branching-node-count | `/app/submission` | 2700 | Imported and opened |
| vas-maskless-deployment-feasibility | `/app/output` | 2400 | Imported and opened |
| fast-adv-budgeted-pgd50-robust-cifar10 | | | Not imported: needs a GPU |
| fastercache-budgeted-video-dit-cache-policy | | | Not imported: needs a GPU |
| fastergcg-candidate-token-rank-ccc | | | Not imported: needs a GPU |
| less-is-more-pretrain-token-budget-selector | | | Not imported: needs a GPU |
| sae-sparse-dict-nmse-frontier | | | Not imported: needs a GPU |
| svdquant-w4a4-psnr | | | Not imported: needs a GPU |
| ties-merging-clip-vitl14-eight-task-merge | | | Not imported: needs a GPU |

Every CPU task asks for 8 CPUs.
