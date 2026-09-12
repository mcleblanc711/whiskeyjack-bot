# X (xAI) retrieval vs a higher AskNews tier

**Date:** 2026-09-11. **Question:** which is the practical next move for MiniBench retrieval?
**Answer:** neither, in the form asked. Move AskNews to **Pro ($7.99/mo)** if volume clears
~53 questions/month, do **not** build X retrieval for MiniBench, and fix the settlement defect
that is the actual reason retrieval looks expensive.

Every per-question number below is measured from the live ledger
(`data/whiskeyjack_bot.sqlite3`, 24 questions, MiniBench project 33122), not estimated. Pricing
was read from the vendors' own pages on 2026-09-11 and is quoted with its units.

---

## 1. What a question actually costs today

| Component | Measured usage | Charged | Share |
| --- | --- | --- | --- |
| **AskNews** | 6 credits (1 news + 5 historical) | **$0.1500** | **74%** |
| Model (GPT-6 Astra via OpenRouter) | 31 calls / 24 questions | $0.0411 | 20% |
| Exa | 2 calls @ $0.00613 actual | $0.0123 | 6% |
| **Total** | | **$0.2033** | |

The total reconciles exactly with the live budget: 24 × $0.2033 = $4.88, and the activation shows
$40.00 − $35.12 = $4.88 consumed.

**AskNews costs 3.6× the forecasting model and 12× Exa.** That is the finding the rest of this
document turns on, and it is not what it looks like.

### Why AskNews is charged at estimate and Exa at actual

`Budget.settle` returns without writing when `actual is None` (`tournament_state.py:376`). Exa's
adapter passes an actual — `complete_call(call_scope, body, call_cost)` (`exa.py:709`) — because
Exa's response body carries `costDollars.total`. AskNews's adapter passes none —
`complete_call(call_scope, raw)` (`asknews.py:352`).

It is not an oversight in the adapter. **AskNews's `SearchResponse.usage` carries `credits: int`
and no dollar figure at all**, so there is nothing to settle *with* unless something converts
credits to dollars at the subscription rate. Across all 48 live calls: 48 reservations, **0
settlements**, `hit_cache` false every time.

So the reservation estimate *is* the charge, permanently. And the estimates
(`asknews.py:324-325`: `0.125` historical, `0.025` news) are 5 credits and 1 credit at
**$0.025/credit — exactly the Pay-as-You-Go overage rate.** The pipeline is, in effect, hard-coded
to the cheapest-per-month and most-expensive-per-credit tier.

*(Inference to confirm with the vendor account: the exact match to $0.025/credit says the account
is on Pay as You Go. Worth checking before acting on §2.)*

---

## 2. A higher AskNews tier

Measured consumption is **6 credits/question**. Published plans and overage rates:

| Tier | $/month | Credits included | Overage | Marginal $/question | Questions covered by included credits |
| --- | --- | --- | --- | --- | --- |
| **Pay as You Go** (current) | $0 | 25 | $0.025/cr | **$0.150** | 4 |
| **Pro** | $7.99 | 500 | $0.020/cr | **$0.120** | 83 |
| Spelunker | $250 | 20,000 | $0.015/cr | $0.090 | 3,333 |
| Analyst | $1,000 | 110,000 | $0.010/cr | $0.060 | 18,333 |

**Break-evens at 6 credits/question:**

- **Pro beats Pay-as-You-Go above ~320 credits/month ≈ 53 questions/month.** Below that the
  $7.99 subscription costs more than metered credits.
- **Spelunker beats Pro above ~12,600 credits/month ≈ 2,100 questions/month.** MiniBench is
  nowhere near this. At the observed 24 questions, Spelunker would cost $250 to replace $3.60 of
  usage.

**Recommendation: Pro, and only Pro.** The saving is real but small — about $0.03/question, or
$6 across 200 questions — and the honest reason to take it is the 2× rate limit and the headroom
against another quota exhaustion, not the unit price.

