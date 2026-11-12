# Break an existing task down into ARI's format

Use this guide when you import a task from a benchmark or turn someone's written
task description into an ARI task. It shows the method, then applies it to three
published autoresearch tasks.

The three breakdowns are worked examples of the method. They are not adapters:
none of these tasks has been run through ARI. They were made from each
benchmark's public task description as read on 2026-11-11, and each case study
lists what was not read.

## The target format

Every ARI task has the same parts, so every import ends in the same place.

| Part | Holds |
| --- | --- |
| `seed` | The starting files of the deliverable. |
| `instructions` | One text with the sections below. |
| `objective` | The metric that selects the best candidate, and its direction. |
| `evaluator` | The code that measures one candidate. |
| `validity` (optional) | Rules that code can decide exactly. |
| `cases` (optional) | The fixed inputs every candidate is measured on. |
| `context` (optional) | Read-only material for the researcher. |

The instructions have two sections.

| Section | Answers | Read by |
| --- | --- | --- |
| Objective | Which files change and what form must they take? Which metric should move, in which direction, and how is a run measured? | Researcher |
| Constraints: Rules | What must a candidate do or not do, that only its author can choose to obey? | Researcher, and the reviewer judges these |
| Constraints: Environment | What does the sandbox enforce or provide, whatever the researcher does? | Researcher |

A long Objective may use sub-headings of its own, such as "Interface" or "How a
run is measured". A suggestion about approach can close it.

## Method

1. **Find the deliverable first.** Ask what the source task grades. Those files
   are the candidate, and their unimproved version is the `seed`.
2. **Sort every sentence of the source.** Most source tasks mix these. Use this
   test for the hard cases:
   - It names the files, their interface, the metric, or how the number is
     computed, including what scores zero: **Objective**.
   - It can be broken by the candidate's content and someone has to judge it:
     **Rules**. Example: "no caching keyed on prompt content".
   - It holds whatever the candidate does, because the sandbox enforces it:
     **Environment**. Example: "no network", "8 CPU cores", "read-only".
   - It suggests a method: the end of the **Objective**, or leave it out.
3. **Move every rule that code can decide into `validity`**, and still state it
   under Rules. The researcher has to read a rule to obey it, and a validity function costs
   no reviewer start.
4. **Decide what the evaluator is.** Reuse the source task's grader where you
   can. If the source task is a Harbor task, start from a base task in
   [Harbor tasks](harbor-harness.md).
5. **Write down what does not fit.** Do not bend the task to hide a gap. The
   common gaps are listed at the end of this guide.

## Case study 1: serve a generation workload faster

Source: AutoResearchExam (Bespoke Labs), task `cpu-llm-decode-throughput`.
Read: its `instruction.md`, `task.toml` and the shared protocol template. Not
read: its grader code and its `hints/` folder.

| Source section | Goes to | Why |
| --- | --- | --- |
| Goal paragraph: build an engine that serves the workload faster with the same greedy token ids | Objective | States what should improve. |
| "Submission": write `/app/output/engine.py` exposing a class with `__init__(model_dir)` and `generate(requests)` | Objective | Names the file and its interface. |
| "Request format", "Output format" | Objective | They define the interface the file must meet. |
| "Metric": speedup is the median over three held-out rounds of the throughput ratio against the reference engine | Objective | Defines the number and its direction. |
| "How a run is measured": fresh process, unprivileged user, warm-up budget | Objective | Describes how the number is taken. |
| "What scores nothing": token agreement below 0.99, run time over twice the reference, oversized output directory | Objective, and `validity` where cheap | The grader decides these exactly. They are part of how a run is scored, not judgment calls. |
| "Environment": 8 CPU cores, 14 GiB RAM, no GPU, no network | Constraints: Environment | Enforced by the sandbox. |
| "Dev workloads": a local benchmark script | Constraints: Environment, and `context` if copied | Something provided, not a rule. |
| `hints/` folder | End of Objective, or leave it out | The benchmark itself keeps hints apart from the instruction. |

Resulting parts:

