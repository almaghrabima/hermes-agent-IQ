import os
import pytest
from pathlib import Path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_device_file_created_and_stable(home):
    from agent import device_identity as di
    a = di.get_device_id()
    n = di.get_device_number()
    assert isinstance(a, str) and len(a) >= 16
    assert 0 <= n <= 65535
    assert (home / "device.json").exists()
    # Stable across calls (re-read file, not regenerate)
    di._reset_cache()  # forces re-read from disk
    assert di.get_device_id() == a
    assert di.get_device_number() == n


@pytest.mark.parametrize("content", [
    "{not json",
    '{"device_id": 123, "device_number": 5}',          # device_id wrong type (int)
    '{"device_id": "abc", "device_number": 99999}',    # device_number out of range
    '{"device_id": "abc", "device_number": true}',     # device_number is bool
])
def test_corrupt_device_file_regenerates(home, content):
    from agent import device_identity as di
    di._reset_cache()
    (home / "device.json").write_text(content, encoding="utf-8")
    di._reset_cache()
    # Must not raise; regenerates a valid identity
    assert isinstance(di.get_device_id(), str)
    assert 0 <= di.get_device_number() <= 65535


def test_snowflake_ids_monotonic_and_fit_63_bits(home):
    from agent.device_identity import SnowflakeGenerator
    g = SnowflakeGenerator(device_number=7)
    ids = [g.next_id(now_ms=1_700_000_000_000) for _ in range(100)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 100  # all unique even within one ms
    assert all(0 < i < (1 << 63) for i in ids)


def test_snowflake_partitioned_by_device(home):
    from agent.device_identity import SnowflakeGenerator
    g1 = SnowflakeGenerator(device_number=1)
    g2 = SnowflakeGenerator(device_number=2)
    # Same ms, same seq slot -> different ids because device differs
    assert g1.next_id(now_ms=1_700_000_000_000) != g2.next_id(now_ms=1_700_000_000_000)


def test_snowflake_clock_backwards_stays_monotonic(home):
    from agent.device_identity import SnowflakeGenerator
    g = SnowflakeGenerator(device_number=3)
    first = g.next_id(now_ms=1_700_000_000_000)
    # Clock jumps backward; generator must not emit a smaller id
    second = g.next_id(now_ms=1_699_999_999_000)
    assert second > first


def test_seq_exhaustion_rolls_to_next_ms(home):
    from agent.device_identity import SnowflakeGenerator
    g = SnowflakeGenerator(device_number=4)
    # 6 seq bits = 64 ids per ms; the 65th in the same ms must advance the ms
    ids = [g.next_id(now_ms=1_700_000_000_000) for _ in range(65)]
    assert len(set(ids)) == 65
    assert ids == sorted(ids)


def test_snowflake_fits_63_bits_in_year_2080(home):
    from agent.device_identity import SnowflakeGenerator
    g = SnowflakeGenerator(device_number=9)
    # ~2080-01-01 in ms — would overflow 2^63 WITHOUT the custom epoch
    future_ms = 3_471_292_800_000
    i = g.next_id(now_ms=future_ms)
    assert 0 < i < (1 << 63)
