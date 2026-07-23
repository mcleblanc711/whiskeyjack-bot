# Cross-model review request — whiskeyjack-bot M1-401

You are a rigorous senior reviewer performing an independent cross-model review of
code authored by another AI model (Claude). Apply the **stricter reading**: when a
line could be read as either correct or subtly wrong, assume the wrong reading and
prove it can't happen from the diff. Do **not** rubber-stamp. If you approve, justify
why each risk area below is actually safe; if you don't, list blocking findings.

## Project context

`whiskeyjack-bot` is a public Metaculus MiniBench forecasting pipeline whose primary
product is an **attribution ledger**: an immutable, replayable SQLite record of every
forecast, its evidence, approvals, submission attempts, resolutions and scores. Python
3.11, `src/` layout, offline-first (tests run with sockets disabled), toolchain gates
are `pytest`, `ruff check`, `ruff format --check`, `mypy --strict src`.

This is **M1-401**, the first piece of the Forecast Generation epic. It gates **M1-402**
(the structured model call) and, through the two `NOT NULL` columns it feeds, **M1-602**
(the forecast writer). It builds on **M1-601** (the ledger migration) and **M1-301** (the
research-document schema), both of which you reviewed and approved.

## Authoritative spec

From `docs/backlog/backlog.csv` (M1-401 row):

> **Version and hash the prompt.** Load `prompts/forecaster.md`, verify declared version
> and compute a content hash.
> **Acceptance:** "Every forecast stores prompt version/hash; changed bytes produce a new
> hash."
> Depends on M0-005. Reference: D4; `prompts/forecaster.md`

Decision **D04**: *Freeze attribution schema before first submission.* Rationale: prevents
missing pre-resolution data and hindsight contamination. Why it binds here — the prompt
version and hash are part of that frozen schema, and neither is reconstructable after the
fact: you cannot recover which prompt text produced an earlier forecast. `forecast_records.
prompt_version` and `prompt_sha256` have been `NOT NULL` since migration 001, so M1-602
cannot write a single row until this item exists.

**The item is larger than its one-line title.** `CLAUDE.md` requires M1-401 to also apply
the forecaster-prompt **v1.1.0 patch** from `CLAUDE_CODE_PROMPT.md` § B and re-hash. That
patch is in this branch.

Standing conventions this branch must honor:

- **Error hygiene**: messages never echo stored or file values, and sanitizing raises use
  `from None` so a value cannot surface through exception text or a rendered traceback.
  A prompt file is a plausible home for a mistakenly pasted credential.
- Every malformed shape must arrive as the module's **own** error type; a raw `OSError`/
  `UnicodeDecodeError`/`ValueError` escaping to a caller that only handles `PromptError`
  is a defect. This has been a finding twice before on this project.
- **Ambiguity rule**: where an acceptance criterion is ambiguous, implement the stricter
  reading and note it.
- **Append-only ledger**; no reachable submission path (unaffected here, but no code may
  weaken it).

## Deliberate choices / out of scope (challenge the rationale, but these are not omissions)

- **The digest is over raw file bytes, unnormalized.** It deliberately does *not* reuse
  `research/hashing.py::content_sha256` (which you reviewed in M1-301), whose pinned rule
  applies Unicode NFC and collapses whitespace runs. That rule is correct for research
  documents — two renderings of one article are the same evidence — and wrong for a
  prompt: a reflow changes what the model actually sees, and the acceptance criterion says
  "changed **bytes**". The precedent followed is `ledger.py:183`, which hashes
  `read_bytes()` before decoding. **Challenge this if you think one hash rule should serve
  both.** There are now four distinct sha256 definitions in this repo (migration checksum,
  research content, prompt bytes, and later forecast/payload hashes); the module docstring
  enumerates them.
- **`prompts/` stays at the repository root, unpackaged.** `pyproject.toml` ships only
  `src/whiskeyjack_bot`, so a relative `prompt_path` resolves against CWD and breaks on a
  wheel install. M1-601 hit this and solved it by moving migrations *into* the package.
  That precedent was deliberately **not** followed here, on an explicit owner decision: the
  prompt is owner-editable, config-referenced data like `config/x_accounts.yaml`. Recorded
  as a known limitation in the notes. **Challenge the decision if you think it is wrong,
  but it was made by the owner, not by the implementer.**
- **Relative-path resolution is not fixed here.** It affects `sqlite_path`,
  `artifact_root`, `export_root`, `logging.file` and `account_allowlist_path` identically;
  fixing it generally changes the config contract for every path field and is its own
  backlog item.
- **Nothing is persisted.** M1-401 produces the two values; writing them onto a forecast
  row is M1-602's write path. No migration is added — the columns exist, and editing an
  applied migration would trip `ledger._verify_checksum`'s schema-drift guard.
