# GPT cross-model review request — M1-305 (Deduplicate and freshness-tag evidence)

You are a second model reviewing another model's code before merge. Be adversarial. The full
branch diff vs `master` is appended verbatim at the end of this file.

## What M1-305 is

Retrieval-epic item. Direct dependent of M1-301 (the research-evidence schema, merged), which
**deliberately deferred** three things to this item and made it the owner of URL-validation
**policy** beyond a minimal syntactic gate:

1. **URL canonicalization** — derive `canonical_url` from `original_url` so duplicate provider
   reports of one artifact collapse. Feeds the ledger's
   `UNIQUE(retrieval_run_id, canonical_url, content_sha256)`.
2. **Freshness-tagging** — mark evidence outside a question-specific window, deterministically.
3. **URL-validation policy consolidation** — see the regression history below.

**Acceptance criteria:** duplicate reports collapse without losing provenance; stale evidence is
flagged deterministically.

## Hard constraints it had to honour

- No new runtime dependency, no `uv.lock` change, no new migration (PRs #7/#10 are open; #7 touches
  the lock). Nothing here touches the DB.
- CLAUDE.md error hygiene: module-owned sanitized error; messages never echo content
  (URLs/field values/bodies); sanitizing raises use `from None`; paths are the only carve-out.
- `from __future__ import annotations`, `mypy --strict`, pydantic v2 strict base / frozen
  dataclasses, module-level `Literal` enums, line length 100, module docstring names the item.
- Stricter reading of any ambiguity, and note it.

## Design and the deliberate choices (please pressure-test these)

Delivered as **three pure-primitive modules** under `src/whiskeyjack_bot/research/`
(`canonical.py`, `freshness.py`, `dedup.py`) plus `tests/unit/test_dedup_freshness.py`, mirroring
how `hashing.py::content_sha256` is a standalone primitive adapters call. **No schema change, no
migration** — freshness is derived at forecast time from timestamps the schema already carries
(there is no freshness column), so it is recomputed on replay, not persisted.

### The URL-policy regression history — the #1 risk area

This is the crux and where M1-301 bled. From `docs/M1-301-NOTES.md`, the URL validator took three
consecutive review regressions, each introduced by the previous round's fix, all in URL handling:

| Round | Fix applied | What it broke |
|---|---|---|
| 4 | Blanket Unicode `Cf` ban (stop invisible spoofing chars) | Rejected standards-valid IDN hostnames (`نامه‌ای.ir`); U+200C/U+200D are *required* in some labels |
| 5 | `idna.encode()` on the hostname, replacing the ban | Rejected IPv6 literals (`https://[::1]/a`); `urlsplit().hostname` returns IP literals too |
| 6 | `ipaddress.ip_address()` first, `idna` as fallback | — (clean) |

Banked lessons: **delegate to the authority** (`unicodedata`, `idna`, `ipaddress`) rather than
hand-roll; **speculative hardening beyond the reported finding is where the next finding comes
from**; **verify a "still works" claim against the case that could falsify it**.

How this item applied them, and what I want you to attack:

- `canonicalize_url` **reuses `model._require_http_url` as its syntactic gate** (one definition of
  "is this a URL", no second copy to drift). Host classification inside canonicalization branches
  **exactly as `_require_resolvable_hostname`** — `ipaddress` first, `idna` (`uts46=True`) only for
  what is not an IP literal.
- Two **agreement tests** pin this: everything `validate_document` accepts, `canonicalize_url`
  accepts (IDN/IPv6/IPv4 families); everything it rejects, `canonicalize_url` rejects as
  `CanonicalizationError` (Cf/format-char, ZWNJ-out-of-context, space, malformed-IP). A third test
  asserts canonicalize output **re-validates** as a `canonical_url` and is **idempotent**.
- **Please falsify the round-trip claim specifically.** `canonicalize_url` uses
  `idna.encode(host, uts46=True)`; the schema gate re-validates the punycode output with strict
  `idna.encode(host)` (no uts46). Is there a host the uts46 path accepts and normalizes to an
  A-label that strict `idna.encode` would then reject — i.e. a canonicalize output that fails
  `validate_document`? The idempotence + revalidation tests cover the fixtures I thought of
  (ZWNJ/ZWJ IDN, IPv6, IPv4, mixed-case IDN); look for a family they miss.
- Conservative on purpose (the round-4 lesson): canonicalization **does not decode** percent-octets
  (only uppercases hex), and **does not reorder or re-encode** query params. Is uppercasing
  `%xx`→`%XX` ever wrong (e.g. inside an already-decoded segment)? Is splitting the raw query on `&`
  and dropping tracking keys ever lossy for a non-tracking param (e.g. a param literally named
  `utm_source` that is load-bearing — accepted risk, but call it if you see worse)?

### Tracking-param stripping (owner-approved lossy step)

Dedup keys on `canonical_url`, so two reports differing only by `utm_*`/`fbclid`/… never collapse
unless stripped — that's the item's whole point. `_TRACKING_PARAMS` is a closed, documented
`frozenset` matched case-insensitively against the percent-decoded key; every other param is
preserved byte-for-byte in order. **Attack the allowlist**: any entry that is *not* purely a
referrer/analytics tag and could select a resource? Any common tag missing that would defeat dedup?

### Userinfo dropped from canonical URL

Not resource identity, and keeps credentials out of the stored dedup key (secret hygiene);
`original_url` preserves the as-retrieved URL. Agree/disagree?

### Freshness

- **Deterministic by construction**: `assess_freshness` is a pure function of caller-supplied
  timestamps; **no module reads `datetime.now()`**. Caller derives the window with
  `freshness_cutoff(reference, days)`.
- Effective date = `updated_at` if present else `published_at`; `retrieved_at` deliberately unused.
  **Is "updated overrides published even when older" correct?** I pinned it as the rule (updated is
  the effective date whenever present); challenge if you think max(published, updated) is righter.
- **Boundary inclusive at the cutoff** (dated exactly at cutoff → `fresh`). Documented as the
  stricter-reading choice for a "last N days" window. Off-by-one worth checking.
- **Undated → `stale`/`undatable`** (owner-approved stricter reading). `undatable` kept distinct
  from `before_cutoff`. Right call, or does flagging undated evidence over-flag in practice?

### Dedup / provenance preservation — the #2 risk area

