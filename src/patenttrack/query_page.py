"""单一职责：检索式分页抓取与分页恢复。

设计要点（决策只接受可观察证据）：
- 查询配置哈希出 ``query_version``，运行全程携带；恢复必须命中同一版本，
  下游 idmap/dedup/store 按版本隔离，旧游标不会把新查询结果混进旧批次。
- 每次请求的请求边界（页码/游标/页大小/版本）与响应边界（首尾 id、总数、
  下一个游标）都落库，失败原因（http 状态/错误码）同样可复现。
- 空页：仅当 ``total`` 可见且已确认数已达 ``total`` 才正常停止；否则按
  预算重试，预算耗尽则隔离待补（partial），而不是静默截断。
- 限速/瞬时错误：按 ``retry_after`` 退避重试（可注入 sleeper，测试零等待），
  总预算耗尽后以 failed/partial 结束，原因写审计表，重启可从检查点继续。
- 游标失效/结果集漂移（插入、重复页、总数缩水、旧游标不兼容）：回退到
  最后一个已确认页面边界重采，回退窗口由调用方配置；窗口内追不齐则隔离
  待补，重试只依赖已确认覆盖边界，不依赖易失游标，因此不丢不重。
- 检查点在每个已确认页面后写入；崩溃/重启后从最后确认页继续。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import store as _store

MODE_PAGE = "page"
MODE_CURSOR = "cursor"

# 终止/异常原因码（写入审计与隔离表，保证失败原因可复现）
ERR_RATE_LIMIT = "rate_limited"
ERR_EMPTY = "empty_page"
ERR_DUPLICATE = "duplicate_page"
ERR_STALE_CURSOR = "stale_cursor"
ERR_VERSION = "version_mismatch"
ERR_TOTAL_SHRUNK = "total_shrunk"
ERR_FETCH = "fetch_error"


# ---- 数据结构 -------------------------------------------------------------

@dataclass(frozen=True)
class QueryConfig:
    """检索式配置；``version`` 由规范化 JSON 哈希派生，条件一变版本即变。"""
    query: dict[str, Any]
    version: Optional[str] = None

    def __post_init__(self):
        if self.version is None:
            digest = hashlib.sha256(
                json.dumps(self.query, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]
            object.__setattr__(self, "version", "qv-" + digest)


@dataclass
class PageRequest:
    """一次请求的可复现边界。"""
    query_version: str
    mode: str
    page: Optional[int]
    cursor: Optional[str]
    page_size: int
    attempt: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {"query_version": self.query_version, "mode": self.mode,
                "page": self.page, "cursor": self.cursor,
                "page_size": self.page_size, "attempt": self.attempt}


@dataclass
class PageResult:
    """一次响应的可观察证据。"""
    rows: list[dict[str, Any]]
    total: Optional[int]
    next_cursor: Optional[str] = None
    query_version: Optional[str] = None
    http_status: int = 200


@dataclass
class PageBoundary:
    """已确认页面的覆盖边界。"""
    page_index: int
    entry_cursor: Optional[str]
    next_cursor: Optional[str]
    total: Optional[int]
    first_id: Optional[str]
    last_id: Optional[str]
    count: int
    request: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"page_index": self.page_index, "entry_cursor": self.entry_cursor,
                "next_cursor": self.next_cursor, "total": self.total,
                "first_id": self.first_id, "last_id": self.last_id,
                "count": self.count, "request": self.request}


class RateLimited(Exception):
    def __init__(self, retry_after: float = 0.0):
        super().__init__("rate limited")
        self.retry_after = max(0.0, retry_after)


class StaleCursor(Exception):
    """旧游标与当前查询/结果集不兼容。"""


class FatalFetchError(Exception):
    """致命抓取错误（进程崩溃/连接永久断开）：不重试，直接冒泡并落检查点。"""


@dataclass
class CrawlSummary:
    run_id: str
    query_version: str
    status: str
    mode: str
    requests: int
    pages_confirmed: int
    unique_rows: int
    written: int
    quarantine: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


# ---- 纯函数决策（证据 → 动作，便于单测） ----------------------------------

def empty_page_action(*, total: Optional[int], confirmed: int,
                      attempts: int, max_attempts: int) -> str:
    """空页是再试还是停止，只依据可观察证据。

    ``stop``  —— 服务端 total 可见且已确认覆盖达到 total（正常耗尽）；
    ``retry`` —— 证据不足（无 total 或未达 total）且重试预算未满；
    ``quarantine`` —— 预算耗尽仍空页，隔离待补而非静默截断。
    """
    if total is not None and confirmed >= total:
        return "stop"
    if attempts < max_attempts:
        return "retry"
    return "quarantine"


def is_empty_dataset(*, total: Optional[int], page_size: int,
                     page_index: int) -> bool:
    """可观察的空数据集证据：total 为 0，或页码已越过 total 覆盖范围。"""
    if total == 0:
        return True
    if total is not None and page_index * page_size >= total:
        return True
    return False


def is_duplicate_page(*, first_id: Optional[str], last_id: Optional[str],
                      known_boundaries: list[dict[str, Any]]) -> bool:
    """重复页判定：响应首尾 id 与任一已确认边界完全相同。

    覆盖两种可观察形态：同位置重复（同索引边界）与游标不推进导致的
    整页回放（与上一确认页边界相同）。
    """
    if first_id is None:
        return False
    return any(b["first_id"] == first_id and b["last_id"] == last_id
               for b in known_boundaries if b)


def fallback_window_exceeded(*, backtracks: int, window: int) -> bool:
    """回退范围是否超出已确认覆盖边界允许的窗口。"""
    return backtracks > window


# ---- 流水线阶段接口（兼容既有逐记录 process 调用） ------------------------

def process(record):
    """流水线单记录变换：打上分页阶段标记（naive 兼容路径）。"""
    rec = dict(record)
    rec.setdefault("_stage_query_page", "ok")
    return rec


def stamp_pagination_context(record, *, query_version, run_id, page_index,
                             mode):
    """把查询版本与请求边界盖到记录上，供 idmap/dedup/store 继承。"""
    rec = dict(record)
    rec["_query_version"] = query_version
    rec["_run_id"] = run_id
    rec["_page_index"] = page_index
    rec["_page_mode"] = mode
    return rec


# ---- 抓取引擎 -------------------------------------------------------------

class Paginator:
    """分页抓取器。

    fetcher: Callable[[PageRequest], PageResult]，离线可注入模拟实现。
    sink:    每条确认记录的回调（默认走 idmap→dedup→keyer→store），
             返回 (key, record) 或 None（被去重丢弃）。
    sleeper: 限速退避时钟（测试注入零等待实现）。
    """

    def __init__(self, fetcher: Callable[[PageRequest], PageResult], *,
                 con=None, sink: Optional[Callable] = None,
                 sleeper: Callable[[float], None] = time.sleep,
                 max_attempts: int = 4, fallback_window: int = 3,
                 backoff_base: float = 0.05):
        self._fetcher = fetcher
        self._own_con = con is None
        self.con = con or _store.connect()
        self._sink = sink
        self._sleep = sleeper
        self.max_attempts = max_attempts
        self.fallback_window = fallback_window
        self.backoff_base = backoff_base
        # 同一请求边界跨循环轮次的尝试计数（空页/重复页预算）
        self._boundary_tries: dict[str, int] = {}
        # 本次 crawl 实际交付下游的记录 key（跨主循环/回退/重采统一计数）
        self._delivered_keys: set[str] = set()

    @staticmethod
    def _boundary_key(req: PageRequest) -> str:
        return f"{req.mode}:{req.page}:{req.cursor}:{req.page_size}"

    def switch_mode(self, run_id: str, new_mode: str,
                    config: QueryConfig) -> None:
        """同一查询版本内页码↔游标切换。

        旧游标不跨模式复用：重置进度后从首页重新确认，去重账本与边界
        重建；请求审计保留。版本不一致直接拒绝，避免新旧批次混杂。
        """
        assert new_mode in (MODE_PAGE, MODE_CURSOR)
        run = _store.get_run(self.con, run_id)
        if run is None:
            raise ValueError(f"unknown run_id: {run_id}")
        if run["query_version"] != config.version:
            raise StaleCursor(
                f"run {run_id} belongs to {run['query_version']}, "
                f"current query is {config.version}")
        self.con.execute("DELETE FROM qp_seen WHERE run_id=?", (run_id,))
        self.con.execute("DELETE FROM qp_pages WHERE run_id=?", (run_id,))
        self.con.execute("DELETE FROM qp_quarantine WHERE run_id=?", (run_id,))
        _store.update_run(self.con, run_id, mode=new_mode, status="running",
                          next_page=0, seen_total=0, last_cursor="",
                          next_cursor=None, total_hint=None, error=None)
        self._boundary_tries.clear()
        self.con.commit()

    # -- 对外入口 --

    def crawl(self, config: QueryConfig, *, mode: str = MODE_PAGE,
              page_size: int = 10, run_id: Optional[str] = None,
              resume: bool = True) -> CrawlSummary:
        assert mode in (MODE_PAGE, MODE_CURSOR)
        # 1) 未指定 run：同版本有可恢复运行则续跑，否则新建
        if run_id is None:
            if resume:
                run_id = _store.find_resumable(self.con, config.version)
            if run_id is None:
                run_id = "run-" + uuid.uuid4().hex[:12]
                _store.create_run(self.con, run_id=run_id,
                                  query_version=config.version,
                                  query_config=config.query, mode=mode,
                                  page_size=page_size)
                self.con.commit()
        elif _store.get_run(self.con, run_id) is None:
            # 2) 显式 run_id 但不存在：作为新运行创建
            _store.create_run(self.con, run_id=run_id,
                              query_version=config.version,
                              query_config=config.query, mode=mode,
                              page_size=page_size)
            self.con.commit()
        else:
            # 3) 显式 run_id 已存在：校验版本后按其检查点恢复
            run_meta = _store.get_run(self.con, run_id)
            if run_meta["query_version"] != config.version:
                # 查询条件已变更：拒绝复用旧运行（含旧游标），避免混批。
                raise StaleCursor(
                    f"run {run_id} belongs to {run_meta['query_version']}, "
                    f"current query is {config.version}")
            mode = run_meta["mode"]
            page_size = run_meta["page_size"]

        try:
            summary = self._crawl_loop(config, run_id, mode, page_size)
        except StaleCursor as exc:
            _store.update_run(self.con, run_id, status="failed",
                              error=f"{ERR_STALE_CURSOR}: {exc}")
            self.con.commit()
            raise
        except Exception as exc:  # 失败原因落库，重启可复现
            _store.update_run(self.con, run_id, status="failed",
                              error=f"{ERR_FETCH}: {type(exc).__name__}: {exc}")
            self.con.commit()
            raise
        else:
            self.con.commit()
            return summary
        finally:
            if self._own_con:
                self.con.close()

    # -- 主循环 --

    def _crawl_loop(self, config: QueryConfig, run_id: str, mode: str,
                    page_size: int) -> CrawlSummary:
        con = self.con
        requests_made = 0
        last_error: Optional[str] = None
        self._delivered_keys = set()

        while True:
            run = _store.get_run(con, run_id)
            if run["status"] not in ("running", "partial", "failed"):
                break
            page_index = run["next_page"]

            # 构造下一次请求边界（检查点恢复点）
            if mode == MODE_PAGE:
                req = PageRequest(config.version, mode, page=page_index,
                                  cursor=None, page_size=page_size)
            else:
                if page_index == 0:
                    req = PageRequest(config.version, mode, page=None,
                                      cursor=None, page_size=page_size)
                else:
                    prev = _store.get_page(con, run_id, page_index - 1)
                    # 优先使用上一确认页保存的 next_cursor（回退重采会重建
                    # 游标链）；运行行 next_cursor 仅作兜底
                    cursor = (prev["next_cursor"] if prev else None) \
                        or run["next_cursor"]
                    if not cursor:
                        # 上一确认页已声明游标链结束：正常耗尽而非游标丢失
                        if prev is not None and prev["next_cursor"] is None \
                                and prev["count"] > 0:
                            _store.update_run(con, run_id, status="done")
                            con.commit()
                            break
                        raise StaleCursor("no resumable cursor in checkpoint")
                    req = PageRequest(config.version, mode, page=None,
                                      cursor=cursor, page_size=page_size)

            bkey = self._boundary_key(req)
            self._boundary_tries[bkey] = self._boundary_tries.get(bkey, 0) + 1
            boundary_attempts = self._boundary_tries[bkey]

            result, attempts, err = self._fetch_one(run_id, req)
            requests_made += attempts
            if result is None:
                if err == ERR_STALE_CURSOR and page_index > 0:
                    # 旧游标不兼容：依据已确认覆盖边界回退重采。
                    # 锚点 = 待确认页（失败边界本身），回退从其上一页开始。
                    realigned, rerr = self._realign(config, run_id, mode,
                                                    page_size, page_index)
                    if realigned:
                        self._boundary_tries.pop(bkey, None)
                        continue  # 检查点已在回退路径推进
                    err = rerr or ERR_STALE_CURSOR
                # 重试预算耗尽 / 无可回退边界：partial + 隔离，重启可续。
                last_error = err
                _store.quarantine(con, run_id=run_id, code=err,
                                  detail="retry budget exhausted or no "
                                         "confirmed boundary to fall back to",
                                  scope=req.as_dict())
                _store.update_run(con, run_id, status="partial", error=err)
                break

            # 查询版本核对：响应不得属于别的检索式
            if result.query_version and result.query_version != config.version:
                _store.quarantine(con, run_id=run_id, code=ERR_VERSION,
                                  detail=f"response={result.query_version} "
                                         f"expected={config.version}",
                                  scope=req.as_dict())
                _store.update_run(con, run_id, status="partial",
                                  error=ERR_VERSION)
                last_error = ERR_VERSION
                break

            ids = [str(r.get("id")) for r in result.rows if r.get("id") is not None]
            first_id, last_id = (ids[0] if ids else None), (ids[-1] if ids else None)

            # 总数缩水的可观察证据：total 回退到已确认覆盖之下
            confirmed = _store.seen_count(con, run_id)
            if result.total is not None and result.total < confirmed:
                _store.quarantine(con, run_id=run_id, code=ERR_TOTAL_SHRUNK,
                                  detail=f"total={result.total} < confirmed={confirmed}",
                                  scope=req.as_dict())
                _store.update_run(con, run_id, status="partial",
                                  error=ERR_TOTAL_SHRUNK)
                last_error = ERR_TOTAL_SHRUNK
                break

            # ---- 空页：停止 / 重试 / 隔离，由证据决定 ----
            if not result.rows:
                if is_empty_dataset(total=result.total, page_size=page_size,
                                    page_index=page_index):
                    _store.update_run(con, run_id, status="done",
                                      total_hint=result.total)
                    break
                action = empty_page_action(
                    total=result.total, confirmed=confirmed,
                    attempts=boundary_attempts, max_attempts=self.max_attempts)
                if action == "stop":
                    _store.update_run(con, run_id, status="done",
                                      total_hint=result.total)
                    break
                if action == "retry":
                    # 记录一次空页审计后，下一轮同边界重试
                    continue
                _store.quarantine(con, run_id=run_id, code=ERR_EMPTY,
                                  detail="empty page past retry budget",
                                  scope=req.as_dict())
                _store.update_run(con, run_id, status="partial",
                                  error=ERR_EMPTY)
                last_error = ERR_EMPTY
                break

            # ---- 重复页判定 ----
            known = _store.get_page(con, run_id, page_index)
            prev_known = (_store.get_page(con, run_id, page_index - 1)
                          if page_index > 0 else None)
            if is_duplicate_page(first_id=first_id, last_id=last_id,
                                 known_boundaries=[known, prev_known]):
                if boundary_attempts < self.max_attempts:
                    continue  # 同边界再试
                _store.quarantine(con, run_id=run_id, code=ERR_DUPLICATE,
                                  detail="duplicate page past retry budget",
                                  scope=req.as_dict())
                _store.update_run(con, run_id, status="partial",
                                  error=ERR_DUPLICATE)
                last_error = ERR_DUPLICATE
                break

            # 落本页边界（先落证据，再推进检查点）
            boundary = PageBoundary(
                page_index=page_index,
                entry_cursor=req.cursor,
                next_cursor=result.next_cursor,
                total=result.total, first_id=first_id, last_id=last_id,
                count=len(result.rows), request=req.as_dict())
            _store.save_page(con, run_id=run_id, page_index=page_index,
                             entry_cursor=boundary.entry_cursor,
                             next_cursor=boundary.next_cursor,
                             total=boundary.total, first_id=first_id,
                             last_id=last_id, count=len(result.rows),
                             boundary=boundary.as_dict())

            # ---- 漂移检测：本页 id 是否与已确认集合重叠 ----
            new_rows = []
            overlap = 0
            for row in result.rows:
                rid = str(row.get("id"))
                if _store.seen_add(con, run_id, rid):
                    new_rows.append(row)
                else:
                    overlap += 1

            for row in new_rows:
                stamped = stamp_pagination_context(
                    row, query_version=config.version, run_id=run_id,
                    page_index=page_index, mode=mode)
                self._deliver(stamped)

            # 推进检查点
            update = {"next_page": page_index + 1,
                      "seen_total": _store.seen_count(con, run_id)}
            if mode == MODE_CURSOR:
                update["last_cursor"] = req.cursor or ""
                update["next_cursor"] = result.next_cursor or ""
            if result.total is not None:
                update["total_hint"] = result.total
            _store.update_run(con, run_id, **update)
            self._boundary_tries.pop(bkey, None)  # 边界已确认，预算复位
            con.commit()

            # ---- 漂移恢复优先：重叠说明结果集在抓取期间被插入/重排 ----
            if overlap:
                # 当前页已确认；锚点为下一未确认页，回退从当前页开始
                realigned, err = self._realign(config, run_id, mode,
                                                page_size, page_index + 1)
                if not realigned:
                    _store.quarantine(
                        con, run_id=run_id, code=err or ERR_STALE_CURSOR,
                        detail=f"realignment failed after fallback_window="
                               f"{self.fallback_window}",
                        scope={"from_page": page_index})
                    _store.update_run(con, run_id, status="partial",
                                      error=err or ERR_STALE_CURSOR)
                    last_error = err or ERR_STALE_CURSOR
                    break
                # realign 内部已推进检查点，回到 while 顶部继续
                continue

            # ---- 正常耗尽（漂移已先行处理）----
            exhausted = (result.total is not None
                         and _store.seen_count(con, run_id) >= result.total)
            if not result.rows:
                exhausted = True
            if mode == MODE_CURSOR and not result.next_cursor:
                exhausted = True
            if mode == MODE_PAGE and result.total is not None and \
                    (page_index + 1) * page_size >= result.total:
                # total 可能在抓取期间增长：终止前强制探测下一页，
                # 探测到数据说明结果集扩张，按漂移路径回退重采而非停止。
                probe_req = PageRequest(
                    config.version, mode, page=page_index + 1,
                    cursor=None, page_size=page_size)
                probe, pattempts, pcode = self._fetch_one(run_id, probe_req)
                requests_made += pattempts
                if probe is not None and probe.rows:
                    pids = [str(r.get("id")) for r in probe.rows]
                    _store.save_page(
                        con, run_id=run_id, page_index=page_index + 1,
                        entry_cursor=None, next_cursor=probe.next_cursor,
                        total=probe.total, first_id=pids[0],
                        last_id=pids[-1], count=len(pids),
                        boundary=PageBoundary(
                            page_index=page_index + 1, entry_cursor=None,
                            next_cursor=probe.next_cursor, total=probe.total,
                            first_id=pids[0], last_id=pids[-1],
                            count=len(pids),
                            request=probe_req.as_dict()).as_dict())
                    for row in probe.rows:
                        if _store.seen_add(con, run_id, str(row.get("id"))):
                            self._deliver(stamp_pagination_context(
                                row, query_version=config.version,
                                run_id=run_id, page_index=page_index + 1,
                                mode=mode))
                    _store.update_run(
                        con, run_id, next_page=page_index + 2,
                        seen_total=_store.seen_count(con, run_id),
                        total_hint=probe.total)
                    con.commit()
                    realigned, rerr = self._realign(
                        config, run_id, mode, page_size, page_index + 2)
                    if not realigned:
                        _store.quarantine(
                            con, run_id=run_id,
                            code=rerr or ERR_STALE_CURSOR,
                            detail="post-exhaustion probe found drift",
                            scope={"from_page": page_index + 1})
                        _store.update_run(con, run_id, status="partial",
                                          error=rerr or ERR_STALE_CURSOR)
                        last_error = rerr or ERR_STALE_CURSOR
                        break
                    continue
                # 探测为空或失败：空则确认耗尽；失败则隔离待补
                if probe is None:
                    _store.quarantine(con, run_id=run_id, code=pcode,
                                      detail="exhaustion probe failed",
                                      scope=probe_req.as_dict())
                    _store.update_run(con, run_id, status="partial",
                                      error=pcode)
                    last_error = pcode
                    break
                exhausted = True
            if exhausted:
                _store.update_run(con, run_id, status="done",
                                  total_hint=result.total)
                con.commit()
                break

        run = _store.get_run(con, run_id)
        return CrawlSummary(
            run_id=run_id, query_version=config.version,
            status=run["status"], mode=mode, requests=requests_made,
            pages_confirmed=_store.max_page_index(con, run_id) + 1,
            unique_rows=_store.seen_count(con, run_id),
            written=len(self._delivered_keys),
            quarantine=_store.list_quarantine(con, run_id),
            error=last_error)

    # -- 限速/瞬时错误重试 --

    def _fetch_one(self, run_id, req: PageRequest):
        """单页抓取，统一把游标失效翻译成可回退的错误码。

        返回 (result, attempts, error_code)；error_code 为
        ``stale_cursor`` 时调用方应依据已确认覆盖边界决定回退或隔离。
        """
        try:
            return self._request_with_retry(run_id, req)
        except StaleCursor:
            return None, 1, ERR_STALE_CURSOR

    def _request_with_retry(self, run_id, req: PageRequest):
        """返回 (PageResult|None, 实际尝试次数, 错误码|None)。"""
        attempts = 0
        last_code = ERR_FETCH
        while attempts < self.max_attempts:
            attempts += 1
            req.attempt = attempts
            seq = _store.next_seq(self.con, run_id)
            try:
                result = self._fetcher(req)
            except RateLimited as rl:
                _store.append_request(
                    self.con, run_id=run_id, seq=seq,
                    request=req.as_dict(), http_status=429,
                    error_code=ERR_RATE_LIMIT)
                last_code = ERR_RATE_LIMIT
                if attempts < self.max_attempts:
                    self._sleep(rl.retry_after or
                                self.backoff_base * (2 ** (attempts - 1)))
                    continue
                return None, attempts, last_code
            except StaleCursor:
                _store.append_request(
                    self.con, run_id=run_id, seq=seq,
                    request=req.as_dict(), error_code=ERR_STALE_CURSOR)
                raise
            except FatalFetchError as exc:
                # 致命错误：审计后直接冒泡，不消耗重试预算
                _store.append_request(
                    self.con, run_id=run_id, seq=seq,
                    request=req.as_dict(),
                    error_code=f"fatal:{type(exc).__name__}")
                raise
            except Exception as exc:  # 其它瞬时/传输错误
                _store.append_request(
                    self.con, run_id=run_id, seq=seq,
                    request=req.as_dict(),
                    error_code=f"{ERR_FETCH}:{type(exc).__name__}")
                last_code = ERR_FETCH
                if attempts < self.max_attempts:
                    self._sleep(self.backoff_base * (2 ** (attempts - 1)))
                    continue
                return None, attempts, last_code
            else:
                _store.append_request(
                    self.con, run_id=run_id, seq=seq,
                    request=req.as_dict(), http_status=result.http_status,
                    response_meta={"total": result.total,
                                   "next_cursor": result.next_cursor,
                                   "query_version": result.query_version,
                                   "count": len(result.rows)})
                return result, attempts, None
        return None, attempts, last_code

    # -- 漂移/游标失效后的回退重采 --

    def _realign(self, config: QueryConfig, run_id: str, mode: str,
                 page_size: int, anchor: int) -> tuple[bool, Optional[str]]:
        """回退重采，把 [anchor-? .. anchor] 区间重新对齐到当前结果集。

        ``anchor`` 是第一个未确认页。回退从 ``anchor-1``（最后确认页）
        开始，最多回退 ``fallback_window`` 步；只依赖已确认边界（页码或
        该页保存的 entry_cursor），不信赖导致漂移的易失游标。

        - 页码模式：结果集插入/重排后，回退页会出现新 id；以“抓到新 id”
          作为对齐信号，再顺序重采到 anchor。
        - 游标模式：游标 epoch 重置后从根游标重采的是旧数据，新 id 不适用
          作信号；因此沿根游标链顺序重走，直到重新确认 anchor，去重账本
          保证重走不重复写入。
        """
        con = self.con
        if mode == MODE_CURSOR:
            return self._realign_cursor(config, run_id, page_size, anchor)

        for backtracks in range(1, self.fallback_window + 1):
            target = anchor - backtracks
            if target < 0:
                return False, ERR_STALE_CURSOR
            req = PageRequest(config.version, mode, page=target,
                              cursor=None, page_size=page_size)
            result, _, err = self._fetch_one(run_id, req)
            if result is None:
                return False, err
            if result.query_version and result.query_version != config.version:
                return False, ERR_VERSION
            new_rows = [r for r in result.rows
                        if _store.seen_add(con, run_id, str(r.get("id")))]
            if not new_rows:
                continue  # 该边界无新内容，漂移点在更早处

            # 追上当前结果集：重写该页边界并顺序重采到 anchor
            ids = [str(r.get("id")) for r in result.rows]
            _store.save_page(
                con, run_id=run_id, page_index=target,
                entry_cursor=req.cursor, next_cursor=result.next_cursor,
                total=result.total, first_id=ids[0], last_id=ids[-1],
                count=len(ids),
                boundary=PageBoundary(
                    page_index=target, entry_cursor=req.cursor,
                    next_cursor=result.next_cursor, total=result.total,
                    first_id=ids[0], last_id=ids[-1], count=len(ids),
                    request=req.as_dict()).as_dict())
            for row in new_rows:
                stamped = stamp_pagination_context(
                    row, query_version=config.version, run_id=run_id,
                    page_index=target, mode=mode)
                self._deliver(stamped)
            _store.update_run(con, run_id, next_page=target + 1,
                              seen_total=_store.seen_count(con, run_id))
            con.commit()
            return self._reswallow(config, run_id, mode, page_size,
                                   target + 1, anchor)
        return False, ERR_STALE_CURSOR

    def _realign_cursor(self, config: QueryConfig, run_id: str,
                        page_size: int, anchor: int) -> tuple[bool, Optional[str]]:
        """游标失效：从根游标顺序重走到 anchor，重建游标链。"""
        # 回退起点不得超出已确认覆盖窗口
        start = max(0, anchor - self.fallback_window)
        ok, err = self._reswallow(config, run_id, MODE_CURSOR, page_size,
                                  start, anchor)
        return ok, err

    def _reswallow(self, config, run_id, mode, page_size, start, stop):
        """回退后把受影响区间顺序重采一遍，账本去重，不丢不重。"""
        con = self.con
        for target in range(start, stop + 1):
            if mode == MODE_PAGE:
                req = PageRequest(config.version, mode, page=target,
                                  cursor=None, page_size=page_size)
            else:
                prev = _store.get_page(con, run_id, target - 1)
                cursor = prev["next_cursor"] if prev else None
                if target > 0 and not cursor:
                    return False, ERR_STALE_CURSOR
                req = PageRequest(config.version, mode, page=None,
                                  cursor=cursor, page_size=page_size)
            result, _, err = self._fetch_one(run_id, req)
            if result is None:
                if err == ERR_STALE_CURSOR:
                    return False, ERR_STALE_CURSOR
                return False, err
            if result.query_version and result.query_version != config.version:
                return False, ERR_VERSION
            ids = [str(r.get("id")) for r in result.rows]
            if ids:
                _store.save_page(con, run_id=run_id, page_index=target,
                                 entry_cursor=req.cursor,
                                 next_cursor=result.next_cursor,
                                 total=result.total, first_id=ids[0],
                                 last_id=ids[-1], count=len(ids),
                                 boundary=PageBoundary(
                                     page_index=target, entry_cursor=req.cursor,
                                     next_cursor=result.next_cursor,
                                     total=result.total, first_id=ids[0],
                                     last_id=ids[-1], count=len(ids),
                                     request=req.as_dict()).as_dict())
            for row in result.rows:
                if _store.seen_add(con, run_id, str(row.get("id"))):
                    stamped = stamp_pagination_context(
                        row, query_version=config.version, run_id=run_id,
                        page_index=target, mode=mode)
                    self._deliver(stamped)
            _store.update_run(
                con, run_id, next_page=target + 1,
                seen_total=_store.seen_count(con, run_id),
                next_cursor=result.next_cursor or "",
                last_cursor=req.cursor or "")
            con.commit()
        return True, None

    # -- 下游交付：idmap → dedup → keyer → store --

    def _deliver(self, record):
        if self._sink is not None:
            result = self._sink(record)
            key = result[0] if result else None
        else:
            from .idmap import process as idmap_process
            from .dedup import process as dedup_process
            from .keyer import key_of
            rec = idmap_process(record)
            rec = dedup_process(rec)
            if rec is None:
                return None, None
            from .models import Record
            key = key_of(rec)
            _store.upsert(self.con, Record(key=key, data=rec))
            result = (key, rec)
        if key is not None:
            self._delivered_keys.add(key)
        return result if result else (None, None)


# ---- 便捷函数 -------------------------------------------------------------

def crawl(fetcher: Callable[[PageRequest], PageResult],
          config: QueryConfig, *, con=None, mode: str = MODE_PAGE,
          page_size: int = 10, run_id: Optional[str] = None,
          resume: bool = True, **kwargs) -> CrawlSummary:
    paginator = Paginator(fetcher, con=con, **kwargs)
    own = con is None
    try:
        return paginator.crawl(config, mode=mode, page_size=page_size,
                               run_id=run_id, resume=resume)
    finally:
        if own and paginator.con:
            try:
                paginator.con.close()
            except Exception:
                pass
