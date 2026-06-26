"""Pure weighting math for turso_vector memory. No I/O."""
from __future__ import annotations

import math


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def ema_update(prev_ema: float | None, score: int, alpha: float) -> float:
    """Exponential moving average of normalized ratings. score in 0..3."""
    r = _clamp01(score / 3.0)
    if prev_ema is None:
        return r
    return _clamp01(alpha * r + (1.0 - alpha) * prev_ema)


def weight_from_ema(weight: float, new_ema: float) -> float:
    """Scale weight by rating: ema 1.0 -> x1.5, ema 0.0 -> x0.5."""
    return weight * (0.5 + new_ema)


def decay_weight(weight: float, days_idle: float, decay_rate: float) -> float:
    """Apply per-day decay; idle time below zero is treated as zero."""
    return weight * (decay_rate ** max(0.0, days_idle))


def retrieval_score(
    cos_dist: float,
    weight: float,
    project_match: bool,
    beta: float,
    project_boost: float,
) -> float:
    """Blend cosine similarity, learned weight, and a current-project boost."""
    sim = 1.0 - cos_dist
    return sim + beta * math.log1p(max(0.0, weight)) + (project_boost if project_match else 0.0)
