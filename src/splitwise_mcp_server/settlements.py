"""Recording a settle-up payment between the signed-in user and one other person.

A payment changes balances as surely as an expense does, so it gets the same
treatment: a preview the user approves, a single-use confirmation bound to the exact
payment, and a check of what Splitwise saved. On top of that, a payment is refused if
it's larger than what's owed in that direction (or nothing is owed), and a payment
with the same people, direction, amount, and group in the last 7 days is reported
as a likely double-record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from .splitwise_api import SplitwiseClient, _user

Direction = Literal["they_paid_me", "i_paid_them"]
RECENT = timedelta(days=7)


class SettlementPreview(BaseModel):
    friend_id: int
    friend_name: str
    direction: Direction
    amount: Decimal
    currency: str
    group_id: int
    group_name: str | None
    balance_before: Decimal = Field(description="What they owe you in this group and currency (negative: you owe them)")
    balance_after: Decimal
    likely_duplicate_of: int | None = Field(description="A matching payment from the last 7 days, if any")
    summary: str = Field(description="Show this to the user, word for word, and get an explicit yes before recording.")
    confirmation: str = Field(description="Pass to record_splitwise_settlement with the same arguments. Valid 30 minutes, once.")


class PreparedSettlement(BaseModel):
    friend_id: int
    friend_name: str
    me_id: int
    direction: Direction
    amount: Decimal
    currency: str
    group_id: int
    group_name: str | None
    balance_before: Decimal
    duplicate_of: int | None
    form: dict[str, str]

    @property
    def balance_after(self) -> Decimal:
        change = -self.amount if self.direction == "they_paid_me" else self.amount
        return self.balance_before + change


def _amount(text: str) -> Decimal:
    try:
        amount = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError(f"amount {text!r} is not a number") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be more than zero")
    if amount.as_tuple().exponent < -2:  # type: ignore[operator]
        raise ValueError("amount can have at most 2 decimal places")
    return amount.quantize(Decimal("0.01"))


async def prepare_settlement(
    client: SplitwiseClient,
    *,
    friend_id: int,
    amount: str,
    direction: Direction,
    group_id: int,
    currency: str,
    now: datetime | None = None,
) -> PreparedSettlement:
    """Check the payment against the current balance and build the form. Raises ValueError."""
    value = _amount(amount)
    currency = currency.strip().upper()
    me = await client.current_user()
    if friend_id == me["id"]:
        raise ValueError("a settlement is between you and someone else")
    friend = next((f for f in await client.friends() if f["id"] == friend_id), None)
    if friend is None:
        raise ValueError(
            f"user {friend_id} is not one of your Splitwise friends. Look them up with find_splitwise_people."
        )
    groups = {g["id"]: g for g in await client.groups()}
    if group_id and group_id not in groups:
        raise ValueError(f"group_id {group_id} is not one of your Splitwise groups.")
    if group_id and friend_id not in {m["id"] for m in groups[group_id].get("members") or []}:
        raise ValueError(f"{_user(friend).name} is not in the group '{groups[group_id].get('name')}'.")

    name = _user(friend).name
    owed = Decimal(0)  # positive: they owe the user, in this group and currency
    for g in friend.get("groups") or []:
        if g.get("group_id") == group_id:
            for b in g.get("balance") or []:
                if b.get("currency_code") == currency:
                    owed += Decimal(str(b["amount"]))
    where = f"in '{groups[group_id].get('name')}'" if group_id else "outside groups"
    if direction == "they_paid_me":
        if owed <= 0:
            raise ValueError(f"{name} doesn't owe you any {currency} {where}, so there's nothing for them to pay.")
        if value > owed:
            raise ValueError(f"{name} owes you {owed} {currency} {where}; a payment of {value} would be more than that.")
        payer, receiver = friend_id, me["id"]
    else:
        if owed >= 0:
            raise ValueError(f"You don't owe {name} any {currency} {where}, so there's nothing to pay.")
        if value > -owed:
            raise ValueError(f"You owe {name} {-owed} {currency} {where}; a payment of {value} would be more than that.")
        payer, receiver = me["id"], friend_id

    form = {
        "cost": str(value),
        "currency_code": currency,
        "group_id": str(group_id),
        "payment": "true",
        "description": "Payment",
        "users__0__user_id": str(payer),
        "users__0__paid_share": str(value),
        "users__0__owed_share": "0.00",
        "users__1__user_id": str(receiver),
        "users__1__paid_share": "0.00",
        "users__1__owed_share": str(value),
    }

    # A matching payment in the last week is probably this one, recorded already.
    since = (now or datetime.now(timezone.utc)) - RECENT
    recent = await client.recent_expenses(
        group_id=group_id if group_id else None, friend_id=None if group_id else friend_id, limit=100
    )
    duplicate = None
    for e in recent:
        if not e.get("payment") or e.get("deleted_at") or Decimal(str(e.get("cost") or 0)) != value:
            continue
        if (e.get("currency_code") or "") != currency or (e.get("group_id") or 0) != group_id:
            continue
        created = datetime.fromisoformat(str(e.get("created_at", "1970-01-01T00:00:00Z")).replace("Z", "+00:00"))
        paid = {u.get("user_id") for u in e.get("users") or [] if Decimal(str(u.get("paid_share") or 0)) > 0}
        if created >= since and paid == {payer}:
            duplicate = e["id"]
            break

    return PreparedSettlement(
        friend_id=friend_id, friend_name=name, me_id=me["id"], direction=direction, amount=value,
        currency=currency, group_id=group_id, group_name=groups[group_id].get("name") if group_id else None,
        balance_before=owed, duplicate_of=duplicate, form=form,
    )


def settlement_summary(p: PreparedSettlement) -> str:
    who = f"{p.friend_name} paid you" if p.direction == "they_paid_me" else f"You paid {p.friend_name}"
    where = f'in group "{p.group_name}"' if p.group_name else "outside any group"
    def state(x: Decimal) -> str:
        if x > 0:
            return f"{p.friend_name} owes you {x} {p.currency}"
        if x < 0:
            return f"you owe {p.friend_name} {-x} {p.currency}"
        return "settled up"
    lines = [
        f"Record a payment: {who} {p.amount} {p.currency}, {where}",
        f"Now: {state(p.balance_before)}. After: {state(p.balance_after)}.",
        "Only record money that has actually changed hands.",
    ]
    if p.duplicate_of:
        lines.append(
            f"Warning: payment {p.duplicate_of} in the last 7 days has the same people, direction, amount, and "
            "group. Recording this again needs the user to confirm it is a separate payment."
        )
    return "\n".join(lines)


def saved_payment_problem(p: PreparedSettlement, e: dict[str, Any]) -> str | None:
    """None if Splitwise saved the payment as sent; otherwise how it differs."""
    if not e.get("payment"):
        return "Splitwise saved it as an ordinary expense, not a payment"
    if Decimal(str(e.get("cost") or 0)) != p.amount:
        return f"the amount is {e.get('cost')}, not {p.amount}"
    shares = {u.get("user_id"): (Decimal(str(u.get("paid_share") or 0)), Decimal(str(u.get("owed_share") or 0)))
              for u in e.get("users") or []}
    payer, receiver = int(p.form["users__0__user_id"]), int(p.form["users__1__user_id"])
    if shares.get(payer) != (p.amount, Decimal(0)) or shares.get(receiver) != (Decimal(0), p.amount) or len(shares) != 2:
        return "the people or shares differ from the preview"
    return None
