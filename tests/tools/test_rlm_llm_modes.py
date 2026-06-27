import pytest

from tools.rlm.llm_modes import resolve_rlm_llm_mode, CODING_AGENT_PROVIDERS


def _cfg(provider="openrouter", default="anthropic/claude-3.5-haiku"):
    return {"model": {"provider": provider, "default": default}}


def test_auto_with_api_key_provider_resolves_api():
    assert resolve_rlm_llm_mode({"llm_mode": "auto"}, _cfg("openrouter")) == "api"


def test_auto_with_codex_provider_resolves_coding_agent():
    assert resolve_rlm_llm_mode({"llm_mode": "auto"}, _cfg("openai-codex")) == "coding_agent"


def test_explicit_api_overrides_codex_provider():
    assert resolve_rlm_llm_mode({"llm_mode": "api"}, _cfg("openai-codex")) == "api"


def test_explicit_coding_agent_overrides_api_provider():
    assert resolve_rlm_llm_mode(
        {"llm_mode": "coding_agent", "coding_agent": "openai-codex"}, _cfg("openrouter")
    ) == "coding_agent"


def test_invalid_llm_mode_raises():
    with pytest.raises(ValueError):
        resolve_rlm_llm_mode({"llm_mode": "bogus"}, _cfg())


def test_coding_agent_unsupported_provider_raises():
    with pytest.raises(ValueError):
        resolve_rlm_llm_mode({"llm_mode": "coding_agent", "coding_agent": "foo"}, _cfg())


def test_openai_codex_is_a_known_coding_agent():
    assert "openai-codex" in CODING_AGENT_PROVIDERS
