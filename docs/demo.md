# Demo walkthrough

A 90-second run through everything the connector does. Use it to record the demo video, or to try the connector yourself.

## Before you start

- The server is deployed with Splitwise sign-in configured (see the README), and `/health` responds.
- In Claude: **Settings → Connectors → Add custom connector**, URL `https://<your-domain>/mcp`. Sign in once before recording, so the video doesn't open on the login screens.
- Two Splitwise friends to split with. **They get a real Splitwise notification** when the expense is added, so tell them first, or use a group made for the demo. Delete the expense afterwards.
- Hide anything personal on screen: other chats, email addresses, the friends list.

## The receipt

Print this, or screenshot it to use as the photo:

```
        LUIGI'S PIZZERIA
     Order #4417 · Delivery

 Margherita pizza          18.00
 Caesar salad              11.50
 Garlic knots               6.00
 Lemonade                   4.25
 Craft beer                 7.50
                         -------
 Subtotal                  47.25
 Promo WELCOME5            -5.00
 Tax                        3.75
 Tip                        8.45
 Delivery fee               2.99
                         =======
 TOTAL                     57.44
```

## Shot list

The amounts below use the names Ann (you), Bob, and Cara. With other names, a cent may move between people, because ties go alphabetically. The total is always $57.44.

| # | Time | You type | What should happen | Point it makes |
|---|---|---|---|---|
| 1 | 0:00 | *(attach the receipt)* "Split this between me, Bob, and Cara. Bob and I shared the pizza, he had 2 of the 3 slices. Knots were for everyone, the salad was Cara's, the lemonade mine, the beer Bob's." | Claude reads the receipt and calls `reconcile_receipt`. It says the receipt adds up and asks how to share the delivery fee. | It checks the math first and **asks instead of guessing**. |
| 2 | 0:20 | "Equally." | Claude calls `split_receipt` and shows **Ann $15.11 · Bob $25.79 · Cara $16.54**. | Exact cents; tax and tip follow what each person ordered, after the promo. |
| 3 | 0:35 | "I paid. Add it to Splitwise as Dinner at Luigi's." | Claude calls `list_splitwise_people`, matches the names, confirms, then calls `add_expense_to_splitwise`. | Your own Splitwise account, through sign-in. |
| 4 | 0:50 | *(switch to the Splitwise app or website)* | The expense shows Bob owing $25.79 and Cara $16.54, with the per-person breakdown in the notes. | It's real, not a mock. |
| 5 | 1:05 | "Add it again." | Claude reports it's a duplicate and names the existing expense. Nothing is added. | **Safe to retry.** |
| 6 | 1:15 | *(optional)* "Actually the total was 58.44." | Claude reports a $1.00 difference and won't split until it's explained. | A misread is never absorbed into someone's share. |

## Recording tips

- macOS: **Cmd+Shift+5** → record a selected portion. Record only the chat window.
- If Claude asks to confirm each tool call, that's fine to show. It's the human-in-the-loop check.
- Add captions for the "point it makes" column, or say it in a voiceover. Keep it under 2 minutes.
- To host it: on GitHub, edit the README in the browser and drag the `.mp4` into the editor. GitHub uploads it and inserts a link. Put it where the README's demo comment is.
- Afterwards, delete the demo expense in Splitwise.
