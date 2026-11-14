"""Splitwise API client and conversion of a split into a Splitwise expense.

API reference: https://dev.splitwise.com/
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx2
from pydantic import BaseModel, Field

from .models import SplitResult

API_BASE = "https://secure.splitwise.com/api/v3.0"

# Duplicate detection: every expense this server creates carries a fingerprint line
# in its notes, and before creating one we look for that fingerprint among the
# expenses added or changed recently. Splitwise is the only store; the server keeps
# nothing.
REF_LINE = "ref: {ref}"
_REF_RE = re.compile(r"^ref: (swmcp-[0-9a-f]{16})$", re.MULTILINE)
DUPLICATE_LOOKBACK = timedelta(days=30)
DUPLICATE_PAGE_SIZE = 100
DUPLICATE_MAX_PAGES = 10


class SplitwiseError(Exception):
    pass


class SplitwiseClient:
    def __init__(self, token: str, http: httpx2.AsyncClient | None = None):
        self._token = token
        self._http = http

    async def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if self._http is not None:
            resp = await self._http.request(method, API_BASE + path, data=data, params=params, headers=headers)
        else:
            async with httpx2.AsyncClient(timeout=20) as http:
                resp = await http.request(method, API_BASE + path, data=data, params=params, headers=headers)
        if resp.status_code == 401:
            raise SplitwiseError("Splitwise rejected the sign-in. Reconnect the Splitwise connector and try again.")
        if resp.status_code == 429:
            raise SplitwiseError("Splitwise rate limit reached. Wait a moment and try again.")
        if resp.status_code >= 400:
            raise SplitwiseError(f"Splitwise returned HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def current_user(self) -> dict[str, Any]:
        return (await self._request("GET", "/get_current_user"))["user"]

    async def friends(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/get_friends"))["friends"]

    async def groups(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/get_groups"))["groups"]

    async def expenses(
        self, *, group_id: int | None, updated_after: datetime, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        params = {
            "updated_after": updated_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": str(limit),
            "offset": str(offset),
        }
        if group_id:
            params["group_id"] = str(group_id)
        return (await self._request("GET", "/get_expenses", params=params))["expenses"]

    async def create_expense(self, form: dict[str, str]) -> dict[str, Any]:
        body = await self._request("POST", "/create_expense", data=form)
        # 200 OK is not success on its own: the call failed if `errors` is non-empty.
        errors = body.get("errors")
        if errors:
            raise SplitwiseError(f"Splitwise did not create the expense: {errors}")
        expenses = body.get("expenses") or []
        if not expenses:
            raise SplitwiseError("Splitwise returned no expense")
        return expenses[0]


# ---------------------------------------------------------------- people


class SplitwiseUser(BaseModel):
    id: int
    name: str
    email: str | None = None


class SplitwiseGroup(BaseModel):
    id: int
    name: str
    members: list[SplitwiseUser]


class SplitwisePeople(BaseModel):
    me: SplitwiseUser
    friends: list[SplitwiseUser]
    groups: list[SplitwiseGroup]


def _user(u: dict[str, Any]) -> SplitwiseUser:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    return SplitwiseUser(id=u["id"], name=name or str(u["id"]), email=u.get("email"))


async def list_people(client: SplitwiseClient) -> SplitwisePeople:
    me = await client.current_user()
    friends = await client.friends()
    groups = await client.groups()
    return SplitwisePeople(
        me=_user(me),
        friends=[_user(f) for f in friends],
        groups=[
            SplitwiseGroup(id=g["id"], name=g.get("name") or "", members=[_user(m) for m in g.get("members", [])])
            for g in groups
            if g.get("id")  # id 0 is Splitwise's pseudo-group for non-group expenses
        ],
    )


# ---------------------------------------------------------------- expenses


class CreatedExpense(BaseModel):
    expense_id: int
    description: str
    cost: Decimal
    currency: str
    group_id: int
    paid_by: str
    owed: dict[str, Decimal] = Field(description="What each person owes, as recorded in Splitwise")


def expense_form(
    result: SplitResult,
    *,
    paid_by: str,
    members: dict[str, int | str],
    description: str,
    group_id: int,
    date: str | None,
) -> dict[str, str]:
    """Build the create_expense form. Raises ValueError for anything that can't be posted as-is."""
    people = [p.person for p in result.people]
    if paid_by not in people:
        raise ValueError(f"paid_by '{paid_by}' is not one of the people on the receipt: {people}")
    unknown = sorted(set(members) - set(people))
    if unknown:
        raise ValueError(f"members has names that are not on the receipt: {unknown}")

    owed = {p.person: p.total for p in result.people}
    involved = [p for p in people if owed[p] != 0 or p == paid_by]
    missing = [p for p in involved if p not in members]
    if missing:
        raise ValueError(
            f"No Splitwise user for {missing}. Map every person who owes something (and the payer) "
            "to a Splitwise user id or email; use list_splitwise_people to look up ids."
        )
    negative = [p for p in involved if owed[p] < 0]
    if negative:
        raise ValueError(f"Splitwise cannot record a negative share, but {negative} would be owed a credit")

    cost = result.grand_total
    ref = expense_ref(
        currency=result.currency,
        cost=cost,
        group_id=group_id,
        date=date,
        owed={_identity(members[p]): owed[p] for p in involved if owed[p] != 0},
    )
    form: dict[str, str] = {
        "cost": str(cost),
        "description": description,
        "currency_code": result.currency,
        "group_id": str(group_id),
        "details": breakdown(result) + "\n" + REF_LINE.format(ref=ref),
    }
    if date:
        form["date"] = date
    for i, person in enumerate(involved):
        who = members[person]
        key = "email" if isinstance(who, str) and "@" in who else "user_id"
        form[f"users__{i}__{key}"] = str(who)
        form[f"users__{i}__paid_share"] = str(cost if person == paid_by else Decimal("0.00"))
        form[f"users__{i}__owed_share"] = str(owed[person])
    return form


