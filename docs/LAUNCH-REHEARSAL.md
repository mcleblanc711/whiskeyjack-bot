# Launch rehearsal — September 7, 2026 UTC

The testing-area workflow completed from one operator command, then repeated without a
new forecast, comment, or paid call. Production project **33122 remains disabled** and
requires explicit owner authorization for its US$20 activation.

| Evidence | Result |
|---|---|
| Authenticated bot | Account 305299 |
| Project | 32977 (testing area) |
| Post / subquestion | [43325 / 43329](https://www.metaculus.com/questions/43325/?sub-question=43329) |
| Forecast | 14% Yes, confirmed by refetch |
| Private comment | ID 1095071; account, post, privacy, and record marker verified |
| Forecast record | `01a07975-50f4-75e5-9e59-a14c5071cc9e` |
| Payload SHA256 | `e55b14329149a13ff45b05937bce5a45478ef4c41a2d1803d6317a8a0d348c12` |
| Completion | 2026-09-07 01:22:49 UTC |
| Repeated command | 01:25:11 UTC; zero processed, one skipped, zero failures/unresolved |
| Budget after repetition | US$0.099559 actual + US$0.15 reserved; US$19.750441 remaining |

The initial attempt refused before buying a forecast because the source check treated a
Wayback archive as the publisher. The fix unwraps the archived publisher URL and restricts
fallback retrieval to named source domains. The successful command reused its completed
AskNews responses and purchased corrected Exa searches. All earlier spending remained
charged to the same budget. The model required one schema repair, within the two-invocation
limit. Two Sol calls reported approximately US$0.07156 combined; four Exa calls reported
US$0.028. The AskNews current/archive estimate remains reserved because actual billing
was not returned. The model used medium reasoning and the configured routing price ceiling.

Both successful invocations used:

```bash
whiskeyjack-bot tournament run-once --config /tmp/whiskeyjack-launch-rehearsal.yaml --question-id 43329
```

Full private artifacts remain on the execution host under
`data/launch-rehearsal/`, alongside the isolated ledger and current posting guard. They are
not committed to this public repository. The temporary testing activation was disabled
following the rehearsal.

Offline verification includes cross-version/two-process exclusion, process death after
server acceptance before receipt persistence, uncertain forecast and comment recovery,
changed inputs, missing artifacts, durable spending, and storage restore guards. All four
pre-release stored forecasts reproduced their original hashes with sockets blocked.
The complete suite, lint, formatting, strict typing, migration hygiene, and systemd unit
validation are release gates; see the release PR for the final run results.

This demonstrates operational compatibility and recovery behavior. It does not establish
forecast calibration or competitive skill. Production activation and installation/start of
the polling service remain owner-controlled deployment steps.

## PR #75 review remediation — September 7, 2026 UTC

All eight review findings were reproduced against `287c105` before implementation, using
fake providers and blocked sockets. The initial regression run had 19 failing cases and
one passing shutdown control. `tests/unit/test_launch_findings.py` retains those contracts
and adds interruption, concurrent settlement, and read-only recovery coverage.

| Finding | Observed before the fix | Regression contract |
|---|---|---|
| Operator storage | All four non-enable commands created a missing database | Refuse without creation; valid disable still works |
| Status bindings | Changed config/prompt, missing prompt, and missing guard artifact reported enabled | Shared local activation checks, sanitized refusal, nonzero exit |
| Phase deadline | AskNews/Exa swallowed the alarm; Sol replaced it with a provider error | Deadline escapes provider handlers, cleans up the timer, and records question failure |
| Evidence selection | Stale and irrelevant documents reached generation in mixed evidence | Only usable documents reach the model; full research remains retained; three hash paths reproduce |
| Research artifacts | Truncated JSON and malformed provenance still allowed posting | Parse every referenced artifact before POST |
| Restored spending | Reservation returned without a hold on new purchases | Persistent account/project hold, committed before witness reconciliation |
| Cached retrieval | Cached Exa/AskNews responses counted as new calls; Exa repeated its charge | Cache-only runs cost zero; mixed runs count only actual new requests |
| Completion/settlement crash | Known model and retrieval costs stayed reserved after restart | Original reservation settles once, including concurrent recovery |

The focused provider/tournament suite passes **317 tests**. The full release gate passes
(lint, formatting, strict typing, and the complete offline suite with the 200-example
Hypothesis `dev` profile; pytest completed in 392 seconds). Migration/artifact hygiene,
backlog lint, and systemd validation also pass. Four existing forecasts in the
operator ledger reproduce their original hashes using read-only SQLite with sockets
blocked. The prompt bytes, existing migrations, and forecast record schema are unchanged.
The runbook documents truthful local status and the conservative spending hold; no automatic
hold release is provided. No provider requests, activation, deployment, or service startup
were performed during remediation. Read-only checks found both service/timer inactive and
not installed, and the earlier testing activation still disabled.
