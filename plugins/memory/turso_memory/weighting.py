"""Pure weighting math for turso_memory. No I/O."""
from __future__ import annotations

import time


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def ema_update(prev_ema: float | None, score: int, alpha: float) -> float:
    r = _clamp01(score / 3.0)
    if prev_ema is None:
        return r
    return _clamp01(alpha * r + (1.0 - alpha) * prev_ema)


def weight_from_ema(ema: float) -> float:
    """ema 1.0 -> 1.5, ema 0.0 -> 0.5 (a [0.5,1.5] multiplier, no compounding)."""
    return 0.5 + _clamp01(ema)


def days_between(earlier_iso: str, later_iso: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    a = time.mktime(time.strptime(earlier_iso, fmt))
    b = time.mktime(time.strptime(later_iso, fmt))
    return max(0.0, (b - a) / 86400.0)


def decay_weight(weight: float, days_idle: float, decay_rate: float) -> float:
    return weight * (decay_rate ** max(0.0, days_idle))
