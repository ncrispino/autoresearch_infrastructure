"""AutoResearchExam's carps-star-discrepancy-subset-select, imported without editing the task.

The Harbor task in harbor-task/ is the benchmark's own directory, unchanged
(bespokelabsai/AutoResearchExam at commit 23c0d92). It is not stored here; TASK.md
says how to fetch it. Its deliverable is
/app/output, which the image ships empty. Its grader image is built from
environment/are-grader-ctx/Dockerfile: intermediate.sh scores the intermediate
split and test.sh the final split, both from hidden data inside that image.
The benchmark's own submit server and its top-level tests/, which only read
that server's ledger, are not used.
"""

import autoresearch_infrastructure as ari

# The source states no rules a reviewer would judge; these are the importer's.
# Rules forbid; what the deliverable must contain is the grader's to score, so an empty starter breaks none.
RULES = [
    "Do not store subsets, indices or results chosen for particular clouds, or return them in place of a "
    "subset computed from the points `select` is given.",
]


def task(run):
    return ari.bases.harbor_deliverable(
        run,
        harbor_task="harbor-task",
        deliverable="/app/output",
        feedback="/tests/intermediate.sh",
        sealed="/tests/test.sh",
        # The grader's own image, built from this directory. [task] grader_image names a prebuilt one.
        grader="environment/are-grader-ctx",
        # The root defaults the benchmark's grader runs with, without the network ones. It runs each call
        # as uid 1001 and kills a hung call's processes (KILL). SYS_ADMIN, which the benchmark's server needs
        # for `unshare -n`, is not needed: the grader container has no network.
        capabilities=["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID", "SETPCAP", "AUDIT_WRITE"],
        rules=RULES,
        # The benchmark gives one agent 24 hours and many submissions.
        memory=ari.Memory(conversation_mode="continue", conversations="all"),
    )
