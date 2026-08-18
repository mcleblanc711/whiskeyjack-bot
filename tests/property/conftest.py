"""Hypothesis profiles for the property suite.

CI must be reproducible, so it replays a fixed set of examples; locally the search
is randomized, which is where new counterexamples come from. Set
``HYPOTHESIS_PROFILE=dev`` to force the randomized profile in a CI-like shell.

``fast`` is for the inner loop only -- writing a property, watching it fail against the
pre-fix code, watching it pass after. It is **not** a gate: 25 draws is enough to see a
property work and nowhere near enough to trust one. Three of M1-303's ten new properties
passed against broken code at the full 200 draws (docs/LESSONS.md, lesson 5); at 25 the
odds are worse, which is exactly why the guard ships with the profile.
``scripts/review-request.py`` overrides ``HYPOTHESIS_PROFILE`` for the gate run it performs
and prints the profile that actually ran, so an exported ``fast`` in the author's shell
cannot make a review request claim a green suite the reviewer would not get.

Measured 2026-08-17, per file, because the whole-directory number hides what matters:

    test_dedup_properties.py       19.7s -> 2.9s   (7x; essentially the process floor)
    test_lifecycle_properties.py    3.5s -> 2.4s   (already at the floor -- its tests
                                                    carry their own @settings, which
                                                    override the profile)
    test_exa_properties.py         15.7s -> 8.7s   (its calls total <1s under fast; the
                                                    residual is import cost, not draws)
    tests/property, all of it      52.6s -> 25.0s

So the profile pays on the file you are iterating on, which is what the inner loop runs.
"""

from __future__ import annotations

import os

from hypothesis import settings

# The two profiles a verdict may rest on.
settings.register_profile("ci", max_examples=200, derandomize=True, deadline=None)
settings.register_profile("dev", max_examples=200, deadline=None)

# The inner-loop profile. Never a gate; see the module docstring.
settings.register_profile("fast", max_examples=25, deadline=None)

FULL_PROFILES = frozenset({"ci", "dev"})

settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)
