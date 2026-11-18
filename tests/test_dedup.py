"""Cross-source identity / dedup behaviour.

These tests use only plain ``assert`` so they run both under pytest and under a
trivial collector.  Each test uses its own temporary SQLite file, so tests do
not depend on execution order or on the default database.
"""

import os
import tempfile
import time

from patenttrack import dedup, family, idmap, legal_status, normalizer, pipeline, store


def _staged(records):
    """Run the per-record business stages and finalise the run registries."""
    idmap.reset()
    family.reset()
    legal_status.reset()
    staged = []
    for record in records:
        staged_record = record
        for stage in (normalizer.process, idmap.process,
                      family.process, legal_status.process):
            staged_record = stage(staged_record)
        staged.append(staged_record)
    idmap.finalize()
    family.finalize()
    legal_status.finalize()
    return staged


def _run(records):
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    con = store.connect(path)
    try:
        result = pipeline.run(con=con, records=records)
        evidence = store.all_evidence(con)
        return result, evidence
    finally:
        con.close()
        os.remove(path)


def _app(record_id, source, name, app_no, ts, **extra):
    record = {"id": record_id, "src": source, "name": name,
              "app_no": app_no, "ts": ts}
    record.update(extra)
    return record


# One application: publication (CN), grant (EP source), amendment (US source).
# A family sibling with a shared priority, and a same-number cross-region pair.
MIXED = [
    {"id": "a1", "src": "cnipa", "name": "智能 手机 支架",
     "app_no": "CN 2024-100001.5", "pub_no": "CN1234567A",
     "status": "公开", "ts": "2024-01-10"},
    {"id": "a2", "src": "epo", "name": "智能手机支架",
     "application_no": "CN2024100001.5", "publication_number": "CN-1234567-B1",
     "status": "granted", "ts": "2025-03-02"},
    {"id": "a3", "src": "uspto", "name": "智能手机支架",
     "app": "CN2024100001.5", "pub": "CN1234567B1",
     "status": "更正公告", "event_kind": "amendment",
     "amendment_text": "权利要求更正", "ts": "2025-06-01"},
    {"id": "b1", "src": "uspto", "name": "Smart phone holder",
     "app_no": "US 18/111,222", "priority": "CN2024100001.5",
     "ts": "2024-05-01"},
    {"id": "c1", "src": "uspto", "name": "Holder",
     "app_no": "US1234567", "ts": "2024-02-01"},
    {"id": "c2", "src": "jpo", "name": "Holder",
     "app_no": "JP1234567", "ts": "2024-02-02"},
]


def test_one_application_many_events_is_one_application():
    output, evidence = dedup.dedup_apply(_staged(MIXED))
    cn_rows = [row for row in output
               if row["dedup"]["application_key"] == "CN:APP:20241000015"]
    # publication + grant + amendment => three stored event rows
    assert len(cn_rows) == 3
    assert evidence["applications"]["CN:APP:20241000015"]["records"] == 3
    assert evidence["applications"]["CN:APP:20241000015"]["events"]
    outcomes = {row["id"]: row["dedup"]["outcome"] for row in cn_rows}
    assert outcomes["a1"] == "independent"
    assert outcomes["a2"] == "follow_up"
    assert outcomes["a3"] == "amendment"
    # The amendment supersedes the earlier event and is not a duplicate.
    amendment = next(row for row in cn_rows if row["id"] == "a3")
    assert amendment["dedup"]["supersedes_event_id"]
    assert amendment["dedup"]["duplicate_of"] is None


def test_amendment_gets_its_own_result_and_is_traceable():
    _output, evidence = dedup.dedup_apply(_staged(MIXED))
    assert len(evidence["amendments"]) == 1
    amendment = evidence["amendments"][0]
    assert amendment["kind"] == "amendment_of"
    assert amendment["amendment"]["record_id"] == "a3"
    assert amendment["previous_event_id"]
    assert amendment["supersedes_event_id"] == amendment["previous_event_id"]


def test_family_sibling_stays_independent():
    output, evidence = dedup.dedup_apply(_staged(MIXED))
    keys = {row["dedup"]["application_key"] for row in output}
    assert "US:APP:18111222" in keys
    assert "CN:APP:20241000015" in keys
    us_row = next(row for row in output
                  if row["dedup"]["application_key"] == "US:APP:18111222")
    assert us_row["dedup"]["outcome"] == "independent"
    # The two applications share a family key but were not merged.
    assert "CN:20241000015" in evidence["families"]
    assert set(evidence["families"]["CN:20241000015"]) == {
        "CN:APP:20241000015", "US:APP:18111222"
    }
    rejected = [item for item in evidence["rejected_candidates"]
                if item["kind"] == "same_family_distinct_application"]
    assert rejected and set(rejected[0]["members"]) == {
        "CN:APP:20241000015", "US:APP:18111222"
    }


