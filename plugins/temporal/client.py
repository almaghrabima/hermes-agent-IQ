from __future__ import annotations
from typing import Any
from plugins.temporal.tconfig import TemporalSettings


def build_connect_kwargs(s: TemporalSettings) -> dict[str, Any]:
    """Assemble temporalio Client.connect kwargs from settings (pure; no I/O)."""
    kw: dict[str, Any] = {"target_host": s.target, "namespace": s.namespace}
    if s.tls:
        kw["tls"] = True
    if s.api_key:
        kw["api_key"] = s.api_key
    return kw


async def connect(s: TemporalSettings):
    """Connect a Temporal client. Lazy-imports temporalio (raises FeatureUnavailable)."""
    from tools.lazy_deps import ensure
    ensure("tool.temporal", prompt=False)
    from temporalio.client import Client  # type: ignore
    return await Client.connect(**build_connect_kwargs(s))
