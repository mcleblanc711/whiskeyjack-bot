# Milestone 2 implementation notes

Running record of M2 decisions and deviations, in the spirit of `docs/M1-NOTES.md`. M2 is
the submission milestone: approval, idempotency, the dry-run gateway, the package-backed
gateway and the bot-testing-area smoke test. The ordering is not incidental — the
approval boundary is built first and everything downstream is gated on it.

## M2-701 — Approval commands

`M1-603` shipped the approval *mechanism*: the `approval_events` table, the
`approved`/`rejected` lifecycle vocabulary, `lifecycle.record_approval()` writing both
rows in one transaction, and migration 003's triggers binding a decision to the record's
exact `forecast_sha256`. It deliberately withheld the command layer — putting one in
would have made an approval path reachable ahead of this item. This is that layer.

Delivered:

- `src/whiskeyjack_bot/approval.py` — `ApprovalError`, the `ForecastSummary` and
  `ApprovalRecord` value objects, the readers `read_forecast_summary()`,
  `approval_history()` and `effective_approval()`, and the two commands `approve()` /
  `reject()`.
- `src/whiskeyjack_bot/cli.py` — `approve` and `reject` subcommands, `EXIT_REFUSED = 4`,
  and `_open_existing_ledger()`.
- `tests/unit/test_approval.py` (36), `tests/unit/test_cli_approval.py` (9),
  `tests/property/test_approval_properties.py` (8 properties). Suite: 1513 passed,
  1 xfailed; four gates green.

No migration, no dependency, no network call on any path. `submission.enabled: false` and
`dry_run: true` remain the committed defaults — the gateway is M2-703/M2-704, and D23's
"submission is a separate approved command" is why approval landing first changes nothing
about what can be posted.

### Decision — the reader is what makes the acceptance criterion checkable

The criterion is *"changed forecast invalidates prior approval; actor/timestamp/note are
retained"*. Both halves hold structurally — a forecast version is immutable, so a changed
forecast is a *new* record (M1-602) whose `record_id` was never approved — but
"structurally true" is demonstrated by the absence of a row, which is a weak thing to hand
a reviewer and a weaker thing for M2-704 to depend on.

`effective_approval()` states it positively: it returns the approval in force for a record
or `None`, and the hash on the returned record is what a submitted payload must still
match. `ApprovalRecord` is the "retained" half — actor, note, and *both* timestamps, which
mean different things (`occurred_at_utc` is when the decision was made and is
caller-supplied so a replay reproduces it; `created_at_utc` is when the ledger stored it
and is writer-owned).

**Owner decision** to include the reader in this item rather than defer it to M2-704.

### Decision — an approval is read through the lifecycle event, never from `approval_events` alone

`tests/unit/test_lifecycle.py` proves a raw-SQL `approval_events` row can exist that no
lifecycle event cites: it satisfies 003's hash-binding trigger and still moves nothing,
because a record's status is derived from `lifecycle_events`. Both readers therefore join
through the event, and a decision the ledger never acted on is not reported as one —
crediting it would be the read-side twin of the "approved state with no approval event"
failure M1-603 is accepted against.

The join also carries `e.forecast_record_id = a.forecast_record_id`, which 003's trigger
already guarantees. Written out anyway, for the reason that migration gives about its own
paired probes: a constraint that holds only because another constraint holds is one
refactor away from holding for no reason.

### Decision — a record holds at most one approval, and that is the state machine's doing

`lifecycle._LEGAL_TRANSITIONS` admits `approved` only from `validated`, and nothing
carries an approved record back to `validated` — `rejected` itself requires
`from_status = 'validated'`. So `effective_approval()` returns one row or none without
choosing between candidates, and rejections stay unbounded: a record can be rejected
repeatedly and then approved.

