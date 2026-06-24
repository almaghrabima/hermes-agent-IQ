import json

import tools.rlm_tool as rlm_tool


def test_check_rlm_available_true_when_deno_and_fastrlm(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_deno_available", lambda: True)
    monkeypatch.setattr(rlm_tool, "_fast_rlm_available", lambda: True)
    assert rlm_tool.check_rlm_available() is True


def test_check_rlm_available_false_when_no_deno(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_deno_available", lambda: False)
    monkeypatch.setattr(rlm_tool, "_fast_rlm_available", lambda: True)
    assert rlm_tool.check_rlm_available() is False


def test_check_rlm_available_false_when_no_fastrlm(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_deno_available", lambda: True)
    monkeypatch.setattr(rlm_tool, "_fast_rlm_available", lambda: False)
    assert rlm_tool.check_rlm_available() is False


def test_deno_available_uses_which(monkeypatch):
    monkeypatch.setattr(rlm_tool.shutil, "which", lambda name: "/usr/bin/deno" if name == "deno" else None)
    assert rlm_tool._deno_available() is True
    monkeypatch.setattr(rlm_tool.shutil, "which", lambda name: None)
    assert rlm_tool._deno_available() is False


def test_pip_install_editable_wraps_internal(monkeypatch):
    import tools.lazy_deps as lazy_deps

    class _R:
        success = True

    calls = []
    monkeypatch.setattr(lazy_deps, "_venv_pip_install", lambda specs, **kw: calls.append(specs) or _R())
    assert lazy_deps.pip_install_editable("/some/checkout") is True
    assert calls == [("-e /some/checkout",)]


def test_load_rlm_config_merges_defaults(monkeypatch):
    monkeypatch.setattr(
        rlm_tool, "load_config_readonly", lambda: {"rlm": {"max_global_calls": 7}}
    )
    cfg = rlm_tool._load_rlm_config()
    assert cfg["max_global_calls"] == 7          # user override
    assert cfg["allow_remote_backends"] is False  # default preserved
    assert cfg["timeout_seconds"] == 600          # default preserved


def test_resolve_credentials_uses_active_provider(monkeypatch):
    class FakeClient:
        base_url = "https://openrouter.ai/api/v1"
        api_key = "sk-test-123"

    monkeypatch.setattr(rlm_tool, "_resolve_api_key_provider", lambda: (FakeClient(), "auxmodel"))
    monkeypatch.setattr(rlm_tool, "load_config_readonly", lambda: {"model": "z-ai/glm-5"})

    creds = rlm_tool._resolve_rlm_credentials({"primary_agent": None, "sub_agent": None})
    assert creds.base_url == "https://openrouter.ai/api/v1"
    assert creds.api_key == "sk-test-123"
    assert creds.primary_agent == "z-ai/glm-5"   # active model, not aux model
    assert creds.sub_agent == "z-ai/glm-5"        # defaults to primary


def test_resolve_credentials_honors_overrides(monkeypatch):
    class FakeClient:
        base_url = "https://x/v1"
        api_key = "k"

    monkeypatch.setattr(rlm_tool, "_resolve_api_key_provider", lambda: (FakeClient(), "aux"))
    monkeypatch.setattr(rlm_tool, "load_config_readonly", lambda: {"model": "active"})

    creds = rlm_tool._resolve_rlm_credentials({"primary_agent": "p", "sub_agent": "s"})
    assert creds.primary_agent == "p"
    assert creds.sub_agent == "s"


def test_resolve_credentials_raises_without_key(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_resolve_api_key_provider", lambda: (None, None))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(rlm_tool, "load_config_readonly", lambda: {"model": "m"})
    try:
        rlm_tool._resolve_rlm_credentials({"primary_agent": None, "sub_agent": None})
        assert False, "expected RlmError"
    except rlm_tool.RlmError:
        pass


def test_validate_context_args_rejects_both():
    try:
        rlm_tool._validate_context_args("inline text", "/some/path")
        assert False, "expected RlmError"
    except rlm_tool.RlmError:
        pass


def test_validate_context_args_allows_one_or_none():
    rlm_tool._validate_context_args("inline", None)
    rlm_tool._validate_context_args(None, "/p")
    rlm_tool._validate_context_args(None, None)


def test_build_cfg_has_no_secrets():
    creds = rlm_tool.RlmCreds(base_url="b", api_key="SECRET", primary_agent="p", sub_agent="s")
    rlm_cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS)
    cfg = rlm_tool._build_rlm_cfg("q", creds, rlm_cfg, context_path="/tmp/ctx", input_path=None)
    assert cfg["query"] == "q"
    assert cfg["primary_agent"] == "p"
    assert cfg["sub_agent"] == "s"
    assert cfg["context_path"] == "/tmp/ctx"
    assert cfg["input_path"] is None
    assert cfg["max_global_calls"] == 50
    assert "SECRET" not in json.dumps(cfg)
    assert "api_key" not in cfg and "base_url" not in cfg
