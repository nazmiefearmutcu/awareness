from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

try:
    import redis
except ImportError:
    redis = None

from awareness.obs.logging import get_logger

logger = get_logger("util.lock")


def parse_redis_url(url: str) -> str:
    """Parse and clean the Redis/Redlock URL for the redis client.

    Supports:
      - redis://... -> redis://...
      - rediss://... -> rediss://...
      - redlock://... -> redis://...
      - redlocks://... -> rediss://...
    """
    if url.startswith("redlock://"):
        return "redis://" + url[len("redlock://") :]
    if url.startswith("redlocks://"):
        return "rediss://" + url[len("redlocks://") :]
    return url


class RedisLock:
    """A Redis-based distributed lock to coordinate task execution or DB operations across workers."""

    def __init__(
        self,
        redis_url: str,
        name: str,
        expire_sec: float = 60.0,
        timeout_sec: float = 10.0,
    ) -> None:
        global redis
        if redis is None:
            try:
                import redis as _redis

                redis = _redis
            except ImportError:
                pass
        if redis is None:
            raise ImportError(
                "The 'redis' package is required to use Redis-based locking. "
                "Please install it using: pip install redis"
            )
        self.redis_url = parse_redis_url(redis_url)
        self.name = f"lock:awareness:{name}"
        self.expire_sec = expire_sec
        self.timeout_sec = timeout_sec
        self.token = str(uuid.uuid4())
        self._client: redis.Redis | None = None
        # H-29: a blackholed Redis must never hang a worker forever — bound
        # socket connect/read so acquire() honors timeout_sec in wall time.
        self._socket_timeout = min(5.0, max(0.1, float(timeout_sec)))

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(
                self.redis_url,
                socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._socket_timeout,
            )
        return self._client

    def acquire(self) -> bool:
        """Acquire the lock. Blocks up to timeout_sec."""
        start = time.time()
        expire_ms = int(self.expire_sec * 1000)

        while True:
            try:
                # Try to set the lock key if not exists
                if self.client.set(self.name, self.token, nx=True, px=expire_ms):
                    logger.debug("lock_acquired", name=self.name, token=self.token)
                    return True
            except Exception as e:
                logger.warning("lock_acquire_error", name=self.name, error=str(e))

            if time.time() - start >= self.timeout_sec:
                logger.warning("lock_acquire_timeout", name=self.name)
                return False

            time.sleep(0.1)

    def release(self) -> None:
        """Release the lock safely using a Lua script."""
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            res = self.client.register_script(lua_script)(keys=[self.name], args=[self.token])
            if res:
                logger.debug("lock_released", name=self.name, token=self.token)
            else:
                logger.debug("lock_release_skipped", name=self.name, reason="not_holder")
        except Exception as e:
            logger.warning("lock_release_failed", name=self.name, error=str(e))

    # Synchronous context manager
    def __enter__(self) -> RedisLock:
        if not self.acquire():
            raise RuntimeError(f"Could not acquire lock: {self.name}")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()

    # Asynchronous context manager
    async def __aenter__(self) -> RedisLock:
        loop = asyncio.get_running_loop()
        acquired = await loop.run_in_executor(None, self.acquire)
        if not acquired:
            raise RuntimeError(f"Could not acquire lock: {self.name}")
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.release)
