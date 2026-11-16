from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx2
import pytest
from mcp.client import Client

from splitwise_mcp_server.server import create_server

pytestmark = pytest.mark.anyio

ME = {"id": 1, "first_name": "Ann", "last_name": "A"}
# Bob owes Ann 25.70 in Roommates (77) and 10 outside groups; Ann owes Cara 8 in Roommates.
FRIENDS = [
    {"id": 2, "first_name": "Bob", "last_name": "Smith",
     "groups": [{"group_id": 77, "balance": [{"currency_code": "USD", "amount": "25.7"}]},
                {"group_id": 0, "balance": [{"currency_code": "USD", "amount": "10.0"}]}]},
    {"id": 5, "first_name": "Cara", "last_name": "Diaz",
     "groups": [{"group_id": 77, "balance": [{"currency_code": "USD", "amount": "-8.0"}]}]},
    {"id": 6, "first_name": "Dana", "last_name": "Lee", "groups": []},
]
GROUPS = [
    {"id": 0, "name": "Non-group expenses", "members": []},
    {"id": 77, "name": "Roommates", "members": [{"id": 1}, {"id": 2}, {"id": 5}]},
    {"id": 88, "name": "Trip", "members": [{"id": 1}, {"id": 6}]},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeSplitwise:
    def __init__(self):
        self.recent: list[dict] = []
        self.posted: list[dict] = []
        self.save_as_expense = False

    def handler(self, request):
        path = request.url.path.removeprefix("/api/v3.0")
        if path == "/get_current_user":
            return httpx2.Response(200, json={"user": ME})
        if path == "/get_friends":
            return httpx2.Response(200, json={"friends": FRIENDS})
        if path == "/get_groups":
            return httpx2.Response(200, json={"groups": GROUPS})
        if path == "/get_expenses":
            return httpx2.Response(200, json={"expenses": self.recent})
        if path == "/create_expense":
            form = {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}
            self.posted.append(form)
            users = [{"user_id": int(form[f"users__{i}__user_id"]), "paid_share": form[f"users__{i}__paid_share"],
                      "owed_share": form[f"users__{i}__owed_share"]} for i in (0, 1)]
            e = {"id": 500 + len(self.posted), "cost": form["cost"], "currency_code": form["currency_code"],
                 "group_id": int(form["group_id"]) or None, "payment": not self.save_as_expense, "users": users,
                 "created_at": datetime.now(timezone.utc).isoformat()}
            self.recent.append(e)
            return httpx2.Response(200, json={"expenses": [e], "errors": {}})
        return httpx2.Response(404)

    def server(self):
        return create_server(splitwise_token=lambda: "tok", http=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)))


def text(r):
    return r.content[0].text


def args(**kw):
    return {"friend_id": 2, "amount": "25.70", "direction": "they_paid_me", "group_id": 77, **kw}


async def preview_and_record(c, **kw):
    p = await c.call_tool("preview_splitwise_settlement", args(**kw))
    assert not p.is_error, text(p)
    extra = {k: kw.pop(k) for k in ("allow_duplicate",) if k in kw}
    r = await c.call_tool("record_splitwise_settlement", {**args(**kw), "confirmation": p.structured_content["confirmation"], **extra})
    return p.structured_content, r


async def test_preview_shows_before_and_after_and_records_nothing():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        r = await c.call_tool("preview_splitwise_settlement", args(amount="10"))
    p = r.structured_content
    assert p["summary"].splitlines()[:2] == [
        'Record a payment: Bob Smith paid you 10.00 USD, in group "Roommates"',
        "Now: Bob Smith owes you 25.7 USD. After: Bob Smith owes you 15.70 USD.",
    ]
    assert sw.posted == []


async def test_full_settlement_posts_a_payment_from_them_to_you():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        p, r = await preview_and_record(c)
    assert not r.is_error, text(r)
    assert "After: settled up." in p["summary"]
    form = sw.posted[0]
    assert form["payment"] == "true" and form["group_id"] == "77" and form["cost"] == "25.70"
    assert (form["users__0__user_id"], form["users__0__paid_share"], form["users__0__owed_share"]) == ("2", "25.70", "0.00")
    assert (form["users__1__user_id"], form["users__1__paid_share"], form["users__1__owed_share"]) == ("1", "0.00", "25.70")
    assert r.structured_content["summary"].startswith("Recorded (Splitwise id 501)")


