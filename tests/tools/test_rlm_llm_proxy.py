import json
import urllib.request
import urllib.error

import pytest

from tools.rlm import llm_proxy


class _Msg:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, content, finish_reason="stop", tool_calls=None):
        self.message = _Msg(content, tool_calls)
        self.finish_reason = finish_reason


class _Usage:
    prompt_tokens = 11
    completion_tokens = 7
    total_tokens = 18


class _Resp:
    def __init__(self, content="hello from codex"):
        self.choices = [_Choice(content)]
        self.model = "gpt-5.5"
        self.usage = _Usage()


def _post(url, token, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_happy_path_forwards_to_call_llm(monkeypatch):
    seen = {}

    def fake_call_llm(task=None, **kw):
        seen.update(kw)
        return _Resp("hello from codex")

    monkeypatch.setattr(llm_proxy, "call_llm", fake_call_llm)
    with llm_proxy.RlmCodingAgentProxy(provider="openai-codex", model="gpt-5.5") as proxy:
        status, body = _post(
            proxy.url + "/chat/completions", proxy.token,
            {"model": "ignored", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert status == 200
    out = json.loads(body)
    assert out["choices"][0]["message"]["content"] == "hello from codex"
    assert out["usage"]["total_tokens"] == 18
    assert seen["provider"] == "openai-codex"
    assert seen["model"] == "gpt-5.5"
    assert seen["messages"] == [{"role": "user", "content": "hi"}]


def test_bad_token_is_401(monkeypatch):
    monkeypatch.setattr(llm_proxy, "call_llm", lambda task=None, **kw: _Resp())
    with llm_proxy.RlmCodingAgentProxy(provider="openai-codex", model="gpt-5.5") as proxy:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(proxy.url + "/chat/completions", "wrong-token",
                  {"messages": [{"role": "user", "content": "hi"}]})
        assert ei.value.code == 401


def test_upstream_error_returns_502(monkeypatch):
    def boom(task=None, **kw):
        raise RuntimeError("codex exploded")

    monkeypatch.setattr(llm_proxy, "call_llm", boom)
    with llm_proxy.RlmCodingAgentProxy(provider="openai-codex", model="gpt-5.5") as proxy:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(proxy.url + "/chat/completions", proxy.token,
                  {"messages": [{"role": "user", "content": "hi"}]})
        assert ei.value.code == 502
        assert "codex exploded" in ei.value.read().decode("utf-8")


def test_streaming_request_emits_sse(monkeypatch):
    monkeypatch.setattr(llm_proxy, "call_llm", lambda task=None, **kw: _Resp("streamed"))
    with llm_proxy.RlmCodingAgentProxy(provider="openai-codex", model="gpt-5.5") as proxy:
        req = urllib.request.Request(
            proxy.url + "/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}], "stream": True}).encode("utf-8"),
            headers={"Authorization": f"Bearer {proxy.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8")
    assert "data: " in body
    assert "streamed" in body
    assert "[DONE]" in body


def test_missing_auth_header_is_401(monkeypatch):
    monkeypatch.setattr(llm_proxy, "call_llm", lambda task=None, **kw: _Resp())
    with llm_proxy.RlmCodingAgentProxy(provider="openai-codex", model="gpt-5.5") as proxy:
        req = urllib.request.Request(
            proxy.url + "/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 401


def test_wrong_path_is_404(monkeypatch):
    monkeypatch.setattr(llm_proxy, "call_llm", lambda task=None, **kw: _Resp())
    with llm_proxy.RlmCodingAgentProxy(provider="openai-codex", model="gpt-5.5") as proxy:
        req = urllib.request.Request(
            proxy.url + "/embeddings",
            data=json.dumps({"input": "hello"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {proxy.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 404
