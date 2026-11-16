# How it was built

An engineering write-up: what the connector does, the decisions behind it, and what testing against the real Splitwise API turned up.

## The problem

Splitting a restaurant bill fairly is fiddly: shared dishes, a promo, tax, tip, and a delivery fee, each divided differently, to the cent. Splitwise records the result but doesn't work it out for you. A chat AI can read a receipt photo and ask who had what, but it isn't trustworthy with arithmetic or with writing to someone's real account.

So the work is divided. **The AI does the fuzzy part:** it reads the receipt and talks to the user. **This server does the exact part:** it checks that the receipt adds up, splits it by explicit rules, and makes every change to Splitwise under its own guards. The two talk over [MCP](https://modelcontextprotocol.io), so the same server works as a ChatGPT or Claude connector.

## Architecture

```
ChatGPT / Claude ──MCP over HTTPS──▶ AWS Lambda function URL
                                      └─ Lambda Web Adapter ─▶ server (Python, uvicorn)
                                                                 ├─ OAuth 2.1 authorization server ─▶ Splitwise login
                                                                 ├─ split engine (exact Fractions)
                                                                 ├─ Splitwise API client
                                                                 ├─ SSM Parameter Store (secrets, at startup)
                                                                 └─ Upstash Redis (single-use codes, revocations)
```

- **Stateless by design.** Each user's Splitwise token lives only inside their own encrypted OAuth token (Fernet, keyed from a server secret). There's no user database. The only shared state is what OAuth requires remembering (used codes, rotated refresh tokens, revocations) plus used confirmation codes. It lives in Redis and expires on its own.
- **Infrastructure as code.** `template.yaml` (AWS SAM / CloudFormation) defines the function, its public URL, a least-privilege role, the log group, and the published issuer URL; `sam deploy` creates or updates it and `sam delete` removes it.
- **Serverless within the free tier.** The unchanged web server runs on Lambda behind AWS's Web Adapter, with plain-JSON MCP responses because buffered function URLs can't stream. At personal usage it stays within the AWS free tier. The cost is a cold start of about 3 s.

## Decisions that matter

| Decision | Why |
|---|---|
| Exact money math: `Decimal` at the edges, `Fraction` inside, largest-remainder rounding with name-ordered ties | Shares always add up to the printed total, and the same input always gives the same split. Rounding each person's total instead gains or loses cents. |
| Reconcile before splitting | A misread line is reported as a difference to fix, never absorbed into someone's share. |
| The server refuses to guess | A shared item needs weights or an explicit "equal". A fee needs a rule. Ambiguity goes back to the user as a question. |
| Name lookup reports match / ambiguous / none | With "two Johns", the AI must ask. A wrong guess would put a real debt on the wrong person. |
| Every person is checked against the user's own contacts | The AI could pass a made-up or wrong user id. The server refuses anyone who isn't the user, a friend, or a member of the target group. |
| Its own OAuth 2.1 server in front of Splitwise | MCP clients need dynamic client registration and PKCE, which Splitwise doesn't offer. The server provides both and hands the actual login to Splitwise. A consent page shows which app is asking, so a crafted link can't quietly take over an account. |

## The safety model for writes

Every action that changes someone's Splitwise follows the same pattern:

1. **Preview.** It's read-only. It shows exactly what will happen (real names, each share, the balance before and after) and returns a confirmation code.
2. **Confirmation code.** It's an HMAC over the exact change and the account, with a random nonce, valid for 30 minutes, and **single-use** (claimed atomically in Redis). Any change after the preview voids it.
3. **Write.**
4. **Verify.** The server re-reads what Splitwise actually saved and reports any difference instead of claiming success.

| Action | Extra rules |
|---|---|
| Add an expense | A duplicate fingerprint in the expense notes, checked against recent expenses, so a retry or re-parse can't double-post. |
| Delete an expense | Only expenses the user added, never settle-up payments. The code covers the expense's last-updated time, so an edit made in the app voids it. |
| Record a settle-up | It can't exceed what's owed in that direction. A matching payment in the last 7 days is flagged as a likely double-record. |

Each rule is **mutation-tested**: switch the check off, and a test fails.

## What testing against the real API found

The fake API passed everything. The real one didn't, every time:

- **Splitwise silently drops a new person from a group expense, and still reports success.** The saved expense covered only half the bill. The fix was to refuse that combination up front, and to verify saved shares after every write, which also guards against quirks not yet found.
- **New people need a name as well as an email.** The API needs `first_name` and `last_name` with the email, which the code wasn't sending. The spec showed this before it broke for real.
- **Confirmation codes collided.** Two previews in the same second produced the same code, so spending one spent the other. Making the codes single-use surfaced this; a per-preview nonce fixed it.
- **A race test that couldn't fail.** The test for concurrent adds passed even with the guard removed, because the fake API serialized the requests. Forcing the two lookups to overlap made it a real test.
- **Settle-up isn't documented.** `payment=true` on `create_expense` isn't in the API docs. A one-cent live test confirmed it works, and showed `repayments`: the exact pairwise effect of each expense, which is now how the connector explains a balance line by line.
- **Deployment details.** A secret was pasted as a whole shell command instead of a URL, so the server failed on start. CloudWatch showed it within a minute. ChatGPT caches a connector's tool list, so new tools only appear after the connector is re-added. Per-tool-call logs (names only, never arguments) made that diagnosable.

Live tests ran only inside a dedicated test group, with small amounts labelled as tests, and were cleaned up afterwards.

## Testing approach

- **111 automated tests** run the MCP tools through a real MCP client, against a fake Splitwise and a full OAuth flow:
  - rounding edge cases
  - reconciliation
  - name matching
  - every refusal path
  - single-use codes, including racing requests
  - duplicates and pagination
  - balances per currency
  - token rotation and replay
  - revocation
- **Mutation checks:** each safety rule was disabled in turn to confirm a test catches it.
- **Live checks** against the real API, for behaviour the documentation doesn't cover.

## Known limits

- **One payer per expense.**
- **No editing.** Delete and re-add instead.
- **Duplicates added by hand aren't caught.** Detection only covers expenses this server added.
- **The preview can't be forced on screen.** The server makes it mandatory, but can't make the AI display it; the client's own approval prompt is the backstop.
- **It depends on the app owner's Pro.** It works for any Splitwise user who signs in, but only while the registering account's Pro is active.
