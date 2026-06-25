"""Stdlib test driver for kernel.py (run: python3 kerneltest.py). Exit 0 = pass."""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(__file__))
import kernel as K  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


async def _pair():
    a, b = socket.socketpair()
    ra, wa = await asyncio.open_connection(sock=a)
    rb, wb = await asyncio.open_connection(sock=b)
    return (ra, wa), (rb, wb)


async def _setup(k, code):
    # exec setup directly into G (mirrors the 'setup' op without the socket)
    exec(compile(code, "<setup>", "exec"), k.G)


_FINAL_DEF = (
    "def FINAL(x):\n"
    " globals()['__final_result__']=x\n"
    " globals()['__final_result_set__']=True\n"
)


async def test_state_and_final():
    (ra, wa), _ = await _pair()
    k = K.Kernel(ra, wa)
    k._inject_bridge()
    await _setup(k, _FINAL_DEF)
    r1 = await k.run_step("x = 40\nprint('hi')")
    check("step1 stdout", "hi" in r1["stdout"])
    check("step1 no final", r1["final_set"] is False)
    r2 = await k.run_step("x += 2\nprint(x)")
    check("state persists", "42" in r2["stdout"])
    r3 = await k.run_step("FINAL({'answer': x})")
    check("final set", r3["final_set"] is True)
    check("final value", r3["final_value"] == {"answer": 42})


async def test_register_tool_and_error():
    (ra, wa), _ = await _pair()
    k = K.Kernel(ra, wa)
    k._inject_bridge()
    await _setup(k, _FINAL_DEF)
    k._register_tool("def add(a, b):\n    return a + b\n")
    r = await k.run_step("print(add(2, 3))")
    check("tool registered", "5" in r["stdout"])
    rerr = await k.run_step("raise ValueError('boom')")
    check("exception captured in stdout", "boom" in rerr["stdout"])
    check("exception in error field", "ValueError" in rerr["error"])


async def _send_frame(writer, frame):
    body = K.json.dumps(frame).encode()
    writer.write(K.struct.pack(">I", len(body)) + body)
    await writer.drain()


async def _recv_frame(reader):
    hdr = await asyncio.wait_for(reader.readexactly(4), timeout=10)
    (n,) = K.struct.unpack(">I", hdr)
    body = await asyncio.wait_for(reader.readexactly(n), timeout=10)
    return K.json.loads(body.decode())


async def test_host_bridge_llm_query():
    # Drive the kernel through serve() — the real production path:
    # serve() reads host requests, dispatches them via ensure_future, and a
    # run_step's `await __js_llm_query__(...)` issues a kernel->host req whose
    # resp serve() must read concurrently while run_step is still suspended.
    (ra, wa), (rb, wb) = await _pair()
    k = K.Kernel(ra, wa)
    serve_task = asyncio.ensure_future(k.serve())

    # Host (other socket end rb/wb) registers FINAL via a setup req.
    await _send_frame(wb, {"kind": "req", "op": "setup", "id": 2, "code": _FINAL_DEF})
    setup_resp = await _recv_frame(rb)
    check("setup ack via serve", setup_resp.get("ok") is True)

    # Host asks the kernel to run a step that calls __js_llm_query__.
    await _send_frame(wb, {"kind": "req", "op": "run_step", "id": 4,
                           "code": "res = await __js_llm_query__('hello')\nprint(res['echo'])"})

    # While run_step is suspended on the host call, serve() keeps reading: the
    # next frame from the kernel is its llm_query REQ (odd id). (Defer check()
    # until after the run_step resp arrives — while run_step is mid-flight the
    # kernel has stdout redirected, which would swallow these prints.)
    kreq = await _recv_frame(rb)
    kreq_ok = kreq.get("op") == "llm_query" and kreq.get("context") == "hello"
    kreq_odd = isinstance(kreq.get("id"), int) and kreq["id"] % 2 == 1
    await _send_frame(wb, {"kind": "resp", "id": kreq["id"], "result": {"echo": "hello"}})

    # Now the run_step completes and its resp (id 4) carries stdout.
    run_resp = await _recv_frame(rb)
    check("kernel issued llm_query req", kreq_ok)
    check("kernel used odd id", kreq_odd)
    check("run_step resp id matches", run_resp.get("id") == 4)
    check("llm_query bridged via serve", "hello" in run_resp.get("stdout", ""))

    # Shut down: kernel replies then closes; serve() returns.
    await _send_frame(wb, {"kind": "req", "op": "shutdown", "id": 6})
    shut_resp = await _recv_frame(rb)
    check("shutdown ack", shut_resp.get("ok") is True)
    await asyncio.wait_for(serve_task, timeout=10)
    wb.close()


async def test_non_serializable_final_sets_error():
    (ra, wa), _ = await _pair()
    k = K.Kernel(ra, wa)
    k._inject_bridge()
    await _setup(k, _FINAL_DEF)
    r = await k.run_step("FINAL({'bad': set([1, 2, 3])})")  # set() is not JSON-serializable
    check("non-serializable final -> not set", r["final_set"] is False)
    check("non-serializable final -> final_error populated", bool(r["final_error"]))


async def test_stdio_mode_via_subprocess():
    import os
    import sys as _sys
    here = os.path.dirname(__file__)
    proc = await asyncio.create_subprocess_exec(
        _sys.executable, os.path.join(here, "kernel.py"), "--stdio",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    def frame(obj):
        import json as _j, struct as _s
        b = _j.dumps(obj).encode()
        return _s.pack(">I", len(b)) + b

    async def recv():
        import struct as _s, json as _j
        hdr = await proc.stdout.readexactly(4)
        (n,) = _s.unpack(">I", hdr)
        return _j.loads((await proc.stdout.readexactly(n)).decode())

    proc.stdin.write(frame({"kind": "req", "op": "setup", "id": 2, "code": _FINAL_DEF}))
    await proc.stdin.drain()
    await recv()  # setup ack
    proc.stdin.write(frame({"kind": "req", "op": "run_step", "id": 4,
                            "code": "v = 21 * 2\nprint(v)"}))
    await proc.stdin.drain()
    r = await recv()
    check("stdio run_step stdout", "42" in r["stdout"])
    proc.stdin.write(frame({"kind": "req", "op": "shutdown", "id": 6}))
    await proc.stdin.drain()
    await proc.wait()
    check("stdio shutdown clean exit", proc.returncode == 0)


async def main():
    await test_state_and_final()
    await test_register_tool_and_error()
    await test_host_bridge_llm_query()
    await test_non_serializable_final_sets_error()
    await test_stdio_mode_via_subprocess()
    print(("FAILED: " + ", ".join(FAILURES)) if FAILURES else "ALL PASS")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    asyncio.run(main())
