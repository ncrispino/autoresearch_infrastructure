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
| `capabilities` | Docker's default root capabilities without the network ones: `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `FSETID`, `KILL`, `SETGID`, `SETUID`, `SETPCAP`, `AUDIT_WRITE`. The benchmark runs its graders with them, and they use them: a grader `chown`s each call's directory to the agent's uid and then `chmod`s it, which needs `FOWNER`, and kills the agent's processes, which needs `KILL`. The candidate runs as the agent's uid, without them. |

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

The script was run on every task at commit `23c0d92e11af1dea821f972b5f0a739060958002`. Each
CPU task's two images were then built, its starting deliverable extracted with
`ari extract-seed`, and `ari prepare` run, which checks that both grader scripts
are in the grader image and that the seed is what the task's image ships. Nothing
was graded except the two hand imports.

| Task | Deliverable | Grading timeout (s) | Starter files | Images (GB) | Result |
| --- | --- | --- | --- | --- | --- |
| act-tensor-sparse-panel-imputation-r2 | `/app/solution` | 1800 | 0 | 1.4 | Imported, built and prepared |
| activeprune-al-unlabeled-pool-pruning | `/app/output` | 1800 | 0 | 1.6 | Imported, built and prepared |
| budgeted-covtype-dual-market-open | `/app/output` | 2000 | 0 | 1.3 | Imported, built and prepared |
| budgeted-imputation-mcar50 | `/app/output` | 1800 | 1 | 1.3 | Imported, built and prepared |
| carps-star-discrepancy-subset-select | `/app/output` | 1200 | 0 | 1.1 | Imported, built and prepared |
| causalpfn-cate-pehe-ihdp-surfaceb | `/app/output` | 1800 | 0 | 3.6 | Imported, built and prepared |
| causalrivers-heldout-station-graph-auroc | `/app/output` | 2700 | 0 | 4.9 | Imported, built and prepared |
| coreset-selection-group-robust-waterbirds | `/app/output` | 2400 | 0 | 1.4 | Imported, built and prepared |
| cpu-decoder-graph-executor | `/app/output` | 1800 | 0 | 0.8 | Imported, built and prepared |
| cpu-llm-decode-throughput | `/app/output` | 2400 | 0 | 5.2 | Imported, built and prepared |
| dctabeval-aeac-pooled-cat-statistics | `/app/output` | 1800 | 0 | 2.8 | Imported, built and prepared |
| grpo-rl-halfcheetah-advantage-estimator | `/app/output` | 15000 | 0 | 3.3 | Imported, built and prepared |
| hicard-latent-encoder | `/app/output` | 1800 | 0 | 1.4 | Imported, built and prepared |
| label-efficient-risk-estimator | `/app/output` | 2700 | 0 | 1.2 | Imported, built and prepared |
| mlh-coco-16bit-hash-head-map5000 | `/app/output` | 1800 | 0 | 4.0 | Imported, built and prepared |
| reppo-reliable-onpolicy-control-trainer | `/app/solution` | 2400 | 1 | 3.0 | Imported, built and prepared |
| shortest-valid-ci-l2-ece | `/app/output` | 1800 | 0 | 1.1 | Imported, built and prepared |
| sketched-newton-cov-estimator | `/app/output` | 1500 | 0 | 1.1 | Imported, built and prepared |
| sopcc-online-chance-constrained-policy | `/app/solution` | 1800 | 0 | 3.4 | Imported, built and prepared |
| sparse-elsa-item-embeddings-8nnz | `/app/output` | 3300 | 0 | 3.7 | Imported, built and prepared |
| tgat-milp-branching-node-count | `/app/submission` | 2700 | 1 | 3.7 | Imported, built and prepared |
| vas-maskless-deployment-feasibility | `/app/output` | 2400 | 0 | 3.7 | Imported, built and prepared |
| fast-adv-budgeted-pgd50-robust-cifar10 | | | | | Not imported: needs a GPU |
| fastercache-budgeted-video-dit-cache-policy | | | | | Not imported: needs a GPU |
| fastergcg-candidate-token-rank-ccc | | | | | Not imported: needs a GPU |
| less-is-more-pretrain-token-budget-selector | | | | | Not imported: needs a GPU |
| sae-sparse-dict-nmse-frontier | | | | | Not imported: needs a GPU |
| svdquant-w4a4-psnr | | | | | Not imported: needs a GPU |
| ties-merging-clip-vitl14-eight-task-merge | | | | | Not imported: needs a GPU |

Every CPU task asks for 8 CPUs. On the 4-CPU machine used here, each import's settings
said `cpus = 4`. Building took 43 minutes in all, from 46 seconds to about 3½ minutes a
task; the images take 0.8 to 5.2 GB a task. Three tasks ship a starter file; the rest
start from an empty deliverable, so their rules must be prohibitions for the baseline
to pass review.
