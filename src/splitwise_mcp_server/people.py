"""Matching receipt names to Splitwise users, and checking the mapping before anything is posted.

The AI matches names, but a wrong match puts a real expense on the wrong person's
account, so the server doesn't take the mapping on trust:

- find_people reports, for each name, exactly one match, several (the user must
  choose), or none (ask for an email, or offer the close suggestions). It never picks
  among several.
- resolve_members checks every mapping against the signed-in user's own contacts:
  an id must be the user, a friend, or a member of one of their groups (of the target
  group, for a group expense), and two receipt names can't be the same person. An
  email that belongs to a contact resolves to that contact; an unknown email is
  allowed and flagged outside groups, because Splitwise invites that address, and
  refused in groups, because Splitwise silently leaves that person out.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from .splitwise_api import SplitwisePeople

ME_WORDS = {"me", "i", "myself"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm(s: str) -> str:
    """Case-, accent-, and spacing-insensitive form: "  José  ÁLVAREZ" -> "jose alvarez"."""
    decomposed = unicodedata.normalize("NFKD", s.casefold())
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).split())


class Contact(BaseModel):
    id: int
    name: str
    email: str | None = None
    is_me: bool = False
    friend: bool = False
    groups: list[str] = Field(default_factory=list, description="Names of the user's groups this person is in")


class NameMatch(BaseModel):
    query: str
    status: Literal["match", "ambiguous", "none"]
    matched_by: Literal["me", "email", "full_name", "first_or_last_name"] | None = None
    candidates: list[Contact] = Field(description="One for 'match'; every possible person for 'ambiguous'")
    suggestions: list[Contact] = Field(default_factory=list, description="Close names, for 'none'")


class PeopleSearch(BaseModel):
    results: list[NameMatch]
    next_step: str = (
        "'match': use the id, but confirm with the user when matched_by is first_or_last_name. "
        "'ambiguous': show the candidates' names and emails and ask which one. "
        "'none': ask for the person's email (Splitwise will invite them) or offer the suggestions. "
        "Never guess."
    )


@dataclass
class Directory:
    """The signed-in user, their friends, and their groups' members, by Splitwise id."""

    me_id: int
    contacts: dict[int, Contact]
    groups: dict[int, tuple[str, set[int]]]  # group id -> (name, member ids)

    @classmethod
    def from_people(cls, people: SplitwisePeople) -> Directory:
        contacts: dict[int, Contact] = {
            people.me.id: Contact(id=people.me.id, name=people.me.name, email=people.me.email, is_me=True)
        }

        def contact(u: Any) -> Contact:
            c = contacts.setdefault(u.id, Contact(id=u.id, name=u.name, email=u.email))
            c.email = c.email or u.email
            return c

        for f in people.friends:
            contact(f).friend = True
        groups: dict[int, tuple[str, set[int]]] = {}
        for g in people.groups:
            groups[g.id] = (g.name, {m.id for m in g.members})
            for m in g.members:
                c = contact(m)
                if not c.is_me:
                    c.groups.append(g.name)
        return cls(people.me.id, contacts, groups)

    def by_email(self, email: str) -> Contact | None:
        e = email.strip().casefold()
        return next((c for c in self.contacts.values() if c.email and c.email.casefold() == e), None)


# ---------------------------------------------------------------- lookup


def find_people(d: Directory, names: list[str]) -> PeopleSearch:
    return PeopleSearch(results=[_find(d, n) for n in names])


def _result(query: str, found: list[Contact], how: str) -> NameMatch:
    return NameMatch(query=query, status="match" if len(found) == 1 else "ambiguous", matched_by=how, candidates=found)


