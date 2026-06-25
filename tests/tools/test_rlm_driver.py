import json
import subprocess
import sys
import textwrap
from pathlib import Path

DRIVER = Path(__file__).resolve().parents[2] / "tools" / "rlm" / "_driver.py"


def _fake_fast_rlm(tmp_path: Path) -> Path:
    """Write a fake fast_rlm package and return a dir to put on PYTHONPATH."""
    pkgdir = tmp_path / "fakepkg"
    (pkgdir / "fast_rlm").mkdir(parents=True)
    (pkgdir / "fast_rlm" / "__init__.py").write_text(
        textwrap.dedent(
            '''
            class RLMConfig:
                def __init__(self, **kw):
                    self.kw = kw

            def run(query=None, instruction=None, input_file=None, config=None, verbose=True):
                if verbose:
                    print("VERBOSE_UI_NOISE")  # driver must pass verbose=False to suppress
                return {
                    "results": f"answer to: {instruction or query}",
                    "usage": {"calls": 1, "completion_tokens": 5},
                    "log_file": "/tmp/rlm.jsonl",
                }
            '''
        ),
        encoding="utf-8",
    )
    return pkgdir


def test_driver_runs_and_emits_json(tmp_path, monkeypatch):
    pkgdir = _fake_fast_rlm(tmp_path)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "query": "count the rs",
        "primary_agent": "z-ai/glm-5",
        "sub_agent": "z-ai/glm-5",
        "context_path": None,
        "input_path": None,
        "max_global_calls": 50,
        "max_money_spent": None,
        "max_completion_tokens": None,
    }), encoding="utf-8")

    env = {"PYTHONPATH": str(pkgdir), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, str(DRIVER), "--config", str(cfg)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    # Driver must pass verbose=False: stdout is exactly our one JSON line, no trace.
    assert "VERBOSE_UI_NOISE" not in proc.stdout
    assert len(proc.stdout.strip().splitlines()) == 1
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] == "answer to: count the rs"
    assert out["usage"]["calls"] == 1
    assert out["log_path"] == "/tmp/rlm.jsonl"


def test_driver_reports_engine_error(tmp_path):
    pkgdir = tmp_path / "fakepkg"
    (pkgdir / "fast_rlm").mkdir(parents=True)
    (pkgdir / "fast_rlm" / "__init__.py").write_text(
        "class RLMConfig:\n    def __init__(self, **kw):\n        pass\n"
        "def run(query=None, instruction=None, input_file=None, config=None, verbose=True):\n"
        "    raise RuntimeError('budget exceeded')\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "query": "x", "primary_agent": "m", "sub_agent": "m",
        "context_path": None, "input_path": None, "max_global_calls": 1,
        "max_money_spent": None, "max_completion_tokens": None,
    }), encoding="utf-8")

    env = {"PYTHONPATH": str(pkgdir), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, str(DRIVER), "--config", str(cfg)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "budget exceeded" in out["error"]


def test_driver_surfaces_engine_error_returned_without_raising(tmp_path):
    pkgdir = tmp_path / "fakepkg"
    (pkgdir / "fast_rlm").mkdir(parents=True)
    (pkgdir / "fast_rlm" / "__init__.py").write_text(
        "class RLMConfig:\n    def __init__(self, **kw):\n        pass\n"
        "def run(query=None, instruction=None, input_file=None, config=None, verbose=True):\n"
        "    return {'results': None, 'log_file': '/tmp/r.jsonl', 'usage': {}, 'error': 'engine boom'}\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "query": "x", "primary_agent": "m", "sub_agent": "m",
        "context_path": None, "input_path": None, "max_global_calls": 1,
        "max_money_spent": None, "max_completion_tokens": None,
    }), encoding="utf-8")
    env = {"PYTHONPATH": str(pkgdir), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, str(DRIVER), "--config", str(cfg)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["error"] == "engine boom"


def test_driver_passes_kernel_kwargs(tmp_path):
    # Fake fast_rlm whose RLMConfig is a dataclass supporting the kernel fields,
    # and whose run() records the config kwargs it received.
    import textwrap
    pkgdir = tmp_path / "fakepkg"
    (pkgdir / "fast_rlm").mkdir(parents=True)
    (pkgdir / "fast_rlm" / "__init__.py").write_text(
        textwrap.dedent('''
            import dataclasses
            @dataclasses.dataclass
            class RLMConfig:
                primary_agent: str = None
                sub_agent: str = None
                executor: str = None
                executor_unsandboxed_ack: bool = False
                kernel_sandbox: str = None
                kernel_runtime: str = None
                kernel_image: str = None
                kernel_network: str = None
            def run(query=None, instruction=None, input_file=None, config=None, verbose=True):
                c = config
                return {"results": {"executor": c.executor, "kernel_sandbox": c.kernel_sandbox,
                                    "kernel_image": c.kernel_image, "ack": c.executor_unsandboxed_ack},
                        "usage": {}, "log_file": "/tmp/r.jsonl"}
        '''), encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(__import__("json").dumps({
        "query": "x", "primary_agent": "m", "sub_agent": "m",
        "context_path": None, "input_path": None, "max_global_calls": 5,
        "max_money_spent": None, "max_completion_tokens": None,
        "executor": "subprocess", "executor_unsandboxed_ack": False,
        "kernel_sandbox": "docker", "kernel_runtime": "runc",
        "kernel_image": "python:3.11-slim", "kernel_network": "none",
    }), encoding="utf-8")
    import subprocess, sys, json as _json
    from pathlib import Path
    DRIVER = Path(__file__).resolve().parents[2] / "tools" / "rlm" / "_driver.py"
    env = {"PYTHONPATH": str(pkgdir), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, str(DRIVER), "--config", str(cfg)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    out = _json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"]["executor"] == "subprocess"
    assert out["result"]["kernel_sandbox"] == "docker"
    assert out["result"]["kernel_image"] == "python:3.11-slim"


def test_driver_errors_when_fastrlm_lacks_kernel_support(tmp_path):
    # Stock-style RLMConfig WITHOUT kernel fields; requesting subprocess must error clearly.
    pkgdir = tmp_path / "fakepkg"
    (pkgdir / "fast_rlm").mkdir(parents=True)
    (pkgdir / "fast_rlm" / "__init__.py").write_text(
        "import dataclasses\n"
        "@dataclasses.dataclass\n"
        "class RLMConfig:\n"
        "    primary_agent: str = None\n    sub_agent: str = None\n"
        "def run(query=None, instruction=None, input_file=None, config=None, verbose=True):\n"
        "    return {'results': 'x', 'usage': {}, 'log_file': None}\n",
        encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(__import__("json").dumps({
        "query": "x", "primary_agent": "m", "sub_agent": "m",
        "context_path": None, "input_path": None, "max_global_calls": 1,
        "max_money_spent": None, "max_completion_tokens": None,
        "executor": "subprocess", "kernel_sandbox": None,
    }), encoding="utf-8")
    import subprocess, sys, json as _json
    from pathlib import Path
    DRIVER = Path(__file__).resolve().parents[2] / "tools" / "rlm" / "_driver.py"
    env = {"PYTHONPATH": str(pkgdir), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, str(DRIVER), "--config", str(cfg)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 1
    out = _json.loads(proc.stdout.strip().splitlines()[-1])
    assert "kernel support" in out["error"] or "engine_path" in out["error"]
