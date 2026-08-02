# M1-303 — Implement Exa fallback — implementation notes

Running record of M1-303 decisions and deviations, in the spirit of `docs/M1-301-NOTES.md`.
**Merges back into `docs/M1-NOTES.md`** with the rest of the retrieval epic.

Acceptance: **use Exa only when AskNews fails or when official-source/web retrieval is required;
the configured fallback records why it ran and preserves citations; no silent provider switching.**

## Delivered

- **`src/whiskeyjack_bot/research/exa.py`** — the adapter and the fallback policy:
  `FallbackReason` (`Literal`), `FallbackDecision` (frozen), `ExaRetrieval` (frozen),
  `ExaFallbackError`, `decide_fallback(...)`, `build_exa_client(config)`, `retrieve_web(...)`.
- **`src/whiskeyjack_bot/research/transport.py`** — `apply_connection_retries`, moved out of
  `asknews.py` unchanged so both adapters share the one httpx/httpcore workaround rather than a
  copy of it. `asknews.py` imports it; no behaviour change, and its retry tests pass untouched.
- **`tests/unit/test_exa.py`** (97 cases at round 1; **167** after the four review rounds below)
  and **`tests/property/test_exa_properties.py`** (14 properties at round 1; **28** after round 4
  added the allowlist validator and the non-boolean flag generators). Full gate green: 836 passed,
  1 xfailed.

**No new dependency, no migration, no schema change, no config change.** `httpx` was already a
declared direct dependency (M1-302), `RetrievalProviderConfig` already carries the `exa` provider
and `EXA_API_KEY`, and `RetrievalProvider`, migration 002's trigger vocabulary and
`AppConfig.secret_env_var_names()` already knew about both. **The dependency slot M1-303 claimed in
`docs/TRACKS.md` is therefore released unused** — see the registry.

## Deliberate choices

- **Direct `httpx` rather than `forecasting_tools.ExaSearcher` or `exa-py`.** D03 says use the
  package where it supports the workflow; here it does not, for the reasons M1-302 bypassed
  `AskNewsSearcher`, and more of them. `ExaSearcher` reads `EXA_API_KEY` through `os.getenv` +
  `assert` rather than the configured `api_key_env`; it is async/aiohttp in a synchronous pipeline;
  it discards the raw response body (which M1-306 must persist to replay) and the `costDollars`
  block; and it parses `publishedDate` as `fromisoformat(value.strip("Z"))`, yielding a **naive**
  datetime this project's schema rejects outright. `exa-py` 2.16.2 would add `openai`, `requests`
  and `python-dotenv` transitively for a single POST and likewise hides the raw body.

- **The API contract was verified, not assumed** — `https://api.exa.ai/openapi.json`, checked
  2026-07-27, recorded with that date in the module docstring. Two things the contract changed
  since the vendored wrapper was written: `livecrawl` is **deprecated** in favour of
  `maxAgeHours`, and the response carries `costDollars.total`, which the API documents as an
  *estimate*, "not an invoice record". We send `maxAgeHours: 24` and record the estimate on that
  understanding.

- **"No silent provider switching" is enforced by the signature, not by discipline.**
  `retrieve_web` **requires** a non-empty `fallback_reasons`; an empty sequence, a value outside
  the vocabulary, or (since round 2) a set carrying no *authorizing* reason raises
  `ExaFallbackError` before any billable call. The config refusal — `retrieval.fallback.provider`
  must be `exa` and `retrieval.primary.provider` must be `asknews`, since running this adapter
  against a differently configured fallback *is* the silent switch — lives in
  `_ensure_exa_is_configured_fallback` and is applied at **both** entry points, `build_exa_client`
  and `retrieve_web` (tightened in round 3; a `client` argument carries no memory of which config
  built it). In `build_exa_client` it is checked **before** the credential lookup. One INFO log
  line records the engagement, from constants and the integer question id only.

- **`should_run` is exactly the backlog's two conditions; every relevant fact is still recorded
  once it does.** (Corrected in round 2 — see below.) `decide_fallback` triggers only on
  `primary_provider_failed` or `official_source_required`; `primary_returned_no_documents` is
  reported alongside a real trigger when it also holds (a true, useful fact — "AskNews raised" and
  "AskNews returned nothing" are different things and the ledger reads them differently via
  `error_summary`) but cannot authorize a call by itself, since a provider that answered with zero
  documents has not *failed*. The tuple is deduplicated and normalized to vocabulary order because
  it is persisted — the stored list must be a function of the triggers, not of the caller's
  bookkeeping.

