#!/bin/bash
# Harbor's own test script and the sealed score: run on the held-out split.
set -eu
python /tests/grade.py sealed