| Part | Value |
| --- | --- |
| `seed` | The reference `engine.py`. |
| `objective` | Speedup, higher is better. |
| `evaluator` | The task's own grader. |
| `validity` | The file exists, parses, and defines a class with the two methods. |
| `cases` | One for each held-out workload round. |

What the source has no Rules for: this task states almost nothing that needs
judgment. Its limits are either part of scoring or sandbox facts. An honest import
has a short Rules list, for example "the engine must compute its outputs, not
replay stored ones", and relies on the validity function and the grader for the rest.

## Case study 2: choose training data for a small CLIP model

Source: RSI Bench (Scale AI), task `biomedical-tinyclip-data-curation`. Read:
its instructions and verification pages. Not read: its `task.toml`, its
environment page (a placeholder) and its grader code.

| Source section | Goes to | Why |
| --- | --- | --- |
| "Context": a pool of 1,438,283 figure-caption pairs, a frozen model and a fixed training recipe | Objective | Background needed to understand what improves. |
| "Goal": retrieval on held-out papers and zero-shot classification, equally weighted | Objective | The aim, and how it is weighed. |
| "Deliverables": `selection.json` with 10,000 to 256,000 unique ids, `curate.py` that reproduces it, `summary.md` | Objective | Three files and their forms. |
| "How you are scored": reward is the mean of the retrieval mean and the zero-shot mean, in percent | Objective | Defines the number. |
| "Resources": pool layout, dev sets, a local validator that takes 3 to 5 minutes | Constraints: Environment, and `context` | Things provided. |
| "Constraints": do not change the recipe; no external model API calls on the data; work only in `/workspace`; the selection must be reproducible | Constraints: Rules | Each can be broken by the author's choices. |
| "Timer": a file reports the time left | Constraints: Environment | Provided by the sandbox. |
| Verification page: reviewers re-run `curate.py`, and a hand-picked id list fails review even if it scores well | Constraints: Rules | A rule the grader cannot decide. This is what a reviewer is for. |

Resulting parts:

| Part | Value |
| --- | --- |
| `seed` | A baseline `curate.py` and the `selection.json` it produces. |
| `objective` | Reward, higher is better. |
| `evaluator` | Fine-tune on the selection with the frozen recipe, then measure both suites. |
| `validity` | `selection.json` parses, its ids are unique integers in range and within the size bounds, and `curate.py` parses. |
| `cases` | One for each evaluation domain or suite, or a single case if the grader reports only the mean. |

This task shows why review is on by default. "The selection must come from a
general procedure" cannot be checked by a script, and the benchmark handles it
with human review after the run. In ARI the same sentence goes under Rules and
the reviewer judges it for every candidate.

## Case study 3: make an attribution library faster

Source: Autoresearch Bench (Emulated), task `attribution-speedup`. Read: the
task's overview page. Not read: the literal prompt (on a separate tab), the
task's files (not yet published) and its grader code.

| Source section | Goes to | Why |
| --- | --- | --- |
| "The task": make the backward sweep faster and lighter on CPU and GPU while returning exactly the same graph | Objective | What improves, and what must stay equal. |
| Interface: `fastattr.prepare(model)` and `fastattr.attribute_fast(handle, prompt)`; everything under `/app/src` may be restructured | Objective | The files and the interface that must hold. |
| "How submissions are scored" and "Evaluation": geometric mean over axes of graphs per second times a memory factor; CPU score plus GPU score | Objective | Defines the number. |
| Gates: any graph that disagrees, a `prepare` over ten minutes, spawned processes, or low CPU use scores zero | Objective, and `validity` for what is cheap | Decided exactly by the grader. |
| "Rules": no forking or worker processes inside `attribute_fast`; no caching keyed on prompt content; do not edit stored reference graphs | Constraints: Rules | "No caching keyed on prompt content" needs judgment. |
| Offline container, a four-hour clock, preinstalled packages, read-only directories | Constraints: Environment | Enforced by the sandbox. |
| Tools `check`, `bench`, `submit`, `clock` | Constraints: Environment | Provided. `submit` is replaced by ARI's `record_candidate`. |
| "Experiment discipline": one idea per commit, keep a results log | End of Objective, or leave it out | A working habit, not a requirement the grader enforces. |
| "A possible approach" | End of Objective, or leave it out | The page itself says it is one possible solution. |

