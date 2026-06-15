#!/bin/bash
# Verifier for the even-sum task. Reward 1.0 iff /app/even_sum.py implements
# even_sum correctly across the cases below. Reward -> /logs/verifier/reward.txt.
mkdir -p /logs/verifier

reward=0
if [ -f /app/even_sum.py ]; then
  if python3 - <<'PY'
import sys
sys.path.insert(0, "/app")
try:
    from even_sum import even_sum
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

cases = [
    ([], 0),
    ([1, 2, 3, 4], 6),
    ([-4, -3, 1], -4),
    ([2, 4, 6], 12),
    ([1, 3, 5], 0),
]
for nums, expected in cases:
    src = list(nums)
    got = even_sum(nums)
    if got != expected:
        print(f"FAIL even_sum({nums}) = {got}, expected {expected}")
        sys.exit(1)
    if nums != src:
        print(f"FAIL even_sum mutated its input: {nums} != {src}")
        sys.exit(1)
print("all cases passed")
PY
  then
    reward=1
  fi
fi

echo "$reward" > /logs/verifier/reward.txt
echo "[verifier] even-sum -> reward=$reward"
