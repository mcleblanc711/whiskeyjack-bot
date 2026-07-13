# whiskeyjack-bot — Claude Code implementation brief

You are implementing `whiskeyjack-bot`, a Metaculus MiniBench forecasting pipeline whose primary product is an **attribution instrument**: an immutable, replayable record of every forecast, its evidence, and its outcome. Competing is the venue; attribution is the point. When a shortcut would weaken the ledger, approval boundary, or replayability, do not take it.

## Canonical documents (in-repo, read before writing code)

1. `CODEX_HANDOFF.md` — full spec: verified interfaces, ledger design, submission seam, pipeline boundaries, test requirements, prohibited claims. Treat as authoritative except where this brief amends it.
2. `whiskeyjack-bot-v1-backlog.xlsx` (or its committed CSV export) — issue-level acceptance criteria.
3. `config.example.yaml` — configuration contract.
4. `prompts/forecaster.md` — forecaster prompt, committed at v1.0.0; you apply the v1.1.0 patch specified in § B as part of M1-401 (re-hash after).
5. `config/x_accounts.yaml` — provided 46-entry seed allowlist for the social adapter (see M1-308). Do not rewrite its contents; build the loader around it.

This brief **amends** the spec in two ways: (A) a revised owner split, and (B) a new X/Twitter retrieval adapter in v1 scope. Both are below.

## A. Your task subset (revised owner split)

You own the **judgment-heavy seams** — the code where a subtle bug produces a duplicate live post or a corrupted attribution record:

| Area | Issues |
|---|---|
| Foundation | M0-001, M0-002, M0-004, M0-005, M0-006 |
| Metaculus integration | M0-101, M0-102, M0-103, M0-104 |
| Normalization | M1-201, M1-202, M1-203 |
| Retrieval | M1-301, M1-302, M1-303, M1-304, M1-305, M1-306, **M1-307, M1-308 (new, below)** |
| Forecast generation | M1-401 through M1-406 |
| Validation | M1-501, M1-502, M1-503, M1-504 |
| Ledger | M1-601, M1-602, M1-603, M1-604 |
| Submission | M2-701, M2-702, M2-703, M2-704 |
| Resolution/scoring (post-M3) | M4-801, M4-802, M4-803, M5-804 |
| Docs | D-1001, D-1002 |

**Codex owns** (do not implement these; leave clean seams for them): M0-003 (CI), M1-605 (secret/trace redaction audit), M2-705 (response-capture spike), T-901–T-904 (test suites), and independent acceptance-test authorship. Where your work needs a test to proceed, write the minimum unit tests to keep yourself honest; Codex writes the acceptance and contract layers from spec, without reading your implementation. Do not "help" by pre-writing their tests.

## B. New v1 scope: X/Twitter retrieval adapter

### Rationale (context, not code)
X data is a third retrieval provider, not an oracle. Its comparative advantage over AskNews/Exa is (1) **primary-source statements** — official accounts of agencies, companies, teams, and named principals announce load-bearing facts before articles exist — and (2) **breaking-event lead time**, which matters under Metaculus time-averaged scoring. It is **not** an "insider signal" system; unverified accounts are weak evidence and are tagged as such. Access route is the xAI API's server-side X Search tool (direct X API access was rejected on cost). That makes this adapter a **research agent**, not a document fetcher: a second model (Grok) sits in the evidence layer, so its outputs are treated as reported claims with retained citations, never as directly retrieved documents.

