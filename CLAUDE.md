# whiskeyjack-bot

A Metaculus MiniBench forecasting pipeline whose primary product is an **attribution
instrument**: an immutable, replayable record of every forecast, its evidence, and its outcome.
Competing is the venue; attribution is the point. **When a shortcut would weaken the ledger, the
approval boundary, or replayability, do not take it.**

## Read before writing code

1. `CLAUDE_CODE_PROMPT.md` — the Claude Code brief. **Amends** the handoff (owner split; adds
   M1-307/M1-308 X-retrieval scope). Where they conflict, this wins.
2. `CODEX_HANDOFF.md` — full spec: interfaces, ledger design, submission seam, pipeline
   boundaries, test requirements, prohibited claims.
3. `docs/backlog/backlog.csv` — issue-level acceptance criteria, and the single source for
   backlog state. `docs/backlog/decisions.csv` — the `D##` decisions referenced throughout the code.
4. `docs/M0-REVIEW.md`, `docs/M1-NOTES.md` — running record of what shipped and what deviated.
5. `docs/TRACKS.md` — who currently holds the dependency-adding item and the next free migration
   number. Read it before starting a worktree; claim yours there.
6. `config.example.yaml` — the configuration contract.

## Toolchain

Python 3.11, `src/` layout, `uv`. The full gate — run all four before calling anything done:

```bash
uv run pytest              # offline; sockets are blocked
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
```

CI (`quality-gate`, required on master) runs these plus a gitleaks full-history scan, a
tracked-artifact hygiene check, a backlog lint, a migration hygiene check, a workbook build,
`uv sync --locked`, and a CLI smoke test. `uv.lock` must stay in step with `pyproject.toml` or the
locked sync fails. A second required job, `backlog-status`, gates the Done-flip — see below.

## Code conventions

- `from __future__ import annotations` at the top of every module; fully annotated (`mypy --strict`).
- **Validated models → pydantic v2**, subclassing a strict base (`config._StrictModel`,
  `ConfigDict(extra="forbid")`). Constraints via `Field(...)`, `@field_validator`,
  `@model_validator(mode="after")`.
- **Internal value objects → `@dataclass(frozen=True)`** (see `SnapshotMeta`, `ResolvedTournament`).
- Closed enums are module-level `Literal` aliases, not `enum.Enum`; validate against them at
  runtime with `get_args(...)`.
- Line length 100. No TypedDict/attrs. Subpackages get a one-line-docstring `__init__.py`.
- Each module docstring names its backlog item (e.g. "(M1-201)").

### Error hygiene — project-wide, non-negotiable

Every module owns a sanitized exception (`ConfigError`, `SnapshotError`, `LedgerError`,
`NormalizationError`). The rule: **an error message never echoes stored/file/field values**, and
sanitizing raises use `from None` so an underlying exception cannot reprint a value through its
text or a rendered traceback. Pydantic's own `ValidationError` interpolates the offending input —
always rebuild it with `errors(include_input=False, include_url=False)`.

Callers only handle the module's own error type, so **every malformed shape must arrive as one** —
a raw `AttributeError`/`KeyError`/`ValueError` escaping is a review finding (it has been, twice).

**Filesystem paths are the one carve-out** (settled M1-401 review, owner decision). "Values" means
*content*: file bodies, field values, stored records, secrets. A path is operator-supplied
configuration, not content, and it is the only thing that makes a load failure actionable — a
`cannot read forecaster prompt` with no path cannot be fixed. So paths **are** rendered, uniformly:
`config.py`, `ledger.py`, `metaculus/snapshots.py`, `prompt.py`, `env_verify.py`. The residual risk
is real but bounded — an operator who pastes a secret into a *path* has already written it to their
config file in plaintext. Do not redact paths in one module while the rest render them; a lone
outlier is worse than either consistent policy.

## Hard constraints

- No reachable submission path until M2; `submission.enabled: false` and `dry_run: true` stay the
  committed defaults.
- Never print or persist secrets; env-var **names** only in diagnostics.
- Never persist hidden chain-of-thought; concise auditable rationale fields only.
- Append-only ledger: forecast versions and lifecycle events are never mutated.
- Approval binds to an exact forecast hash; any content change invalidates it.
- Community prediction is **never** a forecaster input in v1.
- Pin `forecasting-tools==0.2.92`; do not float.
- If spec and observed package behaviour conflict, **stop and ask** — do not silently adapt.
- If an acceptance criterion is ambiguous, implement the **stricter reading** and note it.

## Workflow

- **One backlog item per branch**, in dependency order; commit messages lead with the issue ID.
- Parallel tracks use **git worktrees**, one item each, created by
  `scripts/start-item.sh <ITEM> <slug> [--deps]`. **One worktree per item, named for its branch
  (`../whiskeyjack-m1-303` holds `feat/m1-303-…`), created fresh, removed at merge — never reused
  or renamed.** Each gets its own `uv sync` (`.venv` is gitignored and per-directory). Your main
  checkout stays on `master`; you `cd` between worktrees rather than switching branches.
- **Claim deps and migration numbers in `docs/TRACKS.md` before starting** — one dependency-adding
  item per wave, and migration numbers agreed up front.
- **Merge `master` into every active branch daily**: `scripts/sync-worktrees.sh --merge` from the
  main checkout. Reaching a merge 20 commits behind is how one branch pays for all of them at once.
