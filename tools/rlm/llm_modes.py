"""Resolve which model backend fast-rlm uses: direct API or a coding agent.

Pure: no I/O, no provider resolution, no shim. Given the merged ``rlm`` config
and the loaded Hermes config, decide ``"api"`` vs ``"coding_agent"``. The
orchestration in ``tools/rlm_tool.py`` acts on the decision.
"""

from __future__ import annotations

from dataclasses import dataclass

# Providers whose connection is a coding agent (not a plain OpenAI-compatible
# api-key endpoint) and therefore needs the local shim. Extension point for
# "others"; v1 ships openai-codex only.
CODING_AGENT_PROVIDERS = {"openai-codex"}

_VALID_MODES = {"auto", "api", "coding_agent"}


@dataclass
class CodingAgentSpec:
    provider: str
    model: str


def resolve_rlm_llm_mode(rlm_cfg: dict, config: dict) -> str:
    """Return ``"api"`` or ``"coding_agent"``.

    ``auto`` follows the provider chosen in setup (``config["model"]["provider"]``):
    a coding-agent provider -> ``coding_agent``; anything else -> ``api``.
    ``api`` / ``coding_agent`` force the branch regardless of the setup provider.
    Raises ``ValueError`` on an unknown mode or an unsupported coding agent.
    """
    mode = str(rlm_cfg.get("llm_mode", "auto") or "auto").strip()
    if mode not in _VALID_MODES:
        raise ValueError(
            f"rlm.llm_mode must be one of {sorted(_VALID_MODES)}, got {mode!r}"
        )

    if mode == "auto":
        provider = str((config.get("model") or {}).get("provider", "") or "").strip()
        mode = "coding_agent" if provider in CODING_AGENT_PROVIDERS else "api"

    if mode == "coding_agent":
        agent = str(rlm_cfg.get("coding_agent", "openai-codex") or "openai-codex").strip()
        if agent not in CODING_AGENT_PROVIDERS:
            raise ValueError(
                f"rlm.coding_agent {agent!r} is not a supported coding agent; "
                f"supported: {sorted(CODING_AGENT_PROVIDERS)}"
            )
    return mode
