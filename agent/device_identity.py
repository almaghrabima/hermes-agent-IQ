"""Per-device identity + Snowflake id generation for multi-device-safe writes.

No database dependencies. The device id/number live in a LOCAL-ONLY file
(``<hermes_home>/device.json``) that is never written to a synced DB, so it
does not replicate across devices. Snowflake ids are globally collision-free
(device-partitioned) yet remain ``INTEGER PRIMARY KEY`` values so FTS5 rowids
and ``ORDER BY id`` keep working.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DEVICE_BITS = 16
_SEQ_BITS = 6
_DEVICE_SHIFT = _SEQ_BITS          # 6
_TS_SHIFT = _SEQ_BITS + _DEVICE_BITS  # 22
_MAX_SEQ = (1 << _SEQ_BITS) - 1    # 63
_MAX_DEVICE = (1 << _DEVICE_BITS) - 1  # 65535

_lock = threading.RLock()
_cache: dict | None = None
_process_gen: "SnowflakeGenerator | None" = None


def _device_file():
    return get_hermes_home() / "device.json"


def _load_or_create() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        path = _device_file()
        data = None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                dn = data.get("device_number") if isinstance(data, dict) else None
                if not (isinstance(data, dict) and isinstance(data.get("device_id"), str)
                        and isinstance(dn, int) and not isinstance(dn, bool)
                        and 0 <= dn <= _MAX_DEVICE):
                    data = None
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("device.json unreadable (%s); regenerating", exc)
                data = None
        if data is None:
            data = {"device_id": uuid.uuid4().hex,
                    "device_number": secrets.randbelow(_MAX_DEVICE + 1)}
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data), encoding="utf-8")
            except OSError as exc:
                logger.warning("could not persist device.json (%s); using ephemeral id", exc)
        _cache = data
        return data


def _reset_cache() -> None:
    """Test helper: drop the in-memory cache so the next call re-reads disk."""
    global _cache, _process_gen
    _cache = None
    _process_gen = None


def get_device_id() -> str:
    return _load_or_create()["device_id"]


def get_device_number() -> int:
    return _load_or_create()["device_number"]


class SnowflakeGenerator:
    """Generates 63-bit ``(ms<<22)|(device<<6)|seq`` ids, monotonic per process."""

    def __init__(self, device_number: int):
        self._device = device_number & _MAX_DEVICE
        self._last_ms = 0
        self._seq = 0
        self._mutex = threading.Lock()

    def next_id(self, now_ms: Optional[int] = None) -> int:
        with self._mutex:
            ms = now_ms if now_ms is not None else int(time.time() * 1000)
            # Clock-backwards guard: never go below the last emitted ms.
            if ms < self._last_ms:
                ms = self._last_ms
            if ms == self._last_ms:
                self._seq += 1
                if self._seq > _MAX_SEQ:
                    ms += 1            # seq exhausted this ms -> advance ms
                    self._seq = 0
            else:
                self._seq = 0
            self._last_ms = ms
            return (ms << _TS_SHIFT) | (self._device << _DEVICE_SHIFT) | self._seq


def next_id() -> int:
    """Convenience: emit an id from the process-wide generator for this device."""
    global _process_gen
    with _lock:
        if _process_gen is None:
            _process_gen = SnowflakeGenerator(get_device_number())
        gen = _process_gen
    return gen.next_id()
