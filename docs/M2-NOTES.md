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

**Round 2 approved the remediation** at `953b7c6`, closing the finding and marking all five
nominated risk areas Safe — including the one that mattered, that the narrower fix leaves no
residue: *"The supported receipt-to-ledger path has one record identifier, taken from the
receipt; the prior divergent argument is gone."* No new findings, no new backlog
candidates. Two rounds total.

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

## M2-704 — Package-backed gateway

The first item in the repository that can cause a live Metaculus post. Everything it
stands on was already merged with no caller: M2-701's approval boundary and
`effective_approval()`, M2-702's idempotency keys and `require_key_unused()`, M2-703's
`SubmissionGateway` protocol and `SubmissionReceipt`, and — since M1-603 — the whole
submission vocabulary: `submitted` / `submission_uncertain` / `submission_failed`,
`submission_confirmed` / `submission_disconfirmed`, `record_submission_attempt()`,
`record_submission_verification()` and `unresolved_uncertainties()`. This item is the
caller of all of it.

Delivered:

- `src/whiskeyjack_bot/submission_live.py` — `LiveSubmissionError`, the `MetaculusPoster`
  protocol, `MetaculusSubmissionGateway`, the payload→post-call validator
  (`plan_from_payload`), the refetch comparison (`read_my_forecasts`, `classify_refetch`,
  `expected_values` / `observed_values` / `values_match`), the verification snapshot
  (`build_verification_snapshot` / `read_verification_snapshot`), the error classifier
  (`classify_error`, `http_details`, `storable_text`), the orchestrator
  `post_approved_forecast()` and the resolution command `verify_uncertain_attempt()`.
- `src/whiskeyjack_bot/metaculus/client.py` — `SingleAttemptPoster`, `build_poster()`,
  `PosterContractError` and the import-time contract guard.
- `src/whiskeyjack_bot/submission_gateway.py` — two functions only: `live_artifact_path()`
  and `write_live_artifact()`, plus `read_live_artifact()` / `read_submission_artifact()`
  and one new field, `SubmissionRequest.post_id`.
- `src/whiskeyjack_bot/config.py` — the pre-M2 refusals removed, the contradiction check
  added; `config.example.yaml`'s values unchanged.
- `src/whiskeyjack_bot/cli.py` — `submit` and `verify-submission`.
- `tests/unit/test_submission_live.py`, `tests/unit/test_metaculus_poster.py`,
  `tests/unit/test_cli_submit.py`, `tests/property/test_submission_live_properties.py`,
  and rewritten submission-flag cases in `tests/unit/test_config.py` /
  `tests/unit/test_env_verify.py`.

No migration — `submission_attempts` and `lifecycle_events` already carry every column and
every vocabulary member this needs. No new dependency: the dependency slot is held by
M1-311 this wave and stays held (see the deviation below).

### What execution established about the pinned SDK

Four things, all measured against `forecasting-tools==0.2.92` with `requests` stubbed, and
all now pinned by `tests/unit/test_metaculus_poster.py` so a version bump is a red build:

1. **`MetaculusClient` blind-retries every POST four times.**
   `_post_question_prediction` carries `@retry_with_exponential_backoff()`
   (`max_retries=3`) whose `retry_on_exceptions` is `requests.exceptions.RequestException`
   — which `HTTPError` subclasses. Measured: **four POSTs on a `Timeout`, four on a 400.**
2. Binding `_post_question_prediction.__wrapped__` on the instance yields exactly **one**
   POST, and the real `requests.exceptions.Timeout` propagates.
3. The SDK's `HTTPError` message embeds the **full response body and the request URL**
   (`raise_for_status_with_additional_info`), and logs it at ERROR level besides.
4. The status is nevertheless recoverable: the SDK re-raises a bare `HTTPError` (its own
   `.response` is `None`) chained `from` the original, so `exc.__cause__.response` is
   reachable through public attributes — 429 and `Retry-After` both came back that way.

### Decision — the SDK's blind retry is neutralized, and that is the item's core

Point 1 *is* the thing the acceptance criterion forbids, arriving from inside the
dependency: a timed-out post that actually landed is re-posted three more times, under one
idempotency key, with no refetch in between. Recording it correctly afterwards would not
help; by then four forecasts have been sent.

`metaculus/client.py`'s `SingleAttemptPoster` is the only place that knows this. Per post
call it binds `types.MethodType(MetaculusClient._post_question_prediction.__wrapped__,
client)` on the instance, so the SDK's own **public** `post_binary_question_prediction`
(and its two siblings) still build the payload, still enforce their bounds, and still make
the request — the decorator is simply not between them and it. The line drawn is **reads
may retry, writes must not**: `get_question_by_post_id` is passed through with its retry
intact, because a GET is idempotent and retrying it is what keeps "the refetch could not be
performed" an edge case.

**This is not the private-method dependency D28 rejected.** D28's rejected alternative is
"private method; raw API from day one" *as the way to capture an exact response body*.
Nothing here reads a response through a private name; the guard only declines to have a
request repeated. M2-705's own acceptance criterion — "no private package method dependency
without a guard" — is the standard this is written to, and the guards are three:
`_assert_single_post_is_reachable()` fails at **import** if `__wrapped__` is missing, is the
decorated attribute itself, or takes different parameters; `tests/unit/test_metaculus_poster.py`
drives the real class with a counted stub and asserts four-without / one-with; and
`test_dependency_pins.py` already makes an upgrade a red build.

