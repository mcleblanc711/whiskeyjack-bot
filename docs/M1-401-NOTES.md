# M1 notes — Forecast Generation epic (M1-40x)

> **Merge-back trigger:** when the Forecast Generation epic (M1-40x) is fully merged, append
> these sections to `docs/M1-NOTES.md` in issue order and delete this file, as one docs-only
> commit. This file exists because `docs/M1-NOTES.md` is the one file every parallel M1 branch
> would append to, guaranteeing a conflict on every merge. `docs/M1-NOTES.md` is left
> byte-identical to master on this branch.

## M1-401 — Version and hash the prompt

`forecast_records.prompt_version` and `prompt_sha256` have been `NOT NULL` since migration 001,
so no forecast row can be written until this item produces both. They are also unreconstructable
after the fact — you cannot recover which prompt text produced an earlier forecast — which is why
D04 ("freeze attribution schema before first submission") requires them from the first forecast
rather than as a later addition. Gates M1-402.

Delivered:
- `src/whiskeyjack_bot/prompt.py` — `load_prompt(path, expected_version) -> LoadedPrompt`
  (frozen dataclass: `version`, `sha256`, `text`), plus `prompt_sha256()` and
  `parse_declared_version()` exposed for tests and diagnostics. `PromptError` follows the
  `ConfigError`/`LedgerError` hygiene rule: never echoes prompt contents, and every wrap uses
  `from None`.
- **The v1.1.0 prompt patch** (`CLAUDE_CODE_PROMPT.md` § B, required by `CLAUDE.md`): two bullets
  appended to `prompts/forecaster.md`'s "General rules" governing `reliability_tag` weighting and
  `provenance: llm_reported` load-bearing limits; H1 bumped to `v1.1.0` and
  `config.example.yaml` `prompt_version` to `1.1.0` in the same commit. Verified before applying
  that the patch's vocabulary matches the merged M1-301 literals at `research/model.py:74,79`
  exactly (`Provenance`, `ReliabilityTag`) — the patch describes fields that already exist.
- `ForecastConfig.prompt_version` gains a bare-semver validator; `env_verify` gains
  `_verify_prompt_version`, cross-checking the prompt's H1 against config at startup.
- `tests/unit/test_prompt.py` (+ cases in `test_env_verify.py`, `test_config.py`). Suite: 326
  passed (301 on master @ `a0cbb67`); ruff check + format + `mypy --strict src` clean.

Decisions:
- **The digest is over raw file bytes, unnormalized**, mirroring `ledger.py`'s migration
  checksum. It deliberately does *not* reuse `research/hashing.py::content_sha256`, whose pinned
  rule collapses whitespace runs and applies NFC: correct for research documents, wrong here.
  The acceptance criterion is "changed *bytes* produce a new hash", and a reflowed prompt changes
  what the model sees. `test_whitespace_reflow_changes_hash` pins this and asserts the
  `content_sha256` rule would *not* have distinguished the two. There are now four distinct
  sha256 definitions in this codebase; the module docstring enumerates why this one differs.
  Verified independently: `sha256sum prompts/forecaster.md` equals `load_prompt(...).sha256`.
- **Bare semver is canonical.** The prompt H1 carries a `v` prefix, config does not; the parser
  strips it and config rejects a prefixed value. A version disagreement is a hard error, never a
  coercion — that drift is exactly what D04 exists to catch.
- The version parse is **anchored to line 1**. The prompt body contains
  `"schema_version": "1.0.0"` (the output-record schema, an unrelated number) inside a fenced
  JSON example; a document-wide semver search matches it and would keep matching it, silently and
  wrongly, once the two versions diverge.
- `load_prompt` re-validates `expected_version` as bare semver even though config already does,
  because the mismatch message echoes it — an arbitrary caller-supplied string must not reach a
  diagnostic. Both versions in that message are provably semver before being interpolated.
- `test_prompt.py`'s regression test loads the *real* prompt and *real* `config.example.yaml` and
  asserts they agree, so editing the prompt without bumping both places fails CI. **That alone was
  not enough** (GPT review): comparing H1-to-config cannot see *body* drift — every byte of the
  prompt could change while both versions read `1.1.0`. `RELEASED_PROMPT_SHA256` now pins each
  released version to its exact digest, so a body edit fails CI until the version is bumped *and* a
  new digest pinned. Verified by transiently appending one space to the prompt and watching the
  test fail.

