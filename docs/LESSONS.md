# Lessons

**Read this at every milestone stop point, and again before starting a parallel wave.**
`docs/M1-NOTES.md` records what shipped per item. This file records what the *process* cost,
so the same tax is not paid twice. Every number here was measured, not estimated; the command
that produced it is given so a future reader can re-measure rather than trust.

Add a lesson when something cost more than one round, more than one day, or more than one
person's attention — and state the mechanism, not the moral. "Be careful with X" is not a
lesson; "X and Y are only equal for the inputs the test used" is.

---

## The headline number

```bash
git log --oneline --no-merges | wc -l                    # 129
git log --oneline --no-merges | grep -ciE "round|review" #  73
```

**57% of all non-merge commits are review-round commits.** That is the single largest cost
centre in the project, and it is not evenly distributed:

| Item | Rounds | PR open → merged | Notes |
| --- | ---: | --- | --- |
| M1-202 | **1** | — | First single-round approval |
| M1-401 | 2 | 2026-07-21 → 07-23 | |
| M1-203 | 3 | 2026-07-21 → 07-23 | |
| M1-302 | 4 | 2026-07-21 → 07-23 | |
| M1-305 | 5 | 2026-07-23 → 07-23 | Five rounds on **one function** |
| M1-301 | 6 | — | |
| M1-303 | 6 | 2026-07-27 → **08-06** | |
| M1-603 | 6 | 2026-07-30 → 08-02 | |
| M1-308 | **7** | 2026-07-27 → **08-06** | |

The spread is 1 to 7 on comparably sized items. The lessons below are mostly about that spread.

---

## The second headline number — and the two instincts it corrects

Measured 2026-08-06 and re-measured 2026-08-17, on the perception that "the tests have
dramatically slowed down progress." The instinct was right that something had. It took two
tries to find what, and **both wrong turns are the reusable part.**

```bash
/usr/bin/time -f "%e" uv run pytest -q                 # 497.7s
/usr/bin/time -f "%e" uv run pytest -q tests/unit      # 374.3s
/usr/bin/time -f "%e" uv run pytest -q tests/property  #  77.4s
uv run pytest -q tests/unit --durations=0 \
  | awk '/^[0-9.]+s (call|setup|teardown)/ {gsub("s","",$1); t[$2]+=$1} \
         END {for (k in t) printf "%s %.1fs\n", k, t[k]}'
```

| | before | after |
| --- | ---: | ---: |
| Full suite | **497.7s** | **81.3s** |
| `tests/unit` | 374.3s | 28.2s |
| `tests/property` | 77.4s | 46.0s |
| unit `setup` | **202.3s** | **5.3s** |
| unit `teardown` | 44.7s | — |
| unit `call` — the actual assertions | 74.7s | 13.8s |
| `tests/unit/test_lifecycle.py` alone | 96.5s (149.7s on M1-606) | 3.2s |
| ruff + ruff format + mypy, all three | — | 1.6s |

### Wrong turn 1 — believing a suite is slow because of what it tests

`call` was 20% of unit time on one run and 22% on a second. **Two thirds of unit-test time was
fixture construction, not testing**, and test authorship was not where the time went either:

```bash
git log --no-merges --format='%H' | while read c; do
  git show --format='' --name-only "$c" | grep -v '^$' \
    | awk '{if($0~/^tests\//)t++; if($0~/^src\//)s++} END{print (t&&!s)?"tests-only":(t&&s)?"both":(s)?"src-only":"other"}'
done | sort | uniq -c
```

Of 132 non-merge commits: **14 touched tests without touching `src/`, 61 touched both together,
and exactly 1 touched `src/` without tests.** Tests ride along with the code they cover; they are
not a separate tax paid in separate commits. **Rule: before believing a suite is slow because of
what it tests, split `setup` from `call`.** And quote the ratio, not the seconds — absolute times
move with page-cache warmth.

### Wrong turn 2 — fixing a uniform cost at each call site instead of below them

The profile above named the expensive object, and the proposed fix followed from it directly:

```
connect() + replay migrations 001→003 : 429 ms     ← done ~483 times per run
copy of an already-migrated .sqlite3  :   1.1 ms
```

