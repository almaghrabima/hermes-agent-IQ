import json

import tools.rlm_tool as rlm_tool


class _FakeEnv:
    pass


def _base_cfg(**over):
    cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS)
    cfg.update(over)
    return cfg


def test_api_mode_unchanged_uses_resolved_creds(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_load_rlm_config", lambda: _base_cfg(llm_mode="api"))
    monkeypatch.setattr(rlm_tool, "load_config_readonly",
                        lambda: {"model": {"provider": "openrouter", "default": "x"}})
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (_FakeEnv(), "local"))

    class FakeClient:
        base_url = "https://openrouter.ai/api/v1"
        api_key = "sk-test"

    monkeypatch.setattr(rlm_tool, "_resolve_api_key_provider", lambda: (FakeClient(), "aux"))

    captured = {}

    def fake_run(env, env_type, task_id, cfg, creds, context_text, timeout):
        captured["base_url"] = creds.base_url
        return {"result": "ok"}

    monkeypatch.setattr(rlm_tool, "_run_rlm_in_env", fake_run)
    out = json.loads(rlm_tool.rlm_tool(query="q", context="ctx"))
    assert out["status"] == "success"
    assert out["model_backend"] == "api"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"


def test_coding_agent_mode_points_driver_at_shim(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_load_rlm_config",
                        lambda: _base_cfg(llm_mode="coding_agent", coding_agent="openai-codex"))
    monkeypatch.setattr(rlm_tool, "load_config_readonly",
                        lambda: {"model": {"provider": "openai-codex", "default": "gpt-5.5"}})
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (_FakeEnv(), "local"))
    monkeypatch.setattr(rlm_tool, "_codex_authenticated", lambda model: True)

    captured = {}

    def fake_run(env, env_type, task_id, cfg, creds, context_text, timeout):
        captured["base_url"] = creds.base_url
        captured["api_key"] = creds.api_key
        captured["model"] = creds.primary_agent
        return {"result": "viacodex"}

    monkeypatch.setattr(rlm_tool, "_run_rlm_in_env", fake_run)
    out = json.loads(rlm_tool.rlm_tool(query="q", context="ctx"))
    assert out["status"] == "success"
    assert out["model_backend"] == "coding_agent:openai-codex"
    assert captured["base_url"].startswith("http://127.0.0.1:")
    assert captured["base_url"].endswith("/v1")
    assert captured["api_key"]  # the throwaway shim token
    assert captured["model"] == "gpt-5.5"


def test_coding_agent_mode_rejects_non_local_backend(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_load_rlm_config",
                        lambda: _base_cfg(llm_mode="coding_agent"))
    monkeypatch.setattr(rlm_tool, "load_config_readonly",
                        lambda: {"model": {"provider": "openai-codex", "default": "gpt-5.5"}})
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (_FakeEnv(), "docker"))
    monkeypatch.setattr(rlm_tool, "_codex_authenticated", lambda model: True)
    out = json.loads(rlm_tool.rlm_tool(query="q", context="ctx"))
    assert out["status"] == "error"
    assert "local backend" in out["error"]


def test_coding_agent_mode_errors_when_not_authenticated(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_load_rlm_config",
                        lambda: _base_cfg(llm_mode="coding_agent"))
    monkeypatch.setattr(rlm_tool, "load_config_readonly",
                        lambda: {"model": {"provider": "openai-codex", "default": "gpt-5.5"}})
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (_FakeEnv(), "local"))
    monkeypatch.setattr(rlm_tool, "_codex_authenticated", lambda model: False)
    out = json.loads(rlm_tool.rlm_tool(query="q", context="ctx"))
    assert out["status"] == "error"
    assert "coding agent" in out["error"].lower()


def test_bad_llm_mode_returns_rlm_error_not_generic(monkeypatch):
    """Fix A: a bad llm_mode value produces a clean RlmError, not a noisy stack trace."""
    monkeypatch.setattr(rlm_tool, "_load_rlm_config", lambda: _base_cfg(llm_mode="bogus"))
    monkeypatch.setattr(rlm_tool, "load_config_readonly",
                        lambda: {"model": {"provider": "openrouter", "default": "x"}})
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (_FakeEnv(), "local"))
    out = json.loads(rlm_tool.rlm_tool(query="q", context="ctx"))
    assert out["status"] == "error"
    assert "llm_mode" in out["error"]
    # Must NOT be the generic "rlm failed:" wrapper from the unexpected-exception handler
    assert not out["error"].startswith("rlm failed:")
