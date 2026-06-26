from plugins.temporal import tools


class _FakeHandle:
    id = "durable-deleg-abc"


class _FakeClient:
    async def start_workflow(self, *a, **kw):
        assert kw.get("task_queue")
        return _FakeHandle()


def test_dispatch_durable_delegation_returns_handle(monkeypatch):
    async def fake_connect(s):
        return _FakeClient()

    monkeypatch.setattr(tools, "connect", fake_connect)
    monkeypatch.setattr(tools, "load_config", lambda: {"temporal": {"enabled": True, "target": "localhost:7233", "namespace": "default", "task_queue": "hermes"}})
    out = tools.dispatch_durable_delegation(
        goal="do x",
        context=None,
        toolsets=None,
        role="leaf",
        model=None,
        session_key="sessA",
    )
    assert out["status"] == "dispatched"
    assert out["run_id"] == "durable-deleg-abc"
