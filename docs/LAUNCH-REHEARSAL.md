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
