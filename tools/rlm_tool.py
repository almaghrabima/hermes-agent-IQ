"""Recursive Language Model (fast-rlm) tool for Hermes.

Service-gated: its schema is only sent to the model when ``check_rlm_available``
returns True. Runs fast-rlm inside Hermes' active execution backend, reusing the
agent's active provider credentials. See
docs/superpowers/specs/2026-06-24-hermes-fast-rlm-tool-design.md.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
from dataclasses import dataclass

from tools.code_execution_tool import (
    _env_temp_dir,
    _get_or_create_env,
    _ship_file_to_remote,
)

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
    "executor": None,
    "executor_unsandboxed_ack": False,
    "kernel_sandbox": None,
    "kernel_runtime": None,
    "kernel_image": None,
    "kernel_network": None,
    "durable_max_attempts": 2,
}


class RlmError(Exception):
    """User-facing error from the rlm tool (returned as {'status':'error'})."""


def _temporal_enabled() -> bool:
    try:
        from plugins.temporal.tconfig import resolve_temporal_config
        from hermes_cli.config import load_config
        return bool(resolve_temporal_config(load_config()).enabled)
    except Exception:  # noqa: BLE001 — temporal not installed/configured
        return False


def _current_session_key() -> str:
    from tools.approval import get_current_session_key
    return get_current_session_key(default="default")


def _dispatch_durable_rlm(**kw) -> dict:
    from plugins.temporal.tools import dispatch_durable_rlm
    return dispatch_durable_rlm(**kw)


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
        logger.warning("rlm: provider resolution failed: %s", exc)
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
      1. fast_rlm already importable (covers a checkout the user installed themselves).
      2. rlm.engine_path set + exists -> ``pip install <engine_path>`` (non-editable) on first use.
      3. lazy_deps.ensure("tool.fast_rlm") against pinned PyPI.
    """
    import importlib.util

    if importlib.util.find_spec("fast_rlm") is not None:
        return True

    # (2) local checkout override (non-editable: an editable install surfaces
    # fast-rlm's fast_rlm.py/fast_rlm-package namespace clash and breaks import)
    try:
        from hermes_cli.config import load_config_readonly

        engine_path = (load_config_readonly().get("rlm", {}) or {}).get("engine_path")
    except Exception:
        engine_path = None
    if engine_path:
        import os

        if os.path.isdir(engine_path):
            try:
                from tools.lazy_deps import pip_install_path

                if pip_install_path(engine_path) and importlib.util.find_spec("fast_rlm") is not None:
                    return True
            except Exception as exc:
                logger.debug("fast-rlm install from %s failed: %s", engine_path, exc)

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
        "executor": rlm_cfg.get("executor"),
        "executor_unsandboxed_ack": rlm_cfg.get("executor_unsandboxed_ack", False),
        "kernel_sandbox": rlm_cfg.get("kernel_sandbox"),
        "kernel_runtime": rlm_cfg.get("kernel_runtime"),
        "kernel_image": rlm_cfg.get("kernel_image"),
        "kernel_network": rlm_cfg.get("kernel_network"),
    }


_CLOUD_BACKENDS = {"modal", "daytona"}

RLM_SCHEMA = {
    "name": "rlm",
    "description": (
        "Run a Recursive Language Model (fast-rlm) over a long/large context. The RLM "
        "drives a code REPL to explore, slice, and transform the context and can spawn "
        "recursive sub-agents whose results stay out of your context. Use this when a "
        "task spans far more text than is worth reading directly (huge logs, transcripts, "
        "document sets). Provide the task in `query` and the long content via `context` "
        "(inline) or `input_path` (a file in the workspace) — not both."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The task/question to run over the context."},
            "context": {"type": "string", "description": "Inline long text to load as the RLM prompt."},
            "input_path": {"type": "string", "description": "Path to a file in the active environment to load instead of `context`."},
            "primary_agent": {"type": "string", "description": "Override the RLM model (default: Hermes' active model)."},
            "sub_agent": {"type": "string", "description": "Override the sub-agent model (default: primary_agent)."},
            "max_global_calls": {"type": "integer", "description": "Override the RLM global call budget."},
            "durable": {"type": "boolean", "description": "Run as a crash-durable background Temporal workflow; returns a run_id and the result re-enters the session when done (requires temporal.enabled)."},
        },
        "required": ["query"],
    },
}


