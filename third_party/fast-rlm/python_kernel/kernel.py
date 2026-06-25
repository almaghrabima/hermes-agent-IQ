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
        self._shutdown_event = asyncio.Event()

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

    async def host_call(self, op: str, payload: dict):
        fut = asyncio.get_running_loop().create_future()
        mid = self._next_id
        self._next_id += 2  # odd ids only
        self.pending[mid] = fut
        await self._send({"kind": "req", "op": op, "id": mid, **payload})
        return await fut

    def _inject_bridge(self) -> None:
        kernel = self

        async def __host_call__(op, payload):
            return await kernel.host_call(op, payload)

        async def __js_llm_query__(context, child_schema=None, child_tool_sources=None,
                                   child_mcp_servers=None, child_instruction=None,
                                   suppress_guard=None):
            return await kernel.host_call("llm_query", {
                "context": context, "schema": child_schema,
                "tools": child_tool_sources, "mcp": child_mcp_servers,
                "instruction": child_instruction, "suppress_guard": suppress_guard})

        async def __js_batch_confirm__(meta_json):
            return await kernel.host_call("batch_confirm", {"meta": meta_json})

        async def __js_mcp_call__(server, tool, args):
            return await kernel.host_call("mcp_call", {"server": server, "tool": tool, "args": args})

        async def __js_mcp_read_resource__(server, uri):
            return await kernel.host_call("mcp_read_resource", {"server": server, "uri": uri})

        self.G["__host_call__"] = __host_call__
        self.G["__js_llm_query__"] = __js_llm_query__
        self.G["__js_batch_confirm__"] = __js_batch_confirm__
        self.G["__js_mcp_call__"] = __js_mcp_call__
        self.G["__js_mcp_read_resource__"] = __js_mcp_read_resource__

    async def _handle_request(self, frame: dict) -> None:
        op = frame.get("op")
        rid = frame.get("id")
        try:
            if op == "setup":
                exec(compile(frame["code"], "<setup>", "exec"), self.G)  # noqa: S102
                resp = {"ok": True}
            elif op == "register_tool":
                self._register_tool(frame["src"])
                resp = {"ok": True}
            elif op == "run_step":
                resp = await self.run_step(frame["code"])
            elif op == "reset_final":
                self.G["__final_result__"] = None
                self.G["__final_result_set__"] = False
                resp = {"ok": True}
            elif op == "shutdown":
                await self._send({"kind": "resp", "id": rid, "ok": True})
                self.writer.close()
                self._shutdown_event.set()
                return
            else:
                resp = {"error": f"unknown op {op}"}
        except Exception:
            resp = {"error": traceback.format_exc()}
        await self._send({"kind": "resp", "id": rid, **resp})

    async def serve(self) -> None:
        self._inject_bridge()
        while True:
            try:
                frame = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            if frame.get("kind") == "resp":
                fut = self.pending.pop(frame.get("id"), None)
                if fut and not fut.done():
                    err = frame.get("error")
                    if err is not None:
                        fut.set_exception(RuntimeError(err))
                    else:
                        fut.set_result(frame.get("result"))
                continue
            # host request — dispatch concurrently so serve() keeps reading
            # (a run_step awaits host calls whose resp frames arrive here).
            asyncio.ensure_future(self._handle_request(frame))
            if frame.get("op") == "shutdown":
                # Wait for the shutdown handler to finish, then exit.
                await self._shutdown_event.wait()
                break


async def _serve_streams(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await Kernel(reader, writer).serve()


async def _amain(socket_path, tcp, stdio) -> None:
    if stdio:
        import sys
        # Bind the control channel to the REAL fd 1 first, then send all Python
        # stdout to stderr so only framed control bytes ever reach fd 1.
        real_stdout = sys.stdout.buffer
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        w_transport, w_proto = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, real_stdout)
        writer = asyncio.StreamWriter(w_transport, w_proto, reader, loop)
        sys.stdout = sys.stderr  # stray prints can't corrupt the frame stream
        await _serve_streams(reader, writer)
    elif tcp:
        host, port = tcp.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        await _serve_streams(reader, writer)
    else:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        await _serve_streams(reader, writer)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket")
    ap.add_argument("--tcp")
    ap.add_argument("--stdio", action="store_true")
    args = ap.parse_args()
    asyncio.run(_amain(args.socket, args.tcp, args.stdio))


if __name__ == "__main__":
    main()
