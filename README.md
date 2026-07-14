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
| `CLAUDE_CODE_PROMPT.md` / `CODEX_PROMPT.md` | Agent briefs (implementer / independent verifier) |
| `whiskeyjack-bot-v1-backlog.xlsx`, `docs/backlog/*.csv` | Issue-level acceptance criteria, verified facts, decisions, risks |
| `config.example.yaml` | Configuration contract |
| `prompts/forecaster.md` | Versioned, hashed forecaster prompt |
| `config/x_accounts.yaml` | Curated X/Twitter account allowlist for the social retrieval adapter |
| `src/whiskeyjack_bot/` | Pipeline implementation |

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (installs its own Python 3.11; the repo pins
`3.11` via `.python-version`).

```bash
# 1. Install uv (once), then clone and install
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/mcleblanc711/whiskeyjack-bot.git
cd whiskeyjack-bot
uv sync

# 2. Create your config from the committed contract
cp config.example.yaml config.yaml
# Edit config.yaml: replace REPLACE_WITH_VERIFIED_LITELLM_MODEL_NAME with a real
# model name (e.g. an OpenRouter LiteLLM id). Everything else can stay as-is.

# 3. Verify the environment (reports missing variable NAMES only)
uv run whiskeyjack-bot verify-env --config config.yaml

# 4. Offline dry run: replay the committed fixture snapshot — zero network calls
uv run whiskeyjack-bot questions fetch --config config.yaml \
  --snapshot tests/fixtures/snapshots/minibench_sample_snapshot.json

# 5. Run the test suite (offline by construction; sockets are blocked in tests)
uv run pytest
```

### Environment variables

Secrets live only in environment variables — never in config files, code, or logs.
`verify-env` checks presence without reading values.

| Variable | Purpose | Where to get it |
|---|---|---|
| `METACULUS_TOKEN` | Metaculus bot API token | [metaculus.com/futureeval/participate](https://www.metaculus.com/futureeval/participate/) — create a bot account (owner task A-1101) |
| `OPENROUTER_API_KEY` | Forecaster model route | [openrouter.ai](https://openrouter.ai/) (owner task A-1102) |
| `ASKNEWS_API_KEY` | Primary news retrieval | [docs.asknews.app](https://docs.asknews.app/) (owner task A-1103) |
| `EXA_API_KEY` | Fallback web retrieval | [exa.ai](https://exa.ai/) (owner task A-1103) |
| `XAI_API_KEY` | X/Twitter research agent (only when `retrieval.social.enabled: true`) | [console.x.ai](https://console.x.ai/) (owner task A-1106) |

None are needed for the offline quick start above.

### Live reads (optional, requires `METACULUS_TOKEN`)

```bash
# Fetch open MiniBench questions and save a replayable snapshot
uv run whiskeyjack-bot questions fetch --config config.yaml --live --save data/snapshots/minibench.json

# Target the bot-testing-area smoke tournament without touching config
uv run whiskeyjack-bot questions fetch --config config.yaml --live --tournament bot-testing-area
```

There is no submission path in this codebase yet; `submission.enabled: false` and
`dry_run: true` are enforced by config validation, not convention.

## Status

Milestone 0 (foundation + Metaculus integration): implementation complete and
cross-review findings remediated, but the milestone gate is **not yet passed** —
the CI quality gate with independent verification (M0-003, Codex-owned) and owner
approval at the M0 stop point are both outstanding. No live submission path
exists; nothing is posted to Metaculus.

## License

MIT — see [LICENSE](LICENSE).
