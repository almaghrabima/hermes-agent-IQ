import math

from plugins.memory.turso_vector import weighting as w


def test_ema_update_first_rating_uses_score():
    # No prior ema: result equals the normalized score.
    assert w.ema_update(None, 3, alpha=0.4) == 1.0
    assert w.ema_update(None, 0, alpha=0.4) == 0.0


def test_ema_update_blends_with_prior():
    # prev 0.0, score 3 (r=1.0), alpha 0.4 -> 0.4
    assert math.isclose(w.ema_update(0.0, 3, alpha=0.4), 0.4)


def test_weight_from_ema_promotes_and_demotes():
    assert math.isclose(w.weight_from_ema(1.0, 1.0), 1.5)   # top rating promotes
    assert math.isclose(w.weight_from_ema(1.0, 0.0), 0.5)   # zero rating demotes


def test_decay_reduces_idle_weight():
    decayed = w.decay_weight(1.0, days_idle=10, decay_rate=0.98)
    assert decayed < 1.0
    assert math.isclose(decayed, 0.98 ** 10)
    # No negative idle time amplifies weight.
    assert w.decay_weight(1.0, days_idle=-5, decay_rate=0.98) == 1.0


def test_retrieval_score_blends_similarity_weight_and_project():
    base = w.retrieval_score(0.2, 1.0, project_match=False, beta=0.2, project_boost=0.1)
    boosted = w.retrieval_score(0.2, 1.0, project_match=True, beta=0.2, project_boost=0.1)
    assert math.isclose(boosted - base, 0.1)              # project boost adds exactly project_boost
    assert math.isclose(base, 0.8 + 0.2 * math.log1p(1.0))