### Review round 1 — findings addressed

- **`LoadedPrompt.text` is `field(repr=False)`.** The error paths were sanitized but the value
  object was not: the default dataclass repr printed the whole prompt through any log line, failed
  assertion or frame-capturing traceback. `version` and `sha256` stay visible — both are safe by
  construction and a repr without them is useless.
- **One shared semver rule.** `prompt.py` and `config.py` each had their own pattern, and they
  disagreed. Both now compile `BARE_VERSION_RE` from `prompt.py`: ASCII-only (`\d` matched Unicode
  decimals, so `v١.١.٠` parsed and would have reached the ledger column unsearchable), no leading
  zeroes (`01.1.0` and `1.1.0` named the same prompt but compared unequal), and `fullmatch` rather
  than `match` + `$` (which accepted a terminal newline into a rendered diagnostic).
- **An ambiguous H1 is rejected, not resolved.** `# … v1.1.0 supersedes v2.0.0` parsed as `2.0.0` —
  the *superseded* version — because the anchored scan was greedy. Non-greedy quantifiers do not
  fix this: with a trailing anchor the engine backtracks to the same last token. The parser now
  collects every `v<semver>` token in the H1 and raises unless there is exactly one, trailing. Two
  declared versions is drift, and D04 exists to catch drift, not to pick a winner from it.
- **Paths in diagnostics — reviewed and kept**, with the policy now written down rather than
  implied. GPT was right that the hygiene rule as phrased was ambiguous, but the fix is not local:
  ~30 sites across `config.py`, `ledger.py`, `metaculus/snapshots.py` and `env_verify.py` render
  paths, all shipped through prior approved rounds. Redacting `prompt.py` alone would make it the
  sole outlier and render its load failures unactionable. The boundary — content is withheld, paths
  are shown — is now explicit in `CLAUDE.md` § Error hygiene. Recorded as a considered decision so
  the next reviewer does not re-raise it as an oversight.

Deviation — **`prompts/` stays at the repository root** and is not packaged. `pyproject.toml`
ships only `src/whiskeyjack_bot`, so a relative `prompt_path` resolves against CWD and breaks on a
wheel install. M1-601 hit the same problem and solved it by moving migrations *into* the package;
that precedent was deliberately **not** followed here, on owner decision: the prompt is
owner-editable, config-referenced data like `config/x_accounts.yaml`, and the backlog names the
path `prompts/forecaster.md`. Recorded as a known limitation, not an oversight.

Deferred (do not read the absence as an omission):
- **Relative `Path` config fields still resolve against CWD, not the config file's directory** —
  this affects `sqlite_path`, `artifact_root`, `export_root`, `logging.file` and
  `account_allowlist_path` equally, not just `prompt_path`. Fixing it generally is its own
  backlog item; scoping it into M1-401 would have changed the config contract for every path.
- Storing the version/hash *on a forecast row* is **M1-602**'s write path. M1-401 produces the
  two values; nothing persists them yet.
- No new migration and none needed — the columns exist, and editing an applied migration would
  trip `ledger._verify_checksum`'s schema-drift guard.
- No runtime dependency added (`hashlib`, `re`, `pathlib`, `dataclasses` are stdlib), so
  `uv.lock` is untouched and CI's locked-sync step stays green.

## M1-402 — The structured model call

**Acceptance criterion:** *valid response returns typed output; malformed output gets at most one
bounded repair attempt.* The second clause is the whole item, and the pinned package cannot supply
it — see the first decision below.

Scope was settled with the owner before any code, because the epic's four rows overlap if you let
them: **M1-402 owns everything that is not question- or config-dependent.** Field names, types, the
closed vocabularies, and the one cross-field rule the prompt states outright. M1-403 adds the
configured probability bounds, M1-404 the exact supplied-option set, M1-405 the nine exact
percentile levels — each is that row's own stated criterion, and each needs a question or a config
this module deliberately never reads. Three tests pin the boundary from the inside
(`test_configured_probability_bounds_are_not_applied_here`,
`test_multiple_choice_option_identity_is_not_checked_here`,
`test_numeric_percentile_levels_are_not_checked_here`), so the split is visible rather than inferred
from an absence.

