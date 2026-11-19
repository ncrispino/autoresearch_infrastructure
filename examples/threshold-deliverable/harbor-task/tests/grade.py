"""Score /workspace/candidate/model.json on one hidden split and write Harbor's reward file."""

import csv
import json
import sys
from pathlib import Path

split = sys.argv[1]
accuracy = 0.0
try:
    model = json.loads(Path("/workspace/candidate/model.json").read_text())
    threshold = model["threshold"]
    if set(model) == {"threshold"} and type(threshold) in (int, float) and 0 <= threshold <= 1:
        with open(f"/tests/{split}.csv", newline="") as handle:
            points = list(csv.DictReader(handle))
        right = sum((float(point["x"]) > threshold) == (point["label"] == "1") for point in points)
        accuracy = right / len(points)
except (OSError, ValueError, KeyError, TypeError) as error:
    print("no score:", type(error).__name__)
Path("/logs/verifier/reward.txt").write_text(str(accuracy))
print(f"{split} accuracy {accuracy:.3f}")
