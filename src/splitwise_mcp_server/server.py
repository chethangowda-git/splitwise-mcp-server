"""MCP server exposing receipt reconciliation, splitting, and Splitwise tools."""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx2
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .allocation import ROUNDING_METHOD, AllocationError, allocate
from .balances import Balances, get_balances
from .confirm import CONFIRM_TTL, Confirmer
from .deletion import (
    DeletionPreview,
    ExpenseList,
    deletion_fingerprint,
    deletion_problem,
    deletion_summary,
    expense_info,
)
from .expenses import (
    ExpensePreview,
    PreparedExpense,
    build_preview,
    prepare_expense,
    saved_differences,
)
from .models import AllocationResult, Receipt, Reconciliation, SplitResult
from .oauth import (
    CALLBACK_PATH,
    CONSENT_PATH,
    OAuthConfig,
    SplitwiseAccessToken,
    SplitwiseOAuthProvider,
)
from .people import Directory, PeopleSearch, find_people
from .quota import QuotaConfig, WriteQuota, quota_config_from_env
from .settlements import (
    Direction,
    SettlementPreview,
    prepare_settlement,
    saved_payment_problem,
    settlement_summary,
)
from .split import SplitError, reconcile, split
from .splitwise_api import (
    CreatedExpense,
    SplitwiseClient,
    SplitwiseError,
    SplitwisePeople,
    find_duplicate,
    list_people,
)
from .store import MemoryStore, RedisStore, Store

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Split receipts between people to the exact cent, and optionally record them in Splitwise.

Workflow:
1. Parse the receipt into items, discounts, tax, tip, fees, and the printed total.
   Pass money as strings (e.g. "12.34").
2. Call reconcile_receipt. Fix any reported problems with the user before going on.
   Never absorb an unexplained difference into someone's share.
3. Ask the user whatever is ambiguous: who had which item, whether shared items are
   split equally or by weight, which items are tax-exempt, how each fee is shared,
   and the tip policy if it is not proportional.
4. Call split_receipt and show the result.
5. To record it in Splitwise, first match people: call find_splitwise_people with
   every name on the receipt (use "me" for the user). For "ambiguous", show the
   candidates' names and emails and ask which one. For "none", ask for the person's
   email (Splitwise will invite them) or offer the suggestions. Never guess a person.
   Ask who paid, and use list_splitwise_people if the expense belongs in a group.
6. Call preview_splitwise_expense and show the user its summary. Add nothing until
   they explicitly approve it. Then call add_expense_to_splitwise with the same
   arguments plus the preview's confirmation. If anything changes, preview again.
7. If the preview warns about a duplicate, tell the user. Pass allow_duplicate=true
   only if they confirm it is a separate expense.

For "who owes me" or "how much do I owe" questions, call get_splitwise_balances.

To delete an expense: find it with list_splitwise_expenses (confirm with the user which
one if there is any doubt), call preview_splitwise_deletion and show its summary, and
call delete_splitwise_expense only after the user explicitly approves. Only expenses
the user added can be deleted.

