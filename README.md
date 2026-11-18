# patenttrack — 按检索式翻页抓取专利/商标，编号归并、法律状态历史、同族关联。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → `query_page` → `idmap` → `family`
→ `legal_status` → `dedup` → `keyer` → 幂等落 `sqlite`。
```text
make run                       # 运行离线流程（默认 fixtures/input.json）
make run-mixed                 # 混合导入：同族、同号跨地区、公告修订、重复消息
make test                      # 运行项目测试（流水线、归并、恢复、比较成本）
make run FILE=path/to/batch.json   # 用指定批次文件运行
```
命令行也接受路径参数或 `-`（stdin）：`PYTHONPATH=src python3 -m patenttrack.cli batch.json`。

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`family`, `legal_status`, `query_page`, `idmap`, `dedup`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

`dedup` 是批量决策阶段（跨源判重本质上是批次决策），因此流水线按阶段处理整批记录，
`pipeline.py` 只负责顺序与失败隔离，不做归并判断。

## 编号与身份
- `normalizer`：解析申请号 / 公开号 / 授权号 / 注册号，统一为
  `地区:命名空间:号码`。地区是身份的一部分；公开/授权号剥离 kind code 用于比较；
  格式差异（空格、连字符、点、斜杠、全半角）不影响结果。
- `idmap`：把同一条记录的申请号与同地区公开/授权号连成“同一申请”。
  不同申请号永不互相合并，共享公开号也不能把两个申请号传递合并；
  一行多申请号视为检索列表，不参与合并。跨地区同号保持分离并记录冲突证据。
  无任何编号时回退到记录 id，再回退到内容指纹。
- `family`：同族/优先权是**关系证据**，不是身份证据。共享同族键的不同申请保持独立，
  并作为“被拒绝的合并候选”留证。
- `legal_status`：一条申请可有多条事件（公开、授权、公告修订……）。事件身份由
  申请身份 + 规范化事件内容决定，与来源消息顺序、记录 id、公告号写法无关。
- `dedup`：按申请分组、按事件版本推进，输出：
  - `duplicate`：同一事件被多条消息观测，只落一行，其余指向 canonical；
  - `follow_up`：同一申请的后续事件（授权/重新公布），单独成行；
  - `amendment`：公告修订独立成行，并以 `supersedes` 指向前一事件；
  - `independent`：独立申请（含同族另一份申请）。
  相似名称但编号冲突的候选**不合并**，双方编号与名称相似度写入证据。

## 持久化与证据
- `records`：每个稳定键一行；键为 `申请身份#事件id`（无编号输入保持原 `id` 键）。
- `evidence`：按 `(application_key, kind, subject_key)` 幂等写入，含 decision、
  duplicate、follow_up、amendment、rejected_candidate、cross_region_conflict、
  application 等类型，合并结果始终可追溯被拒绝的候选。

重复消息、乱序到达与部分失败恢复都依赖上述幂等键：中途提交 `commit_every` 批次后，
复跑完整输入会收敛到相同行数与相同证据。

## 比较成本
名称冲突候选使用分块：完全同名按组线性汇总（每个名称一条证据，成员数精确），
模糊相似走前缀/后缀/词元分块且块大小有上限，超出上限的常见名块跳过并计数。
`dedup` 结果中的 `comparisons`（`pairwise / candidates / blocks / skipped_blocks`）
可直接观察大候选批次的实际比较成本。

## 测试
测试夹具位于 `fixtures/`（`input.json` 为既有兼容输入，`mixed.json` 为混合导入批次）；
`make test` 覆盖乱序、重复消息、部分失败恢复、同族分离、跨地区同号、公告修订与比较成本。