def test_cross_region_same_number_is_separate_with_conflict_evidence():
    output, evidence = dedup.dedup_apply(_staged(MIXED))
    keys = {row["dedup"]["application_key"] for row in output}
    assert "US:APP:1234567" in keys
    assert "JP:APP:1234567" in keys
    conflicts = [item for item in evidence["cross_region_conflicts"]
                 if item["kind"] == "cross_region_same_number"]
    assert conflicts
    assert conflicts[0]["body"] == "1234567"
    assert conflicts[0]["regions"] == ["JP", "US"]


def test_similar_name_conflicting_identifiers_is_rejected_and_kept():
    output, evidence = dedup.dedup_apply(_staged(MIXED))
    rejected = [item for item in evidence["rejected_candidates"]
                if item["kind"] == "rejected_candidate"]
    assert rejected, "similar-name/conflicting-id pair must be recorded"
    pair = rejected[0]
    if "members" in pair:
        # Exact-name group is summarised in linear time.
        assert pair["member_count"] >= 2
        identifiers = [
            node for member in pair["members"]
            for values in member["canonical_ids"].values() for node in values
        ]
        assert len(set(identifiers)) >= 2
    else:
        assert pair["left"]["application_key"] != pair["right"]["application_key"]
        assert pair["left"]["canonical_ids"] and pair["right"]["canonical_ids"]
    assert pair["name_similarity"] >= 0.72


def test_cross_source_status_spellings_fold_into_one_event():
    records = [
        _app("x1", "cnipa", "支架", "CN2024100009.1", "2025-01-01",
             status="授权"),
        _app("x2", "epo", "支架", "CN2024100009.1", "2025-01-01",
             status="granted"),
    ]
    output, evidence = dedup.dedup_apply(_staged(records))
    assert len(output) == 1
    assert evidence["summary"]["applications"] == 1
    assert evidence["summary"]["duplicates"] == 1


def test_out_of_order_and_duplicate_messages_converge():
    baseline, baseline_evidence = _run(MIXED)
    imports = [
        list(reversed(MIXED)),
        MIXED + [dict(record) for record in MIXED[:3]],
        [MIXED[3], MIXED[0], MIXED[5], MIXED[2], MIXED[1], MIXED[4], MIXED[2]],
    ]
    base_keys = {
        (item["kind"], item["application_key"])
        for item in baseline_evidence if item["kind"] != "duplicate"
    }
    for records in imports:
        result, evidence = _run(records)
        assert result["applications"] == baseline["applications"] == 4
        assert result["rows"] == baseline["rows"] == 6
        assert result["amendments"] == baseline["amendments"] == 1
        assert result["follow_ups"] == baseline["follow_ups"] == 1
        # Events and application decisions are content based, so the audit
        # trail is identical; only extra ``duplicate`` rows may appear.
        keys = {
            (item["kind"], item["application_key"])
            for item in evidence if item["kind"] != "duplicate"
        }
        assert keys == base_keys


def test_partial_failure_recovers_and_converges():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    original = normalizer.process
    try:
        def flaky(record):
            if record.get("id") == "a2":
                raise ValueError("simulated source failure")
            return original(record)

        normalizer.process = flaky
        con = store.connect(path)
        try:
            partial = pipeline.run(con=con, records=MIXED)
        finally:
            con.close()
        assert partial["failures"]
        assert partial["failures"][0]["stage"] == "normalizer"
        assert partial["failures"][0]["record_id"] == "a2"
        # The CN application survives through its other two observations.
        assert partial["applications"] == 4

        normalizer.process = original
        con = store.connect(path)
        try:
            full = pipeline.run(con=con, records=MIXED)
            rows_after_full = store.count(con)
        finally:
            con.close()
        assert full["applications"] == 4
        assert full["failures"] == []
        # The recovered grant event was persisted on the full run.
        assert full["rows"] == rows_after_full == 6
        assert full["follow_ups"] == 1

        # Re-running the identical input is idempotent.
        con = store.connect(path)
        try:
            again = pipeline.run(con=con, records=MIXED)
            assert store.count(con) == rows_after_full
        finally:
            con.close()
        assert again["rows"] == rows_after_full
    finally:
        normalizer.process = original
        os.remove(path)


