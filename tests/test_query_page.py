"""离线分页测试：页码/游标、插入、重复页、限速、空页、版本变更、游标失效、
回退窗口、断点恢复与模式切换。全部离线、零真实等待。"""
import sqlite3

import pytest

from src.patenttrack import idmap, dedup, store
from src.patenttrack.query_page import (
    MODE_PAGE, MODE_CURSOR, QueryConfig, PageRequest, PageResult,
    Paginator, RateLimited, StaleCursor, FatalFetchError,
    empty_page_action, is_duplicate_page, fallback_window_exceeded,
    is_empty_dataset,
    ERR_RATE_LIMIT, ERR_EMPTY, ERR_DUPLICATE, ERR_STALE_CURSOR,
    ERR_VERSION, ERR_TOTAL_SHRUNK,
)
from src.patenttrack.sim import SimBackend, make_rows


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    store.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def cfg():
    return QueryConfig(query={"q": "widget", "country": "CN"})


def no_sleep(_seconds):
    pass


def paginator(backend, con, **kwargs):
    kwargs.setdefault("sleeper", no_sleep)
    return Paginator(backend, con=con, **kwargs)


# ---- 纯函数：证据驱动的决策 -----------------------------------------------

def test_empty_page_action_decisions():
    # total 可见且已确认覆盖达到 total → 停止
    assert empty_page_action(total=20, confirmed=20, attempts=1,
                             max_attempts=4) == "stop"
    # 无 total（证据不足）→ 重试
    assert empty_page_action(total=None, confirmed=10, attempts=1,
                             max_attempts=4) == "retry"
    # 预算耗尽 → 隔离待补
    assert empty_page_action(total=20, confirmed=10, attempts=4,
                             max_attempts=4) == "quarantine"


def test_empty_dataset_evidence():
    assert is_empty_dataset(total=0, page_size=10, page_index=0)
    assert is_empty_dataset(total=10, page_size=10, page_index=1)
    assert not is_empty_dataset(total=20, page_size=10, page_index=1)
    assert not is_empty_dataset(total=None, page_size=10, page_index=1)


def test_duplicate_and_window_predicates():
    b = [{"first_id": "a", "last_id": "c"}]
    assert is_duplicate_page(first_id="a", last_id="c", known_boundaries=b)
    assert not is_duplicate_page(first_id="a", last_id="x", known_boundaries=b)
    assert not is_duplicate_page(first_id=None, last_id=None,
                                 known_boundaries=b)
    assert not fallback_window_exceeded(backtracks=2, window=3)
    assert fallback_window_exceeded(backtracks=4, window=3)


# ---- 基本覆盖：页码 / 游标 -------------------------------------------------

def test_page_mode_full_crawl(con, cfg):
    backend = SimBackend(make_rows(23))
    s = paginator(backend, con).crawl(cfg, mode=MODE_PAGE, page_size=10,
                                     run_id="r-page", resume=False)
    assert s.status == "done"
    assert s.pages_confirmed == 3
    assert s.unique_rows == 23
    assert s.written == 23
    assert store.count(con) == 23
    # 每次请求的可复现边界都已落库：3 个数据页 + 1 个耗尽探测空页
    reqs = store.list_requests(con, s.run_id)
    assert [r["request"]["page"] for r in reqs] == [0, 1, 2, 3]
    assert reqs[-1]["response_meta"]["count"] == 0
    pages = store.list_pages(con, s.run_id)
    assert pages[0]["first_id"] == "r-0000"
    assert pages[2]["last_id"] == "r-0022"
    # 记录带查询版本与边界，传给下游
    data = con.execute(
        "SELECT data FROM records WHERE key='r-0000'").fetchone()[0]
    import json
    d = json.loads(data)
    assert d["_query_version"] == cfg.version
    assert d["_run_id"] == "r-page"
    assert d["_page_index"] == 0