### M1-307 — Implement X retrieval adapter (xAI X Search agent)
- **Transport:** xAI API (`https://api.x.ai/v1`, OpenAI-compatible chat completions), API key from env `XAI_API_KEY`, with xAI's server-side **X Search** tool enabled. This replaces direct X API v2 access (rejected on cost) and the hosted X MCP (an agent-protocol layer this headless pipeline doesn't need). At implementation time, verify the current tool name, request parameters (handle filtering, date range, result caps), and per-call price against https://docs.x.ai — these have changed repeatedly; record what you find in the adapter's module docstring with the checked date. Keep the transport behind the same adapter interface as AskNews/Exa.
- **Research-agent contract (critical):** Grok's X Search returns a model synthesis with citations, not raw tweets. The adapter must prompt Grok to return **strict JSON** with two separated parts: (a) `posts`: a list of individual X posts it relied on, each with `url` (x.com status URL), `handle`, `posted_at` (as reported), `quoted_text`, and `relevance_note`; and (b) `synthesis`: its own concise summary. One bounded repair attempt on malformed JSON, then fail the social retrieval (gracefully, see below). Persist the raw completion as a retrieval artifact (same replay contract as M1-306).
- **Citation hygiene:** a post entry without a resolvable `https://x.com/<handle>/status/<id>` URL is **dropped**, and the drop is recorded on the research run (`posts_dropped_no_url` count). This is the hallucinated-citation defense. Never promote the `synthesis` into a document without its backing posts; the synthesis is stored on the research run as agent output, not as a source.
- **Normalization into the M1-301 research-document schema (per retained post):**
  - `canonical_url` = the x.com status URL; `publisher` = `X`; `author` = `@handle`
  - `source_type` = `social`; **new field** `provenance` = `llm_reported` (AskNews/Exa/structured documents set `direct_api`; add this field to the M1-301 schema and backfill the other adapters)
  - `published_at` = reported post time; `retrieved_at` = call time; `content_sha256` over normalized `quoted_text`; dedup by `(canonical_url, content_sha256)` like other providers
  - `reliability_tag` ∈ {`official_primary`, `verified_org`, `journalist`, `unverified_social`} — assigned from the allowlist entry matched on `handle`, else `unverified_social`. Never infer a tag from platform verification badges Grok mentions.
  - Record the Grok model name/version on the research run — a second model now participates in evidence gathering, and attribution requires its identity.
- **Query construction:** at most `max_agent_calls_per_question` calls. Allowlist mode constrains the search to allowlist handles whose domain tags match the question domain (via tool parameters if supported, else via prompt instruction — record which); open mode is keyword search from question text, only when config permits.
- **Freshness:** instruct a date window matching the question's freshness policy; posts reported outside it get the standard stale flag (M1-305). Treat Grok-reported timestamps as claims, not verified facts.
- **Cost/rate safety:** per-run agent-call cap; missing `XAI_API_KEY` fails **before** any paid call; cost accounting = tool-call fee estimate + token usage from the API response, counted against `run_limits.max_cost_usd`.
- **Failure mode:** social retrieval is additive evidence — its failure (network, malformed JSON after repair, zero retained posts) must not fail a research run in which AskNews/Exa succeeded. Record the failure/empty result on the research run.
- **Acceptance:** mocked completions produce normalized documents with correct reliability tags and `provenance: llm_reported`; URL-less posts dropped and counted; synthesis never becomes a document; replay makes zero xAI calls and reproduces the research-packet hash; missing key → no network call; raw completions retained; Grok model identity recorded.

### M1-308 — Account allowlist loader
- The allowlist file `config/x_accounts.yaml` is **provided** (46 curated entries: statistical agencies, central banks, election callers, space/launch, health, weather/climate, sports leagues, AI labs, wire services). It is committed and contains no secrets. Do not edit its entries; if you believe an entry is wrong, flag it for the owner rather than changing it.
- Entry shape: `{username, display_name, reliability_tag, domains: [...], notes?}`. The file header documents the domain taxonomy; treat that taxonomy as the canonical set for question-domain matching.
- Loader validates: username uniqueness (case-insensitive), reliability_tag ∈ the known set, non-empty domains, unknown keys rejected. Any violation fails config validation at startup, not at retrieval time.
- Handle verification against live X is the **owner's** job (A-1106), not yours — the loader validates structure, never network state.
- **Acceptance:** the provided file loads clean; injected duplicate usernames and unknown tags are rejected; domain-tag matching selects the correct account subset for fixture questions in at least two domains (e.g., `econ_data` and `space_launch`).

