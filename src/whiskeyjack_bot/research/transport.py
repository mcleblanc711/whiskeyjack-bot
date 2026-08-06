"""Shared HTTP transport tuning for the retrieval adapters (M1-302, M1-303).

One function, and it lives here rather than in an adapter because it encodes a
two-library workaround that is wrong in several plausible ways and right in one.
AskNews (M1-302) and Exa (M1-303) both need it; a copy in each is a copy that
drifts, and the reasoning in the docstring is the point of the code.
"""

from __future__ import annotations

import httpx


def apply_connection_retries(http_client: httpx.Client, retries: int) -> None:
    """Set connection-failure retries on the direct transport's connection pool.

    Applied post-construction rather than via ``transport=``: passing a transport
    to ``httpx.Client`` forces ``allow_env_proxies=False`` (``Client.__init__``,
    httpx 0.28), which drops ``HTTP(S)_PROXY`` routing entirely. Building the
    client normally keeps the env-proxy mounts, and we set the retry count on the
    default transport's pool. httpcore reads ``_pool._retries`` when it lazily
    creates each connection (``ConnectionPool.create_connection`` ->
    ``HTTPConnection(retries=self._retries, ...)``), which happens on the first
    request -- after this runs -- so the assignment takes effect.

    Only the direct transport is touched. A proxy mount's pool is an
    ``httpcore.HTTPProxy`` whose ``create_connection`` builds a
    ``ForwardHTTPConnection``/``TunnelHTTPConnection`` and threads no ``retries``
    into it, so setting ``_pool._retries`` there would be dead storage -- the
    tunneled connection would still use 0. Retries on the proxied hop are out of
    scope for M1-302/M1-303.

    Scope of what a retry means here, on two axes:

    - **Kind:** an ``httpx`` transport retries **connection failures only**, not
      HTTP 5xx. That is the safe kind for a metered API, because a request that
      reached the server is never re-sent and so cannot be billed twice.
    - **Path:** direct connections only, per the paragraph above.

    A client built with an explicit transport (``httpx.MockTransport`` under
    test, or any custom transport) simply has no ``_pool``; that is a no-op
    rather than an error, so callers need no special case.
    """
    pool = getattr(http_client._transport, "_pool", None)
    if pool is not None:
        pool._retries = retries
