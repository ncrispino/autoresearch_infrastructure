#!/bin/bash
set -eu
python - <<'PY'
from pathlib import Path
import json
numbers = Path('/workspace/numbers.txt')
answer = Path('/workspace/answer.txt')
passed = answer.exists() and answer.read_text().strip() == str(sum(map(int, numbers.read_text().split())))
Path('/logs/verifier/reward.txt').write_text('1' if passed else '0')
print(json.dumps({'numbers': numbers.read_text().strip(),
                  'answer': answer.read_text().strip() if answer.exists() else None,
                  'passed': passed}))
PY
