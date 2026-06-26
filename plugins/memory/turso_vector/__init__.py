"""turso_vector — self-improving long-term memory on Turso/libSQL native vectors.

See docs/superpowers/specs/2026-06-27-turso-vector-memory-design.md.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


class TursoVectorMemoryProvider(MemoryProvider):
    """Vector-backed, self-improving long-term memory."""

    def __init__(self) -> None:
        self._enabled = False  # set True once initialize() wires the store

    @property
    def name(self) -> str:
        return "turso_vector"

    def is_available(self) -> bool:
        # Local provider: always selectable. Heavy deps are lazy-installed in
        # initialize(); failure there disables the provider gracefully.
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        # Wired in Task 6.
        return None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Populated in Task 7.
        return []


def register(ctx) -> None:
    """Register turso_vector as a memory provider plugin."""
    ctx.register_memory_provider(TursoVectorMemoryProvider())
