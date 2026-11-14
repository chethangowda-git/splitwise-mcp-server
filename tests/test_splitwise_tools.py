import json
from urllib.parse import parse_qs

import httpx2
import pytest
from mcp.client import Client

from splitwise_mcp_server.server import create_server

pytestmark = pytest.mark.anyio

RECEIPT = {
    "people": ["Ann", "Bob"],
    "items": [
        {"id": "pizza", "amount": "24.00", "split": "equal", "shares": [{"person": "Ann"}, {"person": "Bob"}]},
        {"id": "salad", "amount": "9.00", "shares": [{"person": "Ann"}]},
    ],
    "tax": {"amount": "2.32"},
    "tip": {"amount": "6.00"},
    "printed_total": "41.32",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_api(calls, errors=None, store=None):
    """A Splitwise stand-in. `store` holds created expenses, so later calls see earlier ones."""
    store = [] if store is None else store

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        path = request.url.path.removeprefix("/api/v3.0")
        if path == "/get_current_user":
            return httpx2.Response(200, json={"user": {"id": 1, "first_name": "Ann", "last_name": "A", "email": "ann@x.test"}})
        if path == "/get_friends":
            return httpx2.Response(200, json={"friends": [{"id": 2, "first_name": "Bob", "email": "bob@x.test"}]})
        if path == "/get_groups":
            return httpx2.Response(200, json={"groups": [
                {"id": 0, "name": "Non-group expenses", "members": []},
                {"id": 77, "name": "Roommates", "members": [{"id": 1, "first_name": "Ann"}, {"id": 2, "first_name": "Bob"}]},
            ]})
        if path == "/get_expenses":
            q = request.url.params
            assert "updated_after" in q
            found = [e for e in store if not q.get("group_id") or str(e["group_id"]) == q["group_id"]]
            offset, limit = int(q.get("offset", 0)), int(q.get("limit", 20))
            return httpx2.Response(200, json={"expenses": found[offset:offset + limit]})
        if path == "/create_expense":
            if errors:
                return httpx2.Response(200, json={"expenses": [], "errors": errors})
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            expense = {
                "id": 999 + len(store), "description": form["description"], "cost": form["cost"],
                "currency_code": form["currency_code"], "details": form["details"],
                "group_id": int(form["group_id"]) or None, "created_at": "2026-11-14T12:00:00Z", "deleted_at": None,
            }
            store.append(expense)
            return httpx2.Response(200, json={"expenses": [{"id": expense["id"]}], "errors": {}})
        return httpx2.Response(404)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def server(calls, **kw):
    return create_server(splitwise_token=lambda: "tok", http=fake_api(calls, **kw))


def add(**overrides):
    args = {"receipt": RECEIPT, "paid_by": "Ann", "members": {"Ann": 1, "Bob": 2}, "description": "Pizza night"}
    return {**args, **overrides}


def creates(calls):
    return [r for r in calls if r.url.path.endswith("/create_expense")]


async def test_tools_hidden_without_splitwise():
    async with Client(create_server()) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert "add_expense_to_splitwise" not in names


async def test_list_people():
    calls = []
    async with Client(server(calls)) as c:
        r = await c.call_tool("list_splitwise_people", {})
    assert not r.is_error, r.content
    assert r.structured_content["me"]["name"] == "Ann A"
    assert [g["id"] for g in r.structured_content["groups"]] == [77]


async def test_add_expense_posts_exact_shares():
    calls = []
    async with Client(server(calls)) as c:
        r = await c.call_tool("add_expense_to_splitwise", {
            "receipt": RECEIPT, "paid_by": "Ann", "members": {"Ann": 1, "Bob": "bob@x.test"},
            "description": "Pizza night", "group_id": 77,
        })
    assert not r.is_error, r.content
    assert r.structured_content["expense_id"] == 999
    form = {k: v[0] for k, v in parse_qs(calls[-1].content.decode()).items()}
    assert form["cost"] == "41.32"
    assert form["group_id"] == "77"
    assert form["users__0__user_id"] == "1"
    assert form["users__0__paid_share"] == "41.32"
    assert form["users__0__owed_share"] == "26.30"
    assert form["users__1__email"] == "bob@x.test"
    assert form["users__1__paid_share"] == "0.00"
    assert form["users__1__owed_share"] == "15.02"
    assert "Ann: 26.30" in form["details"]


async def test_add_expense_refuses_unreconciled_or_unmapped():
    calls = []
    async with Client(server(calls)) as c:
        bad_total = await c.call_tool("add_expense_to_splitwise", {
            "receipt": {**RECEIPT, "printed_total": "42.00"}, "paid_by": "Ann",
            "members": {"Ann": 1, "Bob": 2}, "description": "x",
        })
        unmapped = await c.call_tool("add_expense_to_splitwise", {
            "receipt": RECEIPT, "paid_by": "Ann", "members": {"Ann": 1}, "description": "x",
        })
    assert bad_total.is_error and "difference 0.68" in bad_total.content[0].text
    assert unmapped.is_error and "No Splitwise user for ['Bob']" in unmapped.content[0].text
    assert not any(r.url.path.endswith("/create_expense") for r in calls)


async def test_splitwise_errors_surface():
    calls = []
    async with Client(server(calls, errors={"base": ["Invalid user"]})) as c:
        r = await c.call_tool("add_expense_to_splitwise", {
            "receipt": RECEIPT, "paid_by": "Ann", "members": {"Ann": 1, "Bob": 2}, "description": "x",
        })
    assert r.is_error and "Invalid user" in r.content[0].text


async def test_second_add_is_refused_as_duplicate():
    calls = []
    async with Client(server(calls)) as c:
        first = await c.call_tool("add_expense_to_splitwise", add(group_id=77))
        second = await c.call_tool("add_expense_to_splitwise", add(group_id=77))
    assert not first.is_error, first.content
    assert second.is_error
    text = second.content[0].text
    assert "duplicate of Splitwise expense 999" in text and "allow_duplicate=true" in text
    assert len(creates(calls)) == 1
    lookup = [r for r in calls if r.url.path.endswith("/get_expenses")][-1]
    assert lookup.url.params["group_id"] == "77"


async def test_reparsed_receipt_still_matches():
    # Same bill, but renamed people/items, a new title, and Bob's id given as a string.
    reparsed = {
        **RECEIPT,
        "people": ["ann", "bob"],
        "items": [
            {"id": "margherita", "amount": "24.00", "split": "equal", "shares": [{"person": "ann"}, {"person": "bob"}]},
            {"id": "caesar", "amount": "9.00", "shares": [{"person": "ann"}]},
        ],
    }
    calls = []
    async with Client(server(calls)) as c:
        await c.call_tool("add_expense_to_splitwise", add())
        again = await c.call_tool("add_expense_to_splitwise", add(
            receipt=reparsed, paid_by="bob", members={"ann": 1, "bob": "2"}, description="Dinner",
        ))
    assert again.is_error and "duplicate" in again.content[0].text
    assert len(creates(calls)) == 1


async def test_allow_duplicate_overrides():
    calls = []
    async with Client(server(calls)) as c:
        await c.call_tool("add_expense_to_splitwise", add())
        r = await c.call_tool("add_expense_to_splitwise", add(allow_duplicate=True))
    assert not r.is_error, r.content
    assert r.structured_content["expense_id"] == 1000
    assert len(creates(calls)) == 2
    assert not any(r.url.path.endswith("/get_expenses") for r in calls[calls.index(creates(calls)[0]) + 1:])


async def test_different_bill_is_not_a_duplicate():
    calls = []
    async with Client(server(calls)) as c:
        await c.call_tool("add_expense_to_splitwise", add(date="2026-11-01"))
        other_day = await c.call_tool("add_expense_to_splitwise", add(date="2026-11-08"))
        other_group = await c.call_tool("add_expense_to_splitwise", add(date="2026-11-01", group_id=77))
    assert not other_day.is_error and not other_group.is_error
    assert len(creates(calls)) == 3


async def test_deleted_expense_does_not_block():
    calls, store = [], []
    async with Client(server(calls, store=store)) as c:
        await c.call_tool("add_expense_to_splitwise", add())
        store[0]["deleted_at"] = "2026-11-14T12:05:00Z"
        r = await c.call_tool("add_expense_to_splitwise", add())
    assert not r.is_error, r.content


async def test_duplicate_found_on_a_later_page():
    filler = [{"id": n, "details": "added by hand", "group_id": None, "deleted_at": None} for n in range(150)]
    calls, store = [], filler
    async with Client(server(calls, store=store)) as c:
        await c.call_tool("add_expense_to_splitwise", add(allow_duplicate=True))
        r = await c.call_tool("add_expense_to_splitwise", add())
    assert r.is_error and "duplicate of Splitwise expense 1149" in r.content[0].text
    offsets = [r.url.params["offset"] for r in calls if r.url.path.endswith("/get_expenses")]
    assert offsets == ["0", "100"]


async def test_lookup_failure_blocks_create():
    calls = []
    http = fake_api(calls)
    original = http.request

    async def failing(method, url, **kw):
        if url.endswith("/get_expenses"):
            return httpx2.Response(500, text="boom")
        return await original(method, url, **kw)

    http.request = failing
    async with Client(create_server(splitwise_token=lambda: "tok", http=http)) as c:
        r = await c.call_tool("add_expense_to_splitwise", add())
    assert r.is_error and "HTTP 500" in r.content[0].text
    assert not creates(calls)