**Owner decision, 2026-08-25**, taken against two alternatives: accept the retry and record
it as a standing risk (rejected — a hard constraint breached by accepted behaviour is a
defect, not a risk, which is M1-402's finding), or write the narrow HTTP adapter now
(rejected — that is M2-705, and D28 keeps the SDK path default until the smoke test passes).

### Decision — the config gate is relaxed, the committed defaults are not

`SubmissionConfig` refused `enabled: true`, `dry_run: false` and `no_submit: false`
outright, on the grounds that no submission path existed. One does now, so the three
refusals are gone and the flags mean what they say. `config.example.yaml` still commits
`enabled: false`, `dry_run: true`, `no_submit: true`, and
`submission_live._require_live_submission_enabled()` refuses to post unless all three are
deliberately flipped — so turning on a live path is three explicit edits plus an approval.
Every safety invariant survives: `enabled` still requires `require_human_approval`,
`approval_must_match_forecast_hash`, `verify_by_refetch` and
`block_retry_on_uncertain_result`.

One refusal was **added**, and it is the stricter reading of a combination the removals made
reachable: `enabled: true` alongside `dry_run: true` or `no_submit: true` describes a
deployment that both may and may not post. Resolving that at runtime — picking one flag as
dominant — would put the answer somewhere no reader of the config can see. `test_config.py`
now enumerates all eight triples and asserts the accepted set is exactly five, so a rule
that is *removed* fails as loudly as one that is added (M1-501's vacuity lesson).

**Owner decision, 2026-08-25.** The alternative was to keep the gate closed and pass an
`allow_live_post` argument, which would have shipped a path nothing could reach and blocked
M2-706 on a second config item.

### Decision — verification is a before/after comparison, keyed on a baseline

The handoff says to "refetch the question and verify `previous_forecasts` **changed** as
expected". The *changed* is why the gateway fetches the question **before** posting and
keeps the latest of the operator's own forecasts as a baseline. A confirmation requires
both halves: an entry whose `start_time` is strictly greater than the baseline's, and
values matching what was posted. Without the first half, a question the operator had
already forecast on would confirm a submission that never landed. Comparing against a
baseline rather than against this machine's clock also removes any dependence on clock
agreement with Metaculus.

`classify_refetch` is four-valued where `lifecycle.VerificationOutcome` is two-valued, and
both extra members earn their place. `mismatched` is not `absent`: something is there and
it is not what was sent. `unreadable` is not `absent` either — a refetch that could not be
performed observed nothing, and recording that as "the forecast is not there" is how a lost
connection becomes a permanent claim about a live forecast.

### Decision — the payload is the Metaculus wire body plus a discriminator

`{"question_type": ..., "<wire key>": ...}`, where the wire key is the pinned SDK's own:
`probability_yes`, `continuous_cdf`, `probability_yes_per_category`. M1-502/M1-503 then
have one shape to emit rather than a private format to translate. Dispatch is on the
`question_type` literal, never on which key is present (CLAUDE.md's rule). Exactly two keys
are accepted: a third is refused rather than ignored, because a key this module would
silently drop is a forecast nobody reviewed.

Every bound the SDK's public methods enforce is restated in `plan_from_payload`, and the
duplication is deliberate: the SDK raises a bare `ValueError` from inside the dependency,
at a point this module cannot distinguish from a failure that had already posted. Restating
them means the refusal happens before any network call and arrives as
`LiveSubmissionError` (M1-303 round 4's rule). All three types are accepted rather than
binary alone, so the item is not reopened when M1-403/404/405 land.

### Decision — order of operations, and where the boundary is

Everything that can refuse refuses before the single `post` call; nothing after it refuses.
That is M1-303 round 4 joined to M1-312, and the post is the boundary. In order, all
before any network call: the config gate; `unresolved_uncertainties()` must be empty;
`read_forecast_record()` supplies `question_id`, `post_id` and `question_type` **from the
one ledger row**; the payload's type must match the record's;
`submission_key_for_approved_record()`; `require_key_unused()`. Then the baseline fetch and
the platform identity check. After the post: the artifact is written and every failure of it
degrades to `artifact_error` on the result, and the ledger row is written regardless.
`LiveSubmissionRecord` cannot represent a lie about that — `artifact_path` is `None`
exactly when `artifact_error` is not.

Two smaller consequences of the same rule. A clock that ran backwards is **clamped**, not
refused: `record_submission_attempt` rejects a reversed pair outright, and refusing there
would leave a completed post unrecordable. And every string on the receipt goes through
`storable_text()` before the receipt exists, so a hostile provider body — NUL, lone
surrogate, 200 KB — cannot make `record_receipt` refuse a post that has already happened.

### Deviation — `my_forecasts.history`, not `previous_forecasts`

The handoff names `previous_forecasts`. In the pinned SDK that field is populated only by
`BinaryQuestion` and `NumericQuestion`; `MultipleChoiceQuestion` inherits the base class's
`None` and never fills it in. A rule built on it would silently never confirm a
multiple-choice submission — an honest post recorded as uncertain forever, which is the
worst available failure. `api_json["question"]["my_forecasts"]["history"]` is what all
three subclasses read *from*, so it is the one basis that is uniform. It is untrusted
provider JSON and is parsed defensively; `read_my_forecasts` never raises, because its
caller reaches it after a post.

### Deviation — the dependency slot is not taken, so `requests` is never imported

`submission_live` classifies transport exceptions by walking the exception's MRO and
matching class names restricted to the `requests.exceptions` module, and reads
`http_status` / headers / body through `getattr` on `exc.__cause__.response`. Importing
`requests` would make it a declared dependency — the rule `test_dependency_pins.py`
enforces for `idna`, `asknews` and `httpx` — and the slot is held by M1-311 this wave. It
is also better layering: the seam talks to `MetaculusPoster` and should not know the
transport. The vocabulary is pinned against the **real** exception classes in
`test_metaculus_poster.py`, which imports `requests` freely because a test may.

`ConnectTimeout` is deliberately classified as a *connection* error rather than a timeout:
a connect timeout never established a connection, so nothing was sent. A `ReadTimeout` is
the genuinely ambiguous case. Both still refetch, so a misjudgement costs audit fidelity
and never safety.

### Rejected — widening `SubmissionReceipt` with a `detail_code`

`record_receipt` takes `detail_code` separately, and the obvious move is to put it on the
receipt. Rejected: `FailureCode` is a *ledger* vocabulary and the receipt is the gateway's
sanitized record of a call. `LiveSubmissionOutcome` carries the pair instead, so neither
shape has to know about the other and `post_approved_forecast` does not have to re-derive
from a rendered snapshot what this module already knew.

### Reversed in round 1 — reading the option list, after rejecting it

**The original decision was wrong and the rationale that defended it was false.** It is
left here in full because the failure is instructive, and because the request that went to
review carried the false claim as a deliberate choice.

What was written: the platform reports one value per option in the question's option order,
so an exact ordered comparison needs the option list; rejected because "it adds a second
thing that must be readable for a post to be confirmable"; the comparison is a sorted
multiset instead; "two options carrying the same probability become indistinguishable,
which is a genuine weakening and is stated rather than hidden; a *different distribution*
is still caught."

Both halves are wrong.

- **A different distribution is not still caught.** `{a: 0.25, b: 0.75}` and
  `{a: 0.75, b: 0.25}` sort to the same tuple. A **permutation** of a distribution is a
  different distribution, and the multiset cannot see it — so a post whose categories
  landed transposed was reported `confirmed`, and the ledger would record `submitted` for
  a forecast the operator never made. The weakening was not "ties are indistinguishable",
  it was "category identity is not checked at all".
- **There is no second thing that must be readable.** `options` and `my_forecasts` are
  siblings in the same `api_json["question"]` dict, already parsed in a single defensive
  pass. Reading the option list costs no second fetch and no second object. The premise
  that made the trade-off look necessary did not hold.

What ships instead: `expected_values` projects multiple-choice probabilities in the
**payload's** declared order, `read_my_forecasts` carries the platform's option order on
the `ForecastHistory` beside the entries, and `classify_refetch` aligns the two **by
label** before comparing. A platform that lists options in its own order still confirms; a
transposed forecast does not. Where the alignment cannot be made exactly — either label
list missing, label sets differing, value count not the option count — the outcome is
`unreadable`, never `confirmed` and never `mismatched`.

**How it survived a full property pass:** `tests/unit/test_submission_live.py` contained
`test_a_multiple_choice_comparison_does_not_need_the_option_order`, which asserted that a
transposed history *was* a confirmation. The suite agreed with the defect, so no amount of
running it could find it. That test is deleted; eight unit tests and two properties replace
it, and the properties are **iff** rather than implications — a one-sided property holds
for the honest post and for the transposed one alike, which is precisely why the original
pass reported clean.

Raised as blocking finding 2 of M2-704 round-1 cross-model review, reproduced by execution
before any fix code was written.

### Rejected — a third copy of the atomic artifact writer

`write_live_artifact` lives in `submission_gateway.py`, next to its dry-run twin, rather
than in `submission_live`. That module already spells `_write_or_confirm` once and
`research/artifacts._write_new_file` spells it again; **M2-709** is the filed item for
merging them, and a third copy of a race-sensitive write is exactly what that item exists
to prevent. Importing a sibling module's private helper was the other option and is the
wrong direction.

### Rejected — refusing a mismatch by recording it as `absent`

`verify_uncertain_attempt` refuses on `mismatched` and on `unreadable` rather than writing
a verification. `absent` is terminal, so recording a mismatch as absent would end a live
forecast version on evidence that *a* forecast exists. Leaving the uncertainty standing is
the conservative direction: the post gate stays closed and a human decides. D-1001's
runbook is where the manual path belongs.

### Deferred (do not read the absence as an omission)

- **No payload builder.** M1-502/M1-503 are `Not Started`, so `submit` takes
  `--payload-file`. M2-703's notes said this is where that lands, and it does.
- **D33 / M2-707 — the payload→approval binding is still open.** An approval binds to
  `forecast_sha256`, so one approved forecast still covers every payload built from it.
  What is checkable today *is* checked: the payload's `question_type` must equal the
  record's. The gap is pinned by `test_the_documented_payload_binding_gap_is_real_and_is_
  asserted` rather than described in prose, so a later change that closes it fails a test
  and forces this note to be updated.
- **M2-708 — the key is not reserved atomically.** `require_key_unused()` is a read and
  says so; two concurrent commands could both see one key as unused. Nothing changed here.
- **M2-705 — exact response capture.** This item produces the evidence that spike needs:
  statuses, allowlisted headers and a truncated body on *failure*, and nothing at all on
  success, because the public post methods return `None`.
- **The response body of a successful post is not captured**, for the same reason.
- **No numeric CDF construction.** `plan_from_payload` validates a 201-point CDF; building
  one from percentiles is M1-503.

### Standing risk — value equality against a platform that may normalize

`values_match` admits a difference of `1e-9`, which is representation noise and nothing
more. Whether Metaculus round-trips a forecast value exactly is **not knowable offline**.
If it quantizes, a genuine post reads as `refetch_mismatch` and lands as *uncertain* rather
than as a false `submitted` — the failure is in the safe direction, and `verify-submission`
would then refuse rather than record. **M2-706's smoke test is what settles this**, and it
turns a guess into a measurement; the tolerance is a one-line change once there is a real
observation to set it from. It is named rather than pre-emptively widened, because widening
a comparison on speculation is how a `submitted` gets written for a forecast that is not
there.

### Standing risk — one ledger state this item cannot record honestly (M2-711, filed)

`record_submission_attempt` derives its event from `(success, verified_by_refetch)`, and
that pair has no member meaning *"the post raised **and** the refetch could not be
performed, so the platform state is unknown"*. `(False, False)` is `submission_failed`,
which is terminal and claims the post did not go through — more than is known.

Three mitigations, and then the honest admission. The refetch is retried (reads may retry),
so the cell is rare. The row's `error_message` says in words that the platform state was
not established, and `test_a_failed_post_and_an_unreadable_refetch_says_so_in_the_row` pins
that so it cannot drift while the item is open. And terminal `failed` is the conservative
direction: no further automatic post is possible for that record, and a retry is a new
forecast version behind a fresh human approval.

The alternative was to write `verified_by_refetch=True` for a refetch that never happened,
which is a lie in the primary artifact. **M2-711** is filed for the missing state.

**Closed by M2-711** (2026-08-26, migration `009`). The state exists now: such a post lands
`submission_uncertain`, the record stays `approved`, and a later refetch can still decide
it. The mitigation this section describes is gone with the risk —
`test_a_failed_post_and_an_unreadable_refetch_says_so_in_the_row` is now
`..._is_unknown_not_failed` and asserts the *absence* of the prose note, because the ledger
says it in a column. What M2-711 did **not** need was a twelfth `event_type`; see its own
section for why that would have cost a rebuild of `lifecycle_events`.

### Round 1 — three blocking findings, all reproduced by execution first

`GPT_REVIEW_RESPONSE_M2-704_r1.md`, reviewed commit `bc1bbc4`. The review named the exact
request `HEAD`, so nothing here is a stale-review rebuttal; each finding was reproduced by
running it before any fix was written, and each fix is pinned by a test that fails on the
pre-fix code.

**1 — a second poster over one client reopened the blind retry.** `SingleAttemptPoster`
held a `threading.RLock()` **per adapter**, but the attribute it shadows belongs to the
*client*. Two adapters over one `MetaculusClient` therefore held different locks, and the
damaging order is A leaving while B is inside: A's exit restores the attribute to what A
found, B's post then resolves the *class* attribute — the decorated one — and is retried.
Reproduced with plain threading primitives and no patching of this project's code: **four
POSTs for one logical post.**

The docstring had claimed the lock prevented exactly this ("a shadow-and-restore window
shared between two callers would restore the class method while the other was still inside
it, and the cost of preventing that is one lock"). It did not, so the mitigation on record
did not do what it said — which makes it a defect independent of how reachable it is.
`build_poster()` does construct a fresh client per poster and the pipeline is
single-threaded, so no *product* path shared a client; `SingleAttemptPoster.__init__` is a
public boundary that accepts any client, which is where the review put it.

Closed two ways, and the two are independent: the lock is now keyed on the client
(`_lock_for_client`, a guarded `WeakKeyDictionary`), and the window restores the
attribute's **exact prior state** instead of deleting unconditionally. Mutation-tested
separately — reverting either one alone fails tests, and the first regression test written
for this was **vacuous** (the restore closed the interleaving it drove, so it passed with
the lock reverted) and was rewritten around the order only the lock can prevent.

**2 — a transposed multiple-choice forecast was confirmed.** See the reversed decision
above. This is the finding with the widest blast radius: it could put `submitted` in the
ledger for a forecast the operator did not make, which is the one claim this whole item
exists to make trustworthy.

**3 — the SDK logged the full response body.** `raise_for_status_with_additional_info`
builds its message out of the request URL and the complete response text, logs it at ERROR,
and raises an `HTTPError` carrying the same string — which the retry wrapper logs again as
`{e}`. Through this project's own handlers that put an unbounded copy of untrusted provider
content into `logging.file`. Reproduced: a stubbed 400 whose body held a marker put that
marker in the log file.

The notes already said the SDK "logs it at ERROR level besides", which is exactly the shape
of a known-and-unaddressed leak: naming it is not closing it. `SecretRedactionFilter`
covers *configured credential values* and did redact the token in the URL, but a provider
body is not a configured secret and nothing touched it.

Closed with `ProviderResponseTextFilter`, a handler filter that **replaces** the message of
any record from `forecasting_tools.util.misc`. Replaces rather than drops: that a call
failed is a real diagnostic. The **whole module** is closed rather than the one message,
for the reason `PayloadDebugFilter` gives — every logging call in it interpolates a
response or an exception, and matching text is a check whose unknown case is "pass". The
status, allowlisted headers and truncated body an operator needs are already on the
submission attempt row via `http_details`, so nothing diagnostic is lost.

**Non-blocking, and agreed:** the second clock read can raise after a completed post. The
production CLI injects the trusted default clock, so the review scoped it out itself. Left
as-is rather than filed — the clock is not provider input and the threat boundary says the
operator is non-malicious.

**One schema change.** The verification snapshot now carries `expected_labels`, so
`verify_uncertain_attempt` can align a fresh observation to the categories the attempt
sent instead of comparing by position. `VERIFICATION_SCHEMA_VERSION` goes `1.0.0` →
`1.1.0`, and the reader still refuses any version it does not equal. No compatibility shim
and no migration: the gateway has never been reachable, `submission.enabled` has never
been `true` on any branch, and M2-706 is still `Blocked`, so **no stored snapshot exists
anywhere** to be read back.

### Round 2 — all three prior blockers closed, one new finding from the fix itself

`GPT_REVIEW_RESPONSE_M2-704_r2.md`, reviewed commit `c0ec61b`. Findings 1, 2 and 3 from
round 1 were each verified CLOSED against the remediation delta. One new blocker, and it is
the most interesting result of the whole item because **the round-1 fix caused it**.

**Aligning by label made the observed order part of the evidence, and the schema only
stored the expected one.** `expected_values` is rendered in the *payload's* label order;
`observed.latest_values` is whatever the platform reported, in the *platform's* option
order. Round 1 added `expected_labels` and stopped there. So a confirmed multiple-choice
snapshot for an honestly reordered observation looked like this:

```json
{"expected_labels": ["a","b"], "expected_values": [0.25,0.75],
 "observed": {"latest_values": [0.75,0.25]}, "outcome": "confirmed"}
```

An auditor holding only that row cannot tell an honest reordered observation from a
transposed forecast, and a positional replay of a *genuine* confirmation says **mismatch**.
Reproduced by execution before the fix.

This is worth stating plainly: the old sorted comparison was wrong, but its snapshot was at
least internally consistent, because both sides were sorted. Fixing the verdict broke the
evidence. **The ledger is the product** — a row whose verdict cannot be recomputed from its
own contents is exactly the failure this project exists to avoid, and it would have shipped
behind a correct-looking `submitted`.

Closed by storing `observed.labels` beside `observed.latest_values`. A confirmed
multiple-choice snapshot now carries all four things the comparison needs — two label
orders and two value vectors — so the verdict is recomputable from the row alone.
`VERIFICATION_SCHEMA_VERSION` stays `1.1.0`: that version was introduced on this same
unmerged branch and has never been written anywhere persistent, so amending its shape
before merge is not a format change anyone can observe. Bumping again would mint a version
that never existed.

Pinned by `test_a_multiple_choice_snapshot_reproduces_its_own_verdict` (three orderings)
and by `test_a_multiple_choice_snapshot_always_reproduces_its_own_verdict`, a property over
arbitrary option orders and observations. **Both replay the comparison out of the rendered
JSON by hand rather than calling `classify_refetch`** — the claim is about what the row
carries, and calling this module's own comparison would assert nothing about it. Two
mutations confirm they bite: dropping `observed.labels` kills all four, and the subtler one
— storing the *expected* order in the observed slot — is caught only by the reordered case.

**The non-blocking observation was also fixed**, because it was a false claim rather than a
missing feature. The sanitized log message said the details "are recorded on the submission
attempt row", which is true of a failed post and false of a read or any pre-attempt call
through the same helper — a log line telling an operator to look at a row that does not
exist. It is now conditional. The reviewer's other half of that note (the replacement also
discards the SDK's retry count and function name) is accepted and **not** addressed: those
are interpolated into the same records that carry the response text, and re-admitting a
subset by parsing is the text-matching check the module-wide replacement exists to avoid.

### Round 3 — the fix to the fix to the fix, and where that chain stopped

`GPT_REVIEW_RESPONSE_M2-704_r3.md`, reviewed commit `67b5f29`. All four prior blockers
verified CLOSED. One new blocker, and it is the third consecutive round in which **the
previous round's remediation caused the next finding**. That pattern is the record worth
keeping from this item.

**The chain, stated plainly:**

1. Verification sorted multiple-choice values into a multiset — a transposed forecast
   confirmed. *(round 1)*
2. Fixed by aligning on labels. That made the observed label order part of the evidence,
   and the schema stored only the expected order — a confirmed row could not be replayed.
   *(round 2)*
3. Fixed by storing the observed labels too. That wrote the one **unbounded** thing in the
   envelope a second time, and provider JSON supplies it — so a confirmed row could exceed
   `_MAX_BODY` and degrade to an envelope naming no values at all. *(round 3)*

Each fix was correct about the defect it named and each created the next one, because each
moved cost into the snapshot without checking what the snapshot could hold.

**The round-3 reproduction, both halves.** A **binary** question whose provider JSON carries
6,000 options: the gateway posts once, returns `success=True, verified_by_refetch=True`, and
stores `{"outcome":"confirmed","question_type":"binary","values_omitted":true,...}` — 108
bytes, no baseline, no expected values, no observed values. The option list is irrelevant to
a binary question's values and was being written anyway. And a **multiple-choice** payload
with a 32,700-character label, which `plan_from_payload` accepted: the envelope was 32,986
characters before the round-2 field and 65,704 after it.

**Also worth recording: the round-3 request asserted this could not happen.** Risk area 3
claimed "labels are bounded by `_MAX_CATEGORIES`". `_MAX_CATEGORIES` bounds the *count* of
payload categories; nothing bounded label *length*, and `_read_option_labels` bounded
neither count nor length for the platform's list. The reviewer falsified a claim the author
wrote, which is what the risk-areas section is for.

**Closed three ways**, and the third is the one that makes the property true rather than
likely:

- `label_order` replaces the duplicated label strings: the observed order is stored as
  **indices into `expected_labels`**. A confirmation already requires the two label sets to
  be equal, so the permutation is the whole of what the second list carried — and 64 small
  integers cost what 64 labels cannot.
- It is written for **multiple choice only**. A binary question's option list has no
  relationship to its values, so an order derived from it would be a fabricated alignment.
- `_MAX_OPTION_LABEL = 128` bounds label length at both ends. A payload past it is refused
  **before any post**, visible and actionable; a platform option list past it makes the
  refetch `unreadable`, so the post lands uncertain rather than confirmed-without-evidence.
  `_read_option_labels` also bounds the option *count* by `_MAX_CATEGORIES`, which it never
  did.

The bound is not a guess dressed as a constant.
`test_a_maximal_multiple_choice_snapshot_still_carries_its_evidence` renders the worst
accepted case — 64 labels at full length, every character one that `ensure_ascii=True`
escapes to six bytes, platform order reversed — and measures it: **49,997 bytes against a
65,536 limit, 15,539 to spare, evidence intact.** Raising either constant fails that test.

**Six mutations, all killed.** Two are worth noting. Writing `label_order` for every
question type initially **survived**, because both in-tree callers derive `expected_labels`
from the plan or the snapshot and both answer `None` for binary — making the type check
unreachable from inside the package. It was kept rather than deleted (unlike round 2's
redundant guard) because `build_verification_snapshot` is **public** and its two arguments
are independent: nothing in the signature stops a caller pairing a binary `question_type`
with a label list. A test that makes exactly that public call now kills the mutation. And
raising `_MAX_OPTION_LABEL` to 512 is killed by the maximal-snapshot test, which is the
point of measuring the envelope rather than asserting it.

### Before round 4 — the same class again, found by the author this time

Writing round 4's risk-areas section meant asking what *else* is unbounded on its way into
the snapshot, since that was three rounds running. One more field was:
`ForecastEntry.values` comes from `forecast_values` in provider JSON and
`read_my_forecasts` bounds it nowhere — it accepts a 50,000-element vector, and rendering
one pushed the envelope past `_MAX_BODY` into the reduced form that names no values at all.

**It is not round 3's failure, and the difference matters.** Every path to `confirmed` has
already required the observed vector to match a bounded expected one — exactly 2 for
binary, `expected_cdf_points` (a `Literal[201]`) for numeric, the option count for multiple
choice — so a confirmed row could never reach it. Verified by execution across all three
types before deciding severity. What was exposed is the `mismatched` and `absent` rows,
which carry no false claim but are where an operator most needs to see what the platform
actually held.

Closed by rendering at most `_MAX_SNAPSHOT_VALUES = 256` observed values and recording the
**true** length beside the sample as `latest_value_count`. The cap sits above every count an
honest comparison can need, so no genuine verification is ever sampled rather than
recorded; the count is what makes a truncation visible instead of silent, so the row never
implies it saw fewer values than it did. The maximal multiple-choice case — 64 full-length
labels *and* a 50,000-element observation — now renders with its evidence intact. Measured
at the true worst case rather than a convenient one: labels padded with a character
`ensure_ascii=True` escapes to six bytes, *and* observed values chosen for the longest
`json.dumps` rendering a float has — `-2.2250738585072014e-308`, **24** characters with its
sign. That is **55,849 bytes against a 65,536 limit, 9,687 to spare**.

That figure took two corrections, and both are the same mistake at different depths. The
first draft said 50,473, measured with `0.5` — a three-character rendering, so 256 values
understated the envelope by about 3,800 bytes. The second said 55,593, measured with the
longest *non-negative* float; round 4's reviewer pointed out that the sign is a character
too, and that a 24-character value is inside the accepted domain because observed values
are provider JSON and nothing upstream constrains them to probabilities. Round 3's lesson
was to measure the envelope rather than assert it. Measuring it with a convenient input is
the same failure, and searching only half the domain for the worst case is that failure
once more.

Three mutations, all killed: removing the cap, lowering it below an honest CDF, and
reporting the truncated length as the true one.

**The lesson, since this is the fourth instance:** a fixed-size evidence envelope and an
unbounded input are a standing pair, and the fix is not to widen the envelope but to bound
each input at the point it enters. `_MAX_BODY`'s all-or-nothing degradation is what turns a
size overrun into total evidence loss, and a staged degradation — drop the least essential
field first, keep the verdict and the baseline — would be the structural fix. That is a
change to a merged, reviewed function with its own callers, so it is **not** made here;
it is the shape of the follow-up if this recurs.

### Round 4 — closing the class instead of the fourth instance

Three rounds found one class by hand, and the fuzzer watched all three go by. Round 3's
review said why, precisely: *"the new multiple-choice property bounds generated labels to
24 characters, while the general snapshot property uses `ForecastHistory(())`, so neither
exercises large observed labels."* `test_a_snapshot_is_storable_and_survives_its_own_round_
trip` **did** assert `len(snapshot) <= _MAX_BODY` — against an empty history, so the branch
that renders observed values was never entered. The assertion was there and it was vacuous,
which is this project's most expensive recurring defect (M1-501's one-sided conditional,
M1-607's substring no-leak, and the three properties in this item's own first pass that
passed against broken code).

So round 4 adds the property those three rounds should have been.
`test_no_accepted_input_can_cost_a_snapshot_its_evidence` asserts the invariant the caps
exist to guarantee, rather than a consequence of it:

> For every input this module accepts, the envelope fits `_MAX_BODY` **and still names its
> evidence** — the reduced `values_omitted` form is unreachable.

It draws every bound at its maximum simultaneously — up to `_MAX_CATEGORIES` labels at
`_MAX_OPTION_LABEL` characters, each character one `ensure_ascii=True` escapes to six
bytes, against observed vectors up to 2,000 long — and carries a stronger claim on the
`confirmed` branch, which is the row an auditor may later have to check: a confirmation
must always be able to show the values it was confirmed by. The observed vector is drawn
as a *length* and repeated rather than element-wise, because the property is about
rendered size and drawing 2,000 floats per example would spend the budget on the strategy
instead of the assertion.

**Three mutations, all killed** — removing the `_MAX_SNAPSHOT_VALUES` cap, raising
`_MAX_OPTION_LABEL` to 512, and reporting the truncated length as the true one. The second
is worth reading: it fails with `{'outcome': 'unreadable', 'values_omitted': True}`, which
is the exact shape round 3 spent a round on.

`ENTRIES` was also widened from `max_size=6` to 32. Six is below every vector this module
actually sees — 201 for a CDF, up to `_MAX_CATEGORIES` for multiple choice — so the
comparison properties had been judging only lengths the platform never sends.

### Round 4 — two docstring claims that had gone false

`build_verification_snapshot`'s docstring still asserted that value counts *"are bounded by
`plan_from_payload` … so the rendering cannot approach `_MAX_BODY`"* and that *"the
fallback below is unreachable given those bounds"*. `plan_from_payload` bounds only the
**expected** side; the observed side is provider JSON, and rounds 3 and 4 both reached that
fallback. It also promised replayability through `observed.labels`, a field round 3
replaced with `label_order`. In a module whose docstring *is* the format's documentation,
a claim that invites a maintainer to delete a cap is a defect, so both were rewritten to
say what is true: each bound is named, each is attributed to the round that bought it, and
the fallback is described as reachable-in-principle and forbidden to `confirmed` rows.

### Round 4 — approved, and the two observations that followed

Round 4 returned **APPROVE at `7551681`, no blocking findings**, and confirmed round 3's
sole blocker CLOSED by direct execution across all three question types. It also confirmed
the severity argument this round rested on: oversized observations resolve only to
`mismatched` or `unreadable`, because confirmation requires exact vector-length equality
against bounded expected values.

Two non-blocking observations came back, and both are this file's own recurring defect one
level down, so both were closed rather than filed:

1. **The maximal envelope was measured over half its domain.** The worst-rendering float is
   negative — the sign is a 24th character — so the true figure is 55,849 bytes, not the
   55,593 recorded above. The property's `observed_value` strategy was drawing from
   `[0.0, 1.0]`; it now draws the full finite range, which is the honest domain anyway,
   since observed values are provider JSON and nothing upstream constrains them to
   probabilities.
2. **Numeric `confirmed` was unreachable inside the new property.** The observed vector was
   one value repeated, and a valid CDF varies, so the branch carrying the strongest claim —
   that a confirmation can always show the values it was confirmed by — was never entered
   for numeric. Exactly the vacuous-strategy failure this round was written to end. Closed
   with an `echo_expected` draw that has the platform report back what was sent: the
   complementary pair for binary, the CDF itself for numeric, and the expected vector
   permuted into platform order for multiple choice. All three now reach `confirmed`,
   verified by execution, and the three mutations still die.

The lesson is worth stating plainly, because this item has now produced it four times: a
property is only as strong as the reachability of the branch it asserts about, and the
author is the worst judge of that. Two of the four instances were caught by measuring, one
by a reviewer, and one by asking what else shared the shape.

### Round 4 — the schema version follows the envelope

`latest_value_count` is a new field, so `VERIFICATION_SCHEMA_VERSION` goes to **1.2.0**,
matching the branch's own precedent (round 1 bumped `1.0.0 → 1.1.0` for the same kind of
additive change). Nothing in-tree reads the field — `verify_uncertain_attempt` consumes
only `question_type`, `expected_values`, `expected_labels` and `baseline.latest_start_time`
— and no row has ever been persisted, so this cannot cause a misread today. It is a
consistency fix, and free: `read_verification_snapshot` keys on exact equality, and two
different envelope shapes must not share one version string.

### On the mutation pass

Fourteen deliberate mutations were run against the unit suite before the first review, each
applied to a pristine copy and restored afterwards (M1-312's lesson: a harness killed by a
timeout silently leaves the mutation applied). Thirteen were killed. **One survived, and it
was a real gap**: collapsing `unreadable` into `absent` inside `classify_refetch` passed all
52 tests, because the *submit* path never hands that function a `None` — it returns
`unreadable` itself when the retry loop gives up. `verify_uncertain_attempt` is the only
caller that can reach the branch, and nothing tested it. Untested, a lost connection would
have ended a live forecast version by recording `submission_disconfirmed` on no evidence at
all. Two tests were added and the mutation is now killed.

### On the property pass

Eleven mutations were run against the property suite. **Three properties passed against
broken code** — the M1-303 ratio almost exactly — and each was a strategy failing to reach
the branch it was meant to cover:

- `storable_text`'s truncation was never exercised, because `ANY_VALUE` never generated
  text longer than the limit. Fixed with an explicit oversized-text strategy.
- `read_my_forecasts` was asserted to return "`None` or a `ForecastHistory`", which passes
  against a reader that answers *empty* for everything it cannot parse — the exact collapse
  the four-valued outcome exists to prevent. The property now states which answer each shape
  requires.
- The multiple-choice sum rule could be deleted entirely, because every generated vector
  already summed to one. Replaced with an **iff** property, and the same treatment given to
  the binary bounds and to the CDF's length and monotonicity: a one-sided property is
  vacuous against "the rule was removed" (M1-501).

All six now fail on the weakened module and pass on the real one.

The no-leak property closes the message set rather than searching for a substring (M1-607):
every refusal from `plan_from_payload` must match one of twenty-six written-down patterns
that capture nothing from the input, and the same test asserts `__cause__ is None` so no
cause chain can reprint what a message withheld. Writing that set out is what found a real
defect: refusals from `canonical_payload_json` were arriving as the **base**
`GatewayError`, which `LiveSubmissionError` subclasses — so `except LiveSubmissionError`
did not catch them and `cli._run_submit` would have printed a traceback instead of
`refused:`. `_wrap_gateway()` closes it, message preserved.

## M2-711 — Record a submission whose outcome is unknown

M2-704's own filed defect, closed. `record_submission_attempt` derived its event from
`(success, verified_by_refetch)`, and that pair had no member meaning *"the post raised
**and** the refetch could not be performed"*. It fell into `(False, False)` alongside the
genuinely failed attempt, which `003` maps to `submission_failed` and therefore to terminal
`failed` — a permanent claim that the post did not go through, made on no observation at
all. See M2-704's "Standing risk" section above, now marked closed.

The information already existed one layer up and the seam threw it away.
`submission_live.classify_refetch` has returned a four-valued outcome since M2-704 —
`confirmed` / `absent` / `mismatched` / `unreadable` — and its own docstring says why:
*"`unreadable` is not `absent` either: a refetch that could not be performed observed
nothing, and recording that as 'the forecast is not there' would let a lost connection
become a permanent claim about a live forecast."* This item carries that vocabulary across
the seam into the ledger.

Delivered:

- `src/whiskeyjack_bot/migrations/009_submission_refetch_outcome.sql` — one `ADD COLUMN`
  (`submission_attempts.refetch_outcome`) and two `DROP`/`CREATE` trigger rewrites.
- `src/whiskeyjack_bot/lifecycle.py` — `RefetchOutcome`; `SubmissionAttempt.refetch_outcome`
  with `verified_by_refetch` derived; the widened partition in `record_submission_attempt`.
- `src/whiskeyjack_bot/submission_gateway.py` — the same field/property change on
  `SubmissionReceipt`, `refetch_outcome` in the artifact envelope,
  `ARTIFACT_SCHEMA_VERSION` → `1.1.0`.
- `src/whiskeyjack_bot/submission_live.py` — the outcome passed through instead of reduced;
  the `_UNESTABLISHED_NOTE` prose removed; `RefetchOutcome` re-exported from `lifecycle`.
- `src/whiskeyjack_bot/submission.py`, `src/whiskeyjack_bot/cli.py` — `AttemptSummary`
  reports the column; `submit` prints it.
- `tests/unit/test_lifecycle.py` (the eight-row partition, five raw-SQL trigger tests, the
  pre-`009` legacy test), `tests/property/test_lifecycle_properties.py` (totality),
  `tests/unit/test_submission_live.py` (the criterion end to end, both halves).

Migration `009`, claimed in `docs/TRACKS.md` before the worktree existed. No new dependency.

### Decision — the vocabulary member goes on `submission_attempts`, not `lifecycle_events`

The backlog row says closing this *"needs a vocabulary member and therefore a migration"*.
It does not say **which** vocabulary, and the two readings cost very different things.

`lifecycle_events.event_type` is a column `CHECK` with eleven members. SQLite cannot widen a
`CHECK` without rebuilding the table, and rebuilding `lifecycle_events` means dropping its
append-only block triggers and copying every row out and back — which `003`'s own header
calls *"precisely the operation the ledger exists to make impossible"*. `detail_code` is a
`CHECK` on the same table, so a new `FailureCode` member would cost the same rebuild.

`submission_attempts` has no such problem: `ADD COLUMN` reaches it, which is how `002`,
`004` and `008` all added constrained columns, with the constraint living in a `BEFORE
INSERT` trigger because `ADD COLUMN` cannot carry one. So `refetch_outcome` goes there, and
`submission_uncertain` widens to cover the new cell.

That is not a workaround wearing a design's clothes. `submission_uncertain` already means
exactly what this state is: `approved → approved`, named by `unresolved_uncertainties` so
`post_approved_forecast`'s gate stays shut against a blind retry, resolvable by
`record_submission_verification` when a later refetch does establish something. A distinct
event type would have been a **second name for one lifecycle state**, bought with a rebuild
of the table every guarantee in this ledger rests on. *Why* an attempt is uncertain is
carried by `detail_code` and now by `refetch_outcome`, which is what they are for.

No `FailureCode` member was needed either: the post's own error code — `timeout`,
`provider_unavailable`, `http_error` — is the honest account of why the outcome is unknown,
and the trigger already requires an uncertain event to carry one.

(Owner decision at plan time, with the alternative and its cost put side by side.)

### Decision — four members, not a boolean, and `mismatched` moves with `unreadable`

The minimal fix is one bit: *was the refetch performed at all*. Rejected for two reasons.

It would be a **second spelling of part of** a vocabulary that already exists in
`classify_refetch`, and the ledger would hold a lossy projection of a value the caller
already had in full. `LiveErrorType`/`FailureCode` set the precedent for the opposite: the
gateway's vocabulary and the ledger's are related by a total mapping written down once.

And it would have left half the cell wrong. `classify_refetch` returns `mismatched` **only
when an entry newer than the baseline is on the platform and does not match what was
sent** — so for a post that also raised, "it did not go through and nothing is there" is
false of `mismatched` too, in exactly the way it is false of `unreadable`. Whether that
newer entry is this post landing garbled or something else entirely is a human judgement,
and an uncertain record is where a human can still make it; terminal `failed` is where they
cannot. Fixing one half of a cell and leaving the other is the stricter reading declined.

This is wider than the row's literal words ("the refetch could not be performed"), and it is
**the same cell and the same defect**, not a second item. Flagged here rather than left for
a reviewer to notice.

### Decision — `verified_by_refetch` becomes derived on both value objects

`refetch_outcome = 'confirmed'` and `verified_by_refetch = True` are one fact. Keeping both
as fields would have made them two things that can disagree, and the fix would have been a
cross-check — which is exactly what M2-703's review rejected: *a value the writer also takes
separately is a second source of truth; remove it, do not cross-check it.*

So on `lifecycle.SubmissionAttempt` and `submission_gateway.SubmissionReceipt`,
`verified_by_refetch` is now a `@property` over `refetch_outcome`. Every reader is unchanged
— the CLI, the artifact envelope, the ledger writer all still ask the same question — and
no caller can hand the writer two answers. The column is still written, because `001`
declares it `NOT NULL`; it is derived in `record_submission_attempt` from the vocabulary
member just validated, not read back off the property.

The **raw-SQL path** is what the schema closes:
`submission_attempts_require_receipt_on_insert` refuses a row where
`(refetch_outcome = 'confirmed') <> (verified_by_refetch = 1)`. That clause is also why
`lifecycle_events_validate_on_insert`'s `submitted` probe is left exactly as `003` wrote it
— with the equivalence in force, `success = 1 AND verified_by_refetch = 1` and
`success = 1 AND refetch_outcome = 'confirmed'` are the same condition, and restating it
would be a second spelling rather than a second rule.

One consequence worth naming: `dataclasses.asdict` renders fields, not properties, so an
`asdict`ed receipt now carries `refetch_outcome` and not `verified_by_refetch`.
`test_receipt_carries_every_handoff_field` asserts both halves of that deliberately.

### Decision — `COALESCE(refetch_outcome, 'absent')`, and why it is not a default

`ADD COLUMN` cannot add `NOT NULL` without a default, and no default is honest for a row
nobody observed — `002`, `003`, `004` and `008` all say the same thing about their own added
columns. So the column is `NULL`able in the DDL and **required by the trigger on every new
row**: an attempt that declines to say what its refetch established reopens the exact
question the column exists to settle.

That leaves pre-`009` rows holding `NULL`, and the failed-event probe reads them as
`absent`, which reproduces `003`'s rule for them verbatim. Nothing already written changes
meaning. The `COALESCE` is unreachable for anything the ledger accepts from here on, and
`test_an_attempt_written_before_009_still_partitions_by_the_old_rule` builds a genuine
version-8 database — packaged migrations applied directly, real checksums recorded, so
`ledger.py` accepts it as one a previous build produced — upgrades it, and drives both
events through it.

### Decision — `ARTIFACT_SCHEMA_VERSION` → `1.1.0`

`_receipt_envelope` is shared by the dry-run and live artifact writers, and the live
artifact is the operator-facing evidence of a live post, so `refetch_outcome` belongs in it.
`read_submission_artifact` is exact-match on the version, so a `1.0.0` artifact is now
refused rather than read with a field missing — and that is what the version is for.

Accepting both versions was considered and rejected: a reader admitting two shapes has to
decide what the absent field means for the older one, and the only honest answer
("unknown") is a value the field has no member for. The practical cost is nil — M2-704
merged the day before this branch and the submission path has never run against Metaculus,
so no live artifact exists; a rehearsal artifact regenerates for free.

### Rejected — a new `submission_outcome_unknown` event type

Covered above: it is a second name for a state that already exists, and it costs a rebuild
of `lifecycle_events`. It would read marginally better in a history dump. It is not worth
dropping the block triggers on an append-only table to get, and the schema-level reason is
written into `009`'s header so the next person weighing it sees the price first.

### Rejected — widening `submission_verifications.outcome` to four members

`VerificationOutcome` stays two-valued, and the asymmetry with the attempt path is
deliberate. An **attempt** stores all four members because the question it answers is *what
did we see when we posted*, and "nothing" is a real answer to it. A **verification** exists
to decide an open uncertainty, and only `confirmed` and `absent` decide anything — so
`verify_uncertain_attempt` still refuses to record `mismatched` or `unreadable` and leaves
the uncertainty standing, which keeps the post gate closed, the conservative direction.
Widening that table would add rows asserting no conclusion, on the table whose rows *are*
the conclusions.

### Deferred (do not read the absence as an omission)

- **No backfill of `refetch_outcome` for pre-`009` rows.** There is nothing to backfill
  *from*: the distinction the column records was not observed when those rows were written.
  `NULL` is the honest value and the triggers read it as `003` did.
- **`AttemptSummary` keeps both `verified_by_refetch` and `refetch_outcome` as real
  fields.** It is a reader of two stored columns, and reporting what the ledger holds is its
  whole job. The derivation belongs to the writer, where there is one fact to derive from.
- **No CLI surface for "show me the attempts whose outcome is unknown".** `submit` prints
  the outcome and `unresolved_uncertainties` already drives the refusal and the
  `verify-submission` hint; a query command over the column is M1-604/`show` territory.
- **`_REFETCH_ATTEMPTS` is unchanged at 3.** Its justification changed — it is no longer
  what stands between a lost connection and a wrong permanent record — but a transient read
  failure that resolves on the second try still records what the platform actually shows
  instead of an honest `unreadable` an operator then has to chase by hand.

### Standing risk — `mismatched` is now a state an operator must resolve by hand

Before this item, a post that raised while a newer non-matching entry appeared on the
platform was recorded `submission_failed` and the record was done with. It now stands as an
uncertainty, and `verify_uncertain_attempt` deliberately **will not** clear it — a mismatch
is a human judgement. So a record can sit `approved` with an unresolvable-by-machine
uncertainty blocking every further submission for it.

That is the conservative direction and it is the intended behaviour: the alternative is
killing a forecast version on evidence that *a* forecast exists. But it is a real
operational cost, and there is no command today that lets an operator say "I looked; it is
not mine" and close it. Recording a human's judgement about a mismatch is not a gap this
item could close honestly — it needs an actor, a note, and an approval-shaped boundary,
which is an item, not a clause. Named here rather than discovered in an outage.

### Standing risk — the equivalence clause cannot see a blob

`(NEW.refetch_outcome = 'confirmed') <> (NEW.verified_by_refetch = 1)` compares two truth
values, and a blob in `verified_by_refetch` makes the right-hand side false rather than
raising. `001`'s column `CHECK (verified_by_refetch IN (0, 1))` catches that — but column
`CHECK`s run *after* `BEFORE INSERT` triggers, so the row is refused by the `CHECK` with
`001`'s message rather than by this clause with `009`'s. Both refuse it; only the message
differs. Left as it is because narrowing the clause to `typeof()` would duplicate a
constraint that already holds, on a trigger that is immutable once merged.

### On the mutation pass

Five mutations, each reverting one clause to what it replaced or neutering it to
`WHERE 0`, run with `__pycache__` cleared first (a same-size same-second edit can otherwise
be served back stale). Every one was caught, and by the test that claims to own it:

| Mutation | Caught by |
| --- | --- |
| writer's derivation → `if success == 0` (the pre-M2-711 rule) | 7 tests across all three layers |
| trigger's uncertain probe → `success <> verified_by_refetch` | the 2 partition rows, the 2 raw-SQL rows, both live tests |
| `COALESCE(...)` → bare `refetch_outcome = 'absent'` | the pre-`009` legacy test, alone |
| equivalence clause → `WHERE 0` | all 4 disagreement rows |
| required/vocabulary clause → `WHERE 0` | the "must say" test and all 6 vocabulary rows |

The third is the one worth noticing: exactly one test failed, which is what a clause that
exists solely for already-written rows should look like.

### On the property pass

One new property, `test_every_attempt_shape_has_exactly_one_recordable_outcome`, over every
`(success, refetch_outcome)` pair. It asserts **totality** — a pair with no legal event is
an outcome that happened and cannot be recorded, which is the dual of the defect this item
closes — and drives the real writer against the real schema rather than against
`_DESTINATIONS`, so it fails if either layer drifts.

The strategy is `st.booleans()` × `st.sampled_from(REFETCH_OUTCOMES)`, and that is the
guard against the vacuous-property class in `docs/LESSONS.md`: the existing writer fuzzers
send `ANYTHING` and so never reach the derivation with a valid vocabulary member *and* a
valid record — they exercise the type gate standing in front of it. `test_the_submission_
writer_raises_only_lifecycle_error` was widened to draw from the vocabulary as well as from
junk for the same reason.

`test_the_partition_covers_every_pair_exactly_once` is the cheap companion in the unit
suite: the parametrized test drives every row it is given and would pass just as well with a
row missing, so the table is asserted total and single-valued against `get_args`.