### What the criterion is actually guarding against

Not "can we call an LLM". The failure it exists to prevent is a forecast that cost four billable
calls and cannot say why, or one that silently became a *differently shaped* forecast because the
model returned something almost right. A repair is money; an unbounded repair is unbounded money on
exactly the questions where the model is struggling most. So every test in
`tests/unit/test_forecast_generate.py` asserts the **invocation count**, not just the outcome.

### Delivered

- `forecast/schema.py` — the response envelope transcribed from `prompts/forecaster.md:55-101`,
  three `final_prediction` models, `response_model_for()`, and a sanitized
  `validate_forecast_response()`. Imports no provider SDK.
- `forecast/inputs.py` — `build_model_input()` / `render_model_input()`, and `source_id` minting.
- `forecast/generate.py` — `build_forecaster_client()`, the six-check preflight, the call, the one
  bounded repair, and the frozen `ForecastGeneration` result.
- 102 new tests (1461 → 1563). Four gates green; `pytest` 132s.
- No migration, no dependency, no `uv.lock` change, no config-contract change, no prompt edit, no
  edit to any merged module.

### Decision — the SDK's structured-output helper is bypassed, and D03 is why that needs saying

D03 says use `forecasting-tools` where it supports the workflow. `GeneralLlm` **is** used; its
`invoke_and_return_verified_type` is not, and the reasons were read out of the installed 0.2.92
rather than assumed:

- **It is not a repair loop.** On a parse failure `util/misc.py::try_function_till_tries_run_out`
  re-sends the *identical* prompt. Nothing in the package shows the model its own malformed output
  and asks for a correction. The criterion's "repair" does not exist in 0.2.92, so it has to live
  here — that is not a preference, it is the reason the helper cannot be used at all.
- **Its two retry layers multiply.** The constructor's `allowed_tries` (tenacity, 5–60s backoff,
  retrying *any* exception raised inside the call) composes with
  `allowed_invoke_tries_for_failed_output`. At the package defaults one logical request is up to
  **four** billable calls. Measured against the installed package, not inferred from the signature.
- **It discards the raw response**, which M1-406 must persist and replay from.
- **Its failure messages echo everything.** `outputs_text.py:106-119` raises with the full model
  output *and the full input prompt* in the text, and logs both at WARNING before raising. Under
  this project's error-hygiene rule that is a leak channel; the cheapest way to close it is not to
  enter that module.

This is the same shape as M1-302 bypassing `AskNewsSearcher` and M1-303 bypassing `ExaSearcher`,
and it is recorded at the same length for the same reason: a reviewer who cannot see that the
package was actually read will propose the helper as a finding.

### Decision — `model.allowed_tries` is the total number of invocations (owner decision)

The field had **no consumer** before this branch — `grep` for `config.model` in `src/` found one
hit, `api_key_env` inside `secret_env_var_names()` — so M1-402 defines it. It is the total number
of model invocations for one forecast: `repairs_allowed = allowed_tries - 1`. The committed default
of 2 is one call plus at most one repair, exactly the criterion; 1 means no repair, which the
config's own `ge=1` already permits.

The SDK's constructor parameter of the same name is pinned to **1**, so the two layers cannot
multiply and the invocation budget is exactly what this module counts. That layer retries on any
exception, including a timeout *after* generation, which re-bills; `research/transport.py` already
settled that only connection failures may be retried — "a request that reached the server is never
re-sent and so cannot be billed twice" — and the SDK's layer cannot make that distinction.

**Stated rather than left to be found: the criterion's "at most one" holds at the committed
default, not for every configuration.** An operator who sets `allowed_tries: 5` gets four repairs.
The stricter alternative — refusing a value `ModelConfig` itself accepts — was weighed and declined
with the owner: an unread or silently clamped config field is the worse failure, and it is the
shape `docs/LESSONS.md` #7 is about.

### Decision — a provider failure is never repaired

A repair repairs *output*. Re-issuing a call that raised is the transport retry disabled above, so
an exception from the provider ends the attempt at one invocation and this module never re-bills
after one. The exception is discarded unread beyond `isinstance(exc, TimeoutError)` — a provider
error can quote the request, and the request carries the API key in a header. Because litellm
rarely raises the builtin, most timeouts land as `provider_error`; that is a loss of precision,
not of safety, and it is recorded rather than papered over.

