#!/usr/bin/env python3
"""fast-rlm out-of-process Python kernel (Phase 1, stdlib only).

A persistent native-Python REPL the Deno engine drives over a UNIX/TCP socket
using length-prefixed JSON frames. Replaces the in-process Pyodide VM. See
docs/superpowers/specs/2026-06-25-fast-rlm-python-kernel-phase1-design.md
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import io
import json
import struct
import traceback


def _pack(obj) -> bytes:
    data = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(data)) + data


class Kernel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.G: dict = {}                # REPL namespace, persists across steps
        self.pending: dict[int, asyncio.Future] = {}  # kernel->host request futures
        self._next_id = 1                # kernel owns ODD ids
        self._send_lock = asyncio.Lock()

    async def _send(self, frame: dict) -> None:
        async with self._send_lock:
            self.writer.write(_pack(frame))
            await self.writer.drain()

    async def _read_frame(self) -> dict:
        hdr = await self.reader.readexactly(4)
        (n,) = struct.unpack(">I", hdr)
        body = await self.reader.readexactly(n)
        return json.loads(body.decode("utf-8"))

    # ---- code execution -------------------------------------------------
    async def run_step(self, code: str) -> dict:
        buf = io.StringIO()
        err_text = ""
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code_obj = compile(code, "<repl>", "exec",
                                   flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                result = eval(code_obj, self.G)  # noqa: S307 - REPL by design
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # captured, not fatal
                err_text = traceback.format_exc()
                print(f"\nError: {exc}")
        stdout = buf.getvalue()
        final_set = bool(self.G.get("__final_result_set__"))
        final_value = None
        final_error = None
        if final_set:
            try:
                final_value = json.loads(json.dumps(self.G.get("__final_result__")))
            except (TypeError, ValueError) as exc:
                final_set = False
                final_error = f"FINAL value is not JSON-serializable: {exc}"
        return {"stdout": stdout, "error": err_text, "final_set": final_set,
                "final_value": final_value, "final_error": final_error}

    def _register_tool(self, src: str) -> None:
        ns: dict = {}
        exec(compile(src, "<tool>", "exec"), self.G, ns)  # noqa: S102
        fn = next((v for v in ns.values() if callable(v)), None)
        if fn is None:
            raise ValueError("Tool source defined no callable")
        try:
            fn.__fast_rlm_source__ = src
        except (AttributeError, TypeError):
            pass
        self.G[fn.__name__] = fn
        self.G.setdefault("__tools__", []).append(fn)

    def _inject_bridge(self) -> None:
        # Task 2 fills this in (host-call shims). Defined here so Task 1 tests
        # that don't touch the bridge still construct a kernel.
        pass

    async def serve(self) -> None:  # completed in Task 2
        raise NotImplementedError


def main() -> None:  # completed in Task 2
    raise NotImplementedError


if __name__ == "__main__":
    main()