def test_partial_batch_then_full_batch_does_not_duplicate_rows():
    subset = MIXED[:3]  # only the CN application, publication/grant/amendment
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        con = store.connect(path)
        try:
            pipeline.run(con=con, records=subset)
            partial_rows = store.count(con)
            result = pipeline.run(con=con, records=MIXED)
            assert result["applications"] == 4
            # a1/a2/a3 already existed; only b1/c1/c2 are new.
            assert store.count(con) == partial_rows + 3
        finally:
            con.close()
    finally:
        os.remove(path)


def test_large_candidate_batch_comparison_cost_is_blocked():
    # 1500 applications with distinct names: comparisons are confined to
    # small fuzzy blocks instead of the full n^2 candidate space.
    distinct = [
        _app(f"n{i}", "cnipa", f"装置编号{i}号", f"CN2024{i:07d}", "2024-01-01")
        for i in range(1500)
    ]
    started = time.monotonic()
    output, evidence = dedup.dedup_apply(_staged(distinct))
    elapsed = time.monotonic() - started
    assert evidence["summary"]["applications"] == 1500
    assert len(output) == 1500
    assert evidence["comparisons"]["pairwise"] < 1500 * 1499 // 2
    assert evidence["comparisons"]["pairwise"] <= 5000
    assert elapsed < 10

    # 400 same-name applications with conflicting numbers: exact-name groups
    # are summarised in linear time, so evidence and comparisons stay bounded.
    same_name = [
        _app(f"s{i}", "cnipa", "同名装置", f"CN2025{i:07d}", "2024-01-01")
        for i in range(400)
    ]
    started = time.monotonic()
    _output, evidence = dedup.dedup_apply(_staged(same_name))
    elapsed = time.monotonic() - started
    assert evidence["summary"]["applications"] == 400
    assert evidence["comparisons"]["pairwise"] == 0
    assert evidence["comparisons"]["skipped_blocks"] >= 1
    # One grouped rejection per exact name, not one per pair.
    grouped = [item for item in evidence["rejected_candidates"]
               if item["kind"] == "rejected_candidate" and "members" in item]
    assert len(grouped) == 1
    assert grouped[0]["member_count"] == 400
    assert len(grouped[0]["members"]) <= 200  # evidence is capped, count is exact
    assert elapsed < 10


def test_evidence_is_persisted_for_audit():
    result, evidence = _run(MIXED)
    kinds = {item["kind"] for item in evidence}
    assert "decision" in kinds
    assert "amendment" in kinds
    assert "follow_up" in kinds
    assert "rejected_candidate" in kinds
    assert result["evidence"] == len(evidence)
    # Rejected candidate evidence keeps the refused merge auditable: either a
    # pair with both sides, or a grouped same-name listing with all members.
    rejected = next(item for item in evidence
                    if item["kind"] == "rejected_candidate")
    data = rejected["data"]
    assert ("left" in data and "right" in data) or "members" in data


def test_normalizer_parses_identifiers_and_regions():
    parsed = normalizer.process({
        "id": "z1", "src": "cnipa", "name": "X",
        "app_no": "cn 2024-100001.5", "pub_no": "CN 1234567 A",
    })
    assert parsed["canonical_ids"]["application"] == ["CN:APP:20241000015"]
    assert parsed["canonical_ids"]["publication"] == ["CN:PUB:1234567"]
    # A number prefix beats a source hint.
    us = normalizer.process({"id": "z2", "src": "cnipa", "name": "Y",
                             "app_no": "US1234567"})
    assert us["canonical_ids"]["application"] == ["US:APP:1234567"]


