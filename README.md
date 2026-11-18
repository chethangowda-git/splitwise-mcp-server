

https://github.com/user-attachments/assets/fdc9f888-d070-4c87-b752-a3e27957d150

# splitwise-mcp-server

Split a receipt by chatting with Claude or ChatGPT, get everyone's share to the exact cent, and record it in Splitwise.

<!-- Demo video: add the link here once it's recorded (see docs/demo.md). -->

This is an [MCP](https://modelcontextprotocol.io) server, which Claude and ChatGPT call a *connector*. The AI does the fuzzy work: it reads the receipt photo and asks who had what. This server does the work that has to be exact: it checks that the receipt adds up, splits every line by explicit rules, refuses to guess when something is ambiguous, and posts one expense to Splitwise.

## Use it

A hosted server is running. Add it as a connector with this URL:

```
https://xdic6hbarjoa6btpgkc6fc3jwu0oaxor.lambda-url.us-east-1.on.aws/mcp
```

- **ChatGPT** (paid plans, Developer mode):
  1. **Settings → Apps & Connectors → Advanced settings →** turn on **Developer mode**.
  2. **Apps & Connectors → Create**. Paste the URL above, choose **OAuth** authentication, and create it.
  3. Click **Connect**, approve the consent page, then sign in to Splitwise and allow access.
  4. In a new chat, turn the connector on from the **+** menu and try: *"How much do my friends owe me on Splitwise?"*
- **Claude** (plans with custom connectors): **Settings → Connectors → Add custom connector**, paste the same URL, and connect.

You sign in with your own Splitwise account; you don't need Splitwise Pro. The hosted server allows **5 changes per account** (adding an expense, deleting one, or recording a settle-up); reading balances, explaining them, and splitting receipts are unlimited. Want more? [Message me on LinkedIn](https://www.linkedin.com/in/chethangowdagt/). Nothing is changed in your Splitwise without a preview you approve. The server keeps no database: your Splitwise access lives only inside your own encrypted sign-in token, and logs record tool names, never what you asked or your data. You can revoke access anytime in your Splitwise account settings.

After the server gains new tools, refresh or re-add the connector: clients keep the tool list from when it was added. More detail: [docs/demo.md](docs/demo.md#use-it-in-chatgpt). How it was designed and what live testing found: [docs/how-it-was-built.md](docs/how-it-was-built.md). To host your own instead, see [AWS Lambda](#aws-lambda) and [Splitwise sign-in](#splitwise-sign-in-multi-user).

## What it looks like

> **You:** *(photo of a receipt)* Split this between me, Bob, and Cara. Bob and I shared the pizza, he had 2 of the 3 slices. Knots were for everyone, the salad was Cara's, the lemonade mine, the beer Bob's.
>
> **AI:** The receipt adds up to $57.44. How should the $2.99 delivery fee be shared: equally, in proportion to what each person ordered, or some other way?
>
> **You:** Equally.
>
> **AI:** Ann $15.11 · Bob $25.79 · Cara $16.54 (total $57.44). Tax and tip follow what each person ordered, after the $5 promo.
>
> **You:** I paid. Add it to Splitwise.
>
> **AI:** You have two Bobs in Splitwise: Bob Smith (bob.smith@…) and Bob Lee (blee@…). Which one?
>
> **You:** Bob Smith.
>
> **AI:** Here's what I'll add:
> "Dinner at Luigi's": 57.44 USD, outside any group. Paid by Ann (you).
> Ann (you) owes 15.11 · Bob Smith owes 25.79 · Cara Diaz owes 16.54.
> Add it?
>
> **You:** Yes.
>
> **AI:** Added. Bob owes you $25.79 and Cara owes you $16.54.
>
> **You:** Add it again.
>
> **AI:** That's already in Splitwise (same date, total, and shares), so I didn't add a second copy.
>
> **You:** Bob paid me back.
>
> **AI:** Here's the payment I'll record:
> Bob Smith paid you 25.79 USD, outside any group. Now: Bob Smith owes you 25.79 USD. After: settled up.
> Record it?
>
> **You:** Yes.
>
> **AI:** Recorded. You and Bob are settled up.

## How it works

```
 Claude / ChatGPT                    splitwise-mcp-server                      Splitwise
 ────────────────                    ────────────────────                      ─────────
 reads the receipt,  ──tool call──▶  reconcile: does it add up?
 asks who had what                   split: exact shares per person
                     ◀──result────   (or a list of what's ambiguous)
                     ──tool call──▶  duplicate check  ──────── get_expenses ──▶
                                     add expense      ──────── create_expense ─▶
```

## Design decisions

- **Exact money, rounding only where it must happen.** Amounts are `Decimal` at the edges and exact `Fraction`s inside, so a third is exactly a third. Each item, discount, tax, tip, and fee is rounded once, with the largest-remainder method and ties broken by name. Every component's shares add up to that component exactly, so everyone's totals add up to the printed total, and the same input always gives the same split. Rounding each person's total instead can gain or lose a cent.
- **Check the receipt before splitting it.** If the parsed lines don't add up to the printed total, the server reports the difference and stops. A misread line is never quietly absorbed into someone's share.
- **Refuse to guess.** An item several people shared needs weights, or an explicit statement that it was split equally. A fee needs a rule for how it's shared. When something is missing, the server returns the question, and the AI asks the user.
- **Never guess a person.** Name lookup reports one match, several, or none, and never picks between several. Before anything is previewed or posted, the server checks every person against the user's own Splitwise contacts: an id must be the user, a friend, or a member of the target group, and two names on the receipt can't be the same person. A wrong or made-up id is refused rather than posted.
- **Nothing is added without an approved preview.** The preview shows exactly what will be recorded, with real Splitwise names and emails. Adding needs the preview's confirmation code: a signature over the exact expense, valid for 30 minutes and usable once. If anything changes after the preview, the add is refused.
- **A per-user change limit for the shared server.** Each Splitwise account gets a fixed number of changes. A slot is taken atomically just before the write, so racing writes can't both pass the limit, and handed back if Splitwise rejects it. Previews check it first, so nobody goes through a whole preview only to be refused.
- **Settling up is guarded too.** A payment can't exceed what's owed in that direction, needs an approved preview, and a matching payment from the last week is reported as a likely double-record.
- **Deleting is as guarded as adding.** Only expenses the user added, one per approved preview; the preview's code covers the expense as it was, so an edit in between voids it, and the server re-reads the expense afterwards to confirm the delete.
- **Check what Splitwise actually saved.** Splitwise can return success and still change an expense (it silently drops a new person from a group expense). After every add, the server compares the saved shares with the previewed ones and reports any difference instead of claiming success.
- **Duplicate protection without a database.** Each expense carries a fingerprint in its Splitwise notes. Before adding one, the server looks for that fingerprint in recent expenses, so Splitwise itself is the record.
- **Multi-user sign-in without a user database.** The server is a full OAuth 2.1 authorization server (dynamic client registration, PKCE, single-use codes, refresh token rotation with replay detection, revocation) that hands the actual login to Splitwise. Tokens are encrypted and self-contained. The only server-side state is the short-lived record OAuth requires, kept in Redis.
- **A consent page before Splitwise.** Anyone can register an OAuth client with any redirect address, and Splitwise's own screen only names this server. The consent page shows which app is asking and where the user will be sent, so a crafted link can't quietly hand someone's account to a stranger.

## Tools

| Tool | Purpose |
|---|---|
| `reconcile_receipt` | Checks that item lines, discounts, tax, tip, and fees add up to the printed subtotal and total. Lists anything ambiguous. Assigns nothing to anyone. |
| `split_receipt` | Reconciles, then allocates every component to people. Refuses if the receipt doesn't balance or anything is ambiguous. |
| `allocate_amount` | Splits a single amount by weights using the same rounding. |
| `list_splitwise_people` | Lists the signed-in user's Splitwise friends and groups, with ids. |
| `find_splitwise_people` | Looks up names or emails among the user, their friends, and their groups' members. Each result is `match`, `ambiguous` (with every candidate's name and email), or `none` (with close suggestions). |
| `preview_splitwise_expense` | Read-only. Shows exactly what would be added: the people as Splitwise knows them, each share, the group, and any likely duplicate. Returns a summary to show the user and a confirmation code. |
| `get_splitwise_balances` | Read-only. Who owes the user and whom they owe, from Splitwise's own balances: totals per currency, each friend (by group), and who pays whom in each group. Optionally one group only. |
| `list_splitwise_expenses` | Read-only. Expense history, optionally for one group and/or one friend: each expense's shares, who added it, whether it's a payment, and how it moved your balance with each person. With a friend, it sums those effects, which explains how a balance adds up. |
| `preview_splitwise_settlement` | Read-only. Shows a settle-up payment (they paid you, or you paid them) with the balance before and after, and returns a confirmation code. Refuses a payment larger than what's owed that way, and flags a matching payment from the last 7 days. |
| `record_splitwise_settlement` | Records the previewed payment as a Splitwise settle-up, with the preview's single-use code, and checks what Splitwise saved. |
| `preview_splitwise_deletion` | Read-only. Shows exactly which expense would be deleted and returns a confirmation code. Refuses expenses the user didn't add, settle-up payments, and already-deleted ones. |
| `delete_splitwise_expense` | Deletes one expense the user added, with the preview's code: single-use, 30 minutes, and refused if the expense was edited after the preview. Checks afterwards that Splitwise really deleted it. |
| `add_expense_to_splitwise` | Records the previewed expense: the payer's paid share is the full cost, and everyone's owed share is their exact total. Needs the preview's confirmation code. Refuses if anything differs from the preview, a person doesn't check out against the user's contacts, or it looks like a duplicate (unless `allow_duplicate` is set). |

The Splitwise tools only appear when Splitwise is configured (see [Splitwise sign-in](#splitwise-sign-in-multi-user)).

## Allocation rules

- **Money** is `Decimal` at the edges and exact `Fraction` internally. Rounding happens only at defined boundaries: each item, each discount, the tax, the tip, and each fee. Every component's shares add up exactly to that component's amount, so everyone's totals add up exactly to the printed total.
- **Items** are split by explicit weights. A multi-person item with no weights is rejected unless `split: "equal"` is set, which should only be done when the user said it's shared equally.
- **Discounts** are split by the subtotals of the items they cover (`applies_to`, or the whole receipt) by default. `equal` and `custom` allocations are also available.
- **Tax** is split by each person's taxable amount after discounts. Items with `taxable: false` (e.g. exempt groceries) are left out of the tax base.
- **Tip** is `proportional` by default: split by each person's tip-eligible pre-tax items (`basis: "net"` after discounts, or `"gross"` before them). `equal` and `custom` are also available.
- **Fees** have no default policy. A fee without `policy` is reported as a problem.
- **Leftover cents** use the largest-remainder method. Ties go by person name, ascending by Unicode code point, so the result doesn't depend on input order.
- **Reconciliation** is separate from allocation. An unexplained difference is reported and blocks the split. It is never assigned to a person. If the user confirms a real extra charge, add it as a fee with a policy.

### Example input

```json
{
  "receipt": {
    "people": ["ann", "bob"],
    "items": [
      {"id": "pizza", "amount": "24.00", "split": "equal",
       "shares": [{"person": "ann"}, {"person": "bob"}]},
      {"id": "salad", "amount": "9.00", "shares": [{"person": "ann"}]},
      {"id": "milk",  "amount": "3.50", "taxable": false, "shares": [{"person": "bob"}]}
    ],
    "discounts": [{"id": "coupon", "amount": "4.00", "applies_to": ["pizza"]}],
    "tax":  {"amount": "2.32"},
    "tip":  {"amount": "6.00"},
    "fees": [{"id": "delivery", "amount": "3.99", "policy": "equal"}],
    "printed_subtotal": "36.50",
    "printed_total": "44.81"
  }
}
```

Send money as strings so no float ever touches an amount. `minor_unit_digits` (default 2) supports currencies like JPY (`0`).

## Duplicate protection

Each expense the server adds gets a fingerprint line (`ref: swmcp-…`) in its notes. The fingerprint covers the group, purchase date, currency, total, and which Splitwise users owe how much. It ignores receipt names, item ids, the description, and who paid, so a re-parsed copy of the same receipt still matches. Before adding an expense, the server checks expenses added or changed in the last 30 days (up to 1,000 of them) for the same fingerprint, skipping deleted ones. If it finds a match, it refuses and names the existing expense. Pass `allow_duplicate: true` once the user confirms the expense really is separate, e.g. the same order on the same day.

Two adds racing on the same preview can't both post: each confirmation code is single-use.

## Known limits

- Expenses created in the Splitwise app (no `ref` line) aren't detected as duplicates.
- One payer per expense; "I paid $30 and Bob paid $20" isn't supported.
- Someone outside the target group is refused rather than added to it; add them to the group in Splitwise first. A new person (an email Splitwise doesn't know) can only be added to an expense outside any group, because Splitwise leaves new people out of group expenses.
- Name lookup ignores case, accents, and spacing, but not nicknames ("Mike" won't find "Michael"); the AI asks instead.
- No editing expenses, and deleting is limited to expenses you added (not other people's, not settle-up payments), one at a time. Deleted expenses can be restored in the Splitwise app; this server has no restore.
- The server makes the preview mandatory, but can't force the AI to show it; the client's own approval prompt for the add tool is the backstop.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install ".[dev]"
.venv/bin/pytest
```

Re-run the `pip install` after changing source to update the installed server (tests run from `src/` directly). An editable install (`-e`) can break on macOS with Python 3.13, because the `.pth` file it writes may get the `hidden` flag, and Python then skips it. Keep the venv out of iCloud-synced folders such as `~/Documents`: iCloud can offload its files, and imports then hang.

Claude Code:

```bash
claude mcp add splitwise -- /path/to/splitwise-mcp-server/.venv/bin/splitwise-mcp-server
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "splitwise": {
      "command": "/path/to/splitwise-mcp-server/.venv/bin/splitwise-mcp-server"
    }
  }
}
```

**Local use with your own account:** in stdio mode, set `SPLITWISE_API_KEY` to a personal API key (from your app's page at secure.splitwise.com/apps) to enable the Splitwise tools. The HTTP server ignores this variable. To keep the key out of client config, put it in `.env` (git-ignored) and point the client at `scripts/run-local.sh`, which loads `.env` and starts the server.

## Remote (HTTP) mode

```bash
splitwise-mcp-server --http --port 8000   # MCP endpoint: http://localhost:8000/mcp
```

HTTP mode is used automatically whenever `$PORT` is set. It binds `0.0.0.0`, runs stateless, and serves `GET /health` for health checks.

**Railway:** `railway.json` sets the start command (`python main.py --http`, which runs from source with no install step) and the health check. `requirements.txt` lists the runtime dependencies, and `.python-version` pins Python. Keep `requirements.txt` in sync with `pyproject.toml`. After a deploy, generate a public domain under the service's **Settings → Networking**. The connector URL is `https://<your-domain>/mcp`. Use that URL for Claude custom connectors and ChatGPT developer-mode connectors.

Without the Splitwise settings below, the HTTP endpoint has no authentication and serves only the calculation tools. With them, Claude and ChatGPT run the sign-in flow automatically when the connector is added.

## AWS Lambda

The production deployment runs on AWS Lambda, defined as infrastructure as code in [`template.yaml`](template.yaml) (AWS SAM / CloudFormation). At personal usage it stays within the AWS free tier, and it sleeps when idle (a cold start takes about 3 s).

```
claude.ai / ChatGPT ──HTTPS──▶ Lambda function URL ──▶ Lambda Web Adapter ──▶ this server (uvicorn, :8080)
                                                                               │
                                         SSM Parameter Store (secrets) ◀───────┤ at startup
                                         Upstash Redis (OAuth + preview state) ◀┘ per request
```

What the template defines:

| Resource | Purpose |
|---|---|
| `ServerFunction` | Python 3.13 on arm64, 1 GB memory, 30 s timeout, with the [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter) layer, which starts `deploy/lambda/run.sh`, waits for `/health`, and forwards each request to the server. `MCP_JSON_RESPONSE=1` makes the server answer with plain JSON instead of an event stream, which buffered function URLs need. |
| Function URL | Public HTTPS endpoint (`AuthType: NONE`, buffered). The app does its own OAuth: `/mcp` without a valid token gets 401. |
| IAM role (inline policy) | Least privilege: read only this app's parameters under `/splitwise-mcp/`. |
| `PublicUrlParameter` | Publishes the function URL as `/splitwise-mcp/PUBLIC_URL`, the OAuth issuer, which the server loads at startup. Using a parameter rather than an environment variable avoids a function → URL → function dependency cycle. |
| `ServerLogs` | CloudWatch log group, kept 14 days. Each tool call is logged by name, outcome, and duration, never its arguments or results. |

**Secrets** are encrypted SecureString parameters under `/splitwise-mcp/`, loaded at startup (`SSM_PARAMETER_PATH`), so none sits in the function's configuration. CloudFormation can't create SecureStrings, so `deploy/secrets.sh` stores them: `SPLITWISE_CLIENT_ID`, `SPLITWISE_CLIENT_SECRET`, and `REDIS_URL` from `.env`, plus a generated `SERVER_SECRET`, created once and never replaced. **Redis** is [Upstash](https://upstash.com)'s free tier (use the `rediss://` URL, in the same region).

Deploy (needs the AWS CLI signed in, e.g. `aws login`, and the [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)):

```bash
deploy/secrets.sh        # once, or after changing a secret
deploy/lambda/build.sh   # package the server with Linux arm64 wheels into build/lambda/pkg
sam deploy               # create or update the stack (settings in samconfig.toml)
sam delete               # remove everything the stack created
```

`sam deploy` prints the connector URL and the Splitwise callback URL. Set a zero-spend AWS budget alert; new accounts often can't cap the function's concurrency.

**After adding a tool, refresh the connector.** Claude and ChatGPT keep the tool list from when the connector was added. In ChatGPT, refresh it in Settings → Apps & Connectors, or remove and re-add it.

## Splitwise sign-in (multi-user)

When configured, everyone who adds the connector signs in with their own Splitwise account. The server is an OAuth authorization server for MCP clients (dynamic client registration + PKCE) and hands the actual login to Splitwise. Before sending users to Splitwise, it shows a consent page naming the requesting app and where the user will be redirected. All tools then require sign-in.

Splitwise requires an active Splitwise Pro subscription to register an app and use its API. Only the account that registers the app needs it: an account without Pro has been tested signing in through the app, reading its balances, and adding expenses. If the registering account's Pro lapses, the app (and so every user's connection) may stop working.

1. Register an app at https://secure.splitwise.com/apps and set its callback URL to `https://<your-domain>/oauth/splitwise/callback`.
2. Set these environment variables. On AWS Lambda, `deploy/secrets.sh` stores them as encrypted SSM parameters and the SAM template sets `PUBLIC_URL` (see [AWS Lambda](#aws-lambda)); on Railway, use service → **Variables**.

| Variable | Value |
|---|---|
| `PUBLIC_URL` | `https://<your-domain>` (no trailing slash) |
| `SPLITWISE_CLIENT_ID` | The app's consumer key |
| `SPLITWISE_CLIENT_SECRET` | The app's consumer secret |
| `SERVER_SECRET` | A random string of 32+ characters, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `REDIS_URL` | Optional but recommended. On Railway, add a Redis service and set this to `${{Redis.REDIS_URL}}`. |
| `WRITE_QUOTA` | Optional. Changes to Splitwise (adds, deletes, settle-ups) each user may make, ever. Unset = no limit. Counted per Splitwise user id, in Redis. |
| `QUOTA_EXEMPT_USER_IDS` | Optional. Comma-separated Splitwise user ids with no limit (e.g. yours). |
| `QUOTA_CONTACT` | Optional. Where users who hit the limit can ask for more; the AI shows it to them. |

Client registrations, sign-in state, and tokens are encrypted with `SERVER_SECRET` and handed to the client. Each user's Splitwise token exists only inside their own encrypted token. Changing `SERVER_SECRET` signs everyone out. Access tokens last 24 hours and refresh tokens 90 days.

The only server-side state is what OAuth 2.1 requires remembering, and each entry expires with the token it describes:

- **Authorization codes are single-use.** A second exchange fails and revokes everything issued from that sign-in.
- **Refresh tokens rotate.** Each refresh returns a new pair and retires the old refresh token. If a retired one is used again, it must have leaked, so the whole sign-in is revoked: the thief and the user are both signed out, and the user signs in again.
- **Revocation** (`POST /revoke`, RFC 7009) signs out the sign-in that the given token belongs to. Because of an MCP SDK bug, a public client has to send an empty `client_secret` field (see `tests/test_oauth.py`).

That state lives in Redis when `REDIS_URL` is set. Otherwise it's kept in memory, which is only correct for a single instance and is forgotten on restart; the server logs a warning in that case. Users can also revoke the server's access from their Splitwise account settings.

## Tests

```bash
.venv/bin/pytest
```

The suite covers rounding (ties, input order, negative amounts, zero-decimal currencies), splitting and reconciliation, each MCP tool through a real MCP client, the Splitwise integration against a fake API (name matching with ambiguous and missing names, refused mappings, previews, single-use confirmation codes including racing adds, exact posted shares, checking what Splitwise saved, duplicates, pagination, error handling, balances per currency and per group, deleting only your own expenses behind a single-use preview, settlements with overpayment and double-record checks, balance explanations from expense history), tool-call logging (names only, never arguments), loading secrets from SSM, and the full OAuth flow (public and confidential clients, code reuse, refresh rotation and replay, concurrent refreshes, revocation, forged state, consent page escaping).

## License

[MIT](LICENSE) © 2026 Chethan Gowda. Splitwise is a trademark of Splitwise, Inc.; this project is independent and not affiliated with or endorsed by Splitwise.
