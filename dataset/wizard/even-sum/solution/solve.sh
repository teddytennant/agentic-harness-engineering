#!/bin/bash
# Reference solution (harbor's OracleAgent uses it to confirm the task is solvable).
set -e
mkdir -p /app
cat > /app/even_sum.py <<'PY'
def even_sum(nums):
    return sum(n for n in nums if n % 2 == 0)
PY