### The bigger lever is not the tier

Two things are worth more than the tier upgrade:

1. **Recalibrate the estimates when the tier changes.** `0.125`/`0.025` encode $0.025/credit. On
   Pro they should be `0.10`/`0.02`. Left alone, the pipeline charges its own budget 25% more than
   the account is billed, and the $40 cap binds early on money that was never spent. These are
   code constants, not config, so changing them does **not** retire the live activation.
2. **Settle AskNews from `usage.credits`.** Every response already carries the credit count. A
   settlement path that multiplies credits by a configured per-credit rate would replace a
   permanent hold with a true actual, which is what the ledger is supposed to record. Worth a
   backlog item; see §5.

### Do not cut the historical call to save money

83% of AskNews spend is the single 5-credit `historical` strategy call. Cutting it is the obvious
saving and it is the wrong move — measured across the 24 live questions:

| Strategy | Credits | Calls | Documents | Unique URLs | URLs it alone found |
| --- | --- | --- | --- | --- | --- |
| `historical` | 5 | 24 | 180 | 147 | **124** |
| `news` | 1 | 24 | 177 | 146 | 123 |

Only **23 URLs overlap**. The expensive call is not buying a more thorough version of the cheap
call's answer; it is buying a mostly disjoint evidence set. Dropping it saves $0.125/question and
loses 84% of its documents.

---

## 3. X retrieval (xAI X Search)

### The pricing changes in ten days, by 25×

