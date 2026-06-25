# tests/plugins/temporal/test_activities_approval.py
"""
Prove that execute_durable_step installs a non-interactive approval callback
on the Temporal worker thread before running a subagent, mirroring the fix
delegate_task applies to its own ThreadPoolExecutor workers.
"""
import tools.terminal_tool as terminal_tool
import tools.delegate_tool as delegate_tool
from plugins.temporal import activities


def test_install_sets_noninteractive_callback():
    """_install_worker_approval_callback sets a non-interactive callback.

    After calling the helper, _get_approval_callback() must return
    _subagent_auto_deny (the safe default when config doesn't set
    delegation.subagent_auto_approve=true, which is the test-env default).
    This proves no input() fallback can happen on the worker thread.
    """
    # Reset any prior callback so the test is self-contained.
    terminal_tool._callback_tls.approval = None

    activities._install_worker_approval_callback()

    cb = terminal_tool._get_approval_callback()
    assert cb is delegate_tool._subagent_auto_deny, (
        f"Expected _subagent_auto_deny, got {cb!r}; "
        "worker would fall back to input() and hang"
    )


def test_execute_durable_step_installs_callback(monkeypatch):
    """execute_durable_step installs a non-interactive approval callback before
    delegating, so no Temporal worker thread can ever call input().
    """
    # Patch _delegate_handler to bypass real tool discovery / LLM calls.
    monkeypatch.setattr(
        activities,
        "_delegate_handler",
        lambda: lambda args, **kw: '{"status": "success", "result": "ok"}',
    )

    # Reset any prior callback so this test is self-contained.
    terminal_tool._callback_tls.approval = None

    activities.execute_durable_step({"name": "s", "prompt": "p"})

    cb = terminal_tool._get_approval_callback()
    assert cb is not None, "No approval callback installed — worker could call input()"
    allowed = {delegate_tool._subagent_auto_deny, delegate_tool._subagent_auto_approve}
    assert cb in allowed, (
        f"Unexpected callback {cb!r}; expected one of the non-interactive policy callbacks"
    )
