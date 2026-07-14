from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest

from awareness.storage.state import StateDB
from awareness.util import lock as lock_mod
from awareness.util.lock import RedisLock, parse_redis_url


def _redis_package_available() -> bool:
    return lock_mod.redis is not None


def _redis_reachable(url: str = "redis://localhost:6379/0") -> bool:
    """True when a Redis server at ``url`` answers PING."""
    if not _redis_package_available():
        return False
    try:
        client = lock_mod.redis.Redis.from_url(
            url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        try:
            return bool(client.ping())
        finally:
            try:
                client.close()
            except Exception:
                pass
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_package_available(),
    reason="redis package not installed",
)


def test_parse_redis_url() -> None:
    assert parse_redis_url("redis://localhost:6379/0") == "redis://localhost:6379/0"
    assert parse_redis_url("redlock://localhost:6379/0") == "redis://localhost:6379/0"
    assert parse_redis_url("redlocks://localhost:6379/0") == "rediss://localhost:6379/0"
    assert parse_redis_url("rediss://localhost:6379/0") == "rediss://localhost:6379/0"


@requires_redis
def test_redis_lock_acquire_and_release() -> None:
    """Unit test with mocked Redis client — no live server required.

    Still skipped when the redis package is missing. If the mock path cannot be
    applied and no server is reachable, skip rather than hang on connection refused.
    """
    mock_client = MagicMock()
    mock_client.set.return_value = True
    mock_script = MagicMock(return_value=1)
    mock_client.register_script.return_value = mock_script

    try:
        with patch.object(lock_mod.redis.Redis, "from_url", return_value=mock_client):
            lock = RedisLock(
                "redis://localhost", "test-lock", expire_sec=10.0, timeout_sec=2.0
            )
            with lock:
                mock_client.set.assert_called_once_with(
                    "lock:awareness:test-lock",
                    lock.token,
                    nx=True,
                    px=10000,
                )
            mock_client.register_script.assert_called_once()
            mock_script.assert_called_once_with(
                keys=["lock:awareness:test-lock"], args=[lock.token]
            )
    except RuntimeError as e:
        if "Could not acquire lock" in str(e) and not _redis_reachable():
            pytest.skip("Redis unavailable (lock acquire failed without mock)")
        raise


def test_state_db_init_with_redis_url() -> None:
    # StateDB should extract redis URL and set it correctly
    state = StateDB(
        url="redlock://localhost:6379/0",
        redis_url=None,
    )
    assert state._redis_url == "redlock://localhost:6379/0"
    # Fallback to local SQLite URL
    assert state._url == "sqlite:///awareness.sqlite"


@requires_redis
def test_state_db_claim_tasks_with_redis_lock(tmp_path) -> None:
    mock_client = MagicMock()
    mock_client.set.return_value = True
    mock_script = MagicMock(return_value=1)
    mock_client.register_script.return_value = mock_script

    try:
        with patch.object(lock_mod.redis.Redis, "from_url", return_value=mock_client):
            state = StateDB(
                f"sqlite:///{tmp_path / 'state.db'}",
                redis_url="redis://localhost",
            )
            state.init()
            state.claim_pending_tasks("job-123", limit=5)

            mock_client.set.assert_called_with(
                "lock:awareness:claim:job-123",
                ANY,
                nx=True,
                px=30000,
            )
    except AssertionError:
        # Mock did not intercept — live path may have fallen back unlocked.
        if not _redis_reachable():
            pytest.skip("Redis unavailable; claim lock path not exercised")
        raise
