"""Improve the deliverable of a Harbor task, scored by that task's own grader.

The Harbor task in ``harbor-task/`` asks for one file, ``/app/output/model.json``.
A candidate is the contents of ``/app/output``. The task's grader scores each
candidate on a feedback split, which the researcher sees, and on a sealed split,
which is recorded privately.
"""

import autoresearch_infrastructure as ari


def task(run):
    return ari.bases.harbor_deliverable(
        run,
        harbor_task="harbor-task",
        seed="harbor-task/environment/output",   # the starting deliverable, as the task's image ships it
        deliverable="/app/output",          # the directory the grader reads; a candidate is its contents
        feedback="/tests/feedback.sh",      # scored after every submission and shown to the researcher
        sealed="/tests/test.sh",            # scored privately; no agent and no selection step reads it
        rules=[
            "The deliverable is the single file `model.json`.",
            "Choose the threshold from the data in `/app/data/train.csv`.",
        ],
    )
