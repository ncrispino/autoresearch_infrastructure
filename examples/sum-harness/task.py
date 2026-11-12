"""Improve the program that drives a solver agent, on a fixed Harbor task.

The Harbor task in ``harbor-task/`` asks an agent to add the integers in a file
and write the total to another file. The candidate is the solver program in
``seed/``. The Harbor task's own grader scores every trial.
"""

import autoresearch_infrastructure as ari


def task(run):
    return ari.bases.harbor_harness(
        run,
        harbor_tasks={"sum": "harbor-task"},
        advice="Do not hard-code the input numbers or the expected total.",
    )
