#!/bin/bash
set -eu
python - <<'PY'
from pathlib import Path
values = Path('/workspace/numbers.txt').read_text().split()
Path('/workspace/answer.txt').write_text(str(sum(map(int, values))))
PY
