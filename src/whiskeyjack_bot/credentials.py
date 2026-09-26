"""The missing-credential error, in a module that holds no network client (M1-320).

``MissingCredentialError`` used to live in ``metaculus/client.py`` beside ``build_poster`` and
``SingleAttemptPoster``, so every retrieval and generation adapter that raises it -- and so
``pipeline_live`` -- put the Metaculus posting client on its import graph for an exception
class. Here it imports nothing at all, which is what lets M1-315's guard forbid
``metaculus.client`` on the paid path outright.
"""

from __future__ import annotations


class MissingCredentialError(Exception):
    """A required credential environment variable is unset.

    Raised before any network attempt; the message names the variable and
    never contains a value.
    """

    def __init__(self, env_var_name: str):
        self.env_var_name = env_var_name
        super().__init__(
            f"environment variable {env_var_name} is not set; "
            "set it in the environment (never in config or code)"
        )