- **The reason is persisted in `provider_config["fallback_reasons"]`** (owner decision), which maps
  to `research_runs.provider_config_json`, so the ledger alone answers "why was a second provider
  engaged" with no migration and no change to the M1-301 schema that three other branches depend
  on. Alternative considered and declined: a first-class `fallback_reason` column, which would have
  meant claiming migration 004 and touching the shared schema for a field only this adapter writes.

- **Content-hash source rule (pinned): `text` → title → `""`.** Deliberately **not** highlights,
  and the request asks for `contents.text` rather than `contents.highlights` for the same reason:
  Exa generates highlights per query, so one page retrieved by two queries would hash differently
  and defeat both the intra-run collapse and M1-305's dedup. `maxCharacters` is pinned at 4000
  (ceiling is 10000) so document identity does not depend on how much of a page Exa extracted that
  day, and `maxAgeHours: 24` prefers cached content over a live crawl for the same reason — and
  because a live crawl costs more.

- **`canonical_url` is derived, not copied.** `canonical.py`'s contract says adapters call
  `canonicalize_url` once per document and that copying `original_url` was only the pre-M1-305
  placeholder. M1-305 has since merged, so this adapter canonicalizes; `original_url` keeps the
  as-retrieved URL, so nothing is lost. (`asknews.py` still copies — it predates the merge. Worth a
  follow-up, deliberately not done here: changing it changes existing AskNews `canonical_url`s.)

- **`publishedDate` gets one uniform rule.** Offset-aware values convert to UTC; a date-only value
  (the documented `YYYY-MM-DD`) or an offset-less timestamp is pinned to **midnight UTC of the
  stated date**; anything unparseable yields `None`. The pinning carries up to roughly a day of
  error in either direction, which is recorded in the docstring rather than hidden; the
  alternative — discarding the only date the provider gave — makes every document undated, and
  M1-305's owner decision treats undated as stale. A bad date never costs the citation.

- **`source_type` is `official` only when the result's own host matches the caller-supplied
  `include_domains` allowlist**, otherwise `web`. (Tightened in round 3 — see below; it was
  previously decided once per *run*, from whether an allowlist was supplied at all.) The allowlist
  is a precondition, not the proof: `_matches_official_domain` checks each result's canonical host
  for an exact or subdomain match, because Exa's own enforcement of `includeDomains` is not this
  module's to trust. Tagging on the strength of the *reason* would be worse still — it would let
  `official_source_required` label whatever the open web returned, an unearned attribution claim
  (ambiguity rule 4: stricter reading). Round 4 completed this: both sides of the comparison are now
  canonicalized by the same function, and only bare hosts — filter shapes this module can actually
  verify per result — are accepted at all.

- **`publisher` stays `None`.** Exa returns no publisher field, and deriving one from the hostname
  would put a value in the ledger that no provider asserted. `summary` likewise stays `None` (it is
  reserved for the pipeline's own summarization; Exa's text is provider material and goes in
  `snippet`, bounded at 500 characters — the full text stays in the raw response for M1-306).

