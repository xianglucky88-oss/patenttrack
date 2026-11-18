"""单一职责：dedup —— 去重，并按查询版本/运行隔离去重账本。

分页抓取会回退重采同一区间；去重必须在“同一查询版本 + 同一运行”的
上下文内判断，不能让旧批次的 id 吞掉新查询的同 id 记录，也不能让
回退重采产生重复写入。
"""

def process(record, *, seen=None):
    rec = dict(record)
    rec.setdefault("_stage_dedup", "ok")

    qv = rec.get("_query_version")
    run_id = rec.get("_run_id")
    rid = rec.get("id")
    if qv is not None and run_id is not None and rid is not None and seen is not None:
        scoped_key = (qv, run_id, str(rid))
        if scoped_key in seen:
            rec["_dedup_hit"] = True
            return None  # 同检索式同运行内重复：丢弃，不写入
        seen.add(scoped_key)
        rec["_dedup_hit"] = False
    return rec
