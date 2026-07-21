# M1-201 — response to review round 2

All three round-2 findings accepted and fixed. Nothing disputed this round.

Thank you for the domain withdrawal — and specifically for saying you found no mapping source
and would not invent one. That is the outcome the scope argument was hoping for.

---

## Medium — `source_categories` conflates namespaces and drops identity

Accepted in full. Reproduced your exact collision before changing anything:

```
Category(id=17, name="Economics", slug="economy")  -> ["economy"]
Category(id=18, name="economy",   slug=None)       -> ["economy"]
indistinguishable: True
```

This was a regression my own round-1 fix introduced, and I had flagged `slug or name` as the
weakest part of it without following the thought through to the collision. Your framing —
preserve source categories *without conflating or discarding their identity* — is the right
statement of the requirement.

Fixed with an owned canonical model, per your recommendation:

```python
class SourceCategory(_StrictModel):
    id: int
    name: str = Field(min_length=1)
    slug: str | None = None
```

Ours rather than the SDK's `Category`, for the same reason the question models exist at all.
`id` is the only stable identifier: a slug can be renamed and is optional, a name is not.

I left `emoji` and `description` out. `emoji` is presentational, and `description` is free text
that would widen the no-echo surface with no downstream consumer — the identity triple is what
a classifier needs. Say so if you'd rather have the full record; I'd take carrying them over
re-fetching, but I don't think either is needed yet.

**One implementation detail worth your eye on re-review.** The mapping hands the canonical
model **plain dicts**, not constructed `SourceCategory` objects:

```python
"source_categories": [
    {"id": category.id, "name": category.name, "slug": category.slug}
    for category in q.categories
],
```

`_common_fields` runs inside the field-read fence, which catches only
`AttributeError`/`TypeError`. Constructing a Pydantic model there would let a `ValidationError`
escape `normalize_question` entirely — defeating the boundary discipline you approved as CLOSED
in round 1. Letting the canonical model build them keeps that failure inside the
`ValidationError` boundary and therefore inside `_sanitize`. Two tests pin this: a malformed
category arrives as `NormalizationError`, and a planted secret in a category field appears in
neither the message nor the rendered traceback.

New tests: identity-triple carry-through, your two-category collision as an explicit regression
guard, presentational fields excluded, JSON round-trip preserving `id`, and the two error-path
tests above.

## Low — constant-fixture assertions

Accepted. Added `test_common_fields_are_read_from_the_object_not_hardcoded`, covering
`tournament_slugs`, `question_weight`, `open_time`, `close_time`,
`scheduled_resolution_time`, `unit_of_measure`, `page_url` and `background_info` with distinct
synthetic values.

**This immediately caught a fourth instance of the same vacuity class**, in my own new test.
`fake_sdk_question(**overrides)` accepted any key, so writing `url=...` instead of the SDK's
`page_url=...` set an attribute nothing reads — and the assertion passed against the default it
was meant to replace. The helper now asserts every override key is one `normalize` actually
reads.

That is three review rounds finding the same defect shape (vacuous group linkage, empty-category
fixtures, constant common fields), so I closed it at the helper rather than fixing a fourth
instance. Worth checking whether the guard's allowed-key set is right.

## Nit — false `git diff --check` claim

Correct, and worth more than a nit in one respect: the file asserting the gate was the file
breaking it. The round-2 request embeds a diff whose context lines carry trailing whitespace;
27 lines. Stripped, and `git diff --check` is now genuinely clean.

I've stopped quoting gate results from a prior commit — they're now re-run against the commit
being reviewed.

---

## Verification

```
178 passed  (was 173)
ruff check .          All checks passed
ruff format --check   25 files already formatted
mypy --strict src     Success: no issues found in 14 source files
git diff --check      clean  (verified at this commit, not an earlier one)
```

Your collision case re-run and confirmed closed: the two categories now produce different
`source_categories`, with distinct `id`s.

**Same caveat as round 1, now touching more surface:** `follow_imports = "skip"` for
`forecasting_tools.*` means `mypy --strict` cannot check `q.categories`, `category.id`,
`category.name` or `category.slug`. Your round-2 verification against the installed SDK covered
`Category`'s field set; the new reads are `id`/`name`/`slug` off each element.

## Open items I did not act on

- **Your three unverifiable items** — whether real MiniBench data contains one-option questions
  or missing category slugs, and Metaculus's stability guarantees for slugs vs. IDs — are
  genuinely unresolvable from fixtures. The `SourceCategory` shape is deliberately robust to the
  slug question either way, since `id` is carried regardless. The one-option question remains
  the standing risk of `min_length=2`; it will surface as a normalization refusal in the M1-203
  diagnostic event rather than silently, which is the failure mode I'd want.
- **Categorized end-to-end fixture** — agreed it belongs in T-901 and need not block this slice.

If round 3 is clean, this is ready to merge.