async def test_you_paying_them():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        _, r = await preview_and_record(c, friend_id=5, amount="8", direction="i_paid_them")
    assert not r.is_error, text(r)
    assert (sw.posted[0]["users__0__user_id"], sw.posted[0]["users__1__user_id"]) == ("1", "5")


@pytest.mark.parametrize("kw,reason", [
    ({"amount": "30"}, "owes you 25.7 USD in 'Roommates'; a payment of 30.00 would be more than that"),
    ({"direction": "i_paid_them"}, "You don't owe Bob Smith any USD in 'Roommates'"),
    ({"friend_id": 5}, "Cara Diaz doesn't owe you any USD in 'Roommates'"),
    ({"group_id": 0, "amount": "10.01"}, "a payment of 10.01 would be more than that"),
    ({"friend_id": 99}, "is not one of your Splitwise friends"),
    ({"friend_id": 1}, "between you and someone else"),
    ({"friend_id": 6}, "Dana Lee is not in the group 'Roommates'"),
    ({"group_id": 1234}, "is not one of your Splitwise groups"),
    ({"amount": "0"}, "more than zero"),
    ({"amount": "-5"}, "more than zero"),
    ({"amount": "1.234"}, "at most 2 decimal places"),
    ({"amount": "abc"}, "is not a number"),
    ({"currency": "EUR"}, "doesn't owe you any EUR"),
])
async def test_refused(kw, reason):
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        r = await c.call_tool("preview_splitwise_settlement", args(**kw))
    assert r.is_error and reason in text(r), text(r)
    assert sw.posted == []


async def test_record_needs_the_matching_unused_confirmation():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        p = (await c.call_tool("preview_splitwise_settlement", args(amount="10"))).structured_content
        changed = await c.call_tool("record_splitwise_settlement", {**args(amount="12"), "confirmation": p["confirmation"]})
        made_up = await c.call_tool("record_splitwise_settlement", {**args(amount="10"), "confirmation": "1.2.3"})
        ok = await c.call_tool("record_splitwise_settlement", {**args(amount="10"), "confirmation": p["confirmation"]})
        again = await c.call_tool(
            "record_splitwise_settlement", {**args(amount="10"), "confirmation": p["confirmation"], "allow_duplicate": True}
        )
    assert changed.is_error and "not the one that was previewed" in text(changed)
    assert made_up.is_error and "Not recorded" in text(made_up)
    assert not ok.is_error, text(ok)
    assert again.is_error and "already used" in text(again)
    assert len(sw.posted) == 1


async def test_a_matching_payment_this_week_is_flagged_as_a_likely_double_record():
    sw = FakeSplitwise()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    old = {"id": 400, "payment": True, "cost": "10.0", "currency_code": "USD", "group_id": 77,
           "created_at": week_ago, "users": [{"user_id": 2, "paid_share": "10.0", "owed_share": "0.0"}]}
    sw.recent.append(old)  # older than 7 days: not a duplicate
    async with Client(sw.server()) as c:
        _, first = await preview_and_record(c, amount="10")
        p = (await c.call_tool("preview_splitwise_settlement", args(amount="10"))).structured_content
        blocked = await c.call_tool("record_splitwise_settlement", {**args(amount="10"), "confirmation": p["confirmation"]})
        _, allowed = await preview_and_record(c, amount="10", allow_duplicate=True)
    assert not first.is_error, text(first)
    assert p["likely_duplicate_of"] == 501 and "Warning: payment 501" in p["summary"]
    assert blocked.is_error and "allow_duplicate=true" in text(blocked)
    assert not allowed.is_error, text(allowed)
    assert len(sw.posted) == 2


async def test_reports_when_splitwise_saves_something_else():
    sw = FakeSplitwise()
    sw.save_as_expense = True
    async with Client(sw.server()) as c:
        _, r = await preview_and_record(c)
    assert r.is_error and "saved it as an ordinary expense" in text(r) and "Do not record it again" in text(r)
