"""Harbor adapter for the `wizard` CLI (https://github.com/teddytennant/wizard).

Lets AHE's evolve loop use wizard's *native* agent as the agent-under-test. The
loop evolves wizard's externalized base system prompt (the file wizard loads from
`~/.wizard/system_prompt.md`, added in wizard's `feat/externalize-system-prompt`);
this adapter ships the current candidate prompt into each task container and runs
`wizard -p "<instruction>"` headlessly. Harbor's own verifier (`tests/test.sh`)
scores the result — the adapter does not score.

Selected via import path (harbor `--agent-import-path`), so no edit to harbor's
closed `AgentName` enum is needed:

    harbor:
      agent_import_path: "agents.wizard_agent.adapter:WizardAgent"

Wiring contract (host env, read at run time):
  WIZARD_BINARY        host path to the release `wizard` binary to upload
                       (e.g. /home/gradient/projects/ai/wizard/target/release/wizard)
  WIZARD_LLM_BASE_URL  OpenAI-compatible base_url the in-container wizard talks to;
                       defaults to http://host.docker.internal:8080/v1 (the
                       llama-server on the GPU host; container must be launched
                       with --add-host=host.docker.internal:host-gateway, or use
                       --network host and http://localhost:8080/v1).

The evolved prompt path arrives as the `config_path` kwarg (harbor
`--ak config_path=<workspace>/system_prompt.md`, set by evolve.py from
`source_config_dir` + `agent_config_filename`).

NOTE: the end-to-end path (upload mechanics, host.docker.internal reachability,
wizard's openai-compatible provider against a no-auth llama-server) must be shaken
out with a live `hello-file` gate run on the GPU host — see docs/WIZARD-AHE.md.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# Where wizard reads its config and the evolved prompt inside the container.
CONTAINER_WIZARD_BIN = "/usr/local/bin/wizard"
CONTAINER_WIZARD_DIR = "/root/.wizard"
DEFAULT_BASE_URL = "http://host.docker.internal:8080/v1"
EVAL_API_KEY_ENV = "WIZARD_EVAL_API_KEY"


class WizardAgent(BaseInstalledAgent):
    """Run the wizard CLI headlessly as a harbor installed agent."""

    def __init__(self, config_path: str | None = None, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # The evolved system_prompt.md the loop is currently testing (host path).
        self._prompt_source = Path(config_path) if config_path else None

    @staticmethod
    def name() -> str:
        return "wizard"

    @property
    def _install_agent_template_path(self) -> Path:
        return Path(__file__).parent / "install-wizard.sh.j2"

    async def setup(self, environment: BaseEnvironment) -> None:
        """Upload the wizard binary + evolved prompt, then run the install template."""
        binary = os.environ.get("WIZARD_BINARY")
        if not binary or not Path(binary).is_file():
            raise FileNotFoundError(
                "Set WIZARD_BINARY to the host path of the release `wizard` binary "
                f"(got {binary!r}). Build it with `cargo build --release` in the "
                "wizard repo."
            )

        await environment.exec(command=f"mkdir -p {CONTAINER_WIZARD_DIR}")
        await environment.upload_file(
            source_path=Path(binary), target_path=CONTAINER_WIZARD_BIN
        )

        # Ship the current candidate prompt so wizard's runtime override picks it
        # up (~/.wizard/system_prompt.md). Absent → wizard uses its baked default,
        # which is the correct iteration-0 baseline.
        if self._prompt_source and self._prompt_source.is_file():
            await environment.upload_file(
                source_path=self._prompt_source,
                target_path=f"{CONTAINER_WIZARD_DIR}/system_prompt.md",
            )

        # install-wizard.sh.j2: chmod + `wizard --version` verify.
        await super().setup(environment)

    def _config_toml(self) -> str:
        base_url = os.environ.get("WIZARD_LLM_BASE_URL", DEFAULT_BASE_URL)
        model = self.model_name or os.environ.get("WIZARD_MODEL", "")
        # OpenAI-compatible provider → the llama-server on the GPU host. The
        # api_key is read from EVAL_API_KEY_ENV at runtime; llama-server ignores
        # it but wizard's openai provider wants a non-empty value.
        return (
            "mode = \"sovereign\"\n"
            "auto_approve = true\n"
            "max_steps = 50\n"
            "active_provider = \"eval\"\n\n"
            "[[providers]]\n"
            "name = \"eval\"\n"
            "kind = \"openai\"\n"
            f"base_url = \"{base_url}\"\n"
            f"model = \"{model}\"\n"
            f"api_key_env = \"{EVAL_API_KEY_ENV}\"\n"
        )

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        cfg = shlex.quote(self._config_toml())
        prompt = shlex.quote(instruction)
        env = {EVAL_API_KEY_ENV: os.environ.get(EVAL_API_KEY_ENV, "sk-noauth-local")}
        return [
            ExecInput(
                command=f"mkdir -p {CONTAINER_WIZARD_DIR} && "
                f"printf '%s' {cfg} > {CONTAINER_WIZARD_DIR}/config.toml",
            ),
            ExecInput(
                # Headless run; tee transcript so populate_context can read it and
                # so it survives as failure evidence for the evolve agent.
                command=f"{CONTAINER_WIZARD_BIN} -p {prompt} 2>&1 | tee /logs/agent/wizard.txt",
                env=env,
            ),
        ]

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Best-effort: surface wizard's JSONL session as the trajectory/evidence.

        wizard writes sessions to ~/.wizard/sessions/*.jsonl. Token accounting from
        a local llama-server is not cost-bearing, so we leave cost/token counts at
        their defaults and just expose the transcript for the transcript-fed evolve
        agent. Parsing the JSONL into ATIF is a follow-up (see docs/WIZARD-AHE.md).
        """
        sessions = self.logs_dir / "agent" / "wizard.txt"
        if not sessions.exists():
            return
        try:
            transcript = sessions.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        out = self.logs_dir / "trajectory.json"
        try:
            out.write_text(
                json.dumps({"agent": "wizard", "transcript": transcript}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
