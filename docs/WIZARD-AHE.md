# Running the AHE evolve loop on wizard

AHE used as an external lab tool to improve [`wizard`](https://github.com/teddytennant/wizard):
it runs wizard over a task set in Docker, analyzes failures, has a meta-model
rewrite wizard's base system prompt, and re-measures — producing a before/after
pass-rate. Wizard's native runtime loop is untouched.

Model: `DavidAU/Qwen3.6-27B-Heretic-Uncensored-FINETUNE-NEO-CODE-Di-IMatrix-MAX-GGUF`,
served by `llama-server` on the **GPU host (A100 80GB)**, used as both the
agent-under-test and the evolve-agent. Marginal cost ≈ $0 (own the GPU).

## What's wired (this branch)

- **wizard** (`feat/externalize-system-prompt`): loads its base prompt from
  `~/.wizard/system_prompt.md` / `$WIZARD_SYSTEM_PROMPT` when present — the surface
  this loop mutates. ✅ tested + pushed.
- **harbor adapter** `agents/wizard_agent/adapter.py` (+ `install-wizard.sh.j2`):
  uploads the wizard binary + the current candidate prompt into each task
  container, writes a `~/.wizard/config.toml` pointing at the host llama-server,
  runs `wizard -p`. Selected by import path.
- **evolve.py**: now emits `--agent-import-path` when `harbor.agent_import_path`
  is set, so the custom adapter is usable without touching harbor's `AgentName`
  enum.
- **config** `configs/experiments/exp-wizard.yaml`: 15-task set, k=2, 5 iters,
  debugger off (transcript-fed), evolve target = `agents/wizard_harness/system_prompt.md`.
- **scripts** `scripts/serve-qwen.sh` (serve the model), `scripts/evolve-wizard.sh`
  (run the loop).
- **dataset** `dataset/wizard/` — one worked example (`even-sum`) + `README.md`;
  author the rest from real wizard failure modes.

## Run it (on the GPU host)

```bash
# 0. Build wizard (in the wizard repo) and note the binary path
cargo build --release            # -> target/release/wizard
export WIZARD_BINARY=/abs/path/to/wizard/target/release/wizard

# 1. Serve the model (separate shell; stays running)
cd agentic-harness-engineering
./scripts/serve-qwen.sh
curl http://localhost:8080/v1/models     # confirm "qwen3.6-27b"

# 2. Point AHE at it
cp .env.example .env   # then edit:
#   LLM_BASE_URL=http://localhost:8080/v1
#   LLM_API_KEY=sk-noauth-local
uv sync

# 3. Smoke gate first — prove the wiring on the trivial task before the real run
uv run python evolve.py --config configs/experiments/exp-wizard-smoke.yaml --skip-eval=false
#   (a smoke overlay = exp-wizard.yaml with path=./dataset/local-sample,
#    max_iterations=1, k=1; create it by copying exp-wizard.yaml.)

# 4. Full run
WIZARD_BINARY=$WIZARD_BINARY ./scripts/evolve-wizard.sh
# baseline vs final: experiments/<run>/iteration_scores.yaml
# evolved prompt + history: experiments/<run>/workspace/system_prompt.md
```

Container → host networking (Linux): the adapter defaults the in-container
endpoint to `http://host.docker.internal:8080/v1`. harbor must launch task
containers with `--add-host=host.docker.internal:host-gateway` (or `--network
host`, then set `WIZARD_LLM_BASE_URL=http://localhost:8080/v1`). llama-server
binds `0.0.0.0` already.

## Option B: Grok via your xAI OAuth subscription (no API key)

wizard's `wizard --login xai` stores a Bearer token (`~/.wizard/xai_oauth.json`)
used against the OpenAI-compatible API at `https://api.x.ai/v1` (model `grok-4.3`).
`scripts/xai-oauth-proxy.py` reuses that session as a local OpenAI-compatible
endpoint — auto-refreshing the token — so **both** the evolve-agent and the
in-container wizard use Grok with no API key, on one port. The adapter needs no
change. This works on any box (no GPU needed; Grok is remote), so you can skip
`serve-qwen.sh` entirely.

```bash
# 0. one-time: sign in (interactive browser flow)
wizard --login xai

# 1. start the proxy (0.0.0.0 so containers can reach it; stays running)
cd agentic-harness-engineering
python3 scripts/xai-oauth-proxy.py          # -> http://0.0.0.0:8080/v1
curl -s http://localhost:8080/v1/models      # confirm Grok responds

# 2. point AHE + the adapter at the proxy
#   .env:   LLM_BASE_URL=http://localhost:8080/v1   LLM_API_KEY=unused
export WIZARD_LLM_BASE_URL=http://host.docker.internal:8080/v1   # for task containers
export WIZARD_BINARY=/abs/path/to/wizard/target/release/wizard

# 3. run (grok-4.3, n_concurrent=3 to respect subscription rate limits)
uv run python evolve.py --config configs/experiments/exp-wizard-xai.yaml
```

Caveats: OAuth API access is gated to certain SuperGrok plans (403 if yours lacks
it — fall back to `XAI_API_KEY` + `kind="xai"`); it's your personal subscription,
so keep `n_concurrent` low and be mindful that automated loop use differs from
interactive use; binding the proxy on `0.0.0.0` exposes a token-injecting endpoint
on your LAN (use `HOST=127.0.0.1` if only the host-side evolve-agent needs it, but
then containers can't reach it — use `--network host` for those instead).

For a smoke gate on this path, run the same command with
`path: ./dataset/local-sample`, `max_iterations: 1`, `k: 1` (copy
`exp-wizard-xai.yaml` and override those three).

## Open items (need a live gate run to finalize)

These could not be validated off-GPU and are the most likely things to need a
tweak during the first `hello-file`/`even-sum` gate:

1. **`code_agent_patch` vs markdown prompt.** AHE deep-merges `code_agent_patch`
   into `agent_config_filename` as YAML; our agent config is markdown
   (`system_prompt.md`). It's neutralized to `{}` in exp-wizard.yaml, but
   `evolve.py:apply_code_agent_patch` may still need a guard to skip the merge
   when the file isn't YAML. Verify on the first run.
2. **Container networking flag.** Confirm harbor's docker env actually applies
   `--add-host=...:host-gateway` (or switch to `--network host`). If the
   in-container `curl http://host.docker.internal:8080/v1/models` fails, this is why.
3. **Binary upload path.** The adapter uploads `$WIZARD_BINARY` via
   `environment.upload_file`; confirm the binary is glibc-compatible with the task
   base images (build static/musl if a task image is Alpine).
4. **Transcript → evolve evidence.** With the debugger off, confirm the evolve
   agent receives wizard's transcript (`/logs/agent/wizard.txt` →
   `trajectory.json`) as failure evidence; wire it into the evolution query if not.
5. **wizard `-p` + openai provider against a no-auth llama-server.** Confirm the
   `openai` provider tolerates the dummy key and that `-p` runs sovereign/headless
   as expected.

## Cost / scale

Larger run = ~15 tasks × k=2 × 5 iters = 150 rollouts + ~5 evolve passes.
Marginal cost ≈ $0 (own the A100; electricity only). Wall-clock a few hours to
~1 day at `n_concurrent: 6`. Optional: point only `evolve_agent.llm_config` at an
API model (e.g. GPT-5.5 via OpenRouter, ~$20–40 total) for stronger harness edits
while the high-volume base stays free on the GPU.
