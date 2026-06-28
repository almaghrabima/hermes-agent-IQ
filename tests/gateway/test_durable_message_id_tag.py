"""The gateway forge must tag a durable completion's MessageEvent with the
run_id (carried as delegation_id) so persistence stamps the synced
platform_message_id. Tests the real production helper, not a mirror."""
from gateway.run import _durable_message_id


def test_delegation_id_used_as_message_id_when_no_message_id():
    evt = {"type": "async_delegation", "delegation_id": "durable-rlm-abc123"}
    assert _durable_message_id(evt) == "durable-rlm-abc123"


def test_explicit_message_id_wins():
    evt = {"type": "async_delegation", "delegation_id": "durable-rlm-abc123", "message_id": "plat-9"}
    assert _durable_message_id(evt) == "plat-9"


def test_no_ids_is_none():
    assert _durable_message_id({"type": "async_delegation"}) is None
