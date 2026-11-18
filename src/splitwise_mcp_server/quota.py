"""A per-user limit on changes to Splitwise, for a shared hosted server.

Only changes count: adding an expense, deleting one, and recording a settle-up.
Reading, splitting, and previews stay free. The count is per Splitwise user id and
never resets; the owner can lift it for someone by adding their id to the exempt list
(or by deleting their counter in Redis).

A slot is taken atomically just before the write (so racing writes can't both slip
past the limit), and given back if Splitwise rejects the write. Previews check the
limit too, so a user who has none left hears it before going through a whole preview.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from mcp.server.mcpserver.exceptions import ToolError

from .store import Store


@dataclass(frozen=True)
class QuotaConfig:
    limit: int
    exempt: frozenset[int] = field(default_factory=frozenset)
    contact: str | None = None


def quota_config_from_env() -> QuotaConfig | None:
    """WRITE_QUOTA (a number; unset = no limit), QUOTA_EXEMPT_USER_IDS (comma-separated
    Splitwise user ids), QUOTA_CONTACT (where to ask for more, e.g. a LinkedIn URL)."""
    raw = os.environ.get("WRITE_QUOTA", "").strip()
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError:
        raise SystemExit(f"WRITE_QUOTA must be a whole number, got {raw!r}") from None
    if limit < 0:
        raise SystemExit("WRITE_QUOTA can't be negative")
    ids = os.environ.get("QUOTA_EXEMPT_USER_IDS", "")
    try:
        exempt = frozenset(int(i) for i in ids.replace(" ", "").split(",") if i)
    except ValueError:
        raise SystemExit("QUOTA_EXEMPT_USER_IDS must be comma-separated Splitwise user ids") from None
    contact = os.environ.get("QUOTA_CONTACT", "").strip() or None
    return QuotaConfig(limit=limit, exempt=exempt, contact=contact)


class WriteQuota:
    def __init__(self, store: Store, config: QuotaConfig | None):
        self._store = store
        self._config = config

    @staticmethod
    def _key(user_id: int) -> str:
        return f"quota:writes:{int(user_id)}"

    @property
    def enabled(self) -> bool:
        return self._config is not None

    def _limited(self, user_id: int | None) -> bool:
        return self._config is not None and user_id is not None and int(user_id) not in self._config.exempt

    def _refusal(self, action: str) -> ToolError:
        assert self._config is not None
        contact = f" To get more, contact the server's owner: {self._config.contact}" if self._config.contact else ""
        return ToolError(
            f"Not {action}: this hosted server allows {self._config.limit} changes to Splitwise per user, and "
            f"this account has used them all.{contact} Reading balances and splitting receipts still work. "
            "Tell the user this, including the contact link, and don't retry."
        )

    async def check(self, user_id: int | None, action: str) -> None:
        """Refuse early (in a preview) when the user has no changes left."""
        if self._limited(user_id) and await self._store.count(self._key(user_id)) >= self._config.limit:
            raise self._refusal(action)

    async def take(self, user_id: int | None, action: str) -> None:
        """Use one change, or refuse. Call right before the write; give_back() it if the write fails."""
        if not self._limited(user_id):
            return
        if await self._store.incr(self._key(user_id)) > self._config.limit:
            await self._store.decr(self._key(user_id))
            raise self._refusal(action)

    async def give_back(self, user_id: int | None) -> None:
        if self._limited(user_id):
            await self._store.decr(self._key(user_id))
