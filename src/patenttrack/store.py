"""Single responsibility: local sqlite3 persistence.

Two tables:

* ``records``  - one current row per stable key (idempotent upsert);
* ``evidence`` - append-only decision evidence keyed by
  ``(application_key, kind, subject_key)`` so re-runs update in place instead
  of duplicating audit trails.

Both operations are idempotent, which is what makes repeated messages and
recovery from a partial failure safe: re-feeding the same input converges to
the same row count and the same evidence set.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .config import DB_PATH

EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence(
    application_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY(application_key, kind, subject_key)
)
"""


def connect(path=None):
    con = sqlite3.connect(path or DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    con.execute(EVIDENCE_SCHEMA)
    return con


def upsert(con, record) -> None:
    con.execute(
        "INSERT INTO records(key,data) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
        (record.key, json.dumps(record.data, ensure_ascii=False, default=str)),
    )


def upsert_evidence(con, application_key: str, kind: str, subject_key: str,
                    data: dict[str, Any]) -> None:
    con.execute(
        "INSERT INTO evidence(application_key,kind,subject_key,data) VALUES(?,?,?,?) "
        "ON CONFLICT(application_key,kind,subject_key) DO UPDATE SET data=excluded.data",
        (application_key, kind, subject_key,
         json.dumps(data, ensure_ascii=False, default=str)),
    )


def store_evidence(con, evidence: dict[str, Any]) -> int:
    """Persist an evidence bundle produced by ``dedup.dedup_apply``."""
    written = 0
    for decision in evidence.get("decisions", []):
        application_key = decision.get("application_key", "")
        event_id = decision.get("event_id", "")
        upsert_evidence(
            con, application_key, "decision", event_id,
            {
                "outcome": decision.get("outcome"),
                "records": decision.get("records", []),
            },
        )
        written += 1
    for section, kind in (
        ("duplicates", "duplicate"),
        ("follow_ups", "follow_up"),
        ("amendments", "amendment"),
    ):
        for index, item in enumerate(evidence.get(section, [])):
            # Include the observed record so several duplicates of one event
            # are stored separately rather than collapsed into one row.
            observed = item.get("duplicate") or item.get("follow_up") \
                or item.get("amendment") or {}
            subject = "{}:{}:{}".format(
                item.get("event_id") or "",
                observed.get("record_id") or "",
                index,
            )
            upsert_evidence(con, item.get("application_key", ""), kind, subject, item)
            written += 1
    for index, item in enumerate(evidence.get("rejected_candidates", [])):
        application_key = (
            item.get("application_key")
            or item.get("left", {}).get("application_key")
            or item.get("name_key")
            or item.get("family_key")
            or ""
        )
        subject = (
            item.get("family_key")
            or item.get("name_key")
            or f"{index}:{item.get('kind', 'rejected')}"
        )
        upsert_evidence(con, application_key, "rejected_candidate", str(subject), item)
        written += 1
    for application_key, data in evidence.get("applications", {}).items():
        upsert_evidence(con, application_key, "application", application_key, data)
        written += 1
    for index, item in enumerate(evidence.get("cross_region_conflicts", [])):
        upsert_evidence(
            con, item.get("body") or f"conflict:{index}", "cross_region_conflict",
            f"{item.get('namespace', '')}:{item.get('body', '')}:{index}", item,
        )
        written += 1
    return written


def commit(con) -> None:
    con.commit()


def count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def evidence_count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records ORDER BY key")]


def all_evidence(con) -> list[dict[str, Any]]:
    return [
        {
            "application_key": application_key,
            "kind": kind,
            "subject_key": subject_key,
            "data": json.loads(data),
        }
        for application_key, kind, subject_key, data in con.execute(
            "SELECT application_key,kind,subject_key,data FROM evidence "
            "ORDER BY application_key,kind,subject_key"
        )
    ]
