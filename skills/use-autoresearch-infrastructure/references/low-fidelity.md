# Screen and confirm

A researcher that studies the results of its own cases can fit them. To learn
whether an improvement holds in general, use two sets of cases:

- The screen panel is small and cheap. The researcher sees its results and
  proposes candidates against it.
- The confirmation panel is separate. No researcher sees it. It measures the
  baseline and the selected candidate once the search is over.

## Choose the panels

- Keep the panels disjoint and fix both before the search. Do not choose them
  from scores.
- Say what makes the screen panel cheaper: fewer cases, less data, fewer repeats
  or a shorter simulation.
- Keep the solver, the model and the grader the same on both panels. A smaller
  model is a different solver, and its results do not predict the larger one.
- Check that the screen panel can tell candidates apart. If the baseline already
  succeeds on every screen case, there is nothing to improve. A very small
  dataset can also reverse a ranking, for example when query cost depends on
  table size.
- The instructions and the validity function apply on both panels. A smaller panel does not
  permit a forbidden edit.

## Run the two panels

Each panel is its own run directory with its own cases. Metrics are averaged
over all cases of a run, so two panels in one run would be mixed into one score.

1. Run the search on the screen panel.
2. Fix the choice before looking at any confirmation result: the selected
   candidate, and the hash of its files.
3. In the confirmation run, measure the baseline first. Then measure the selected
   candidate from its exact saved files, with the same repeat count.
4. Report both panels separately, with every failure and every case that got
   worse. Do not average screen and confirmation scores together.

If confirmation results lead to more search, the confirmation panel has become
development data. Reserve new cases before making a final claim.

### With the harness study

For solver programs on Harbor benchmarks,
[the harness study](harbor-harness.md#the-benchmark-harness-study) does all four
steps and counts the starts of both panels in one ledger.

### With `ari`

`ari run` searches one panel. For a confirmation run, make the task choose its
cases from the run settings, for example from a `[task] panel` value that
`task.py` reads through `run.settings.task`. Keep the confirmation cases out of
the screen run's context. Then:

```sh
# Search on the screen panel.
ari run <task-directory> --config screen.toml --output <screen-run> --image <image-id>

# Confirmation: the first record measures the baseline.
ari record <task-directory> --config confirm.toml --output <confirm-run> --image <image-id>

# Replace the confirmation workspace with the selected candidate's saved files.
rm -r <confirm-run>/research/sessions/research/workspace/candidate
cp -r --no-preserve=mode <screen-run>/research/sessions/research/history/candidates/<selected-id>/snapshot \
  <confirm-run>/research/sessions/research/workspace/candidate

# The second record measures it as candidate-0002.
ari record <task-directory> --config confirm.toml --output <confirm-run> --image <image-id>
```

`confirm.toml` needs no `[researcher]`. Give it `candidates = 2` and enough
`evaluations` for two candidates on the confirmation cases. Without a
researcher, the settings must also say who reviews. A `[reviewer]` table reviews
both candidates again and uses two `reviewer` starts. `reviewer = false` relies
on the task's validity function and on the review that the selected candidate passed in the
screen run.

Each run directory counts its own starts. The budget of the study is the sum of
both settings files, so state both to the user before the first paid start. To
count both panels in one ledger, build the two `Research` objects in Python with
a shared `StartLedger`, as
[`pilot.py`](../../../src/autoresearch_infrastructure/pilot.py) does.