### Config additions (`config.example.yaml`, under `retrieval:`)
```yaml
  social:
    provider: "xai_x_search"
    enabled: false               # off until XAI_API_KEY exists and pricing verified (A-1106)
    api_key_env: "XAI_API_KEY"
    # Verify current Grok model name at docs.x.ai; per D27, no silent default.
    agent_model: "REPLACE_WITH_VERIFIED_GROK_MODEL_NAME"
    temperature: 0.0
    timeout_seconds: 60
    account_allowlist_path: "config/x_accounts.yaml"
    allow_open_search: true
    max_agent_calls_per_question: 2
    max_posts_per_call: 25
    est_cost_per_tool_call_usd: 0.005   # x_search listed ~USD 5/1k calls; verify at docs.x.ai
```

### Forecaster prompt patch (bump to v1.1.0, re-hash)
Append to the "General rules" section of `prompts/forecaster.md`:
> - Social-media documents carry a `reliability_tag` and a `provenance` field. Treat `official_primary` statements from the account that controls the resolution-relevant fact as primary evidence. Treat `verified_org` and `journalist` as ordinary secondary sources. Treat `unverified_social` as weak, low-diagnosticity evidence: it may justify a `tiny` or `small` adjustment at most, never a load-bearing fact. Multiple unverified accounts repeating one claim remain one piece of evidence.
> - Documents with `provenance: llm_reported` were reported by a research agent, not retrieved directly; their content and timestamps are claims. An `llm_reported` fact may be load-bearing only when the cited account is `official_primary` or the fact is corroborated by a `direct_api` document. Otherwise cap its adjustment at `small` and note the provenance limitation in `uncertainty_notes` if it materially affects the forecast.

### New owner blocker — A-1106 (Chris)
Create an xAI developer account at console.x.ai, generate `XAI_API_KEY`, and confirm current X Search tool pricing (listed around USD 5 per 1,000 tool calls plus tokens; promotional credits may apply). No X API plan and no Grok consumer subscription are required — the developer API is a separate pay-as-you-go product. This blocks only `retrieval.social.enabled: true` and the live smoke test; build fully against mocks in the meantime.

## Hard constraints (repeat of spec, non-negotiable)

- No network calls anywhere until M0-102; no reachable submission path until M2; `submission.enabled: false` and `dry_run: true` remain the committed defaults.
- Never print or persist secrets; env-var names only in diagnostics.
- Never persist hidden chain-of-thought; concise auditable rationale fields only.
- Append-only ledger: forecast versions and lifecycle events are never mutated.
- Approval binds to an exact forecast hash; any content change invalidates it.
- Community prediction is never a forecaster input in v1; snapshot it separately when available.
- Honor every item in CODEX_HANDOFF.md § "Prohibited implementation claims."
- Pin `forecasting-tools==0.2.92`; do not float.

## Workflow

1. Work strictly in dependency order starting **M0-001**. One issue per branch; commit messages reference the issue ID; `pytest` green before an issue is done.
2. **Stop points for owner review: end of M0 and end of M1.** Summarize what was built, what deviated from spec and why, and open questions. Do not proceed past a stop point without explicit go-ahead.
3. If the spec and observed package behaviour conflict (e.g., `forecasting-tools` interface drift), stop, document the discrepancy against the pinned version, and ask — do not silently adapt.
4. If an acceptance criterion is ambiguous, implement the stricter reading and note it.
5. Timeline context: current MiniBench series ends 2026-07-18 and is a **shakedown target only** (bot-testing-area smoke test + at most one live submission, zero score expectations). The next back-to-back series (~2026-07-18/21) is the competitive debut. Correctness of the ledger and submission seam outranks speed.

## Definition of done, Milestone 1

One saved or current question moves fetch → normalize → retrieve (AskNews fixture + X fixture) → forecast → validate → persisted draft record, with: complete metadata; ≥1 source or explicit `insufficient_research`; base rate, prior, adjustments, failure modes, typed forecast; numeric CDF when applicable; zero submission calls; replay reproducing the same forecast hash with zero provider calls; zero secrets in any artifact.
