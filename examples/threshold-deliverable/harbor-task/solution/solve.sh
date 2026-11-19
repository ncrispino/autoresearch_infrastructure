#!/bin/bash
set -eu
mkdir -p /workspace/candidate
echo '{"threshold": 0.62}' > /workspace/candidate/model.json
