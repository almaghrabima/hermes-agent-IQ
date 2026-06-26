"""turso_vector provider is discoverable and loads without its heavy deps."""
from plugins.memory import load_memory_provider


def test_provider_loads_by_name():
    provider = load_memory_provider("turso_vector")
    assert provider is not None
    assert provider.name == "turso_vector"


def test_is_available_does_not_raise():
    provider = load_memory_provider("turso_vector")
    # Must be a bool and must not import/install heavy deps just to answer.
    assert isinstance(provider.is_available(), bool)
