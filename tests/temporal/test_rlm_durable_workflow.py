import pytest
pytest.importorskip("temporalio")

from plugins.temporal import workflows


def test_rlm_retry_policy_uses_max_attempts():
    assert workflows._rlm_retry_policy(3).maximum_attempts == 3


def test_make_rlm_run_workflow_returns_class():
    assert workflows._make_rlm_run_workflow().__name__ == "RlmRunWorkflow"
