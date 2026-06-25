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


def test_pip_install_path_wraps_internal(monkeypatch):
    import tools.lazy_deps as lazy_deps

    class _R:
        success = True

    calls = []
    monkeypatch.setattr(lazy_deps, "_venv_pip_install", lambda specs, **kw: calls.append(specs) or _R())
    # Non-editable: the path is passed as-is (no "-e ") so fast-rlm's
    # fast_rlm.py/fast_rlm-package namespace clash doesn't surface.
    assert lazy_deps.pip_install_path("/some/checkout") is True
    assert calls == [("/some/checkout",)]


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


class _FakeEnv:
    """Records shipped files and the executed command."""
    def __init__(self):
        self.shipped = {}
        self.commands = []

    def get_temp_dir(self):
        return "/tmp"

    def execute(self, command, cwd="", timeout=None, **kw):
        self.commands.append(command)
        # Simulate the driver's stdout for the run command.
        if "_driver.py" in command:
            return {"output": '{"result": "ok", "usage": {"calls": 2}, "log_path": "/tmp/r.jsonl"}\n',
                    "returncode": 0}
        return {"output": "", "returncode": 0}


def _patch_staging(monkeypatch, env):
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (env, "local"))

    def fake_ship(e, path, content):
        e.shipped[path] = content

    monkeypatch.setattr(rlm_tool, "_ship_file_to_remote", fake_ship)
    monkeypatch.setattr(rlm_tool, "_env_temp_dir", lambda e: "/tmp")


def test_run_in_env_stages_files_and_parses(monkeypatch):
    env = _FakeEnv()
    _patch_staging(monkeypatch, env)
    creds = rlm_tool.RlmCreds(base_url="https://b/v1", api_key="SECRET", primary_agent="p", sub_agent="s")
    cfg = {"query": "q", "primary_agent": "p", "sub_agent": "s", "context_path": None,
           "input_path": None, "max_global_calls": 50, "max_money_spent": None,
           "max_completion_tokens": None}

    out = rlm_tool._run_rlm_in_env(env, "local", "task1", cfg, creds, context_text=None, timeout=600)

    assert out["result"] == "ok"
    assert out["usage"]["calls"] == 2
    # driver + cfg staged
    staged = "\n".join(env.shipped)
    assert any(p.endswith("_driver.py") for p in env.shipped)
    assert any(p.endswith("cfg.json") for p in env.shipped)
    # secret is NOT in cfg.json content, but IS in the sourced env file
    cfg_content = next(c for p, c in env.shipped.items() if p.endswith("cfg.json"))
    assert "SECRET" not in cfg_content
    env_file = next(c for p, c in env.shipped.items() if p.endswith(".env.sh"))
    assert "RLM_MODEL_API_KEY=SECRET" in env_file
    assert "RLM_MODEL_BASE_URL=https://b/v1" in env_file
    # run command sources the env file and removes it
    run_cmd = next(c for c in env.commands if "_driver.py" in c)
    assert ".env.sh" in run_cmd and "rm -f" in run_cmd
    # env file must be locked down (chmod 600) before sourcing
    assert "chmod 600" in run_cmd
    # env file must be sourced (POSIX dot or bash source)
    assert (". " in run_cmd or "source " in run_cmd)


def test_run_in_env_ships_inline_context(monkeypatch):
    env = _FakeEnv()
    _patch_staging(monkeypatch, env)
    creds = rlm_tool.RlmCreds(base_url="b", api_key="k", primary_agent="p", sub_agent="s")
    cfg = {"query": "q", "primary_agent": "p", "sub_agent": "s", "context_path": "/tmp/PLACEHOLDER",
           "input_path": None, "max_global_calls": 50, "max_money_spent": None, "max_completion_tokens": None}

    rlm_tool._run_rlm_in_env(env, "local", "task1", cfg, creds, context_text="my big context", timeout=600)
    ctx_files = [c for p, c in env.shipped.items() if "context" in p]
    assert ctx_files and ctx_files[0] == "my big context"
    # cfg.json must reference the staged remote path, not the original PLACEHOLDER
    cfg_content = next(c for p, c in env.shipped.items() if p.endswith("cfg.json"))
    assert "PLACEHOLDER" not in cfg_content
    assert "context.txt" in cfg_content


import json as _json


def test_rlm_tool_rejects_both_context_and_path(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_load_rlm_config", lambda: dict(rlm_tool._RLM_CONFIG_DEFAULTS))
    out = _json.loads(rlm_tool.rlm_tool(query="q", context="a", input_path="/p", task_id="t"))
    assert out["status"] == "error"
    assert "not both" in out["error"]


def test_rlm_tool_blocks_cloud_backend_when_gate_off(monkeypatch):
    cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS)
    cfg["allow_remote_backends"] = False
    monkeypatch.setattr(rlm_tool, "_load_rlm_config", lambda: cfg)
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (object(), "modal"))
    out = _json.loads(rlm_tool.rlm_tool(query="q", task_id="t"))
    assert out["status"] == "error"
    assert "allow_remote_backends" in out["error"]