def test_cursor_mode_full_crawl(con, cfg):
    backend = SimBackend(make_rows(25), serve_version=cfg.version)
    s = paginator(backend, con).crawl(cfg, mode=MODE_CURSOR, page_size=10,
                                     run_id="r-cur", resume=False)
    assert s.status == "done"
    assert s.pages_confirmed == 3
    assert s.unique_rows == 25 == store.count(con)
    # 游标边界可复现：entry_cursor / next_cursor 均已保存
    pages = store.list_pages(con, s.run_id)
    assert pages[0]["entry_cursor"] is None
    assert pages[1]["entry_cursor"] == pages[0]["next_cursor"]
    assert pages[-1]["next_cursor"] is None


def test_second_crawl_same_version_is_idempotent(con, cfg):
    backend = SimBackend(make_rows(12))
    p = paginator(backend, con)
    s1 = p.crawl(cfg, run_id="run-a", resume=False)
    assert s1.status == "done" and s1.written == 12
    # 再来一次：旧运行已 done，自动开新运行；幂等 upsert 不增行
    s2 = p.crawl(cfg, run_id="run-b", resume=False)
    assert s2.status == "done"
    assert store.count(con) == 12


# ---- 结果插入：回退重采，不丢不重 ------------------------------------------

def test_insert_during_page_crawl(con, cfg):
    # 第 2 次调用（抓 page 1）前，在头部插入 5 条
    backend = SimBackend(make_rows(30), events={2: [("insert_head", 5)]})
    s = paginator(backend, con).crawl(cfg, mode=MODE_PAGE, page_size=10,
                                     run_id="r-ins", resume=False)
    assert s.status == "done"
    assert s.unique_rows == 35          # 30 原始 + 5 插入，全部确认
    assert store.count(con) == 35       # 写入计数一致
    assert not s.quarantine


def test_insert_during_cursor_crawl(con, cfg):
    backend = SimBackend(make_rows(30), serve_version=cfg.version,
                         events={2: [("insert_head", 4)]})
    s = paginator(backend, con).crawl(cfg, mode=MODE_CURSOR, page_size=10,
                                     run_id="r-cins", resume=False)
    assert s.status == "done"
    assert s.unique_rows == 34
    assert store.count(con) == 34
    assert not s.quarantine


def test_insert_on_last_page_still_realigns(con, cfg):
    # 漂移发生在最后一页：不能因耗尽判定跳过回退重采
    backend = SimBackend(make_rows(20), events={3: [("insert_head", 3)]})
    s = paginator(backend, con).crawl(cfg, mode=MODE_PAGE, page_size=10,
                                     run_id="r-lastins", resume=False)
    assert s.status == "done"
    assert s.unique_rows == 23
    assert store.count(con) == 23
    assert not s.quarantine


def test_persistent_drift_isolated_after_window(con, cfg):
    # 每次都在头部插入，漂移持续超出回退窗口 → 隔离待补而非静默截断
    backend = SimBackend(
        make_rows(40),
        events={i: [("insert_head", 6)] for i in range(2, 9)})
    s = paginator(backend, con, fallback_window=2).crawl(
        cfg, mode=MODE_PAGE, page_size=10, run_id="r-win", resume=False)
    assert s.status == "partial"
    assert s.quarantine and s.quarantine[0]["code"] == ERR_STALE_CURSOR
    # 已确认的行都已写入，没有重复
    assert store.count(con) == s.unique_rows


# ---- 重复页：重试后继续 ----------------------------------------------------

def test_duplicate_page_retried_then_continues(con, cfg):
    # 第 2 次调用回放第 1 次的响应；下一轮恢复正常
    backend = SimBackend(make_rows(20), events={2: "duplicate"})
    s = paginator(backend, con).crawl(cfg, mode=MODE_PAGE, page_size=10,
                                     run_id="r-dup", resume=False)
    assert s.status == "done"
    assert s.unique_rows == 20
    assert store.count(con) == 20
    # 审计里能复现重复页请求边界
    reqs = store.list_requests(con, s.run_id)
    assert sum(1 for r in reqs if r["request"]["page"] == 1) == 2


