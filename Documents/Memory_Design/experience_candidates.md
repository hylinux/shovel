# P3：限定来源的待审经验候选

2026-11-10 实现的 P3 试点，是**人工发起、有来源和结果核验记录的候选收集**，
不是程序性学习、Memory 激活或新的执行引擎。专题状态见 [总览](README.md)；
完整路线见 [第四步以后](memory_completion_roadmap.md#6-第四步试点与后续第-56-步)。

## 1. 来源和核验边界

当前只接受当前 Platform Workspace/User 所有的、人工发起的 root Execution：
其状态必须为 `succeeded`，冻结 Workflow 仅含 Input、Condition、Output，
没有连接要求、required permissions、依赖或外部副作用。
Function、Skill/Agent、子调用、自动触发、未完成/failed/cancelled Execution、
通知/事件/审批等节点及企业绑定均不在试点来源内。
不导入任意测试报告、业务回执、工具输出或模型自述。

采集者提供问题、环境、适用条件及预期输出字段。服务端从**持久 Execution.output**
逐项读取并比较，而不是接受 `verified=true`、`passed=true` 或模型评分。
核验版本为 `workflow-output-checks-v1`：

- 至少一项、最多八项检查；路径为 1–16 个字典键，不解释表达式或运行脚本。
- 保存 expected、observed、exists、passed；缺失路径与真实 JSON null 区分。
- 以规范 JSON 比较，保留类型差异，包括嵌套的 `true` 与 `1`；
  对象键顺序不影响结果。
- 全部匹配则 `observed_outcome=passed`，否则为 `failed`，
  `failure_reason=output_assertion_mismatch`。不猜测根因。
- Execution 成功但检查失败仍产生**失败观察候选**，不能写成业务成功。
- 问题、环境、适用条件是人工备注，标记 `annotations_source=human_unverified`。
  期望值同样由人提供；相等只证明记录匹配，不证明业务正确、适用条件充分或结果独立可信。

来源保留 Execution ID/revision、Capability ID/version/content hash、
完整 Execution 的规范摘要及核验器版本。尝试信息保存冻结节点类型、
检查点中的已完成节点 ID 和 Execution 尝试次数；不重新运行 Workflow，
不复制完整 input/output，不调用提取器、聊天模型或 embedder。
这也不是对旧已发布 Capability 的改写。

## 2. 状态、所有权和持久化

`memory_experience` 是 SQLite 候选/审核回执表，不继承 NativeMemory，
不进入 Memory FTS、Profile、Zvec、Working 或主 Agent 上下文：

| 状态 | 含义 |
|---|---|
| `candidate` | 已采集，待人工审核 |
| `reviewed` | 人工选择保留观察，**仍不激活、不可召回** |
| `rejected` | 人工拒绝，保留墓碑，不能再次保留 |
| `deleted` | 显式遗忘，清除候选内容和审核备注，保留最小身份/摘要墓碑 |
| `invalidated` | 读取投影：Memory generation 或来源已变化/不可用；不能保留为 reviewed |

`reviewed` 可再拒绝；任意未删除状态都可遗忘。审核/遗忘要求已观察到的修订，
不能盲目覆盖。保留和拒绝保存结构化 decision、人工备注、reviewer ID 与 UTC Unix 秒。
这些审核不等于 Execution Approval，不提供执行、模型出站或企业数据使用授权。
保留不会创建 Procedural Memory、Skill/Workflow Draft，不改变 instructions。

唯一采集身份是 `(workspace_id, platform_user_id, source_execution_id)`：
同源、同备注/检查和同一冻结来源重试返回原记录；改变参数冲突。
拒绝、遗忘或失效后不能通过重发采集复活，不能借新 request key 建立第二份候选。
需要不同观察时须产生新的来源 Execution；试点不支持编辑已采集证据。
接口显式拒绝 `request_key`/`Idempotency-Key`，不要把自然来源身份与通用请求键混用。
审核/遗忘丢失响应后先读取并比较当前修订，不能用新修订盲目重放旧动作。

采集先读取当前 Memory generation；在 `sqlite_write` 的 `BEGIN IMMEDIATE` 内
再次核对来源摘要、所有权及 generation，并原子提交候选。
保留审核也在同一写事务里重验来源及 generation；失效记录仍允许拒绝/遗忘。
来源可被既有历史清理移除，候选不通过外键永久钉住 Execution；
来源不在时明确显示 `source_unavailable`，不能把缓存证据当成当前核验。

Platform 默认用户 `owner` 与原生 Memory 的既有 `local-user` 是不同命名空间。
Runtime 显式绑定当前 Platform scope 和当前 Memory owner 的 generation，
不改写旧 Memory 所有权；该仓库绑定不能用于其他 Workspace/User。
候选归属仍是 Platform Workspace/User，不自动扩展为跨用户或子 Agent 记忆。

迁移 25 `reviewed_experience_candidates` 是新增表，兼容版本 24 及更老库，
重复升级不回填、复制或激活旧历史，不重写既有事实/发布版本。

## 3. API 与 CLI

复用现有 `PlatformApplication`、`/api/v1/platform/commands` 和 Generic Host。
采集/审核/遗忘都是短本地事务，返回 HTTP 200 的持久候选快照，
不伪造后台 Operation，也不调用第二个 Runtime。

| action | 关键参数 |
|---|---|
| `experience.collect` | `execution_id`、来源 `expected_revision`、`input` 提交材料 |
| `experience.list` | 返回 `{"items": [...]}`，最新 100 条 |
| `experience.show` | 候选 `identifier` |
| `experience.review` | 候选 `identifier`、`expected_revision`、`input: {"decision":"retain 或 reject","note":"人工备注"}` |
| `experience.forget` | 候选 `identifier`、`expected_revision` |

读取也提供 `GET /api/v1/platform/experiences` 和
`GET /api/v1/platform/experiences/{id}`。所有动作都要求显式 human 通道；
Main Agent 不暴露采集/审核工具，普通聊天不是同意，`human=false` 一律拒绝。
当前没有专用前端面板，使用 API/CLI 管理，普通 Memory 页面不显示这些候选。

提交文件示例，假设选定来源 output 中确实有 `result` 字段：

```json
{
  "problem": "Check the local workflow outcome.",
  "environment": ["Local deterministic workflow without external effects."],
  "applicability": ["Only this frozen workflow and the asserted result field."],
  "checks": [{"path": ["result"], "expected": "sorted"}]
}
```

```powershell
# 先用 execution show 读取实际来源 ID、revision 和冻结版本，不猜测修订。
uv run shovel execution show <execution-id>
uv run shovel experience collect <execution-id> .\experience.json --revision <source-revision>
uv run shovel experience list
uv run shovel experience show <candidate-id>
uv run shovel experience review <candidate-id> --revision 1 --decision retain --note "Recorded output checked."
uv run shovel experience forget <candidate-id> --revision 2
```

problem/单条环境/适用条件/审核备注各最多 500 字符，环境与适用条件各 1–8 条；
提交总量最多 12000 字符，所选证据总量最多 16000 字符。
每个所有者最多 1000 条非 deleted 候选，超出需先遗忘；
当前无自动过期或自动清理，墓碑不因达到上限被复用。

## 4. 隐私、验证与未完成项

Memory/PERSISTENCE/DISPLAY 复用 `require_ordinary`，新表纳入已有数据库个人写入围栏。
凭据字段及保守秘密模式会拒绝；受保护引用、企业 flow 或企业绑定不能降级为普通经验。
开发日志仅含固定事件、候选 ID 与状态，不含输入/结果/备注、路径或凭据。
这些防线不是完整敏感信息分类、加密存储或物理擦除证明。
遗忘只删除候选副本，不删除原 Execution，也不承诺 WAL、备份和磁盘介质已经清除。

`backend\tests\test_memory_experience.py` 使用合成材料和隔离存储，
通过真实发布/Invocation/ExecutionEngine、REST/CLI、迁移和完整 Runtime 重开验证。
覆盖通过/失败观察、Condition 结果、类型/缺失差异、非合格来源、
人工/所有权边界、并发 generation/来源变化、审核修订、墓碑重放、
不进入召回/Outbox、凭据/企业 flow 拒绝及开发日志隐私。
本轮逻辑是确定性结果检查，**无需真实模型，也未读取个人模型配置或调用外部服务**。

本轮 P3/迁移/Platform/CLI/Host/Memory tasks/Execution 组合回归 **239 passed**；
独立 Memory/生命周期/企业出口回归 **120 passed、3 live skipped**，
跳过的是 P2 的真实模型用例，不把历史模型分数冒充 P3 效果。
另有两个企业旧库迁移回归通过，验证已发布哈希和历史回执不变；
三组选集共 **361 passed、3 live skipped**，不是全量 pytest。
全库 Ruff lint、改动 Python 文件格式及后端/打包脚本 mypy（245 个源文件）通过，
锁文件和已安装依赖兼容性通过。
全库 `ruff format --check .` 仍报告既有 Markdown Python 代码块的 CRLF 格式差异，
已用 HEAD 原文和 Windows checkout 换行复现，不为本任务批量改写无关文档。

```powershell
uv run --locked pytest backend\tests\test_memory_experience.py backend\tests\test_capability_migrations.py --no-cov
```

外部测试/业务回执的独立签名与专用核验器、failed Execution 的失败证据适配器、
主动收集、模型生成摘要、可激活的方法、使用反馈/回滚、真实任务收益和生产验收
仍未实现。扩展来源必须单独定义可信核验及授权，不能扩大本试点白名单、
把一个 JSON 字段当通用正确性证明，或放宽用户自述提取的来源规则。
