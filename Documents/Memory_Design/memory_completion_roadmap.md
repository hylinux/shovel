# Native Memory 完整化路线与第一阶段落地方案

当前状态与专题导航见 [Memory 设计与进度总览](README.md)；
本文保留分阶段方案和各轮历史验证，不把待开发阶段视为已完成。

## 1. 目标与范围

“完整 Memory”指可解释、可管理、可恢复的记忆闭环，不是无限保存聊天、
模型回答、文档和工具输出。闭环为：

获取 → 候选验证 → 持久化 → 召回 → 使用 → 反馈 → 纠正、过期或遗忘。

沿用 SQLite 权威存储、可重建 Zvec 索引、Generic Host、Memory 后台
Operation 和 MAF 主 Agent。不能引入平行的任务执行器、业务状态存储或
自动修改 Agent instructions 的旁路。

本文区分 **当前代码落地的第 1–3 步** 与 **后续第 4–6 步的建议**。
后续建议不是已实现能力，离线测试也不代表模型效果或企业生产验收。

## 2. 分类与来源边界

| 层 | 内容 | 生命周期与权威 |
|---|---|---|
| Semantic / 事实 | 用户明确自述的稳定偏好、约束、属性和决策 | SQLite 中有证据、Scope、状态的长期记录 |
| Episodic / 经历 | 重要事件及结果 | 明确区分事件日期和记录时间；结果不能靠模型自证 |
| Procedural / 方法 | 带适用条件的方法、检查步骤和禁用条件 | 当前仍为显式维护的建议，不提供执行权限 |
| Working / 工作 | 当前任务的目标、约束、决策、计划、阻塞项和下一步 | Session + task 显式隔离，有修订和有效期 |

“经验记忆”由经历和经过验证的方法组成，不只是保存一条事件摘要。
History 保留原始消息，Summary 提供会话压缩，两者不是长期事实；
RAG 文档仍属于知识检索，不能因为进入上下文就成为用户自述。

来源与可信程度是独立维度。`confidence` 是候选提取评分，不是事实正确率，
也不是经过人工或业务结果核验的证明。用户批准候选只表示允许激活该记忆，
不会增加执行权限。

## 3. 第一步：治理与验收基线

### 3.1 当前落地

- Memory 列表与详情提供类型、状态、Scope、结构化字段、来源、原文证据、
  提取评分、重要性、有效期和事件时间来源。
- 管理界面支持显式创建、更正、查看历史、批准候选、拒绝和忘记。
- 更正保留旧记录并创建替代记录；不能直接改变类型或 Scope。
- `[memory].review_required = true` 时，自动提取的有效候选先进入
  `candidate`，不进入主 Agent 的长期召回或向量索引。
- 该配置默认 `false`，保留原有自动提取并激活的行为。严格控制部署应显式
  开启，并在加载配置后重启 Host；不是通过普通聊天确认候选。
- 批准只接受未过期候选，重新检查原文证据、来源消息、Epoch 和等价墓碑。
  待审的明确纠正只在批准事务里替代旧事实，不能提前覆盖。
- 拒绝后保留 `rejected` 墓碑，阻止同 Scope 的相同事实自动重新出现。
  这不是对所有自然语言改写的完美语义遗忘。
- 创建、更正、审核、重建返回 HTTP 202 Operation。使用同一
  `Idempotency-Key` 重试必须返回原回执；即使 Epoch 后来改变，也不能
  把旧请求重新绑定到新一代任务。

### 3.2 API

| 方法与路径 | 用途 |
|---|---|
| `GET /api/v1/memories?include_history=true&conversation_id=...` | 查看历史及当前 Session 可见记录 |
| `GET /api/v1/memories/{id}?conversation_id=...` | 详情与原文证据 |
| `POST /api/v1/memories` | 显式创建，返回 Operation |
| `PUT /api/v1/memories/{id}` | 同类型、同 Scope 的更正，返回 Operation |
| `POST /api/v1/memories/{id}/review` | `decision: approve/reject` 与 `conversation_id`，返回 Operation |
| `DELETE /api/v1/memories/{id}?conversation_id=...` | 沿用遗忘围栏和索引删除流程 |