def test_persistent_duplicate_quarantined(con, cfg):
    # 同一位置持续重复 → 预算耗尽，partial + 隔离
    backend = SimBackend(make_rows(20),
                         events={i: "duplicate" for i in range(2, 10)})
    s = paginator(backend, con, max_attempts=3).crawl(
        cfg, mode=MODE_PAGE, page_size=10, run_id="r-dupx", resume=False)
    assert s.status == "partial"
    assert s.quarantine[0]["code"] == ERR_DUPLICATE
    assert s.unique_rows == 10  # 只有第一页被确认写入


# ---- 限速：退避重试 / 预算耗尽 ---------------------------------------------

def test_rate_limit_retried_with_backoff(con, cfg):
    sleeps = []
    backend = SimBackend(make_rows(15), events={1: "rate_limit"})
    p = Paginator(backend, con=con, sleeper=sleeps.append)
    s = p.crawl(cfg, mode=MODE_PAGE, page_size=10, run_id="r-rl",
                resume=False)
    assert s.status == "done"
    # 1 次限速重试 + 2 个数据页 + 1 个耗尽探测页
    assert s.requests == 4
    assert sleeps             # 退避被触发（测试中无真实等待）
    reqs = store.list_requests(con, s.run_id)
    assert any(r["error_code"] == ERR_RATE_LIMIT for r in reqs)
    assert s.unique_rows == 15


def test_rate_limit_exhausted_is_partial_and_reproducible(con, cfg):
    backend = SimBackend(make_rows(15),
                         events={i: "rate_limit" for i in range(1, 8)})
    s = paginator(backend, con, max_attempts=3).crawl(
        cfg, mode=MODE_PAGE, page_size=10, run_id="r-rlx", resume=False)
    assert s.status == "partial"
    assert s.error == ERR_RATE_LIMIT
    assert s.quarantine[0]["code"] == ERR_RATE_LIMIT
    # 失败原因与请求边界可复现
    reqs = store.list_requests(con, s.run_id)
    assert all(r["error_code"] == ERR_RATE_LIMIT for r in reqs)
    assert reqs[0]["request"] == {
        "query_version": cfg.version, "mode": MODE_PAGE, "page": 0,
        "cursor": None, "page_size": 10, "attempt": 1}


# ---- 空页：证据决定停止/重试/隔离 ------------------------------------------

def test_empty_page_with_unknown_total_is_quarantined(con, cfg):
    class UnknownTotal:
        def __init__(self):
            self.n = 0

        def __call__(self, req):
            self.n += 1
            if self.n == 1:
                return PageResult(rows=make_rows(10), total=None)
            return PageResult(rows=[], total=None)  # 空页且无 total

    s = paginator(UnknownTotal(), con, max_attempts=3).crawl(
        cfg, mode=MODE_PAGE, page_size=10, run_id="r-empty", resume=False)
    assert s.status == "partial"
    assert s.quarantine[0]["code"] == ERR_EMPTY


def test_empty_dataset_done_without_retry(con, cfg):
    # total=0 的空数据集：可观察证据足以判定正常耗尽
    backend = SimBackend(make_rows(0))
    s = paginator(backend, con).crawl(cfg, mode=MODE_PAGE, page_size=10,
                                     run_id="r-zero", resume=False)
    assert s.status == "done"
    assert s.unique_rows == 0
    assert store.list_quarantine(con, s.run_id) == []


def test_empty_page_at_exhaustion_stops(con, cfg):
    class ExactTotal:
        def __call__(self, req):
            rows = make_rows(10)
            page = req.page or 0
            chunk = rows[page * 10:(page + 1) * 10]
            return PageResult(rows=chunk, total=10)

    s = paginator(ExactTotal(), con).crawl(cfg, mode=MODE_PAGE,
                                           page_size=10, run_id="r-ex",
                                           resume=False)
    assert s.status == "done"
    assert s.unique_rows == 10


# ---- 查询条件变更：版本隔离，旧游标不混用 ----------------------------------

