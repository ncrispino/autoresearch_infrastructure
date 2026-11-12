"""Improve a Harbor task's instructions while the agent that solves it stays fixed.

The Harbor task in ``harbor-task/`` asks an agent to add the integers in a file
and write the total to another file. The candidate is that Harbor task's
``instruction.md``. The Harbor task's own grader scores every trial.
"""

import autoresearch_infrastructure as ari

REQUIRED_TEXT = ("sum", "/workspace/numbers.txt", "/workspace/answer.txt")


def validity(candidate, context):
    """The instructions must still name the computation, the input file and the output file."""
    text = (candidate / "sum/instruction.md").read_text()
    if not all(phrase in text for phrase in REQUIRED_TEXT):
        return ari.Validity("invalid", ("Keep the task's goal, input path and output path",))
    return ari.Validity()


def task(run):
    return ari.bases.harbor_task_files(
        run,
        harbor_tasks={"sum": "harbor-task"},
        editable=["instruction.md"],
        validity=validity,
    )
