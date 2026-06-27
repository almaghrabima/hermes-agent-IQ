"""Ephemeral localhost OpenAI-compatible shim for fast-rlm coding-agent mode.

fast-rlm builds its own OpenAI client from RLM_MODEL_BASE_URL / RLM_MODEL_API_KEY
and calls ``/chat/completions``. For coding-agent providers (Codex) there is no
plain API key. This shim accepts those calls on 127.0.0.1, authenticates them
with a per-run random token, and re-emits each through Hermes' existing
``call_llm(provider=..., ...)`` path so the real (OAuth) credential never leaves
the Hermes process. Bound to the run's lifetime via the context manager.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent.auxiliary_client import call_llm


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default stderr access log
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        proxy: "RlmCodingAgentProxy" = self.server.proxy  # type: ignore[attr-defined]
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not secrets.compare_digest(token, proxy.token):
            self._json(401, {"error": {"message": "invalid api key", "type": "invalid_request_error"}})
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": f"no such path: {self.path}"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"error": {"message": "invalid json body"}})
            return
        try:
            result = proxy.complete(req)
            if req.get("stream"):
                self._sse(result)
            else:
                self._json(200, result)
        except Exception as exc:
            self._json(502, {"error": {"message": str(exc), "type": "upstream_error"}})
            return

    def _sse(self, result: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        msg = result["choices"][0]["message"]
        delta = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = msg["tool_calls"]
        first = {
            "id": result["id"], "object": "chat.completion.chunk",
            "created": result["created"], "model": result["model"],
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        last = {
            "id": result["id"], "object": "chat.completion.chunk",
            "created": result["created"], "model": result["model"],
            "choices": [{"index": 0, "delta": {},
                         "finish_reason": result["choices"][0]["finish_reason"]}],
        }
        for chunk in (first, last):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")


class RlmCodingAgentProxy:
    """Context manager running the shim on a random localhost port."""

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        self.token = secrets.token_urlsafe(32)
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address  # type: ignore[union-attr]
        return f"http://127.0.0.1:{port}/v1"

    def complete(self, req: dict) -> dict:
        """Forward one OpenAI chat/completions request through Hermes' call_llm."""
        resp = call_llm(
            task="rlm",
            provider=self.provider,
            model=self.model,
            messages=req.get("messages") or [],
            temperature=req.get("temperature"),
            max_tokens=req.get("max_tokens"),
            tools=req.get("tools"),
        )
        choice = resp.choices[0]
        message = choice.message
        usage = getattr(resp, "usage", None)
        out_msg = {"role": "assistant", "content": getattr(message, "content", "") or ""}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            out_msg["tool_calls"] = tool_calls
        result = {
            "id": "rlmproxy-" + secrets.token_hex(8),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": getattr(resp, "model", self.model) or self.model,
            "choices": [{
                "index": 0,
                "message": out_msg,
                "finish_reason": getattr(choice, "finish_reason", "stop") or "stop",
            }],
        }
        if usage is not None:
            result["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            }
        return result

    def __enter__(self) -> "RlmCodingAgentProxy":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.proxy = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False
