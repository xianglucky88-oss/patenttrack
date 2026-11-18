"""Single responsibility: derive a stable primary key for a stored record.

Key priority
------------
1. Explicit, identifier-based identity produced by ``dedup``:
   ``<application_key>#<event_id>``.  This is stable across sources, arrival
   order and partial/full runs, so re-ingesting a message never creates a new
   row, and two sources reporting the same event converge to one row.
2. ``id`` for identifier-less inputs, preserving the original pipeline's keys
   and therefore its idempotency semantics.
3. A content digest as the last resort.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Identity prefixes that are not real, cross-source identifiers.
_FALLBACK_PREFIXES = ("REC:", "FP:")


def key_of(record: dict[str, Any]) -> str:
    dedup = record.get("dedup") or {}
    application_key = dedup.get("application_key")
    event_id = dedup.get("event_id")
    if application_key and event_id and not str(application_key).startswith(_FALLBACK_PREFIXES):
        return f"{application_key}#{event_id}"

    rid = record.get("id")
    if rid not in (None, ""):
        return str(rid)
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]
