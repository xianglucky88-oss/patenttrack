"""单一职责：idmap —— 编号归并，并继承分页抓取上下文。

分页恢复时必须携带查询版本（``_query_version``）与请求边界
（``_run_id`` / ``_page_index`` / ``_page_mode``）：旧游标对应的旧批次
记录若与当前查询版本不一致，会在这里被识别并隔离，而不是混入新批次。
"""
from .query_page import ERR_VERSION

REQUIRED_CONTEXT = ("_query_version", "_run_id", "_page_index", "_page_mode")


class QueryVersionMismatch(Exception):
    """记录携带的查询版本与当前运行不一致（旧游标污染防护）。"""


def process(record, *, expected_version=None):
    rec = dict(record)
    rec.setdefault("_stage_idmap", "naive")

    qv = rec.get("_query_version")
    if qv is not None:
        # 透传分页上下文给 dedup / store，并做版本守卫
        for key in REQUIRED_CONTEXT:
            rec.setdefault(key, None)
        rec["_idmap_query_version"] = qv
        if expected_version is not None and qv != expected_version:
            raise QueryVersionMismatch(
                f"{ERR_VERSION}: record={qv} expected={expected_version} "
                f"run={rec.get('_run_id')} page={rec.get('_page_index')}")
    return rec
