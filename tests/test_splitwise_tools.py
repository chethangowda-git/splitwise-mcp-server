from urllib.parse import parse_qs

import httpx2
import pytest
from mcp.client import Client

from splitwise_mcp_server.confirm import Confirmer
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

# The signed-in user is Ann (1). Friends include two Johns. Dana (6) shares a group but isn't a friend.
ME = {"id": 1, "first_name": "Ann", "last_name": "A", "email": "ann@x.test"}
FRIENDS = [
    {"id": 2, "first_name": "Bob", "last_name": "Smith", "email": "bob@x.test"},
    {"id": 3, "first_name": "John", "last_name": "Smith", "email": "john.smith@x.test"},
    {"id": 4, "first_name": "John", "last_name": "Doe", "email": "jd@x.test"},
    {"id": 5, "first_name": "Cara", "last_name": "Diaz", "email": "cara@x.test"},
]
GROUPS = [
    {"id": 0, "name": "Non-group expenses", "members": []},
    {"id": 77, "name": "Roommates", "members": [{"id": 1, "first_name": "Ann"}, {"id": 2, "first_name": "Bob"}]},
    {"id": 88, "name": "Trip", "members": [{"id": 1, "first_name": "Ann"}, {"id": 6, "first_name": "Dana", "last_name": "Lee"}]},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_api(calls, errors=None, store=None, fail=None):
    """A Splitwise stand-in. `store` holds created expenses, so later calls see earlier ones.
    `fail` is a set of paths that return HTTP 500 while they're in it."""
    store = [] if store is None else store
    fail = set() if fail is None else fail

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        path = request.url.path.removeprefix("/api/v3.0")
        if path in fail:
            return httpx2.Response(500, text="boom")
        if path == "/get_current_user":
            return httpx2.Response(200, json={"user": ME})
        if path == "/get_friends":
            return httpx2.Response(200, json={"friends": FRIENDS})
        if path == "/get_groups":
            return httpx2.Response(200, json={"groups": GROUPS})
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


def args(**overrides):
    base = {"receipt": RECEIPT, "paid_by": "Ann", "members": {"Ann": 1, "Bob": 2}, "description": "Pizza night"}
    return {**base, **overrides}


async def preview(c, **kw):
    r = await c.call_tool("preview_splitwise_expense", args(**kw))
    assert not r.is_error, r.content[0].text
    return r.structured_content


async def preview_and_add(c, allow_duplicate=False, **kw):
    p = await preview(c, **kw)
    extra = {"allow_duplicate": True} if allow_duplicate else {}
    return await c.call_tool("add_expense_to_splitwise", {**args(**kw), "confirmation": p["confirmation"], **extra})


def creates(calls):
    return [r for r in calls if r.url.path.endswith("/create_expense")]


def posted(calls):
    return {k: v[0] for k, v in parse_qs(creates(calls)[-1].content.decode(), keep_blank_values=True).items()}


def error_text(r):
    assert r.is_error, r.content
    return r.content[0].text


# ---------------------------------------------------------------- tool list and people


async def test_tools_hidden_without_splitwise():
    async with Client(create_server()) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"reconcile_receipt", "split_receipt", "allocate_amount"}


async def test_list_people():
    calls = []
    async with Client(server(calls)) as c:
        r = await c.call_tool("list_splitwise_people", {})
    assert not r.is_error, r.content
    assert r.structured_content["me"]["name"] == "Ann A"
    assert [g["id"] for g in r.structured_content["groups"]] == [77, 88]


async def test_find_people_reports_match_ambiguous_and_none():
    calls = []
    async with Client(server(calls)) as c:
        r = await c.call_tool("find_splitwise_people", {"names": ["me", "bob smith", "John", "Jon", "jd@x.test", "Dana", "Zed"]})
    assert not r.is_error, r.content
    me, bob, john, jon, by_email, dana, zed = r.structured_content["results"]

    assert (me["status"], me["matched_by"], me["candidates"][0]["id"]) == ("match", "me", 1)
    assert (bob["status"], bob["matched_by"], bob["candidates"][0]["id"]) == ("match", "full_name", 2)
    # Two Johns: never picks one, and gives the emails to ask with.
    assert john["status"] == "ambiguous"
    assert {(x["name"], x["email"]) for x in john["candidates"]} == {
        ("John Smith", "john.smith@x.test"), ("John Doe", "jd@x.test")
    }
    assert jon["status"] == "none" and {x["id"] for x in jon["suggestions"]} >= {3, 4}
    assert (by_email["status"], by_email["candidates"][0]["name"]) == ("match", "John Doe")
    assert dana["status"] == "match" and dana["candidates"][0]["groups"] == ["Trip"]
    assert dana["candidates"][0]["friend"] is False
    assert zed["status"] == "none" and zed["candidates"] == []


