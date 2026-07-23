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