### Decision — the prompt is the only schema instruction the model gets

No generated JSON schema is appended and no `response_format` is sent.
`GeneralLlm.get_schema_format_instructions_for_pydantic_type` would put instructions in front of
the model that `forecast_records.prompt_sha256` does not attest to — an attribution hole in the one
column built to close it. `response_format` is forwarded to litellm, which sets
`drop_params = True` and **silently discards** it when a provider does not support it; a guarantee
that can vanish without a signal is not one.

That makes prompt/schema drift a real risk, so it is pinned mechanically:
`test_the_prompts_own_examples_validate` parses the JSON examples out of `prompts/forecaster.md`
and asserts each composes into a value these models accept. Its companion,
`test_the_prompt_examples_would_notice_a_drift`, asserts the composition *fails* without the
prompt's own non-binary priors rule — so the first test is evidence that the schema enforces the
prompt rather than evidence that it accepts anything.

The same reasoning chose the message shape: the hashed prompt file is sent **verbatim as the system
message** and the reasoning packet as the user message, so `prompt_sha256` is the digest of exactly
the instructions the model was given, with no separator this module invented spliced in.
`test_the_system_message_is_the_hashed_prompt_verbatim` recomputes the digest from what the client
was actually handed.

### Decision — `source_id` is minted over the ledger's own order, not the packet's

The prompt asks for documents carrying "a stable `source_id`" and cites them as `src-001`. Nothing
upstream has one: `ResearchDocument` has no such field, and `document_id` is a writer-minted UUID
that `packet_sha256` deliberately excludes, so it identifies *when a row was written* rather than
what the evidence is.

Ids are assigned over the documents sorted by `dedup.dedup_key` — the ledger's own
`UNIQUE (retrieval_run_id, canonical_url, content_sha256)`, and the same total order
`packet_sha256` sorts by. **`ResearchPacket` keeps its tuples in supplied order on purpose**
("order is not part of packet identity", `packet.py:131-133`), and replay reads documents back
ordered by `canonical_url, content_sha256`, so anything keyed on the tuple order would assign
different ids to the same evidence after a round trip and every citation in a stored forecast would
become unresolvable. `build_model_input` returns the
`source_id -> (document_id, canonical_url, content_sha256)` mapping so M1-406/M1-602 can persist
the resolution; nothing here writes it.

Rejected: `content_sha256[:12]`, which is order-independent but costs tokens on every citation and
is not the form the prompt's example shows.

### Decision — cost is captured here, and `0.0` is recorded as unknown (owner decision)

`invoke()` discards cost; the only route is `MonetaryCostManager`, whose `None` the package coerces
to `0.0` — so at the source, zero and untrackable are indistinguishable. A reading of exactly `0.0`
is therefore recorded as **`None`**. The settled rule (`docs/M1-303-NOTES.md`, round 3) is that
`cost_usd is None` means *unknown, never free*, and recording a real spend as `0.0` would undercount
against `run_limits.max_cost_usd` on exactly the runs most likely to be retried. The accounting
shape is `exa.py:674-681,784-796` verbatim: attempts counted the moment a call is about to be
issued, and a total published only when every attempted call reported a usable figure.

Captured here rather than deferred to M1-406 for the reason M1-306 opens the run row before the
calls: a spend not recorded when it happens is not recoverable afterwards. The manager is entered
**inside the coroutine** rather than in the synchronous wrapper, because it tracks through a
`ContextVar` and a task copies its context at creation.

`hard_limit=0` (no limit) is deliberate: passing `run_limits.max_cost_usd` would give budget
enforcement for free, and that is **M1-504's** row.

### Decision — the public API is synchronous, and a running loop is refused

`GeneralLlm`'s invoke methods are `async def`, and `grep -rn "async def|await |asyncio"` over `src/`
and `tests/` returned **zero** before this branch; `research/exa.py:56` cites "async/aiohttp in an
otherwise synchronous pipeline" as a reason to bypass a forecasting-tools helper. `generate_forecast`
drives the coroutine with `asyncio.run()` and adds no dependency — no `pytest-asyncio`, so no
`uv.lock` change and no dependency claim.

Importing `GeneralLlm` runs `nest_asyncio.apply()`, which would probably let `asyncio.run` succeed
inside a running loop anyway. Resting correctness on a third-party monkeypatch of the global event
loop is not a thing to do quietly, so that case is refused explicitly with this module's own error.

