# Threshold deliverable

Improve the deliverable of a Harbor task, scored by that task's own grader. Use
this example as the model for importing a benchmark task in which the agent
writes files into one directory and a grader scores them.

The Harbor task in [harbor-task/](harbor-task/instruction.md) asks for one file,
`/workspace/candidate/model.json`, holding a threshold that separates two classes of
points. Its grader reports the accuracy on points the agent cannot read.

| Part | Definition |
| --- | --- |
| Candidate | The contents of `/workspace/candidate`: here the one file `model.json`. |
| Baseline | [harbor-task/environment/candidate/model.json](harbor-task/environment/candidate/model.json), a threshold of 0.2. The task's Dockerfile copies the same directory to `/workspace/candidate`, so the starting deliverable exists once. |
| Objective | Maximize `reward`: the accuracy from `tests/feedback.sh`, on 50 hidden points. |
| Sealed score | The accuracy from `tests/test.sh`, on 200 other hidden points. No agent sees it and selection does not use it. |
| Rules | Two, written in [task.py](task.py). A Harbor instruction does not list rules, and the reviewer needs them. |
| Fixed inputs | The Harbor task, its grader, both hidden splits and the budget. |

One tiny task shows how the pieces connect. It says nothing about whether a
researcher improves a real benchmark task.

## Files

| File | Purpose |
| --- | --- |
| [task.py](task.py) | The task: one call to `ari.bases.harbor_deliverable`. |
| [harbor-task/](harbor-task/task.toml) | The Harbor task, laid out as Harbor expects, plus one added script. |
| [harbor-task/environment/](harbor-task/environment/Dockerfile) | The task's main container, with the training data and, in `candidate/`, the starting deliverable. The researcher works in it. |
| [harbor-task/tests/](harbor-task/tests/grade.py) | The grader and both hidden splits. `test.sh` is Harbor's own test script and the sealed score. `feedback.sh` is the script added for ARI. |
| [local.toml](local.toml) | Run settings without agents. |
| [run.toml](run.toml) | Run settings with a researcher and a reviewer. |

The Harbor task still runs with `harbor run` as it is. ARI needs two things it
does not say, and `task.py` supplies them: which script may run after every
submission, and the rules. The deliverable is `/workspace/candidate`, the
convention for a task written for ARI, so `task.py` does not name it.

## Grade your own edits

This needs Docker and no model. Commands run from the package root.

```sh
ari record examples/threshold-deliverable --config examples/threshold-deliverable/local.toml --output runs/threshold
```

The first call builds the grader image from `harbor-task/environment/`, records
its ID in `runs/threshold/harbor-deliverable/grader.json`, and grades the
baseline. Edit `model.json` in the workspace directory the command prints and
run it again to grade your version. Each call starts two short grader
containers, one for each script, and deletes them.

```sh
ari sealed runs/threshold
```

prints the sealed reward of every graded candidate. Read it when the run is
over. Choosing a candidate by its sealed reward spends the held-out split.

## Run a researcher

```sh
ari check examples/threshold-deliverable --output runs/threshold-live
ari run   examples/threshold-deliverable --output runs/threshold-live
```

`ari run` calls your models and costs money. There is no `--image`: the
researcher and the reviewer both run in the Harbor task's own container, built
from `harbor-task/environment/`. The agent in [run.toml](run.toml) is Terminus-2,
which also needs `tmux`, and the task's Dockerfile has only Python. Build an
image from the task's own that adds it and name its ID under `[task] image` in
`run.toml`, as the [curve-fitting example](../curve-fitting/README.md#run-a-harbor-researcher)
shows. The grader keeps using the task's own image.
