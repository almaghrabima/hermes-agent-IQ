"""Recursive Language Model (fast-rlm) tool for Hermes.

Service-gated: its schema is only sent to the model when ``check_rlm_available``
returns True. Runs fast-rlm inside Hermes' active execution backend, reusing the
agent's active provider credentials. See
docs/superpowers/specs/2026-06-24-hermes-fast-rlm-tool-design.md.
"""

from __future__ import annotations

import logging
import shutil

logger = logging.getLogger(__name__)


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
                from tools.lazy_deps import _venv_pip_install

                result = _venv_pip_install((f"-e {engine_path}",))
                if result.success and importlib.util.find_spec("fast_rlm") is not None:
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
