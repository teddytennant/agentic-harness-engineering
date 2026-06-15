#!/usr/bin/env bash
# Turnkey runner for the wizard evolve loop on Grok via xAI OAuth.
#
# One-time: `wizard --login xai`. Then this script does everything else:
#   - starts the OAuth proxy on a free port (background; reused if already up)
#   - writes the LLM_* keys into .env (evolve.py load_dotenv override=True)
#   - exports the in-container endpoint for the wizard adapter
#   - runs ROUNDS evolve rounds back-to-back, logging each to .wizard-xai-runs/
#
# Env knobs:
#   CONFIG   experiment config        (default: configs/experiments/exp-wizard-xai.yaml)
#   ROUNDS   how many runs to do      (default: 1)
#   PROXY_PORT  proxy port            (default: auto-pick a free port)
#   WIZARD_BINARY  host wizard binary (default: ../wizard/target/release/wizard)
#
# NOTE: the OAuth api:access path is METERED on your xAI account. A full run
# (~150 rollouts x 5 iters) is on the order of hundreds of dollars; the smoke
# config is ~$1-5. Set CONFIG=...-smoke.yaml first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

CONFIG="${CONFIG:-configs/experiments/exp-wizard-xai.yaml}"
ROUNDS="${ROUNDS:-1}"
MODEL="${MODEL:-grok-4.3}"
RUNDIR="$PROJECT_ROOT/.wizard-xai-runs"
mkdir -p "$RUNDIR"

# --- login guard ---
TOKEN="${XAI_OAUTH_PATH:-$HOME/.wizard/xai_oauth.json}"
[ -f "$TOKEN" ] || { echo "Not signed in to xAI. Run: wizard --login xai" >&2; exit 3; }

# --- wizard binary ---
: "${WIZARD_BINARY:=$PROJECT_ROOT/../wizard/target/release/wizard}"
if [ ! -x "$WIZARD_BINARY" ]; then
  echo "WIZARD_BINARY not found/executable: $WIZARD_BINARY" >&2
  echo "Build it: (cd ../wizard && cargo build --release)" >&2
  exit 4
fi
export WIZARD_BINARY

# --- pick a free port for the proxy ---
if [ -z "${PROXY_PORT:-}" ]; then
  for p in 8088 8090 8123 9099 8080; do
    if ! ss -ltn 2>/dev/null | grep -q ":$p "; then PROXY_PORT="$p"; break; fi
  done
fi
: "${PROXY_PORT:=8088}"

# --- ensure the proxy is up ---
if ! curl -sf "http://127.0.0.1:$PROXY_PORT/v1/models" >/dev/null 2>&1; then
  echo "[run] starting xai-oauth-proxy on :$PROXY_PORT"
  HOST=0.0.0.0 PORT="$PROXY_PORT" nohup python3 "$SCRIPT_DIR/xai-oauth-proxy.py" \
    > "$RUNDIR/proxy.log" 2>&1 &
  echo $! > "$RUNDIR/proxy.pid"
  curl -s --retry 30 --retry-connrefused --retry-delay 1 \
    "http://127.0.0.1:$PROXY_PORT/v1/models" >/dev/null \
    || { echo "[run] proxy did not come up; see $RUNDIR/proxy.log" >&2; exit 5; }
fi
echo "[run] proxy live on :$PROXY_PORT"

# Unify the LLM address on the docker bridge gateway so it's reachable from BOTH
# the host (evolve-agent) and the task container (wizard), AND so harbor — which
# whitelists the host parsed from LLM_BASE_URL when it restricts container egress
# via iptables — whitelists the same address wizard actually calls. harbor runs
# allow_internet tasks with network_mode=bridge (no host.docker.internal), so the
# gateway is the reachable host address from inside the container.
DOCKER_GW="$(docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)"
[ -n "$DOCKER_GW" ] || DOCKER_GW="172.17.0.1"
LLM_URL="http://$DOCKER_GW:$PROXY_PORT/v1"

# --- wire AHE (.env is loaded with override=True, so set it there) ---
touch .env
# Replace or append the LLM_ keys without clobbering other .env entries.
grep -vE '^(LLM_BASE_URL|LLM_API_KEY|LLM_MODEL)=' .env > .env.tmp 2>/dev/null || true
{
  echo "LLM_BASE_URL=$LLM_URL"
  echo "LLM_API_KEY=oauth-via-proxy"
  echo "LLM_MODEL=$MODEL"
} >> .env.tmp
mv .env.tmp .env
export WIZARD_LLM_BASE_URL="$LLM_URL"

# harbor imports the adapter by path (agents.wizard_agent.adapter); put the AHE
# project root on PYTHONPATH so that import resolves in the harbor subprocess.
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# evolve.py spawns the `harbor` CLI as a subprocess; make sure the venv bin is on
# PATH so it resolves even when uv doesn't propagate it to the child env.
if [ -d "$PROJECT_ROOT/.venv/bin" ]; then
  export PATH="$PROJECT_ROOT/.venv/bin:$PATH"
fi

# NixOS: harbor's native deps (litellm -> tokenizers) need libstdc++.so.6 on the
# loader path, which isn't there by default. Point LD_LIBRARY_PATH at a gcc lib.
if ! echo "${LD_LIBRARY_PATH:-}" | grep -q 'gcc.*-lib'; then
  for d in /nix/store/*-gcc-*-lib/lib; do
    if [ -e "$d/libstdc++.so.6" ]; then
      export LD_LIBRARY_PATH="$d${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
      break
    fi
  done
fi

echo "[run] CONFIG=$CONFIG ROUNDS=$ROUNDS WIZARD_BINARY=$WIZARD_BINARY"
echo "[run] WIZARD_LLM_BASE_URL=$WIZARD_LLM_BASE_URL"

# --- run the rounds ---
for r in $(seq 1 "$ROUNDS"); do
  log="$RUNDIR/round-$(date +%Y%m%d-%H%M%S)-$r.log"
  echo "===== round $r/$ROUNDS  $(date)  -> $log ====="
  if uv run python evolve.py --config "$CONFIG" 2>&1 | tee "$log"; then
    echo "[run] round $r ok"
  else
    echo "[run] round $r FAILED (see $log)" >&2
  fi
done
echo "[run] done."
