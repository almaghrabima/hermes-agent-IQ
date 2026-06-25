from plugins.temporal.tconfig import resolve_temporal_config

def test_defaults_enable_dev_server():
    s = resolve_temporal_config(config={}, env={})
    assert s.enabled is False
    assert s.target == "localhost:7233"
    assert s.namespace == "default"
    assert s.tls is False
    assert s.dev_server is True
    assert s.task_queue == "hermes"


def test_cloud_config_with_api_key_from_env():
    cfg = {"temporal": {"enabled": True, "target": "ns.acct.tmprl.cloud:7233",
                         "namespace": "ns.acct", "tls": True}}
    s = resolve_temporal_config(config=cfg, env={"TEMPORAL_API_KEY": "sek"})
    assert s.enabled is True
    assert s.tls is True
    assert s.api_key == "sek"
    assert s.target == "ns.acct.tmprl.cloud:7233"


def test_retry_and_timeout_defaults_overridable():
    cfg = {"temporal": {"step_timeout_seconds": 120,
                        "default_retry": {"max_attempts": 5}}}
    s = resolve_temporal_config(config=cfg, env={})
    assert s.step_timeout_seconds == 120
    assert s.retry["max_attempts"] == 5
    assert s.retry["backoff_coefficient"] == 2.0  # untouched default
