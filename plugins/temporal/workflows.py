from __future__ import annotations
from datetime import timedelta


def _make_workflow():
    from temporalio import workflow  # type: ignore
    from temporalio.common import RetryPolicy  # type: ignore

    @workflow.defn(name="DurableRunWorkflow")
    class DurableRunWorkflow:
        @workflow.run
        async def run(self, params: dict) -> dict:
            steps = params.get("steps", [])
            retry = params.get("retry") or {}
            timeout_s = int(params.get("step_timeout_seconds", 600))
            policy = RetryPolicy(
                maximum_attempts=int(retry.get("max_attempts", 3)),
                initial_interval=timedelta(seconds=int(retry.get("initial_interval_seconds", 1))),
                backoff_coefficient=float(retry.get("backoff_coefficient", 2.0)),
            )
            results = []
            for step in steps:
                r = await workflow.execute_activity(
                    "run_step", step,
                    start_to_close_timeout=timedelta(seconds=timeout_s),
                    retry_policy=policy,
                )
                results.append(r)
            return {"steps": results, "completed": len(results)}

    return DurableRunWorkflow
