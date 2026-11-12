# Make the task instructions clearer

## Objective

Improve `/workspace/candidate/sum/instruction.md` so that a fixed agent
completes its task more often. The agent is given that file as its
instructions. The task is to read the integers in `/workspace/numbers.txt`, add
them, and write only the total to `/workspace/answer.txt`.

`success_rate` is the share of trials in which the task's grader accepts the
answer file. The grader reads the file, not the agent's final message.

Read the trajectories of earlier trials to find where the agent went wrong, such
as a misread path or extra text in the answer file, and change the wording that
caused it.

## Constraints

### Rules

- Keep the task's goal, its required input and its output requirements. Clearer
  wording must not make the task easier than it is.
- Do not state the input numbers or the expected total, hint at them, or
  describe a way around the grader.
- Edit only `sum/instruction.md`.

### Environment

- The agent, its model, the task's other files and the grader are fixed and out
  of your reach.
- `/workspace/numbers.txt` and `/workspace/answer.txt` are inside the agent's
  own container, not yours.
