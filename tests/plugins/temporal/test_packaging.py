from tools.lazy_deps import LAZY_DEPS


def test_temporal_lazy_dep_registered():
    assert "tool.temporal" in LAZY_DEPS
    pkgs = LAZY_DEPS["tool.temporal"]
    assert any(p.startswith("temporalio==") for p in pkgs), pkgs
