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

    @_wf.defn(name="BackgroundDelegationWorkflow")
    class BackgroundDelegationWorkflow:
        @_wf.run
        async def run(self, params: dict) -> dict:
            # params: {goal, context, toolsets, role, model, session_key, run_id, retry, step_timeout_seconds}
            retry = params.get("retry") or {}
            timeout_s = int(params.get("step_timeout_seconds", 600))
            policy = _RetryPolicy(
                maximum_attempts=int(retry.get("max_attempts", 3)),
                initial_interval=timedelta(seconds=int(retry.get("initial_interval_seconds", 1))),
                backoff_coefficient=float(retry.get("backoff_coefficient", 2.0)),
            )
            step = {
                "name": "delegation",
                "prompt": params["goal"],
                "context": params.get("context"),
                "toolsets": params.get("toolsets"),
                "role": params.get("role"),
            }
            try:
                result = await _wf.execute_activity(
                    "run_step", step,
                    start_to_close_timeout=timedelta(seconds=timeout_s), retry_policy=policy,
                )
                ok = bool(result.get("ok"))
                summary = result.get("result")
                error = None if ok else result.get("result")
            except Exception as exc:  # activity exhausted its retries
                # Record the failure durably instead of letting the workflow fail
                # silently — otherwise the user never learns the delegation died.
                ok, summary, error = False, None, f"durable delegation failed: {exc}"
            block = {
                "goal": params.get("goal", ""), "context": params.get("context"),
                "toolsets": params.get("toolsets"), "role": params.get("role"),
                "model": params.get("model"),
                "summary": summary,
                "error": error,
                "status": "completed" if ok else "failed",
            }
            await _wf.execute_activity(
                "record_outbox",
                {"run_id": params["run_id"], "session_key": params.get("session_key", "default"),
                 "status": block["status"], "block": block},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_RetryPolicy(maximum_attempts=10),
            )
            return {"run_id": params["run_id"], "session_key": params.get("session_key", "default"), "status": block["status"], "block": block}

    @_wf.defn(name="HumanInputWorkflow")
    class HumanInputWorkflow:
        def __init__(self) -> None:
            self._answer = None
            self._answered = False

        @_wf.signal(name="respond")
        def respond(self, answer: str) -> None:
            if not self._answered:
                self._answer = answer
                self._answered = True

        @_wf.query(name="get_session_key")
        def get_session_key(self) -> str:
            return self._session_key  # set in run()

        @_wf.run
        async def run(self, params: dict) -> dict:
            import asyncio as _asyncio
            self._session_key = params.get("session_key", "default")
            timeout_s = int(params.get("timeout_seconds", 86400))
            try:
                await _wf.wait_condition(lambda: self._answered, timeout=timedelta(seconds=timeout_s))
                status, answer = "answered", self._answer
            except _asyncio.TimeoutError:
                status, answer = "timed_out", None
            block = {
                "goal": params.get("prompt", ""), "context": params.get("context"),
                "toolsets": None, "role": None, "model": None,
                "summary": answer, "error": None,
                "status": status,
            }
            await _wf.execute_activity(
                "record_outbox",
                {"run_id": params["run_id"], "session_key": self._session_key,
                 "status": status, "block": block},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_RetryPolicy(maximum_attempts=10),
            )
            return {"run_id": params["run_id"], "session_key": self._session_key,
                    "status": status, "block": block}

    def _make_workflow() -> type:
        return DurableRunWorkflow

    def _make_background_workflow() -> type:
        return BackgroundDelegationWorkflow

    def _make_human_input_workflow() -> type:
        return HumanInputWorkflow

except ImportError:

    def _make_workflow() -> type:  # type: ignore[misc]
        raise ImportError(
            "temporalio is required for the durable orchestration worker; "
            "install the optional extra: uv pip install -e '.[temporal]'"
        )

    def _make_background_workflow() -> type:  # type: ignore[misc]
        raise ImportError(
            "temporalio is required for the durable orchestration worker; "
            "install the optional extra: uv pip install -e '.[temporal]'"
        )

    def _make_human_input_workflow() -> type:  # type: ignore[misc]
        raise ImportError(
            "temporalio is required for the durable orchestration worker; "
            "install the optional extra: uv pip install -e '.[temporal]'"
        )
