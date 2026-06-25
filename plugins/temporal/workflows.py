from __future__ import annotations
from datetime import timedelta

# Temporal's sandbox runner does a fresh import of the workflow module and
# imports the workflow class by name.  The class must therefore be defined at
# module level so that `from plugins.temporal.workflows import DurableRunWorkflow`
# works inside the sandbox.
#
# We guard the definition with try/except so that importing this module does not
# fail when temporalio is not installed — other parts of the codebase import
# only `_make_workflow` lazily (e.g. worker.py calls it inside an async function).
# When temporalio IS installed the class is defined immediately at import time;
# when it is not, `_make_workflow()` will raise ImportError at call time with a
# clear message.

try:
    from temporalio import workflow as _wf  # type: ignore
    from temporalio.common import RetryPolicy as _RetryPolicy  # type: ignore

    @_wf.defn(name="DurableRunWorkflow")
    class DurableRunWorkflow:
        @_wf.run
        async def run(self, params: dict) -> dict:
            steps = params.get("steps", [])
            retry = params.get("retry") or {}
            timeout_s = int(params.get("step_timeout_seconds", 600))
            policy = _RetryPolicy(
                maximum_attempts=int(retry.get("max_attempts", 3)),
                initial_interval=timedelta(
                    seconds=int(retry.get("initial_interval_seconds", 1))
                ),
                backoff_coefficient=float(retry.get("backoff_coefficient", 2.0)),
            )
            results = []
            for step in steps:
                r = await _wf.execute_activity(
                    "run_step",
                    step,
                    start_to_close_timeout=timedelta(seconds=timeout_s),
                    retry_policy=policy,
                )
                results.append(r)
            return {"steps": results, "completed": len(results)}

    def _make_workflow() -> type:
        return DurableRunWorkflow

except ImportError:

    def _make_workflow() -> type:  # type: ignore[misc]
        """Raise ImportError — temporalio must be installed to create a workflow."""
        from temporalio import workflow  # noqa: F401  # re-raises ImportError
        from temporalio.common import RetryPolicy  # noqa: F401
        raise ImportError(
            "temporalio is required; install with: pip install temporalio"
        )