- **Failure is data, not an exception** (the M1-302 rule). A run makes up to
  `max_queries_per_question` billable calls, so a mid-run failure stops early, sets
  `provider_failed`, fills `error_summary` with the same constants-only wording AskNews uses, and
  returns everything already retrieved. A 200 whose body is not a mapping, or carries no `results`
  list, is treated the same way — it is a contract breach, not an empty answer, and continuing
  would pay for more calls against a provider that is not answering in the documented shape.
  Provider exceptions are **discarded, never inspected**: an httpx error can quote the request, and
  the request carries the API key in a header.

  **What reaches `raw_responses`, precisely** (corrected in round 4 — this previously claimed "the
  body is recorded first either way", which held for only one of the three failure shapes). A 200
  whose body is a `dict` **is** retained, including one carrying no `results` list. A body that is
  valid JSON but not a mapping, and one that does not parse at all, are **not**: the field is
  `tuple[dict[str, Any], ...]` and there is nowhere in it for a bare list, scalar or byte string to
  go. The spend is still marked rather than lost — `calls_attempted` counts the call the moment the
  POST is about to be issued, so an unrecordable body forces the run's `cost_usd` to `None` under
  the complete-or-nothing rule. If those failures must be *replayable*, M1-306 needs a raw
  byte/status representation; inventing one here would be inventing the raw-artifact shape that
  item owns, on a branch that adds no schema and no migration.

- **Cost is recorded but qualified, and it is complete-or-nothing.** A per-call `costDollars.total`
  is unusable when it is `bool` (an `int` subclass — `true` would otherwise be recorded as one
  dollar), non-numeric, negative or non-finite. `NaN`/`Infinity` are reachable in practice: they
  are not valid JSON but `json.loads` accepts them, and a stored non-finite cost would validate
  here and fail at ledger-write time, after the money was spent. **A single unusable per-call cost
  drops the whole run's `cost_usd` to `None`, not just that call's contribution** (tightened in
  round 3 — see below): a total is only published when every *attempted* call, including one that
  raised after reaching Exa, also reported a usable figure. Anything less is a subtotal, and a
  subtotal stored in `cost_usd` looks exactly like a complete one.

## Round 1 — GPT cross-model review findings (PR #16)

Five P2 findings from the automated cross-model review (the inline Codex threads on PR #16), all
fixed on the same branch in `d82f6f6`, before the round-2 request:

- **Zero documents alone no longer authorizes a call** (`decide_fallback`). The as-submitted
  version treated "AskNews returned nothing" as a third, independent trigger; the reviewer noted that
  the backlog's acceptance text is a closed pair ("AskNews fails **or** official-source/web
  retrieval required") and the function's own docstring already conceded a zero-document run "has
  not failed" — a direct contradiction with letting it trigger anyway. Fixed as described above.
  This is a real behavior change, not a rewording: an all-success run with nothing retained and no
  official-source requirement now does **not** spend on Exa.
- **`retrieval.primary.provider` must be `asknews`.** `build_exa_client` checked only that the
  *fallback* provider was `exa`; a config naming `exa` as its own primary passed and would let Exa
  "fall back" to itself. Added the symmetric check, before the credential lookup like the existing
  one.
- **`numResults` is capped at Exa's documented ceiling of 100** (`_MAX_NUM_RESULTS`).
  `RetrievalConfig.max_documents_per_query` only enforces `ge=1` — it is shared with AskNews's
  `n_articles`, which has no such ceiling — so a value above 100 was previously sent as-is and
  rejected by Exa, turning a configuration choice into a full run failure. Capped at the transport
  layer rather than in the shared schema; the capped value is what gets persisted into
  `provider_config["num_results"]`, so the ledger reflects what was actually sent.
- **Two `OverflowError` escapes**, both reachable after a billable call had already been made and
  both outside this module's "never raises" contracts: `_published_at_utc`'s
  `astimezone(timezone.utc)` on a syntactically valid boundary timestamp (e.g.
  `0001-01-01T00:00:00+14:00`), and `_call_cost_usd`'s `float(total)` on a `costDollars.total`
  integer too large to convert (e.g. `10**400`). Both now degrade to `None`, matching every other
  unusable value these functions already handle. The property-test fixtures
  (`PUBLISHED_DATES`, `COST_VALUES`) didn't include these boundary values, which is why hypothesis
  hadn't already caught them; both were added.

## Round 2 — GPT cross-model review findings (PR #16)

Three P2 findings, all fixed in `07060ea`:

- **An *authorizing* reason is now required, not merely a non-empty one** (`_AUTHORIZING_REASONS`,
  enforced in `retrieve_web` before any network call). `decide_fallback` already refused to let
  `primary_returned_no_documents` trigger a run on its own, but `retrieve_web` accepted it as the
  sole reason, so a caller assembling the tuple by hand could spell an Exa call authorized only by
  a fact the policy says cannot authorize one. The two entry points now agree. A behavior change
  for hand-assembled callers; `decide_fallback`'s own output could never hit it, because that
  function never emits the non-authorizing reason alone.
- **Intra-run duplicate collapsing reuses M1-305's `deduplicate()`** instead of a local first-seen
  set. Both collapse on the same identity — every document in a run shares one
  `retrieval_run_id`, so the local set matched `research_documents`' `UNIQUE (retrieval_run_id,
  canonical_url, content_sha256)` — but first-seen made the *survivor* a function of provider
  result order, so the retained author/URL could differ between two replays of the same evidence.
  `deduplicate()` picks it by M1-305's deterministic total order. A real behavior change, and the
  reason to prefer the shared function over a four-line local one: the tiebreak is the part that
  took five review rounds to get right in M1-305, and it should exist once.
- **The cumulative cost sum is guarded against overflow to `inf`.** Each per-call figure is
  already checked with `isfinite`, but their *sum* can still overflow, and `ResearchRun.cost_usd`
  requires a finite value — so two billed, successful calls could crash `validate_run` after the
  money was spent. The post-loop guard drops the total the same way an unusable per-call cost is
  dropped, rather than turning a paid run into a failure.

## Round 3 — GPT cross-model review findings (PR #16)

Three P2 findings, fixed in `d2ca86c` with property coverage added in `844e389`:

- **The config preflight is applied at both entry points.** `build_exa_client` refused to build a
  client against a config that did not name Exa as the fallback, but `retrieve_web` re-checked
  nothing, and it takes a bare `httpx.Client` — so a caller holding a client built any other way
  could run Exa against a config naming `asknews` as the fallback, and the run would persist
  `provider="exa"`. That is precisely the silent switch the acceptance criterion forbids. The two
  checks moved into `_ensure_exa_is_configured_fallback`, called from both. Rejected alternative:
  a marker type (`ExaClient`) that only `build_exa_client` can construct, making the bypass
  unrepresentable rather than re-checked — disproportionate for a pure config inspection with no
  I/O, and it would change `retrieve_web`'s signature out from under every test that injects a
  `MockTransport` client.
- **`official` is decided per result, from that result's own URL** (`_matches_official_domain`),
  not inherited from whether the run requested an allowlist at all. A real behavior change: a
  result Exa returns *outside* `includeDomains` — which it can, the filter is the provider's
  promise, not ours to trust — used to be labelled `official` anyway. The match is exact-or-
  subdomain against the canonical host (`data.bls.gov` counts for `bls.gov`; a federal agency's
  data desk is still the agency), and deliberately `host == d or host.endswith(f".{d}")` rather
  than `host.endswith(d)`, so `notbls.gov` does not earn the label off `bls.gov`. A malformed or
  typo'd allowlist entry fails safe — nothing matches, the result stays `web`. (Round 4 replaced
  that last sentence's behaviour: a malformed entry is now **refused before the run starts** rather
  than silently matching nothing. Failing safe was the right direction but the wrong altitude — it
  made an unusable allowlist indistinguishable from a genuinely unofficial result.)
- **`cost_usd` is complete-or-nothing.** It was set whenever *any* call reported a usable figure,
  so a run where a later call omitted its cost or failed after reaching Exa stored a subtotal that
  looked exactly like a complete total — an undercount against `max_cost_usd` with nothing marking
  it as partial. A call now counts as attempted the moment the loop is about to issue the POST, and
  the total is published only when every attempted call also reported a usable cost. The reviewer
  offered two options; this took "withhold the total" over "persist a completeness marker" because
  `ResearchRun.cost_usd` is already nullable, so `None` says "run spend unknown" with no schema
  field, no migration and no change to the shared M1-301 schema. See the downstream note below —
  `None` means *unknown*, not *free*.

## Round 4 — GPT cross-model review findings (PR #16)

Five blocking findings, all reproduced locally before being fixed. Every one is the same defect
class the module already claimed to have closed, and the module docstring said so outright — "all
of them fire before any network use, and therefore before any billing". Findings 1–4 were
counterexamples to that sentence. Each fix is now pinned by a test that **fails against the pre-fix
code** (verified by stashing `src/whiskeyjack_bot/research/exa.py` and re-running).

- **The client was never bound to Exa.** `retrieve_web` takes a bare `httpx.Client` and posts to the
  relative `_SEARCH_PATH`, so a client built with `base_url="https://other-provider.example"` sent
  the query to that host while the run persisted `provider="exa"` — a silent provider switch reached
  from the side the round-3 fix did not cover. The round-3 config check proves what was
  *configured*; it says nothing about where the client argument points. `_require_exa_client` now
  compares `base_url` **structurally** against `httpx.URL(_BASE_URL)` — scheme, host, port,
  `path.rstrip("/")` and an empty `userinfo`. String comparison would not do: httpx spells the same
  destination as both `https://api.exa.ai` and `https://api.exa.ai/` depending on the caller, and a
  prefix test would admit `https://api.exa.ai/v1`, whose merged request URL is
  `https://api.exa.ai/v1/search`. `userinfo` is checked because a base URL is the one place a
  credential could ride into the request. The round-3 `ExaClient` marker type stays rejected for the
  reason recorded above — every test injects a `MockTransport`, and one carrying the right
  `base_url` still passes. Stated limit, not overclaimed: this binds the *destination*, not the
  transport, and an absolute URL handed to `.post()` would bypass `base_url` — unreachable, since
  the path is a module constant.
- **A bare `str` was accepted as a sequence of queries.** `str` *satisfies* `Sequence[str]`, so
  `mypy --strict` cannot catch it, and `list("inflation")` became six billable single-character
  searches with `run.queries == ['i','n','f','l','a','t']` in the ledger. `include_domains="bls.gov"`
  became seven single-character filters the same way. `_string_list` now refuses `str`/`bytes`/
  `bytearray` containers outright and requires every element to be a non-blank string, and it wraps
  `list(values)` so a container whose `__iter__` raises still arrives as this module's error.
  `fallback_reasons` needed no change — no single character is in the vocabulary, so a bare string
  there already failed — but that is now pinned by a test rather than assumed.
- **Truthiness was standing in for a boolean.** `decide_fallback(primary_failed="false", …)`
  returned `should_run=True` with `primary_provider_failed` among the reasons: a paid call
  authorized, and a fabricated attribution persisted as the reason it happened. `primary_documents`
  was already gated on its exact type; the two flags were not, and the asymmetry *was* the finding.
  Both now require exact `bool`.
- **Caller metadata was validated only after the money was spent.** `question_id`,
  `retrieval_run_id` and `now` reached `validate_run` at the *end* of the run, so a malformed one
  let every billable call happen and then raised — discarding the record of the spend, which is the
  one thing this module promises not to do — and raised `ResearchSchemaError`, a sibling module's
  error rather than its own. Worse, the engagement log interpolates `question_id` with `%d`: given a
  string, `logging` catches its own `TypeError` and writes a `--- Logging error ---` report to
  **stderr containing the raw argument**. That is a value leak through a channel neither the
  exception message nor `caplog` sees, so the new leak test asserts on `capsys` as well (the M1-302
  rule: cover every egress channel, not every message). `_require_run_metadata` now gates all three
  before the log line and before the first POST.
  - `question_id` is gated **more strictly than the schema, deliberately**. `ResearchRun` is not
    strict about it: pydantic coerces `"42"` to `42` and `True` to `1`, so a string id would
    validate happily *after* the stderr leak had already happened and the run would have succeeded
    with a coerced value. An exact `int` closes the channel at its source.
  - Found while reproducing, not in the review: an aware `now` near `datetime.min` makes
    `now - timedelta(days=freshness_days_default)` raise a raw `OverflowError`. Same class — a
    caller mistake escaping as somebody else's exception type — so it is converted to
    `ExaFallbackError` too.
- **The allowlist and the result host were normalized by different code.** `_matches_official_domain`
  compared a canonical A-label host against a merely lowercased entry, so an allowlist of
  `("bücher.de",)` never matched a result at `https://bücher.de/` (canonical host
  `xn--bcher-kva.de`) and a genuinely official IDN source was labelled `web`. Separately, Exa's
  `includeDomains` also documents path prefixes (`exa.ai/blog`) and subdomain wildcards
  (`*.substack.com`) — confirmed against Exa's published changelog and search reference — and this
  adapter forwarded both while being structurally unable to match either.
  - `_validated_domains` now **rejects everything but a bare host** and canonicalizes what survives
    through the same public `canonicalize_url` that produces the `canonical_url` it is compared
    against. That shared code path is the fix; a private import of `canonical._canonical_host` would
    have worked but would not have guaranteed *the same* path.
  - The character screen is not redundant with canonicalization: `canonicalize_url` silently
    *reduces* `bls.gov:443` and `user@bls.gov` to `bls.gov`, so an entry meaning something other
    than a bare host has to be refused before it is normalized into one that looks fine.
  - Rejecting rather than implementing wildcard/path semantics is the stricter reading (CLAUDE.md
    ambiguity rule 4), and it follows from the design already in place: per-result verification
    exists precisely because Exa's filter is the provider's promise, not ours. A filter shape this
    module cannot verify per result is one it must not accept — forwarding it means Exa honours the
    restriction and the ledger then under-attributes every result it selected. Widening to those
    forms means pinning down semantics Exa does not document at the edges (does `*.substack.com`
    match bare `substack.com`?), which is a deliberate future change and not an adaptation to make
    mid-review.
  - **Behaviour change to note:** `provider_config["include_domains"]` and the `includeDomains` sent
    to Exa now hold the *canonical* form (`bücher.de` → `xn--bcher-kva.de`, `BLS.GOV` → `bls.gov`),
    not the caller's spelling. The ledger records the filter that was actually applied and matched.
  - `_matches_official_domain` correspondingly drops its own `.strip().lower()`: both sides arrive
    canonical, and normalizing at the comparison would mask an un-normalized allowlist reaching it
    while fixing only one of the two forms (a U-label needs IDNA, not `str.lower`).
- Deliberately **not** done: rejecting single-label entries such as `"gov"`, which would promote
  every `.gov` host. Beyond the finding, and inventing allowlist policy on a round-4 review is how
  a review reaches round six. **Reversed in round 5 — see finding 5 below.** The judgement was
  wrong in one specific way: this was never allowlist *policy*, it was a false attribution the
  module already had the machinery to refuse.

Two non-blocking observations were also acted on — the `max_cost_usd` path below, and the
"body is recorded first either way" claim in the failure-handling note, which was wrong for two of
the three failure shapes and has been corrected.

## Round 5 — GPT cross-model review findings (PR #16)

Six blocking findings. **All six were reproduced by execution before any of them was fixed**, and
each reproduction was re-run afterwards to confirm it now refuses. One is worse than the review
said. None was spurious.

Round 4's own summary claimed the caller-mistake surface was closed; findings 3 and 6 are
counterexamples to that claim, in the same shape as round 4's counterexamples to round 3's. The
pattern worth naming: **hardening applied argument-by-argument leaves the argument nobody listed.**
Round 4 routed `queries` and `include_domains` through `_string_list` and did not route
`fallback_reasons`; round 4 moved `question_id`/`retrieval_run_id`/`now` into preflight and
converted `now` to UTC at the *end*. Both gaps are the same omission.

**1. Redirects handed the API key to another host.** `retrieve_web` accepts any `httpx.Client`, and
a client built with `follow_redirects=True` — an ordinary, unremarkable setting — turned a `307
Location: https://other-provider.example/search` into a second request carrying `x-api-key`
verbatim. httpx strips `Authorization` when a redirect leaves the origin; it forwards every other
header, and this API's credential is a custom one. The run still recorded `provider="exa"`. That is
the silent provider switch `_require_exa_client` exists to prevent, reached from a third side —
and unlike the other two it also discloses the credential. `follow_redirects=False` is now pinned
**at the call site**, not left to the client's default, because the client is the caller's. A
redirect is refused explicitly rather than left to `raise_for_status` (which does treat 3xx as an
error in the pinned httpx) so that a redirect carrying a JSON body can never parse as a real answer
from a host that never sent one. `build_exa_client` states the default too.

Cost note: with a following client this was not one extra call but up to twenty-one — httpx chases
a same-origin redirect to `max_redirects` before giving up, every hop billable. The regression test
asserts `len(handler.requests) == 1`, which is the assertion that fails on the pre-fix code.

**2. The structural `base_url` check did not establish `/search`.** Round 4 compared `(scheme, host,
port, path.rstrip("/"), userinfo)`. That reads as stricter than a string comparison but is looser:
it never looks at the query or the fragment, and `rstrip("/")` collapses repeated slashes. Verified
counterexamples — three named by the review, one found while reproducing:

| `base_url` (or client kwarg) | merged request URL | round 4 |
|---|---|---|
| `https://api.exa.ai?x=1` | `https://api.exa.ai/?x=1/search` | accepted |
| `https://api.exa.ai//` | `https://api.exa.ai//search` | accepted |
| `https://api.exa.ai#f` | `https://api.exa.ai/search#f` | accepted |
| `params={"k": "v"}` | `https://api.exa.ai/search?k=v` | accepted |

The last one is the one the review missed, and it is the reason the fix is not "also check query and
fragment": `base_url` is not the only thing httpx merges into a request URL, so decomposing
`base_url` is the wrong object to inspect at all. `_require_exa_client` now asks httpx to
**build the actual request** — the same merge `.post()` performs — and requires
`request.url == httpx.URL("https://api.exa.ai/search")`. One equality covers scheme, host, port,
path, query, fragment and userinfo, and it cannot drift from what is sent, because it *is* what
would be sent. The two legitimate spellings (`https://api.exa.ai`, `https://api.exa.ai/`) still
pass; every round-4 rejection still fails.

These are wrong-endpoint bugs, not provider switches — all four still address `api.exa.ai`. Recorded
as such rather than inflated: the review's own wording ("does not guarantee `/search`") is the
accurate one.

**3. An upper-bound `now` billed a call and then raised raw.** `now = datetime(9999, 12, 31, 23, 59,
59, tzinfo=timezone(-timedelta(hours=14)))` passed every preflight — it is aware, and the freshness
*subtraction* moves away from the boundary, so round 4's `OverflowError` guard never fired. One POST
was made and paid for. Then `validate_run` converted it to UTC, which overflows, and raised
`builtins.OverflowError: date value out of range`: a raw exception from a sibling module, after the
money, discarding the record of the spend. Round 4 fixed the low end of exactly this and did not
look at the high end.

`_require_run_metadata` is now **validate-and-return**, like `_validated_domains`: it converts `now`
to UTC once, in preflight, translates the failure to `ExaFallbackError`, and returns the normalized
value that the run, the documents and `startPublishedDate` all then use. The `utcoffset()` gate
stays ahead of the conversion and is not redundant with it — `astimezone` on a *naive* datetime
silently assumes local time and succeeds, so it cannot be the thing that rejects one.

**Behaviour change to note:** for a caller passing a non-UTC `now`, `startPublishedDate` and
`provider_config["start_published_date"]` now carry the UTC spelling of the same instant. This is an
improvement rather than a cost — `provider_config` now agrees with the run's own already-UTC
`freshness_cutoff_utc` column, and the persisted record no longer depends on which timezone a caller
happened to spell the same moment in.

**4. A terminal DNS root dot under-attributed official sources.** `canonicalize_url` preserves it, so
`bls.gov.` and `bls.gov` — two valid spellings of one host — canonicalized to two different strings
that never matched. Both directions verified: a `bls.gov` allowlist against a result at
`https://bls.gov./report`, and a `bls.gov.` allowlist against `https://bls.gov/report`, each
labelled `web`. The run asked for official sources, Exa honoured it, and the ledger recorded the
weaker claim.

One root dot is now normalized on both sides, and **deliberately in two places**: `_validated_domains`
strips it as part of the canonicalization it already does, while `_matches_official_domain` strips it
from the **result host only** and never from `domains`. That asymmetry preserves the rule round 4
established — the allowlist is ours and arrives validated, so normalizing it at the comparison would
mask an un-normalized allowlist reaching the function; the result host is provider-derived and must
be normalized on arrival.

**Fixed in `exa.py`, not in `canonicalize_url` — owner decision.** The root cause is upstream: two
reports of one page differing only by the root dot are also two distinct dedup keys today. But
`canonical_url` *is* document identity, so changing it moves the key of every already-stored
document, in a module owned by already-merged M1-305, on a branch scoped to M1-303. **M1-310** is
filed for that question. Recorded plainly: this fix corrects the attribution, and leaves the dedup
consequence open behind a backlog row.

**5. A single-label allowlist manufactured false official attribution.** `include_domains=("com",)`
was accepted, and the subdomain rule then labelled `https://attacker.com/report` **official**.
Round 4 declined this as allowlist policy beyond the finding. That was wrong, and the reversal is
recorded rather than quietly made: it is not policy, it is a false attribution claim, in the one
place this project says it will not make one — an instrument whose product is attribution cannot
ship a spelling of "official" that means "any host on the public internet". Entries now require at
least two labels, checked *after* the root dot is stripped so `gov.` cannot get in around `gov`.

**Known residual, stated rather than half-fixed:** a *multi-label* public suffix — `co.uk`,
`com.au` — still over-attributes everything beneath it. Closing that needs a public-suffix list,
i.e. a new dependency, which serializes against every other track through `uv.lock` and is a
wave-level decision, not a review fix. The review agrees the broader policy can stay separate. The
two-label rule is the part available for nothing, and it is taken.

**6. A malformed reason container escaped as a raw exception.** `fallback_reasons=None` raised
`TypeError: 'NoneType' object is not iterable`; a container whose `__iter__` raises escaped as
whatever it threw. `_canonical_reasons` now goes through the same `_string_list` that round 4 built
for `queries` and `include_domains` — the one caller argument that hardening never reached. The
container refusal gets its own constant (`_BAD_REASONS`), separate from the vocabulary message,
because the two say different things: one is about the shape of the argument and says nothing about
what was in it.

### Non-blocking observations — both acted on, neither defended

- **`IntEnum` was accepted where the notes claimed "exact `int`".** `isinstance(question_id, int)`
  admits any `int` subclass. Harmless in practice (`%d` and pydantic both handle it), but the
  documented claim was false, and the cheapest way to make a claim true is to make it true:
  `type(question_id) is not int`, the exact-type gate M1-203 already established. The `bool` check
  is now subsumed by it.
- **The provider-binding claim was too broad.** A caller-supplied `transport` or `event_hooks` can
  still send the bytes elsewhere — a request hook may rewrite `request.url` after it is built. The
  claim is narrowed, in the module docstring and in `_require_exa_client`, to exactly what is
  enforced: **the request URL this module builds is Exa's search endpoint.** The transport remains a
  trusted boundary, for the round-2 reason (every test injects a `MockTransport`), and that is now
  stated as a limit rather than implied away.
- The raw-response persistence boundary staying with M1-306 was affirmed; no change.

### Test discipline

35 new cases. Each was run against the pre-fix `exa.py` (stashed) and **23 unit cases and 4 of 5 new
properties fail there**. The exceptions are recorded rather than presented as regression coverage:

- `test_the_root_dot_does_not_widen_a_suffix_coincidence` and its unit twin pass pre-fix by
  construction — they guard the fix against *over*-widening `notbls.gov` into a match, which is a
  different failure than the one being fixed.
- `base_url="https://api.exa.ai/search"` and `include_domains=("bls.gov..",)` were already rejected
  by round 4; they are in the tables so the rewrite is shown not to have loosened anything.
- The `follow_redirects=False` half of the redirect test passes pre-fix, because the pinned httpx
  already errors on a 3xx. It pins the explicit `is_redirect` branch against a future httpx that
  does not.

One test written during this round was **deleted for proving nothing**: a first version asserted
that a redirect body is not recorded, using a client with httpx's default `follow_redirects=False`
— which passes on the broken code. Catching it required the round-4 habit of running every new test
against the pre-fix module, and it is the reason the surviving redirect test asserts the *request
count* rather than only the outcome. The same run also corrected a code comment claiming "3xx is not
an error status to httpx", which is false for the pinned version and would have left the
`is_redirect` branch dead and mis-explained.

## Notes for downstream items

- **M1-306** owns persistence and replay: `raw_responses` are in memory only, and
  `run.raw_response_path` / every `raw_artifact_path` stay `None`. It also owns the wiring —
  `decide_fallback(primary_failed=..., primary_documents=..., official_source_required=...)` then
  `retrieve_web(..., fallback_reasons=decision.reasons)`.
- **M1-504** reads `error_summary`, and this adapter keeps its meaning identical to the AskNews
  adapter's: set when the run failed or returned nothing, never for routine drops or intra-run
  duplicate collapsing (those ride on `ExaRetrieval`).
- **Whoever wires budget enforcement must decide what `cost_usd is None` means, explicitly.**
  Under round 3's complete-or-nothing rule it means **unknown**, never *free*: the run may have
  spent real money whose total this adapter could not vouch for. Summing a column of costs and
  treating `None` as zero reintroduces exactly the undercount against `run_limits.max_cost_usd`
  that the round-3 finding was about — and it would do so on precisely the runs where a paid call
  failed, i.e. the ones most likely to be retried. Nothing reads the field today: `max_cost_usd`
  exists only as a config field (`RunLimitsConfig` in `config.py`, `run_limits:` in
  `config.example.yaml`) with no consumer, and the AskNews adapter always writes `None` because its
  credits are unconvertible. So this is a note for M1-306/M1-504, not a live defect. (Round 4
  corrected the path here: it read `retrieval.max_cost_usd`, which does not exist.)
- **The AskNews adapter has the same two caller-preflight holes round 4 closed here, and M1-302 is
  already merged.** `retrieve_news` (`asknews.py`) takes `queries: Sequence[str]` and does
  `list(queries)[:n]` with no container or element check, so a bare `str` expands into one billable
  search *per character* — and AskNews makes **two** calls per query (current and historical), so
  the same mistake costs twice as much there. Its `question_id`, `retrieval_run_id` and `now`
  likewise reach `validate_run` only at the end of the run, so a malformed one discards the record
  of every call already paid for and surfaces as `ResearchSchemaError` rather than the module's own
  error. It is *less* exposed in one respect: it has no `_LOGGER` call, so it lacks the stderr
  leak channel that made finding 4 a hygiene defect as well as a billing one.
  Fixed here rather than there per CLAUDE.md's one-item-per-branch rule, and filed as its own
  backlog row (**M1-309**) rather than left in a notes file. Whoever takes it should lift
  `_string_list` and `_require_run_metadata` into a shared module next to `transport.py` rather than
  copy them, so the two adapters cannot drift again.
- The known **`content_sha256` lone-surrogate defect** (CLAUDE.md gotcha, open owner decision)
  reaches this adapter through Exa's `text`. It degrades to a counted drop rather than a crash,
  because `UnicodeEncodeError` is a `ValueError` and the per-result catch includes it. The property
  suite exercises the path (~45 of 400 generated results hit it).