### Decision — nothing here writes to the ledger

`generate_forecast` returns a value, the way M1-203's `DeferralEvent` is an in-process value rather
than a row. Two reasons beyond precedent: there is no production caller yet (no `run` command
exists), and `004_pipeline_failure_events.sql:150` refuses a failure row under an `attempt_id` that
later produced a successful `forecast_records` row — so a first response that a repair then fixes
has **nowhere to go as an event**. Classifying it and handing it back is the only shape that does
not poison the caller's later insert. `failure_code` is already in
`lifecycle.PreForecastFailureCode`'s vocabulary so a caller writes `generation_failed` without
re-deriving it, and a test pins that.

### Decision — the sanitizer collects nested field names, unlike `research/model.py`'s

`research/model.py::_sanitize` keeps only the top-level `model_fields`, which is right for its two
flat models. This response is four levels deep, so the same rule applied naively renders
`<withheld>.<withheld>` and every nested diagnostic becomes unusable — an error nobody can act on
is its own failure mode, which is the argument the M1-401 path carve-out already made. Widening it
stays safe because the set is still schema-authored only: a key the model invented is in no
`model_fields` anywhere, so it is still withheld.

### Deviation — two fields are sent that the prompt's "Inputs" list does not name

`group_parent_title` and `unit_of_measure`. Both are departures from a list this module otherwise
treats as a contract, and both were taken because omitting them risks a wrong forecast rather than
a missing one:

- M1-202 lifts the group parent's title out of the post specifically so "the forecaster always
  receives what is actually being asked"; an unpacked subquestion's own title can be a bare option
  label ("September 2024"), which is not self-describing.
- A numeric question is asked for percentile *values*, and a value has no meaning without its unit
  while the bounds it must respect are printed in that unit. Sending the bounds and withholding
  what they measure invites a forecast on the wrong scale.

Both are candidates for the prompt's next version bump, which this branch does not make: the prompt
is at v1.1.0 with a digest pinned in `RELEASED_PROMPT_SHA256`, and changing a byte fails CI until
the version is bumped and a new digest pinned.

### Deviation — `source_disagreements` is read as a list of strings

The prompt prints it empty and never gives it an item shape. Its two unshaped neighbours in the
same object, `failure_modes` and `uncertainty_notes`, are lists of strings by example, so it is
read the same way. An ambiguity resolved by consistency rather than by the stricter-reading rule,
because both readings are equally strict: giving it an object shape the prompt does not print would
*fail a model that followed the prompt exactly*, which is the worse error.

### Rejected — options weighed and not taken

