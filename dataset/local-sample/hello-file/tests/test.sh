#!/bin/bash
# Verifier for the hello-file task.
# Reward 1.0 iff /app/solution.txt exists and contains the word "hello".
# Reward is written to /logs/verifier/reward.txt (harbor reads it from there).

mkdir -p /logs/verifier

if [ -f /app/solution.txt ] && [ "$(tr -d '[:space:]' < /app/solution.txt)" = "hello" ]; then
  reward=1
else
  reward=0
fi

echo "$reward" > /logs/verifier/reward.txt
echo "[verifier] /app/solution.txt -> reward=$reward"
