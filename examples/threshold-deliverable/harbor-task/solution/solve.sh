#!/bin/bash
set -eu
mkdir -p /app/output
echo '{"threshold": 0.62}' > /app/output/model.json
