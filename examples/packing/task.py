"""Choose items to pack: maximize their value without exceeding a weight limit.

The candidate is ``items.json``, a list of item names from the catalog below.
"""

import json

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.persistence import write_json

CATALOG = {"map": (2, 3), "water": (3, 6), "book": (4, 4), "food": (2, 5)}  # name: (weight, value)
WEIGHT_LIMIT = 7
INSTRUCTIONS = """\
## Objective
Improve `items.json`, a JSON list of item names from `/context/catalog.json`, so
that the packed items are worth more. `value` is their total value and `weight`
their total weight.

## Constraints
### Rules
- List each item at most once.
- Use only names from the catalog.
"""

VALUE = ari.Metric("value")
# A candidate over the limit is still measured, but it can never be selected.
WEIGHT = ari.Metric("weight", "min", maximum=WEIGHT_LIMIT)


def validity(candidate, context):
    try:
        names = json.loads((candidate / "items.json").read_text())
    except (ValueError, OSError):
        return ari.Validity("invalid", ("Unreadable item list",))
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        return ari.Validity("invalid", ("Items must be a JSON list of names",))
    if len(names) != len(set(names)) or set(names) - set(CATALOG):
        return ari.Validity("invalid", ("Items must be distinct catalog entries",))
    return ari.Validity()


def evaluate(candidate, context):
    names = json.loads((candidate / "items.json").read_text())
    weight = sum(CATALOG[name][0] for name in names)
    value = sum(CATALOG[name][1] for name in names)
    write_json(context.artifacts / "calculation.json", {"items": names, "weight": weight, "value": value})
    return ari.Evaluation({VALUE: value, WEIGHT: weight})


def task(run):
    return ari.Task(
        objective=ari.Objective(
            VALUE, tie_breakers=(WEIGHT,), description="Maximize packed value under a weight limit"
        ),
        evaluator=evaluate,
        instructions=INSTRUCTIONS,
        validity=validity,
        context={"catalog.json": {"items": CATALOG, "weight_limit": WEIGHT_LIMIT}},
    )
