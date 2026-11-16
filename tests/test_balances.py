import httpx2
import pytest
from mcp.client import Client

from splitwise_mcp_server.server import create_server

pytestmark = pytest.mark.anyio

# Signed in as Ann (1). Positive friend balances mean the friend owes Ann.
ME = {"id": 1, "first_name": "Ann", "last_name": "A", "email": "ann@x.test"}
FRIENDS = [
    {"id": 2, "first_name": "Bob", "last_name": "Smith", "email": "bob@x.test",
     "balance": [{"currency_code": "USD", "amount": "30.5"}],
     "groups": [{"group_id": 77, "balance": [{"currency_code": "USD", "amount": "20.5"}]},
                {"group_id": 0, "balance": [{"currency_code": "USD", "amount": "10.0"}]}]},
    {"id": 5, "first_name": "Cara", "last_name": "Diaz", "email": "cara@x.test",
     "balance": [{"currency_code": "USD", "amount": "-12.25"}, {"currency_code": "INR", "amount": "500.0"}],
     "groups": [{"group_id": 77, "balance": [{"currency_code": "USD", "amount": "-12.25"}]},
                {"group_id": 88, "balance": [{"currency_code": "INR", "amount": "500.0"}]}]},
    {"id": 7, "first_name": "Dan", "email": "dan@x.test", "balance": [], "groups": []},
]
MEMBERS = {
    1: {"id": 1, "first_name": "Ann", "last_name": "A"},
    2: {"id": 2, "first_name": "Bob", "last_name": "Smith"},
    5: {"id": 5, "first_name": "Cara", "last_name": "Diaz"},
}
GROUPS = [
    {"id": 0, "name": "Non-group expenses", "members": []},
    {"id": 77, "name": "Roommates", "simplify_by_default": True,
     "members": [{**MEMBERS[1], "balance": [{"currency_code": "USD", "amount": "8.25"}]}, MEMBERS[2], MEMBERS[5]],
     "simplified_debts": [{"from": 2, "to": 1, "amount": "20.5", "currency_code": "USD"},
                          {"from": 1, "to": 5, "amount": "12.25", "currency_code": "USD"}],
     "original_debts": [{"from": 2, "to": 5, "amount": "99.0", "currency_code": "USD"}]},
    {"id": 88, "name": "Trip", "simplify_by_default": False,
     "members": [{**MEMBERS[1], "balance": [{"currency_code": "INR", "amount": "500.0"}]}, MEMBERS[5]],
     "simplified_debts": [], "original_debts": [{"from": 5, "to": 1, "amount": "500.0", "currency_code": "INR"}]},
    {"id": 99, "name": "Settled", "members": [{**MEMBERS[1], "balance": []}], "original_debts": []},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def server():
    def handler(request):
        path = request.url.path.removeprefix("/api/v3.0")
        assert request.method == "GET", "balances must only read"
        body = {"/get_current_user": {"user": ME}, "/get_friends": {"friends": FRIENDS}, "/get_groups": {"groups": GROUPS}}
        return httpx2.Response(200, json=body[path])

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return create_server(splitwise_token=lambda: "tok", http=http)


async def balances(**kw):
    async with Client(server()) as c:
        r = await c.call_tool("get_splitwise_balances", kw)
    return r


def amounts(rows):
    return {a["currency"]: a["amount"] for a in rows}


async def test_totals_are_per_currency_and_signed_from_the_users_side():
    b = (await balances()).structured_content
    assert amounts(b["owed_to_you"]) == {"USD": "30.5", "INR": "500.0"}
    assert amounts(b["you_owe"]) == {"USD": "12.25"}
    assert amounts(b["net"]) == {"INR": "500.0", "USD": "18.25"}
    # Zero balances are left out; the largest balance comes first.
    assert [f["name"] for f in b["friends"]] == ["Cara Diaz", "Bob Smith"]
    bob = b["friends"][1]
    assert {(g["group_name"], amounts(g["balances"])["USD"]) for g in bob["by_group"]} == {
        ("Roommates", "20.5"), ("Non-group expenses", "10.0")
    }


async def test_group_debts_follow_the_groups_simplify_setting():
    groups = {g["name"]: g for g in (await balances()).structured_content["groups"]}
    assert set(groups) == {"Roommates", "Trip"}  # "Settled" has nothing outstanding
    assert [(d["from_name"], d["to_name"], d["amount"]) for d in groups["Roommates"]["debts"]] == [
        ("Bob Smith", "Ann A", "20.5"), ("Ann A", "Cara Diaz", "12.25")
    ]
    assert [(d["from_name"], d["to_name"], d["currency"]) for d in groups["Trip"]["debts"]] == [
        ("Cara Diaz", "Ann A", "INR")
    ]
    assert amounts(groups["Roommates"]["your_balance"]) == {"USD": "8.25"}


async def test_one_group_only():
    b = (await balances(group_id=77)).structured_content
    assert {f["name"]: amounts(f["balances"]) for f in b["friends"]} == {
        "Bob Smith": {"USD": "20.5"}, "Cara Diaz": {"USD": "-12.25"}
    }
    assert amounts(b["owed_to_you"]) == {"USD": "20.5"} and amounts(b["you_owe"]) == {"USD": "12.25"}
    assert [g["name"] for g in b["groups"]] == ["Roommates"]

    non_group = (await balances(group_id=0)).structured_content
    assert {f["name"]: amounts(f["balances"]) for f in non_group["friends"]} == {"Bob Smith": {"USD": "10.0"}}


async def test_unknown_group_is_an_error():
    r = await balances(group_id=1234)
    assert r.is_error and "not one of your Splitwise groups" in r.content[0].text


async def test_tool_calls_are_logged_by_name_without_arguments(caplog):
    caplog.set_level("INFO", logger="splitwise_mcp_server.server")
    async with Client(server()) as c:
        await c.list_tools()
        await c.call_tool("get_splitwise_balances", {"group_id": 77})
        await c.call_tool("get_splitwise_balances", {"group_id": 1234})
    lines = [r.getMessage() for r in caplog.records if r.name == "splitwise_mcp_server.server"]
    assert any(l.startswith("list_tools -> ") and "get_splitwise_balances" in l for l in lines)
    assert any(l.startswith("tool=get_splitwise_balances outcome=ok ms=") for l in lines)
    assert any(l.startswith("tool=get_splitwise_balances outcome=error ms=") for l in lines)
    assert not any("77" in l.split("ms=")[0] or "1234" in l for l in lines)
