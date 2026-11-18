"""Single responsibility: derive *family* relations (the family stage).

Family is evidence, not identity.  A family groups different applications that
share a priority claim or an explicit family identifier.  ``dedup`` may use the
grouping to explain why two applications are kept apart, but it must never
merge two different application numbers just because they are in one family.

Inputs used
-----------
* explicit family fields (``family_id``, ``family``, ``patent_family_id``,
  ``同族号``, ``family_members``);
* priority identifiers normalised by ``normalizer``.

A record may belong to several family keys (e.g. two priorities); membership is
recorded individually and the cluster is computed at ``finalize()``.  Family
keys are canonical ``region:body`` strings so formatting differences do not
split a family, while region still distinguishes national phase entries.

The module is order-independent: clusters are keyed by sorted family keys and
the application keys are sorted before being exposed.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import idmap, normalizer

_FAMILY_FIELDS = (
    "family_id", "family", "patent_family", "patent_family_id",
    "family_number", "同族号", "同族编号", "family_members", "family_ids",
)
_MAX_MEMBERS = 200


class _Registry:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.family_records: dict[str, set[str]] = {}

    def reset(self) -> None:
        self.records = []
        self.family_records = {}


_registry = _Registry()
_groups: dict[str, list[str]] = {}
_by_application: dict[str, list[str]] = {}
_rejections: list[dict[str, Any]] = []
_finalized = False


def begin() -> None:
    global _finalized, _groups, _rejections, _by_application
    _registry.reset()
    _groups = {}
    _by_application = {}
    _rejections = []
    _finalized = False


def reset() -> None:
    begin()


def _family_values(record: dict[str, Any]) -> list[str]:
    """Explicit family identifiers, canonicalised like document numbers."""
    values: list[str] = []
    for field in _FAMILY_FIELDS:
        raw = record.get(field)
        if raw is None:
            continue
        items: Iterable[Any]
        if isinstance(raw, (list, tuple, set)):
            items = raw
        else:
            items = [raw]
        for item in items:
            if isinstance(item, dict):
                item = item.get("value") or item.get("id") or item.get("no")
            text = normalizer.normalize_text(item)
            if not text:
                continue
            region = normalizer.parse_region(record)
            if text[:2] in normalizer.KNOWN_REGIONS:
                key = f"{text[:2]}:{text[2:]}"
            elif region:
                key = f"{region}:{text}"
            else:
                key = f"??:{text}"
            if key not in values:
                values.append(key)
    return values


def _priority_values(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    ids = record.get("ids")
    if isinstance(ids, dict):
        for item in ids.get("priority", []):
            key = f"{item['region']}:{item['body']}"
            if key not in values:
                values.append(key)
    else:
        for item in normalizer.extract_identifiers(record).get("priority", []):
            key = f"{item['region']}:{item['body']}"
            if key not in values:
                values.append(key)
    return values


def _own_application_values(record: dict[str, Any]) -> list[str]:
    """A record's own application number can be another record's priority.

    Adding it as a family anchor is what links a child application to its
    parent without merging them: the shared *key* is the parent's application
    number, while the application identities remain distinct.
    """
    values: list[str] = []
    ids = record.get("ids")
    if isinstance(ids, dict):
        for item in ids.get("application", []):
            key = f"{item['region']}:{item['body']}"
            if key not in values:
                values.append(key)
    return values


def process(record: dict[str, Any]) -> dict[str, Any]:
    """Annotate one record with its family evidence."""
    rec = dict(record)
    explicit = _family_values(rec)
    priorities = _priority_values(rec)
    own_application = _own_application_values(rec)
    family_keys = sorted(set(explicit) | set(priorities))

    basis: list[str] = []
    if explicit:
        basis.append("explicit_family_id")
    if priorities:
        basis.append("shared_priority")

    rec["family"] = {
        "family_keys": family_keys,
        "explicit_family_ids": explicit,
        "priority_ids": priorities,
        "application_ids": own_application,
        "basis": basis,
        "same_family_as": [],
        "distinct_applications": [],
    }
    _registry.records.append(rec)
    for key in family_keys:
        _registry.family_records.setdefault(key, set()).add(idmap.final_key(rec))
    # Register the record's own application number as an anchor so a later
    # record claiming it as priority joins the same family key.
    for key in own_application:
        _registry.family_records.setdefault(key, set()).add(idmap.final_key(rec))
    rec["_stage_family"] = "ok"
    return rec


def finalize() -> dict[str, Any]:
    """Compute family clusters across all ingested records."""
    global _finalized, _groups, _rejections, _by_application
    if _finalized:
        return {"families": len(_groups), "members": sum(len(v) for v in _groups.values())}

    groups: dict[str, list[str]] = {}
    for key, members in _registry.family_records.items():
        if len(members) >= 2:
            groups[key] = sorted(members)
    _groups = dict(sorted(groups.items()))

    _by_application = {}
    for key, members in _groups.items():
        for member in members:
            _by_application.setdefault(member, []).append(key)
    # Singleton families are still evidence of a priority/family claim, so an
    # application with a family key but no sibling is also indexed.
    for key, members in _registry.family_records.items():
        for member in members:
            bucket = _by_application.setdefault(member, [])
            if key not in bucket:
                bucket.append(key)
    for bucket in _by_application.values():
        bucket.sort()

    # A family cluster is inherently a rejected merge candidate: its members
    # are different applications that share priority/family evidence and must
    # stay separate.  One grouped entry per cluster keeps this evidence linear
    # in family size while listing every member (capped for very large
    # clusters; ``member_count`` is always exact).
    rejections: list[dict[str, Any]] = []
    for key, members in _groups.items():
        rejections.append(
            {
                "kind": "same_family_distinct_application",
                "family_key": key,
                "members": members[:_MAX_MEMBERS],
                "member_count": len(members),
                "reason": "shared family/priority must not merge distinct "
                          "applications; members retained independently",
            }
        )
    _rejections = rejections
    _finalized = True
    return {"families": len(_groups), "members": sum(len(v) for v in _groups.values())}


def family_groups() -> dict[str, list[str]]:
    return {key: list(value) for key, value in _groups.items()}


def family_keys_for(application_key: str) -> list[str]:
    if _finalized:
        return list(_by_application.get(application_key, []))
    keys = [key for key, members in _registry.family_records.items()
            if application_key in members]
    return sorted(keys)


def families_of(application_key: str) -> list[str]:
    return family_keys_for(application_key)


def rejections() -> list[dict[str, Any]]:
    return list(_rejections)


def summary() -> dict[str, Any]:
    return {
        "families": len(_groups),
        "members": sum(len(value) for value in _groups.values()),
    }
