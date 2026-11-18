"""Thin orchestration: loader -> normalizer -> query_page -> idmap -> family
-> legal_status -> dedup -> keyer -> store.  No business judgements here.

Why staged (batch) rather than strictly per-record
--------------------------------------------------
Cross-source duplicate detection is inherently a batch decision: whether a
record is a duplicate, a follow-up event or an independent application depends
on the other records in the same run.  The orchestrator therefore applies each
stage to the whole batch in dependency order and only then makes decisions.
It makes no judgements of its own; ordering and error handling are all it
contributes.

Failure recovery
----------------
Records that fail a stage are counted and skipped (the failure is reported);
the remaining records are still persisted and committed.  Because
``store.upsert`` is idempotent and keys are identifier-based, a later full
re-run converges to the same rows.  ``commit_every`` can be set to commit
incrementally for long batches.
"""

from __future__ import annotations

import importlib
from typing import Any, Iterable

from . import dedup as _dedup
from . import family as _family
from . import idmap as _idmap
from . import legal_status as _legal_status
from . import loader as _loader
from . import store as _store
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ["normalizer", "query_page", "idmap", "dedup"]


def _module(name: str):
    return importlib.import_module("." + name, __package__)


def _reset_business_state() -> None:
    """Clear per-run registries so repeated runs cannot leak decisions."""
    for module in (_idmap, _family, _legal_status):
        reset = getattr(module, "reset", None)
        if reset is not None:
            reset()


def _apply(module, records: list[dict[str, Any]], failures: list[dict[str, Any]],
           stage: str) -> list[dict[str, Any]]:
    processed: list[dict[str, Any]] = []
    for record in records:
        try:
            processed.append(module.process(record))
        except Exception as exc:  # record-level isolation for partial failures
            failures.append({"stage": stage, "record_id": record.get("id"),
                             "error": f"{type(exc).__name__}: {exc}"})
    return processed


def run(con=None, records: Iterable[dict[str, Any]] | None = None,
        commit_every: int | None = None) -> dict[str, Any]:
    """Run the pipeline.

    ``records`` overrides the loader (used by import tooling and tests).
    ``commit_every`` commits after that many persisted rows, which bounds the
    work lost if the process dies mid-batch.
    """
    own = con is None
    con = con or _store.connect()
    try:
        _reset_business_state()
        source = list(records) if records is not None else list(_loader.load())
        fed = len(source)

        failures: list[dict[str, Any]] = []

        # Generic early stages (field shaping, pagination bookkeeping).
        staged = _apply(_module("normalizer"), source, failures, "normalizer")
        staged = _apply(_module("query_page"), staged, failures, "query_page")

        # Identity first: family and legal events need final application keys.
        staged = _apply(_idmap, staged, failures, "idmap")
        _idmap.finalize()

        staged = _apply(_family, staged, failures, "family")
        _family.finalize()

        staged = _apply(_legal_status, staged, failures, "legal_status")
        _legal_status.finalize()

        persisted_records, evidence = _dedup.dedup_apply(staged)

        rows = 0
        for index, data in enumerate(persisted_records):
            try:
                _store.upsert(con, Record(key=key_of(data), data=data))
                rows += 1
            except Exception as exc:
                failures.append({"stage": "store", "record_id": data.get("id"),
                                 "error": f"{type(exc).__name__}: {exc}"})
            if commit_every and rows % commit_every == 0:
                _store.commit(con)

        evidence_written = _store.store_evidence(con, evidence)
        _store.commit(con)

        return {
            "fed": fed,
            "rows": _store.count(con),
            "persisted": rows,
            "duplicates": evidence["summary"]["duplicates"],
            "follow_ups": evidence["summary"]["follow_ups"],
            "amendments": evidence["summary"]["amendments"],
            "applications": evidence["summary"]["applications"],
            "rejected_candidates": evidence["summary"]["rejected_candidates"],
            "evidence": evidence_written,
            "failures": failures,
        }
    finally:
        if own:
            con.close()
