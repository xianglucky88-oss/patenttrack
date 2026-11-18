# patenttrack — 按检索式翻页抓取专利/商标，编号归并、法律状态历史、同族关联。

运行栈：Python 3.11+ · 标准库 `sqlite3` · `httpx` · `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、幂等性与分页恢复测试
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`family`, `legal_status`, `query_page`, `idmap`, `dedup`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli / sim`。

## 检索式分页抓取（query_page）
`query_page.py` 在保留逐记录 `process()` 兼容接口的同时，提供完整分页引擎
`Paginator` / `crawl()`，所有决策只接受**可观察证据**（请求边界、响应首尾 id、
总数、游标、HTTP 状态、错误码），不依赖易失内存状态：

- **查询版本**：`QueryConfig` 由规范化查询 JSON 哈希出 `query_version`，随每个
  请求与每条记录传递；恢复必须命中同一版本，条件变更后旧运行（含旧游标）被拒绝，
  新查询走独立运行，避免新旧批次混杂。
- **页码 / 游标双模式**：`MODE_PAGE` 用页码切片，`MODE_CURSOR` 用不透明游标；
  同版本内可 `switch_mode()` 切换（重建进度，不复用旧游标）。
- **可复现边界**：每次请求的请求边界（版本/模式/页码/游标/页大小/尝试序号）与
  响应边界（首尾 id、总数、下一个游标、HTTP 状态、错误码）落 `qp_requests` /
  `qp_pages` 表，失败原因可离线重放复现。
- **空页**：仅当 `total` 可见且已确认覆盖达到 `total`（或空数据集）才正常停止；
  证据不足则按预算重试，预算耗尽隔离待补（`partial`），不静默截断。
- **重复页**：响应首尾 id 与已确认边界相同即判定重复，同边界重试；持续重复则隔离。
- **限速/瞬时错误**：按 `retry_after` 与指数退避重试（`sleeper` 可注入，测试零
  等待）；预算耗尽以 `partial` 结束并保留检查点。致命错误（`FatalFetchError`）
  不重试，直接冒泡，重启后从最后确认页继续。
- **总数变化 / 结果插入 / 游标失效**：检测到 id 重叠、总数缩水或旧游标不兼容时，
  回退到最后一个已确认页面边界重采（页码模式按新 id 对齐，游标模式从根游标链重
  走），回退范围受 `fallback_window` 约束；窗口内追不齐则写入 `qp_quarantine`
  隔离待补。去重账本（`qp_seen`，按 运行+id 隔离）保证重试不丢不重。
- **断点恢复**：检查点在每个确认页后提交；重开数据库后自动按 `query_version`
  找到可恢复运行并续跑。

### 下游版本传递
- `idmap.process(rec, expected_version=...)` 继承分页上下文并做版本守卫，版本
  不符抛 `QueryVersionMismatch`，防止旧游标把新查询结果混进旧批次。
- `dedup.process(rec, seen=...)` 按 `(query_version, run_id, id)` 作用域去重，
  回退重采的同 id 记录被丢弃，不同查询版本的同 id 不会被旧账本吞掉。

### 离线模拟（sim.SimBackend）
`sim.py` 提供不联网的脚本化后端，按调用序号注入事件：页码/游标切换、头部/定点
插入、总数缩水、重复页、空页、限速、查询版本切换（旧游标 epoch 作废）、游标
失效、致命失败。`tests/test_query_page.py` 覆盖重启恢复、条件变更后的覆盖范围、
写入计数与失败原因复现。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入。分页状态表：
`qp_runs`（运行/检查点）、`qp_requests`（请求审计）、`qp_pages`（确认页边界）、
`qp_seen`（去重账本）、`qp_quarantine`（隔离待补）。测试夹具位于 `tests/`，
运行方式以 `Makefile` 为准。