# ---------------------------------------------------------------- mapping checks


@pytest.mark.parametrize("members,group_id,expected", [
    ({"Ann": 1, "Bob": 999}, 0, "user id 999 is not you, one of your friends, or a member of your groups"),
    ({"Ann": 1, "Bob": "John Smith"}, 0, "is not a Splitwise user id or an email address"),
    ({"Ann": 1, "Bob": 5}, 77, "Cara Diaz is not a member of the group 'Roommates'"),
    ({"Ann": 1, "Bob": 2}, 1234, "group_id 1234 is not one of your Splitwise groups"),
    ({"Ann": 1, "Bob": 1}, 0, "all map to the same Splitwise user"),
    ({"Ann": 1, "Bob": "ANN@x.test"}, 0, "all map to the same Splitwise user"),
])
async def test_bad_mappings_are_refused_before_anything_is_posted(members, group_id, expected):
    calls = []
    async with Client(server(calls)) as c:
        p = await c.call_tool("preview_splitwise_expense", args(members=members, group_id=group_id))
        a = await c.call_tool(
            "add_expense_to_splitwise", {**args(members=members, group_id=group_id), "confirmation": "x"}
        )
    assert expected in error_text(p)
    assert expected in error_text(a)
    assert not creates(calls)


async def test_known_email_resolves_to_the_contact():
    calls = []
    async with Client(server(calls)) as c:
        r = await preview_and_add(c, members={"Ann": 1, "Bob": "BOB@x.test"})
    assert not r.is_error, r.content
    form = posted(calls)
    assert form["users__1__user_id"] == "2" and "users__1__email" not in form


async def test_unknown_email_is_a_new_person():
    calls = []
    async with Client(server(calls)) as c:
        p = await preview(c, members={"Ann": 1, "Bob": "newbie@x.test"})
        bob = p["people"][1]
        assert bob["new_to_splitwise"] is True and bob["splitwise_id"] is None
        assert (
            'newbie@x.test (not in your Splitwise contacts; Splitwise will invite them as "Bob") owes 15.02'
        ) in p["summary"]
        r = await c.call_tool("add_expense_to_splitwise", {
            **args(members={"Ann": 1, "Bob": "newbie@x.test"}), "confirmation": p["confirmation"],
        })
    assert not r.is_error, r.content
    form = posted(calls)
    # Splitwise needs email + first_name + last_name for someone identified by email.
    assert (form["users__1__email"], form["users__1__first_name"], form["users__1__last_name"]) == (
        "newbie@x.test", "Bob", ""
    )
    assert "users__1__user_id" not in form


async def test_add_refuses_unreconciled_or_unmapped():
    calls = []
    async with Client(server(calls)) as c:
        bad_total = await c.call_tool("add_expense_to_splitwise", {
            **args(receipt={**RECEIPT, "printed_total": "42.00"}), "confirmation": "x",
        })
        unmapped = await c.call_tool("add_expense_to_splitwise", {**args(members={"Ann": 1}), "confirmation": "x"})
    assert "difference 0.68" in error_text(bad_total)
    assert "No Splitwise user for ['Bob']" in error_text(unmapped)
    assert not creates(calls)


# ---------------------------------------------------------------- preview and confirmation


async def test_preview_shows_exactly_what_will_be_posted_and_posts_nothing():
    calls = []
    async with Client(server(calls)) as c:
        p = await preview(c, group_id=77, date="2026-11-14")
    assert not creates(calls)
    assert p["summary"].splitlines() == [
        '"Pizza night": 41.32 USD, in group "Roommates", dated 2026-11-14',
        "Paid by Ann A (you)",
        '- Ann A (you) owes 26.30  (receipt: "Ann")',
        '- Bob Smith <bob@x.test> owes 15.02  (receipt: "Bob")',
    ]
    assert [(x["splitwise_id"], x["paid"], x["owes"]) for x in p["people"]] == [(1, "41.32", "26.30"), (2, "0.00", "15.02")]
    assert p["duplicate_of"] is None


async def test_add_posts_exact_shares():
    calls = []
    async with Client(server(calls)) as c:
        r = await preview_and_add(c, group_id=77)
    assert not r.is_error, r.content
    assert r.structured_content["expense_id"] == 999
    form = posted(calls)
    assert form["cost"] == "41.32"
    assert form["group_id"] == "77"
    assert (form["users__0__user_id"], form["users__0__paid_share"], form["users__0__owed_share"]) == ("1", "41.32", "26.30")
    assert (form["users__1__user_id"], form["users__1__paid_share"], form["users__1__owed_share"]) == ("2", "0.00", "15.02")
    assert "Ann: 26.30" in form["details"]


