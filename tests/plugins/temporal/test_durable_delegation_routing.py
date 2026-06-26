from plugins.temporal import outbox, tools


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


def test_list_completed_skips_already_recorded(tmp_path, monkeypatch):
    """Reconcile must not re-fetch handle.result() for delegations already in the
    outbox — otherwise every startup scans the whole Temporal retention window."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-A", "s", "completed", {"goal": "g"})

    fetched: list[str] = []

    class _WF:
        def __init__(self, wid):
            self.id = wid

    class _Handle:
        def __init__(self, wid):
            self.id = wid

        async def result(self):
            fetched.append(self.id)
            return {"run_id": self.id, "session_key": "s", "status": "completed", "block": {}}

    class _Client:
        async def list_workflows(self, query=None):
            for wid in ("run-A", "run-B"):
                yield _WF(wid)

        def get_workflow_handle(self, wid):
            return _Handle(wid)

    async def fake_connect(s):
        return _Client()

    monkeypatch.setattr(tools, "connect", fake_connect)
    monkeypatch.setattr(tools, "load_config", lambda: {"temporal": {"enabled": True, "target": "x"}})

    out = tools.list_completed_durable_delegations()

    # run-A is already recorded -> its result() is never fetched; only run-B.
    assert fetched == ["run-B"], f"expected only run-B fetched, got {fetched}"
    assert [o["run_id"] for o in out] == ["run-B"]