def _find(d: Directory, query: str) -> NameMatch:
    q = _norm(query)
    everyone = list(d.contacts.values())
    if q in ME_WORDS:
        return _result(query, [d.contacts[d.me_id]], "me")
    if "@" in q:
        c = d.by_email(q)
        if c:
            return _result(query, [c], "email")
        return NameMatch(query=query, status="none", candidates=[])
    full = [c for c in everyone if _norm(c.name) == q]
    if full:
        return _result(query, full, "full_name")
    partial = [c for c in everyone if q in _norm(c.name).split()]
    if partial:
        return _result(query, partial, "first_or_last_name")

    by_label: dict[str, list[Contact]] = defaultdict(list)
    for c in everyone:
        by_label[_norm(c.name)].append(c)
        for token in _norm(c.name).split():
            by_label[token].append(c)
    suggestions: dict[int, Contact] = {}
    for label in difflib.get_close_matches(q, list(by_label), n=6, cutoff=0.6):
        for c in by_label[label]:
            suggestions.setdefault(c.id, c)
    return NameMatch(query=query, status="none", candidates=[], suggestions=list(suggestions.values())[:3])


# ---------------------------------------------------------------- validation


class ResolvedPerson(BaseModel):
    receipt_name: str
    splitwise_id: int | None = None
    splitwise_name: str | None = None
    email: str | None = None
    is_me: bool = False
    new_to_splitwise: bool = Field(default=False, description="Unknown email: Splitwise will invite it")

    @property
    def splitwise_ref(self) -> int | str:
        """What create_expense needs: the user id, or the email for someone new."""
        return self.splitwise_id if self.splitwise_id is not None else self.email  # type: ignore[return-value]

    def label(self) -> str:
        if self.is_me:
            return f"{self.splitwise_name} (you)"
        if self.new_to_splitwise:
            return f'{self.email} (not in your Splitwise contacts; Splitwise will invite them as "{self.receipt_name}")'
        return f"{self.splitwise_name} <{self.email}>" if self.email else str(self.splitwise_name)


def resolve_members(
    d: Directory, involved: list[str], members: dict[str, int | str], group_id: int
) -> dict[str, ResolvedPerson]:
    """Check each involved person's mapping against the user's contacts. Raises ValueError listing every problem."""
    group = None
    if group_id:
        group = d.groups.get(group_id)
        if group is None:
            raise ValueError(f"group_id {group_id} is not one of your Splitwise groups. Use list_splitwise_people.")

    problems: list[str] = []
    out: dict[str, ResolvedPerson] = {}
    for name in involved:
        raw = members[name]
        text = str(raw).strip()
        if (isinstance(raw, int) and not isinstance(raw, bool)) or text.isdigit():
            c = d.contacts.get(int(text))
            if c is None:
                problems.append(
                    f"'{name}' -> user id {text} is not you, one of your friends, or a member of your groups. "
                    "Look the name up with find_splitwise_people."
                )
                continue
        elif EMAIL_RE.match(text):
            c = d.by_email(text)
            if c is None:
                if group is not None:
                    # Splitwise accepts this but silently drops the new person from the expense.
                    problems.append(
                        f"'{name}' -> {text} is not on Splitwise yet, and Splitwise leaves new people out of "
                        f"group expenses. Add them to the group '{group[0]}' in Splitwise first, or add this "
                        "expense outside any group (group_id 0)."
                    )
                    continue
                out[name] = ResolvedPerson(receipt_name=name, email=text, new_to_splitwise=True)
                continue
        else:
            problems.append(
                f"'{name}' -> {text!r} is not a Splitwise user id or an email address. "
                "Use find_splitwise_people to get the id."
            )
            continue
        if group is not None and c.id not in group[1]:
            problems.append(f"'{name}' -> {c.name} is not a member of the group '{group[0]}'.")
            continue
        out[name] = ResolvedPerson(
            receipt_name=name, splitwise_id=c.id, splitwise_name=c.name, email=c.email, is_me=c.is_me
        )

    same: dict[int | str, list[str]] = defaultdict(list)
    for r in out.values():
        same[r.splitwise_id if r.splitwise_id is not None else str(r.email).casefold()].append(r.receipt_name)
    for names in same.values():
        if len(names) > 1:
            problems.append(f"{names} all map to the same Splitwise user. Each person on the receipt needs their own.")

    if problems:
        raise ValueError("Can't match people to Splitwise:\n- " + "\n- ".join(problems))
    return out
