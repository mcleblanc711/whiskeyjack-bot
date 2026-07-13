"""Single construction point for the forecasting-tools MetaculusClient (M0-101).

Everything that talks to Metaculus goes through :func:`build_client`; nothing
else in the codebase may instantiate ``MetaculusClient`` directly. The token
is read from the configured environment variable at construction time, passed
to the SDK, and never stored, logged, or echoed by this module. Constructing
the SDK client performs no network I/O (verified against the pinned
forecasting-tools==0.2.92 source).
"""

from __future__ import annotations

import os

from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.config import AppConfig


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


def build_client(config: AppConfig) -> MetaculusClient:
    """Construct the one configured MetaculusClient.

    Raises :class:`MissingCredentialError` when the configured token variable
    is unset or empty — callers on fixture-only paths must not call this at
    all, so reaching here without a token is always an operator error worth
    failing loudly on.
    """
    token_env = config.metaculus.token_env
    token = os.environ.get(token_env)
    if not token:
        raise MissingCredentialError(token_env)
    return MetaculusClient(
        base_url=config.metaculus.base_url,
        timeout=int(config.metaculus.request_timeout_seconds),
        sleep_seconds_between_requests=config.metaculus.request_spacing_seconds,
        sleep_jitter_seconds=config.metaculus.request_jitter_seconds,
        token=token,
    )