人工审核不新增一套确认票据系统，也不让 Main Agent 代替用户操作审核入口。
Memory 内容仍以参考数据进入上下文，不能当作系统指令。

### 3.3 验收要求

固定离线用例覆盖：候选未批准不可召回；纠正批准前后状态正确；拒绝后重放
不会恢复；源消息删除或 Epoch 改变后旧候选不能激活；同键不同参数冲突；
不同 Workspace/User/Agent/Session 不泄漏。

不要把健康、身份、凭据等广泛敏感性判断承诺为已经完备。当前凭据过滤是
保守防线，不是内容分类器、加密存储或多租户授权系统。

## 4. 第二步：提取、时间与混合召回

### 4.1 当前落地

1. 沿用只读取当前用户明确自述、精确原文证据、置信度门槛和凭据/引用过滤。
   自动提取仍只产生 Semantic/Episodic。
2. 自动事件候选可提供 `occurred_on`，仅接受证据里明确出现的
   `YYYY-MM-DD`。验证真实日历日期，转换为 UTC 当日零点的 Unix 秒。
   它表示日期精度，不表示事件确实发生于零点。
   API 携带 `episode_time_precision=day`；显式时间戳为 `instant`，
   未知事件时间为 `unknown`。界面按 UTC 日期显示 day，不能补出具体钟点。
3. “今天”“上个月”等相对或含糊日期不自动猜测。新事件没有明确日期时，
   API 的 `occurred_at` 为 `null`、`episode_time_source` 为 `recorded`；
   `created_at` 仍为记录时间。
4. 老事件保留原有字节与时间值，并标注 `legacy_unspecified`；
   不能把旧默认记录时间当作已经核验的事件时间。
5. 相同事件文本、不同明确日期可以保留为不同事件，不因摘要相同被合并。
   现有无日期记录的 fingerprint 不批量改写。
6. 模型提出 `merge_with` 不足以证明等价：程序再次要求相同主体、谓词、
   标准化对象和 topic。对象或 topic 不同就忽略合并并记录诊断。
   这会保留部分近义重复，优先避免错误合并。
   新的自动提取记录来源版本为 `native-v2`，不重写旧任务和来源记录。
7. 使用独立 `memory_search` FTS5 trigram 索引，以事务内触发器维护。
   与知识库的 `chunk_search` 分开；短于三字符的词采用有界、可见性过滤的
   SQLite 查询。ASCII 词/标识符在条数限制前检查混排兼容的词边界，
   中文保留字面匹配；FTS 仍为 OR，不靠通用动词或子串命中证明答案。
8. 查询命中的当前 Profile 与其他事实按关键词/向量 RRF 优先选择，
   通用 Profile 只补充剩余预算；重要性、提取评分等仅作次级排序。
   模型上下文采用共享紧凑投影，保留语义三元组、时间限定和 ID/revision
   追溯，完整来源留在 SQLite/管理 API。Memory 关键词过滤英语问句功能词，
   不改知识库索引。向量命中仍必须重验 SQLite 类型、修订、模型版本、
   Scope、状态与有效期；历史 Profile 不能替代当前属性。
9. 融合后按现有 topic、正文、明确别名检查实体、关系和个人/团队条件，
   区分支持、背景、相关不足、冲突和未知。只允许支持事实与背景 Profile
   进入上下文；最终预算后无支持事实时显式标注，允许回答未找到记录。
   不改变权限 Scope、存储身份或来源；旧不明事件时间不能提供日期证据。
   这是有界规则，不是通用自然语言蕴含证明。
10. 未配置 embedding 或向量故障时，保留 Profile + 关键词召回，同时明确
   报告语义索引不可用，不伪装为完整语义召回。

### 4.2 迁移和维护

有序迁移增加审核元数据、Working 所有权与修订字段、Memory FTS 表及触发器，
并为现有 active 长期记录回填关键词索引。重复执行升级不能复制索引条目，
不能改变旧事实 ID 或已发布 Capability 的哈希。

删除、过期、拒绝、替代使记录退出关键词召回。Zvec 删除仍走原有 Outbox，
成功 flush 后才确认；不能直接绕过现有索引身份和 Epoch。
显式创建/更正可携带 `time_precision=day` 保留日期精度，要求 UTC 零点；
默认 `instant` 不加入旧命令的规范化载荷，避免新增默认字段改变旧请求指纹。

