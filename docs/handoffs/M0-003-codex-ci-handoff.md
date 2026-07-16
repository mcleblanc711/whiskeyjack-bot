# M0-003 handoff — CI quality gate (Codex)

To: Codex (independent verification agent, CI owner)
From: Claude (implementer)
Date: 2026-07-15
Scope: **M0-003 only** — the CI quality gate. This is the standing cue referenced
in `docs/M0-REVIEW.md` that the Claude-side M0 slice is complete and reviewed
(cross-review rounds 1–3 all merged; suite green at 85 tests), so the CI gate can
be built. It does **not** hand you the acceptance/contract suites — those are
separate task IDs governed by the blind-authorship rule (see "Out of scope").

## The task

Backlog `M0-003` (owner: Codex, dep: `M0-001`, source: D15):

> Add CI quality gate — pull requests run deterministic offline checks and publish
> a clear pass/fail result.

Per `CODEX_PROMPT.md` § "Your task subset": deterministic, fully offline — unit +
schema tests, formatting, type checks on Python 3.11 — and **CI must fail on any
live network attempt** so an accidental provider call is a red build, not a silent
cost.

## What CI must run

Every check below already passes on `master` today; the gate's job is to make a PR
that breaks any of them a **visible red build**. These are the repo's existing,
documented commands — not new design:

| Check | Command | Notes |
|-------|---------|-------|
| Locked install | `uv sync --locked` | Fails if `uv.lock` drifts from `pyproject.toml`; this is also how the `forecasting-tools==0.2.92` pin (M0-002) is enforced — a drifted pin is a failed resolve. |
| CLI smoke | `uv run whiskeyjack-bot --help` | M0-001 acceptance; catches a broken entry point / import-time error. |
| Unit + schema tests | `uv run pytest` | 85 tests, all offline. |
| Lint | `uv run ruff check .` | |
| Format | `uv run ruff format --check .` | Check-only; do not auto-format in CI. |
| Types | `uv run mypy --strict src` | |

- **Python 3.11.** `.python-version` pins `3.11`; `uv` provisions its own CPython
  (currently 3.11.15) — CI must not rely on a system interpreter (the dev box only
  has 3.12). `requires-python = ">=3.11,<4.0"`.
- **Pass/fail must be legible on the PR** — a single required status (or a small
  matrix) with an unambiguous check name, per the D15 acceptance criterion.

## The non-negotiable: fail on any live network attempt

This is the property that makes the gate worth having. A test session that reaches
a non-loopback address must turn the build **red**, not spend money quietly.

- A Claude-side honesty check already exists: `tests/unit/conftest.py` has an
  autouse fixture that monkeypatches `socket.socket.connect` to raise on any
  connection. It is deliberately labelled the *implementer's* honesty check, **not**
  the enforcement mechanism — do not treat its presence as satisfying M0-003.
- **You own the CI-level enforcement.** Choose the mechanism (e.g. a session-wide
  socket block that permits only loopback, a locked-down runner, etc.) — I am not
  prescribing the implementation, only the property: *a non-loopback connection
  attempt during the CI test session is a failing build.* Include a canary that
  proves the guard fires (a test that attempts a real connection and asserts the
  guard trips), so the enforcement can't silently regress into a no-op.

## Repo hygiene the gate should enforce (from `CODEX_PROMPT.md` § "CI + repo hygiene")

- **Secret scan** — fail the build on token-shaped strings; `data/` and env files
  stay ignored; no generated artifacts tracked. (The codebase's rule is env-var
  *names* only, never values — a value-shaped literal is a finding.)
- **Dependency-pin drift** is a visible failure — `uv sync --locked` above backs
  this; `tests/unit/test_dependency_pins.py` is the in-suite assertion.
- Anything requiring credentials is excluded from CI. The `bot-testing-area` smoke
  test (M2-706) is owner-run and **never** in CI.

## Clean seams already in place (build against these; nothing is missing)

- `pyproject.toml` — `[project.scripts] whiskeyjack-bot`, `requires-python`, the
  `forecasting-tools==0.2.92` pin, and `[tool.ruff]` / `[tool.mypy]` config.
- `uv.lock` present and resolving on 3.11.15; `.python-version` pins 3.11.
- `tests/unit/` — 7 files, 85 tests, all offline, all green on `master`.
- No `.github/workflows/` exists yet — the gate is greenfield; you are not editing
  around an existing pipeline.

If any of these seams is not clean enough to build the gate on, file it as a
divergence rather than working around it — I'll fix it on the implementer side.

## Out of scope for M0-003 (do not fold these in)

These are separate task IDs and, where noted, bound by the blind-authorship rule
(write from spec, not from my implementation):

- **Acceptance / contract suites** — T-901 (golden schema fixtures), T-902 (mocked
  Metaculus integration), T-903 (dry-run acceptance), T-904 (numeric CDF contract),
  T-905/T-906 (X adapter + M1-307/308). Authored blind from the spec; M0-003 only
  *runs* whatever offline tests exist — it does not require these to exist first.
- **M1-605** secret/trace redaction audit and **M2-705** response-capture spike.

M0-003 can land and be green against the current unit suite alone; the acceptance
layers plug into the same gate as they arrive.

## Deliverable & workflow

- One branch for `M0-003` (`CODEX_PROMPT.md` § "Deliverable format": one branch per
  task ID). Owner merges.
- File any spec-vs-implementation divergence you hit as an issue quoting the
  violated spec sentence — do not silently adjust around it.
- On merge, flip `M0-003` to Done in `docs/backlog/backlog.csv` and note the gate
  in `docs/M0-REVIEW.md`.

## What the owner can verify before/after

```bash
# the checks the gate wraps, run locally against master:
uv sync --locked
uv run whiskeyjack-bot --help
uv run pytest                       # 85 passed, offline
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
# after the gate lands: open a throwaway PR that (a) adds a live-network test and
# (b) drifts a pin — both must turn the required check red.
```

M0-003 closing does **not** by itself pass the M0 gate: the independent
verification run (acceptance/contract authorship) and the owner's stop-point
approval remain outstanding, per `docs/M0-REVIEW.md`.
