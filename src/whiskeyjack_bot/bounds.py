"""The field-length bounds the ledger's writers and its schema both enforce (M1-608).

Six modules used to spell these numbers themselves -- ``lifecycle``, ``approval``,
``submission``, ``submission_gateway``, ``submission_live`` and ``forecast.record`` -- each
under a comment saying it matched the others. They did match, at every revision anyone
checked. Nothing held them there: a change to one would have let two entry points accept
different ``record_id`` sets, and a record written through one path and unreadable through
the other is exactly the failure the attribution ledger exists to make impossible.

This module is the one public contract they now derive from. It imports nothing from this
package, deliberately: ``forecast/record.py`` is a pydantic schema module and must be able
to take a number from here without acquiring a dependency on the ledger writer stack.

**Only the constants are shared, not the validators.** Each module keeps its own
``_require_identifier`` raising its own sanitized error type, for the reason
``approval._require_identifier``'s docstring gives -- a shared helper would have to raise
one module's error inside another. Two spellings of a *rule* that are tested for equality
is a different thing from two spellings of a *number* that are not tested at all, and only
the second was M1-608.

**The four bounds are not equally pinned, and the difference matters more than the
numbers.**

``MAX_IDENTIFIER_LENGTH`` is enumerated as the literal ``200`` in the ``length(...) > 200``
clauses of migrations ``004``, ``006``, ``007``, ``009`` and ``010``. A migration on master
is immutable by checksum, so this number cannot be moved without the schema disagreeing --
and ``tests/property/test_lifecycle_properties.py`` probes a real INSERT alongside every
Python validator, so the disagreement is a test failure rather than a row that is
append-only and permanently unlookupable.

``MAX_ACTOR_LENGTH`` is witnessed by one column: ``010``'s clause on
``submission_key_releases.released_by``. ``lifecycle``'s own ``actor`` columns (``003``,
``004``) carry no ceiling at all, so for those this is writer policy alone.

``MAX_NOTE_LENGTH`` and ``MAX_BODY_LENGTH`` have **no schema clause anywhere**. What holds
them together is cross-module agreement between the writers, tested, and nothing else.
Do not read their presence here as a claim that the ledger enforces them.

``MAX_ACTOR_LENGTH`` stays a separate name from ``MAX_IDENTIFIER_LENGTH`` although both are
200. Collapsing them would be this item's own defect pointed the other way: two bounds that
agree by coincidence, fused into one that cannot move independently. An identifier is the
value every other table points at; an actor name is prose that ``010`` happens to guard
(M2-710). They are different fields that currently share a number.

These bound what a single row can put into the ledger. They are not a substitute for
M1-605's redaction.
"""

from __future__ import annotations

MAX_IDENTIFIER_LENGTH = 200
MAX_ACTOR_LENGTH = 200
MAX_NOTE_LENGTH = 4000
MAX_BODY_LENGTH = 65536
