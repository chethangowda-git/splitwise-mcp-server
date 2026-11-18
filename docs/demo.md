# Demo walkthrough (ChatGPT)

A two-minute run through everything the connector does: split a receipt, add it to Splitwise, explain a balance, and settle up. Use it to record the demo video, or to try the connector yourself.

## Use it in ChatGPT

ChatGPT runs MCP servers as **connectors** in Developer mode, which is available on paid plans. Menu names may differ slightly between versions.

1. **Settings → Apps & Connectors → Advanced settings →** turn on **Developer mode**.
2. **Apps & Connectors → Create**:
   - **Name:** Splitwise receipt splitter
   - **MCP server URL:** `https://xdic6hbarjoa6btpgkc6fc3jwu0oaxor.lambda-url.us-east-1.on.aws/mcp` (or, if you host your own, the `ConnectorUrl` that `sam deploy` prints)
   - **Authentication:** OAuth
3. Click **Connect**. A consent page names ChatGPT and where you'll be sent; approve it, then sign in to Splitwise and allow access.
4. In a chat, turn the connector on from the **+** (tools) menu. ChatGPT asks before running any tool that changes data. That's on top of the server's own preview step.

**After the server gains new tools,** ChatGPT keeps the old tool list. Refresh the connector, or delete it and create it again, and start a new chat.

Any account can sign in. Only the account that registered the Splitwise app needs Splitwise Pro.

## Before you record

- Sign in once beforehand, so the video doesn't open on the login screens.
- Use a group made for the demo, with one friend who knows it's a demo. **They get real Splitwise notifications.** This script uses a group called `testing` and a friend called Sam; use your own.
- Hide anything personal: other chats, email addresses, the friends list, other groups.
- Have the Splitwise app or website open next to ChatGPT, on the `testing` group.

## The receipt

Screenshot this to use as the photo:

```
        LUIGI'S PIZZERIA
     Order #4417 · Delivery

 Margherita pizza          18.00
 Garlic knots               6.00
 Caesar salad              11.50
 Craft beer                 7.50
                         -------
 Subtotal                  43.00
 Tax                        3.44
 Tip                        7.74
 Delivery fee               2.99
                         =======
 TOTAL                     57.17
```

## Shot list

The amounts assume the AI calls you "Me" (or your name) and your friend "Sam". With other names, a cent may move between you, because rounding ties go alphabetically. The total is always $57.17.

| # | Time | You type | What should happen | Point it makes |
|---|---|---|---|---|
| 1 | 0:00 | *(attach the receipt)* "Split this with Sam. We shared the pizza and knots equally, the salad was mine, the beer was his." | ChatGPT calls `reconcile_receipt`. It says the receipt adds up to $57.17 and asks how to share the $2.99 delivery fee. | It checks the math first, and **asks instead of guessing**. |
| 2 | 0:15 | "Equally." | `split_receipt`: **you $31.11 · Sam $26.06**. | Exact cents. Tax and tip follow what each person ordered. |
| 3 | 0:25 | "I paid. Add it to Splitwise in the testing group as Dinner at Luigi's." | `find_splitwise_people`, then `preview_splitwise_expense`. ChatGPT shows exactly what will be added (real Splitwise names, each share, the group) and asks you to approve. | **It never guesses a person**, and nothing is added without a preview. |
| 4 | 0:40 | "Yes." | `add_expense_to_splitwise` with the preview's one-time code. | The add only works with the approved preview. |
| 5 | 0:45 | *(switch to Splitwise)* | The expense is there: Sam owes $26.06, with the item breakdown in the notes. | It's real, not a mock. |
| 6 | 0:55 | "Add it again." | It reports a duplicate of the expense just added. Nothing is added. | **Safe to retry.** |
| 7 | 1:05 | "How much does Sam owe me in testing, and why?" | `get_splitwise_balances`, then `list_splitwise_expenses` for Sam. The total, then each expense's effect, adding up to it. | Balances are explained line by line, not just stated. |
| 8 | 1:25 | "He just paid me back all of it. Record it." | `preview_splitwise_settlement`: *"Sam paid you … Now: Sam owes you … After: settled up."* | You see the before and after first. |
| 9 | 1:35 | "Yes." | `record_splitwise_settlement`. In Splitwise, a settle-up payment appears and the group shows settled. | Settle-ups get the same checks as expenses. |
| 10 | 1:45 | *(optional)* "Actually the total on the receipt was 58.17." | It reports a $1.00 difference and won't split until that's explained. | A misread is never absorbed into someone's share. |

At step 8, the amount is whatever Sam owes in the group at that point: the dinner's $26.06 plus anything already there. Record a settlement only when the money has really changed hands, or delete the payment afterwards.

## Recording tips

- macOS: **Cmd+Shift+5** → record a selected portion. Put ChatGPT and Splitwise side by side.
- ChatGPT's "confirm this action" prompts are fine to show. They're the human-in-the-loop check.
- Add captions from the "point it makes" column, or say them in a voiceover. Keep it under two minutes.
- To host it: on GitHub, edit the README in the browser and drag the `.mp4` into the editor. GitHub uploads it and inserts a link that plays inline.
- Afterwards, clean up in Splitwise: delete the demo expense and the settle-up payment.
