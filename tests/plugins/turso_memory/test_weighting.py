import math
from plugins.memory.turso_memory.weighting import (
    ema_update, weight_from_ema, days_between, decay_weight,
)


def test_ema_first_is_normalized():
    assert math.isclose(ema_update(None, 3, 0.3), 1.0, abs_tol=1e-6)
    assert math.isclose(ema_update(None, 0, 0.3), 0.0, abs_tol=1e-6)


def test_ema_blends():
    assert math.isclose(ema_update(0.0, 3, 0.5), 0.5, abs_tol=1e-6)


def test_weight_from_ema_range():
    assert math.isclose(weight_from_ema(1.0), 1.5, abs_tol=1e-6)
    assert math.isclose(weight_from_ema(0.0), 0.5, abs_tol=1e-6)


def test_days_and_decay():
    assert math.isclose(days_between("2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"), 2.0, abs_tol=1e-6)
    assert math.isclose(decay_weight(1.0, 2.0, 0.98), 0.98 ** 2, abs_tol=1e-6)
    assert decay_weight(1.0, 0.0, 0.98) == 1.0
