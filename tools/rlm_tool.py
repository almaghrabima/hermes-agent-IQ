"""Recursive Language Model (fast-rlm) tool for Hermes.

Service-gated: its schema is only sent to the model when ``check_rlm_available``
returns True. Runs fast-rlm inside Hermes' active execution backend, reusing the
agent's active provider credentials. See
docs/superpowers/specs/2026-06-24-hermes-fast-rlm-tool-design.md.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_RLM_CONFIG_DEFAULTS = {
    "primary_agent": None,
    "sub_agent": None,
    "max_global_calls": 50,
    "max_money_spent": None,
    "max_completion_tokens": None,
    "timeout_seconds": 600,
    "allow_remote_backends": False,
    "engine_path": None,
}


class RlmError(Exception):
    """User-facing error from the rlm tool (returned as {'status':'error'})."""


@dataclass
class RlmCreds:
    base_url: str
    api_key: str
    primary_agent: str
    sub_agent: str


def load_config_readonly():
    """Indirection so tests can monkeypatch; delegates to hermes_cli.config."""
    from hermes_cli.config import load_config_readonly as _impl

    return _impl()


def _load_rlm_config() -> dict:
    """Return the rlm: config block merged over defaults."""
    user = (load_config_readonly().get("rlm", {}) or {})
    merged = dict(_RLM_CONFIG_DEFAULTS)
    for key, value in user.items():
        if key in merged:
            merged[key] = value
    return merged


def _resolve_api_key_provider():
    """Indirection so tests can monkeypatch; delegates to auxiliary_client."""
    from agent.auxiliary_client import _resolve_api_key_provider as _impl

    return _impl()


def _resolve_rlm_credentials(rlm_cfg: dict) -> RlmCreds:
    """Map Hermes' active provider/model to fast-rlm credentials.

    base_url/api_key come from Hermes' active API-key provider (the same
    resolver the auxiliary client uses). primary_agent defaults to Hermes'
    active model (config["model"]); overrides from rlm_cfg win.
    """
    config = load_config_readonly()
    active_model = str(config.get("model", "") or "").strip()

    base_url = ""
    api_key = ""
    try:
        client, _aux_model = _resolve_api_key_provider()
    except Exception as exc:
        logger.debug("rlm: provider resolution failed: %s", exc)
        client = None
    if client is not None:
        base_url = str(getattr(client, "base_url", "") or "").strip()
        api_key = str(getattr(client, "api_key", "") or "").strip()

    # Fallback to env for OpenAI-compatible setups not covered by the resolver.
    if not api_key:
        api_key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not base_url:
        base_url = (os.environ.get("RLM_MODEL_BASE_URL") or "https://openrouter.ai/api/v1").strip()

    primary = (rlm_cfg.get("primary_agent") or active_model or "").strip()
    if not api_key:
        raise RlmError(
            "No LLM API key available for fast-rlm. Configure an API-key provider "
            "in Hermes, or set rlm.primary_agent and OPENAI_API_KEY/OPENROUTER_API_KEY."
        )
    if not primary:
        raise RlmError(
            "No model for fast-rlm. Set a Hermes model or rlm.primary_agent in config.yaml."
        )
    sub = (rlm_cfg.get("sub_agent") or primary).strip()
    return RlmCreds(base_url=base_url, api_key=api_key, primary_agent=primary, sub_agent=sub)


def _deno_available() -> bool:
    """True when the Deno runtime (required by fast-rlm) is on PATH."""
    return shutil.which("deno") is not None


def _fast_rlm_available() -> bool:
    """True when fast_rlm is importable, or lazily installable.

    Resolution order:
      1. fast_rlm already importable (covers an editable ``pip install -e`` checkout).
      2. rlm.engine_path set + exists -> ``pip install -e <engine_path>`` on first use.
      3. lazy_deps.ensure("tool.fast_rlm") against pinned PyPI.
    """
    import importlib.util

    if importlib.util.find_spec("fast_rlm") is not None:
        return True

    # (2) editable checkout override
    try:
        from hermes_cli.config import load_config_readonly

        engine_path = (load_config_readonly().get("rlm", {}) or {}).get("engine_path")
    except Exception:
        engine_path = None
    if engine_path:
        import os

        if os.path.isdir(engine_path):
            try:
                from tools.lazy_deps import pip_install_editable

                if pip_install_editable(engine_path) and importlib.util.find_spec("fast_rlm") is not None:
                    return True
            except Exception as exc:
                logger.debug("editable fast-rlm install from %s failed: %s", engine_path, exc)

    # (3) pinned PyPI
    try:
        from tools.lazy_deps import FeatureUnavailable, ensure

        ensure("tool.fast_rlm", prompt=False)
        return importlib.util.find_spec("fast_rlm") is not None
    except Exception as exc:  # FeatureUnavailable or install failure
        logger.debug("fast-rlm lazy install unavailable: %s", exc)
        return False


def check_rlm_available() -> bool:
    """Availability gate for the rlm tool schema (TTL-cached by the registry)."""
    return _deno_available() and _fast_rlm_available()


def _validate_context_args(context, input_path) -> None:
    if context and input_path:
        raise RlmError("Provide either `context` or `input_path`, not both.")


def _build_rlm_cfg(query, creds: RlmCreds, rlm_cfg: dict, context_path, input_path) -> dict:
    """Build the cfg.json the driver reads. Contains NO secrets (key is env-only)."""
    return {
        "query": query,
        "primary_agent": creds.primary_agent,
        "sub_agent": creds.sub_agent,
        "context_path": context_path,
        "input_path": input_path,
        "max_global_calls": rlm_cfg.get("max_global_calls", 50),
        "max_money_spent": rlm_cfg.get("max_money_spent"),
        "max_completion_tokens": rlm_cfg.get("max_completion_tokens"),
    }