def test_mixed_fixture_import_counts_and_event_sources():
    import json
    import pathlib

    fixture = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "mixed.json"
    records = json.loads(fixture.read_text(encoding="utf-8"))
    result, evidence = _run(records)

    # 8 input messages: CN publication + grant + amendment + a distinct US
    # family sibling + a same-body US/JP pair + a Chinese trademark seen twice.
    assert result["fed"] == 8
    assert result["applications"] == 5
    # 1 duplicate (the trademark re-crawl) and one stored row per event.
    assert result["duplicates"] == 1
    assert result["amendments"] == 1
    assert result["follow_ups"] == 1
    assert result["rows"] == 7

    applications = {item["application_key"]: item["data"]
                    for item in evidence if item["kind"] == "application"}
    assert set(applications) == {
        "CN:APP:20241000015", "US:APP:18111222",
        "US:APP:1234567", "JP:APP:1234567", "CN:REG:30512345",
    }

    # Event provenance is preserved: the grant event is attributed to CNIPA's
    # data even though it was imported from an EPO-labelled source, and the
    # amendment keeps its own source.
    decisions = [item["data"] for item in evidence if item["kind"] == "decision"]
    cn_decisions = [d for d in decisions if d["records"]
                    and d["records"][0]["application_key"] == "CN:APP:20241000015"]
    assert len(cn_decisions) == 3
    kinds = {rec["kind"] for d in cn_decisions for rec in d["records"]}
    assert {"publication", "grant", "amendment"} <= kinds
    amendment = next(d for d in cn_decisions
                     if any(rec["kind"] == "amendment" for rec in d["records"]))
    assert amendment["outcome"] == "amendment"


def test_same_event_from_two_sources_records_both_sources():
    records = [
        {"id": "g1", "src": "cnipa", "name": "支架", "app_no": "CN2024100011.2",
         "status": "授权", "ts": "2025-02-02"},
        {"id": "g2", "src": "epo", "name": "支架", "app_no": "CN2024-100011.2",
         "status": "granted", "ts": "2025-02-02"},
    ]
    output, evidence = dedup.dedup_apply(_staged(records))
    assert len(output) == 1
    assert output[0]["dedup"]["sources"] == ["cnipa", "epo"]
    assert set(output[0]["dedup"]["record_ids"]) == {"g1", "g2"}
    assert evidence["summary"]["duplicates"] == 1


def test_stored_rows_are_permutation_invariant():
    import itertools

    # The grant event is observed twice (different sources/wording) and an
    # amendment follows it.  Every arrival order must produce the same
    # application keys, outcomes, event ids and supersedes chain.
    records = [
        {"id": "d1", "src": "epo", "name": "X", "app_no": "CN2024100011.2",
         "status": "granted", "ts": "2025-02-02"},
        {"id": "d2", "src": "cnipa", "name": "X", "app_no": "CN2024-100011.2",
         "status": "授权", "ts": "2025-02-02"},
        {"id": "d3", "src": "uspto", "name": "X", "app_no": "CN2024100011.2",
         "status": "更正公告", "event_kind": "amendment",
         "amendment_text": "t", "ts": "2025-05-01"},
    ]
    signatures = set()
    for permutation in itertools.permutations(records):
        output, evidence = dedup.dedup_apply(_staged([dict(r) for r in permutation]))
        signature = tuple(sorted(
            (row["dedup"]["application_key"], row["dedup"]["event_id"],
             row["dedup"]["outcome"], row["dedup"]["supersedes_event_id"])
            for row in output
        ))
        signatures.add(signature)
        assert evidence["summary"]["applications"] == 1
        assert evidence["summary"]["amendments"] == 1
    assert len(signatures) == 1


def test_ambiguous_listing_row_is_not_merged_into_either_application():
    records = [
        # A result row that lists two applications (a family listing).
        {"id": "list", "src": "cnipa", "name": "组合", "ts": "2024-01-01",
         "ids": [{"namespace": "application", "value": "CN2024000001.1"},
                 {"namespace": "application", "value": "CN2024000002.2"}]},
        {"id": "one", "src": "cnipa", "name": "组合", "app_no": "CN2024000001.1",
         "ts": "2024-02-01"},
    ]
    output, evidence = dedup.dedup_apply(_staged(records))
    keys = {row["dedup"]["application_key"] for row in output}
    # The listing and the real application are separate rows.
    assert len(keys) == 2
    assert "CN:APP:20240000011" in keys
    listing = next(row for row in output if row["id"] == "list")
    assert listing["dedup"]["application_key"].startswith("FP:")
    assert listing["dedup"]["outcome"] == "independent"


def test_conflicting_publication_claims_do_not_merge_applications():
    records = [
        {"id": "p1", "src": "cnipa", "name": "A", "app_no": "CN2024100001.5",
         "pub_no": "CN1234567A", "ts": "2024-01-01"},
        {"id": "p2", "src": "cnipa", "name": "B", "app_no": "CN2024100009.9",
         "pub_no": "CN1234567A", "ts": "2024-01-02"},
    ]
    output, evidence = dedup.dedup_apply(_staged(records))
    assert len({row["dedup"]["application_key"] for row in output}) == 2
    assert evidence["summary"]["applications"] == 2
