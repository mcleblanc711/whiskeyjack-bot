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

---

## Milestone stop-point checklist

Run this at the end of each milestone, before asking for owner go-ahead:

1. **Re-measure the headline number.** If the review-commit share has not fallen, the changes
   made in response to this file did not work.
2. **List every workflow-layer change that landed mid-wave** (`git log -- scripts/ .github/
   CLAUDE.md` bounded to the milestone) and check each against lesson 1: could it have waited
   for the wave boundary?
3. **For each item that took more than three rounds**, name which lesson above would have
   prevented it. A round that maps to no lesson is a candidate for a new one.
4. **Check every "Deferred" note landed a backlog row.** M1-603's deferrals became M1-606;
   M1-303's became M1-309/310/311. An unclaimed deferral gets reported as a finding next time.
5. **Add the lessons.** Mechanism, measurement, rule.
