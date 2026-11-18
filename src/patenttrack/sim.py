"""离线模拟分页后端：不联网，按调用序号脚本化故障与数据漂移。

支持两种服务端形态：
- 页码分页：``page`` + ``page_size`` 切片；
- 游标分页：游标编码 ``cur-<epoch>-<offset>``，版本变更后旧 epoch 游标失效。

事件（key 为第几次调用，从 1 开始；值为动作或动作列表）：
- "rate_limit"                 抛 RateLimited（可带 retry_after）
- "empty"                      返回空页，但 total 仍是当前数据集大小
- "duplicate"                  原样重复上一次的响应体
- ("insert_head", k)           在数据集头部插入 k 条新 id
- ("insert_at", pos, k)        在 pos 位置插入 k 条新 id
- ("shrink", k)                从尾部删除 k 条（结果总数缩水）
- ("version", new_version)     切换服务端查询版本并作废旧游标
- ("stale_cursor", n)          接下来 n 次带游标请求抛 StaleCursor
- ("fail", n, exc)             接下来 n 次请求抛给定异常（模拟崩溃/断连）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .query_page import (PageRequest, PageResult, RateLimited, StaleCursor,
                         FatalFetchError)


def make_rows(n: int, prefix: str = "r") -> list[dict[str, Any]]:
    return [{"id": f"{prefix}-{i:04d}", "name": f"name-{i}", "value": i}
            for i in range(n)]


@dataclass
class SimBackend:
    rows: list[dict[str, Any]]
    events: dict[int, Any] = field(default_factory=dict)
    serve_version: Optional[str] = None
    calls: int = 0
    _epoch: int = 0
    _last_payload: Optional[dict] = None
    _stale_left: int = 0
    _fail_left: int = 0
    _fail_exc: Optional[Exception] = None
    _serial: int = 0

    # -- 事件 --

    def _apply_events(self):
        actions = self.events.get(self.calls)
        if actions is None:
            return
        if not isinstance(actions, list):
            actions = [actions]
        for action in actions:
            kind = action[0] if isinstance(action, tuple) else action
            if kind == "insert_head":
                k = action[1]
                self.rows = self._new_rows(k, 0) + self.rows
            elif kind == "insert_at":
                _, pos, k = action
                self.rows = self.rows[:pos] + self._new_rows(k, pos) + self.rows[pos:]
            elif kind == "shrink":
                del self.rows[-action[1]:]
            elif kind == "version":
                self.serve_version = action[1]
                self._epoch += 1  # 旧游标全部失效
            elif kind == "stale_cursor":
                self._stale_left = action[1]
            elif kind == "fail":
                self._fail_left = action[1]
                self._fail_exc = action[2]

    def _new_rows(self, k, pos):
        out = []
        for i in range(k):
            self._serial += 1
            out.append({"id": f"ins-{self.calls}-{self._serial}",
                        "name": f"inserted-{pos}-{i}", "value": -1})
        return out

    # -- 游标编解码 --

    def _encode_cursor(self, offset: int) -> str:
        return f"cur-{self._epoch}-{offset}"

    def _decode_cursor(self, cursor: str) -> int:
        parts = cursor.split("-")
        if len(parts) != 3 or parts[0] != "cur":
            raise StaleCursor(f"unparseable cursor: {cursor!r}")
        epoch, offset = int(parts[1]), int(parts[2])
        if epoch != self._epoch:
            raise StaleCursor(
                f"cursor epoch {epoch} != current {self._epoch}")
        return offset

    # -- fetcher 入口 --

    def __call__(self, req: PageRequest) -> PageResult:
        self.calls += 1
        self._apply_events()

        if self._fail_left > 0:
            self._fail_left -= 1
            exc = self._fail_exc or FatalFetchError("simulated crash")
            raise exc
        if kind := self._event_kind("rate_limit"):
            retry_after = 0.0
            action = self._raw_action("rate_limit")
            if isinstance(action, tuple) and len(action) > 1:
                retry_after = float(action[1])
            raise RateLimited(retry_after)
        if self._stale_left > 0 and req.cursor:
            self._stale_left -= 1
            raise StaleCursor("simulated stale cursor")
        if self._event_kind("empty"):
            self._last_payload = {"ids": [], "total": len(self.rows)}
            return PageResult(rows=[], total=len(self.rows),
                              next_cursor=None,
                              query_version=self.serve_version)

        size = req.page_size
        if req.mode == "page":
            offset = (req.page or 0) * size
            chunk = self.rows[offset:offset + size]
            next_cursor = None
        else:
            offset = 0 if not req.cursor else self._decode_cursor(req.cursor)
            chunk = self.rows[offset:offset + size]
            end = offset + len(chunk)
            next_cursor = self._encode_cursor(end) if end < len(self.rows) else None

        if self._event_kind("duplicate") and self._last_payload is not None:
            # 重复页：回放上次响应，边界与服务端当前状态故意不一致
            payload = self._last_payload
            rows = [dict(r) for r in payload["rows"]]
            return PageResult(rows=rows, total=payload["total"],
                              next_cursor=payload.get("next_cursor"),
                              query_version=self.serve_version)

        rows = [dict(r) for r in chunk]
        self._last_payload = {"rows": rows, "total": len(self.rows),
                              "next_cursor": next_cursor}
        return PageResult(rows=rows, total=len(self.rows),
                          next_cursor=next_cursor,
                          query_version=self.serve_version)

    # -- 小工具 --

    def reset(self, *, keep_rows: bool = True) -> None:
        """新运行复用同一后端时重置调用序号与一次性故障状态。"""
        self.calls = 0
        self._stale_left = 0
        self._fail_left = 0
        self._fail_exc = None
        if not keep_rows:
            self.rows = []
            self._serial = 0

    def _raw_action(self, kind):
        actions = self.events.get(self.calls)
        if actions is None:
            return None
        if not isinstance(actions, list):
            actions = [actions]
        for action in actions:
            k = action[0] if isinstance(action, tuple) else action
            if k == kind:
                return action
        return None

    def _event_kind(self, kind):
        return self._raw_action(kind) is not None