- Key = `(canonical_url, content_sha256)` — the ledger UNIQUE minus the run id; scope of a collapse
  is the caller's input set. **Is dropping the run id from the key correct?** Rationale: within one
  run it prevents a UNIQUE violation; across a question's runs it is what lets two providers that
  both surfaced one article collapse (which is where "without losing provenance" bites — within one
  run, provenance is uniform). Attack: could a caller collapse documents that the ledger would (and
  should) store as distinct rows?
- On collision the survivor carries the **stronger** provenance (`direct_api` > `llm_reported`);
  ties break to earliest `retrieved_at_utc`, then first-seen. Is the merge total and
  order-independent? Any way a reported claim displaces a fetched one, or vice-versa?
- The survivor is one whole document; **non-key fields of the collapsed duplicate are discarded**
  (e.g. a title/snippet the loser had). Is that "losing provenance"? My reading: `original_url` and
  the stronger provenance are the provenance that matters, and the two are the same artifact by
  hash. Push on this if you disagree.

### Error hygiene

`CanonicalizationError` is module-owned, constant message (`_BAD_URL`), never echoes the URL, always
`from None`. A test plants a secret in a rejected URL and asserts absence + `__cause__ is None`.
Confirm no raise in the three modules can leak an input-derived value (including via `idna`/
`urlsplit`/`ipaddress` exceptions that embed the offending value).

## Gate status

`uv run pytest` → 418 passed; `ruff check .`, `ruff format --check .`, `mypy --strict src` all
clean.

## Full branch diff vs master

