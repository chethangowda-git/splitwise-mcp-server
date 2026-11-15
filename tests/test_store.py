import fakeredis
import pytest

from splitwise_mcp_server.store import MemoryStore, RedisStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_memory_claim_is_first_come_and_expires():
    clock = Clock()
    s = MemoryStore(clock)
    assert await s.claim("k", 10) is True
    assert await s.claim("k", 10) is False
    assert await s.exists("k")
    clock.now += 10
    assert not await s.exists("k")
    assert await s.claim("k", 10) is True


async def test_memory_put_overwrites_and_prunes(monkeypatch):
    clock = Clock()
    s = MemoryStore(clock)
    monkeypatch.setattr(MemoryStore, "PRUNE_AT", 3)
    for k in "abc":
        await s.put(k, 5)
    clock.now += 6
    await s.put("d", 5)
    assert set(s._expiry) == {"d"}


async def test_redis_store_semantics():
    r = fakeredis.FakeAsyncRedis()
    s = RedisStore(r, prefix="t:")
    assert await s.claim("k", 30) is True
    assert await s.claim("k", 30) is False
    assert await s.exists("k") and not await s.exists("other")
    assert 0 < await r.ttl("t:k") <= 30
    await s.put("g", 0)  # a zero ttl still sets a short-lived mark instead of failing
    assert await s.exists("g")