### 4.3 验收要求与限制

测试中英文关键词、短词、SQL/FTS 特殊字符、旧记录回填、过期/拒绝/删除、
向量旧版本和故障降级。用真实 MAF 输出格式的固定响应验证日期证据，不调用
付费模型。

当前关键词分词不是通用中文语义分词，日期解析不是任意自然语言时间推理，
合并规则也不是完美同义词识别。应通过用户纠正和评估逐步扩展，而不是
放宽来源过滤或把不确定结果默认为真。

[真实模型基线](memory_baseline.md) 另有冻结事实/元数据/native 候选的
18 问配对回答评估：本轮正例上下文召回与最终回答均为 100%，负例正确
拒答保持 100%，非 Profile 上下文污染由 66.67% 降至 0%。
这不是幻觉率下降或生产保证，不能替代真实提取基线、自由文本回答评测
及更大独立语料；目前保留 `min_similarity=0.55`。

## 5. 第三步：结构化工作记忆

### 5.1 数据与更新规则

复用 `MemoryWorking`，新增明确的 Workspace/User/task 所有权和修订。
旧的无所有权占位槽位不自动暴露或迁移成可信上下文。

每个 Session 可以有多个任务；每个任务保存一份有界结构化快照：

| 字段 | 限制与含义 |
|---|---|
| `goal` | 1–1000 字符，用户明确维护的任务目标 |
| `constraints`, `decisions`, `plan`, `blockers` | 每组最多 20 条，每条 1–500 字符 |
| `next_step` | 最多 1000 字符 |
| `evidence_message_ids` | 最多 20 个；必须属于当前 Session 且消息已完成持久化 |
| `state` | 用户维护的 `active/completed/archived`，不是 Execution 状态 |
| `expires_at` | Unix 秒；省略或 null 使用配置 TTL |
| `revision` | 创建后为 1，编辑及成功聊天绑定回执后递增 |

序列化输入不超过 12000 字符。默认有效期七天，每 Session 最多保留 100 个
未删除任务。用户可以新建任务、切换、编辑、归档、完成或删除。
过期任务可检查和删除，但不能用旧 ID 复活。

### 5.2 API 与并发

- `GET /api/v1/conversations/{session}/working-memory` 返回 `items`，
  包括过期任务，后者 `state = expired`。
- `PUT /api/v1/conversations/{session}/working-memory/{task_id}` 提交快照。
  `task_id` 为 1–200 位字母数字及 `._-`，例如 UUID。
  创建要求 `expected_revision = 0`；更新要求已观察到的修订。
- `DELETE` 同路径使用 `?revision=<已观察到的修订>`。
- PUT/DELETE 在短 SQLite 写事务内完成，不排入模型任务队列。
  冲突返回 409，不能盲目覆盖。
- 丢失响应后读取当前快照比较；前端保留编辑草稿，显示不确定结果或冲突，
  不自动生成新 task ID 重发。
- 与聊天、Session 删除复用同一 Session 锁。生成回答期间拒绝修改，
  避免回答使用的目标和最终编辑内容错位。

### 5.3 主 Agent 接入与恢复

普通消息 API 与 AG-UI `forwardedProps` 都接受可选 `working_task_id`。
只有用户选中的 active、未过期任务才注入当前主 Agent，且该请求必须开启
Memory。不自动选择其他 Session 的任务，也不把任务笔记传播给子 Agent。

目标、约束、决策、阻塞项和下一步优先保留。关键字段超过 Working 或本轮
模型预算时明确拒绝运行，不能静默丢失目标。可选计划/证据引用放不下时，
上下文明确携带 `omitted_fields`，完整快照仍保留在 SQLite。

成功回答与最后用户/助手消息引用在同一事务内持久化。失败或断开的模型请求
不会推进任务回执；成功聊天不自动把 task 标记 completed。
Epoch 改变时旧聊天不会更新工作回执。

重启后从 SQLite 恢复目标与用户维护的计划，而不是依赖进程内 AgentSession。
消息引用证明来源可定位，不证明模型回答正确或外部操作成功。
此阶段不实现模型自动编写计划、自动确认进度或恢复外部副作用：
Execution/Workflow 引擎仍是业务任务状态、重试与检查点的唯一权威。

