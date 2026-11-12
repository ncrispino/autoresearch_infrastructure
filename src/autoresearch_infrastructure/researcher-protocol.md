## Your files

| Path | What is here |
| --- | --- |
| `/workspace/candidate/` | Your editable solution, copied from `$parent_id`. Only this directory is submitted. |
| `/context/` | Read-only task material. |
| `/history/index.json` | Read-only summary of earlier candidates: each idea, its validity verdict, its scores, and which candidate is currently selected. Start here. |
| `/history/candidates/<id>/` | One earlier candidate: its files in `snapshot/`, its changes in `diff.patch`, its review in `validity/`, and its results in `evaluations/`. |
| `/tmp/` | Scratch space. Nothing here is submitted. |

The unchanged baseline is `candidate-0001`. Your starting version is `$parent_id`.
Read its evidence at `/history/candidates/$parent_id/` before editing.
The index links each trial's result, and its native trajectory when one exists,
under `attempts[].paths`. Those paths are relative to `/history`. Use the result
and native trajectory paths linked in `/history/index.json` to see what happened
in a trial. A score alone does not explain its cause.

`/context` and `/history` are mounted read-only. The evaluator, the validity
rules and the budget run outside your container.

## Submit one candidate

Submit with `record_candidate`. If your harness exposes it as a tool, call it
directly. In a terminal harness, write the arguments to a JSON file and send
them with the client below. Replace the hypothesis and mechanism with your own
explanation.

```bash
python - <<'PY'
import json
from pathlib import Path

Path("/tmp/proposal.json").write_text(json.dumps({
    "candidate": "workspace",
    "proposal": {
        "hypothesis": "What should improve?",
        "mechanism": "What changed and why?"
    }
}))
PY
python /opt/autoresearch/mcp_proxy.py --call /tmp/proposal.json
```

The controller already knows your starting version and runs $repeats repeat(s)
per case. It saves your files, checks validity and runs the evaluation. Wait
for the reply; it can take several minutes.

- A reply containing `candidate_id` means your candidate was recorded. Stop
  there, even if it is invalid, its evaluation failed, or its score is poor.
  This attempt allows one submission.
- If the request was rejected before recording, fix the request and retry.
- If the connection breaks after you send the request, stop. The controller may
  still be evaluating your candidate, so do not send it again.