From [docs.x.ai/developers/pricing](https://docs.x.ai/developers/pricing), verbatim:

> Starting September 21, 2026 at 12:00 PM PT, X Search is billed at **$5 per 1k posts fetched**
> and **$10 per 1k user profiles fetched**, replacing the current **$5 per 1k calls**.

Against the committed config (`retrieval.social`: `max_agent_calls_per_question: 2`,
`max_posts_per_call: 25`):

| | Tool fee | + Grok tokens (est.) | Total/question |
| --- | --- | --- | --- |
| **Now → Sept 21** | 2 calls × $0.005 = **$0.010** | ~$0.06 | ~$0.07 |
| **From Sept 21** | 50 posts × $0.005 = **$0.250** | ~$0.06 | **~$0.31** |

The token figure is an **estimate, not a measurement** — no `XAI_API_KEY` is provisioned, so
nothing here was executed. It assumes grok-4.3 ($1.25/M in, $2.50/M out), ~20k input tokens for
50 posts plus the prompt, and ~2k output for the strict-JSON synthesis, across 2 calls.

**After Sept 21, X retrieval alone would cost more than the entire current pipeline**
($0.31 vs $0.203/question). At the $40 cap and $35.12 remaining, adding it takes the remaining
run from ~173 questions to ~69.

### Two things this invalidates in the existing scope

Both are in the committed tree and both are wrong as of Sept 21:

- **`config/tournament.yaml` sets `est_cost_per_tool_call_usd: 0.005`.** Correct today, wrong by
  25× in ten days, because the billable unit stops being the call.
- **`CLAUDE_CODE_PROMPT.md` § M1-307 specifies** "cost accounting = tool-call fee estimate + token
  usage from the API response". Per *call*. Under the new model the fee is driven by posts
  returned — a number the adapter only learns from the response — so the reservation cannot be
  computed from the call count at all. It has to reserve `max_posts_per_call × price` and settle
  down, which is a different shape from what the spec describes.

M1-307 is already blocked on **A-1106** (provision `XAI_API_KEY`, record X Search pricing). A-1106
should now also cover re-specifying the cost accounting, not just recording a number.

### Effort, and the calendar

| | AskNews tier | X retrieval |
| --- | --- | --- |
| Work | Vendor plan change; recalibrate 2 constants | New adapter (M1-307), `provenance` field added to the M1-301 schema and **backfilled across AskNews/Exa/structured**, allowlist handling, citation-hygiene drops, artifact replay, cost accounting, plus a full review cycle |
| Blocked on | Nothing | A-1106 (`XAI_API_KEY`, pricing) |
| Realistic | Same day | Multiple days plus review rounds |

MiniBench resolves around **2026-09-25**. X Search's price model changes **Sept 21**. Even if the
adapter landed immediately, it would run at the cheap rate for a few days and at 25× for the rest
of the tournament.

---

## 4. What each adds evidentially

Measured yield, 24 live questions:

| Provider | Calls | Docs/call | Empty calls |
| --- | --- | --- | --- |
| AskNews | 48 | 7.44 | 0 (0%) |
| Exa | 48 | 6.44 | 6 (12%) |

Both run on **every** question — Exa is nominally the fallback, but `official_source_required`
means a named source sends every question to it, which is intended behaviour and not a failure
signal (the signal to watch is `primary_provider_failed` in the reason list).

For **MiniBench-style short-horizon questions**, the qualitative case splits cleanly:

- **A higher AskNews tier adds no new evidence at all.** Same corpus, same 6 credits, same
  documents — it is purely a price and rate-limit change. Anything framed as "better retrieval
  through a higher tier" is not supported: the tiers differ in quota, overage rate and RPS, not
  in what the search returns.
- **X adds a genuinely different source class** — primary-source posts, official accounts, and
  breaking signal that news aggregation lags. That is real value for short-fuse questions where
  the resolving event is announced on X before it is written up.
- **But X's evidence is the weakest kind this pipeline handles.** M1-307's own spec sets
  `provenance = llm_reported` (against `direct_api` for every other adapter) because Grok returns
  a *synthesis with citations*, not raw posts. The spec then has to drop any post without a
  resolvable status URL and count `posts_dropped_no_url`, precisely because the citations can be
  fabricated. Paying 25× more per question for the only evidence class that needs a
  hallucinated-citation defense is a poor trade while cheaper, `direct_api` evidence is not yet
  exhausted.

---

## 5. Recommendation

1. **Do not build X retrieval for MiniBench.** The price model changes four days before the
   tournament resolves, at 25× the rate the config assumes, and the adapter is a multi-day build
   plus review. Keep M1-307 blocked on A-1106, and widen A-1106 to re-specify cost accounting
   for the per-post model rather than just recording a price.
2. **Move AskNews to Pro ($7.99/mo) once volume clears ~53 questions/month.** Take it for the 2×
   rate limit and quota headroom; the unit-price saving is ~$0.03/question. **Not Spelunker** —
   it needs 2,100 questions/month to beat Pro, roughly 90× observed volume.
3. **When the tier changes, recalibrate `asknews.py:324-325` to the new per-credit rate.**
   Otherwise the internal budget over-charges by 25% against a $40 cap.
4. **File the settlement gap.** AskNews returns `usage.credits` on every response; the pipeline
   holds an estimate forever instead. Converting credits to dollars at a configured rate and
   settling would make the ledger record what was actually spent, which is the instrument's whole
   point. This is worth more than the tier decision.
5. **Leave the 5-credit historical call alone.** It is 83% of AskNews spend and it returns 124
   URLs nothing else finds.

### Re-check before acting

- Confirm the account really is on Pay as You Go (inferred from the exact $0.025/credit match,
  not read from the vendor console).
- Re-read [docs.x.ai/developers/pricing](https://docs.x.ai/developers/pricing) after Sept 21;
  this note is written against an announcement, and announcements slip.
- The Grok token cost in §3 is an estimate with stated assumptions. Nothing was executed against
  the xAI API — no key is provisioned.

## Sources

- [xAI API pricing](https://docs.x.ai/developers/pricing) — read 2026-09-11
- [AskNews plans](https://my.asknews.app/en/plans) — read 2026-09-11
- Live ledger `data/whiskeyjack_bot.sqlite3`, MiniBench project 33122, 24 questions