def test_rlm_tool_happy_path(monkeypatch):
    cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS)
    monkeypatch.setattr(rlm_tool, "_load_rlm_config", lambda: cfg)
    fake_env = object()
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (fake_env, "local"))
    monkeypatch.setattr(
        rlm_tool, "_resolve_rlm_credentials",
        lambda c: rlm_tool.RlmCreds(base_url="b", api_key="k", primary_agent="p", sub_agent="s"),
    )

    captured = {}

    def fake_run(env, env_type, task_id, cfg_, creds, context_text, timeout):
        captured["cfg"] = cfg_
        captured["context_text"] = context_text
        return {"result": "done", "usage": {"calls": 3}, "log_path": "/l"}

    monkeypatch.setattr(rlm_tool, "_run_rlm_in_env", fake_run)

    out = _json.loads(rlm_tool.rlm_tool(query="count rs", context="big text", task_id="t"))
    assert out["status"] == "success"
    assert out["result"] == "done"
    assert out["usage"]["calls"] == 3
    assert out["log_path"] == "/l"
    assert captured["context_text"] == "big text"
    assert captured["cfg"]["query"] == "count rs"


def test_rlm_schema_shape():
    assert rlm_tool.RLM_SCHEMA["name"] == "rlm"
    props = rlm_tool.RLM_SCHEMA["parameters"]["properties"]
    assert "query" in props and "context" in props and "input_path" in props
    assert rlm_tool.RLM_SCHEMA["parameters"]["required"] == ["query"]


def test_rlm_registered_and_gated():
    from tools.registry import registry
    entry = registry.get_entry("rlm")
    assert entry is not None
    assert entry.check_fn is rlm_tool.check_rlm_available


def test_rlm_not_in_core_tools():
    import toolsets
    assert "rlm" not in toolsets._HERMES_CORE_TOOLS
    assert "rlm" in toolsets.TOOLSETS


def test_build_cfg_includes_executor_kernel_keys():
    creds = rlm_tool.RlmCreds(base_url="b", api_key="k", primary_agent="p", sub_agent="s")
    rlm_cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS)
    rlm_cfg.update({"executor": "subprocess", "kernel_sandbox": "docker",
                    "kernel_runtime": "runc", "kernel_image": "python:3.11-slim",
                    "kernel_network": "none"})
    cfg = rlm_tool._build_rlm_cfg("q", creds, rlm_cfg, context_path=None, input_path=None)
    assert cfg["executor"] == "subprocess"
    assert cfg["kernel_sandbox"] == "docker"
    assert cfg["kernel_runtime"] == "runc"
    assert cfg["kernel_image"] == "python:3.11-slim"
    assert cfg["kernel_network"] == "none"
    assert cfg["executor_unsandboxed_ack"] is False


def test_build_cfg_defaults_executor_keys_none():
    creds = rlm_tool.RlmCreds(base_url="b", api_key="k", primary_agent="p", sub_agent="s")
    cfg = rlm_tool._build_rlm_cfg("q", creds, dict(rlm_tool._RLM_CONFIG_DEFAULTS),
                                  context_path=None, input_path=None)
    assert cfg["executor"] is None
    assert cfg["kernel_sandbox"] is None


def test_rlm_tool_blocks_docker_kernel_on_nonlocal_backend(monkeypatch):
    cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS); cfg["kernel_sandbox"] = "docker"
    monkeypatch.setattr(rlm_tool, "_load_rlm_config", lambda: cfg)
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (object(), "docker"))
    out = __import__("json").loads(rlm_tool.rlm_tool(query="q", task_id="t"))
    assert out["status"] == "error"
    assert "local Hermes backend" in out["error"]


def test_rlm_tool_allows_docker_kernel_on_local_backend(monkeypatch):
    cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS); cfg["kernel_sandbox"] = "docker"
    monkeypatch.setattr(rlm_tool, "_load_rlm_config", lambda: cfg)
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (object(), "local"))
    monkeypatch.setattr(rlm_tool, "_resolve_rlm_credentials",
                        lambda c: rlm_tool.RlmCreds(base_url="b", api_key="k", primary_agent="p", sub_agent="s"))
    monkeypatch.setattr(rlm_tool, "_run_rlm_in_env",
                        lambda *a, **k: {"result": "ok", "usage": {}, "log_path": "/l"})
    out = __import__("json").loads(rlm_tool.rlm_tool(query="q", task_id="t"))
    assert out["status"] == "success"  # local backend → no guard error


def test_build_cfg_forwards_microvm_kernel_runtime_verbatim():
    # kernel_runtime is opaque to Hermes — a microVM value must pass through unchanged
    # (the engine preflights it). Guards against accidental filtering/allow-listing here.
    creds = rlm_tool.RlmCreds(base_url="b", api_key="k", primary_agent="p", sub_agent="s")
    rlm_cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS)
    rlm_cfg.update({"executor": "subprocess", "kernel_sandbox": "docker", "kernel_runtime": "kata-fc"})
    cfg = rlm_tool._build_rlm_cfg("q", creds, rlm_cfg, context_path=None, input_path=None)
    assert cfg["kernel_runtime"] == "kata-fc"


def test_rlm_tool_blocks_docker_microvm_kernel_on_nonlocal_backend(monkeypatch):
    # The docker→local-backend guard is runtime-agnostic: it must fire for microVM
    # runtimes too, not just runc/runsc.
    # Use a non-local, non-cloud backend ("docker") so the docker-kernel guard is
    # what fires — a cloud backend (modal/daytona) would trip the remote-backend
    # guard first.
    cfg = dict(rlm_tool._RLM_CONFIG_DEFAULTS)
    cfg["kernel_sandbox"] = "docker"; cfg["kernel_runtime"] = "kata-fc"
    monkeypatch.setattr(rlm_tool, "_load_rlm_config", lambda: cfg)
    monkeypatch.setattr(rlm_tool, "_get_or_create_env", lambda task_id: (object(), "ssh"))
    out = __import__("json").loads(rlm_tool.rlm_tool(query="q", task_id="t"))
    assert out["status"] == "error"
    assert "local Hermes backend" in out["error"]