So: build the migrated ledger once, copy it per test. Converting the lifecycle `ledger` fixture to
a session-scoped template returned **122.3s → 92.7s, about 30s** against a projection of ~195s — a
6× miss — because the fixture was never the only path to a database. The raw-SQL schema tests, the
v2 backfill and the 003-refusal test each build their own, correctly. Closing the gap needed ~45
more `initialize_ledger` call sites converted across four files, and it was blocked behind
migration 004.

**The 429 ms was never about migrations.** `ledger.connect()` opens WAL with
`synchronous = NORMAL` — real fsyncs — and the suite was buying disk durability for databases
discarded seconds later. That cost sits *below* every path to a database, including every one a
cached template cannot reach. Moving pytest's temp root to tmpfs took the full suite **497.7s →
81.3s** and `test_lifecycle.py` **96.5s → 3.2s**, for ~30 lines in `tests/conftest.py`, no test
edits, no dependency, and no migration dependency:

```bash
PYTEST_DEBUG_TEMPROOT=/dev/shm uv run pytest -q      # now the default, via pytest_configure
```

**Rule: when a cost is uniform across many call sites, fix it below them, not at each one.** The
tell is the ratio between the projection and the measurement: a 6× miss on "cache the expensive
object" means the expense was not in the object.

### The root cause, and why CI never showed it

```bash
cat /sys/block/sda/queue/rotational   # 1
cat /sys/block/sda/device/model       # TOSHIBA DT01ACA2
python3 - <<'PY'
import os, time, tempfile
for label, d in (("project dir", "/home/cleblanc/projects"), ("/dev/shm", "/dev/shm")):
    fd, path = tempfile.mkstemp(dir=d); t = time.perf_counter()
    for _ in range(50): os.write(fd, b"x" * 4096); os.fsync(fd)
    print(f"{label:12} {(time.perf_counter()-t)/50*1000:7.3f} ms per fsync")
    os.close(fd); os.unlink(path)
PY
```

| | ms per fsync |
| --- | ---: |
| project directory (`/dev/sda2`, ext4) | **49.638** |
| `/dev/shm` (tmpfs) | **0.013** |

**The development machine's only drive is a 7200 RPM hard disk.** 49.6 ms is one platter
rotation plus a seek — the physical price of durability here, roughly 50–500× what an SSD
charges. That single number explains the whole 497.7s, and it is why the fix is a filesystem
and not a fixture.

**And CI would have told you there was no problem.** `quality-gate` has run in 92–118s
throughout, before and after this change, because GitHub's runners are SSD-backed:

```bash
gh run list --workflow=ci.yml --limit 12 --json headBranch,startedAt,updatedAt
```

So the honest scope of this lesson is *local development*, and the transferable part is the
trap, not the tmpfs: **a profile is only valid in the environment the work happens in.** The
suite was 5× slower where it was being written than where it was being checked, and the number
that was easy to look up was the one that said nothing was wrong.

`PYTEST_DEBUG_TEMPROOT` rather than `--basetemp` matters and is not a detail — pytest still
creates its own per-run numbered directory underneath, so parallel worktrees keep separate roots.
`--basetemp` names one shared path and wipes it on entry, which two concurrent worktrees would do
to each other mid-run. An explicit `--basetemp`, a preset `PYTEST_DEBUG_TEMPROOT`, a platform with
no `/dev/shm`, or a tmpfs below the free-space floor all fall back to pytest's default: the
failure mode of the whole mechanism is "slow", never "wrong". Verified both ways — 4.1s with the
hook, 118s with `PYTEST_DEBUG_TEMPROOT=/tmp`, 122s with `--basetemp`, green in all three.

**The session-scoped template patch is superseded, not deferred** — it needs no backlog row (see
checklist item 4). What survives from it is lesson 5's constraint, which still binds anything that
replaces a slow fixture: prove the replacement can fail.

### What this cost, and what it buys back

The inner loop lesson 5 mandates — write test → prove it fails pre-fix → fix → re-run → run the
full suite — was **~13 minutes per test** on M1-606, and one test genuinely took 45 minutes. It is
now **~1.5 minutes**. A review round costs at least two full gate runs, so a round drops from ~17
minutes of waiting to ~3. None of the checks were removed to get there.

---

## 1. The review protocol changed underneath the reviews it was governing

This is the most expensive structural lesson so far, and the least obvious.

```bash
git log --format="%ad %h %s" --date=short --no-merges -- scripts/ .github/ CLAUDE.md
```

