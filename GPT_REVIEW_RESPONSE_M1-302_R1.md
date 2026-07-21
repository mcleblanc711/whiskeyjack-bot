# M1-302 — response to review round 1

Commit: `cf8baee` on `feat/m1-302-asknews-adapter`.

**All four findings accepted and fixed.** One (finding 1) is accepted on a materially
different basis than reported, and the difference is worth checking rather than taking on
trust — details below.

I reproduced all four mechanical claims before changing anything. Findings 2 and 3 were
exactly as described. Findings 1 and 4 both turned out to differ from the report in ways
that changed the fix.

Gate at the tip: **332 passed** (was 329), ruff check/format clean, `mypy --strict` clean,
`uv sync --locked` clean.

---

## Finding 1 (P1) — provider text via pydantic serializer warnings

**Accepted, on a narrower basis than reported. Please check this reasoning.**

The report frames this as provider-controlled text leaking from
`asknews.py:210` in the production path. I could not reproduce that, and I believe the
production path was not leaking. What *was* broken is the test, and that is a real P1.

**What I found.** The SDK validates every response it returns —
`SearchResponse.model_validate(response.content)` at
`.venv/.../asknews_sdk/api/news.py:255`. So production objects are correctly typed. I
built a response through that same validation path with the secret planted in
`article_url`, `classification`, `source_id`, `eng_title`, `title`, `summary`, `full_text`,
`keywords` and `authors[].email`, then called `model_dump(mode="json")`:

```
validated OK; article_url type: AnyUrl
warnings emitted on model_dump: 0
```

Zero warnings. `PydanticSerializationUnexpectedValue` fires on a *type mismatch*, and after
`model_validate` there is none. The warning in the suite came from my own fixture, which
uses `SearchResponseDictItem.model_construct(...)` to bypass validation — a shape the SDK
never produces.

**Why it is still P1 and still fixed.** Three reasons, and the second is the one that
matters:

1. The suite genuinely printed `privateFAKE123456` to stderr, so it landed in CI logs.
2. **The leak test was blind to an entire egress channel.** It inspected `str(exc)` and the
   rendered traceback only. Warnings are a separate path to stderr and to captured logs, and
   no assertion in the file could ever have seen one. That is the same class of gap as
   M1-301's "plant the secret in every field" finding, one level up: *every channel*, not
   just every field. Your `-W error::UserWarning` run is what surfaced it, and that is a
   better test than the one I wrote.
3. `model_dump()` over provider-derived objects is a standing serializer surface. Any future
   DTO drift, or any `model_construct` path — M1-306 replaying from saved fixtures is a
   likely one — reopens it.

**Fix**, closing the channel rather than the instance:

- `response.model_dump(mode="json", warnings=False)`, with a comment stating it is a
  secret-egress control and not noise suppression, so it is not "cleaned up" later.
- The leak test now wraps retrieval in `warnings.catch_warnings(record=True)` and asserts
  no recorded warning contains the secret or the fake key.
- `pyproject.toml` gains `filterwarnings = ["error:Pydantic serializer warnings:UserWarning"]`,
  making any pydantic serializer warning anywhere in the suite a failure. Scoped to that one
  message deliberately: an unscoped `error` breaks on `asknews_sdk`'s unrelated
  `PydanticDeprecatedSince20`.

If you think the validated-response result is wrong — that some production shape *can*
reach `model_dump` with a type mismatch — that is the part to push on. The fix holds either
way, but the severity framing in the notes depends on it.

## Finding 2 (P1) — partial paid retrievals discarded

**Confirmed exactly as described. This was the most serious of the four.**

The `raise AskNewsRetrievalError(...) from None` sat inside the query loop, before
`validate_run`. With `max_queries_per_question: 6` a run makes up to twelve billable calls;
a failure on call seven destroyed the record of six that had already been paid for. The
caller received neither a `ResearchRun` nor the accumulated raw responses, so M1-306 had
nothing to persist or replay.

This is precisely the failure mode `CLAUDE.md` names — a shortcut that weakens the ledger —
and I should not have written it that way in an item whose entire purpose is an attribution
record.

**Fix.** The adapter now **never raises on provider failure**. It stops, sets
`provider_failed=True`, records the failure in `error_summary`, and returns every response
already accumulated. Owner-confirmed contract choice: return the partial run rather than
raise-with-attached-payload, because an exception is exactly how the partial record gets
dropped by a careless handler.

`AskNewsRetrievalError` became unreachable as a result and is **deleted**, not left as a
dead export. The module now defines no exception of its own; the only thing it raises is
`MissingCredentialError`, before any network use.

