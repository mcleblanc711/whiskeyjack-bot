# M1-302 — response to review round 2

Branch: `feat/m1-302-asknews-adapter`.

**The one blocking P1 is accepted and fixed.** The round-1 retry fix restored a real
capability (retries) at the cost of a silent one (env-proxy routing) — a bad trade, and
exactly right to block on.

Gate at the tip: **356 passed** (was 355 — one test added, one rewritten), ruff check/format
clean, `mypy --strict` clean.

---

## P1 — retry transport disabled env-proxy routing

**Confirmed exactly as reported, and reproduced against the pinned httpx 0.28.1 before
changing anything.** `httpx.Client.__init__` computes:

```python
allow_env_proxies = trust_env and transport is None
proxy_map = self._get_proxy_map(proxy, allow_env_proxies)
```

So the moment round 1 passed `transport=httpx.HTTPTransport(retries=...)`,
`allow_env_proxies` went `False` and `HTTP(S)_PROXY` stopped being read. Reproduced
mechanically: with `HTTPS_PROXY` set, a default client builds an `HTTPTransport` proxy
mount; the transport-injected client builds **zero mounts**. In a proxy-only deployment
every AskNews call would fail to connect, and — because of round 1's own finding-2 fix —
that surfaces as an ordinary `provider_failed` fallback, masking the regression as routine.
Your read is correct on both the mechanism and the masking.

**Fix — apply retries without ever passing a transport.** Build the SDK normally (env
proxies preserved), then set the retry count on the resulting connection pool(s):

```python
sdk = AskNewsSDK(api_key=api_key, scopes={"news"}, timeout=provider.timeout_seconds)
_apply_connection_retries(sdk.client._client, provider.retries)
```

```python
def _apply_connection_retries(http_client: httpx.Client, retries: int) -> None:
    for transport in (http_client._transport, *http_client._mounts.values()):
        pool = getattr(transport, "_pool", None)
        if pool is not None:
            pool._retries = retries
```

Why this is sound, not a hack around httpx: httpcore reads `_pool._retries` when it
*lazily creates* each connection (`ConnectionPool.create_connection` →
`HTTPConnection(retries=self._retries, ...)`), which happens on the first request — after
this runs. Setting it post-construction therefore takes full effect. Verified the default
transport pool and the env-proxy mount pool both carry the configured count.

**On the test you flagged.** You are right that `assert transport._pool._retries == N`
proves storage, not routing, and could not have caught this. I kept that assertion (retries
must still land somewhere) but added the test that guards the thing that actually broke:

```python
def test_retries_do_not_disable_env_proxy_routing(...):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:8080")
    client = build_asknews_client(custom)
    proxy_mounts = [t for t in client.client._client._mounts.values() if t is not None]
    assert proxy_mounts, "env proxy routing was dropped -- no proxy mount configured"
    for transport in proxy_mounts:
        assert transport._pool._retries == 7
```

This is red against the round-1 `transport=` implementation (empty mounts) and green after
the fix, and it proves both properties at once: routing preserved **and** retries reaching
that mount's pool. Construction stays I/O-free, so it runs under all three network guards —
`HTTPS_PROXY` only wires transports, it opens no socket.

**One scope note, stated rather than overclaimed.** httpcore's forward/tunnel *proxy
connection* classes don't take a per-connection retry count, so on a proxied hop retries
apply at the pool level we can reach but not deeper in the tunnel. The deliverable here is
the one that regressed: env-proxy routing is preserved, and retries are restored on the
direct path. Widening retry semantics into proxy tunnels is not something I'd add on a
theory.

---

## What changed

| File | Change |
|---|---|
| `src/whiskeyjack_bot/research/asknews.py` | drop `transport=`; add `_apply_connection_retries`; rewrite the retry docstring to cite the `allow_env_proxies` coupling |
| `tests/unit/test_asknews.py` | add `test_retries_do_not_disable_env_proxy_routing`; keep the pool-retries assertion |

## Unchanged, and why

Everything you accepted in round 2 is untouched: the partial-result contract, the revised
`error_summary`, stop-on-first-failure, and the finding-1 validated-response reasoning. No
other files changed.

---

# Round 3 — P2: retries on the proxy pool were dead storage

**Confirmed exactly as reported.** `_apply_connection_retries` walked the mounts too and set
`_pool._retries` on the proxy mount, but that value never reaches a connection:
`httpcore.HTTPProxy.create_connection` builds `ForwardHTTPConnection`/`TunnelHTTPConnection`
and threads **no** `retries` into either. So the proxy pool stored 7 while the tunneled
connection used 0 — and my round-2 test asserted that 7, i.e. asserted dead storage. That is
the exact plumb-through anti-pattern from round 1, finding 4, one layer down. Good catch.

**Fix — direct-path only, and say so.**

- `_apply_connection_retries` now sets `_pool._retries` on the **direct transport only**; the
  mount loop is gone. Its docstring records why the proxy pool is deliberately skipped.
- `build_asknews_client`'s docstring now scopes retries on two axes: *kind* (connection
  failures, not 5xx) and *path* (direct connections only; a no-op on the proxied hop, accepted
  M1-302 scope). Env-proxy **routing** — the thing that regressed in round 2 — stays intact.
- The routing test no longer asserts retries. It now asserts routing through httpx's own
  selection path, at the real endpoint:

  ```python
  selected = hc._transport_for_url(httpx.URL("https://api.asknews.app"))
  assert selected is not hc._transport, "AskNews traffic did not route through the proxy mount"
  ```

  `https://api.asknews.app` is the SDK's actual `base_url` (verified). This is red against the
  round-2 `transport=` implementation (selection falls back to the base transport) and green
  after the fix. Direct-path retries remain covered by `test_retries_reach_the_actual_transport`.

Gate: **356 passed**, ruff check/format clean, `mypy --strict` clean. Two files changed
(`asknews.py`, `test_asknews.py`).