Nine workflow-layer commits reached master. **Six of them landed while PRs #15 and #16 were
open** — the two longest-lived branches in the project, at ten days each. Because CLAUDE.md
requires merging master into every active branch daily (correctly — see lesson 3), each of
those changes entered both branches *mid-review-cycle*.

The concrete result on M1-308: **three consecutive rounds of one review used three different
request formats.** Round 5 was generated under the old unbounded prompt. Round 6 under PR
#18's stopping-rule contract. Round 7 needed PR #19's HEAD-pinning hand-patched in because
the tooling had not landed yet. M1-603's round-5 request was generated, discarded and
regenerated for the same reason.

Each of those changes was individually correct and is still in force. The cost was not in any
one of them; it was in landing them against branches that were mid-conversation with an
external reviewer.

**Rule: a workflow change is a track, and it takes a slot.** Claim it in `docs/TRACKS.md` like
a dependency or a migration number. Land it **between waves**, not during one. If it must land
mid-wave, say so in the next review request explicitly — the reviewer is a stateless model and
will otherwise read a format change as a substantive one.

**Corollary — the round number counts reviews *performed*, not requests sent.** A response
older than the latest request means the last round is still unanswered. M1-308's round 7 was
nearly numbered 8 on exactly this confusion, which would have asserted a review that never
happened.

## 2. A review request that does not pin its own commit cannot be answered wrongly *or* rightly

Three separate rounds — M1-308 r6, M1-603 r4, M1-303 r5 — were spent on findings already
closed on a newer tree. The request said "on branch `feat/…`, diffed against `origin/master`".
Both of those move. Nothing in the document identified the tree it described, so a review
answered against a different commit contradicted nothing.

Fixed in PR #19: `HEAD` and the diff base resolve to full hashes once, print in the request,
and build every range. **The complementary half is the author's**: before writing any fix code,
diff the commit the review names against `HEAD` and reproduce each finding by execution. A
finding you cannot reproduce gets a rebuttal, not a fix.

**Rule: both ends of a round must be pinned — the request states its HEAD, the response states
what it examined, and a mismatch voids the round rather than starting an argument.**

## 3. Daily master merges are not optional, and the reason is not tidiness

M1-302 reached its merge 18 commits behind and paid for all of them at once. But the
second-order effect is the one that bites a *review*: M1-308's remediation diffstat was
~11,495 insertions, and **almost none of it was the branch**. Two master merges had pulled in
all of M1-303 and M1-603 — code already approved on PRs #16 and #17.

A reviewer handed that diff re-audits ~10k already-approved lines unless the request says
otherwise.

**Rule: merge master daily *and* name the real surface in the request.** After any master
merge, the deliberate-choices section must say which files are actually this branch's. The
"already on the diff base" scope test exists for exactly this and only works if the reviewer
can tell which lines those are.

## 4. Write the reasoning before round 1, not after round 5

M1-202 closed in one round. M1-305 took five, on one function, for three properties a single
local run finds. The difference is not difficulty — it is `docs/M1-NOTES.md:165-307`, where
M1-202's notes are written as:

> `### Decision — X, and why` · `### Deviation` · `### Rejected — X, and why not` ·
> `### Deferred (do not read the absence as an omission)` · `### Standing risk — not verifiable offline`

**A reviewer who can see that you already weighed an option does not propose it as a finding.**
`review-request.py` emits those five headings; anything still carrying its `TODO(author)`
comment when the request is sent is a section the author skipped, visible to the reviewer.

## 5. Fuzz the pure functions before the first review, and check the test can fail

M1-305's tiebreak: five rounds, one function, three properties. Worse, **3 of M1-303's 10 new
properties passed against the broken code** — they round-tripped through the stored key, or
their hostile-text strategy accepted 1 draw in 300.

**Rule: every new property and every new regression test is run against the pre-fix code and
confirmed to FAIL.** Assert the discriminating post-condition; feed input *as written*, not
normalized; never import a private constant to assert against it. A test that passes both ways
either says so in its own docstring and explains why (the vacuity guards in
`test_allowlist.py` are deliberate) or it is testing nothing.

The same failure mode reaches ordinary tests. M1-308's round-3 permission test had become a
test of nothing — it patched `Path.read_bytes`, which the rewritten code no longer called. And
M1-603's whitespace test asserted both layers for four rounds while missing the defect, because
every parameter it used was a *space* — the one character the two definitions already agreed on.
**Two-layer parity is not tested by exercising both layers; it is tested by a case where they
could differ.**