Where the ledger nonetheless holds two, the reader **raises rather than answering with one
of them**. Values read back out of the ledger are untrusted (CLAUDE.md's threat boundary),
and a history the schema cannot produce is corrupt rather than ambiguous. The test reaches
that state by dropping 003's validating trigger, which is the only way it exists at all.

### Decision — `LifecycleError`'s message is preserved when it is re-raised as `ApprovalError`

The module-own-error rule says every malformed shape must arrive as this module's error
type, and it does: nothing raises `LifecycleError`, `sqlite3.Error`, `OverflowError` or
`UnicodeEncodeError` out of `approval.py`. But the wrapper keeps the underlying *text*.

That is deliberate, and it is worth stating because it reads like a leak channel.
`LifecycleError`'s own contract guarantees its message names no stored or caller-supplied
value, and its hash-mismatch message — *"the forecast changed and any prior approval no
longer binds"* — is the single thing that makes a refused approval actionable. Replacing
it with a constant would satisfy the letter of the rule and destroy what the operator
needs. The raise is still `from None`, so nothing can be reprinted through a cause chain,
and a property asserts a planted secret reaches neither the message nor the rendered
traceback.

### Decision — the read and the write are one transaction

`_record_decision()` opens `lifecycle.transaction(conn)` (`BEGIN IMMEDIATE`; it nests
through a SAVEPOINT, so `record_approval`'s own block inside it is safe), reads the stored
hash, compares, and writes. Reading the hash *outside* and passing the value back into
`record_approval()` would turn that function's binding check into a comparison of a value
against itself — the check would still be there and would no longer be a check.

A mutation that moves the comparison after the write, with no enclosing transaction, is
caught by both `test_only_the_stored_hash_is_ever_bound` and
`test_a_supplied_hash_that_does_not_match_writes_nothing`.

### Decision — only `expected_sha256` is validated here

`record_id`, `actor`, `note` and `occurred_at` are left to `record_approval()`'s
validators rather than restated. Two sets of field rules for one column is exactly how
M1-603's round-5 defect happened (`str.strip()` against SQLite's one-argument `trim()`),
one layer up. `expected_sha256` is validated here because it is this module's own
parameter and the writer never sees it — and a malformed digest gets its own message
rather than falling through to the mismatch, because "does not match" would be true while
describing a typo as a changed forecast.

`approval.py` does keep its own `_require_text` for `record_id`, and that one is
load-bearing rather than duplicative: `sqlite3` encodes text parameters as UTF-8, so a
lone surrogate reaching a query raises a raw `UnicodeEncodeError` **quoting the offending
character** — a leak and an unhandled type, the M1-308 defect class.

### Decision — `--actor` is required and nothing is inferred

An approval is an attribution claim about a person. Defaulting to `getpass.getuser()`
would write an inferred identity into the one table that exists to be trusted, and
`approval_events` is append-only, so it would be permanent. A `WHISKEYJACK_ACTOR`
environment variable was also considered: it is not inferred, but a stale exported value
is invisible at the point of use, and nothing else in this tree uses a non-secret env var.
**Owner decision:** required flag, no default.

### Decision — `--forecast-sha256` is optional and verified when supplied

`CODEX_HANDOFF.md`'s signature is `approve --record-id ID [--note TEXT]`, with no hash.
Three readings were on the table and the middle one was taken (**owner decision**):

- *Read it from the record* — the handoff's exact signature. Since `forecast_records` is
  append-only, the hash always matches by construction, so the CLI adds no binding of its
  own and `record_approval`'s check can never fire from this path.
- *Require it always* — strictest, and never vacuous, but it departs from the documented
  signature and trains an operator to paste a hash without reading it.
- *Optional, verified when given, always printed* — what shipped. The command prints the
  record summary and the hash it is binding to before it writes, so a review and the
  approval that follows can be tied together; an operator holding a hash from that review
  can supply it, and a mismatch refuses without writing anything.

The mismatch message prints **neither** hash, matching `lifecycle._require_hash_binds`:
one is a stored value, and printing the other would let a caller confirm a guess against
it.

### Decision — the ledger file must already exist

`initialize_ledger()` would happily create one, and a mistyped `--config` would then mint
an empty database and report "record_id does not name a stored forecast record" against
it. That is a true statement about the wrong ledger, which is worse than an error because
it looks like an answer.

Given that the file exists, it is opened *through* `initialize_ledger()` rather than
`connect()` alone: that is the only public path that re-verifies every applied migration's
checksum and refuses a database written by a newer build, and it is a no-op on a current
ledger. The alternative — a read-only schema-version probe — needs `ledger._current_version`,
which is private to that module.

**The existence check is not what carries the guarantee.** It answers a *question* — is
this a mistyped `--config`, or a ledger that vanished? — and the two need different
messages. The guarantee is `ledger.open_verified_ledger()`'s, which closes two separate
windows the review found from opposite directions. Both are ordinary local I/O races, which
CLAUDE.md's threat boundary keeps in scope as reachable reliability conditions; neither is a
defence against a hostile operator, and both are the difference between a wrong answer and
an error.

*Round 1, finding 1 — the file can vanish between the check and the open.* `sqlite3.connect`
brings a database into being for any path it is handed, so a caller that has already checked
still races its own answer: a deletion or rotation in between used to yield a brand-new empty
ledger, and the command then reported "record_id does not name a stored forecast record"
against it — this decision's own failure mode, reached from the other side. The pre-fix code
prints exactly that line under `test_a_ledger_that_disappears_after_the_check_is_refused_not_recreated`.
Re-checking cannot close the window; only an open that *cannot* create can, so `connect()`
took a `create` keyword (`file:…?mode=rw`; default `True`, so no existing caller moves).

*Round 2, finding 1 — verification and use were two opens of the same name.* The first fix
passed `create=False` to `initialize_ledger()` and then to `connect()`, which is a second and
worse window: refusing to create catches nothing here, because both files exist. An atomic
rotation in between — a backup, a restore, a log-style rename — and the schema that was
checked is not the database that gets written. Reproduced: the command exited 0, printed the
*replacement's* hash, and appended an immutable approval to a ledger it had never read, while
the verified ledger kept its own hash and no approval. An approval is the one record in this
project that must bind to a forecast someone actually saw, so this is worse than the round-1
case it grew out of.

The fix is one function that opens once: `open_verified_ledger()` calls `connect(create=False)`,
runs the migration and checksum verification against *that* connection via the extracted
`_migrate(conn)`, and hands the same connection back. `initialize_ledger()` returns to its
pre-branch signature — it opens, migrates and closes, which is right for a caller that is
creating a ledger rather than acting on one. The rotation is then harmless: the descriptor
still refers to the database that was verified and shown to the operator.

### Deviation — `EXIT_REFUSED` lives in `cli.py`, not beside the other exit codes

`EXIT_OK` / `EXIT_CONFIG_INVALID` / `EXIT_ENV_MISSING` live in `env_verify.py`, where they
are that module's report vocabulary and predate every other command. An approval refusal
is not an environment verdict, and relocating the existing three into a shared home would
touch every caller of them — a change worth making, and not this item's. Noted rather than
half-fixed.

### Rejected — gating `approve` on `submission.enabled`

`submission.require_human_approval` and `submission.enabled` are submission settings, and
it is tempting to refuse an approval when submission is disabled. Rejected: an approval is
a record of a human decision, and making whether that decision can be *recorded* depend on
a deployment flag would put a configuration value between a reviewer and the ledger. D23
separates the two commands precisely so that approving and posting are different acts. The
gate belongs in front of the post, which is M2-704's.

### Rejected — reusing `lifecycle`'s private validators

`_require_text`, `_require_member` and the `_stored_*` gates exist in `lifecycle.py` and
were not imported. They raise `LifecycleError`, so every call would need wrapping anyway,
and importing another module's private names to do it is the wrong direction. `approval.py`
carries its own small set — around forty lines — and each raises this module's type.

### Deferred (do not read the absence as an omission)

- **The submission gateway → M2-703 / M2-704.** Nothing here contacts Metaculus, and
  `tests/unit/conftest.py` blocks `socket.connect` for every test in this item.
  `effective_approval()` is the seam M2-704 gates on; whether that check is fused with
  M2-702's idempotency key ("approved *and* the payload hash equals the approved hash") is
  M2-702/M2-704's call, not this item's.
- **`show` and the joined canonical record → M1-604.** `read_forecast_summary()` prints
  the identity, derived status and hash a decision binds to. It is not the handoff's full
  record view and does not try to be; approval and submission history is joined at
  read/export time, never written back into `record_json` (D25).
- **Minting `forecast_records` rows → M1-602.** It does not exist yet, so every test here
  seeds a draft by raw SQL, in the same shape `tests/unit/test_lifecycle.py` uses. That is
  a fixture cost, not a gap in what is under test: the writer and the schema were reviewed
  under M1-603.
- **Redaction of the operator's `--note` → M1-605 (Codex).** A note is stored verbatim, as
  `approval_events.note` has always been. The length cap (`lifecycle._MAX_NOTE`, 4000) is
  the only bound this item adds.
- **Relocating the shared exit codes.** See the deviation above.
- **Pinning the shared identifier bound → M1-608.** `approval._MAX_IDENTIFIER` restates
  `lifecycle._MAX_IDENTIFIER`; both are 200 and behaviour agrees at this revision, but nothing
  holds them together, so a change to either would quietly let the two entry points accept
  different `record_id` sets. Filed rather than fixed for the same reason the private
  validators were not imported: the fix is a shared public contract, and inventing one inside
  this diff would put a new seam into already-reviewed code. Raised as a non-blocking
  observation in round 1.
- **`lifecycle.py`'s "a approved event is not a legal transition" article slip.** The
  message predates this branch (M1-603) and this item is the first thing that shows it to
  an operator, which is an argument for fixing it and not a good enough one: it would put
  a one-word change to already-reviewed code into a diff that otherwise touches none, and
  the project's review history is unkind to that. Filed here rather than fixed.

### Standing risk — a rotation still strands the WAL, and that is SQLite's, not ours

Holding one connection fixes *which* database the command reads and writes. It cannot fix
what a rotation does to the sidecars, because SQLite resolves `-wal` and `-shm` by
**pathname**, not by the descriptor it holds. Rotate a live ledger and the approval is
committed to the verified database's WAL — which is still sitting under the old pathname,
now occupied by the replacement. Measured, with the main files read in isolation:

| | verified ledger | verified ledger + its WAL | replacement |
|---|---|---|---|
| before the fix | 0 approvals | 0 approvals | **1 approval** |
| after the fix | 0 approvals | **1 approval** | 0 approvals |

So the decision does follow the ledger it was bound to, and reuniting the two is a matter of
moving the sidecar the rotation left behind. Two things remain true and are not this branch's
to fix: the ledger and its WAL can be separated by a rotation that moves only the main file,
and anything that opens the replacement while that foreign WAL sits beside it will replay it.
SQLite documents renaming a database out from under an open connection as unsupported, and it
is unsupported identically for every other writer in the pipeline — `research.store`,
`lifecycle`, the M1-602 record writer — none of which this branch introduced or made more
reachable. `_isolated_approval_count()` in `tests/unit/test_cli_approval.py` is what keeps the
test honest about the distinction.

### Standing risk — the reader's cardinality claim rests on an immutable trigger

"A record holds at most one approval" is derived from `_LEGAL_TRANSITIONS` and enforced by
003's `lifecycle_events_validate_on_insert`. Migrations are immutable, so that trigger
cannot change — but a *later* migration could add a transition that returns an approved
record to `validated`, and `effective_approval()` would then start raising on histories
that had become legal. There is no way to make the reader notice that from inside itself.
The guard is `test_a_record_never_holds_more_than_one_approval`, which drives arbitrary
decision sequences through the real database: a migration that widened the graph this way
would fail it. That is the intended outcome — a loud failure rather than a silently wrong
reader — and it is a real maintenance obligation, recorded here so it is not rediscovered
as a surprise.

## M2-702 — Idempotency keys

`submission_attempts.idempotency_key` has been `TEXT NOT NULL UNIQUE` since migration
`001`, and `lifecycle.record_submission_attempt()` has written it since M1-603 — but
nothing in the tree *minted* one, so every caller would have had to invent a key. An
invented key defeats the column it goes in: the UNIQUE constraint stops a duplicate key
from claiming a second live post, it cannot stop two differently-spelled keys for one
forecast from claiming two. This item is the derivation, and the reader that lets a
gateway ask the question before it decides to post.

Delivered:

- `src/whiskeyjack_bot/submission.py` — `SubmissionError`, `KEY_SCHEMA_VERSION`,
  `canonical_key_json()`, `submission_key()`, `submission_key_for_record()`, the
  `AttemptSummary` value object, `attempt_for_key()` and `require_key_unused()`.
- `tests/unit/test_submission.py` (67), `tests/property/test_submission_properties.py`
  (36). Suite: 1758 passed, 1 xfailed; four gates green.

No migration, no dependency, no CLI, no network call on any path. `submission.enabled:
false` and `dry_run: true` remain the committed defaults — the gateways are M2-703 and
M2-704, and nothing here is reachable from a submission path that does not yet exist.

### Decision — the key material is exactly the four declared inputs

The backlog says "derive keys from tournament, question, forecast version and payload
hash", and that is what is hashed, alongside `key_schema_version` which pins the rule.
Two obvious candidates are excluded, each for its own reason:

- **`record_id`** — a writer-minted UUID. Including it would make the key a fact about
  when a row was written rather than about what is being submitted, so a replay would mint
  a *second* key for identical work and claim a second live post. That is precisely the
  failure the key exists to prevent, so the field that would cause it cannot be in it.
- **`forecast_sha256`** — functionally determined by the triple already present, because
  `001` declares `UNIQUE (question_id, tournament_id, forecast_version)` and a stored
  version is immutable (D25). It would add no discrimination and a second value that has
  to agree. The hash binding lives in `approval.py`, where an operator can act on it.

The acceptance criterion's two halves fall out of this. *"Changed payload requires a new
key"* is direct: `request_payload_sha256` is in the material. *"Changed forecast requires
a new key"* is structural: a changed forecast is a new record at a new `forecast_version`
(M1-602/D25), and the version is in the material.

### Decision — a visible scheme tag on the digest

Every other hash in this package is a bare 64-hex digest (`content_sha256`,
`packet_sha256`, `forecast_sha256`). This one is `wjsub-1-` + digest, and the departure is
deliberate: `idempotency_key` is an **append-only** column whose values must still be
interpretable after the rule is versioned, and a bare digest tells a later reader nothing
about which rule produced it. The version is *also* inside the hashed payload, so the two
cannot silently disagree — `_assert_prefix_matches_version()` fails at import if they do,
the way `packet._assert_fields_exist()` guards its exclusion list, and for the same reason
it runs at import rather than only in a test.

Key length is 72 characters, well inside the writer's 200-character identifier bound. The
unit test asserts that by putting a minted key through `lifecycle.record_submission_attempt`
rather than by importing `lifecycle._MAX_IDENTIFIER`: a private constant imported to assert
against tests the constant, not the writer that enforces it (M1-303).

### Decision — the reader ships with the derivation

`require_key_unused()` is what M2-703/M2-704 call before deciding to post. `001`'s UNIQUE
constraint remains the enforcement and callers must still let it decide — but it fires as
a `sqlite3.IntegrityError` at the write, which is after a gateway has made its decision
and, on the live path, possibly its call. The guard says the same thing beforehand, as
this module's own error, naming no value.

It is a **read**, so it is not a race-free claim, and the docstring says so. That division
is the honest one and it is the same one M1-603 settled for `unresolved_uncertainties`:
whether to make a request is decided before it is made, and the writer that is handed a
finished receipt cannot refuse it without producing a live post with no ledger row.

Mirrors M2-701's owner decision to ship `effective_approval()` alongside the writer, for
the same reason: "structurally true" demonstrated by the absence of a row is a weak thing
to hand a reviewer and a weaker thing for M2-704 to depend on.

### Deviation — M1-602 is `Not Started`, and this did not wait for it

The backlog names M1-602 (*persist immutable forecast versions*) as the dependency. That
dependency is on the **writer**, not on the columns: `tournament_id`, `question_id` and
`forecast_version` shipped in `001_initial.sql` along with the UNIQUE constraint over the
triple, and `approval.read_forecast_summary()` (M2-701, merged) already reads exactly
those three columns from exactly that table. `submission_key_for_record()` reads them the
same way. Tests seed records directly, as `tests/unit/test_approval.py` does and for the
same stated reason.

Nothing here anticipates M1-602's shape. When the record writer lands, this module needs
no change.

### Rejected — a human-readable structured key, and a bare digest

A key like `wjsub-1/minibench/38402/v2/3f9c2ae1b0d4` reads at a glance in a database dump,
and that is its whole case. Against it: `tournament_id` is free-form operator
configuration, so it needs escaping and a length budget inside a 200-character column, and
a *truncated* hash weakens the one claim the key makes — that the same payload gives the
same key and a different payload does not. Neither cost buys anything an operator cannot
get from `submission_key_for_record()`.

A bare 64-hex digest was the consistent choice and was rejected for the append-only reason
above.

### Rejected — normalizing an uppercase digest

`_require_sha256` refuses `"D"*64` rather than lowercasing it. Accepting both spellings
would mean two callers could mint the same key from what they believe are different
inputs, or — worse — that a stored key could not be traced back to which spelling built
it. An uppercase digest arriving from a caller is a bug in the caller, and this is where
it is cheapest to see.

### Deferred (do not read the absence as an omission)

- **No CLI.** There is nothing to submit yet; the operator command is M2-703's.
- **No payload builder.** `request_payload_sha256` is an *input*, exactly as the backlog
  wording says. M1-502/M1-503 own producing the binary/multiple-choice/numeric submission
  payload, and both are `Not Started`.
- **No `submission.enabled`/`dry_run` handling.** This module is not on a submission path.

### Owner decision (D33) — an approval binds to the forecast hash, not to a payload

**Round 1 blocked on this**, and it was the standing risk this item declared up front.
The criterion reads *"changed payload requires a new approval/key"*. The **key** half is
direct. The **approval** half cannot be satisfied here: an approval binds to
`forecast_sha256`, so one approved forecast covers every payload built from it, and a
payload that changed *without the forecast changing* gets a new key while keeping its old
approval in force.

The reviewer was right that noting it is not enough — CLAUDE.md's stricter-reading rule
says implement the stricter reading *and* note it — and right that the alternative is an
explicit owner decision. Both were taken:

**The strengthening that is in scope shipped.** `submission_key_for_approved_record()`
refuses to derive a key for a record with no approval in force, read through
`approval.effective_approval()` — so an `approval_events` row that no lifecycle event
cites does not open the gate either. `submission_key_for_record()` stays ungated and
serves a `draft`, because that is exactly what M2-703's dry run needs: seeing what *would*
be submitted is how an operator decides whether to approve it. **Two functions rather than
one function with a flag**, for the reason M1-402 settled — a bound any caller can lift is
not a bound.

**The rest is D33, and it is a sequencing fact rather than a preference.** Binding an
approval to a payload requires the forecast→payload mapping, which is M1-502/M1-503 and
does not exist. An operator asked to approve a payload hash today would be approving a
value nothing in the tree can compute, and the approval command that took it would be
`M2-701`'s, already merged. Doing it now would mean a migration, a changed approval
command, and an operator ceremony over a value that has no producer.

**M2-707** carries the payload binding (dependency `M2-702; M1-502; M1-503`), and M2-704
is where the check lands. The gap is asserted as a test
(`test_the_documented_gap_is_real_and_is_asserted`) rather than left as prose: if a later
change closes it, that test fails and this note has to be updated, which is the point of
pinning a known limitation instead of only describing one.

### Round 2 — an approval event is history, not a current state

Round 2 closed all three round-1 findings and blocked on a defect in the gate round 1 had
just produced. **Reproduced by execution before any fix was written**, as the workflow
requires:

```text
status                     = failed
effective_approval is None = False
GATE OPENED, key           = wjsub-1-1ca7c9a3…
```

`approval_events` is append-only, so a record carries its approval **forever**.
`effective_approval()` answers "was this forecast approved", and M2-701's contract is right
that it does — but it is not the same question as "is it approved *now*". A record that has
since reached terminal `failed` or `submitted` still reports one, so the gate minted a key
for a record `record_submission_attempt()` can no longer append an event for. A future
M2-704 gateway trusting the advertised gate would have posted and only then discovered the
attempt was unrecordable: **a live post the ledger cannot record**, which is this project's
primary failure mode.

The fix is the second check, in `submission_key_for_approved_record()` rather than in
`effective_approval()`. The reviewer offered both; changing `effective_approval()` would
alter M2-701's merged public contract and its tests for a caller that does not exist yet,
and M2-701's reader is genuinely a reader of *history*. The gate is the thing that needs a
current-state question, so the question is asked there.

`approved` is the only status admitted, and an **uncertain** attempt still passes because it
leaves the record at `approved` — deliberately, per M1-603: whether to make a second request
while one is unresolved is `lifecycle.unresolved_uncertainties`' decision, and an uncertain
attempt must not be terminal or a later confirming refetch has nowhere to land. A fix that
refused it would have overshot, so that case has its own test.

The ungated seam still serves a terminal record: reading back the key a *past* attempt used
must keep working, or the ledger could not explain its own history.

`test_every_status_reachable_from_approved_is_accounted_for` enumerates the destinations out
of `approved` from `lifecycle._LEGAL_TRANSITIONS` rather than from a hand-written list —
M1-308's lesson that a guard tested against the cases the author thought of moves when the
truth table does. The private constant is read to *generate* the cases, never as the expected
value of an assertion.

### Round 1 — the other two findings

**Non-blocking, fixed:** `test_the_same_key_cannot_create_two_attempts` used
`pytest.raises(Exception)`, which would have passed for an unrelated failure. Narrowed to
`LifecycleError`, to its sanitized text, and with an assertion that the key is not echoed.

**Backlog candidate, filed as M2-708:** `require_key_unused()` is a read, so two concurrent
commands can both see one key as unused, and `001`'s UNIQUE constraint only refuses the
second row *after* its post has been made. Nothing is reachable today — no gateway exists
— and the module's docstring already disclaims race safety, which the reviewer confirmed.

The six risk areas the request asked to be pressure-tested all came back **safe**:
canonical-material injectivity, store/load replay stability, the `require_key_unused` race
claim as documented, own-error-only, no value in messages or tracebacks, and the boolean
integer dispatch.

### Standing risk — two stored-value gates are not reachable through this schema

`_stored_flag` and `_stored_text` re-gate values read back out of the ledger, per
CLAUDE.md's threat boundary. For `submission_attempts` specifically, neither can fire from
a row this schema accepted: `success`/`verified_by_refetch` carry `CHECK (… IN (0, 1))`,
and TEXT affinity coerces an integer written to `attempt_id`. Both facts are asserted
rather than assumed (`test_the_schema_is_what_refuses_a_flag_outside_zero_and_one`,
`test_text_affinity_is_why_the_stored_text_gate_is_defense_in_depth`), and the gates are
kept as defense in depth for a row this package did not write. `_stored_int` on
`forecast_records.question_id` **is** reachable — INTEGER affinity leaves non-numeric text
as TEXT and no trigger types that column — and has its own test.

### On the property pass

Seven deliberate mutations were run against the suite before the first review, each
confirmed to fail the property it is meant to fail: a delimiter-free field concatenation,
the payload hash dropped from the material, the tournament collapsed to a constant, a
nonce added to the material, a refusal that echoes its value, the lone-surrogate probe
removed, and the stored version altered on the way out.

The first of those is the lesson worth writing down. The injectivity property **passed**
against the concatenation on its first draft, because the smear pools were large enough
that the colliding pair was never drawn — M1-303's "collision properties need colliding
draws", reproduced exactly. The fix was to shrink the pool to six tuples containing three
pairs that collide under `tournament_id + question_id + forecast_version`, and to assert
those three pairs directly as well, so the claim does not depend on the search at all.

A second near-miss is worth recording because it would have produced a false claim rather
than a weak test: two of the first mutation attempts were silent no-ops, because `ruff
format` had rewrapped the lines the patch matched against. A mutation that does not apply
looks exactly like a property that does not catch it. Assert that the patch applied.

## M2-703 — Dry-run gateway

`CODEX_HANDOFF.md` asks for a `SubmissionGateway` protocol *owned by this repository*
returning a sanitized `SubmissionReceipt`, and two implementations. This item is the seam
plus the first of them; `MetaculusSubmissionGateway` is M2-704.

Delivered:

- `src/whiskeyjack_bot/submission_gateway.py` — `GatewayError`, `GatewayMode`, the
  `SubmissionRequest` / `SubmissionReceipt` value objects, the `SubmissionGateway`
  protocol, `canonical_payload_json()` / `payload_sha256()`, `dry_run_attempt_id()`,
  `dry_run_artifact_path()`, `DryRunSubmissionGateway`, `write_dry_run_artifact()` /
  `read_dry_run_artifact()`, and `record_receipt()`.
- `tests/unit/test_submission_gateway.py` (82), `tests/property/test_submission_gateway_properties.py`
  (104: 13 properties plus a 91-case injectivity table). Suite: 2098 passed, 1 xfailed;
  four gates green.

No migration, no dependency, no CLI, no config change, no network call on any path.
`submission.enabled: false`, `dry_run: true` and `no_submit: true` remain the committed
defaults — see the deferral below.

### Decision — a dry run writes no `submission_attempts` row, and that is not a shortcut

This is the constraint the whole module is arranged around, and it has two independent
causes, either one fatal:

1. `001` declares `idempotency_key TEXT NOT NULL UNIQUE`. A dry-run row spends the key the
   real submission needs, so the live post that follows could never be recorded. A live
   post the ledger cannot record is this product's primary failure mode — the same one
   M1-603 round 4 withdrew its retry block over.
2. `lifecycle.record_submission_attempt()` always appends a lifecycle event, and derives
   its type from `(success, verified_by_refetch)`. `(True, True)` is `submitted`, a lie
   about a post that never happened. Every other pair is `submission_uncertain` or
   `submission_failed`, and `submission_failed` moves the record to terminal `failed` — a
   rehearsal would permanently kill the forecast version it was rehearsing. There is no
   honest event for "nothing was posted", and `_LEGAL_TRANSITIONS` admits none of them
   from `draft`, which is where a record sits when a dry run is most useful.

So the acceptance criterion's *"records payload/hash"* is satisfied by a **file** under
`storage.artifact_root`, and the ledger is untouched. `test_a_dry_run_of_a_draft_records_
nothing_and_spends_no_key` asserts that positively against a real ledger: status still
`draft`, `submission_attempts` empty, `lifecycle_events` empty, and the key still free.

**Owner decision** (2026-08-22) to record the receipt as an artifact rather than
receipt-only or a new `dry_run_receipts` table. A table would have claimed migration `007`
for a mode that posts nothing, and lane 1's `M1-602` may want it.

### Decision — the writer on the way into the ledger ships here, and the refusal is the guard

`record_receipt()` is the only door from a receipt into `lifecycle`, and it raises
on any receipt whose `mode` is not `live`. Shipping it *with* the dry-run gateway rather
than deferring it to M2-704 is the point: without it, "a dry-run receipt can never be
recorded" is a property of absence, which is a weak thing to hand a reviewer and a weaker
thing for M2-704 to build on. `test_the_refusal_is_what_stops_a_rehearsal_killing_the_
record` demonstrates the consequence by hand-assembling what the refusal withheld and
watching the record reach terminal `failed`.

There is deliberately no `force` parameter. M1-402's rule: a bound any caller can lift is
not a bound.

### Decision — `success=False`, and no invented `error_type`

`success=True` would be a claim that a post went through, and the whole ledger rests on
that claim only ever being made by something that actually posted. The mirror case is
easier to get wrong: a dry run is not a *failure* either, so every HTTP, refetch and error
field stays `None` rather than being filled with a plausible-looking cause. A fabricated
`error_type` in an audit record is worse than an empty one.

### Decision — the digest is computed here, from the payload the receipt was handed

`request_payload_sha256` is an *input* to M2-702's key derivation, and nothing in the tree
turned a payload into one. `payload_sha256()` is that function, and the gateway computes
it rather than accepting it alongside the payload — a receipt that could claim a digest
for a payload it never saw is not evidence of anything. The artifact writer re-derives it
a third time and refuses a receipt whose claim does not match the payload supplied with
it.

The rendering is M1-305's rule verbatim, the spelling `submission.canonical_key_json` and
`research/packet.py` already use. `research.hashing.content_sha256` is deliberately *not*
reused: it collapses whitespace runs, which is meaning-preserving for article prose and
structurally wrong for a JSON body.

It lives here rather than beside `canonical_key_json` because M2-702's docstring states
that module does not own the payload, and because the payload→hash binding is what makes
the receipt honest — it belongs with the receipt.

### Decision — the accepted payload domain is "survives its own canonical rendering"

Two halves, and the second is the one worth reading.

The structural half rejects what JSON silently mangles. Non-`str` object keys are the rule
with teeth: `json.dumps` *coerces* `int`/`float`/`bool`/`None` keys to strings, so
`{1: "a", "1": "b"}` renders as one key and one of the two values is gone — a payload the
operator wrote and the receipt does not describe. `tuple` is refused for the persisted-form
reason: it renders identically to a `list` and reads back as one, so a payload holding one
is not equal to the payload a replay reconstructs.

The behavioural half is a **round-trip guard**: the canonical text is reparsed and
re-rendered, and a disagreement is refused. `ensure_ascii=True` escapes an astral scalar
and its UTF-16 surrogate-pair spelling to the same two `\uXXXX` units, and `json.loads`
recombines them — so two such strings used as two object *keys* persist as one key and one
entry is silently gone.

Defining the domain as a round trip rather than as a list of characters to look for is
what makes it total, and **the property suite proved that by finding a second mechanism**
the blocklist version would have missed. `sort_keys` orders by the Python string, so a key
that reparses to a *different scalar* can sort into a different position without colliding
with anything at all: `{U+D83D U+DE00: null, U+D83E: null}` renders in one order and
reparses into the other. Same defect, different mechanism, and no character you could have
grepped for. This is the third time on this project a test found a defect review did not
(M1-306's three); it is also the second time the fix was to assert the *post-condition*
rather than enumerate the inputs.

Two of them as *values* are correctly one value: they persist as one scalar, so a replay
reproduces one, and the digests must agree. `test_the_same_two_spellings_as_values_are_one_
value_and_that_is_correct` pins that so a later "fix" cannot make the guard stricter than
the persisted form.

### Decision — deterministic means derived, not minted

`attempt_id` is `wjdry-1-` + SHA-256 of the idempotency key, not a `uuid4`. A `uuid4`
would make two dry runs of identical work produce different receipts, and an operator has
to be able to re-run a dry run and see that nothing changed. It is a hash *of* the key
rather than the key itself so an attempt id can never be pasted into a query against
`submission_attempts.idempotency_key` and match, and `_assert_prefix_is_distinct()` fails
at import if the two identity spaces could ever be confused — `submission._assert_prefix_
matches_version`'s reason for running at import rather than only in a test.

The clock is the only impure input and it is injected. Two readings bracket the *request*,
not the artifact write: a receipt's timestamps describe the post it reports on, and the
bookkeeping that follows is not part of it. A clock that runs backwards is refused, because
the row this maps into is append-only and a reversed pair would be permanent.

### Decision — the gateway holds no ledger connection and derives no key

The caller derives the key (`submission.submission_key_for_record` admits a `draft`, which
its own docstring says is *"what a dry run needs"*; `submission_key_for_approved_record`
is the gated seam) and calls `require_key_unused()` before deciding to post. Pushing
either behind `submit()` would put the approval boundary inside the thing the boundary
exists to gate, and would cost the two claims that make this item checkable: a module with
no connection and no HTTP client is *provably* deterministic and *provably* offline, rather
than asserted to be.

The path-safety rule on `idempotency_key` runs unconditionally, though — including when no
artifact will be written. A gateway whose accepted domain depended on a constructor
argument would let the pure form mint receipts the recording form refuses.

### Deviation — the artifact writer accepts an identical existing file

`research/artifacts.py` refuses an existing destination outright, and is right to: a
retrieval artifact records a paid call, so a second one at the same path is a collision.
Here the path is derived from the idempotency key, which is derived from the payload, so an
existing file means the identical dry run was performed before — and refusing it would make
the one mode whose entire purpose is repeatability un-repeatable. The bytes are compared
and only a *disagreement* raises. The `os.link` EEXIST mechanism is unchanged, so "never
overwrite" is still atomic against a concurrent writer rather than a check that can be
raced.

One consequence is deliberate and slightly sharp: because the receipt is part of the
envelope, a second dry run at a *different instant* is a different body at the same path
and is refused. That is the honest answer — the first file is the record that the rehearsal
happened, and silently replacing it would destroy it.

### Rejected — writing a dry-run row under a separate key namespace

A `wjdry-`-prefixed idempotency key would keep the real key free, and was rejected: the row
still forces a lifecycle event, and there is no legal event from `draft` and no honest one
from `approved`. The failure is in the event, not in the key.

### Rejected — a `question_id` field on the receipt

The artifact path needs a question, and the obvious move is to put one on the receipt. It
is refused because nothing could check it: the idempotency key is a digest, so the question
inside it is not recoverable, and a receipt field that must agree with the key while
nothing verifies it is a value that can disagree with it. The writer takes `question_id`
from the caller instead. What *can* be checked is checked — the digest.

### Deferred (do not read the absence as an omission)

- **No CLI.** There is no payload builder — M1-502/M1-503 are `Not Started` — so a command
  could only take `--payload-file`, an operator affordance for a payload nothing can
  currently produce. It lands with M2-704 and D-1001's runbook.
- **No config change.** The pre-M2 validator gate on `submission.enabled` / `dry_run` /
  `no_submit` stays exactly as written, and this module reads no configuration. A gateway
  that posts nothing is legal under all three; relaxing that gate is M2-704's change and
  belongs with the code that makes a post reachable.
- **The duplicated atomic writer.** `_write_or_confirm` re-spells
  `research/artifacts._write_new_file` rather than importing it — that function is private
  to its module and raises `ArtifactError`. Extracting a shared helper changes merged,
  reviewed code, which is its own item: **M2-709**, filed with the behavioural difference
  named so the shared version takes it as a parameter rather than picking one.
- **No payload→approval binding.** Still D33 and **M2-707**; nothing here changes it.

### Standing risk — `-0.0`, and floats generally

`0.0 == -0.0` in Python, but they render as `0.0` and `-0.0` and therefore hash
differently and produce different idempotency keys. Both round-trip exactly, so the replay
guard does not fire and nothing here is wrong; the risk is that two payloads an operator
would call equal are two submissions. It becomes reachable when M1-503's CDF arrays exist.

It is named rather than fixed, and deliberately: normalizing floats inside a
replay-critical hash rule is a change to what every future key means, in exchange for a
case no builder in the tree can currently produce. The same argument applies to float
`repr` stability, which CPython guarantees (shortest round-tripping form) but the language
does not.

### Standing risk — a type name in one refusal

`payload contains a <typename>, which is not a JSON value` interpolates
`type(value).__name__`. A type name comes from a class definition rather than from payload
content, so it is not the "value" the hygiene rule guards, and it is the only thing that
makes that refusal actionable. A caller who names a class after a secret defeats it; that
is outside CLAUDE.md's threat boundary and is stated here rather than left implicit.

### Deviation — one line of another item's property test was narrowed

`tests/property/test_submission_properties.py` (M2-702) draws a `tournament_id` from
`ENCODABLE_TEXT` and, in the one property that *stores* what it draws, a whitespace-only
draw reaches SQLite as a raw `IntegrityError`: `submission._require_text` accepts a blank
identifier and `006_non_blank_identifiers.sql` refuses one. `006` landed the day after
M2-702, so this has been a coin-flip gate failure on master ever since — invisible on CI,
whose `ci` profile is derandomized, and reproducible locally about half the time.

The strategy for that one property is narrowed to what a row can hold, and nothing else in
that file changes. The **product** disagreement is left alone and filed as **M2-710**:
widening `submission.py`'s validator would change what an already-shipped, already-reviewed
writer accepts, smuggled in under a different item, which is the call M1-606 made and
M1-607 then paid for properly. It is a hardening item rather than a live defect — `config.py`
already refuses a blank tournament slug, and `submission_key_for_record` reads the value
back out of a row `006` has vetted — so nothing on a product path reaches it.

**This is a pre-existing failure this branch did not introduce**, and it is named here
because the reviewer is stateless and will otherwise read the diff line as scope creep.

### Round 1 — one blocking finding, and why the fix is narrower than the one proposed

Reviewed commit `24d5eb5`, verdict CHANGES REQUESTED, one blocking finding; the other six
risk areas the request nominated came back Safe.

**The finding.** `lifecycle.SubmissionAttempt` carries no `forecast_record_id` — it never
has, since M1-603 — so the first cut's public `attempt_from_receipt()` handed back an
attempt and left the caller to re-supply the record to `record_submission_attempt()`. A
receipt naming `rec-1` could be recorded against `rec-2`. Reproduced by execution against
`24d5eb5` before any fix code was written, per the standing rule: the write succeeded,
`submission_attempts.forecast_record_id` held `rec-2`, `rec-2` advanced to `submitted` on
an approval that authorized nothing, and `rec-1` stayed `approved`. Append-only, so
permanently.

The two-parameter shape of `record_submission_attempt()` is pre-existing. What **this
branch** introduced is a *second source of truth* for the record id — a receipt that names
one — with nothing reconciling the two. That is the amplification, and it is why the
finding is in scope.

**The fix removes the divergence rather than detecting it.** `attempt_from_receipt()` is
now private, and `record_receipt(conn, *, receipt, occurred_at, detail_code=None)` is the
exported door: it takes the record from the receipt and offers **no `record_id`
parameter**, so nothing exists for a caller to get wrong. M1-402's rule — a bound any
caller can lift is not a bound — is why the transcription stopped being public rather than
merely being paired with a safer alternative.

**Rejected — the fix the review proposed.** Adding `forecast_record_id` to
`lifecycle.SubmissionAttempt` and having `record_submission_attempt()` derive or reconcile
it is the other way to close this. It changes a merged, reviewed dataclass and its writer,
owned by M1-603, and five construction sites across `tests/unit/test_lifecycle.py`,
`tests/unit/test_submission.py`, `tests/property/test_lifecycle_properties.py` and this
item's own suite would change behaviour under a different item's branch — the call M1-606
made and M1-607 then paid for properly.

Nothing is left uncovered by the narrower fix, which is the part worth stating rather than
assuming: a caller who hand-builds a `SubmissionAttempt` supplies exactly **one** record
id and so has no second source to disagree with. The divergence was new, and it is gone.
No follow-up row is filed, because there is no residue.

`test_a_receipt_cannot_be_recorded_against_a_different_record` is the regression test, and
a sixth mutation — re-adding a `record_id` override parameter — was confirmed to fail it.

Two further tests cover the new boundary's error discipline, because the round-2 request
claims it and a request must not claim a test that does not exist (M1-308 round 7):
`record_receipt` turns both the `LifecycleError` of an illegal transition and the
`sqlite3.IntegrityError` of a spent idempotency key into a `GatewayError` with no cause
chain. A seventh mutation — removing the `LifecycleError` wrapper — was confirmed to fail
the first of them.

### On the property pass

Five deliberate mutations were run against the suite before the first review, each
confirmed to fail the property it is meant to fail: the replay round-trip guard disabled,
`attempt_id` minted as a `uuid4`, the `live`-mode check on the receipt transcription disabled,
a refusal that echoes its offending key, and the artifact writer clobbering a differing
body with `os.replace`. Each was applied to a pristine copy, run with `__pycache__`
removed, and restored — M1-312's lesson that a mutation harness killed by a timeout
silently leaves the mutation applied, and M1-501's that a mutation which fails to apply
looks exactly like a property that does not catch it. Two attempts here *did* fail to
apply, because `ruff format` had rewrapped the lines the patch matched; both are visible
as an explicit `APPLY FAILED`.

The no-leak property closes the message set rather than searching for a substring
(M1-607): every refusal from `payload_sha256` must match one of eight written-down
patterns that capture nothing from the input, and the same test asserts `__cause__ is
None` so no cause chain can reprint what a message withheld.