@pytest.mark.parametrize("change", [
    {"description": "Something else"},
    {"paid_by": "Bob"},
    {"date": "2026-11-15"},
    {"receipt": {**RECEIPT, "tip": {"amount": "7.00"}, "printed_total": "42.32"}},
])
async def test_add_refuses_anything_that_differs_from_the_preview(change):
    calls = []
    async with Client(server(calls)) as c:
        p = await preview(c)
        r = await c.call_tool("add_expense_to_splitwise", {**args(**change), "confirmation": p["confirmation"]})
    assert "the expense is not the one that was previewed" in error_text(r)
    assert not creates(calls)


async def test_add_refuses_a_made_up_confirmation():
    calls = []
    async with Client(server(calls)) as c:
        r = await c.call_tool("add_expense_to_splitwise", {**args(), "confirmation": "looks-fine"})
    assert "not a confirmation code from preview_splitwise_expense" in error_text(r)
    assert not creates(calls)


def test_confirmation_expires_and_is_bound_to_the_account_and_secret():
    form = {"cost": "1.00"}
    a, b = Confirmer("s" * 40), Confirmer("s" * 40)
    code = a.sign(form, "tok", now=1000)
    assert b.problem(code, form, "tok", now=1000) is None  # any instance with the same secret accepts it
    assert "expired" in a.problem(code, form, "tok", now=1000 + 31 * 60)
    assert "not the one that was previewed" in a.problem(code, form, "other-account", now=1000)
    assert "not the one that was previewed" in Confirmer("x" * 40).problem(code, form, "tok", now=1000)


async def test_splitwise_errors_surface():
    calls = []
    async with Client(server(calls, errors={"base": ["Invalid user"]})) as c:
        r = await preview_and_add(c)
    assert "Invalid user" in error_text(r)


# ---------------------------------------------------------------- duplicates


async def test_second_add_is_refused_as_duplicate():
    calls = []
    async with Client(server(calls)) as c:
        first = await preview_and_add(c, group_id=77)
        p = await preview(c, group_id=77)
        second = await c.call_tool("add_expense_to_splitwise", {**args(group_id=77), "confirmation": p["confirmation"]})
    assert not first.is_error, first.content
    assert p["duplicate_of"]["expense_id"] == 999 and "Warning: this looks like expense 999" in p["summary"]
    text = error_text(second)
    assert "duplicate of Splitwise expense 999" in text and "allow_duplicate=true" in text
    assert len(creates(calls)) == 1
    lookup = [r for r in calls if r.url.path.endswith("/get_expenses")][-1]
    assert lookup.url.params["group_id"] == "77"


async def test_reparsed_receipt_still_matches():
    # Same bill, but renamed people/items, a new title, a different payer, and Bob's id as a string.
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
        await preview_and_add(c)
        again = await preview_and_add(c, receipt=reparsed, paid_by="bob", members={"ann": 1, "bob": "2"}, description="Dinner")
    assert "duplicate" in error_text(again)
    assert len(creates(calls)) == 1


async def test_allow_duplicate_overrides():
    calls = []
    async with Client(server(calls)) as c:
        await preview_and_add(c)
        r = await preview_and_add(c, allow_duplicate=True)
    assert not r.is_error, r.content
    assert r.structured_content["expense_id"] == 1000
    assert len(creates(calls)) == 2


async def test_different_bill_is_not_a_duplicate():
    calls = []
    async with Client(server(calls)) as c:
        await preview_and_add(c, date="2026-11-01")
        other_day = await preview_and_add(c, date="2026-11-08")
        other_group = await preview_and_add(c, date="2026-11-01", group_id=77)
    assert not other_day.is_error and not other_group.is_error
    assert len(creates(calls)) == 3


async def test_deleted_expense_does_not_block():
    calls, store = [], []
    async with Client(server(calls, store=store)) as c:
        await preview_and_add(c)
        store[0]["deleted_at"] = "2026-11-14T12:05:00Z"
        r = await preview_and_add(c)
    assert not r.is_error, r.content


async def test_duplicate_found_on_a_later_page():
    filler = [{"id": n, "details": "added by hand", "group_id": None, "deleted_at": None} for n in range(150)]
    calls, store = [], filler
    async with Client(server(calls, store=store)) as c:
        await preview_and_add(c, allow_duplicate=True)
        calls.clear()
        r = await preview_and_add(c)
    assert "duplicate of Splitwise expense 1149" in error_text(r)
    offsets = [r.url.params["offset"] for r in calls if r.url.path.endswith("/get_expenses")]
    assert offsets == ["0", "100", "0", "100"]  # the preview's scan, then the add's


async def test_lookup_failure_blocks_create():
    calls, fail = [], set()
    async with Client(server(calls, fail=fail)) as c:
        p = await preview(c)
        fail.add("/get_expenses")
        r = await c.call_tool("add_expense_to_splitwise", {**args(), "confirmation": p["confirmation"]})
    assert "HTTP 500" in error_text(r)
    assert not creates(calls)
