import httpx2
import pytest
from mcp.client import Client

from splitwise_mcp_server.server import create_server

pytestmark = pytest.mark.anyio

ME = {"id": 1, "first_name": "Ann", "last_name": "A"}
BOB = {"id": 2, "first_name": "Bob", "last_name": "Smith"}
GROUPS = [{"id": 0, "name": "Non-group expenses", "members": []}, {"id": 77, "name": "Roommates", "members": []}]


def expense(id, *, by=ME, payment=False, deleted=False, group_id=77, updated="2026-11-16T10:00:00Z"):
    return {
        "id": id, "description": f"Expense {id}", "cost": "10.0", "currency_code": "USD",
        "date": "2026-11-16T09:00:00Z", "created_at": "2026-11-16T09:05:00Z", "updated_at": updated,
        "group_id": group_id, "created_by": by, "payment": payment,
        "deleted_at": "2026-11-16T11:00:00Z" if deleted else None,
        "users": [
            {"user_id": 1, "user": ME, "paid_share": "10.0", "owed_share": "5.0"},
            {"user_id": 2, "user": BOB, "paid_share": "0.0", "owed_share": "5.0"},
        ],
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeSplitwise:
    def __init__(self):
        self.expenses = {
            10: expense(10),
            11: expense(11, by=BOB),
            12: expense(12, payment=True),
            13: expense(13, deleted=True),
            14: expense(14, group_id=None),
        }
        self.deletes: list[int] = []
        self.fail_delete = False
        self.ignore_delete = False

    def handler(self, request):
        path = request.url.path.removeprefix("/api/v3.0")
        if path == "/get_current_user":
            return httpx2.Response(200, json={"user": ME})
        if path == "/get_groups":
            return httpx2.Response(200, json={"groups": GROUPS})
        if path == "/get_expenses":
            q = request.url.params
            found = [e for e in self.expenses.values()
                     if "group_id" not in q or str(e["group_id"]) == q["group_id"]]
            return httpx2.Response(200, json={"expenses": sorted(found, key=lambda e: -e["id"])[: int(q["limit"])]})
        if path.startswith("/get_expense/"):
            e = self.expenses.get(int(path.rsplit("/", 1)[1]))
            return httpx2.Response(200, json={"expense": e}) if e else httpx2.Response(404, text="not found")
        if path.startswith("/delete_expense/"):
            assert request.method == "POST"
            eid = int(path.rsplit("/", 1)[1])
            self.deletes.append(eid)
            if self.fail_delete:
                return httpx2.Response(200, json={"success": False, "errors": {"base": ["nope"]}})
            if not self.ignore_delete:
                self.expenses[eid]["deleted_at"] = "2026-11-16T12:00:00Z"
            return httpx2.Response(200, json={"success": True, "errors": {}})
        return httpx2.Response(404)

    def server(self):
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        return create_server(splitwise_token=lambda: "tok", http=http)


def text(r):
    return r.content[0].text


async def preview(c, eid):
    r = await c.call_tool("preview_splitwise_deletion", {"expense_id": eid})
    assert not r.is_error, text(r)
    return r.structured_content


async def test_list_marks_who_added_each_and_skips_deleted():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        r = await c.call_tool("list_splitwise_expenses", {"group_id": 77})
    rows = {e["id"]: e for e in r.structured_content["expenses"]}
    assert set(rows) == {10, 11, 12}  # 13 is deleted, 14 is outside the group
    assert rows[10]["added_by_you"] and not rows[11]["added_by_you"] and rows[11]["added_by"] == "Bob Smith"
    assert rows[12]["payment"] is True and rows[10]["group_name"] == "Roommates"


async def test_list_limit_is_bounded():
    async with Client(FakeSplitwise().server()) as c:
        r = await c.call_tool("list_splitwise_expenses", {"limit": 500})
    assert r.is_error and "between 1 and 100" in text(r)


async def test_preview_then_delete_your_own_expense():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        p = await preview(c, 10)
        assert sw.deletes == []  # previewing deletes nothing
        assert p["summary"].startswith('Delete "Expense 10": 10.0 USD, in group "Roommates", dated 2026-11-16')
        assert "Added by you on 2026-11-16" in p["summary"] and "can be restored" in p["summary"]
        r = await c.call_tool("delete_splitwise_expense", {"expense_id": 10, "confirmation": p["confirmation"]})
    assert not r.is_error, text(r)
    assert sw.deletes == [10] and r.structured_content["summary"].startswith("Deleted.")


@pytest.mark.parametrize("eid,reason", [
    (11, "was added by Bob Smith, and only expenses you added can be deleted"),
    (12, "is a settle-up payment"),
    (13, "is already deleted"),
])
async def test_refused_at_preview(eid, reason):
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        r = await c.call_tool("preview_splitwise_deletion", {"expense_id": eid})
    assert r.is_error and reason in text(r)
    assert sw.deletes == []


async def test_a_code_for_one_expense_cannot_delete_another():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        p = await preview(c, 10)
        r = await c.call_tool("delete_splitwise_expense", {"expense_id": 14, "confirmation": p["confirmation"]})
        other = await c.call_tool("delete_splitwise_expense", {"expense_id": 11, "confirmation": p["confirmation"]})
    assert r.is_error and "not the one that was previewed" in text(r)
    assert other.is_error and "only expenses you added" in text(other)
    assert sw.deletes == []


async def test_an_add_confirmation_or_made_up_code_cannot_delete():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        r = await c.call_tool("delete_splitwise_expense", {"expense_id": 10, "confirmation": "9999999999.abc.def"})
    assert r.is_error and "Not deleted" in text(r)
    assert sw.deletes == []


async def test_edited_after_preview_is_refused():
    sw = FakeSplitwise()
    async with Client(sw.server()) as c:
        p = await preview(c, 10)
        sw.expenses[10]["updated_at"] = "2026-11-16T10:30:00Z"  # someone edited it in the app
        r = await c.call_tool("delete_splitwise_expense", {"expense_id": 10, "confirmation": p["confirmation"]})
    assert r.is_error and "not the one that was previewed" in text(r)
    assert sw.deletes == []


async def test_a_confirmation_works_once():
    sw = FakeSplitwise()
    sw.fail_delete = True
    async with Client(sw.server()) as c:
        p = await preview(c, 10)
        first = await c.call_tool("delete_splitwise_expense", {"expense_id": 10, "confirmation": p["confirmation"]})
        sw.fail_delete = False
        again = await c.call_tool("delete_splitwise_expense", {"expense_id": 10, "confirmation": p["confirmation"]})
    assert first.is_error and "did not delete" in text(first)
    assert again.is_error and "already used" in text(again)
    assert sw.deletes == [10]


async def test_delete_is_verified_afterwards():
    sw = FakeSplitwise()
    sw.ignore_delete = True  # Splitwise says success but the expense is still there
    async with Client(sw.server()) as c:
        p = await preview(c, 10)
        r = await c.call_tool("delete_splitwise_expense", {"expense_id": 10, "confirmation": p["confirmation"]})
    assert r.is_error and "still shows as active" in text(r)