- **`env_verify` was extended** with a startup version cross-check. This is the stricter
  reading of "verify declared version" and was an explicit owner decision; the alternative
  (verify only at load time) was on the table.
- **Bare semver is canonical.** The prompt H1 carries a `v` prefix, config does not. The
  parser strips it; config rejects a prefixed value. A mismatch is a hard error, never a
  coercion.
- **No runtime dependency added** (`hashlib`, `re`, `pathlib`, `dataclasses` are stdlib),
  so `uv.lock` is untouched.

## Risk areas to pressure-test

1. **The hash rule is the item's correctness property.** The claim is that
   `load_prompt(...).sha256` is exactly `sha256(file bytes)` with nothing in between —
   verified out-of-band: `sha256sum prompts/forecaster.md` returns
   `7ce2e9ea2a6df73e90e224bafc7402071f16878339cd177f57cad135516958da`, matching the loader.
   **Find a path where a byte change does *not* change the hash, or where an unchanged
   file hashes differently across platforms.** Line endings are the obvious candidate:
   is there any git attribute, editor setting, or `read_bytes` behaviour that could make
   the same logical prompt hash differently on another machine — and if so, is that a
   defect or the intended strictness? Note `.gitattributes` marks `docs/backlog/*.csv` as
   CRLF but says nothing about `prompts/`.

2. **The version parse is anchored to line 1 on purpose.** The prompt body contains
   `"schema_version": "1.0.0"` inside a fenced JSON example — the *output record* schema,
   an unrelated number. A document-wide semver search matches it and would keep matching
   it, silently and wrongly, once the two diverge. The regex is
   `^#\s+\S.*\bv(\d+\.\d+\.\d+)\s*$`. **Attack it.** Does `.*` with a trailing anchor
   backtrack pathologically on a long first line (ReDoS)? Can a crafted H1 make it capture
   the wrong number — e.g. two versions on one line, where `.*` is greedy and takes the
   last? Is `\b` correct given the preceding `v`? What does it do with a BOM, a CRLF file,
   or an H1 that is legitimately absent because the file is empty?

3. **Error hygiene under a prompt containing a secret.** Every raise in `prompt.py` is
   meant to be constant or to interpolate only provably-safe values. `load_prompt`
   re-validates `expected_version` as bare semver *before* the mismatch message
   interpolates it, specifically so an arbitrary caller string cannot reach a diagnostic.
   **Try to find a path where prompt content, or an unvalidated caller value, reaches an
   exception message or a rendered traceback.** `parse_declared_version` is public and can
   be called directly — is its error safe? The `OSError` handler interpolates `path`: is
   the path itself ever attacker- or content-derived?

4. **`env_verify._verify_prompt_version` catches `PromptError` and appends `str(exc)` to
   `filesystem_problems`, which `render()` prints.** This is the one place a `PromptError`
   message is guaranteed to reach stdout. If risk area 3 has any hole, this is where it
   becomes a leak. Also: is `filesystem_problems` the right bucket — it maps to
   `EXIT_ENV_MISSING` (3), not `EXIT_CONFIG_INVALID` (2), and a version mismatch is
   arguably a config problem. Does that miscategorization matter to an operator or script?

5. **Hashing happens before decoding, deliberately**, so a file that fails UTF-8 decoding
   still has a well-defined identity and no decode step sits between the file and its
   recorded hash. But `load_prompt` then raises on the decode failure, discarding that
   digest. Is computing it first dead work, or does the ordering matter for a reason worth
   keeping? Would a reviewer expect the digest to be *returned* for an undecodable file?

6. **The config validator's blast radius.** `ForecastConfig.prompt_version` now rejects
   anything but bare `MAJOR.MINOR.PATCH`. That is a **breaking change to the config
   contract** for any existing config using `v1.0.0` or a pre-release suffix. Confirm
   `config.example.yaml` and every test config stayed in step, and judge whether rejecting
   e.g. `1.1.0-rc1` is too strict for a prompt that might legitimately be iterated.

7. **The v1.1.0 patch content itself.** Two bullets were appended to "General rules"
   governing `reliability_tag` weighting and `provenance: llm_reported` load-bearing
   limits. Verified before applying that all four `ReliabilityTag` values and both
   `Provenance` values match the merged M1-301 literals at `research/model.py:74,79`
   exactly. **Check the patch text was transcribed faithfully from `CLAUDE_CODE_PROMPT.md`
   § B** (the source renders the bullets as blockquote lines; the `> ` is quoting syntax,
   not literal text to write into the prompt — confirm that reading is right). Does the
   new guidance contradict any existing rule in the same section, particularly the
   pre-existing "Do not double-count multiple articles reporting the same underlying
   event" versus the new "Multiple unverified accounts repeating one claim remain one
   piece of evidence"?

