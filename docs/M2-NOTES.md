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

**The existence check is not what carries the guarantee** (review round 1, finding 1,
reproduced). It answers a *question* — is this a mistyped `--config`, or a ledger that
vanished? — and the two need different messages. But `sqlite3.connect` brings a database
into being for any path it is handed, so a caller that has already checked still races its
own answer: an ordinary deletion or rotation between the check and the open used to yield a
brand-new empty ledger, and the command then reported "record_id does not name a stored
forecast record" against it. That is this decision's own failure mode reached from the other
side, and the pre-fix code printed exactly that line. Re-checking cannot close the window;
only an open that *cannot* create can, so `ledger.connect()` and `initialize_ledger()` grew a
`create` keyword (`file:…?mode=rw`, default `True` so no existing caller moves) and
`_open_existing_ledger` passes `create=False` to both — the second call is its own window.
Ordinary local I/O races are reachable reliability conditions under CLAUDE.md's threat
boundary; this is not a defence against a hostile operator, it is the difference between a
wrong answer and an error.

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
