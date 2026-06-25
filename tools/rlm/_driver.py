"""Standalone fast-rlm driver, staged into the active backend by tools/rlm_tool.py.

Has NO Hermes imports — it runs in whatever environment fast-rlm lives in. Reads a
JSON config, runs fast_rlm, prints exactly one JSON line to stdout. Credentials
arrive via the process environment (RLM_MODEL_API_KEY / RLM_MODEL_BASE_URL), never
in the config file.
"""

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)

    try:
        import fast_rlm
        from fast_rlm import RLMConfig

        # Only pass non-None overrides so fast-rlm's own defaults (e.g.
        # max_money_spent=0.2, max_completion_tokens=50000) stay intact.
        cfg_kwargs = {
            "primary_agent": cfg["primary_agent"],
            "sub_agent": cfg.get("sub_agent") or cfg["primary_agent"],
        }

        import dataclasses, inspect
        if dataclasses.is_dataclass(RLMConfig):
            supported = {f.name for f in dataclasses.fields(RLMConfig)}
        else:
            supported = set(inspect.signature(RLMConfig).parameters)

        requested_kernel = (cfg.get("executor") == "subprocess") or (cfg.get("kernel_sandbox") is not None)
        if requested_kernel and not ({"executor", "kernel_sandbox"} <= supported):
            raise RuntimeError(
                "this fast-rlm build does not support executor/kernel_sandbox; point "
                "rlm.engine_path at a fast-rlm with kernel support (Phases 1-2)."
            )

        for key in ("max_global_calls", "max_money_spent", "max_completion_tokens",
                    "executor", "executor_unsandboxed_ack", "kernel_sandbox",
                    "kernel_runtime", "kernel_image", "kernel_network"):
            value = cfg.get(key)
            if value is not None and key in supported:
                cfg_kwargs[key] = value
        rlm_config = RLMConfig(**cfg_kwargs)

        # fast-rlm semantics: the long content is `query` (or `input_file=<path>`),
        # and the *task* is `instruction`. Pass exactly one of query/input_file.
        # verbose=False: fast-rlm otherwise prints its full REPL trace (tens to
        # hundreds of KB) to stdout, which would be captured as the tool result
        # and bloat context. We want only our single JSON line on stdout.
        task = cfg["query"]
        context_file = cfg.get("input_path") or cfg.get("context_path")
        if context_file:
            result = fast_rlm.run(input_file=context_file, instruction=task, config=rlm_config, verbose=False)
        else:
            result = fast_rlm.run(query=task, config=rlm_config, verbose=False)

        if result.get("error"):
            sys.stdout.write(json.dumps({"error": result["error"]}, ensure_ascii=False) + "\n")
            return 1

        out = {
            "result": result.get("results"),
            "usage": result.get("usage"),
            "log_path": result.get("log_file"),
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 0
    except Exception as exc:  # engine/budget/config error
        sys.stdout.write(json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
