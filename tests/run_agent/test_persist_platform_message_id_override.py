"""_apply_persist_user_message_override must stamp platform_message_id on the
current-turn user message when configured, even with no content/timestamp
override (the durable-tagging case)."""
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            api_key="k", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            session_db=MagicMock(),
        )


def test_override_stamps_platform_message_id_only():
    agent = _make_agent()
    messages = [{"role": "user", "content": "tagged turn"}]
    agent._persist_user_message_idx = 0
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    agent._persist_user_message_platform_id = "durable-rlm-xyz"

    agent._apply_persist_user_message_override(messages)

    assert messages[0]["platform_message_id"] == "durable-rlm-xyz"
    assert messages[0]["content"] == "tagged turn"  # content untouched


def test_override_noop_when_nothing_configured():
    agent = _make_agent()
    messages = [{"role": "user", "content": "plain"}]
    agent._persist_user_message_idx = 0
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    agent._persist_user_message_platform_id = None

    agent._apply_persist_user_message_override(messages)

    assert "platform_message_id" not in messages[0]