## 6. When a finding names one exception type from a third-party parser, enumerate its siblings

M1-308 round 7 reported a `ValueError` escaping `load_allowlist`. Execution found **six**
shapes, because only PyYAML's scanner, parser and composer raise `YAMLError` — the *constructor*
raises whatever Python raised at it. Two of the six put file content in the message, making it a
secret-hygiene leak the finding as written never reached.

**Rule: close the class, not the reported instance.** The enumeration belongs to the dependency,
and a shape missing from your tuple escapes raw — which is how this reached a review in the first
place.

## 7. A gate against forgetting a step must not itself skip silently

The first `backlog-status` gate skipped whatever its pattern missed, and its pattern was anchored
to lower case and to `feat|fix`. Both `feat/M1-303-x` and `feature/m1-303-x` reported success on a
`Not Started` row. It now **fails** on an unrecognized prefix.

The same shape appeared twice more: `review-request.py` asserted the four gates passed without
running them, and `check-migrations.sh` is blind to a collision it never sees — which is why
master requires branches to be up to date before merging. That requirement is load-bearing, not
bureaucracy.

**Rule: fail closed. A check whose unknown case is "pass" is decoration.**

## 8. A mutation check can be answered by stale bytecode

The verification technique this file leans on hardest — mutate the source, confirm the test
fails, restore, confirm it passes — has a failure mode that returns a **false green**.

`scripts/*.py` and `tests/conftest.py` are loaded in tests by path, through
`importlib.util.spec_from_file_location`. That consults `__pycache__`, and its cache validation
is the source file's **size** plus its mtime at **one-second granularity**. A mutation that
preserves the byte count — reordering a tuple, swapping `<` for `<=` — and is restored within
the same second leaves both fields unchanged, so Python serves the *mutant's* bytecode to every
later run.

Observed 2026-08-17 while reordering the `GATES` tuple in `review-request.py`. The mutation was
caught, correctly. Then the restored source kept reporting the mutant's ordering, and the next
green run was green about code that was no longer there.

The direction that matters is the other one: had the mutation been introduced *and* the cache
been warm from the pre-mutation source, the check would have reported "mutation survived" — or
worse, "test still passes", which reads as "the test is fine".

**Rule: a loader used for mutation checking must compile from source.**

```python
spec = importlib.util.spec_from_loader(name, loader=None)
module = importlib.util.module_from_spec(spec)
module.__file__ = str(path)
exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
```

All four path-loading test modules use that form now (`test_tracks.py`, `test_check_backlog.py`,
`test_review_request.py`, `test_conftest_temproot.py`). The general shape is the one lesson 5 is
already about: **a verification step has to be verified too.** Re-run a mutation check whose
result surprises you with `find . -name __pycache__ -prune -exec rm -rf {} +` first, and if the
answer changes, the earlier answer was the cache talking.

## 9. A strategy that normalizes its own inputs can be what makes a property vacuous

Lesson 5 says run every new property against the pre-fix code. M1-306 is the case that shows
*why the mutation has to be aimed at the named defect*, not merely at the function.

`test_the_hash_survives_the_persisted_round_trip` exists for one bug class: the `datetime.fold`
distinction that cost M1-305 its round 3. `tests/property/strategies.py` generates `fold`
correctly — that was fixed by an earlier review, and the file's docstring says so. The
**consumer** threw it away. The `packets()` strategy re-keyed each generated run onto the
packet's question with:

```python
payload = run.model_dump(mode="json")   # ISO-8601 has nowhere to put fold
payload["question_id"] = question_id
unique.append(validate_run(payload))    # ...so it is gone before the property runs
```

So no packet reaching any property could carry the distinction, and a deliberately
fold-sensitive `packet.py` **passed** the property named after it. Two other mutations of the
same function were caught, which is what makes this dangerous: the suite looked discriminating.

The measurement that separates the two states is the only one that could:

| mutation | old strategy | fixed strategy |
| --- | --- | --- |
| hash the in-memory form (crashes) | fails | fails |
| hash a surrogate-pair-sensitive extra | fails | fails |
| hash a **fold**-sensitive extra | **passes** | fails |

The property was green before and after the strategy fix. Nothing but running it against
deliberately broken code could tell the two apart.

