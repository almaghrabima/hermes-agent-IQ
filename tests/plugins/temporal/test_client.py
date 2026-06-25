from plugins.temporal.tconfig import TemporalSettings
from plugins.temporal.client import build_connect_kwargs


def test_dev_kwargs_minimal():
    kw = build_connect_kwargs(TemporalSettings(target="localhost:7233", namespace="default"))
    assert kw["target_host"] == "localhost:7233"
    assert kw["namespace"] == "default"
    assert "api_key" not in kw or kw["api_key"] is None


def test_cloud_kwargs_include_api_key_and_tls():
    s = TemporalSettings(target="ns.acct.tmprl.cloud:7233", namespace="ns.acct",
                         tls=True, api_key="sek")
    kw = build_connect_kwargs(s)
    assert kw["tls"] is True
    assert kw["api_key"] == "sek"
