"""Turning a receipt into a checked, previewable Splitwise expense.

Preview and add share prepare_expense, so the expense a user approves is built by the
same code, from the same arguments, as the one that is posted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from .models import Receipt, SplitResult
from .people import Directory, ResolvedPerson, resolve_members
from .split import split
from .splitwise_api import SplitwiseClient, expense_form, involved_people, list_people


class PreviewPerson(BaseModel):
    receipt_name: str
    splitwise_name: str | None
    email: str | None
    splitwise_id: int | None
    is_me: bool
    new_to_splitwise: bool
    paid: Decimal
    owes: Decimal


class DuplicateWarning(BaseModel):
    expense_id: Any
    description: str | None
    cost: str | None
    created_at: str | None


class ExpensePreview(BaseModel):
    description: str
    cost: Decimal
    currency: str
    group_id: int
    group_name: str | None
    date: str | None
    paid_by: str
    people: list[PreviewPerson]
    duplicate_of: DuplicateWarning | None = Field(
        description="An expense already in Splitwise that looks the same. Adding needs allow_duplicate=true."
    )
    summary: str = Field(description="Show this to the user, word for word, and get an explicit yes before adding.")
    confirmation: str = Field(description="Pass to add_expense_to_splitwise with the same arguments. Valid 30 minutes.")


@dataclass
class PreparedExpense:
    result: SplitResult
    resolved: dict[str, ResolvedPerson]
    group_name: str | None
    form: dict[str, str]


async def prepare_expense(
    client: SplitwiseClient,
    receipt: Receipt,
    *,
    paid_by: str,
    members: dict[str, int | str],
    description: str,
    group_id: int,
    date: str | None,
) -> PreparedExpense:
    """Split, check every person against the user's contacts, and build the form.

    Raises SplitError, ValueError (bad mapping), or SplitwiseError.
    """
    result = split(receipt)
    involved = involved_people(result, paid_by=paid_by, members=members)
    directory = Directory.from_people(await list_people(client))
    resolved = resolve_members(directory, involved, members, group_id)
    form = expense_form(
        result,
        paid_by=paid_by,
        members={name: r.splitwise_ref for name, r in resolved.items()},
        description=description,
        group_id=group_id,
        date=date,
    )
    group_name = directory.groups[group_id][0] if group_id else None
    return PreparedExpense(result=result, resolved=resolved, group_name=group_name, form=form)


def build_preview(
    prepared: PreparedExpense,
    *,
    paid_by: str,
    description: str,
    group_id: int,
    date: str | None,
    duplicate: dict[str, Any] | None,
    confirmation: str,
) -> ExpensePreview:
    result = prepared.result
    owed = {p.person: p.total for p in result.people}
    zero = Decimal(0).quantize(result.grand_total)
    people = [
        PreviewPerson(
            receipt_name=name,
            splitwise_name=r.splitwise_name,
            email=r.email,
            splitwise_id=r.splitwise_id,
            is_me=r.is_me,
            new_to_splitwise=r.new_to_splitwise,
            paid=result.grand_total if name == paid_by else zero,
            owes=owed[name],
        )
        for name, r in prepared.resolved.items()
    ]

    where = f'in group "{prepared.group_name}"' if prepared.group_name else "outside any group"
    lines = [
        f'"{description}": {result.grand_total} {result.currency}, {where}' + (f", dated {date}" if date else ""),
        f"Paid by {prepared.resolved[paid_by].label()}",
    ]
    for name, r in prepared.resolved.items():
        lines.append(f"- {r.label()} owes {owed[name]}" + (f'  (receipt: "{name}")' if name != r.splitwise_name else ""))
    dup = None
    if duplicate:
        dup = DuplicateWarning(
            expense_id=duplicate.get("id"),
            description=duplicate.get("description"),
            cost=duplicate.get("cost"),
            created_at=duplicate.get("created_at"),
        )
        lines.append(
            f'Warning: this looks like expense {dup.expense_id} ("{dup.description}", {dup.cost}, created '
            f"{dup.created_at}). Adding it again needs the user to confirm it is a separate expense."
        )

    return ExpensePreview(
        description=description,
        cost=result.grand_total,
        currency=result.currency,
        group_id=group_id,
        group_name=prepared.group_name,
        date=date,
        paid_by=paid_by,
        people=people,
        duplicate_of=dup,
        summary="\n".join(lines),
        confirmation=confirmation,
    )


def saved_differences(prepared: PreparedExpense, expense: dict[str, Any]) -> list[str]:
    """How the expense Splitwise saved differs from the one we sent; empty if it matches.

    Splitwise can return success yet change an expense (it drops a new person from a
    group expense), so add checks the saved shares against the previewed ones.
    """
    form = prepared.form
    names = {str(r.splitwise_ref).casefold(): name for name, r in prepared.resolved.items()}
    saved = expense.get("users")
    if saved is None:
        return ["Splitwise did not return the saved shares, so they could not be checked"]

    def amount(x: Any) -> Decimal:
        return Decimal(str(x or 0))

    problems = []
    if amount(expense.get("cost")) != amount(form["cost"]):
        problems.append(f"the total is {expense.get('cost')}, not {form['cost']}")
    matched: set[int] = set()
    i = 0
    while f"users__{i}__paid_share" in form:
        uid, email = form.get(f"users__{i}__user_id"), form.get(f"users__{i}__email", "").casefold()
        who = names.get((uid or email).casefold(), uid or email)
        found = next(
            (
                (k, u) for k, u in enumerate(saved)
                if k not in matched and (
                    (uid and str(u.get("user_id")) == uid)
                    or (email and str((u.get("user") or {}).get("email") or "").casefold() == email)
                )
            ),
            None,
        )
        if found is None:
            problems.append(f"{who} was left out")
        else:
            k, u = found
            matched.add(k)
            paid, owed = amount(form[f"users__{i}__paid_share"]), amount(form[f"users__{i}__owed_share"])
            if (amount(u.get("paid_share")), amount(u.get("owed_share"))) != (paid, owed):
                problems.append(
                    f"{who} paid {u.get('paid_share')} and owes {u.get('owed_share')}, not paid {paid} and owes {owed}"
                )
        i += 1
    for k, u in enumerate(saved):
        if k not in matched:
            user = u.get("user") or {}
            problems.append(f"{user.get('first_name') or u.get('user_id')} was added with share {u.get('owed_share')}")
    return problems
