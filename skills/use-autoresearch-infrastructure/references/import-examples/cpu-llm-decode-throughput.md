# Serve a concurrent generation workload faster

## Objective

Improve `/workspace/candidate/engine.py` so that it serves a concurrent text
generation workload faster than the reference engine, while returning the same
greedy token ids.

### Interface

`engine.py` defines one class. The grader constructs it once and then calls it
with a batch of requests.

- `__init__(self, model_dir: str)` loads the model from `model_dir`.
- `generate(self, requests: list[dict]) -> list[list[int]]` returns one list of
  new token ids for each request, in the same order.

Each request is a dictionary with three fields:

| Field | Meaning |
| --- | --- |
| `id` | An identifier for the request. |
| `prompt_token_ids` | Between 16 and 160 token ids. |
| `max_new_tokens` | Between 4 and 64. |

For each request, return exactly `max_new_tokens` token ids: the greedy
continuation of the prompt. Do not stop early, pad, or add anything else.

### How a run is measured

`speedup` is the median, over three rounds, of your engine's throughput divided
by the reference engine's throughput on the same workload. The reference engine
submitted unchanged scores nothing. There is no upper limit.

Each round runs your engine in a fresh process on 8 CPU cores. Constructing the
engine and warming it up may take up to 180 seconds and is not timed. Each round
uses a different workload, which your engine cannot read before the clock starts.

A round scores nothing if any of these holds:

- The output is not a list of the right length, or a list has the wrong number
  of tokens or a token id outside the vocabulary.
- Fewer than 99% of the returned tokens agree with the reference engine's.
- The timed run takes more than twice as long as the reference engine's, with a
  minimum allowance of 60 seconds.
- Construction and warm-up take more than 180 seconds.

### Measuring locally

`/context/bench.py` runs a development workload against your engine and prints
its throughput and its agreement with the reference. Use it as often as you
like before submitting. The development workloads have the same shape as the
graded ones and different contents.

## Constraints

### Rules

- The engine computes every output from the model at run time. It does not
  replay stored outputs or recognise particular prompts.
- The engine does all of its work inside the process the grader starts. It does
  not start other processes or contact other machines.
- The candidate contains only `engine.py` and modules it imports. It contains no
  model weights, caches or logs.
- The engine uses only PyTorch, NumPy, safetensors and the Python standard
  library.

### Environment

- 8 CPU cores and 14 GiB of memory. There is no GPU.
- There is no network access.
- The grader runs your engine as an unprivileged user from a read-only copy of
  the candidate, with its own scratch home and temporary directories.
- The graded workloads exist only inside the grader.
