"""The session-DB flush must pass a message dict's platform_message_id through
to append_message, so durable-run tagging (run_id) reaches the synced column."""
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_agent(session_db):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
        )


def test_platform_message_id_passed_through_on_flush():
    session_db = MagicMock()
    agent = _make_agent(session_db)

    messages = [
        {"role": "user", "content": "result of run", "platform_message_id": "durable-rlm-abc123"},
    ]
    agent._flush_messages_to_session_db(messages)

    user_appends = [
        c for c in session_db.append_message.call_args_list
        if c.kwargs.get("role") == "user"
    ]
    assert len(user_appends) == 1
    assert user_appends[0].kwargs["platform_message_id"] == "durable-rlm-abc123"


def test_missing_platform_message_id_is_none():
    session_db = MagicMock()
    agent = _make_agent(session_db)

    agent._flush_messages_to_session_db([{"role": "user", "content": "hi"}])

    user_appends = [
        c for c in session_db.append_message.call_args_list
        if c.kwargs.get("role") == "user"
    ]
    assert len(user_appends) == 1
    assert user_appends[0].kwargs.get("platform_message_id") is None