Session 删除清理其所有拥有的工作快照和 Session 记忆，不删除跨 Session
的 Agent 事实。Working 不进入长期向量索引，也不自动转为 Semantic。

### 5.4 配置

```toml
[memory]
review_required = true
working_context_max_chars = 3000
working_ttl_seconds = 604800
```

加载配置后重启 Host。没有 embedding 仍可管理和使用结构化 Working，
且具有关键词召回；没有聊天模型则不能生成回答。

## 6. 后续第 4–6 步：尚未启用

### 第四步：经历到经验候选

新增独立的任务结果来源通道，读取限定、可核验的 Execution 结果、测试、
业务回执和用户反馈。不得直接放宽“工具输出不是用户事实”的提取规则。

候选记录问题、环境、尝试、结果、失败原因和适用条件；失败也有学习价值。
首先只生成待审候选。任务 completed 不等于业务效果已正确，应保留核验依据。

### 第五步：验证后的程序性学习

将候选转换为适用条件、建议步骤、检查方法、禁用条件、来源和版本。
通过隔离测试或人工确认后激活，记录使用反馈并支持失效、回滚。

自由文本方法留在 Memory；确定执行的逻辑生成 Skill/Workflow Draft，
仍走现有 validate/publish/权限/审批，不自动发布，不修改冻结版本。
成功率必须结合样本量和场景解释，不能仅凭模型评分或调用次数晋升。

### 第六步：评估与受控运营

基础真实模型提取/召回基线已建立，见
[Memory 真实模型基线](memory_baseline.md)。它使用独立合成数据和两轮重复，
分别测量提取、金标准召回及端到端召回；不代表下述真实任务对照或受控运营
已经完成。

第一步就开始建立评估，后续增加真实任务开启/关闭 Memory 的对照：
召回准确性、错误合并、纠正次数、任务结果、延迟及模型成本。
长期补齐备份/恢复/删除边界、维护诊断和受控导出。

子 Agent 访问、跨用户协作、图记忆、任意双时态查询和独立 Memory Server
均为独立设计任务，不能借“完整 Memory”默认开放。企业内容的 READ、Memory、
存储、模型提交和导出授权仍分别检查。

## 7. 发布与验证

第一批交付第 1–3 步；第二批交付第 4–5 步，先人工确认，再有限自动化。
模型与真实环境效果评估另行批准，不在普通测试中调用外部服务。

从仓库根目录运行：

```powershell
uv run pytest backend\tests\test_native_memory.py backend\tests\test_memory_tasks.py backend\tests\test_working_memory.py --no-cov
uv run ruff check .
uv run ruff format --check .
uv run mypy backend\src
```

前端从 `frontend` 运行 `npm run lint`、`npm run typecheck`、
`npm test`、`npm run build`，验证中英文、草稿保留、审核回执、任务选择及
静态导出。Host/API、迁移和既有检索行为同样需要回归。

相关文档：[当前 Native Memory 架构](native_memory_architecture.md)、
[原始设计](memory_layer_design.md)、
[向量身份](db_base_memory_vector_id.md)。

## 8. 第一批离线验证记录

- Memory、后台任务、Working 和 Capability 迁移的定向测试：98 项通过。
- 前端测试：80 项通过；lint、typecheck 和最终静态生产构建通过。
- Windows 后端及三个打包/验收脚本的 mypy 检查通过；全仓 Ruff lint 通过。
- 全量后端运行 1496 项通过、5 项显式跳过；3 项旧测试由于默认 Memory 路径
  被运行中的用户 Host 持锁而失败。仅在测试进程内隔离 USERPROFILE/HOME
  后，这 3 项重测通过。未停止用户 Host，未修改生产配置或旧测试。
- 随后新增的日期精度编辑、旧载荷硬删除后重放用例已包含在上述 98 项定向
  测试中并通过。全部修改的 Python 文件和文档通过格式检查。
- 全仓格式检查仍报告未修改文件的既有格式问题，例如
  `backend\src\shovel\automation\conditions.py`；未为此批量改写无关文件。

这些验证使用固定模型响应及临时 SQLite/索引路径，不证明真实模型的经验学习
效果，也不是企业安装、存储治理或真实业务操作的生产验收。
