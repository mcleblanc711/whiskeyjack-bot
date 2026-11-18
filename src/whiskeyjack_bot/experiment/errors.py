"""The one error type the replay experiment harness raises (M5-806).

Same hygiene rule as ``ConfigError`` and ``LedgerError``: a message never echoes a packet, a
record, a model reply or a spec field value. Filesystem paths are rendered, under the settled
M1-401 carve-out, because a path is operator configuration and the only thing that makes a load
failure actionable.
"""

from __future__ import annotations


class ExperimentError(Exception):
    """A replay arm could not be loaded, checked, run or stored; the message names no value."""
