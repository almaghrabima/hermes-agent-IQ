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

            def run(query=None, instruction=None, input_file=None, config=None):
                return {
                    "results": f"answer to: {instruction or query}",
                    "usage": {"calls": 1, "completion_tokens": 5},
                    "log_path": "/tmp/rlm.jsonl",
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
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] == "answer to: count the rs"
    assert out["usage"]["calls"] == 1
    assert out["log_path"] == "/tmp/rlm.jsonl"


def test_driver_reports_engine_error(tmp_path):
    pkgdir = tmp_path / "fakepkg"
    (pkgdir / "fast_rlm").mkdir(parents=True)
    (pkgdir / "fast_rlm" / "__init__.py").write_text(
        "class RLMConfig:\n    def __init__(self, **kw):\n        pass\n"
        "def run(query=None, instruction=None, input_file=None, config=None):\n"
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