**Rule: the mutation must express the defect the property is named for.** A property that
survives three mutations has been shown to discriminate on three things, not on the thing it
claims. And when a property covers a distinction the schema *drops on serialization*, check the
path from the generator to the assertion for a `model_dump` → re-validate round trip — that is
the normalization, and it is invisible because it looks like ordinary fixture plumbing.

### The same shape one level up: a simulated boundary tests the simulation

M1-306's round-1 review found that a schema-valid `cost_usd = -0.0` hashed one way in memory
and another after persistence, because SQLite maps `-0.0` to `0.0` on a REAL column. No
property could see it: `round_trip_run` modelled storage as `json.dumps` → `json.loads`, and
**JSON is the half of storage that behaves** — it preserves the sign. The defect lived in the
half being simulated away.

Replacing it with a property that persists into a real ledger found an *eighth* defect the
review had not reported, on the first run: a **surrogate pair** in a `provider_config` key is
not UTF-8 encodable, `json.loads` silently **recombines** it into the astral scalar, and
pydantic's `model_dump(mode="json")` renders the original as six `U+FFFD` — so in-memory and
stored hashed differently. Again invisible to a JSON-simulated round trip, and again the
recombination *succeeded* rather than failing, which is what made it silent.

**Rule: for a persistence invariant, at least one property must cross the real boundary.**
Simulate the boundary for speed everywhere else, but a claim of the form "what is stored
replays to what was hashed" has to be tested against the thing that does the storing. The
cheap version of this is one property with a module-scoped ledger and a per-example row id —
about a second of runtime for the class of defect that is otherwise unreachable.

Corollary, from the same item: a value that is both hashed into a record **and** accepted as a
separate argument by the writer can be stored inconsistent with the object that was hashed.
M1-306's `documents_dropped` was passed alongside the run and defaulted to `None` on it, so the
stored packet and the in-memory packet hashed differently. Found by an end-to-end smoke test,
not by any unit test, because every unit test built both sides the same way. **Give a hashed
field exactly one source.**

## 10. A hung tool looks exactly like a slow one, except in CPU time

`scripts/run-review.sh` invoked from a non-interactive shell hung for **57 minutes** with
**`00:00:00` of CPU time** and no output. The cause was `codex exec` reading stdin: from a
terminal it behaves, but where stdin is a pipe or a socket — any harness, CI job, or
backgrounded shell — it blocks before doing any work. Fixed with `< /dev/null` on the
invocation.

The reusable part is the diagnosis, not the fix. "Still running" and "wedged" are the same
observation from the outside, and the instinct is to wait longer, because the request really
was large. What separated them in about five seconds:

```bash
ps -o pid,etime,time -p <pid>       # ELAPSED 56:03, TIME 00:00:00
```

A process that is genuinely working accumulates CPU time — even one that is mostly waiting on
a network response has to parse its input and stream its output. Near-zero CPU against a large
elapsed time means it never started. Two supporting checks, in order of cost: whether its log
files are still being written, and `ls -l /proc/<pid>/fd/0` to see what stdin actually is.

**Rule: before waiting longer on a slow tool, check that it has spent any CPU at all.** And
when you background a tool that normally runs interactively, redirect stdin from `/dev/null`
rather than inheriting whatever the harness handed you.

---

## Milestone stop-point checklist

Run this at the end of each milestone, before asking for owner go-ahead:

1. **Re-measure both headline numbers.** If the review-commit share has not fallen, the changes
   made in response to this file did not work. Re-time the suite the same way (`/usr/bin/time -f
   "%e" uv run pytest -q`, then the `--durations=0` phase split): a suite that has crept back
   past ~3 minutes has grown a new uniform cost, and the ratio between `setup` and `call` says
   whether it is below the tests or in them.
2. **List every workflow-layer change that landed mid-wave** (`git log -- scripts/ .github/
   CLAUDE.md` bounded to the milestone) and check each against lesson 1: could it have waited
   for the wave boundary?
3. **For each item that took more than three rounds**, name which lesson above would have
   prevented it. A round that maps to no lesson is a candidate for a new one.
4. **Check every "Deferred" note landed a backlog row.** M1-603's deferrals became M1-606;
   M1-303's became M1-309/310/311. An unclaimed deferral gets reported as a finding next time.
5. **Add the lessons.** Mechanism, measurement, rule.
