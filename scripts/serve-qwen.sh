#!/usr/bin/env bash
# Serve the agent-under-test model with an OpenAI-compatible API via llama.cpp,
# for the AHE wizard evolve loop. Run this on the GPU host (A100 80GB).
#
# GGUF is llama.cpp's native format (not vLLM's). With 80GB, a high-fidelity
# quant (Q6_K ~22GB / Q8_0 ~29GB) leaves ample room for KV cache. The A100 is
# Ampere (no native FP8); GGUF is unaffected.
#
# After it's up:
#   curl http://localhost:8080/v1/models
# The evolve-agent (on the host) uses LLM_BASE_URL=http://localhost:8080/v1;
# task containers reach it at http://host.docker.internal:8080/v1 (launch them
# with --add-host=host.docker.internal:host-gateway, or --network host).
set -euo pipefail

MODEL_REPO="${MODEL_REPO:-DavidAU/Qwen3.6-27B-Heretic-Uncensored-FINETUNE-NEO-CODE-Di-IMatrix-MAX-GGUF}"
QUANT="${QUANT:-Q6_K}"        # confirm the tag exists in the repo; else use -m <path>
ALIAS="${ALIAS:-qwen3.6-27b}" # must match the `model` in exp-wizard.yaml
CTX="${CTX:-100000}"
PORT="${PORT:-8080}"

exec llama-server \
  -hf "${MODEL_REPO}:${QUANT}" \
  --alias "${ALIAS}" \
  -ngl 99 \
  -c "${CTX}" \
  --host 0.0.0.0 --port "${PORT}" \
  --jinja