```diff
diff --git a/docs/M1-305-NOTES.md b/docs/M1-305-NOTES.md
new file mode 100644
index 0000000..481d85e
--- /dev/null
+++ b/docs/M1-305-NOTES.md
@@ -0,0 +1,120 @@
+# M1-305 — Deduplicate and freshness-tag evidence — implementation notes
+
+Running record of M1-305 decisions and deviations, in the spirit of `docs/M1-301-NOTES.md`.
+**Merges back into `docs/M1-NOTES.md`** with the rest of the retrieval epic (see the merge-back
+trigger at the top of `docs/M1-301-NOTES.md`).
+
+M1-305 is the direct dependent of M1-301, which deferred three things to it (URL canonicalization,
+duplicate collapsing, freshness-tagging) and made it the owner of URL-validation *policy* beyond the
+minimal syntactic gate. Acceptance: **duplicate reports collapse without losing provenance; stale
+evidence is flagged deterministically.**
+
+## Delivered
+
+Three pure-primitive modules under `src/whiskeyjack_bot/research/`, mirroring how
+`hashing.py::content_sha256` is a standalone primitive that adapters call:
+
+- **`canonical.py`** — `canonicalize_url(url) -> str` and `CanonicalizationError`. Derives the
+  `canonical_url` that `UNIQUE(retrieval_run_id, canonical_url, content_sha256)` keys dedup on.
+- **`freshness.py`** — `FreshnessState`/`FreshnessReason` (`Literal`), `FreshnessVerdict` (frozen),
+  `freshness_cutoff(reference, days)`, `assess_freshness(published, updated, cutoff)`,
+  `assess_document(doc, cutoff)`.
+- **`dedup.py`** — `dedup_key(doc)`, `deduplicate(docs) -> DedupResult`, `DedupResult` (frozen).
+
+Tests: `tests/unit/test_dedup_freshness.py` (47 cases). Full gate green — `pytest` 418 passed,
+`ruff check`, `ruff format --check`, `mypy --strict src` all clean.
+
+## Deliberate choices
+
+- **No schema change, no migration, no new dependency.** Freshness is derived at forecast time from
+  timestamps the schema already carries (there is no freshness column on `research_documents`), so
+  it is recomputed on replay rather than persisted. `canonical_url`/`content_sha256` are minted by
+  adapters using these functions. This is the lowest-drift reading of "nothing here touches the DB",
+  and it keeps the six-round-hardened `model.py` validator untouched. `idna` and `ipaddress` are
+  already declared (M1-301); nothing new enters `pyproject.toml`/`uv.lock`.
+
+- **The URL syntactic gate is reused, not re-implemented.** `canonicalize_url` runs
+  `model._require_http_url` before normalizing, so there is exactly one definition of "is this a URL
+  at all", and a second hand-rolled copy cannot drift from it. This is the direct application of the
+  M1-301 retrospective's banked lesson (delegate to the authority; a hand-rolled second copy is
+  where the next regression comes from). Host classification inside canonicalization branches
+  **exactly as `_require_resolvable_hostname` branches** — `ipaddress` first, `idna` only for what
+  is not an IP literal — because the round-6 finding was precisely that IP literals must never reach
+  `idna`. Two agreement tests pin this: everything `validate_document` accepts, `canonicalize_url`
+  accepts (IDN/IPv6/IPv4 families), and everything it rejects, `canonicalize_url` rejects as
+  `CanonicalizationError` (Cf/format-char, ZWNJ-out-of-context, space, malformed-IP families). A
+  third test asserts canonicalize output re-validates as a `canonical_url` and is idempotent.
+
+- **Canonicalization is conservative where the round history says to be.** It lowercases scheme and
+  host, folds IDN to its A-label (`idna.encode(uts46=True)`) and compresses/re-brackets IPv6, drops
+  the default port, drops the fragment, drops userinfo, uppercases percent-octet hex, and normalizes
+  an empty path to `/`. It does **not** decode percent-octets (decoding an unreserved octet is where
+  subtle equivalence bugs live) and does **not** reorder or re-encode query parameters — order can be
+  load-bearing, and re-encoding could alter a value that is part of the resource identity.
+
+- **Tracking-param stripping is the one lossy step, and it is a closed, documented allowlist**
+  (owner decision, 2026-07-22). Dedup keys on `canonical_url`, so two provider reports of one
+  article differing only by a `utm_*`/`fbclid`/… tag would never collapse unless those tags are
+  removed — which is the whole point of the item. `_TRACKING_PARAMS` is a module-level `frozenset`
+  matched case-insensitively against the percent-decoded key; every non-tracking parameter is
+  preserved byte-for-byte in its original position.
+
+- **Userinfo is dropped from the canonical URL** — it is not part of the resource identity for
+  dedup, and keeping it would write credentials into the stored dedup key. `original_url` still
+  preserves the as-retrieved URL byte-for-byte, so this is not an attribution loss; it is also a
+  secret-hygiene win.
+
+- **Freshness is deterministic by construction.** `assess_freshness` is a pure function of the
+  timestamps the caller supplies; **no module here reads `datetime.now()`**. The caller derives the
+  window with `freshness_cutoff(reference, days)` from a reference time (e.g. the run's
+  `started_at_utc` or the question snapshot time) and `retrieval.freshness_days_default` (or a
+  per-question override). Effective date is `updated_at` when present, else `published_at`;
+  `retrieved_at` is deliberately not used — it records when *we* fetched the document, not how old
+  its content is, and a fresh fetch of stale content is still stale. The boundary is **inclusive at
+  the cutoff**: a document dated exactly at the cutoff is `fresh` (the window is "on or after"). This
+  is documented because "outside the window" is ambiguous at the boundary; the inclusive reading is
+  the natural one for a "last N days" window and is pinned by a test.
+
+- **An undated document is `stale` / `undatable`** (owner decision, 2026-07-22; stricter reading per
+  CLAUDE.md ambiguity rule 4). It cannot be shown to fall within the window, so it is flagged rather
+  than allowed to pass unchecked where M1-504 could never catch it. The `undatable` reason is kept
+  distinct from `before_cutoff` so a consumer can tell "we checked and it is old" from "we could not
+  check".
+
+- **M1-305 only tags; it does not gate.** It never reads `forecast.fail_on_stale_research` /
+  `flag_on_stale_research` — that fail-vs-flag policy is **M1-504** (which depends on this item).
+  Splitting them keeps the epic boundary: tagging is evidence about the document, gating is policy
+  about the forecast.
+
+- **Dedup preserves the stronger provenance.** `deduplicate` collapses documents sharing
+  `(canonical_url, content_sha256)` — the ledger's UNIQUE minus the run id. The key is the artifact
+  identity, so the scope of a collapse is the input the caller passes: one run's documents for
+  strict per-run semantics, or a question's whole set to dedup across providers (which is where
+  "without losing provenance" bites — within one run, provenance is uniform). On collision the
+  survivor carries the **stronger claim** (`direct_api` > `llm_reported`): a verified retrieval is
+  never silently downgraded to a reported one, nor a reported one upgraded to verified. Ties on equal
+  provenance break to the earliest `retrieved_at_utc`, then first-seen — a total, order-independent
+  rule, so the result is stable. First-seen order is preserved. `DedupResult.collapsed_count` is
+  exposed so a future writer can record an auditable dedup counter, in the spirit of
+  `ResearchRun.posts_dropped_no_url`.
+
+## Error hygiene
+
+`CanonicalizationError` is module-owned and sanitized: a URL is row content, so its message is the
+constant `_BAD_URL` and never echoes the input. Every raise uses `from None` (the `idna`/`urlsplit`
+exceptions embed the offending value, and a chained `__cause__` would reprint it through a
+traceback). A test plants a secret in a rejected URL and asserts it is absent from the message and
+that `__cause__ is None`. `freshness.py`/`dedup.py` raise nothing input-derived — they operate on
+already-validated `ResearchDocument`s and pure timestamps.
+
+## Deferred / boundaries (do not read the absence as an omission)
+
+- **Wiring into adapters is the adapters' job.** No adapter exists yet to call these primitives
+  (M1-302/M1-303 are the consumers); M1-305 ships the primitives + tests, exactly as `hashing.py`
+  did. The consumer contract is documented in each module docstring.
+- **The stale/insufficient-research gate is M1-504** (`fail_on_stale_research` /
+  `flag_on_stale_research`), which depends on this item.
+- **Host allow/deny policy (loopback, private ranges, homograph adjudication beyond IDNA) is not
+  decided here.** `model.py` already states shape-validation holds no network code; canonicalization
+  inherits that boundary — whether a *reachable* host is an *appropriate* one belongs to whatever
+  fetches a URL.
diff --git a/docs/backlog/backlog.csv b/docs/backlog/backlog.csv
index 74c6b93..d7ee065 100644
--- a/docs/backlog/backlog.csv
+++ b/docs/backlog/backlog.csv
@@ -16,7 +16,7 @@ M1-301,Retrieval,Define research-document schema,"Normalize provider results wit
 M1-302,Retrieval,Implement AskNews adapter,Retrieve current and historical news while retaining article-level provenance.,M1-301,Critical,Claude Code,Mocked call returns normalized documents; missing credentials fail before a paid call.,M,Not Started,https://docs.asknews.app/
 M1-303,Retrieval,Implement Exa fallback,Use Exa only when AskNews fails or when official-source/web retrieval is required.,M1-301,High,Claude Code,Configured fallback records why it ran and preserves citations; no silent provider switching.,M,Not Started,https://exa.ai/pricing
 M1-304,Retrieval,Add structured-source router,Bypass general retrieval for configured official datasets such as FRED and authoritative resolution sources.,M1-301,Medium,Claude Code,Router produces the same normalized document/observation format and records endpoint parameters.,M,Not Started,https://fred.stlouisfed.org/docs/api/fred/overview.html | D19
-M1-305,Retrieval,Deduplicate and freshness-tag evidence,"Canonicalize URLs, hash content and mark documents outside the question-specific freshness window. Also owns URL-validation policy beyond basic absolute-http(s)-with-a-host syntax: M1-301 kept that validator minimal after three consecutive cross-model review regressions came out of extending it (a blanket Unicode-Cf ban broke valid IDN hostnames; the IDNA check that replaced it broke IPv6 literals), so consolidate the policy here with canonicalization rather than growing it in the schema.",M1-301,High,Claude Code,Duplicate reports collapse without losing provenance; stale evidence is flagged deterministically.,M,Not Started,D17
+M1-305,Retrieval,Deduplicate and freshness-tag evidence,"Canonicalize URLs, hash content and mark documents outside the question-specific freshness window. Also owns URL-validation policy beyond basic absolute-http(s)-with-a-host syntax: M1-301 kept that validator minimal after three consecutive cross-model review regressions came out of extending it (a blanket Unicode-Cf ban broke valid IDN hostnames; the IDNA check that replaced it broke IPv6 literals), so consolidate the policy here with canonicalization rather than growing it in the schema.",M1-301,High,Claude Code,Duplicate reports collapse without losing provenance; stale evidence is flagged deterministically.,M,In Review,D17
 M1-306,Retrieval,Persist replayable retrieval runs,"Save queries, provider configs, raw responses, costs and normalized documents.",M1-302; M1-303,Critical,Claude Code,Replay produces zero provider calls and the same research packet hash.,M,Not Started,D12; D16
 M1-401,Forecast Generation,Version and hash the prompt,"Load prompts/forecaster.md, verify declared version and compute a content hash.",M0-005,Critical,Claude Code,Every forecast stores prompt version/hash; changed bytes produce a new hash.,S,Done,D4; prompts/forecaster.md
 M1-402,Forecast Generation,Implement structured model call,Call one configured GeneralLlm model with a question-specific Pydantic response type.,M1-401; M1-306,Critical,Claude Code,Valid response returns typed output; malformed output gets at most one bounded repair attempt.,M,Not Started,https://github.com/Metaculus/forecasting-tools
diff --git a/src/whiskeyjack_bot/research/__init__.py b/src/whiskeyjack_bot/research/__init__.py
index e64d6b5..454815a 100644
--- a/src/whiskeyjack_bot/research/__init__.py
+++ b/src/whiskeyjack_bot/research/__init__.py
@@ -2,8 +2,19 @@
 
 Adapters (M1-302 AskNews, M1-303 Exa, M1-304 structured router, M1-307 X agent)
 import from here so every provider produces one comparable evidence record.
+Deduplication, freshness-tagging and URL canonicalization are M1-305.
 """
 
+from whiskeyjack_bot.research.canonical import CanonicalizationError, canonicalize_url
+from whiskeyjack_bot.research.dedup import DedupResult, dedup_key, deduplicate
+from whiskeyjack_bot.research.freshness import (
+    FreshnessReason,
+    FreshnessState,
+    FreshnessVerdict,
+    assess_document,
+    assess_freshness,
+    freshness_cutoff,
+)
 from whiskeyjack_bot.research.hashing import content_sha256, normalize_content
 from whiskeyjack_bot.research.model import (
     Provenance,
@@ -18,6 +29,11 @@ from whiskeyjack_bot.research.model import (
 )
 
 __all__ = [
+    "CanonicalizationError",
+    "DedupResult",
+    "FreshnessReason",
+    "FreshnessState",
+    "FreshnessVerdict",
     "Provenance",
     "ReliabilityTag",
     "ResearchDocument",
@@ -25,7 +41,13 @@ __all__ = [
     "ResearchSchemaError",
     "RetrievalProvider",
     "SourceType",
+    "assess_document",
+    "assess_freshness",
+    "canonicalize_url",
     "content_sha256",
+    "dedup_key",
+    "deduplicate",
+    "freshness_cutoff",
     "normalize_content",
     "validate_document",
     "validate_run",
diff --git a/src/whiskeyjack_bot/research/canonical.py b/src/whiskeyjack_bot/research/canonical.py
new file mode 100644
index 0000000..f25b242
--- /dev/null
+++ b/src/whiskeyjack_bot/research/canonical.py
@@ -0,0 +1,192 @@
+"""URL canonicalization and the consolidated URL-validation policy (M1-305).
+
+``canonicalize_url`` derives the ``canonical_url`` that
+``UNIQUE(retrieval_run_id, canonical_url, content_sha256)`` (M1-601) keys dedup
+on. Adapters (M1-302 AskNews, M1-303 Exa, M1-304 structured router, M1-307 X
+agent) call it once per document; until M1-305 they set ``canonical_url`` equal
+to ``original_url`` (M1-301 note). ``original_url`` is preserved on the schema,
+so canonicalization is never an attribution loss: the as-retrieved URL survives.
+
+**This module owns URL-validation policy beyond bare syntax** (backlog M1-305).
+``model._require_http_url`` was kept deliberately minimal after three consecutive
+cross-model review regressions came out of extending it (a blanket Unicode-``Cf``
+ban broke valid IDN hostnames; the IDNA check that replaced it broke IPv6
+literals -- see ``docs/M1-301-NOTES.md``). The lesson banked there governs every
+line below: **delegate host classification to the authority** (``ipaddress``
+first, ``idna`` only for what is not an IP literal) and **do not speculatively
+transform** what a provider sent. So query parameters keep their order and their
+bytes, percent-encoding is only case-normalized (never decoded), and the one
+lossy step -- dropping a documented tracking-parameter allowlist -- exists solely
+because dedup keys on ``canonical_url`` and two reports differing only by a
+``utm_*`` tag must collapse.
+
+The gate is *reused*, not re-implemented: ``canonicalize_url`` runs
+``model._require_http_url`` before normalizing, so there is one definition of
+"is this a URL at all", and a second hand-rolled copy cannot drift from it.
+
+Canonicalization is pure: no network, no wall-clock, string operations only. Its
+output is itself a valid ``HttpUrlString`` -- it round-trips the schema's own gate.
+"""
+
+from __future__ import annotations
+
+import ipaddress
+import re
+from urllib.parse import unquote, urlsplit, urlunsplit
+
+import idna
+
+from whiskeyjack_bot.research.model import _require_http_url
+
+# Query keys removed during canonicalization. Analytics/click-tracking tags that
+# name the referrer, not the resource: two provider reports of one article differ
+# only by these, so keying dedup on a URL that keeps them would never collapse
+# them. Everything else is preserved byte-for-byte -- an ordinary query parameter
+# can select the resource (``?id=42``), so only this closed, documented set goes.
+# Matched case-insensitively against the percent-decoded key.
+_TRACKING_PARAMS: frozenset[str] = frozenset(
+    {
+        "utm_source",
+        "utm_medium",
+        "utm_campaign",
+        "utm_term",
+        "utm_content",
+        "utm_id",
+        "utm_name",
+        "utm_cid",
+        "utm_reader",
+        "utm_source_platform",
+        "gclid",
+        "gclsrc",
+        "dclid",
+        "wbraid",
+        "gbraid",
+        "fbclid",
+        "fb_action_ids",
+        "fb_action_types",
+        "fb_ref",
+        "fb_source",
+        "msclkid",
+        "mc_cid",
+        "mc_eid",
+        "_hsenc",
+        "_hsmi",
+        "igshid",
+        "yclid",
+        "twclid",
+        "ttclid",
+        "vero_id",
+        "vero_conv",
+        "oly_anon_id",
+        "oly_enc_id",
+        "spm",
+    }
+)
+
+# Default port per scheme, dropped from the canonical authority.
+_DEFAULT_PORT: dict[str, int] = {"http": 80, "https": 443}
+
+# A percent-encoded octet; the hex digits are uppercased (RFC 3986 s6.2.2.1).
+# Deliberately only case-normalized, never decoded: decoding an unreserved octet
+# is where subtle equivalence bugs live, and this module does not take that risk.
+_PERCENT_OCTET_RE = re.compile(r"%[0-9a-fA-F]{2}")
+
+
+class CanonicalizationError(Exception):
+    """A URL could not be canonicalized, with the offending input withheld.
+
+    Same hygiene rule as ``ResearchSchemaError``/``ConfigError``: a URL is row
+    content, so the message is a constant that never echoes it. Every raise here
+    uses ``from None`` -- ``idna`` and ``urlsplit`` embed the offending value in
+    their own exceptions, and a chained ``__cause__`` reprints it in a traceback.
+    """
+
+
+# Mirrors model._BAD_URL: constant, value-free, names no fragment of the input.
+_BAD_URL = "not a canonicalizable http(s) URL (offending input withheld)"
+
+
+def _canonical_host(host: str) -> str:
+    """Return the canonical form of a validated host.
+
+    Branched exactly as ``model._require_resolvable_hostname`` branches, for the
+    reason recorded there: ``urlsplit().hostname`` returns IP literals as well as
+    domain names, and ``idna`` refuses IP literals. So an IP literal answers to
+    ``ipaddress`` (compressed, and re-bracketed for IPv6, which is how it must
+    reassemble into an authority) and everything else to ``idna`` (IDNA 2008
+    A-label, lowercased). ``uts46=True`` folds case/width so a mixed-case IDN host
+    canonicalizes; a host that already passed the stricter gate cannot fail here.
+    """
+    try:
+        ip = ipaddress.ip_address(host)
+    except ValueError:
+        pass  # Not an IP literal, so it must be a domain name.
+    else:
+        return f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
+    try:
+        return idna.encode(host, uts46=True).decode("ascii")
+    except idna.IDNAError:
+        raise CanonicalizationError(_BAD_URL) from None
+
+
+def _strip_tracking(query: str) -> str:
+    """Drop tracking pairs; keep everything else in order, byte-for-byte.
+
+    Splits on the raw ``&`` rather than round-tripping through ``parse_qsl`` /
+    ``urlencode`` so a preserved parameter is stored exactly as the provider sent
+    it -- re-encoding could alter a value that is part of the resource identity.
+    """
+    if not query:
+        return query
+    kept: list[str] = []
+    for pair in query.split("&"):
+        if not pair:
+            continue  # An empty segment ("a&&b") carries nothing; not preserved.
+        key = pair.split("=", 1)[0]
+        if unquote(key).lower() in _TRACKING_PARAMS:
+            continue
+        kept.append(pair)
+    return "&".join(kept)
+
+
+def _uppercase_percent_octets(text: str) -> str:
+    return _PERCENT_OCTET_RE.sub(lambda m: m.group(0).upper(), text)
+
+
+def canonicalize_url(url: str) -> str:
+    """Return the canonical form of ``url`` for dedup, or raise on non-URLs.
+
+    Reuses ``model._require_http_url`` as the syntactic gate, then normalizes the
+    parts that vary cosmetically between reports of one resource: it lowercases
+    the scheme, canonicalizes the host (IDN -> A-label, IPv6 compressed), drops
+    the default port, drops userinfo and the fragment, uppercases percent-octet
+    hex, and strips tracking parameters. Query order and every non-tracking
+    parameter are preserved. The result validates as an ``HttpUrlString``.
+    """
+    try:
+        _require_http_url(url)
+    except ValueError:
+        # from None and a constant message: _require_http_url raises the sanitized
+        # model._BAD_URL, but re-raising as this module's own error keeps callers
+        # handling a single type, and drops any __cause__ that could reprint input.
+        raise CanonicalizationError(_BAD_URL) from None
+
+    parts = urlsplit(url)
+    scheme = parts.scheme.lower()
+    # .hostname is guaranteed present by the gate; lower() is redundant with what
+    # urlsplit already does but makes the canonical intent explicit for the reader.
+    host = _canonical_host(parts.hostname or "")
+
+    netloc = host
+    port = parts.port
+    if port is not None and port != _DEFAULT_PORT[scheme]:
+        netloc = f"{host}:{port}"
+
+    path = _uppercase_percent_octets(parts.path) or "/"
+    query = _uppercase_percent_octets(_strip_tracking(parts.query))
+
+    # Fragment dropped: it is never sent to the server, so it is not part of the
+    # resource identity. Userinfo dropped by rebuilding netloc from host alone --
+    # it is not resource identity either, and keeping it would write credentials
+    # into the stored dedup key (original_url still preserves the as-retrieved URL).
+    return urlunsplit((scheme, netloc, path, query, ""))
diff --git a/src/whiskeyjack_bot/research/dedup.py b/src/whiskeyjack_bot/research/dedup.py
new file mode 100644
index 0000000..08d001e
--- /dev/null
+++ b/src/whiskeyjack_bot/research/dedup.py
@@ -0,0 +1,91 @@
+"""Provenance-preserving deduplication of research evidence (M1-305).
+
+Collapses documents that are the **same underlying artifact** -- identical
+``canonical_url`` *and* identical ``content_sha256`` -- into one, so a forecaster
+is not shown, and the ledger is not asked to store, the same article twice. The
+key mirrors the ledger's ``UNIQUE(retrieval_run_id, canonical_url,
+content_sha256)`` (M1-601) minus the run id: within one run this prevents a
+constraint violation, and across a question's runs it is what lets two providers
+that both surfaced one article collapse to a single piece of evidence. The scope
+of a collapse is the input the caller passes -- one run's documents for strict
+per-run semantics, or a question's whole set to dedup across providers.
+
+**Without losing provenance** is the acceptance criterion and the delicate part.
+``provenance`` distinguishes a document the pipeline fetched (``direct_api``) from
+one a research agent merely reported (``llm_reported``), and the forecaster
+prompt's evidence caps read it. When the same artifact arrives both ways, the
+survivor must carry the *stronger* claim (``direct_api``): a verified retrieval is
+never silently downgraded to a reported one, nor a reported one upgraded to
+verified. ``original_url`` is a schema field on whichever document survives, so
+the as-retrieved URL is never lost either.
+
+Pure and deterministic: no I/O, first-seen order preserved, ties broken by a
+total, timestamp-based rule so the same input always yields the same output.
+"""
+
+from __future__ import annotations
+
+from collections.abc import Iterable
+from dataclasses import dataclass
+
+from whiskeyjack_bot.research.model import Provenance, ResearchDocument
+
+# Lower rank is the stronger, more-attributable claim and wins a collapse. A
+# pipeline-fetched document outranks an agent-reported one; see the module
+# docstring for why the direction is never reversed.
+_PROVENANCE_RANK: dict[Provenance, int] = {"direct_api": 0, "llm_reported": 1}
+
+
+@dataclass(frozen=True)
+class DedupResult:
+    """The collapsed document set and how many duplicates were removed.
+
+    ``collapsed_count`` is the number of documents dropped as duplicates (input
+    length minus ``documents`` length); it is exposed so a writer can record an
+    auditable dedup counter, in the spirit of ``ResearchRun.posts_dropped_no_url``.
+    """
+
+    documents: tuple[ResearchDocument, ...]
+    collapsed_count: int
+
+
+def dedup_key(document: ResearchDocument) -> tuple[str, str]:
+    """The artifact identity a collapse is keyed on: ``(canonical_url, hash)``."""
+    return (document.canonical_url, document.content_sha256)
+
+
+def _prefer(current: ResearchDocument, candidate: ResearchDocument) -> ResearchDocument:
+    """Choose the survivor of two same-artifact documents.
+
+    Stronger provenance wins; on equal provenance the earliest ``retrieved_at_utc``
+    wins (the first observation of the artifact); a remaining tie keeps the
+    first-seen document. Total and order-independent, so the result is stable.
+    """
+    current_rank = _PROVENANCE_RANK[current.provenance]
+    candidate_rank = _PROVENANCE_RANK[candidate.provenance]
+    if candidate_rank < current_rank:
+        return candidate
+    if candidate_rank > current_rank:
+        return current
+    if candidate.retrieved_at_utc < current.retrieved_at_utc:
+        return candidate
+    return current
+
+
+def deduplicate(documents: Iterable[ResearchDocument]) -> DedupResult:
+    """Collapse same-artifact documents, preserving the strongest provenance.
+
+    Returns the survivors in first-seen order and the count of duplicates removed.
+    """
+    survivors: dict[tuple[str, str], ResearchDocument] = {}
+    order: list[tuple[str, str]] = []
+    collapsed = 0
+    for document in documents:
+        key = dedup_key(document)
+        if key not in survivors:
+            survivors[key] = document
+            order.append(key)
+        else:
+            collapsed += 1
+            survivors[key] = _prefer(survivors[key], document)
+    return DedupResult(documents=tuple(survivors[key] for key in order), collapsed_count=collapsed)
diff --git a/src/whiskeyjack_bot/research/freshness.py b/src/whiskeyjack_bot/research/freshness.py
new file mode 100644
index 0000000..3c53b19
--- /dev/null
+++ b/src/whiskeyjack_bot/research/freshness.py
@@ -0,0 +1,101 @@
+"""Deterministic freshness-tagging of research evidence (M1-305).
+
+Marks a document fresh or stale relative to a question-specific window. The two
+acceptance words are load-bearing: **deterministic** means the verdict is a pure
+function of timestamps the caller supplies -- it never reads the wall clock, so a
+replay of a stored forecast reproduces the same tags -- and **flagged** means this
+module only *tags*. Whether a stale document fails the run or merely annotates it
+is ``forecast.fail_on_stale_research`` / ``flag_on_stale_research``, and that gate
+is M1-504 (which depends on this item). Splitting them keeps the epic boundary:
+tagging is evidence about the document, gating is policy about the forecast.
+
+The window is expressed as a cutoff instant. A caller derives it with
+``freshness_cutoff`` from a reference time (e.g. the run's ``started_at_utc`` or
+the question snapshot time) and a day count (``retrieval.freshness_days_default``,
+or a per-question override), then asks ``assess_freshness`` per document. A
+document's effective date is ``updated_at_utc`` when present, else
+``published_at_utc``; ``retrieved_at_utc`` is deliberately not used -- it records
+when *we* fetched the document, not how old its content is, and a fresh fetch of
+stale content is still stale evidence.
+
+An **undated** document (neither published nor updated) is tagged ``stale`` with
+reason ``undatable``: it cannot be shown to fall within the window, and the
+stricter reading (CLAUDE.md ambiguity rule 4) flags what it cannot prove rather
+than letting undated evidence pass unchecked where M1-504 could never catch it.
+"""
+
+from __future__ import annotations
+
+from dataclasses import dataclass
+from datetime import datetime, timedelta
+from typing import Literal
+
+from whiskeyjack_bot.research.model import ResearchDocument
+
+# Fresh: effective date is at or after the cutoff. Stale: before it, or undatable.
+FreshnessState = Literal["fresh", "stale"]
+
+# Why a verdict landed where it did. ``within_window`` and ``before_cutoff`` are
+# dated outcomes; ``undatable`` is the no-timestamp case, kept distinct so a
+# consumer (M1-504) can tell "we checked and it is old" from "we could not check".
+FreshnessReason = Literal["within_window", "before_cutoff", "undatable"]
+
+
+@dataclass(frozen=True)
+class FreshnessVerdict:
+    """The freshness of one document against one cutoff.
+
+    A value object, not stored: freshness is derived at forecast time from
+    timestamps the schema already carries, so it is recomputed on replay rather
+    than persisted (there is no freshness column on ``research_documents``).
+    """
+
+    state: FreshnessState
+    reason: FreshnessReason
+    cutoff: datetime
+    # None exactly when reason is ``undatable``; otherwise the date compared.
+    effective_date: datetime | None
+
+
+def freshness_cutoff(reference: datetime, days: int) -> datetime:
+    """Return the oldest instant a document may be dated and still count as fresh.
+
+    Pure subtraction: the caller owns ``reference`` (never ``datetime.now()`` in
+    this module) so the window, and therefore every verdict against it, is
+    reproducible on replay. ``days`` is validated at the config boundary
+    (``retrieval.freshness_days_default`` is ``ge=1``); this function does not
+    re-police it.
+    """
+    return reference - timedelta(days=days)
+
+
+def assess_freshness(
+    published_at: datetime | None,
+    updated_at: datetime | None,
+    cutoff: datetime,
+) -> FreshnessVerdict:
+    """Tag a document fresh or stale against ``cutoff``, deterministically.
+
+    Effective date is ``updated_at`` when present, else ``published_at`` -- the
+    most recent evidence that the content is current. Undated -> ``stale`` /
+    ``undatable``. Otherwise the boundary is inclusive at the cutoff: a document
+    dated exactly at ``cutoff`` is ``fresh`` (the window is "on or after"), and
+    only a strictly earlier date is ``stale`` / ``before_cutoff``.
+    """
+    effective_date = updated_at if updated_at is not None else published_at
+    if effective_date is None:
+        return FreshnessVerdict(
+            state="stale", reason="undatable", cutoff=cutoff, effective_date=None
+        )
+    if effective_date < cutoff:
+        return FreshnessVerdict(
+            state="stale", reason="before_cutoff", cutoff=cutoff, effective_date=effective_date
+        )
+    return FreshnessVerdict(
+        state="fresh", reason="within_window", cutoff=cutoff, effective_date=effective_date
+    )
+
+
+def assess_document(document: ResearchDocument, cutoff: datetime) -> FreshnessVerdict:
+    """``assess_freshness`` reading the two timestamp fields off a document."""
+    return assess_freshness(document.published_at_utc, document.updated_at_utc, cutoff)
diff --git a/tests/unit/test_dedup_freshness.py b/tests/unit/test_dedup_freshness.py
new file mode 100644
index 0000000..109bd18
--- /dev/null
+++ b/tests/unit/test_dedup_freshness.py
@@ -0,0 +1,265 @@
+"""M1-305: URL canonicalization consolidates the URL policy without regressing
+the IDN/IPv6/Cf cases M1-301 fought through, freshness-tagging is deterministic
+and flags what it cannot date, and duplicate artifacts collapse without losing
+the stronger provenance."""
+
+from __future__ import annotations
+
+from datetime import datetime, timezone
+
+import pytest
+
+from whiskeyjack_bot.research import (
+    CanonicalizationError,
+    ResearchDocument,
+    assess_document,
+    assess_freshness,
+    canonicalize_url,
+    content_sha256,
+    deduplicate,
+    freshness_cutoff,
+    validate_document,
+)
+
+TS = "2026-07-17T00:00:00+00:00"
+SHA = "a" * 64
+
+
+def _document(**overrides: object) -> ResearchDocument:
+    data: dict[str, object] = {
+        "retrieval_run_id": "run-1",
+        "original_url": "https://example.org/a",
+        "canonical_url": "https://example.org/a",
+        "retrieved_at_utc": TS,
+        "source_type": "news",
+        "provenance": "direct_api",
+        "content_sha256": SHA,
+    }
+    data.update(overrides)
+    return validate_document(data)
+
+
+# --- canonicalization -------------------------------------------------------
+
+
+@pytest.mark.parametrize(
+    "url, expected",
+    [
+        # The exact case model.py's test_url_validation_does_not_rewrite_the_url
+        # preserves verbatim -- canonicalization is where it collapses.
+        ("https://example.org:443/a/b?utm_source=x&q=1#frag", "https://example.org/a/b?q=1"),
+        # Default ports for both schemes, and an empty path becomes "/".
+        ("http://example.org:80/", "http://example.org/"),
+        ("https://example.org", "https://example.org/"),
+        # Scheme and host lowercase; path case is content and is preserved.
+        ("HTTPS://EXAMPLE.ORG/A", "https://example.org/A"),
+        # Userinfo is dropped -- not resource identity, and keeps credentials out
+        # of the stored dedup key.
+        ("https://user:pass@example.org/a", "https://example.org/a"),
+        # A non-default port survives.
+        ("https://8.8.8.8:8080/a", "https://8.8.8.8:8080/a"),
+        # Percent-octet hex is uppercased, never decoded.
+        ("https://example.org/%e2%98%83?x=%2f", "https://example.org/%E2%98%83?x=%2F"),
+        # Tracking params drop; every other param keeps its place and its bytes.
+        (
+            "https://example.org/a?a=1&utm_source=x&b=2&fbclid=y&c=3",
+            "https://example.org/a?a=1&b=2&c=3",
+        ),
+        # IDN host folds to its A-label; IPv6 compresses and re-brackets.
+        ("https://MÜNCHEN.DE/a", "https://xn--mnchen-3ya.de/a"),
+        ("https://[2001:0db8:0000:0000:0000:0000:0000:0001]:443/a", "https://[2001:db8::1]/a"),
+        ("https://[::1]/a", "https://[::1]/a"),
+    ],
+)
+def test_canonicalize_url_normalizes_expected_forms(url: str, expected: str) -> None:
+    assert canonicalize_url(url) == expected
+
+
+@pytest.mark.parametrize(
+    "url",
+    [
+        "https://example.org:443/a/b?utm_source=x&q=1#frag",
+        "https://MÜNCHEN.DE/a",
+        "https://نامه‌ای.ir/a",  # Persian ZWNJ
+        "https://क्‍ष.com/a",  # Devanagari ZWJ
+        "https://[2001:db8::1]:443/a",
+        "https://8.8.8.8:8080/a",
+        "http://example.org:80/",
+    ],
+)
+def test_canonical_output_revalidates_and_is_idempotent(url: str) -> None:
+    once = canonicalize_url(url)
+    # The output is itself a valid canonical_url: it round-trips the schema gate.
+    assert validate_document(_document(canonical_url=once)).canonical_url == once
+    # Canonicalizing an already-canonical URL is a fixed point.
+    assert canonicalize_url(once) == once
+
+
+@pytest.mark.parametrize(
+    "url",
+    [
+        # Standards-valid international hostnames model.py accepts: canonicalize
+        # must accept them too (policy consolidation, not a second, stricter gate).
+        "https://نامه‌ای.ir/a",
+        "https://क्‍ष.com/a",
+        "https://münchen.de/a",
+        "https://例え.jp/a",
+        "https://[fe80::1]/a",
+        "https://192.168.1.1/a",
+    ],
+)
+def test_canonicalize_accepts_what_the_schema_accepts(url: str) -> None:
+    # Agreement direction 1: everything validate_document accepts, canonicalize
+    # accepts. (Both are exercised on the same fixtures the M1-301 rounds added.)
+    validate_document(_document(original_url=url))
+    canonicalize_url(url)
+
+
+@pytest.mark.parametrize(
+    "url",
+    [
+        # Cf that IDNA refuses everywhere; ZWNJ out of context; a space; a
+        # bracketed non-address; and inputs that are not http(s) URLs at all.
+        "https://exa​mple.org/a",  # zero-width space
+        "https://ex‮ample.org/a",  # right-to-left override
+        "https://ab‌cd.com/a",  # ZWNJ in a disallowed context
+        "https://exam ple.org/a",  # raw space
+        "https://[gg::1]/a",  # bracketed but not an address
+        "https://[::1]:99999/a",  # out-of-range port
+        "ftp://example.org/a",  # scheme we never retrieve over
+        "not a url",
+        "/relative/path",
+    ],
+)
+def test_canonicalize_rejects_what_the_schema_rejects(url: str) -> None:
+    # Agreement direction 2: everything the schema refuses, canonicalize refuses
+    # too -- and as CanonicalizationError, so callers handle one type.
+    from whiskeyjack_bot.research import ResearchSchemaError
+
+    with pytest.raises(ResearchSchemaError):
+        validate_document(_document(original_url=url))
+    with pytest.raises(CanonicalizationError):
+        canonicalize_url(url)
+
+
+def test_canonicalization_error_never_echoes_the_url() -> None:
+    secret = "hunter2-do-not-print"
+    try:
+        canonicalize_url(f"https://exa mple.org/{secret}")
+    except CanonicalizationError as exc:
+        assert secret not in str(exc)
+        # from None: no __cause__ to reprint the input through a traceback.
+        assert exc.__cause__ is None
+    else:  # pragma: no cover - the call must raise
+        pytest.fail("expected CanonicalizationError")
+
+
+# --- freshness --------------------------------------------------------------
+
+CUTOFF = datetime(2026, 7, 1, tzinfo=timezone.utc)
+BEFORE = datetime(2026, 6, 1, tzinfo=timezone.utc)
+AFTER = datetime(2026, 7, 15, tzinfo=timezone.utc)
+
+
+def test_freshness_cutoff_is_pure_subtraction() -> None:
+    reference = datetime(2026, 7, 31, tzinfo=timezone.utc)
+    assert freshness_cutoff(reference, 30) == datetime(2026, 7, 1, tzinfo=timezone.utc)
+
+
+def test_document_after_cutoff_is_fresh() -> None:
+    verdict = assess_freshness(published_at=AFTER, updated_at=None, cutoff=CUTOFF)
+    assert verdict.state == "fresh"
+    assert verdict.reason == "within_window"
+    assert verdict.effective_date == AFTER
+
+
+def test_document_before_cutoff_is_stale() -> None:
+    verdict = assess_freshness(published_at=BEFORE, updated_at=None, cutoff=CUTOFF)
+    assert verdict.state == "stale"
+    assert verdict.reason == "before_cutoff"
+
+
+def test_boundary_instant_is_fresh() -> None:
+    # The window is "on or after" the cutoff: exactly at the cutoff is fresh.
+    verdict = assess_freshness(published_at=CUTOFF, updated_at=None, cutoff=CUTOFF)
+    assert verdict.state == "fresh"
+
+
+def test_updated_at_overrides_published_at_in_both_directions() -> None:
+    # A stale publish date rescued by a recent update.
+    assert assess_freshness(published_at=BEFORE, updated_at=AFTER, cutoff=CUTOFF).state == "fresh"
+    # And a recent publish date superseded by an older update: updated_at is the
+    # effective date whenever it is present, not merely when it helps.
+    assert assess_freshness(published_at=AFTER, updated_at=BEFORE, cutoff=CUTOFF).state == "stale"
+
+
+def test_undated_document_is_stale_and_undatable() -> None:
+    verdict = assess_freshness(published_at=None, updated_at=None, cutoff=CUTOFF)
+    assert verdict.state == "stale"
+    assert verdict.reason == "undatable"
+    assert verdict.effective_date is None
+
+
+def test_assess_is_deterministic() -> None:
+    a = assess_freshness(published_at=AFTER, updated_at=None, cutoff=CUTOFF)
+    b = assess_freshness(published_at=AFTER, updated_at=None, cutoff=CUTOFF)
+    assert a == b
+
+
+def test_assess_document_reads_the_schema_fields() -> None:
+    doc = _document(published_at_utc=BEFORE.isoformat())
+    assert assess_document(doc, CUTOFF).state == "stale"
+
+
+# --- deduplication ----------------------------------------------------------
+
+
+def _hash(text: str) -> str:
+    return content_sha256(text)
+
+
+def test_identical_artifacts_collapse() -> None:
+    body = _hash("payrolls rose")
+    a = _document(content_sha256=body)
+    b = _document(content_sha256=body)
+    result = deduplicate([a, b])
+    assert len(result.documents) == 1
+    assert result.collapsed_count == 1
+
+
+def test_distinct_artifacts_are_not_collapsed_and_keep_order() -> None:
+    first = _document(canonical_url="https://example.org/1", content_sha256=_hash("one"))
+    second = _document(canonical_url="https://example.org/2", content_sha256=_hash("two"))
+    result = deduplicate([first, second])
+    assert result.collapsed_count == 0
+    assert [d.canonical_url for d in result.documents] == [
+        "https://example.org/1",
+        "https://example.org/2",
+    ]
+
+
+@pytest.mark.parametrize("reported_first", [True, False])
+def test_collapse_keeps_the_stronger_provenance(reported_first: bool) -> None:
+    body = _hash("same article, two providers")
+    fetched = _document(provenance="direct_api", content_sha256=body)
+    reported = _document(
+        source_type="social",
+        provenance="llm_reported",
+        reliability_tag="unverified_social",
+        content_sha256=body,
+    )
+    order = [reported, fetched] if reported_first else [fetched, reported]
+    result = deduplicate(order)
+    assert len(result.documents) == 1
+    # Regardless of arrival order, the survivor is the verified retrieval: a
+    # reported claim never silently displaces a fetched one.
+    assert result.documents[0].provenance == "direct_api"
+
+
+def test_equal_provenance_ties_break_to_earliest_retrieval() -> None:
+    body = _hash("one artifact, two fetches")
+    later = _document(retrieved_at_utc="2026-07-17T12:00:00+00:00", content_sha256=body)
+    earlier = _document(retrieved_at_utc="2026-07-17T06:00:00+00:00", content_sha256=body)
+    result = deduplicate([later, earlier])
+    assert len(result.documents) == 1
+    assert result.documents[0].retrieved_at_utc == datetime(2026, 7, 17, 6, tzinfo=timezone.utc)
```