def test_query_version_change_starts_isolated_run(con, cfg):
    backend = SimBackend(make_rows(20), serve_version=cfg.version)
    p = paginator(backend, con)
    s1 = p.crawl(cfg, mode=MODE_CURSOR, page_size=10, run_id="r-v1",
                 resume=False)
    assert s1.status == "done"

    cfg2 = QueryConfig(query={"q": "gadget", "country": "CN"})
    assert cfg2.version != cfg.version
    # 显式复用旧 run_id 必须被拒绝（旧批次不能混入新查询）
    with pytest.raises(StaleCursor):
        p.crawl(cfg2, run_id="r-v1", resume=False)

    # 服务端切换版本并作废旧游标；新查询走新运行，账本/边界/写入独立
    backend.reset()
    backend.events = {1: ("version", cfg2.version)}
    backend.rows = make_rows(8, prefix="g")
    s2 = p.crawl(cfg2, mode=MODE_CURSOR, page_size=10, run_id="r-v2",
                 resume=False)
    assert s2.status == "done"
    assert s2.query_version == cfg2.version
    assert s2.unique_rows == 8
    run2 = store.get_run(con, "r-v2")
    assert run2["query_version"] == cfg2.version
    # 旧运行的边界未被新运行污染
    assert store.get_page(con, "r-v1", 0)["first_id"] == "r-0000"


def test_stale_cursor_falls_back_to_confirmed_boundary(con, cfg):
    # 第 3 次调用（page 2 的游标）失效一次；回退到已确认边界重建游标链
    backend = SimBackend(make_rows(25), serve_version=cfg.version,
                         events={3: ("stale_cursor", 1)})
    s = paginator(backend, con).crawl(cfg, mode=MODE_CURSOR, page_size=10,
                                     run_id="r-stale", resume=False)
    assert s.status == "done"
    assert s.unique_rows == 25
    assert store.count(con) == 25
    reqs = store.list_requests(con, s.run_id)
    assert any(r["error_code"] == ERR_STALE_CURSOR for r in reqs)


def test_stale_cursor_at_first_boundary_is_quarantined(con, cfg):
    # 首次游标请求就失效（无已确认边界可退）→ 隔离待补
    backend = SimBackend(make_rows(10), serve_version=cfg.version)
    backend._stale_left = 1

    class FirstStale:
        def __init__(self, inner):
            self.inner = inner
            self.first = True

        def __call__(self, req):
            if self.first and req.cursor is None:
                self.first = False
                raise StaleCursor("epoch reset before first cursor")
            return self.inner(req)

    s = paginator(FirstStale(backend), con).crawl(
        cfg, mode=MODE_CURSOR, page_size=10, run_id="r-stale0",
        resume=False)
    assert s.status == "partial"
    assert s.quarantine[0]["code"] == ERR_STALE_CURSOR


# ---- 总数变化 --------------------------------------------------------------

def test_total_shrunk_is_detected_and_quarantined(con, cfg):
    backend = SimBackend(make_rows(25),
                         events={3: [("shrink", 10)]})  # total 25 → 15
    s = paginator(backend, con).crawl(cfg, mode=MODE_PAGE, page_size=10,
                                     run_id="r-shrink", resume=False)
    assert s.status == "partial"
    assert s.quarantine[0]["code"] == ERR_TOTAL_SHRUNK
    assert s.unique_rows == 20  # 已确认 20 > 新 total 15


# ---- 断点恢复 --------------------------------------------------------------

def test_resume_after_crash_continues_same_context(con, cfg, tmp_path):
    db_path = tmp_path / "resume.db"
    fcon = sqlite3.connect(db_path)
    store.init_schema(fcon)

    class CrashFetcher:
        def __init__(self):
            self.calls = 0

        def __call__(self, req):
            self.calls += 1
            if self.calls == 2:
                raise FatalFetchError("simulated process crash")
            offset = (req.page or 0) * req.page_size
            rows = make_rows(23)[offset:offset + req.page_size]
            return PageResult(rows=rows, total=23)

    p1 = Paginator(CrashFetcher(), con=fcon, sleeper=no_sleep)
    with pytest.raises(FatalFetchError):
        p1.crawl(cfg, mode=MODE_PAGE, page_size=10, run_id="r-resume",
                 resume=False)
    fcon.commit()
    fcon.close()

    # 新进程：重新打开库，自动找到可恢复运行，从检查点继续
    con2 = sqlite3.connect(db_path)
    store.init_schema(con2)

    class Static:
        def __call__(self, req):
            offset = (req.page or 0) * req.page_size
            rows = make_rows(23)[offset:offset + req.page_size]
            return PageResult(rows=rows, total=23)

    p2 = Paginator(Static(), con=con2, sleeper=no_sleep)
    s = p2.crawl(cfg, mode=MODE_PAGE, page_size=10)  # resume 自动命中
    assert s.run_id == "r-resume"
    assert s.status == "done"
    assert s.unique_rows == 23
    assert store.count(con2) == 23
    run = store.get_run(con2, "r-resume")
    assert run["query_version"] == cfg.version
    con2.close()