- **`invoke_and_return_verified_type`.** Cannot meet the criterion; see above.
- **Requiring a binary response to supply a prior.** The prompt states only the converse ("if the
  question is not binary, they must be null"). Presence requirements over the attribution fields are
  **M1-501's** row, and `test_a_binary_response_may_carry_priors` pins that both spellings validate
  here.
- **Composing the model string as `f"{provider}/{name}"`.** `model.name` is already the full
  LiteLLM string, so that yields `openrouter/openrouter/...`. `provider` is recorded, not composed —
  which leaves the two free to disagree, so a self-contradiction is refused instead (a `/`-prefixed
  name whose first segment is not `provider`). Nothing enumerates litellm's prefixes: a bare name
  carries no claim to check, and `test_a_model_name_with_no_prefix_carries_no_claim_to_check` pins
  that it still runs.
- **Fixing `questions/__init__.py`.** Its re-export block makes *any* importer of the canonical
  question model load `forecasting_tools`, `litellm` and `streamlit` — the same condition M1-308
  round 4 fixed for `research/__init__.py`. It is pre-existing on the diff base with no live symptom
  (`env_verify` and `cli` import no canonical question, and the existing probe fails if that
  changes), so it is filed as **M1-204** rather than edited on a review branch.
- **Hiding the parsed forecast from the result repr.** `request` and `raw_responses` are
  `repr=False` — a whole research packet and unvalidated model output. The parsed forecast is not:
  it is the auditable product D24 asks for, bounded by the schema, and `ResearchDocument` keeps its
  repr while holding arbitrary provider text. A result object that cannot be read in a debugger is
  its own defect.

### On the property tests, and the check that they discriminate

Nine properties, and every one was run against a mutated source before being trusted
(`docs/LESSONS.md` #5). Eight mutations were caught: the sanitizer returning pydantic's own
rendering, the sanitizer trusting every `loc` part, `source_id` following supplied order, the
renderer using the in-memory dump, the renderer dropping `ensure_ascii`, an unbounded repair loop,
a retried provider error, and a `0.0` cost recorded as free.

Two mutations were **not** caught, and both are worth recording because the reason is not a gap:
`include_input=True` alone and rendering `err["input"]` alone are each *inert*. Withholding the
input makes rendering it harmless; rendering nothing makes including it harmless. Only the pair
leaks — and the pair **is** caught. A mutation that expresses half a defect proves nothing about
the property, which is the `docs/LESSONS.md` #9 lesson from the other direction.

The leak property is written as **invariance, not substring absence**, and that is the other thing
worth reading. "The generated value does not appear in the message" cannot be made to work over
hostile text: a draw can be a single character, and `"0"` is a substring of essentially every
string this repository produces, so the test fails for reasons unrelated to leaking — the same trap
as M1-308 round 5's `tmp_path` assertions. So the same mutation is applied twice with two different
markers and the whole observable outcome must be byte-identical. A message that echoed any part of
the offending value could not satisfy that.

**The property suite found one defect the unit tests did not:** `response_model_for` reached
`dict.get` with an unhashable argument and let a raw `TypeError` escape — the exact "every malformed
shape must arrive as the module's own error type" rule that has been a review finding in this
project twice. Fixed with an exact-type gate before the lookup.

### Deferred (do not read the absence as an omission)

- **The `run` CLI command.** Adding one would invent a command shape M1-602 owns; nothing calls
  `generate_forecast` in production yet.
- **Persisting the raw response, the model settings and the cost** — M1-406, which will need
  migration `007` (`006` is claimed by M1-607). The result object carries all three so that item
  does not have to reopen this module.
- **Structured-data observations**, which the prompt's Inputs list names as optional. M1-304 has
  not shipped a distinct observation shape; structured documents arrive through the same
  `ResearchDocument` schema and are already in `research_documents`.
- **Budget enforcement** against `run_limits.max_cost_usd` — M1-504's, and the reason
  `MonetaryCostManager` is opened with no limit.
- **Resolving `source_ids` against the documents actually supplied** — M1-501's "evidence links".
  This module mints the ids and returns the mapping; it does not check that the model cited real
  ones.
- **Naive timestamps on a canonical question.** `open_time` / `close_time` /
  `scheduled_resolution_time` are passed through from the SDK untouched by `normalize.py`, so they
  may be naive and are rendered as they are. Tightening that is M1-201's model, not this renderer's.
  `as_of_utc`, which this module owns, is required aware and normalized to UTC.

### Standing risk — not verifiable offline

- **No real provider call happens anywhere on this branch.** The suite blocks sockets, so the
  `GeneralLlm` seam is exercised only against a recording double. What the tests establish is the
  call shape, the invocation bound and the hygiene; that the pinned SDK behaves as its source reads
  was verified by reading and executing 0.2.92 locally, not by a live call.
- **Cost capture is best-effort.** It depends on litellm's callback reaching our
  `MonetaryCostManager` through a `ContextVar`. If it does not, `current_usage` stays `0.0` and we
  record `None` — the failure mode is "unknown", which is the safe direction, but it is not proof
  that cost was captured.
- **`GeneralLlm` opens its own nested `MonetaryCostManager(1)` around every call**
  (`general_llm.py:271`), a $1-per-call ceiling this module neither sets nor can turn off.
- **`general_llm.py` logs the full prompt and the full response at DEBUG.** At the committed
  `logging.level: INFO` nothing is emitted, but an operator who raises it to DEBUG puts the whole
  research packet and the model's output in the log file. No credential is involved; it is content,
  and it is the pinned package's behaviour rather than this module's.
- **Importing `GeneralLlm` runs `nest_asyncio.apply()`**, monkeypatching the global event loop for
  the process, and costs ~6.3s and a Streamlit import. Confining the import to `generate.py` is what
  keeps that off the startup path; `tests/unit/test_env_verify.py`'s probe is what keeps it there.
