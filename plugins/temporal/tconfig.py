from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Optional

_DEFAULT_RETRY = {"max_attempts": 3, "initial_interval_seconds": 1, "backoff_coefficient": 2.0}


@dataclass
class TemporalSettings:
    enabled: bool = False
    target: str = "localhost:7233"
    namespace: str = "default"
    tls: bool = False
    task_queue: str = "hermes"
    dev_server: bool = True
    step_timeout_seconds: int = 600
    retry: dict = field(default_factory=lambda: dict(_DEFAULT_RETRY))
    api_key: Optional[str] = None
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None


def resolve_temporal_config(config: Optional[dict] = None, env: Optional[dict] = None) -> TemporalSettings:
    """Resolve the ``temporal:`` block from config.yaml + secrets from env."""
    config = config or {}
    env = env if env is not None else os.environ
    t = config.get("temporal") or {}
    retry = dict(_DEFAULT_RETRY)
    retry.update(t.get("default_retry") or {})
    return TemporalSettings(
        enabled=bool(t.get("enabled", False)),
        target=str(t.get("target", "localhost:7233")),
        namespace=str(t.get("namespace", "default")),
        tls=bool(t.get("tls", False)),
        task_queue=str(t.get("task_queue", "hermes")),
        dev_server=bool(t.get("dev_server", True)),
        step_timeout_seconds=int(t.get("step_timeout_seconds", 600)),
        retry=retry,
        api_key=env.get("TEMPORAL_API_KEY"),
        tls_cert=env.get("TEMPORAL_TLS_CERT"),
        tls_key=env.get("TEMPORAL_TLS_KEY"),
    )
