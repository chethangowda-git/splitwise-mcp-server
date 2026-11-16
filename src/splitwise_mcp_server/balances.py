"""Who owes the signed-in user, and whom they owe, from Splitwise's own balances.

Read-only. Amounts stay per currency: Splitwise doesn't convert, and adding USD to INR
would be meaningless. Sign convention throughout: positive means the other person owes
the user; negative means the user owes them.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from .splitwise_api import SplitwiseClient, _user

NON_GROUP = "Non-group expenses"


class Amount(BaseModel):
    currency: str
    amount: Decimal


class GroupShare(BaseModel):
    group_id: int = Field(description="0 for expenses outside any group")
    group_name: str
    balances: list[Amount]


class FriendBalance(BaseModel):
    id: int
    name: str
    email: str | None
    balances: list[Amount] = Field(description="Positive: they owe you. Negative: you owe them.")
    by_group: list[GroupShare]


class Debt(BaseModel):
    from_name: str
    to_name: str
    amount: Decimal
    currency: str


class GroupBalance(BaseModel):
    group_id: int
    name: str
    your_balance: list[Amount] = Field(description="Positive: the group owes you. Negative: you owe the group.")
    debts: list[Debt] = Field(description="Who pays whom to settle the group, as Splitwise shows it")


class Balances(BaseModel):
    owed_to_you: list[Amount] = Field(description="Total others owe you, per currency")
    you_owe: list[Amount] = Field(description="Total you owe others, per currency (positive numbers)")
    net: list[Amount] = Field(description="owed_to_you - you_owe, per currency")
    friends: list[FriendBalance] = Field(description="Friends with a non-zero balance, largest first")
    groups: list[GroupBalance] = Field(description="Groups with anything outstanding")
    note: str = (
        "Positive means they owe you. Amounts are per currency and never added across currencies. "
        "These are Splitwise's own balances, including expenses added in the app."
    )


def _amounts(raw: list[dict[str, Any]] | None) -> list[Amount]:
    out = [Amount(currency=b["currency_code"], amount=Decimal(str(b["amount"]))) for b in raw or []]
    return [a for a in out if a.amount != 0]


async def get_balances(client: SplitwiseClient, group_id: int | None = None) -> Balances:
    """All balances, or only those in one group when group_id is given (0 = non-group)."""
    me = await client.current_user()
    friends = await client.friends()
    groups = await client.groups()
    group_names = {g["id"]: (g.get("name") or "") for g in groups}
    group_names[0] = NON_GROUP
    if group_id is not None and group_id not in group_names:
        raise ValueError(f"group_id {group_id} is not one of your Splitwise groups. Use list_splitwise_people.")

    friend_rows: list[FriendBalance] = []
    for f in friends:
        u = _user(f)
        shares = [
            GroupShare(group_id=g["group_id"], group_name=group_names.get(g["group_id"], "?"), balances=_amounts(g.get("balance")))
            for g in f.get("groups") or []
            if group_id is None or g["group_id"] == group_id
        ]
        shares = [s for s in shares if s.balances]
        if group_id is None:
            balances = _amounts(f.get("balance"))
        else:
            balances = [a for s in shares for a in s.balances]
        if balances:
            friend_rows.append(FriendBalance(id=u.id, name=u.name, email=u.email, balances=balances, by_group=shares))
    friend_rows.sort(key=lambda r: -max(abs(a.amount) for a in r.balances))

    owed: dict[str, Decimal] = defaultdict(Decimal)
    owe: dict[str, Decimal] = defaultdict(Decimal)
    for r in friend_rows:
        for a in r.balances:
            (owed if a.amount > 0 else owe)[a.currency] += abs(a.amount)
    currencies = sorted(set(owed) | set(owe))

    group_rows: list[GroupBalance] = []
    for g in groups:
        if not g.get("id") or (group_id is not None and g["id"] != group_id):
            continue
        names = {m["id"]: _user(m).name for m in g.get("members") or []}
        mine = next((m for m in g.get("members") or [] if m["id"] == me["id"]), {})
        raw_debts = g.get("simplified_debts") if g.get("simplify_by_default") else g.get("original_debts")
        debts = [
            Debt(
                from_name=names.get(d["from"], str(d["from"])),
                to_name=names.get(d["to"], str(d["to"])),
                amount=Decimal(str(d["amount"])),
                currency=d["currency_code"],
            )
            for d in raw_debts or []
            if Decimal(str(d["amount"])) != 0
        ]
        yours = _amounts(mine.get("balance"))
        if yours or debts:
            group_rows.append(GroupBalance(group_id=g["id"], name=g.get("name") or "", your_balance=yours, debts=debts))

    return Balances(
        owed_to_you=[Amount(currency=c, amount=owed[c]) for c in currencies if owed[c]],
        you_owe=[Amount(currency=c, amount=owe[c]) for c in currencies if owe[c]],
        net=[Amount(currency=c, amount=owed[c] - owe[c]) for c in currencies],
        friends=friend_rows,
        groups=group_rows,
    )
