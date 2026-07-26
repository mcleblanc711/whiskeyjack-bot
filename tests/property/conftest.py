"""Hypothesis profiles for the property suite.

CI must be reproducible, so it replays a fixed set of examples; locally the search
is randomized, which is where new counterexamples come from. Set
``HYPOTHESIS_PROFILE=dev`` to force the randomized profile in a CI-like shell.
"""

from __future__ import annotations

import os

from hypothesis import settings

settings.register_profile("ci", max_examples=200, derandomize=True, deadline=None)
settings.register_profile("dev", max_examples=200, deadline=None)
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)
