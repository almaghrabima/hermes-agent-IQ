from plugins.temporal import temporal_available


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr("plugins.temporal.load_config", lambda: {})
    assert temporal_available() is False


def test_available_when_enabled(monkeypatch):
    monkeypatch.setattr("plugins.temporal.load_config",
                        lambda: {"temporal": {"enabled": True, "target": "localhost:7233"}})
    # SDK import is gated separately; here we only require enabled+target.
    assert temporal_available() is True