def rlm_tool(query, context=None, input_path=None, primary_agent=None,
             sub_agent=None, max_global_calls=None, task_id=None, durable=False) -> str:
    try:
        _validate_context_args(context, input_path)
        if durable:
            if not _temporal_enabled():
                raise RlmError(
                    "rlm durable=true requires temporal.enabled; see docs/temporal/. "
                    "Not falling back to a non-durable run.")
            rlm_cfg = _load_rlm_config()
            rlm_args = {
                "query": query, "context": context, "input_path": input_path,
                "primary_agent": primary_agent, "sub_agent": sub_agent,
                "max_global_calls": max_global_calls,
            }
            out = _dispatch_durable_rlm(
                rlm_args=rlm_args,
                session_key=_current_session_key(),
                max_attempts=int(rlm_cfg.get("durable_max_attempts", 2)),
                timeout_seconds=int(rlm_cfg.get("timeout_seconds", 600)),
            )
            return json.dumps(out, ensure_ascii=False)
        rlm_cfg = _load_rlm_config()
        if primary_agent is not None:
            rlm_cfg["primary_agent"] = primary_agent
        if sub_agent is not None:
            rlm_cfg["sub_agent"] = sub_agent
        if max_global_calls is not None:
            rlm_cfg["max_global_calls"] = max_global_calls

        env, env_type = _get_or_create_env(task_id or "default")
        if env_type in _CLOUD_BACKENDS and not rlm_cfg.get("allow_remote_backends"):
            raise RlmError(
                f"fast-rlm is disabled for the '{env_type}' cloud backend because your LLM "
                "key would transit to that sandbox. Set rlm.allow_remote_backends: true in "
                "config.yaml to allow it."
            )
        if rlm_cfg.get("kernel_sandbox") == "docker" and env_type != "local":
            raise RlmError(
                f"rlm.kernel_sandbox: docker requires the local Hermes backend, but the "
                f"active backend is '{env_type}'. Running the kernel container from a remote "
                "backend would require docker-in-docker, which is not supported yet."
            )

        creds = _resolve_rlm_credentials(rlm_cfg)
        cfg = _build_rlm_cfg(query, creds, rlm_cfg, context_path=None, input_path=input_path)
        out = _run_rlm_in_env(
            env, env_type, task_id or "default", cfg, creds,
            context_text=context, timeout=rlm_cfg.get("timeout_seconds", 600),
        )
        return json.dumps({"status": "success", **out}, ensure_ascii=False)
    except RlmError as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # unexpected
        logger.exception("rlm tool failed")
        return json.dumps({"status": "error", "error": f"rlm failed: {exc}"}, ensure_ascii=False)


from tools.registry import registry  # noqa: E402

registry.register(
    name="rlm",
    toolset="rlm",
    schema=RLM_SCHEMA,
    handler=lambda args, **kw: rlm_tool(
        query=args.get("query", ""),
        context=args.get("context"),
        input_path=args.get("input_path"),
        primary_agent=args.get("primary_agent"),
        sub_agent=args.get("sub_agent"),
        max_global_calls=args.get("max_global_calls"),
        task_id=kw.get("task_id"),
        durable=bool(args.get("durable", False)),
    ),
    check_fn=check_rlm_available,
    emoji="🔁",
    description="Recursive Language Model over long context (fast-rlm)",
)

_DRIVER_LOCAL_PATH = os.path.join(os.path.dirname(__file__), "rlm", "_driver.py")


def _run_rlm_in_env(env, env_type, task_id, cfg, creds: RlmCreds, context_text, timeout) -> dict:
    """Stage the driver + cfg (+ inline context) and run fast-rlm in *env*.

    The LLM key is written to a chmod-600 ``.env.sh`` that the run command
    sources and then removes — never on argv, never in cfg.json.
    """
    base = _env_temp_dir(env).rstrip("/")
    sandbox = f"{base}/hermes_rlm_{task_id}"
    env.execute(f"mkdir -p {shlex.quote(sandbox)}", cwd="/", timeout=30)

    driver_remote = f"{sandbox}/_driver.py"
    cfg_remote = f"{sandbox}/cfg.json"
    envfile_remote = f"{sandbox}/.env.sh"

    # 1) driver
    with open(_DRIVER_LOCAL_PATH, encoding="utf-8") as fh:
        _ship_file_to_remote(env, driver_remote, fh.read())

    # 2) inline context (if any) -> file; thread its path into cfg
    if context_text is not None:
        ctx_remote = f"{sandbox}/context.txt"
        _ship_file_to_remote(env, ctx_remote, context_text)
        cfg = {**cfg, "context_path": ctx_remote}

    # 3) cfg.json (no secrets)
    _ship_file_to_remote(env, cfg_remote, json.dumps(cfg, ensure_ascii=False))

    # 4) secret env file (sourced then deleted)
    env_lines = (
        f"export RLM_MODEL_API_KEY={shlex.quote(creds.api_key)}\n"
        f"export RLM_MODEL_BASE_URL={shlex.quote(creds.base_url)}\n"
    )
    _ship_file_to_remote(env, envfile_remote, env_lines)

    q = shlex.quote
    run_cmd = (
        f"chmod 600 {q(envfile_remote)}; . {q(envfile_remote)}; "
        f"python3 {q(driver_remote)} --config {q(cfg_remote)}; rc=$?; "
        f"rm -f {q(envfile_remote)}; exit $rc"
    )
    result = env.execute(run_cmd, cwd=sandbox, timeout=timeout)
    output = (result.get("output") or "").strip()
    last_line = output.splitlines()[-1] if output else ""
    try:
        parsed = json.loads(last_line)
    except (ValueError, IndexError):
        raise RlmError(f"fast-rlm produced no parseable result. Tail: {output[-500:]}")
    if "error" in parsed:
        raise RlmError(parsed["error"])
    return parsed
