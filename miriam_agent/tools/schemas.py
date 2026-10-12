"""Shared JSON-schema fragments for tool arg_schemas.

Split out of :mod:`miriam_agent.tools.definitions` for the blob ratchet; the
constants are unchanged and ``definitions`` re-imports them, so the public
surface is preserved.
"""

from __future__ import annotations

_SCHEMA_STRING = {"type": "string"}
_SCHEMA_NUMBER = {"type": "number"}
_SCHEMA_INT = {"type": "integer", "minimum": 0}
_SCHEMA_BOOL = {"type": "boolean"}
# Money amounts must be strictly positive. Zero and negative amounts used to
# pass schema validation untouched and were only ever caught (if at all) by
# the Go ledger, so they are rejected here at the tool boundary.
_SCHEMA_MONEY = {"type": "number", "exclusiveMinimum": 0}
# Opaque backend identifiers. Constrained so an id can never be interpolated
# into a Go URL path as a traversal (``../../admin``) or smuggle a query
# separator; every id is embedded in a path by integrations/go_client.py.
_SCHEMA_ID = {
    "type": "string",
    "pattern": "^(?=.*[A-Za-z0-9_:-])[A-Za-z0-9_.:-]{1,64}$",
}
