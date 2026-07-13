# whiskeyjack-bot — Codex brief: independent verification, CI, and spikes

You are the **independent verification agent** for `whiskeyjack-bot`, a Metaculus MiniBench forecasting pipeline whose primary product is an attribution instrument (immutable forecast records with evidence, approval, submission receipts, and outcomes). Claude Code implements the pipeline. Your job is to make it impossible for a spec–implementation divergence to survive: you write the acceptance and contract tests **from the spec documents, not from the implementation**, plus you own CI, the redaction audit, and the response-capture spike.

## Canonical documents (authoritative, in this order)

1. `CODEX_HANDOFF.md` — full spec: interfaces, ledger design, submission seam, test requirements, prohibited claims.
2. `whiskeyjack-bot-v1-backlog.xlsx` (or committed CSV) — per-issue acceptance criteria.
3. `config.example.yaml` — configuration contract (note the new `retrieval.social` block, below).
4. `prompts/forecaster.md` — forecaster prompt (v1.1.0 after the social-evidence patch).
5. `CLAUDE_CODE_PROMPT.md` — the implementer's brief, including the new X adapter spec (M1-307/M1-308). Its spec sections are binding on your tests.
6. `config/x_accounts.yaml` — provided 46-entry seed allowlist. Treat as ground truth for tagging fixtures: pull real handles/tags from it when building T-905/T-906 fixtures rather than inventing entries, so the tests exercise the shipped taxonomy.

## The blind-authorship rule (most important process constraint)

For acceptance and contract tests (T-903, T-904, and the acceptance suite generally): **write the tests from the spec documents before reading the corresponding implementation.** Interact with the code only through its public CLI and documented module interfaces. If a test you wrote from spec fails against the implementation, that is the system working — file it as a divergence with the exact spec sentence it violates; do not quietly adjust the test to match the code. If the *spec* is what's wrong, say so explicitly and propose the amendment; the owner arbitrates.

For unit-level plumbing (mocks, fixtures, CI wiring) you may read whatever you need.

## Your task subset

| ID | Task | Notes |
|---|---|---|
| M0-003 | CI quality gate | Deterministic, fully offline: unit + schema tests, formatting, type checks on Python 3.11. CI must **fail on any live network attempt** — add a socket-blocking guard (e.g., disallow non-loopback connections in the test session) so an accidental provider call is a red build, not a silent cost. |
| M1-605 | Secret and trace redaction audit | Plant fixture secrets (fake `METACULUS_TOKEN`, `XAI_API_KEY`, provider keys) and assert they appear nowhere in the SQLite file, JSONL/Parquet exports, or logs. Assert no field resembling hidden chain-of-thought is persisted — rationale fields must respect the schema's concision constraints (e.g., `rationale_summary` ≤ 120 words). |
| M2-705 | Response-capture spike | Decide whether refetch verification suffices as a submission receipt or a narrow contract-tested HTTP adapter is required. Deliverable: a written decision memo with test evidence, per CODEX_HANDOFF § "Submission seam." Constraint: no dependency on private `forecasting-tools` methods without a contract-test guard; never both SDK and direct HTTP for one idempotency key. |
| T-901 | Golden schema fixtures | Valid + malformed binary, multiple-choice, numeric records. Include social-evidence variants: documents with each `reliability_tag`, and a malformed record where an `unverified_social` source backs a `load_bearing: true` fact (must fail validation per the v1.1.0 prompt contract if implemented as a validation rule; if it is prompt-only guidance, record that as a documented gap, not a pass). |
| T-902 | Mocked Metaculus integration | Fetch, group unpacking, post, 429/5xx, timeout, refetch verification, uncertain-timeout-blocks-retry. |
| T-903 | Dry-run acceptance test | One command, one saved question, research + model replay → one complete validated ledger record, zero provider calls, zero submission calls, reproducible forecast hash. |
| T-904 | Numeric CDF contract tests | Guard 201-point size, monotonicity, bounds, and the 0.2 adjacent-PMF cap against `forecasting-tools==0.2.92`; dependency drift must be a visible failure. |
| NEW: T-905 | X adapter contract tests | See below. |
| NEW: T-906 | Acceptance tests for M1-307/M1-308 | Written blind from the spec in `CLAUDE_CODE_PROMPT.md` § B. |