To record a settle-up payment: check the balance with get_splitwise_balances, call
preview_splitwise_settlement and show its summary, and call record_splitwise_settlement
only after the user explicitly approves. Only record money the user says has actually
changed hands. To explain how a balance adds up, use list_splitwise_expenses with
friend_id (and group_id).
"""

TokenSource = Callable[[], str | None]


def oauth_token_source() -> str | None:
    token = get_access_token()
    return token.splitwise_token if isinstance(token, SplitwiseAccessToken) else None


def create_server(
    oauth: OAuthConfig | None = None,
    splitwise_token: TokenSource | None = None,
    http: httpx2.AsyncClient | None = None,
    store: Store | None = None,
    quota: QuotaConfig | None = None,
) -> MCPServer:
    """Build the server.

    oauth: enable per-user Splitwise sign-in (remote HTTP deployments).
    splitwise_token: where Splitwise tools get a token. Defaults to the signed-in
        user's token when oauth is set. Splitwise tools are registered only when
        one of the two is given.
    http: optional shared HTTP client for calls to Splitwise (used by tests).
    store: OAuth replay/revocation and used-confirmation store. Defaults to Redis
        when oauth.redis_url is set, otherwise in memory.
    quota: limit each user's changes to Splitwise (adds, deletes, settle-ups). None = no limit.
    """
    if store is None and oauth is not None and oauth.redis_url:
        store = RedisStore.from_url(oauth.redis_url)
    if store is None:
        store = MemoryStore()
    provider = SplitwiseOAuthProvider(oauth, http, store) if oauth else None
    mcp = MCPServer(
        name="splitwise-mcp-server",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=oauth.auth_settings() if oauth else None,
    )
    if splitwise_token is None and oauth is not None:
        splitwise_token = oauth_token_source

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    if provider is not None:

        @mcp.custom_route(CONSENT_PATH, methods=["GET", "POST"])
        async def consent(request: Request):
            return await provider.handle_consent(request)

        @mcp.custom_route(CALLBACK_PATH, methods=["GET"])
        async def splitwise_callback(request: Request):
            return await provider.handle_callback(request)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def reconcile_receipt(receipt: Receipt) -> Reconciliation:
        """Check that the parsed receipt adds up, without assigning anything to people.

        Compares the item lines to the printed subtotal (if given) and
        subtotal - discounts + tax + tip + fees to the printed total. Also reports
        anything that would make a split ambiguous: unassigned items, shared items
        without weights, fees without a policy, amounts finer than one cent, and so on.
        """
        return reconcile(receipt)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def split_receipt(receipt: Receipt) -> SplitResult:
        """Split a reconciled receipt between people.

        Items are split by their weights (equal only when split='equal'). Discounts are
        split by covered item subtotals unless another allocation is given. Tax follows
        each person's taxable amount after discounts. Tip is proportional to tip-eligible
        pre-tax items by default, or equal/custom. Each fee needs an explicit policy.
        Each component is rounded with the largest-remainder method, so everyone's
        shares add up to exactly the printed total. Fails with a list of problems if the
        receipt does not reconcile or anything is ambiguous.
        """
        try:
            return split(receipt)
        except SplitError as e:
            raise ToolError("Cannot split receipt:\n- " + "\n- ".join(e.problems)) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def allocate_amount(
        amount: Decimal, weights: dict[str, Decimal], minor_unit_digits: int = 2
    ) -> AllocationResult:
        """Split one amount across people by weight, to the exact cent.

        Uses the same largest-remainder rounding as split_receipt. Useful for one-off
        amounts such as a shared ride or a deposit.
        """
        try:
            shares, extra = allocate(amount, weights, minor_unit_digits)
        except AllocationError as e:
            raise ToolError(str(e)) from e
        return AllocationResult(
            amount=amount, shares=shares, extra_minor_units=extra, rounding_method=ROUNDING_METHOD
        )

    if splitwise_token is None:
        return mcp

    def current_token() -> str:
        token = splitwise_token()
        if not token:
            raise ToolError("Not signed in to Splitwise. Reconnect the Splitwise connector and try again.")
        return token

    def client() -> SplitwiseClient:
        return SplitwiseClient(current_token(), http)

    confirmer = Confirmer(oauth.server_secret if oauth else None)
    writes = WriteQuota(store, quota)

    async def quota_user(sw: SplitwiseClient) -> int | None:
        """The Splitwise user id the change limit applies to (looked up only when there is a limit)."""
        if not writes.enabled:
            return None
        try:
            return int((await sw.current_user())["id"])
        except SplitwiseError as e:
            raise ToolError(str(e)) from e

    async def prepare(
        receipt: Receipt, paid_by: str, members: dict[str, int | str], description: str, group_id: int, date: str | None
    ) -> tuple[str, PreparedExpense]:
        token = current_token()
        try:
            prepared = await prepare_expense(
                SplitwiseClient(token, http),
                receipt,
                paid_by=paid_by,
                members=members,
                description=description,
                group_id=group_id,
                date=date,
            )
        except SplitError as e:
            raise ToolError("Cannot split receipt:\n- " + "\n- ".join(e.problems)) from e
        except (ValueError, SplitwiseError) as e:
            raise ToolError(str(e)) from e
        return token, prepared

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def list_splitwise_people() -> SplitwisePeople:
        """List the signed-in Splitwise user, their friends, and their groups with members.

        Use it to pick a group for the expense. To match names on a receipt to
        Splitwise users, use find_splitwise_people.
        """
        try:
            return await list_people(client())
        except SplitwiseError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def get_splitwise_balances(group_id: int | None = None) -> Balances:
        """Who owes the signed-in user and whom they owe, from Splitwise's own balances.

        group_id: only balances in that group (0 = expenses outside any group);
            omit for everything.

        Positive amounts mean the other person owes the user. Totals are per currency,
        never added across currencies. Includes expenses added in the Splitwise app.
        """
        try:
            return await get_balances(client(), group_id)
        except (ValueError, SplitwiseError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def find_splitwise_people(names: list[str]) -> PeopleSearch:
        """Look up names (or emails) among the user, their friends, and their groups' members.

        Each result is 'match' (one person), 'ambiguous' (several: ask the user which
        one), or 'none' (ask for an email, or offer the close suggestions). Use "me"
        for the signed-in user. It never picks between several people.
        """
        try:
            directory = Directory.from_people(await list_people(client()))
        except SplitwiseError as e:
            raise ToolError(str(e)) from e
        return find_people(directory, names)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def preview_splitwise_expense(
        receipt: Receipt,
        paid_by: str,
        members: dict[str, int | str],
        description: str,
        group_id: int = 0,
        date: str | None = None,
    ) -> ExpensePreview:
        """Show exactly what add_expense_to_splitwise would record, without recording anything.

        Takes the same arguments as add_expense_to_splitwise (minus confirmation).
        Checks every person against the user's Splitwise contacts, reports a likely
        duplicate, and returns a summary to show the user and a confirmation code that
        add_expense_to_splitwise requires.
        """
        token, prepared = await prepare(receipt, paid_by, members, description, group_id, date)
        await writes.check(await quota_user(SplitwiseClient(token, http)), "added")
        try:
            duplicate = await find_duplicate(SplitwiseClient(token, http), prepared.form)
        except SplitwiseError as e:
            raise ToolError(str(e)) from e
        return build_preview(
            prepared,
            paid_by=paid_by,
            description=description,
            group_id=group_id,
            date=date,
            duplicate=duplicate,
            confirmation=confirmer.sign(prepared.form, token),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        )
    )
    async def add_expense_to_splitwise(
        receipt: Receipt,
        paid_by: str,
        members: dict[str, int | str],
        description: str,
        confirmation: str,
        group_id: int = 0,
        date: str | None = None,
        allow_duplicate: bool = False,
    ) -> CreatedExpense:
        """Record the receipt as one expense in Splitwise, after the user approved its preview.

        paid_by: the person on the receipt who paid the whole bill.
        members: receipt person name -> Splitwise user id (from find_splitwise_people)
            or email address. Required for the payer and everyone who owes something.
        description: expense title shown in Splitwise, e.g. "Dinner at Luigi's".
        confirmation: from preview_splitwise_expense, called with these same arguments.
        group_id: Splitwise group id, or 0 for an expense outside any group.
        date: optional ISO 8601 date of the purchase.
        allow_duplicate: set true only after the user confirms that a reported
            duplicate is really a separate expense.

        Refuses if anything differs from the preview, the preview is over 30 minutes
        old, a person doesn't check out against the user's contacts, or this server
        already added the same expense in the last 30 days (a deleted one doesn't count).
        """
        token, prepared = await prepare(receipt, paid_by, members, description, group_id, date)
        if problem := confirmer.problem(confirmation, prepared.form, token):
            raise ToolError(
                f"Not added: {problem}. Call preview_splitwise_expense with these arguments, show the user "
                "its summary, and pass its confirmation once they approve."
            )
        sw = SplitwiseClient(token, http)
        try:
            if not allow_duplicate and (dup := await find_duplicate(sw, prepared.form)):
                raise ToolError(
                    f"Not added: this looks like a duplicate of Splitwise expense {dup.get('id')} "
                    f"(\"{dup.get('description')}\", {dup.get('cost')} {dup.get('currency_code')}, "
                    f"created {dup.get('created_at')}). It has the same group, date, currency, total, "
                    "and owed shares. If the user confirms this is a separate expense, call again "
                    "with allow_duplicate=true."
                )
            # A preview approves one expense: two adds racing on the same code, or a retry
            # after a lost response, can't both post. The second gets a fresh preview, which
            # then reports the first as a duplicate.
            user = await quota_user(sw)
            if not await store.claim(confirmer.used_key(confirmation), CONFIRM_TTL):
                raise ToolError(
                    "Not added: this preview's confirmation was already used. Call "
                    "preview_splitwise_expense again; it will show whether the expense is already in Splitwise."
                )
            await writes.take(user, "added")
            try:
                expense = await sw.create_expense(prepared.form)
            except SplitwiseError:
                await writes.give_back(user)
                raise
        except SplitwiseError as e:
            raise ToolError(str(e)) from e
        if differences := saved_differences(prepared, expense):
            raise ToolError(
                f"Added as Splitwise expense {expense['id']}, but Splitwise saved it differently from the "
                "preview: " + "; ".join(differences) + ". Tell the user, and have them fix or delete it in "
                "Splitwise. Do not add it again."
            )
        result = prepared.result
        return CreatedExpense(
            expense_id=expense["id"],
            description=description,
            cost=result.grand_total,
            currency=result.currency,
            group_id=group_id,
            paid_by=paid_by,
            owed={p.person: p.total for p in result.people if p.total},
        )

    async def _expense_context(sw: SplitwiseClient) -> tuple[int, dict[int, str]]:
        me = await sw.current_user()
        return me["id"], {g["id"]: g.get("name") or "" for g in await sw.groups() if g.get("id")}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def list_splitwise_expenses(
        group_id: int | None = None, friend_id: int | None = None, limit: int = 20
    ) -> ExpenseList:
        """Expense history, most recent first: use it to find an expense, or to explain how a balance adds up.

        group_id: only that group's expenses. friend_id: only expenses shared with that
        person (combine both for one friend within one group). limit: 1 to 100.
        Each expense lists its shares, who added it, whether it's a settle-up payment, and
        effect_on_you: how it moved your balance with each person. With friend_id,
        friend_total sums those effects, which explains a balance from
        get_splitwise_balances when the list reaches back far enough.
        """
        if not 1 <= limit <= 100:
            raise ToolError("limit must be between 1 and 100")
        sw = client()
        try:
            me_id, names = await _expense_context(sw)
            raw = await sw.recent_expenses(group_id=group_id, friend_id=friend_id, limit=limit)
        except SplitwiseError as e:
            raise ToolError(str(e)) from e
        rows = [expense_info(e, me_id=me_id, group_names=names) for e in raw if not e.get("deleted_at")]
        total = None
        if friend_id is not None:
            rows = [r for r in rows if any(s.user_id == friend_id for s in r.shares)]
            total = {}
            for r in rows:
                for eff in r.effect_on_you:
                    if eff.user_id == friend_id:
                        total[r.currency] = total.get(r.currency, Decimal(0)) + eff.amount
        return ExpenseList(expenses=rows, friend_total=total)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def preview_splitwise_deletion(expense_id: int) -> DeletionPreview:
        """Show exactly which expense would be deleted, and get a confirmation code. Deletes nothing.

        Refuses expenses the user didn't add, settle-up payments, and already-deleted
        ones. Show the summary
        to the user and get an explicit yes before calling delete_splitwise_expense.
        """
        token = current_token()
        sw = SplitwiseClient(token, http)
        try:
            me_id, names = await _expense_context(sw)
            e = await sw.expense(expense_id)
        except SplitwiseError as err:
            raise ToolError(str(err)) from err
        if problem := deletion_problem(e, me_id):
            raise ToolError(f"Can't delete: {problem}.")
        await writes.check(me_id if writes.enabled else None, "deleted")
        info = expense_info(e, me_id=me_id, group_names=names)
        return DeletionPreview(
            expense=info,
            summary=deletion_summary(info),
            confirmation=confirmer.sign(deletion_fingerprint(e), token),
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False, open_world_hint=True))
    async def delete_splitwise_expense(expense_id: int, confirmation: str) -> DeletionPreview:
        """Delete one expense the user added, after they approved its preview.

        confirmation: from preview_splitwise_deletion for this expense_id. Refuses if the
        expense changed after the preview, the preview is over 30 minutes old or was
        already used, the user didn't add the expense, or it's a settle-up payment.
        Checks afterwards that Splitwise really deleted it. Returns what was deleted.
        """
        token = current_token()
        sw = SplitwiseClient(token, http)
        try:
            me_id, names = await _expense_context(sw)
            e = await sw.expense(expense_id)
            if problem := deletion_problem(e, me_id):
                raise ToolError(f"Not deleted: {problem}.")
            if problem := confirmer.problem(confirmation, deletion_fingerprint(e), token):
                raise ToolError(
                    f"Not deleted: {problem}. Call preview_splitwise_deletion again, show the user its "
                    "summary, and pass its confirmation once they approve."
                )
            if not await store.claim(confirmer.used_key(confirmation), CONFIRM_TTL):
                raise ToolError("Not deleted: this preview's confirmation was already used.")
            user = me_id if writes.enabled else None
            await writes.take(user, "deleted")
            try:
                await sw.delete_expense(expense_id)
            except SplitwiseError:
                await writes.give_back(user)
                raise
            log.info("deleted expense_id=%d", int(expense_id))
            # Like adds, don't trust the status alone: check Splitwise really deleted it.
            after = await sw.expense(expense_id)
        except SplitwiseError as err:
            raise ToolError(str(err)) from err
        if not after.get("deleted_at"):
            raise ToolError(
                f"Splitwise reported expense {expense_id} as deleted, but it still shows as active. "
                "Tell the user to check it in the Splitwise app. Do not retry without a new preview."
            )
        info = expense_info(e, me_id=me_id, group_names=names)
        return DeletionPreview(expense=info, summary="Deleted.\n" + deletion_summary(info), confirmation="")

    async def _prepare_settlement(
        friend_id: int, amount: str, direction: Direction, group_id: int, currency: str
    ) -> tuple[str, SplitwiseClient, Any]:
        token = current_token()
        sw = SplitwiseClient(token, http)
        try:
            prepared = await prepare_settlement(
                sw, friend_id=friend_id, amount=amount, direction=direction, group_id=group_id, currency=currency
            )
        except (ValueError, SplitwiseError) as e:
            raise ToolError(str(e)) from e
        return token, sw, prepared

    def _settlement_preview(prepared: Any, confirmation: str, summary: str) -> SettlementPreview:
        return SettlementPreview(
            friend_id=prepared.friend_id, friend_name=prepared.friend_name, direction=prepared.direction,
            amount=prepared.amount, currency=prepared.currency, group_id=prepared.group_id,
            group_name=prepared.group_name, balance_before=prepared.balance_before,
            balance_after=prepared.balance_after, likely_duplicate_of=prepared.duplicate_of,
            summary=summary, confirmation=confirmation,
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def preview_splitwise_settlement(
        friend_id: int, amount: str, direction: Direction, group_id: int = 0, currency: str = "USD"
    ) -> SettlementPreview:
        """Show a settle-up payment before recording it, with the balance before and after. Records nothing.

        friend_id: the other person (from find_splitwise_people). amount: e.g. "25.70".
        direction: "they_paid_me" or "i_paid_them". group_id: the group the balance is in
        (0 = outside groups). Refuses a payment larger than what's owed that way.
        """
        token, sw, prepared = await _prepare_settlement(friend_id, amount, direction, group_id, currency)
        await writes.check(await quota_user(sw), "recorded")
        return _settlement_preview(prepared, confirmer.sign(prepared.form, token), settlement_summary(prepared))

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False, open_world_hint=True))
    async def record_splitwise_settlement(
        friend_id: int,
        amount: str,
        direction: Direction,
        confirmation: str,
        group_id: int = 0,
        currency: str = "USD",
        allow_duplicate: bool = False,
    ) -> SettlementPreview:
        """Record a settle-up payment the user approved in preview_splitwise_settlement.

        Same arguments as the preview, plus its confirmation (single-use, 30 minutes).
        allow_duplicate: only if the user confirms a reported matching payment is separate.
        Checks what Splitwise saved.
        """
        token, sw, prepared = await _prepare_settlement(friend_id, amount, direction, group_id, currency)
        if problem := confirmer.problem(confirmation, prepared.form, token):
            raise ToolError(
                f"Not recorded: {problem}. Call preview_splitwise_settlement with these arguments, show the "
                "user its summary, and pass its confirmation once they approve."
            )
        if prepared.duplicate_of and not allow_duplicate:
            raise ToolError(
                f"Not recorded: payment {prepared.duplicate_of} from the last 7 days matches this one. If the user "
                "confirms this is a separate payment, call again with allow_duplicate=true."
            )
        user = await quota_user(sw)
        if not await store.claim(confirmer.used_key(confirmation), CONFIRM_TTL):
            raise ToolError("Not recorded: this preview's confirmation was already used.")
        await writes.take(user, "recorded")
        try:
            saved = await sw.create_expense(prepared.form)
        except SplitwiseError as e:
            await writes.give_back(user)
            raise ToolError(str(e)) from e
        log.info("recorded payment expense_id=%s", saved.get("id"))
        if problem := saved_payment_problem(prepared, saved):
            raise ToolError(
                f"Recorded as Splitwise expense {saved.get('id')}, but {problem}. Tell the user to check it in the "
                "Splitwise app. Do not record it again."
            )
        return _settlement_preview(
            prepared, "", f"Recorded (Splitwise id {saved.get('id')}).\n" + settlement_summary(prepared)
        )

    _log_tool_use(mcp)
    return mcp


def _log_tool_use(mcp: MCPServer) -> None:
    """Log each tool call (name, outcome, duration) and tool listing. Never arguments or results."""
    call_tool, list_tools = mcp.call_tool, mcp.list_tools

    async def logged_call_tool(name: str, arguments: dict, *args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        outcome = "error"
        try:
            result = await call_tool(name, arguments, *args, **kwargs)
            outcome = "ok"
            return result
        finally:
            log.info("tool=%s outcome=%s ms=%d", name, outcome, (time.monotonic() - start) * 1000)

    async def logged_list_tools(*args: Any, **kwargs: Any) -> Any:
        tools = await list_tools(*args, **kwargs)
        log.info("list_tools -> %s", ",".join(sorted(t.name for t in tools)))
        return tools

    mcp.call_tool = logged_call_tool  # type: ignore[method-assign]
    mcp.list_tools = logged_list_tools  # type: ignore[method-assign]


def oauth_config_from_env() -> OAuthConfig | None:
    names = ("PUBLIC_URL", "SPLITWISE_CLIENT_ID", "SPLITWISE_CLIENT_SECRET", "SERVER_SECRET")
    values = {n: os.environ.get(n, "").strip() for n in names}
    if not any(values.values()):
        return None
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise SystemExit(f"Splitwise sign-in is partly configured; missing: {', '.join(missing)}")
    if len(values["SERVER_SECRET"]) < 32:
        raise SystemExit("SERVER_SECRET must be at least 32 characters")
    return OAuthConfig(
        public_url=values["PUBLIC_URL"].rstrip("/"),
        splitwise_client_id=values["SPLITWISE_CLIENT_ID"],
        splitwise_client_secret=values["SPLITWISE_CLIENT_SECRET"],
        server_secret=values["SERVER_SECRET"],
        redis_url=os.environ.get("REDIS_URL", "").strip() or None,
    )


def load_ssm_parameters(path: str) -> None:
    """Copy SSM Parameter Store values under `path` into the environment (AWS Lambda).

    /splitwise-mcp/SERVER_SECRET becomes $SERVER_SECRET. Variables already set win, so
    secrets stay out of the function's configuration and live encrypted in SSM.
    """
    import boto3  # available in the AWS Lambda Python runtime

    ssm = boto3.client("ssm")
    token = None
    while True:
        kwargs = {"Path": path, "WithDecryption": True, **({"NextToken": token} if token else {})}
        page = ssm.get_parameters_by_path(**kwargs)
        for p in page["Parameters"]:
            os.environ.setdefault(p["Name"].rsplit("/", 1)[-1], p["Value"])
        token = page.get("NextToken")
        if not token:
            return


def main() -> None:
    parser = argparse.ArgumentParser(prog="splitwise-mcp-server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve streamable HTTP at /mcp instead of stdio. Implied when $PORT is set (e.g. on Railway).",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.http or "PORT" in os.environ:
        if ssm_path := os.environ.get("SSM_PARAMETER_PATH", "").strip():
            load_ssm_parameters(ssm_path)
        oauth = oauth_config_from_env()
        if oauth is None:
            log.warning("Splitwise sign-in is not configured: serving calculation tools only, without auth.")
        elif not oauth.redis_url:
            log.warning(
                "REDIS_URL is not set: used codes, rotated refresh tokens, and revocations are kept in memory. "
                "That is only correct for a single instance, and a restart forgets them."
            )
        quota = quota_config_from_env()
        if quota is not None:
            log.info("Change limit: %d per user, %d exempt", quota.limit, len(quota.exempt))
        mcp = create_server(oauth, quota=quota)
        # Stateless: every request stands alone, so any replica can serve it.
        # MCP_JSON_RESPONSE=1 answers with plain JSON instead of an SSE stream, for hosts that
        # buffer responses (AWS Lambda function URLs).
        json_response = os.environ.get("MCP_JSON_RESPONSE", "").strip().lower() in ("1", "true", "yes")
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=json_response)
    else:
        # Local stdio: a personal API key (never used by the public HTTP server).
        api_key = os.environ.get("SPLITWISE_API_KEY", "").strip() or None
        mcp = create_server(splitwise_token=(lambda: api_key) if api_key else None)
        mcp.run()


if __name__ == "__main__":
    main()
