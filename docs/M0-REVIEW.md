# Milestone 0 stop-point review

Written 2026-07-13 at the mandatory M0 stop point (brief § Workflow 2).
**M1 work has not started and will not start without explicit owner go-ahead.**
This document is also Codex's signal to begin T-901 and the CI gate (M0-003):
schemas and seams they depend on now exist.

## What was built

| Issue | Delivered | Acceptance evidence |
|---|---|---|
| — (housekeeping) | Canonical layout: `prompts/forecaster.md`, `config/x_accounts.yaml`, backlog CSVs in `docs/backlog/`, `.gitignore`, `retrieval.social` block added to `config.example.yaml` | `chore/repo-layout` branch |
| M0-001 | Python 3.11 src-layout package `whiskeyjack_bot`, uv-managed venv/lock, argparse CLI, MIT licence | Clean clone: `uv sync` + `whiskeyjack-bot --help` pass |
| M0-002 | `forecasting-tools==0.2.92` exact pin + pydantic/pyyaml; dev: pytest/ruff/mypy; drift unit test | `test_dependency_pins.py`; lock resolves on CPython 3.11.15 |
| M0-005 | Typed config, `extra=forbid`, placeholder rejection (D27), live-submit combinations rejected outright pre-M2, sanitized `ConfigError` (input values withheld) | 21 tests in `test_config.py` |
| M0-004 | `verify-env`: config validation, dir creation, env-var presence by **name only**; exit codes 0/2/3 | `test_env_verify.py`; manual matrix: no vars → 3, all vars → 0 with zero value leakage, live-submit config → 2 |
| M0-006 | README quick start | Executed verbatim from a fresh clone: sync, verify-env, offline fixture fetch, 56 tests green with zero credentials |
| M0-101 | Single `MetaculusClient` construction point; missing token → `MissingCredentialError` naming the env var, no network; JSON logging with value-redaction filter across all loggers | `test_metaculus_client.py` |
| M0-102 | Live fetch via verified `get_all_open_questions_from_tournament(id, "unpack_subquestions")`; fixture mode default; D31 alias check logs, never adopts silently | `test_fetch.py` |
| M0-103 | Versioned snapshot envelope; typed round-trip retaining question/post/tournament IDs and raw `api_json`; committed synthetic fixtures + sample snapshot | `test_snapshots.py` |
| M0-104 | `questions fetch --tournament bot-testing-area` targets smoke tournament; config untouched | `test_fetch.py::test_cli_override_wins_without_config_change` |

Suite: **56 tests, all passing offline with sockets blocked** (autouse guard in
`tests/unit/conftest.py` — my honesty check; the CI-level guard remains M0-003/Codex).
`ruff` and `mypy --strict` clean.

## Deviations from spec (each needs owner ack)

1. **Rename applied in full.** Package/CLI/data paths are `whiskeyjack`-named; backlog
   acceptance text like `minibench-bot --help` was read as renamed. "minibench" survives
   only as the tournament id and in docs.
2. **The full spec (`CODEX_HANDOFF.md`) is missing from the repo.** The Codex
   verification brief lives at `CODEX_PROMPT.md` (restored to its original name after a
   transient rename). The "full spec" both briefs cite — ledger DDL, submission seam
   detail, § "Prohibited implementation claims" — **does not exist in the repo.** M0
   was fully specifiable without it; M1's ledger (M1-601) and M2's submission seam are
   where it becomes load-bearing. **If you have that document, add it as
   `CODEX_HANDOFF.md` before M1 go-ahead;** otherwise I will propose the ledger design
   explicitly in the M1 plan for your approval instead of implementing against an
   assumed spec.
3. **Python 3.11 provisioned via uv** (system has only 3.12): `.python-version` pins
   3.11; `uv.lock` is the lockfile M0-002's "lock resolves on 3.11" refers to.
4. **Fixtures are synthetic.** The public Metaculus API now returns 403 without
   authentication, so the committed API-post fixtures are hand-built to the pinned
   parser's exact shape (clearly `[SYNTHETIC FIXTURE]`-labelled) rather than captured
   from live data. Regenerate from a real fetch once `METACULUS_TOKEN` exists (A-1101);
   the `--save` flag on `questions fetch --live` does this in one command.
5. **`retrieval.social` config landed in M0** (spec places the adapter in M1) so the
   typed config matched the full contract from day one. Adapter code: not started.
6. **Prompt file moved + one mojibake fix** (`FORECASTER.md` → `prompts/forecaster.md`,
   title em dash repaired). Version stays 1.0.0; no hash recorded yet (M1-401 hashes
   after the v1.1.0 patch).
7. **Stricter readings taken** (brief rule 4): `expected_cdf_points` locked to 201;
   `community_prediction_policy` and `redact_secrets` locked to their only legal
   values; `dry_run: false` / `no_submit: false` rejected pre-M2 even with
   `enabled: false`.

## Notes for Codex

- Drift-check seam: `tests/unit/test_dependency_pins.py` asserts the 0.2.92 pin.
- The SDK logs a "METACULUS_TOKEN not set" warning at import time (its module-level
  behaviour, not ours); harmless but visible in CI logs.
- Config validation errors are pre-sanitized (`ConfigError`); the redaction filter is
  `whiskeyjack_bot.logging_setup.SecretRedactionFilter`. Both are relevant to M1-605.

## Open questions for M1

1. **CODEX_HANDOFF full spec** — see deviation 2. Biggest open item.
2. **Ledger DDL approval**: if the full spec stays unavailable, the M1 plan will
   include the proposed append-only schema (forecast versions, lifecycle events,
   research runs, documents, approvals, submissions, resolutions, scores) for sign-off
   before implementation.
3. **Backlog xlsx vs CSV**: the exported CSVs under `docs/backlog/` are convenient for
   grepping; confirm the xlsx remains authoritative (the LibreOffice lock file
   suggests it's open/edited locally).
4. **A-11xx owner tasks**: METACULUS_TOKEN (A-1101) unblocks realistic fixtures and the
   live smoke read; nothing in M1 hard-blocks on any key (mocks carry all of M1), but
   A-1102 (model route) is needed for the first live forecast at M1's end.

## How to review

```bash
git log --oneline --graph master   # one branch per issue, IDs in messages
uv run pytest                      # 56 tests, offline
uv run whiskeyjack-bot verify-env --config config.yaml
uv run whiskeyjack-bot questions fetch --config config.yaml \
  --snapshot tests/fixtures/snapshots/minibench_sample_snapshot.json
```