Resulting parts:

| Part | Value |
| --- | --- |
| `seed` | The starting `/app/src` tree. |
| `objective` | Task score, higher is better. |
| `evaluator` | The task's grader on its graded prompt pool. |
| `validity` | The package imports and exposes the two functions. |
| `cases` | One for each of the ten axes, if the grader can report them separately. |

## Two imports written out in full

The tables above sort a source task's sections. These two files are the
finished instructions, at the length a real task needs:

- [cpu-llm-decode-throughput.md](import-examples/cpu-llm-decode-throughput.md), from case study 1
- [attribution-speedup.md](import-examples/attribution-speedup.md), from case study 3

Both parse as `ari.Instructions`, and a test keeps it that way. Neither is a
runnable task: there is no `task.py`, grader or data here.

A long Objective keeps its own sub-headings. Both files use the same three:
"Interface" for the files and the form they must take, "How a run is measured"
for the metric and what scores nothing, and "Measuring locally" for tools the
researcher can run. Reuse these names when you import a task.

The instructions are only the prose. The parts that define the task sit beside
them in `task.py`, and ARI writes the "What is measured" list in the
researcher's prompt from those parts:

```python
SPEEDUP = ari.Metric("speedup")          # direction defaults to "max"


def task(run):
    return ari.Task(
        objective=SPEEDUP,
        evaluator=run_grader,            # runs the benchmark's own grader on one round
        validity=has_engine_class,       # engine.py parses and defines the class
        cases=("round-1", "round-2", "round-3"),
        context={"bench.py": Path("bench.py")},
    )
```

What changed between each source and its import:

| Change | cpu-llm-decode-throughput | attribution-speedup |
| --- | --- | --- |
| Paths | `/app/output/engine.py` became `/workspace/candidate/engine.py`; the dev benchmark moved to `/context` | `/app/src` became `/workspace/candidate`; `check`, `bench` and the budgets moved to `/context` |
| Left out | The submission tool and the time limit, which ARI's protocol and run settings cover | `submit`, `clock` and the four-hour limit, for the same reason; the "possible approach" section |
| Rules taken from the source | None. The source states no rules of this kind | The first four, from its "Rules" section |
| Rules added in the import | All four. They state what the source leaves to its grader and sandbox: compute outputs at run time, stay in one process, ship no weights, use only installed packages | The fifth, from the source's note that nothing can be installed at run time |
| Wording | Paraphrased from the source's sections, with its numbers kept | Paraphrased from the task's overview page, with its numbers kept. The literal prompt was not read |

Rules added in an import are a judgment by the importer. Mark them, as this
table does, so that a reader can tell the source's requirements from yours.

## What does not fit today

All three tasks share gaps that an import has to state instead of hiding.

- **A private score.** Each benchmark shows the agent a public score and ranks
  it on a hidden one. In ARI the researcher sees the score that selects the best
  candidate. An import that used the hidden split as the evaluator would leak it
  to the researcher through the returned score.
- **Working inside the task's environment.** These tasks expect the agent to
  run experiments beside the data, the GPU and a local validator. ARI's
  researcher runs in a small container that holds the candidate, `/context` and
  `/history`. Material can be copied into `context`, but a GPU and an 18 GB data
  pool cannot.
- **Long runs.** The tasks give one agent 4 to 24 hours. ARI charges one
  researcher start for each candidate, so the budget has to be restated in
  candidates and evaluations.
- **Many submissions from one session.** These tasks let one agent submit
  repeatedly. An ARI researcher submits one candidate per start.

Two of the three are Harbor tasks and run as they are with `harbor run`, without
ARI. Import a task into ARI when you want ARI's saved candidates, review and
budget around it, and say in the task's README which of these gaps apply.
