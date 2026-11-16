"""Finding expenses, and deleting ones the signed-in user added, behind a preview.

Deleting follows the same rule as adding: nothing happens without a preview the user
has seen. The preview signs the expense's id and last-updated time, so if the expense
is edited after the preview, the delete is refused. Only regular expenses the
signed-in user created can be deleted (not other people's, not settle-up payments),
one per approved preview, and the server checks afterwards that it really is deleted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

NON_GROUP = "outside any group"


class ExpenseShare(BaseModel):
    name: str
    user_id: int | None
    paid: Decimal
    owes: Decimal


class ExpenseInfo(BaseModel):
    id: int
    description: str
    cost: Decimal
    currency: str
    date: str | None
    group_id: int | None
    group_name: str | None
    added_by: str
    added_by_you: bool = Field(description="Only expenses you added can be deleted")
    added_at: str | None = None
    payment: bool = Field(default=False, description="A settle-up payment, not an expense; can't be deleted here")
    shares: list[ExpenseShare]


class ExpenseList(BaseModel):
    expenses: list[ExpenseInfo] = Field(description="Most recent first; deleted expenses left out")


class DeletionPreview(BaseModel):
    expense: ExpenseInfo
    summary: str = Field(description="Show this to the user, word for word, and get an explicit yes before deleting.")
    confirmation: str = Field(description="Pass to delete_splitwise_expense with the same expense_id. Valid 30 minutes, once.")


def _name(u: dict[str, Any] | None) -> str:
    u = u or {}
    return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or str(u.get("id", "?"))


def expense_info(e: dict[str, Any], *, me_id: int, group_names: dict[int, str]) -> ExpenseInfo:
    creator = e.get("created_by") or {}
    gid = e.get("group_id") or None
    return ExpenseInfo(
        id=e["id"],
        description=e.get("description") or "",
        cost=Decimal(str(e.get("cost") or 0)),
        currency=e.get("currency_code") or "",
        date=(e.get("date") or "")[:10] or None,
        group_id=gid,
        group_name=group_names.get(gid) if gid else None,
        added_by=_name(creator) if creator else "unknown",
        added_by_you=creator.get("id") == me_id,
        added_at=(e.get("created_at") or "")[:10] or None,
        payment=bool(e.get("payment")),
        shares=[
            ExpenseShare(
                name=_name(u.get("user")),
                user_id=u.get("user_id"),
                paid=Decimal(str(u.get("paid_share") or 0)),
                owes=Decimal(str(u.get("owed_share") or 0)),
            )
            for u in e.get("users") or []
        ],
    )


def deletion_problem(e: dict[str, Any], me_id: int) -> str | None:
    """Why this expense can't be deleted through this server, or None."""
    if e.get("deleted_at"):
        return f"expense {e['id']} is already deleted"
    if e.get("payment"):
        return f"expense {e['id']} is a settle-up payment, not an expense; undo payments in the Splitwise app"
    if (e.get("created_by") or {}).get("id") != me_id:
        who = _name(e.get("created_by")) if e.get("created_by") else "someone else"
        return f"expense {e['id']} was added by {who}, and only expenses you added can be deleted here"
    return None


def deletion_fingerprint(e: dict[str, Any]) -> dict[str, str]:
    """What a deletion confirmation signs: this expense, as it was when previewed."""
    return {"action": "delete_expense", "expense_id": str(e["id"]), "updated_at": str(e.get("updated_at") or "")}


def deletion_summary(info: ExpenseInfo) -> str:
    where = f'in group "{info.group_name}"' if info.group_name else NON_GROUP
    lines = [
        f'Delete "{info.description}": {info.cost} {info.currency}, {where}'
        + (f", dated {info.date}" if info.date else ""),
    ]
    payers = [s for s in info.shares if s.paid]
    if payers:
        lines.append("Paid by " + ", ".join(f"{s.name} ({s.paid})" for s in payers))
    lines += [f"- {s.name} owes {s.owes}" for s in info.shares if s.owes]
    if info.added_at:
        lines.append(f"Added by you on {info.added_at}")
    lines.append("Everyone on it will see it removed. It can be restored in the Splitwise app.")
    return "\n".join(lines)
