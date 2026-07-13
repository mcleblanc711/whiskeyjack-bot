# whiskeyjack-bot

A Metaculus [MiniBench](https://www.metaculus.com/tournament/minibench/) forecasting
pipeline whose primary product is an **attribution instrument**: an immutable,
replayable record of every forecast, its evidence, and its outcome. Competing is the
venue; attribution is the point.

> Renamed from `minibench-bot`; spec documents in `docs/backlog/` may still use the
> old name.

## Design tenets

- **Append-only ledger.** Forecast versions and lifecycle events are never mutated.
- **Replayability.** Saved research and model output replay to the same forecast hash
  with zero provider calls.
- **Human approval boundary.** Approval binds to an exact forecast hash; any content
  change invalidates it. Submission stays disabled (`submission.enabled: false`,
  `dry_run: true`) by default.
- **No secrets anywhere.** Credentials live in environment variables; diagnostics
  mention variable *names* only.

## Repository map

| Path | Purpose |
|---|---|
| `CLAUDE_CODE_PROMPT.md` / `CODEX_HANDOFF.md` | Agent briefs (implementer / independent verifier) |
| `whiskeyjack-bot-v1-backlog.xlsx`, `docs/backlog/*.csv` | Issue-level acceptance criteria, verified facts, decisions, risks |
| `config.example.yaml` | Configuration contract |
| `prompts/forecaster.md` | Versioned, hashed forecaster prompt |
| `config/x_accounts.yaml` | Curated X/Twitter account allowlist for the social retrieval adapter |
| `src/whiskeyjack_bot/` | Pipeline implementation |

## Quick start

Documented at the end of Milestone 0 (issue M0-006).

## Status

Milestone 0 (foundation + Metaculus integration) in progress. No live submission path
exists; nothing is posted to Metaculus.

## License

MIT — see [LICENSE](LICENSE).
