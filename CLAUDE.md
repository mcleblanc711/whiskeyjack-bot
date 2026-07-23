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
3. `docs/backlog/backlog.csv` — issue-level acceptance criteria (mirror of the `.xlsx`).
   `docs/backlog/decisions.csv` — the `D##` decisions referenced throughout the code.
4. `docs/M0-REVIEW.md`, `docs/M1-NOTES.md` — running record of what shipped and what deviated.
5. `config.example.yaml` — the configuration contract.

## Toolchain

Python 3.11, `src/` layout, `uv`. The full gate — run all four before calling anything done:

```bash
uv run pytest              # offline; sockets are blocked
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
```

CI (`quality-gate`, required on master) runs these plus a gitleaks full-history scan, a
tracked-artifact hygiene check, `uv sync --locked`, and a CLI smoke test. `uv.lock` must stay in
step with `pyproject.toml` or the locked sync fails.

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
- Parallel tracks use **git worktrees** (`../whiskeyjack-<item>`), one branch each. Each worktree
  needs its own `uv sync` — `.venv` is gitignored and per-directory. Your main checkout stays on
  `master`; you `cd` between worktrees rather than switching branches.
- Branch → PR → **GPT cross-model review** (write a `GPT_REVIEW_REQUEST_<item>.md`: spec,
  deliberate choices, risk areas to pressure-test, full branch diff) → address findings → merge.
- Record what shipped, decisions, and deviations in `docs/M1-NOTES.md`.
- **Stop points at end of M0 and end of M1** — summarize and get explicit owner go-ahead.

### Backlog status

Vocabulary: `Not Started` → `In Review` (PR open) → `Done` (**at merge**, not when code lands).
`Blocked` for owner-gated items. Update **both** `docs/backlog/backlog.csv` and the `.xlsx`
(often open in LibreOffice — check for a `.~lock` file first).

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
  branches each adding `002_*.sql` will collide. Agree the number before starting.
- **`uv.lock` serializes tracks.** Any item adding a dependency (AskNews, Exa) conflicts messily
  with another doing the same. Don't run two dependency-adding items concurrently.
- **The backlog CSV/xlsx predates `CLAUDE_CODE_PROMPT.md`**: M1-307, M1-308 and A-1106 exist in
  the brief but not in the backlog rows. Don't treat the CSV as the complete scope.
- **M1-401 is more than its one-line title** — it also applies the forecaster-prompt v1.1.0 patch
  from `CLAUDE_CODE_PROMPT.md` § B and re-hashes.
