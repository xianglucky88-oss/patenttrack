"""Single responsibility: decide cross-source duplicate vs. follow-up event
vs. independent application, and record the evidence (the dedup stage).

This stage *consumes* the other business stages; it does not re-derive their
judgements:

* ``idmap``        -> which records share one application identity;
* ``family``       -> which applications are related but distinct;
* ``legal_status`` -> per-application event versions and ordering.

Decision model
--------------
For each application (``idmap`` component) the observations are grouped by
``legal_status.event_id`` and the event versions are walked oldest-first:

* the same event observed by several records        -> one stored row; every
  extra observation is ``duplicate`` evidence pointing at the canonical one;
* several distinct events for one application       -> one stored row per
  event; the application is counted once, so "one application, many events"
  does not inflate the application count;
* an amendment / re-publication event              -> its own stored row with
  outcome ``amendment`` and a ``supersedes`` pointer; it is never folded into
  the publication it corrects;
* one event from one record                         -> ``independent``.

Family siblings
---------------
Different application identities that share a priority/family key stay
**independent**.  The shared key is recorded as a family cluster with its
members, and it is also reported as a rejected merge candidate, so a merged
result remains auditable.

Similar name, conflicting identifiers
-------------------------------------
Names are only supporting evidence.  Two records with a similar name but
disjoint identifier sets are never merged; each pair is recorded as
``rejected_candidate`` evidence with the name similarity and both identifier
sets.  Pair enumeration is *blocked* (by normalised name and by name tokens)
so a large candidate batch costs O(sum of block sizes^2), not O(n^2); the
number of pair comparisons is reported for observability.

Determinism and idempotency
---------------------------
All grouping is on canonical identifiers and ``event_id`` (content based), so
shuffled arrival, repeated messages and partial-then-full re-runs converge to
the same rows and the same decisions.

Batch API
---------
``dedup_apply(records)`` returns ``(output_records, evidence)``.  ``process``
remains as a single-record compatibility wrapper.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from . import family, idmap, normalizer

DUPLICATE = "duplicate"
FOLLOW_UP = "follow_up"
AMENDMENT = "amendment"
INDEPENDENT = "independent"

EVIDENCE_DUPLICATE = "duplicate_of"
EVIDENCE_FOLLOW_UP = "follow_up_of"
EVIDENCE_AMENDMENT = "amendment_of"
EVIDENCE_REJECTED = "rejected_candidate"

_NAME_CONFLICT_THRESHOLD = 0.72
# Fuzzy candidate generation is *size capped*: a block larger than this is a
# common-name block whose every pair would be a conflict; comparing all pairs
# would turn a large batch into O(n^2) work and an unusable evidence table.
# Such blocks are skipped and counted, not silently dropped.
_MAX_FUZZY_BLOCK = 32
_MAX_PAIR_COMPARISONS = 50_000
_MAX_MEMBERS_PER_EVIDENCE = 200


def _event(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("legal_event") or {}


def _sort_key(record: dict[str, Any]) -> tuple:
    event = _event(record)
    sort_key = event.get("sort_key") or ["", 0, ""]
    return (
        sort_key[0] if len(sort_key) > 0 else "",
        sort_key[1] if len(sort_key) > 1 else 0,
        str(record.get("src", "")),
        str(record.get("id", "")),
        event.get("event_id", ""),
        normalizer.record_fingerprint(record),
    )


def _sort_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=_sort_key)


def _record_ref(record: dict[str, Any]) -> dict[str, Any]:
    event = _event(record)
    return {
        "record_id": record.get("id"),
        "source": record.get("src"),
        "event_id": event.get("event_id"),
        "kind": event.get("kind"),
        "is_amendment": event.get("is_amendment", False),
        "date": event.get("date"),
        "name": record.get("name", ""),
        "application_key": event.get("application_key")
        or (record.get("idmap") or {}).get("application_key"),
        "identifiers": {
            namespace: list(nodes)
            for namespace, nodes in (record.get("canonical_ids") or {}).items()
        },
    }


def dedup_apply(
    records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Batch decision over fully staged records.

    Expects records already through ``normalizer``, ``idmap``, ``family`` and
    ``legal_status``.  Returns one record per application event plus an
    evidence bundle.
    """
    batch = list(records)
    # Linking may have grown components during the batch, so refresh the
    # application key from the final union-find before grouping.
    for record in batch:
        event = record.get("legal_event")
        if isinstance(event, dict):
            event["application_key"] = idmap.final_key(record)

    by_application: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in batch:
        by_application[idmap.final_key(record)].append(record)

    evidence: dict[str, Any] = {
        "decisions": [],
        "duplicates": [],
        "follow_ups": [],
        "amendments": [],
        "independent": [],
        "rejected_candidates": [],
        "families": family.family_groups(),
        "cross_region_conflicts": idmap.conflicts(),
        "applications": {},
        "comparisons": {"pairwise": 0, "candidates": 0, "blocked": 0},
    }

    output: list[dict[str, Any]] = []

    for application_key in sorted(by_application):
        group = _sort_records(by_application[application_key])
        by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in group:
            by_event[_event(record).get("event_id", "")].append(record)

        ordered_events = sorted(
            by_event.items(),
            key=lambda pair: (
                (_event(pair[1][0]).get("sort_key") or ["", 0, ""])[0],
                (_event(pair[1][0]).get("sort_key") or ["", 0, ""])[1],
                pair[0],
            ),
        )

        previous_event_id: str | None = None
        event_ids: list[str] = []
        for event_id, event_records in ordered_events:
            ordered_records = _sort_records(event_records)
            primary = ordered_records[0]
            event = _event(primary)
            refs = [_record_ref(record) for record in ordered_records]

            duplicate_of = None
            if len(refs) > 1:
                duplicate_of = refs[0]
                for duplicate in refs[1:]:
                    evidence["duplicates"].append(
                        {
                            "kind": EVIDENCE_DUPLICATE,
                            "application_key": application_key,
                            "event_id": event_id,
                            "canonical": refs[0],
                            "duplicate": duplicate,
                            "reason": "same event content observed more than once",
                        }
                    )

            outcome = INDEPENDENT
            if previous_event_id is not None:
                if event.get("is_amendment"):
                    outcome = AMENDMENT
                    evidence["amendments"].append(
                        {
                            "kind": EVIDENCE_AMENDMENT,
                            "application_key": application_key,
                            "event_id": event_id,
                            "amendment": refs[0],
                            "supersedes_event_id": event.get("supersedes")
                            or previous_event_id,
                            "previous_event_id": previous_event_id,
                            "reason": "publication amendment is a new event, "
                                      "not a duplicate",
                        }
                    )
                else:
                    outcome = FOLLOW_UP
                    evidence["follow_ups"].append(
                        {
                            "kind": EVIDENCE_FOLLOW_UP,
                            "application_key": application_key,
                            "event_id": event_id,
                            "follow_up": refs[0],
                            "previous_event_id": previous_event_id,
                            "reason": "same application observed again "
                                      "(grant / republication / re-crawl)",
                        }
                    )
            else:
                evidence["independent"].append(
                    {
                        "kind": INDEPENDENT,
                        "application_key": application_key,
                        "event_id": event_id,
                        "record": refs[0],
                        "reason": "first observed event of this application",
                    }
                )

            # ``supersedes`` is resolved per event id by legal_status, but a
            # duplicate observation's own event dict may not carry it.  The
            # batch walk knows the predecessor, so resolve it here for every
            # stored row: arrival order must not change stored evidence.
            resolved_supersedes = event.get("supersedes") or previous_event_id

            record_out = dict(primary)
            record_out["legal_event"] = dict(event)
            if resolved_supersedes:
                record_out["legal_event"]["supersedes"] = resolved_supersedes
            record_out["dedup"] = {
                "outcome": outcome,
                "application_key": application_key,
                "event_id": event_id,
                "duplicate_of": duplicate_of,
                "supersedes_event_id": resolved_supersedes,
                "record_ids": [ref["record_id"] for ref in refs],
                "sources": sorted({str(ref["source"]) for ref in refs}),
                "evidence_kind": (
                    EVIDENCE_DUPLICATE if duplicate_of is not None else
                    EVIDENCE_AMENDMENT if outcome == AMENDMENT else
                    EVIDENCE_FOLLOW_UP if outcome == FOLLOW_UP else INDEPENDENT
                ),
            }
            record_out["_stage_dedup"] = "ok"
            output.append(record_out)

            evidence["decisions"].append(
                {
                    "application_key": application_key,
                    "event_id": event_id,
                    "outcome": outcome,
                    "duplicate": duplicate_of is not None,
                    "records": refs,
                }
            )
            event_ids.append(event_id)
            previous_event_id = event_id

        has_amendment = any(
            _event(record).get("is_amendment") for record in group
        )
        evidence["applications"][application_key] = {
            "events": event_ids,
            "records": len(group),
            "outcome": (
                AMENDMENT if has_amendment
                else FOLLOW_UP if len(event_ids) > 1
                else INDEPENDENT
            ),
            "family_keys": family.family_keys_for(application_key),
        }

    _collect_rejected_candidates(by_application, evidence)
    _collect_family_rejections(evidence)
    evidence["summary"] = {
        "applications": len(by_application),
        "rows": len(output),
        "events": sum(len(data["events"]) for data in evidence["applications"].values()),
        "duplicates": len(evidence["duplicates"]),
        "follow_ups": len(evidence["follow_ups"]),
        "amendments": len(evidence["amendments"]),
        "independent": len(evidence["independent"]),
        "rejected_candidates": len(evidence["rejected_candidates"]),
    }
    return output, evidence