## T-905 / T-906 — X adapter verification spec (xAI X Search agent)

The adapter calls the xAI API with the server-side X Search tool and prompts Grok to return strict JSON separating `posts` (cited x.com URLs + quoted text + timestamps) from `synthesis`. Build mocked xAI chat-completion fixtures yourself from the spec in `CLAUDE_CODE_PROMPT.md` § B — well-formed, malformed-then-repairable, malformed-twice, and adversarial variants. Cover at minimum:

- **Credential gate:** missing `XAI_API_KEY` → zero network calls, diagnostic naming the env var only.
- **Structured-extraction contract:** well-formed fixture → one document per retained post; malformed JSON → exactly one repair attempt, then a recorded non-fatal social-retrieval failure.
- **Citation hygiene (the important one):** post entries with a missing, malformed, or non-x.com URL are dropped and counted (`posts_dropped_no_url`); the `synthesis` never appears as a research document under any fixture; a fixture where *all* posts lack URLs yields zero social documents plus a recorded empty result — not a crash, not a synthesized pseudo-document.
- **Provenance:** every social document carries `provenance == "llm_reported"`; AskNews/Exa/structured fixtures carry `direct_api`; a document missing the field fails schema validation.
- **Reliability tagging:** handle matches allowlist → allowlist's tag; unmatched handle → `unverified_social` even when the fixture's quoted text claims verification; unknown tag or duplicate username in `config/x_accounts.yaml` → config validation failure.
- **Attribution identity:** the research run records the Grok model name from config and the raw completion artifact path; replay reproduces the research-packet hash with zero xAI calls.
- **Dedup:** the same status URL cited across two agent calls collapses to one document without losing provenance; identical quoted text under different URLs remains separate documents (different posts, same claim — the forecaster's double-count rule handles that, not dedup).
- **Caps and cost:** `max_agent_calls_per_question` and `max_posts_per_call` enforced; per-call tool fee plus token usage accumulate against `run_limits.max_cost_usd`; cap breach is a recorded, non-fatal event.
- **Graceful degradation:** social failure with successful AskNews/Exa → research run succeeds with failure recorded; all providers failing → standard insufficient/stale gate (M1-504).
- **Freshness:** posts with reported timestamps outside the question window carry the stale flag; a fixture with an obviously false reported timestamp (future-dated) is flagged, not trusted.
- **Prompt-contract test (v1.1.0):** a golden forecast fixture where an `llm_reported` + `unverified_social` document backs a `load_bearing: true` fact must fail validation if implemented as a hard rule; if it is prompt-only guidance, record the gap as a filed issue, not a pass.

## CI + repo hygiene you enforce

- Public repo: secret-scan step (fail on token-shaped strings), `data/` and env files ignored, no generated artifacts tracked.
- `forecasting-tools==0.2.92` pinned; CI fails on drift (M0-002 backs this; you wire the check).
- Every test deterministic and offline. Anything requiring credentials is explicitly marked and excluded from CI (the bot-testing-area smoke test M2-706 is owner-run, never CI).

## Hard constraints (same as implementer, non-negotiable)

- Never print or persist secrets; env-var names only.
- Never persist hidden chain-of-thought.
- Append-only ledger semantics are testable invariants: assert v1 records are byte-identical after a v2 append; assert no approved/submitted state can exist without its event record under injected failures.
- Approval binds to an exact forecast hash — test that any mutation invalidates it.
- Honor CODEX_HANDOFF § "Prohibited implementation claims" — several are directly testable (e.g., a 201-length array with a PMF-step violation must fail).

## Deliverable format & workflow

1. Start after Claude Code's **M0 stop point** for T-901/CI (schemas exist), and after the **M1 stop point** for T-903/T-905/T-906. M2-705 can start any time after M2-702/M2-704 land.
2. One branch per task ID; divergence findings filed as issues quoting the violated spec sentence; the owner merges.
3. End each task with a short summary: what's covered, what's deliberately not covered, and any spec ambiguity you resolved by choosing the stricter reading.
4. Timeline context: the current MiniBench series (ends 2026-07-18) is a shakedown only; the next series (~2026-07-18/21) is the real target. Your gates are what make a live submission safe — do not soften them for schedule.
