from __future__ import annotations

import sys
from unittest.mock import MagicMock
# Mock redis module for environments without redis installed
mock_redis = MagicMock()
sys.modules["redis"] = mock_redis

from unittest.mock import patch, ANY
import pytest

from awareness.storage.state import StateDB
from awareness.util.lock import RedisLock, parse_redis_url


def test_parse_redis_url() -> None:
    assert parse_redis_url("redis://localhost:6379/0") == "redis://localhost:6379/0"
    assert parse_redis_url("redlock://localhost:6379/0") == "redis://localhost:6379/0"
    assert parse_redis_url("redlocks://localhost:6379/0") == "rediss://localhost:6379/0"
    assert parse_redis_url("rediss://localhost:6379/0") == "rediss://localhost:6379/0"


@patch("redis.Redis.from_url")
def test_redis_lock_acquire_and_release(mock_from_url) -> None:
    mock_client = MagicMock()
    mock_from_url.return_value = mock_client

    # Simulate successful lock acquisition (client.set returns True)
    mock_client.set.return_value = True

    # Simulate successful release Lua script execution
    mock_script = MagicMock()
    mock_script.return_value = 1
    mock_client.register_script.return_value = mock_script

    lock = RedisLock("redis://localhost", "test-lock", expire_sec=10.0, timeout_sec=2.0)

    # Test block context manager
    with lock:
        mock_client.set.assert_called_once_with(
            "lock:awareness:test-lock",
            lock.token,
            nx=True,
            px=10000,
        )

    # Verify release script executed
    mock_client.register_script.assert_called_once()
    mock_script.assert_called_once_with(keys=["lock:awareness:test-lock"], args=[lock.token])


@patch("redis.Redis.from_url")
def test_state_db_init_with_redis_url(mock_from_url) -> None:
    # StateDB should extract redis URL and set it correctly
    state = StateDB(
        url="redlock://localhost:6379/0",
        redis_url=None,
    )
    assert state._redis_url == "redlock://localhost:6379/0"
    # Fallback to local SQLite URL
    assert state._url == "sqlite:///awareness.sqlite"


@patch("redis.Redis.from_url")
def test_state_db_claim_tasks_with_redis_lock(mock_from_url, tmp_path) -> None:
    mock_client = MagicMock()
    mock_from_url.return_value = mock_client
    mock_client.set.return_value = True

    mock_script = MagicMock()
    mock_script.return_value = 1
    mock_client.register_script.return_value = mock_script

    # Create StateDB with a redis url
    state = StateDB(f"sqlite:///{tmp_path / 'state.db'}", redis_url="redis://localhost")
    state.init()

    # Call claim_pending_tasks, verify it acquires the lock
    state.claim_pending_tasks("job-123", limit=5)

    # Verify Redis set was called with claim:job-123 lock name
    mock_client.set.assert_called_with(
        "lock:awareness:claim:job-123",
        ANY,
        nx=True,
        px=30000,
    )
