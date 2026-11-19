"""AutoResearchExam's tgat-milp-branching-node-count, imported without editing the task.

The Harbor task in harbor-task/ is the benchmark's own directory, unchanged
(bespokelabsai/AutoResearchExam at commit 23c0d92). It is not stored here;
TASK.md says how to fetch it. Its deliverable is
/app/submission, where the image ships a starter policy.py. Its grader image is
built from environment/are-grader-ctx/Dockerfile: intermediate.sh scores the
intermediate panel and test.sh the final panel, both sealed inside that image.
"""

import autoresearch_infrastructure as ari

# The source's rules are enforced by its grader; these restate the two a reviewer can judge.
# Rules forbid; what the deliverable must contain is the grader's to score.
RULES = [
    "Do not store branching decisions or node counts chosen for particular instances, or return them in place "
    "of a decision made from the observation `select` is given.",
    "Do not reach a MILP solver from the policy while it is graded, directly or through another module or "
    "process.",
]


def task(run):
    return ari.bases.harbor_deliverable(
        run,
        harbor_task="harbor-task",
        deliverable="/app/submission",
        feedback="/tests/intermediate.sh",
        sealed="/tests/test.sh",
        grader="environment/are-grader-ctx",
        # The benchmark's grading service allows 2700 seconds a grade (ARE_GRADE_TIMEOUT_SEC in its
        # compose file); its [verifier] timeout_sec belongs to the step that reads its own ledger.
        timeout=2700,
        rules=RULES,
        memory=ari.Memory(conversation_mode="continue", conversations="all"),
    )