8. **The drift-guard test loads real repo files.** `test_real_prompt_and_example_config_
   agree` reads the actual `prompts/forecaster.md` and `config.example.yaml` so that
   editing one without the other fails CI. Is coupling a unit test to repo state
   appropriate here, or does it make the suite fragile (e.g. under a wheel install where
   `parents[2]` is not a checkout)? Note the whole test suite already uses this pattern.

9. **`LoadedPrompt.text` carries the full prompt in memory.** It is a frozen dataclass, so
   it will be held by whatever M1-402 builds. Is there a hygiene risk in a value object
   that holds file contents reaching a log, a repr, or a pydantic model dump later? Its
   `repr` is the default dataclass one, which prints `text` in full.

10. **Scope discipline.** M1-301's rounds 4, 5 and 6 each found a defect *introduced by
    the previous round's fix*, and speculative hardening past the acceptance criterion was
    the recurring cause. **Flag anything in this diff that is a guard beyond what the
    acceptance criterion requires** — the `expected_version` re-validation in item 3 is the
    one I added deliberately and can justify; tell me if you find others, or if you think
    that one is itself over-reach.

## What changed

Branch `feat/m1-401-prompt-version-hash`, one commit off `master` (`a0cbb67`).

| File | Change |
|---|---|
| `src/whiskeyjack_bot/prompt.py` | **new** — `load_prompt`, `LoadedPrompt`, `PromptError` |
| `tests/unit/test_prompt.py` | **new** — 23 tests |
| `prompts/forecaster.md` | v1.1.0 patch: H1 bump + two "General rules" bullets |
| `config.example.yaml` | `prompt_version` → `1.1.0` |
| `src/whiskeyjack_bot/config.py` | `ForecastConfig.prompt_version` bare-semver validator |
| `src/whiskeyjack_bot/env_verify.py` | `_verify_prompt_version` startup cross-check |
| `tests/unit/test_config.py`, `tests/unit/test_env_verify.py` | validator + verifier cases |
| `docs/M1-401-NOTES.md` | epic-wide notes file (see below) |

`docs/M1-NOTES.md` is intentionally **byte-identical to master**: M1 is being built across
parallel worktrees and that file is the one every branch would append to. Per-epic notes
merge back into it when the epic completes.

Gates at the tip: `pytest` 348 passed, `ruff check` clean, `ruff format --check` clean,
`mypy --strict src` clean, `uv.lock` untouched.

## Round 2 — response to round-1 findings

All four reproduced before fixing. Three are fixed in code; one is answered with a written
policy. Please re-pressure-test the parser rewrite in particular.

| # | Finding | Response |
|---|---------|----------|
| 1 | `LoadedPrompt` repr exposes the prompt | **Fixed.** `text` is `field(repr=False)`; `version`/`sha256` stay (safe by construction). Two regression tests, including the rendered-locals path. |
| 2 | Version patterns accept malformed/ambiguous semver | **Fixed.** One `BARE_VERSION_RE` in `prompt.py`, reused by `config.py`. `re.ASCII` kills Unicode numerals; `(?:0\|[1-9]\d*)` kills leading zeroes; `fullmatch` replaces `match` + `$`. Ambiguous H1 now **raises** rather than resolving — see note below. |
| 3 | Paths reach rendered diagnostics | **Reviewed, kept, documented.** Not local to `prompt.py`: ~30 sites across `config.py`, `ledger.py`, `snapshots.py`, `env_verify.py` do the same. Redacting one module makes it the sole outlier and makes its failures unactionable. The boundary (content withheld, paths shown) is now explicit in `CLAUDE.md` § Error hygiene, with the residual risk stated. Owner decision. **If you still consider this blocking, the argument to make is why the project-wide sweep is in scope for M1-401** — a prompt.py-only change I will push back on again. |
| 4 | Drift guard misses body drift | **Fixed.** `RELEASED_PROMPT_SHA256` pins `1.1.0` → `7ce2e9e…958da`. Proven by appending one space to the prompt and confirming the test fails. Docstring and `docs/M1-401-NOTES.md` corrected to describe what is actually enforced. |

On finding 2's ambiguity fix specifically: switching to a non-greedy quantifier does **not**
work — with the trailing anchor the engine backtracks to the same last token, so
`v1.1.0 supersedes v2.0.0` still resolved to `2.0.0`. The parser instead collects every
`v<semver>` token in the H1 and raises unless exactly one is present and trailing. This is the
stricter reading per `CLAUDE.md`: two declared versions is drift, not a pick-one situation.
Worth checking whether that rule is too strict for any legitimate H1 you can construct.

## How to get the diff

```bash
git fetch origin feat/m1-401-prompt-version-hash
git diff master...origin/feat/m1-401-prompt-version-hash
```

Review the full diff, not just the summary above.