def _identity(who: int | str) -> str:
    return str(who).strip().lower()


def expense_ref(
    *, currency: str, cost: Decimal, group_id: int, date: str | None, owed: dict[str, Decimal]
) -> str:
    """Fingerprint of what makes two expenses the same bill.

    Same group, purchase date, currency, total, and Splitwise users owing the same
    amounts. It ignores receipt names, item ids, the description, and who paid, so a
    re-parsed or retitled copy of the same receipt still matches.
    """
    canon = json.dumps(
        {
            "currency": currency.upper(),
            "cost": str(cost),
            "group_id": group_id,
            "date": date.strip()[:10] if date else None,
            "owed": {who: str(amount) for who, amount in owed.items()},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "swmcp-" + hashlib.sha256(canon.encode()).hexdigest()[:16]


async def find_duplicate(
    client: SplitwiseClient, form: dict[str, str], now: datetime | None = None
) -> dict[str, Any] | None:
    """Return a live expense added or changed in the lookback window with the same fingerprint."""
    match = _REF_RE.search(form["details"])
    if match is None:  # every form from expense_form has one
        raise ValueError("expense form has no ref line")
    ref = match.group(1)
    since = (now or datetime.now(timezone.utc)) - DUPLICATE_LOOKBACK
    group_id = int(form["group_id"]) or None
    for page in range(DUPLICATE_MAX_PAGES):
        expenses = await client.expenses(
            group_id=group_id, updated_after=since, limit=DUPLICATE_PAGE_SIZE, offset=page * DUPLICATE_PAGE_SIZE
        )
        for e in expenses:
            if not e.get("deleted_at") and ref in _REF_RE.findall(e.get("details") or ""):
                return e
        if len(expenses) < DUPLICATE_PAGE_SIZE:
            break
    return None


def breakdown(result: SplitResult) -> str:
    lines = [f"Split by splitwise-mcp-server ({result.currency})"]
    for p in result.people:
        if p.total == 0:
            continue
        parts = [f"items {p.items}"]
        for label, value in (("discounts", p.discounts), ("tax", p.tax), ("tip", p.tip), ("fees", p.fees)):
            if value:
                parts.append(f"{label} {value}")
        lines.append(f"{p.person}: {p.total} ({', '.join(parts)})")
    return "\n".join(lines)
