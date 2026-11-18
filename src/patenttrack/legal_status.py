"""Single responsibility: version legal-status events (the legal_status stage).

Each record is an *event observation* for one application.  This stage gives
the observation a stable event identity and an ordering so that ``dedup`` can
decide whether it is a repeat, a later event, or an amendment of an earlier
one.

Event identity
--------------
``event_id`` is derived from the application identity plus the event's own
content: canonical kind, canonical status, date, revision and a digest of the
amendment text.  Two messages with the same content produce the same
``event_id`` even if they arrive out of order, from different sources, or with
different formatting.  Deliberately excluded: the source record id, ingestion
timestamp, source order and the bulletin/notice reference (sources number the
same notice differently), so a re-crawl of a notice does not look like a new
event.

Ordering
--------
``sort_key`` is ``(effective_date_or_ts, amendment_priority, event_id)``.
Dates are parsed loosely (ISO, ``YYYY/MM/DD``, ``YYYYMMDD``, Chinese dates).
Missing dates sort first (unknown history) but never displace a known event
with the same content.

Amendment semantics
-------------------
An amendment/re-publication (kind ``amendment`` / ``republication`` /
``correction``, or a changed status with a newer date) is a *new version* of
the application's legal history.  It is never a duplicate of the original
publication.  ``supersedes`` records the earlier event id once ordering is
known (resolved in ``finalize()``).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import idmap, normalizer

_AMENDMENT_KINDS = {
    "amendment", "amend", "correction", "corrected", "republication",
    "re-publish", "republish", "reissue", "certificate", "更正", "修改",
    "公告修订", "更正公告", "重新公布",
}
_EVENT_FIELDS = (
    "event_id", "event", "event_no", "event_type", "event_kind",
    "notice", "notice_no", "bulletin", "bulletin_no", "版本", "公告号",
)
_KIND_FIELDS = ("kind", "event_kind", "event_type", "type", "类型", "公告类型")
_STATUS_FIELDS = ("status", "legal_status", "state", "法律状态", "状态")
_DATE_FIELDS = (
    "effective_date", "event_date", "date", "ts", "timestamp",
    "publication_date", "notice_date", "公告日", "生效日", "日期",
)
_VERSION_FIELDS = ("amendment_no", "version", "revision", "seq", "sequence", "版本号")
# Placeholder that sorts *after* every real ISO date, so an undated record is
# never mistaken for the earliest event.  Real dates are ``YYYY-MM-DD``.
_UNKNOWN_DATE = "9999-12-31"
# Free-text fields that make one amendment different from another.  Only these
# affect amendment identity; unrelated source-specific columns do not.
_AMENDMENT_DETAIL_FIELDS = (
    "amendment_text", "correction_text", "amendment_detail", "change",
    "changes", "description", "summary", "备注", "变更事项", "修订内容",
    "更正内容",
)

_DATE_PATTERNS = (
    re.compile(r"^(?P<y>\d{4})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})"),
    re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})$"),
    re.compile(r"^(?P<y>\d{4})[-/.](?P<m>\d{1,2})$"),
    re.compile(r"^(?P<y>\d{4})$"),
)

# Canonical status vocabulary.  Different sources describe the same legal
# state in different languages/wording; mapping to one token lets a re-crawl
# from another source fold into the same event instead of appearing as a new
# one.  Unrecognised statuses keep their normalised text so they still compare
# if identically worded.
_STATUS_CANON = {
    "active": "active", "inforce": "active", "in_force": "active",
    "granted": "granted", "grant": "granted", "授权": "granted",
    "registered": "registered", "注册": "registered",
    "published": "published", "publication": "published", "公开": "published",
    "公布": "published", "公告": "published",
    "pending": "pending", "审查中": "pending", "申请中": "pending",
    "expired": "expired", "lapsed": "expired", "失效": "expired", "届满": "expired",
    "revoked": "revoked", "invalidated": "revoked", "撤销": "revoked",
    "abandoned": "abandoned", "withdrawn": "abandoned", "撤回": "abandoned",
    "rejected": "rejected", "驳回": "rejected",
    "amended": "amended", "corrected": "amended", "更正": "amended",
    "sold": "sold",
}
# Status -> event kind when no explicit kind field is present.
_STATUS_KIND = {
    "granted": "grant",
    "registered": "grant",
    "published": "publication",
    "amended": "amendment",
}


def canonical_status(value: Any) -> str:
    text = normalizer._text(value)
    if not text:
        return ""
    key = text.lower().replace("-", "_").replace(" ", "_")
    key = normalizer.name_key(key)
    if key in _STATUS_CANON:
        return _STATUS_CANON[key]
    compact = normalizer.name_key(text)
    for token, canonical in _STATUS_CANON.items():
        if normalizer.name_key(token) == compact:
            return canonical
    return compact


class _Registry:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        self.events = {}


_registry = _Registry()
_versions: dict[str, list[dict[str, Any]]] = {}
_finalized = False


def begin() -> None:
    global _finalized, _versions
    _registry.reset()
    _versions = {}
    _finalized = False


def reset() -> None:
    begin()


def _first(record: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = record.get(field)
        if value is None:
            continue
        if isinstance(value, dict):
            value = value.get("value") or value.get("code") or value.get("name")
        text = normalizer._text(value)
        if text:
            return text
    return ""


def parse_date(value: Any) -> str | None:
    """Best-effort date normalisation to ``YYYY-MM-DD``.

    Unparseable values return ``None`` rather than raising; partial dates are
    padded with ``01`` so they still order sensibly.
    """
    text = normalizer._text(value)
    if not text:
        return None
    compact = text.replace("T", " ").strip()
    for pattern in _DATE_PATTERNS:
        match = pattern.match(compact)
        if not match:
            continue
        groups = match.groupdict()
        year = int(groups["y"])
        month = int(groups.get("m") or 1)
        day = int(groups.get("d") or 1)
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _kind(record: dict[str, Any]) -> str:
    kind = _first(record, _KIND_FIELDS).lower()
    for token in _AMENDMENT_KINDS:
        if token in kind or token in normalizer.name_key(kind):
            return "amendment"
    if kind in ("grant", "granted", "授权"):
        return "grant"
    if kind in ("publication", "published", "publish", "公开", "公布"):
        return "publication"
    if kind:
        return normalizer.normalize_text(kind).lower() or "event"
    # No explicit kind: infer from the (canonical) status.
    return _STATUS_KIND.get(canonical_status(_first(record, _STATUS_FIELDS)), "event")


def _is_amendment(record: dict[str, Any], kind: str) -> bool:
    if kind == "amendment":
        return True
    for field in ("amendment", "is_amendment", "correction", "revision", "修订", "更正"):
        value = record.get(field)
        if value in (True, 1, "1", "true", "True", "yes", "是"):
            return True
    return _first(record, _VERSION_FIELDS) != ""


def process(record: dict[str, Any]) -> dict[str, Any]:
    """Annotate one record with legal-event version evidence."""
    rec = dict(record)
    application_key = idmap.final_key(rec)

    kind = _kind(rec)
    status = _first(rec, _STATUS_FIELDS)
    status_key = canonical_status(status)
    date_text = _first(rec, _DATE_FIELDS)
    date = parse_date(date_text)
    event_ref = _first(rec, _EVENT_FIELDS)
    version_text = _first(rec, _VERSION_FIELDS)
    amendment = _is_amendment(rec, kind)

    revision = 1
    match = re.search(r"\d+", version_text)
    if match:
        revision = int(match.group(0))
    elif amendment:
        revision = 2

    # Content digest for amendments.  Only amendment-specific free text is
    # included, so a re-crawl of the same correction from another source maps
    # to the same event while two genuinely different corrections do not.
    detail_fields = [field for field in _AMENDMENT_DETAIL_FIELDS if field in rec]
    detail_fingerprint = normalizer.record_fingerprint(rec, detail_fields)

    # ``event_ref`` (bulletin/notice number) is deliberately excluded from the
    # identity digest: different sources number the same notice differently,
    # and the re-crawl of one notice must not look like a new event.  The value
    # is retained on the event as evidence.  Amendments remain distinct through
    # kind/status/date/revision and the amendment-text fingerprint.
    material = "|".join(
        [application_key, kind, status_key, date or "",
         str(revision), detail_fingerprint]
    )
    event_id = hashlib.sha1(material.encode("utf-8")).hexdigest()[:20]
    rec["legal_event"] = {
        "event_id": event_id,
        "application_key": application_key,
        "kind": kind,
        "is_amendment": amendment,
        "status": status,
        "status_key": status_key,
        "date": date,
        "date_raw": date_text,
        "event_ref": event_ref,
        "revision": revision,
        "detail_fingerprint": detail_fingerprint,
        "sort_key": [date or _UNKNOWN_DATE, revision, event_id],
        "supersedes": None,
        "version_of": None,
    }
    rec["legal_status"] = status
    _registry.events.setdefault(event_id, rec["legal_event"])
    rec["_stage_legal_status"] = "ok"
    return rec


def finalize() -> dict[str, Any]:
    """Order events per application and resolve ``supersedes`` pointers."""
    global _finalized, _versions
    if _finalized:
        return _summary()

    by_application: dict[str, list[dict[str, Any]]] = {}
    for event in _registry.events.values():
        by_application.setdefault(event["application_key"], []).append(event)

    versions: dict[str, list[dict[str, Any]]] = {}
    for application_key, events in sorted(by_application.items()):
        events.sort(key=lambda item: (item["sort_key"][0], item["sort_key"][1], item["event_id"]))
        previous: dict[str, Any] | None = None
        for event in events:
            if previous is not None:
                event["supersedes"] = previous["event_id"]
                if event["is_amendment"] or event["kind"] in ("grant", "amendment"):
                    event["version_of"] = events[0]["event_id"]
            previous = event
        versions[application_key] = events
    _versions = versions
    _finalized = True
    return _summary()


def _summary() -> dict[str, Any]:
    amendments = sum(
        1 for events in _versions.values() for event in events if event["is_amendment"]
    )
    return {
        "events": len(_registry.events),
        "applications": len(_versions),
        "amendments": amendments,
    }


def events_for(application_key: str) -> list[dict[str, Any]]:
    return [dict(event) for event in _versions.get(application_key, [])]


def versions() -> dict[str, list[dict[str, Any]]]:
    return {key: [dict(event) for event in value] for key, value in _versions.items()}


def summary() -> dict[str, Any]:
    return _summary()