- Branch → PR → **GPT cross-model review** → address findings → merge → `scripts/finish-item.sh
  <ITEM>`. Generate the request with `scripts/review-request.py <ITEM>`; it emits everything
  mechanical and leaves *deliberate choices* and *risk areas* as TODOs for you to write, which is
  the part that decides whether the review takes one round or six. It **runs the four gates and
  refuses to emit anything if one fails** — it used to assert they passed without checking, which
  is how a review request could open with a falsehood. `--no-verify` skips them and says so in the
  output. `GPT_REVIEW_*` files are gitignored scaffolding — never commit them.
  Do **not** run `gh pr merge --delete-branch`: it fails while the branch is checked out in a
  sibling worktree. Merge, then `finish-item.sh`.
- **Fuzz pure functions before the first review.** Any hash, tiebreak, canonicalizer or validator
  gets a `tests/property/` pass asserting: never raises outside the module's own error type; a
  total order wherever ordering is claimed; replay-stability across the persisted form
  (`model_dump(mode="json")` → `json.dumps(ensure_ascii=True, sort_keys=True)` → load); and no
  value leak in any message. M1-305's tiebreak took five review rounds on one function for three
  properties a single local run finds.
- Record what shipped, decisions, and deviations in `docs/M1-NOTES.md`.
- **Stop points at end of M0 and end of M1** — summarize and get explicit owner go-ahead.

### Backlog status

Vocabulary: `Not Started` → `In Review` (PR open) → `Done` (**at merge**, not when code lands).
`Blocked` for owner-gated items.

`docs/backlog/*.csv` are the **single source**. The `.xlsx` is a build output — untracked,
gitignored, rebuilt on demand with `uv run python scripts/backlog_xlsx.py`. Never hand-edit it;
edits there are discarded on the next rebuild. (It was tracked and hand zip-patched until the
workflow-hardening change; that drifted from the CSV once and made every status flip a two-file
edit.) CSVs are CRLF and marked `-text` — rewrite them with a `csv.writer` using
`lineterminator="\r\n"`, or every row shows as changed.

**Flip the row to `Done` on the branch, before the merge.** CI's `backlog-status` job classifies
the branch name into one of three buckets, and **the third one fails**:

- **Item branch** — `feat|feature|fix|bugfix|hotfix` + `/<item>-<slug>`, matched case-insensitively.
  The row must read `Done`. Expect this red for most of a branch's life; it is a checklist item,
  which is why it is a separate job from `quality-gate`. An item with no backlog row fails
  outright — add the row, with acceptance criteria, before opening the PR.
- **Infrastructure** — `chore/ ci/ build/ deps/ dependabot/ docs/ refactor/ release/ revert/ test/`.
  Skipped, along with draft PRs.
- **Anything else** — **fails**, telling you to rename the branch or add the prefix to
  `SKIP_PREFIXES` in `.github/scripts/check_backlog.py`. The first version of this gate skipped
  whatever its pattern missed, and its pattern was anchored to lower case and to `feat|fix`, so
  `feat/M1-303-x` and `feature/m1-303-x` both reported success on a `Not Started` row. A gate
  against forgetting a step must not itself skip silently. `tests/unit/test_check_backlog.py`
  holds the branch-name table; add a case there when you change the rules.

**master requires branches to be up to date before merging** (`required_status_checks.strict`).
That is not bureaucracy: `check-migrations.sh` compares against the `origin/master` of *its own
run*, so with stale checks allowed, two PRs off the same base could each add an `003_*.sql`, both
go green, and the second merge on its old result. Keep syncing daily and the requirement costs
nothing; turning it off silently reopens that collision.

### Owner split

Claude Code owns the judgment-heavy seams (normalization, retrieval, forecast generation,
validation, ledger writers, submission). **Codex owns** M0-003, M1-605, M2-705, T-901–T-904, and
independent acceptance-test authorship — they write those from spec *without reading the
implementation*. Write the minimum unit tests to keep yourself honest; **do not pre-write Codex's
tests.**

## Gotchas

- **`DiscreteQuestion` subclasses `NumericQuestion`** in the pinned SDK. Dispatch on the
  `question_type` literal, never `isinstance` — otherwise an unsupported type silently normalizes
  as numeric (a wrong forecast, not an error). See `questions/normalize.py`.
- **Migration numbers are claimed globally.** `ledger.py` rejects duplicates, so two parallel
  branches each adding `003_*.sql` collide — and if the filenames differ (`003_alpha.sql` vs
  `003_beta.sql`) git merges both cleanly and the collision only appears at runtime. Claim the
  number in `docs/TRACKS.md` before writing the file. `.github/scripts/check-migrations.sh` gates
  uniqueness, contiguity, and the immutability of any migration already on master — but only
  because master requires up-to-date branches; the gate is blind to a collision it never sees.
  The `TRACKS.md` claim is advisory (it lives on a branch until that branch merges), so the
  check is the enforcement, not the registry.
- **`uv.lock` serializes tracks.** Any item adding a dependency (AskNews, Exa) conflicts messily
  with another doing the same. One dependency-adding item per wave, claimed in `docs/TRACKS.md`.
- **M1-401 is more than its one-line title** — it also applies the forecaster-prompt v1.1.0 patch
  from `CLAUDE_CODE_PROMPT.md` § B and re-hashes.
- **`content_sha256` raises on a lone surrogate** (`hashing.py`, found by `tests/property/`).
  Lone surrogates reach the schema from provider JSON, so an adapter hashing provider text can
  crash with an unsanitized `UnicodeEncodeError` that quotes the offending character. Open —
  awaiting an owner decision; see the xfail in `tests/property/test_canonical_properties.py`.