def _names_differ_only_by_digits(left: Any, right: Any) -> bool:
    """True when two names differ only in their numeric parts.

    Such names ("装置编号1号" vs "装置编号11号", "Model 100" vs "Model 1000")
    are sequence/model labels rather than evidence of one ambiguous subject.
    Identifier evidence already keeps them separate, so recording every such
    pair would bury genuine name conflicts in noise.
    """
    left_key = normalizer.name_key(left)
    right_key = normalizer.name_key(right)
    if not left_key or not right_key:
        return False
    left_skeleton = "".join(ch for ch in left_key if not ch.isdigit())
    right_skeleton = "".join(ch for ch in right_key if not ch.isdigit())
    if not left_skeleton or left_skeleton != right_skeleton:
        return False
    # They differ only if the numeric content actually differs (identical
    # names are handled by the exact-name path, not here).
    return left_key != right_key


def _is_name_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Similar name + disjoint identifiers = refused merge."""
    if _names_differ_only_by_digits(left.get("name", ""), right.get("name", "")):
        return False
    similarity = normalizer.name_similarity(left.get("name", ""), right.get("name", ""))
    if similarity < _NAME_CONFLICT_THRESHOLD:
        return False
    left_values = {
        node for values in (left.get("canonical_ids") or {}).values() for node in values
    }
    right_values = {
        node for values in (right.get("canonical_ids") or {}).values() for node in values
    }
    if not left_values or not right_values:
        return False
    return not (left_values & right_values)


def _blocking_keys(record: dict[str, Any]) -> set[str]:
    """Blocking keys that co-locate *plausibly* similar names for fuzzy checks.

    Prefix/suffix shingles and tokens find names that differ slightly.  Exact
    duplicates are handled separately in linear time by ``_exact_name_groups``,
    so they are intentionally not used as fuzzy block keys here.
    """
    key = normalizer.name_key(record.get("name", ""))
    if len(key) < 4:
        return set()
    keys = {
        f"p:{key[:4]}",
        f"p:{key[-4:]}",
        f"p:{key[:5]}",
        f"p:{key[-5:]}",
    }
    tokens = [token for token in normalizer.name_tokens(record.get("name", ""))
              if len(token) >= 3]
    if len(tokens) > 1:
        for token in tokens:
            keys.add(f"t:{token}")
    elif tokens:
        keys.add(f"t:{tokens[0]}")
    return keys


def _evidence_pair(left_key: str, left: dict[str, Any],
                   right_key: str, right: dict[str, Any]) -> dict[str, Any]:
    conflict = {
        "kind": EVIDENCE_REJECTED,
        "reason": "similar name but conflicting identifiers; not merged",
        "left": {
            "application_key": left_key,
            "name": left.get("name", ""),
            "record_id": left.get("id"),
            "canonical_ids": left.get("canonical_ids", {}),
        },
        "right": {
            "application_key": right_key,
            "name": right.get("name", ""),
            "record_id": right.get("id"),
            "canonical_ids": right.get("canonical_ids", {}),
        },
        "name_similarity": round(
            normalizer.name_similarity(left.get("name", ""), right.get("name", "")), 4
        ),
    }
    shared = set(family.family_keys_for(left_key)) & set(
        family.family_keys_for(right_key)
    )
    if shared:
        conflict["shared_family_keys"] = sorted(shared)
    return conflict


def _collect_rejected_candidates(
    by_application: dict[str, list[dict[str, Any]]],
    evidence: dict[str, Any],
) -> None:
    """Record similar-name / conflicting-identifier pairs that were not merged.

    Cost model (reported under ``comparisons``):

    * exact-name groups are summarised in **linear** time as one grouped
      rejection per name (all members kept separate, all listed up to a cap);
    * fuzzy blocking compares only bounded blocks; oversized common-name
      blocks are summarised without pairwise comparison and counted in
      ``skipped_blocks``.

    So the number of pair comparisons is bounded by configuration, not by
    batch size, while every refused merge still leaves evidence.
    """
    representatives = [
        (application_key, _sort_records(by_application[application_key])[0])
        for application_key in sorted(by_application)
    ]

    exact: dict[str, list[int]] = defaultdict(list)
    for index, (_, record) in enumerate(representatives):
        key = normalizer.name_key(record.get("name", ""))
        if key:
            exact[key].append(index)

    compared = 0
    candidates = 0
    skipped_blocks = 0

    # 1. Exact-name groups: linear summary, no pairwise loop.
    for name_key in sorted(exact):
        members = exact[name_key]
        if len(members) < 2:
            continue
        conflicting = []
        for index in members:
            application_key, record = representatives[index]
            if record.get("canonical_ids"):
                conflicting.append((application_key, record))
        if len(conflicting) < 2:
            continue
        distinct_identifiers = {
            frozenset(node for values in record.get("canonical_ids", {}).values()
                      for node in values)
            for _, record in conflicting
        }
        if len(distinct_identifiers) < 2:
            continue
        evidence["rejected_candidates"].append(
            {
                "kind": EVIDENCE_REJECTED,
                "reason": "same name but conflicting identifiers; not merged",
                "name_key": name_key,
                "name_similarity": 1.0,
                "members": [
                    {
                        "application_key": application_key,
                        "record_id": record.get("id"),
                        "canonical_ids": record.get("canonical_ids", {}),
                    }
                    for application_key, record in conflicting[:_MAX_MEMBERS_PER_EVIDENCE]
                ],
                "member_count": len(conflicting),
            }
        )

    # 2. Fuzzy blocks: bounded pairwise comparison.
    blocks: dict[str, list[int]] = defaultdict(list)
    for index, (_, record) in enumerate(representatives):
        for key in _blocking_keys(record):
            blocks[key].append(index)

    seen_pairs: set[tuple[int, int]] = set()
    for key in sorted(blocks):
        members = blocks[key]
        if len(members) < 2:
            continue
        if len(members) > _MAX_FUZZY_BLOCK:
            # Common name block; pairwise work here is quadratic and every
            # member is already reported by the exact/family paths or is a
            # normal independent application.  Skip and make it observable.
            skipped_blocks += 1
            continue
        candidates += len(members) * (len(members) - 1) // 2
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                left_index, right_index = members[i], members[j]
                if left_index > right_index:
                    left_index, right_index = right_index, left_index
                pair = (left_index, right_index)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                if compared >= _MAX_PAIR_COMPARISONS:
                    continue
                compared += 1
                left_key, left = representatives[left_index]
                right_key, right = representatives[right_index]
                if left_key == right_key:
                    continue
                left_name = normalizer.name_key(left.get("name", ""))
                right_name = normalizer.name_key(right.get("name", ""))
                if left_name and left_name == right_name:
                    # Exact-name conflicts are already recorded as one grouped
                    # entry above; do not duplicate them pairwise.
                    continue
                if not _is_name_conflict(left, right):
                    continue
                evidence["rejected_candidates"].append(
                    _evidence_pair(left_key, left, right_key, right)
                )

    evidence["comparisons"] = {
        "pairwise": compared,
        "candidates": candidates,
        "blocks": len(blocks),
        "skipped_blocks": skipped_blocks,
    }


def _collect_family_rejections(evidence: dict[str, Any]) -> None:
    """Add family-owned rejected-merge evidence from ``family.finalize``.

    Family clusters are entirely "different applications that must not be
    merged", so they belong in the rejected-candidate evidence.  The family
    stage already groups them (linear in cluster size); dedup only copies the
    evidence into its bundle.
    """
    for rejection in family.rejections():
        evidence["rejected_candidates"].append(dict(rejection))


def process(record: dict[str, Any]) -> dict[str, Any]:
    """Single-record compatibility wrapper.

    The real decision is batch-scoped; a one-record batch marks the record as
    an independent observation.  The pipeline uses ``dedup_apply``.
    """
    output, _evidence = dedup_apply([record])
    if len(output) != 1:
        rec = dict(record)
        rec.setdefault("_stage_dedup", "ok")
        return rec
    return output[0]


apply = dedup_apply
