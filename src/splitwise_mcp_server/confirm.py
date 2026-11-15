"""Confirmation codes that tie add_expense_to_splitwise to a preview the user has seen.

preview_splitwise_expense signs the exact form that would be posted. add re-derives the
form from its own arguments and accepts only a matching, unexpired signature, so any
change after the preview (amount, people, payer, group, title, date) is refused. The
code is bound to the Splitwise account, and is an HMAC under a key derived from
SERVER_SECRET, so any server instance can check it. It is single-use: add marks it
spent in the shared store before posting.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

CONFIRM_TTL = 30 * 60


class Confirmer:
    def __init__(self, secret: str | None = None, ttl: int = CONFIRM_TTL):
        # Without a secret (local stdio mode) a per-process key works: one process.
        self._key = hashlib.sha256(b"confirm:" + secret.encode()).digest() if secret else secrets.token_bytes(32)
        self._ttl = ttl

    def _mac(self, expires: int, nonce: str, account: str, form: dict[str, str]) -> str:
        msg = json.dumps(
            {"exp": expires, "nonce": nonce, "account": hashlib.sha256(account.encode()).hexdigest(), "form": form},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()[:32]

    def sign(self, form: dict[str, str], account: str, now: float | None = None) -> str:
        expires = int(time.time() if now is None else now) + self._ttl
        # The nonce makes every preview's code distinct, so spending one never spends another.
        nonce = secrets.token_hex(8)
        return f"{expires}.{nonce}.{self._mac(expires, nonce, account, form)}"

    @staticmethod
    def used_key(code: str) -> str:
        """Store key that marks a confirmation code as spent."""
        return "confirm-used:" + hashlib.sha256(code.strip().encode()).hexdigest()

    def problem(self, code: str, form: dict[str, str], account: str, now: float | None = None) -> str | None:
        """None if `code` confirms this exact form for this account; otherwise what's wrong."""
        try:
            expires_text, nonce, mac = code.strip().split(".")
            expires = int(expires_text)
        except ValueError:
            return "that is not a confirmation code from preview_splitwise_expense"
        if not hmac.compare_digest(mac, self._mac(expires, nonce, account, form)):
            return "the expense is not the one that was previewed"
        if expires < (time.time() if now is None else now):
            return "the preview has expired"
        return None
