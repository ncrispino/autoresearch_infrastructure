#!/bin/bash
# The least-squares quadratic, rounded.
set -eu
mkdir -p /workspace/candidate
echo '{"coefficients": [1.9, 1.5, 0.5]}' > /workspace/candidate/coefficients.json
