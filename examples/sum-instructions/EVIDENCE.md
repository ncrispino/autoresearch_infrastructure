# Reference controls

Results of [controls.py](controls.py) with Harbor's Oracle and Nop agents. No model was
called. They show that the Harbor task, its grader and the evaluator work. They do not
show any improvement from optimization.

| Candidate scope | Fixed native agent | Completed trials | Rewards |
| --- | --- | --- | --- |
| Instruction input | Oracle | 2 | 1.0, 1.0 |
| Instruction input | Nop | 2 | 0.0, 0.0 |
| Task source | Oracle | 2 | 1.0, 1.0 |
| Task source | Nop | 2 | 0.0, 0.0 |

Each invocation evaluates the baseline and one proposal in fresh containers.
The task-source proposal changes `numbers.txt` from `2 3` to `1 4` and forces
an image rebuild. Both inputs sum to 5. Oracle executes its fixed reference;
Nop leaves the answer missing and receives a measured reward of zero.

An additional two-trial task-source Oracle invocation checks the verifier's
actual input log: `2 3` for the baseline and `1 4` for the proposal. This confirms
that changed source reached the running container.

The controls retain a read-only root, dropped capabilities, default seccomp,
no-new-privileges and static no-network execution. The offline backend uses
Harbor's network-mode-none overlay when dynamic egress support is unavailable.
Earlier construction/upload failures remain in their original development outputs
and are excluded from the completed-trial counts. No model requests were made.

These results were recorded with this script before it took its current name and options.
To reproduce them, use the commands in the [README](README.md#check-the-harbor-task-first).