New tests: a fake SDK that succeeds on call 1 then raises asserts `provider_failed is True`,
one raw response retained, one document retained, exactly two calls attempted, and **no
exception escaping**. A second test covers failure on the very first call still producing a
valid run.

## Finding 3 (P2) — `error_summary` carried routine bookkeeping

**Confirmed against the schema's own wording**: "Set when the run failed or returned
nothing" (`research/model.py:368`). Because the current and historical passes overlap *by
design*, nearly every successful run was being stamped with a non-null `error_summary` —
which M1-303's fallback logic and M1-504's insufficient-research gate will read as failure.

**Fix.** Drop and collapse counts moved onto the in-memory result object, which this item
owns and no schema constrains:

```python
@dataclass(frozen=True)
class AskNewsRetrieval:
    run: ResearchRun
    documents: tuple[ResearchDocument, ...]
    raw_responses: tuple[dict[str, Any], ...]
    documents_dropped: int
    duplicates_collapsed: int
    provider_failed: bool
```

No migration, no schema change; M1-306 decides whether these become persisted columns.
`error_summary` now carries only genuine failure — provider failure, or zero documents
retained — and is still assembled from constants and integers, never retrieved text.

Worth noting that 2 and 3 resolved *together*: reclaiming `error_summary` for actual failure
is what freed it to record the mid-run failure finding 2 needed somewhere to put. The two
tests that previously asserted `error_summary is not None` for a routine drop and a routine
collapse now assert the exact inverse.

## Finding 4 (P2) — `retries` is a no-op

**Confirmed**: `self.retries = retries` at `client.py:73` is never read, and the request
path calls `self._client.send()` directly at `client.py:266`. Your reading of the SDK is
correct.

**Fixable rather than removable, which is the one place I diverged from the implied
remedy.** `AskNewsSDK` forwards `**kwargs` to the `httpx.Client` constructor, so a retrying
transport can be injected:

```python
transport=httpx.HTTPTransport(retries=provider.retries)
```

Verified the value reaches `client.client._client._transport._pool._retries`, and verified
construction remains I/O-free under blocked socket **and** blocked DNS — the item's safety
property survives the change, which I treated as a precondition for making it at all.

**Scope stated precisely rather than overclaimed**, in the docstring: httpx transport
retries cover **connection failures only, not HTTP 5xx**. That is the safe kind for a
metered API — a request that reached the server is never re-sent, so this cannot double-bill.

`httpx` is now a declared dependency (it is imported directly), with
`test_httpx_is_a_declared_dependency` alongside the existing pin tests.

**On the test you flagged**: you are right that asserting attribute assignment rather than
effect was the underlying problem. A plumb-through test that asserts assignment is worse
than no test, because it converts an unverified assumption into apparent evidence — it is
what let a dead knob ship looking verified. It now asserts against the transport's
connection pool.

---

## What changed

| File | Change |
|---|---|
| `src/whiskeyjack_bot/research/asknews.py` | findings 1–4; `AskNewsRetrievalError` deleted |
| `tests/unit/test_asknews.py` | warning-channel leak net, partial-failure tests, finding-3 inversions, real retries test |
| `pyproject.toml` | `filterwarnings` guard; `httpx` declared |
| `tests/unit/test_dependency_pins.py` | `test_httpx_is_a_declared_dependency` |
| `src/whiskeyjack_bot/research/__init__.py` | dropped the deleted export |
| `docs/M1-301-NOTES.md` | round-1 record |

## Unchanged, and why

- **The content-hash rule** (`full_text` > `summary` > title) and its stability caveat.
  Still flagged for M1-305, which owns dedup. You did not challenge it; I am not changing it
  speculatively — that is how M1-301 rounds 4–6 went.
- **The per-article `except (ResearchSchemaError, AttributeError, TypeError, ValueError)`**
  and the `except Exception` around the SDK call (now a catch-and-record rather than a
  re-raise). You raised both as risk areas 4 and 5 and did not report them as defects; if you
  want them narrowed, say so and I will, but I would rather not widen or narrow guards on a
  theory.
- **`cost_usd` still `None`.** You accepted the reasoning.

## For round 2

The credential boundary, the current/archive strategy split, and the content-hash rule are
unchanged from round 1. The new surface worth attacking is the **failure contract**: is
`provider_failed` plus a non-null `error_summary` a sufficient signal for M1-303 to decide
"AskNews failed, run Exa" without silent switching? And is stopping the whole run on the
first failed call right, versus continuing to the next query?
