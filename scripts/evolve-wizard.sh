#!/usr/bin/env bash
# Launch the wizard evolve loop. Thin wrapper over scripts/evolve.sh that checks
# the wizard-specific prerequisites, then runs exp-wizard.yaml in a tmux session.
#
# Prereqs (see docs/WIZARD-AHE.md):
#   - llama-server up on the GPU host  (scripts/serve-qwen.sh)
#   - .env has LLM_BASE_URL / LLM_API_KEY pointing at it
#   - WIZARD_BINARY -> host path of the release `wizard` binary
#   - Docker daemon running
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="${1:-configs/experiments/exp-wizard.yaml}"

: "${WIZARD_BINARY:?Set WIZARD_BINARY to the host path of the release wizard binary (cargo build --release)}"
[ -f "$WIZARD_BINARY" ] || { echo "WIZARD_BINARY not found: $WIZARD_BINARY" >&2; exit 1; }

# Default the in-container endpoint for the adapter if the caller didn't set it.
export WIZARD_LLM_BASE_URL="${WIZARD_LLM_BASE_URL:-http://host.docker.internal:8080/v1}"

echo "[evolve-wizard] WIZARD_BINARY=$WIZARD_BINARY"
echo "[evolve-wizard] WIZARD_LLM_BASE_URL=$WIZARD_LLM_BASE_URL"
echo "[evolve-wizard] config=$CONFIG"

exec "$SCRIPT_DIR/evolve.sh" --attach "$CONFIG"
