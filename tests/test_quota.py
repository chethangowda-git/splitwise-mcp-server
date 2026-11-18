"""The per-user change limit for the hosted server: adds, deletes, and settle-ups count."""

import anyio
import fakeredis
import httpx2
import pytest
from mcp.client import Client

import test_deletion as deletion
import test_settlements as settlements
import test_splitwise_tools as adds
from splitwise_mcp_server.quota import QuotaConfig, quota_config_from_env
from splitwise_mcp_server.server import create_server
from splitwise_mcp_server.store import MemoryStore, RedisStore

pytestmark = pytest.mark.anyio

CONTACT = "https://example.test/contact"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def add_server(calls, quota, store=None, **kw):
    return create_server(splitwise_token=lambda: "tok", http=adds.fake_api(calls, **kw), store=store, quota=quota)


async def test_adds_stop_at_the_limit_with_the_contact_link():
    calls = []
    async with Client(add_server(calls, QuotaConfig(limit=2, contact=CONTACT))) as c:
        for total in ("1", "2"):
            r = await adds.preview_and_add(c, description=f"Bill {total}", date=f"2026-11-0{total}")
            assert not r.is_error, adds.error_text(r)
        r = await c.call_tool("preview_splitwise_expense", adds.args(date="2026-11-03"))
    assert "allows 2 changes to Splitwise per user" in adds.error_text(r)
    assert CONTACT in adds.error_text(r)
    assert len(adds.creates(calls)) == 2


async def test_no_limit_by_default_and_exempt_users_are_unlimited():
    for quota in (None, QuotaConfig(limit=0, exempt=frozenset({adds.ME["id"]}))):
        calls = []
        async with Client(add_server(calls, quota)) as c:
            r = await adds.preview_and_add(c)
        assert not r.is_error, adds.error_text(r)


async def test_add_is_refused_even_with_a_preview_from_before_the_limit_was_reached():
    calls = []
    async with Client(add_server(calls, QuotaConfig(limit=1))) as c:
        early = await adds.preview(c, date="2026-11-01")
        assert not (await adds.preview_and_add(c, date="2026-11-02")).is_error
        r = await c.call_tool("add_expense_to_splitwise", {**adds.args(date="2026-11-01"), "confirmation": early["confirmation"]})
    assert "has used them all" in adds.error_text(r)
    assert len(adds.creates(calls)) == 1


async def test_a_failed_write_gives_the_change_back():
    store, quota = MemoryStore(), QuotaConfig(limit=1)
    async with Client(add_server([], quota, store, errors={"base": ["Splitwise said no"]})) as c:
        r = await adds.preview_and_add(c)
    assert "Splitwise said no" in adds.error_text(r)
    calls = []
    async with Client(add_server(calls, quota, store)) as c:
        r = await adds.preview_and_add(c)
    assert not r.is_error, adds.error_text(r)


async def test_racing_adds_cannot_both_pass_the_limit():
    calls, gate = [], {"hold": 0}
    async with Client(add_server(calls, QuotaConfig(limit=1), gate=gate)) as c:
        p1 = await adds.preview(c, date="2026-11-01")
        p2 = await adds.preview(c, date="2026-11-02")
        gate["hold"] = 2  # both adds pass their duplicate lookups together, then race for the last change
        results = []

        async def add(date, p):
            results.append(await c.call_tool("add_expense_to_splitwise", {**adds.args(date=date), "confirmation": p["confirmation"]}))

        async with anyio.create_task_group() as tg:
            tg.start_soon(add, "2026-11-01", p1)
            tg.start_soon(add, "2026-11-02", p2)
    assert sorted(r.is_error for r in results) == [False, True]
    assert len(adds.creates(calls)) == 1


async def test_deletes_and_settle_ups_share_the_same_limit():
    store, quota = MemoryStore(), QuotaConfig(limit=2)
    sw_del = deletion.FakeSplitwise()
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(sw_del.handler))
    async with Client(create_server(splitwise_token=lambda: "tok", http=http, store=store, quota=quota)) as c:
        p = await deletion.preview(c, 10)
        r = await c.call_tool("delete_splitwise_expense", {"expense_id": 10, "confirmation": p["confirmation"]})
        assert not r.is_error, deletion.text(r)

    sw_pay = settlements.FakeSplitwise()
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(sw_pay.handler))
    async with Client(create_server(splitwise_token=lambda: "tok", http=http, store=store, quota=quota)) as c:
        _, r = await settlements.preview_and_record(c, amount="10")
        assert not r.is_error, settlements.text(r)
        r = await c.call_tool("preview_splitwise_settlement", settlements.args(amount="5"))
        assert r.is_error and "Not recorded" in settlements.text(r)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(sw_del.handler))
    async with Client(create_server(splitwise_token=lambda: "tok", http=http, store=store, quota=quota)) as c:
        r = await c.call_tool("preview_splitwise_deletion", {"expense_id": 14})
    assert r.is_error and "Not deleted" in deletion.text(r)
    assert sw_del.deletes == [10] and len(sw_pay.posted) == 1


async def test_reading_and_splitting_never_count():
    store = MemoryStore()
    calls = []
    async with Client(add_server(calls, QuotaConfig(limit=0), store)) as c:
        assert not (await c.call_tool("split_receipt", {"receipt": adds.RECEIPT})).is_error
        assert not (await c.call_tool("list_splitwise_people", {})).is_error
        r = await c.call_tool("preview_splitwise_expense", adds.args())
    assert "allows 0 changes" in adds.error_text(r)
    assert await store.count(f"quota:writes:{adds.ME['id']}") == 0


async def test_redis_counters():
    s = RedisStore(fakeredis.FakeAsyncRedis(), prefix="t:")
    assert await s.count("c") == 0
    assert await s.incr("c") == 1 and await s.incr("c") == 2
    await s.decr("c")
    assert await s.count("c") == 1


def test_config_from_env(monkeypatch):
    monkeypatch.delenv("WRITE_QUOTA", raising=False)
    assert quota_config_from_env() is None
    monkeypatch.setenv("WRITE_QUOTA", "5")
    monkeypatch.setenv("QUOTA_EXEMPT_USER_IDS", "12, 34")
    monkeypatch.setenv("QUOTA_CONTACT", CONTACT)
    assert quota_config_from_env() == QuotaConfig(limit=5, exempt=frozenset({12, 34}), contact=CONTACT)
    for bad in ("five", "-1"):
        monkeypatch.setenv("WRITE_QUOTA", bad)
        with pytest.raises(SystemExit):
            quota_config_from_env()