def test_mode_switch_resets_progress_within_same_version(con, cfg):
    backend = SimBackend(make_rows(15), serve_version=cfg.version)
    p = paginator(backend, con)
    s1 = p.crawl(cfg, mode=MODE_PAGE, page_size=10, run_id="r-switch",
                 resume=False)
    assert s1.status == "done" and s1.mode == MODE_PAGE

    # 切到游标：旧页码边界不跨模式复用，进度重建；审计保留
    p.switch_mode("r-switch", MODE_CURSOR, cfg)
    s2 = p.crawl(cfg, run_id="r-switch", resume=False)
    assert s2.status == "done" and s2.mode == MODE_CURSOR
    assert s2.unique_rows == 15
    assert store.count(con) == 15          # 幂等，无重复写入
    assert store.list_requests(con, "r-switch")  # 审计未清空

    # 跨版本切换必须拒绝
    cfg2 = QueryConfig(query={"q": "other"})
    with pytest.raises(StaleCursor):
        p.switch_mode("r-switch", MODE_PAGE, cfg2)


# ---- 下游：idmap 版本守卫 / dedup 版本隔离 ---------------------------------

def test_idmap_guards_query_version():
    rec = {"id": "x", "_query_version": "qv-1", "_run_id": "r",
           "_page_index": 0, "_page_mode": MODE_PAGE}
    out = idmap.process(rec, expected_version="qv-1")
    assert out["_idmap_query_version"] == "qv-1"
    with pytest.raises(idmap.QueryVersionMismatch):
        idmap.process(rec, expected_version="qv-2")
    # 无分页上下文的既有调用保持兼容
    assert idmap.process({"id": "y"})["_stage_idmap"] == "naive"


def test_dedup_scopes_by_query_version_and_run():
    seen = set()
    base = {"_query_version": "qv-1", "_run_id": "r1"}
    r1 = dedup.process(dict(base, id="a"), seen=seen)
    assert r1 and r1["_dedup_hit"] is False
    assert dedup.process(dict(base, id="a"), seen=seen) is None  # 同运行重采
    # 同 id 但新查询版本/新运行：不应被旧账本吞掉
    r2 = dedup.process({"_query_version": "qv-2", "_run_id": "r2",
                        "id": "a"}, seen=seen)
    assert r2 is not None
    # 无上下文的既有逐记录路径兼容
    assert dedup.process({"id": "z"})["_stage_dedup"] == "ok"


def test_boundary_replay_is_reproducible(con, cfg):
    backend = SimBackend(make_rows(12))
    s = paginator(backend, con).crawl(cfg, mode=MODE_PAGE, page_size=10,
                                     run_id="r-rep", resume=False)
    static = make_rows(12)
    data_reqs = [r for r in store.list_requests(con, s.run_id)
                 if r["response_meta"]["count"] > 0]
    # 用保存下来的请求边界重放，响应首尾 id 与已确认边界一致
    for r in data_reqs:
        req = PageRequest(**r["request"])
        offset = req.page * req.page_size
        chunk = static[offset:offset + req.page_size]
        page = store.get_page(con, s.run_id, req.page)
        assert page["first_id"] == chunk[0]["id"]
        assert page["last_id"] == chunk[-1]["id"]
        assert r["http_status"] == 200
    # 耗尽探测请求同样可复现：重放得到空页
    probe = [r for r in store.list_requests(con, s.run_id)
             if r["response_meta"]["count"] == 0][0]
    preq = PageRequest(**probe["request"])
    assert static[preq.page * preq.page_size:] == []
