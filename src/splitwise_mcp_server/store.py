"""The little server-side state that OAuth needs: what was used, and what was revoked.

Everything else the server hands out is sealed and self-contained (see oauth.py).
OAuth 2.1 still requires remembering three things, so they live here:

- authorization codes that were already exchanged (codes are single-use),
- refresh tokens that were already rotated (a reused one means it was stolen),
- grants that were revoked (sign-out, or a detected replay).

Each entry expires together with the token it describes, so the store stays small.
Counters (per-user change limits, see quota.py) are the one thing kept without expiry.
Use RedisStore when the server runs as more than one instance or must survive
restarts; MemoryStore is for local runs, tests, and a single instance.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol


class Store(Protocol):
    async def claim(self, key: str, ttl: int) -> bool:
        """Atomically mark `key` for `ttl` seconds. True only for the first caller."""
        ...

    async def put(self, key: str, ttl: int) -> None:
        """Mark `key` for `ttl` seconds, whether or not it was already marked."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def incr(self, key: str) -> int:
        """Atomically add 1 to the counter `key` (no expiry) and return the new value."""
        ...

    async def decr(self, key: str) -> None: ...

    async def count(self, key: str) -> int: ...


class MemoryStore:
    """In-process store. Safe under asyncio: no await between a check and its write."""

    PRUNE_AT = 10_000

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._expiry: dict[str, float] = {}
        self._counts: dict[str, int] = {}

    def _live(self, key: str) -> bool:
        exp = self._expiry.get(key)
        if exp is None:
            return False
        if exp <= self._clock():
            del self._expiry[key]
            return False
        return True

    def _set(self, key: str, ttl: int) -> None:
        self._expiry[key] = self._clock() + max(1, ttl)
        if len(self._expiry) > self.PRUNE_AT:
            now = self._clock()
            self._expiry = {k: e for k, e in self._expiry.items() if e > now}

    async def claim(self, key: str, ttl: int) -> bool:
        if self._live(key):
            return False
        self._set(key, ttl)
        return True

    async def put(self, key: str, ttl: int) -> None:
        self._set(key, ttl)

    async def exists(self, key: str) -> bool:
        return self._live(key)

    async def incr(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    async def decr(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) - 1

    async def count(self, key: str) -> int:
        return self._counts.get(key, 0)


class RedisStore:
    """Shared store for several instances; entries survive restarts."""

    def __init__(self, client: Any, prefix: str = "swmcp:"):
        self._r = client
        self._prefix = prefix

    @classmethod
    def from_url(cls, url: str) -> RedisStore:
        from redis.asyncio import from_url

        return cls(from_url(url))

    async def claim(self, key: str, ttl: int) -> bool:
        return bool(await self._r.set(self._prefix + key, b"1", nx=True, ex=max(1, ttl)))

    async def put(self, key: str, ttl: int) -> None:
        await self._r.set(self._prefix + key, b"1", ex=max(1, ttl))

    async def exists(self, key: str) -> bool:
        return bool(await self._r.exists(self._prefix + key))

    async def incr(self, key: str) -> int:
        return int(await self._r.incr(self._prefix + key))

    async def decr(self, key: str) -> None:
        await self._r.decr(self._prefix + key)

    async def count(self, key: str) -> int:
        return int(await self._r.get(self._prefix + key) or 0)
