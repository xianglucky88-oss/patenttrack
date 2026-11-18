import os
import tempfile

from src.patenttrack import pipeline, store


def _isolated():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    return path


def test_pipeline_runs_and_idempotent():
    path = _isolated()
    try:
        con = store.connect(path)
        try:
            a = pipeline.run(con=con)
            b = pipeline.run(con=con)   # 重跑不增行（幂等 upsert）
        finally:
            con.close()
        assert a["fed"] == 4
        assert b["rows"] == a["rows"] == 3   # r-003 重复被去重
        assert a["duplicates"] == 1
        assert a["applications"] == 3
    finally:
        os.remove(path)


def test_store_roundtrip():
    path = _isolated()
    try:
        con = store.connect(path)
        try:
            assert store.count(con) == 0
            assert store.evidence_count(con) == 0
        finally:
            con.close()
    finally:
        os.remove(path)
