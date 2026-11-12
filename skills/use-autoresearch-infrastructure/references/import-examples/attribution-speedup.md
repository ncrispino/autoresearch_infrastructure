# Make an attribution library's backward sweep faster

## Objective

Improve the `fastattr` package in `/workspace/candidate/` so that it computes
attribution graphs faster and with less memory, on CPU and on GPU, while
returning exactly the same graphs as the reference implementation.

Circuit tracing explains a model's output by building a graph of how much each
internal feature contributed to it. Building that graph needs one backward pass
for each feature of interest, and those passes dominate the running time. The
candidate starts as a working library whose backward sweep is as slow as the
reference by construction.

### Interface

Two functions must keep their signatures. Everything else in the package may be
restructured.

- `fastattr.prepare(model)` does any one-time setup and returns a handle. It
  must finish within ten minutes.
- `fastattr.attribute_fast(handle, prompt)` returns a graph with three fields:
  `adjacency_matrix`, `active_features` and `selected_features`.

The reference implementation is vendored in the candidate under
`src/circuit_tracer`. You may read it, borrow from it or restructure it. It is
also what the local correctness check compares against, so changing it weakens
your own check.

### How a run is measured

The grader measures ten axes: five model and prompt-length combinations (small
with 16 tokens, small with 48, large with 16, large with 32, and deep with 16),
each on CPU and on GPU.

For each prompt on an axis the grader makes one warm-up call and three timed
calls, and takes the median time. A call longer than 20 seconds is timed once.
The score of an axis is the number of graphs per second, multiplied by
`min(1, memory budget / peak memory)`, so exceeding the memory budget lowers the
score in proportion. The score for a device is the geometric mean over its five
axes. `task_score` is the CPU score plus the GPU score.

Every returned graph is compared with a stored reference graph. It must reach
0.99 on three measures: the overlap of active features, the overlap of selected
features, and the correlation of edge weights. A single graph that falls short
makes the whole candidate score zero. The candidate also scores zero if:

- the package does not import;
- `prepare` takes more than ten minutes;
- the measured process starts any other process;
- on CPU, the process's CPU time is less than 0.3 of its wall-clock time.

### Measuring locally

`/context/check` compares your graphs with the reference on a development pool
of prompts, using the same three measures. `/context/bench` prints a development
score computed the same way as the task score. Both are free and unlimited. The
graded prompts have the same shapes and lengths as the development pool and
different contents, so nothing tuned to the development prompts carries over.

Change one thing at a time and keep a log of what you tried and the development
score it produced. Three known inefficiencies in the starting library are memory
that grows with prompt length, an edge matrix that is fully materialised, and
the lack of support for multi-token outputs.

## Constraints

### Rules

- `attribute_fast` does not fork or start worker processes.
- Nothing is cached by prompt content. The same prompt seen twice is computed
  twice.
- The stored reference graphs are not edited or replaced.
- The candidate holds only the package and its tests. Environment setup, caches
  and scratch files live elsewhere.
- No packages are installed at run time. The candidate uses only what is
  already installed: PyTorch, TransformerLens and the reference tracer's
  dependencies.

### Environment

- The container has no network access.
- A GPU is available. CPU timing uses a single thread.
- The reference graphs and the local tools are read-only.
- The graded prompt pools exist only inside the grader.
- Each axis has its own memory budget, listed in `/context/budgets.json`.
