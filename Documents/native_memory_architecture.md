# Shovel 原生记忆系统：原理、架构与详细实现

本文依据 2026-10-29 当前工作树中的实现整理，描述已经接入运行链路的行为，
不把架构设想、数据库预留模型或模型自身的能力当作已实现功能。
“原生记忆”指 Shovel 自己实现的提取、整理、持久化、召回和生命周期管理，
不是模型权重训练，也不是把聊天记录整体保存到向量库。

当前方案不依赖 mem0、Qdrant、Redis 或独立 Memory Server。
Microsoft Agent Framework（下文简称 MAF）负责模型调用及 Agent 执行；
SQLite 是记忆的权威存储，独立的 Zvec 索引负责可重建的语义检索。

## 目录

1. [设计目标与基本理论](#1-设计目标与基本理论)
2. [四种信息的边界](#2-四种信息的边界)
3. [总体架构与模块职责](#3-总体架构与模块职责)
4. [数据模型与身份](#4-数据模型与身份)
5. [归属、Scope 与隔离](#5-归属scope-与隔离)
6. [一次完整交互的时序](#6-一次完整交互的时序)
7. [长期记忆的提取](#7-长期记忆的提取)
8. [去重、语义归并与纠正](#8-去重语义归并与纠正)
9. [生命周期与遗忘](#9-生命周期与遗忘)
10. [语义召回与排序](#10-语义召回与排序)
11. [主 Agent 的上下文装配](#11-主-agent-的上下文装配)
12. [Session Summary 的增量压缩](#12-session-summary-的增量压缩)
13. [后台任务、幂等与事务](#13-后台任务幂等与事务)
14. [Outbox 与向量最终一致性](#14-outbox-与向量最终一致性)
15. [索引版本、重建与切换](#15-索引版本重建与切换)
16. [取消、删除竞争与重启恢复](#16-取消删除竞争与重启恢复)
17. [安全、隐私与可信边界](#17-安全隐私与可信边界)
18. [配置及模型分工](#18-配置及模型分工)
19. [管理 API 与页面能力](#19-管理-api-与页面能力)
20. [按依赖顺序理解实现步骤](#20-按依赖顺序理解实现步骤)
21. [测试与验证](#21-测试与验证)
22. [排错与运维](#22-排错与运维)
23. [当前限制与尚未接入的模型](#23-当前限制与尚未接入的模型)
24. [源码阅读索引](#24-源码阅读索引)

## 1. 设计目标与基本理论

### 1.1 为什么不能只保存聊天记录

聊天记录包含问题、临时设想、引用、模型回答、过时结论和重复表达。
“历史里出现过一句话”并不能证明它是用户长期有效的事实。
如果所有消息都直接进入向量库，很难回答以下问题：

- 这是谁的事实，属于哪个工作区、Agent 或会话？
- 是用户明确说的，还是模型、文档或工具说的？
- 新说法是补充、同义表达，还是纠正旧说法？
- 用户点击忘记后，旧向量或延迟完成的提取是否会让它再次出现？
- 向量服务失败时，已经保存的事实是否仍然存在？

因此，本实现把记忆当作具有归属、证据、状态和生命周期的业务对象，
而不是只有 `text + embedding` 的记录。

### 1.2 三条核心原则

**第一，SQLite 决定事实是否存在、归谁所有以及是否可用。**
向量库只给出相似候选，不能决定权限、状态或最终返回的文本。

**第二，提取模型只提出候选，不获得修改数据库的权力。**
候选必须经过 JSON Schema、证据、置信度、来源、归属和事务检查。
模型不直接决定数据库主键、Scope 或向量删除。

**第三，正确性优先于“尽量记住”。**
对于凭据、粘贴引用、缺少原文证据、低置信度和含糊纠正，系统采取保守策略。
空候选是正常结果，不应为了生成记忆而强行记忆。

### 1.3 理论与工程实现的对应关系

| 理论概念 | 当前工程落点 | 不应误解为 |
| --- | --- | --- |
| 短期上下文 | 最近完成的消息、会话摘要、MAF 单次运行内的消息/工具上下文 | 独立的长期向量化聊天历史 |
| 语义记忆 | 用户稳定偏好、约束、选择等结构化事实 | 文档知识库或模型通用知识 |
| 情景记忆 | 重要事件或结果的摘要 | 自动完整记录所有经历 |
| 程序性记忆 | 可显式维护的 advisory 规则记录 | 自动修改 Agent instructions 或自动学会工作流 |
| 整理 / consolidation | 规范化去重、受约束的语义归并、显式纠正 supersession | 模型直接覆盖数据库 |
| 遗忘 | 软删除、过期、召回过滤、任务失效与向量清理 | 一键擦除源聊天、备份及模型服务端数据 |
| 记忆衰减 | 排序时使用记录年龄、重要性、访问次数 | 持续衰减并自动删除所有旧记录 |
| 双时间信息 | 有效期、记录/失效时间、事件时间等字段 | 已完成任意历史时点查询的双时态数据库 |

## 2. 四种信息的边界

### 2.1 Conversation History：原始对话

`conversation` 和 `chat_message` 保留用户/助手原始文本、消息状态、顺序、
引用和警告。默认给主 Agent 读取最近 20 条 `completed` 消息，
不是 20 轮问答；失败或未完成的回答不作为正常历史注入。

完整历史仍留在 SQLite。读取窗口和上下文预算可能使部分历史不进入某一轮请求。

### 2.2 Session Summary：会话连续性

`session_summary` 压缩已经离开近期消息窗口的完成消息，
保存目标、决策、未解决问题和来源归属，并记录 `through_sequence` 水位。

它是当前会话的压缩上下文，不是长期用户画像，不进入 Memory 向量索引，
也不会被当成新的用户事实再次提取。

### 2.3 Long-term Memory：长期用户事实

自动提取只读取当前用户消息，提炼跨会话有价值的断言，
保存到 `memory_belief` 或 `memory_episode`。
默认自动写入的 Scope 是当前主 Agent 的 `agent` Scope。

例如，“I prefer green tea.” 可以形成一条偏好事实；
助手回答中的推测、RAG 返回的文档和工具输出不能自动成为用户事实。

### 2.4 RAG：外部资料的证据

RAG 读取文档/邮件的 chunk 与 parent 文本，提供资料引用；
Memory 提供用户背景，二者独立检索、分别配置向量模型。
主 Agent 可以同时使用二者，但 Memory 不生成本地资料的 `[1]` 引用。

Agent instructions 又是另一类配置：它定义行为规则，不是记忆记录。
自定义 instructions 不会替换隔离提取器和摘要器的提示词。

## 3. 总体架构与模块职责

```mermaid
flowchart TD
    U[用户消息 / AG-UI 或 REST] --> C[ChatService]
    C --> H[(SQLite: History / Summary)]
    C --> CP[ShovelContextProvider]
    CP --> MR[Memory 召回]
    CP --> RAG[独立 RAG 检索]
    MR --> Z[(Memory Zvec 派生索引)]
    MR --> DB[(SQLite: 权威记忆)]
    CP --> A[MAF 主 Agent]
    A --> C
    C --> TX[事务: 回答完成 + Memory Operation 入队]
    TX --> O[(SQLite: Operation)]
    O --> W[MemoryExtractionWorker]
    W --> E[隔离的 MAF 提取器 / 摘要器]
    E --> V[校验 + Consolidation]
    V --> DB
    V --> OB[(SQLite: MemoryIndexOutbox)]
    W --> M[Index maintenance]
    OB --> M
    M --> Z
    M --> ACK[flush 后确认 Outbox]
```

### 3.1 分层

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 领域值与策略 | `backend\src\shovel\memory\domain.py` | 类型、Scope、候选、记录、指纹、来源准入及冲突判断 |
| 端口 | `backend\src\shovel\memory\ports.py` | Repository、Extractor、VectorIndex Protocol |
| 领域应用服务 | `backend\src\shovel\memory\service.py` | 候选筛选、归并约束、召回复核、排序和预算 |
| SQLite 适配器 | `backend\src\shovel\memory\sqlite_repository.py` | 事务、归属过滤、生命周期、Outbox、访问统计 |
| MAF 适配器 | `backend\src\shovel\memory\agent_framework.py` | 无工具的结构化提取、独立摘要、模型客户端生命周期 |
| Zvec 适配器 | `backend\src\shovel\memory\zvec_index.py` | embedding、过滤、确定性 ID、generation、flush |
| Runtime 门面 | `backend\src\shovel\runtime\memory.py` | 配置装配、Scope、管理、索引维护、摘要事务、降级状态 |
| 后台编排 | `backend\src\shovel\runtime\memory_tasks.py` | 持久任务、generation 围栏、执行、重试、取消 |
| Host / 对话接入 | `runtime\hosting.py`、`chat.py`、`agent.py` | Worker、对话事务、上下文和 MAF hooks |

领域服务不依赖 MAF Agent 或 Zvec 原生对象。
模型和存储实现通过端口替换；Runtime 负责把它们接到真实应用生命周期中。
MAF 客户端复用现有 provider 创建逻辑，但主 Agent 和提取器分别持有客户端，
不是复用同一个聊天会话或同一套工具。

## 4. 数据模型与身份

### 4.1 已接入的数据表

| 表 | 核心内容 | 在链路中的作用 |
| --- | --- | --- |
| `conversation` | 会话 ID、标题、更新时间、`has_short_memory` | 会话边界；标志位不是外部记忆记录 |
| `chat_message` | role、content、state、sequence、citations、warnings、`memory_operation_id` | 原始历史及提取源消息 |
| `session_summary` | 会话/归属、content、`through_sequence` | 会话压缩水位及内容 |
| `memory_belief` | subject、predicate、object、domain/topic、note | semantic 事实 |
| `memory_episode` | summary、occurred_at 等 | episodic 事件 |
| `memory_procedure` | name、trigger_pattern、instruction 等 | 手动维护的程序性规则 |
| `operation` | kind、payload、state、attempts、result、error | Memory 与其他操作共用的持久任务/回执 |
| `memory_epoch` | workspace、user、generation | 遗忘/取消后的陈旧任务写入围栏 |
| `memory_index_outbox` | memory ID、kind、revision、action、重试信息 | 尚未确认的向量变更 |
| `memory_index_state` | active/building generation、模型版本、状态 | 重建与切换元数据 |
| `chat_tool_interaction` | 工具名、状态、结果类型、run ID | 审计元数据，不是工具结果全文 |

### 4.2 一条长期记忆包含什么

`MemoryRecord` 对外统一表示不同实体表中的记录，主要字段包括：

- 身份与归属：`id`、`kind`、`scope`、`fingerprint`、`revision`。
- 内容：`text`、`subject`、`predicate`、`object`、`topic`。
- 生命周期：`state`、`expires_at`、`superseded_by`。
- 排序信息：`confidence`、`importance`、`recorded_at`、`access_count`、
  `last_accessed_at`。
- 追溯信息：`provenance`、事件的 `occurred_at`。
- 索引信息：记录关联的 `model_version`，ORM 行另保存 `vector_state`。

`provenance` 保存来源类型、源会话、源消息、原文 evidence 和提取器版本。
正常重复归并最多保留最近 20 条不同来源项；当前默认提取器版本是 `native-v1`。
这是来源追溯，不是无限增长的完整审计日志。

### 4.3 规范化指纹

`normalize()` 依次做 Unicode NFKC、大小写折叠和空白规范化。
指纹大致为：

```text
SHA256(
  kind | normalize(subject) | predicate |
  normalize(topic) | normalize(object or text)
)
```

它解决结构化同义值、大小写和格式差异导致的重复。
它不能自行判断任意自然语言的语义等价，因此还需要受约束的语义归并。

### 4.4 记忆向量 ID

```python
uuid.uuid5(SHOVEL_NAMESPACE, f"mem|{memory_kind}|{memory_id}|{rev}")
```

同一记忆、类型和 revision 的重复写入使用同一个向量 ID，
使重试可以幂等执行。Memory ID 与文档 `chunk_vector_id` 不混用。

当前管理更新采用“新记录替代旧记录”，不是每次访问、追加来源都递增 revision。
revision 参与身份与复核，但不能据此推断系统维护了每次属性修改的完整版本日志。
详见 [Memory 向量身份说明](db_base_memory_vector_id.md)。

## 5. 归属、Scope 与隔离

### 5.1 Runtime 生成归属

当前默认归属是：

```text
workspace_id = SHA256(str(resolved_workspace_path))[:24]
user_id      = "local-user"
agent_id     = "shovel"
scope_kind   = "agent"
```

归属由 Host 配置生成，提取模型不能返回任意 workspace/user/agent 身份。
工作区路径参与身份，所以移动或改变工作区路径不是透明的记忆迁移操作。

### 5.2 可见性规则

所有读取首先要求 workspace 和 user 一致，再应用下表：

| 记录 Scope | 同一 workspace/user 下的可见范围 |
| --- | --- |
| `user` | 当前实现中可跨 Agent、跨会话读取 |
| `workspace` | 可跨 Agent、跨会话读取 |
| `agent` | agent_id 相同，可跨会话读取 |
| `session` | agent_id 和非空 session_id 都相同 |

`user` Scope 仍不能穿透 workspace 分区，并不等于任意工作区共享的全局用户记忆。
管理 session 记录时，API 必须给出对应会话上下文。

SQLite 查询和 Zvec 过滤均执行隔离；向量命中后还会再次读取 SQLite。
这属于本机单所有者应用内部的数据隔离，不是多租户身份认证或授权服务。

### 5.3 向量目录分区

```text
memory.index_path
  \<SHA256(workspace_id + "|" + user_id)[:24]>
    \<active_generation>
    \<building_generation>
```

Memory 与知识库使用不同目录、schema、配置和索引实例。
Runtime 的 OS 锁同时保护数据库、知识向量目录和 Memory 向量目录，
避免两个写入 Host 同时使用同一存储身份。

## 6. 一次完整交互的时序

```mermaid
sequenceDiagram
    participant U as 用户
    participant C as ChatService
    participant DB as SQLite
    participant A as 主 Agent / ContextProvider
    participant W as Memory Worker
    participant E as MAF Extractor
    participant Z as Memory Zvec
    U->>C: 发送消息
    C->>DB: 保存 user 与 pending assistant
    C->>A: 当前消息 + 最近完成历史
    A->>Z: 相关 Memory 检索
    A->>DB: 复核事实 / 读取摘要
    Note over A: 并行执行独立 RAG；装配有界上下文
    A-->>C: 回答、引用、警告、是否提取
    C->>DB: 同事务提交完成回答 + queued Operation
    C-->>U: 回答成功 / AG-UI RUN_FINISHED
    Note over W: 前台空闲后取任务
    W->>DB: queued → running，读取源用户消息
    W->>E: 当前用户原文 + 可选相关事实
    E-->>W: 结构化候选
    W->>DB: 同事务提交事实 + Outbox + 成功回执
    W->>Z: 后续 embedding / upsert / delete / flush
    W->>DB: 确认 Outbox
```

因此有三个不同的“完成”：

1. **回答完成**：回答与持久 Memory 任务已提交。
2. **提取完成**：候选整理、事实和 Outbox 已提交。
3. **语义索引完成**：embedding 和 Zvec flush 成功，Outbox 已确认。

AG-UI `RUN_FINISHED` 只证明第一项，不证明长期记忆已经提取或已可跨会话语义召回。

## 7. 长期记忆的提取

### 7.1 触发条件

`MessageInput.memory_enabled` 默认是 `true`。
主 Agent 成功产生并通过校验的回答后，`after_run` 设置提取标志；
`ChatService` 以本轮用户消息作为持久任务来源。

识别出的独立寒暄跳过事实提取和自动 RAG，但仍保存对话、可以进行 Memory 召回。
只有 `search_mode=auto`、没有显式资源、当前及已有上下文符合寒暄条件时，
才使用该快路径，不是只看输入是否为 “hello”。

### 7.2 第一层：源消息准入

`eligible_source()` 在调用提取模型前排除已识别的凭据和外部引用。
当前规则包括凭据关键词/常见形式、代码围栏、引用行、部分文档/工具叙述形式、
引号等。

这是有意保守的正则策略：一条含引号的正常用户断言也可能被整体排除。
它不是完整的语言理解、隐私分类或提示注入检测器。

### 7.3 第二层：隔离的 MAF 提取 Agent

`AgentFrameworkMemoryExtractor` 创建：

```text
name         = shovel-memory-extractor
instructions = 独立 EXTRACTION 提示词
tools        = 无
output       = memory_candidates JSON Schema
```

模型收到：

```json
{
  "user_text": "I prefer green tea.",
  "related_memories": []
}
```

相关记忆只包含已有 ID、文本、主体、谓词，不允许模型自行发明 ID。
同一个提取器实例复用自己的客户端，不共享主 Agent 的 Skills、MCP 或 Python 工具。

### 7.4 第三层：结构化输出约束

一次最多 10 个候选，额外字段被拒绝。候选包含：

```json
{
  "text": "User prefers green tea.",
  "kind": "semantic",
  "subject": "user",
  "predicate": "prefers",
  "object": "green tea",
  "topic": "drink",
  "confidence": 0.95,
  "importance": 0.5,
  "evidence": "I prefer green tea.",
  "replaces": false,
  "merge_with": null
}
```

自动输出类型只有 `semantic` 和 `episodic`，没有 `procedural`。
谓词只接受：
`prefers`、`dislikes`、`uses`、`works_at`、`has_role`、`has_timezone`、
`has_language`、`has_goal`、`has_constraint`、`decides`、`knows_about`。

自动候选 schema 当前不输出 `expires_at` 或 `occurred_at`。
事件记录未提供事件时刻时使用写入时刻；不能把它理解为已实现自然语言日期解析。

### 7.5 第四层：程序校验

`admissible()` 要求：

- 文本去除首尾空白后长度为 8～1500 个字符。
- 置信度达到 `memory.min_confidence`，且置信度和重要性取值合法。
- evidence 非空，并且是源用户消息的**精确子串**。
- 来源与候选相关字段不匹配凭据规则；来源不匹配外部引用规则。
- 不自动接受 procedural。
- `replaces=true` 时，evidence 还必须包含可识别的明确纠正/选择表达。

模型无效 JSON、额外字段、格式错误等会使任务进入显式失败/重试流程。
合法候选被策略排除则可以得到空结果并正常提交成功回执。
“没有记住”与“提取失败”不是同一个状态。

## 8. 去重、语义归并与纠正

### 8.1 精确或规范化重复

在同一写入 Scope 和类型中，若 fingerprint 相同，或规范化后的文本相同：

- 复用已有 active 记录，不创建另一条。
- 置信度和重要性取现有值与新值的较大者。
- 合并不同 provenance。
- 仅合并来源/评分不需要重新向量化相同正文。

### 8.2 受约束的语义归并

纯向量相似不能证明两个断言相同。例如“喜欢茶”和“不再喜欢茶”可能很相似。

当前实现先检索最多 4 条相关记忆，只保留相似度至少 0.85、
同 Scope、active、未过期、revision 与模型版本均有效的记录。
模型可用 `merge_with` 指向其中一个已提供 ID。

程序要求目标存在于提供的参考集合，且满足 semantic 类型、主体和谓词一致，
不能同时是纠正。事务还会复核目标当前仍 active。
归并后保留原正文/身份并补充来源和评分，不用新表述静默覆盖旧正文。

自然语言“恰好同义”仍包含模型判断；程序约束缩小了错误面，
不能形式化证明两个句子语义完全等价。

### 8.3 明确纠正与 supersession

自动冲突判断要求：

```text
旧、新都是 semantic
subject 规范化相同
predicate 相同
topic 非空且规范化相同
object 规范化不同
新候选 replaces = true
```

例如，同一 `topic=drink` 下明确表示“改喝咖啡，不再喝绿茶”，可产生：

```text
旧 green tea：active → superseded，superseded_by → 新记录
新 coffee：active
同事务：旧向量 delete intent + 新向量 upsert intent
```

没有明确纠正时，不因为相似或不同值就删除旧事实。
不同 topic 的兼容偏好允许并存。

管理 API 的显式更新使用 `replace_id`，创建新记录并替代指定旧记录；
要求同一写入 Scope 内目标仍 active，不允许顺便改变记忆类型。

## 9. 生命周期与遗忘

### 9.1 状态机

```mermaid
stateDiagram-v2
    [*] --> active: 合法候选经整理提交
    active --> superseded: 显式纠正或管理更新
    active --> expired: 到达有效期
    active --> deleted: 用户忘记
    active --> rejected: 显式仓库状态转换
    superseded --> deleted: 显式删除
    expired --> deleted: 显式删除
    deleted --> [*]: hard delete
```

领域枚举还包括 `candidate`、`rejected`，但正常自动提取中候选是内存值，
被拒绝候选不会自动持久化为一条 rejected 记录。
不要从枚举存在推断已经有候选审批界面。

### 9.2 软删除

忘记操作把权威状态改为 deleted，同时持久化向量删除意图。
即使向量清理尚未运行，SQLite 复核也会阻止召回。

保留的 deleted fingerprint/正文作为 tombstone，阻止同一写入 Scope 中的
精确/规范化同一事实被再次整理激活。
它不是对所有未来自然语言改写的永久语义封禁。

### 9.3 硬删除

硬删除移除事实行，但仍保留向量删除意图。
因为墓碑也随事实行移除，不能再承诺通过该墓碑阻止之后重新学习同一内容。

无论软删除还是硬删除，都不等于清除其他会话源消息、历史备份、旧索引目录，
或撤回已经发送给模型供应商的数据。

### 9.4 过期

召回和正常列表先按有效期过滤，后台 `expire_due()` 再把到期 active 行转为 expired，
每种类型一批最多处理 32 条，并创建删除意图。
因此即使后台尚未来得及物化 expired 状态，已到期事实也不应该正常召回。

### 9.5 删除会话

会话删除先使对应后台任务失效，再软删除该会话的 session Scope Memory，
最后删除会话；摘要和消息通过外键级联删除。
默认自动提取的 agent Scope 跨会话事实保留。

当前会话专属记忆清理会列出最多 10000 条可见记录再筛选 session Scope，
不是无限规模的分页清理机制。

## 10. 语义召回与排序

### 10.1 召回流程

1. 使用独立 `[memory.embedder]` 向量化当前问题。
2. Zvec 按 workspace、user、Scope、agent/session 和模型版本过滤。
3. 取最多 `memory.max_items × 4` 个候选。
4. COSINE 距离转换为 `similarity = clamp(1 - distance, 0, 1)`。
5. 应用 `memory.min_similarity` 门槛。
6. 按 ID 回到 SQLite 读取当前记录。
7. 复核归属、类型、revision、模型版本、active 状态和有效期。
8. 对通过复核的记录排序、按字符与条数预算选择，更新访问统计。

残留向量、过时 revision、旧模型向量和已删除记录不能单凭向量命中进入上下文。

### 10.2 排序公式

设 `age_days = max(0, now - recorded_at) / 86400`：

```text
score =
    0.70 × relevance
  + 0.10 × importance
  + 0.10 × confidence
  + 0.05 / (1 + age_days / 30)
  + 0.05 × min(1, ln(1 + access_count) / 5)
```

相关性先经过门槛，再和重要性、置信度、记录新近性、访问次数共同排序。
时间项使用 `recorded_at`，不是 `last_accessed_at`；
没有后台不断改写重要性的“遗忘曲线”任务。

### 10.3 返回信息与预算

领域召回最多返回 `memory.max_items` 条，整个序列化列表不能超过
`memory.context_max_chars`，元数据同样计入预算。
放不下的条目会跳过，而不是只按事实正文长度计算。

召回上下文只携带最后一条来源中的 `source_kind`、`conversation_id`、`message_id`，
不把完整 evidence 历史重复发送给聊天模型。
管理列表可以显示更完整的 provenance。

应用层选中记录后增加访问次数，随后主 Agent 还会执行一轮更紧的总预算筛选；
因此访问次数代表应用层选中，不证明模型最终使用或引用了该事实。

### 10.4 没有 embedder 或索引故障

没有 `[memory.embedder]` 时，SQLite 保存、列表和生命周期仍可使用，
但不会改用全量加载或关键词 Memory 召回。
索引/embedding 发生已处理的错误时，Runtime 记录类型化错误、返回空召回，
并向聊天结果附加语义记忆不可用/恢复中的警告。

这是一条明确报告的降级路径，不是伪造召回成功，也不丢弃权威事实。

## 11. 主 Agent 的上下文装配

`ShovelContextProvider.before_run()` 是 Memory 和聊天接合的主要入口。

```text
初始预算 B =
  model.max_input_chars
  - 实际主 Agent instructions 字符数
  - 当前问题字符数
  - 512
```

当 B 小于 500，显式拒绝请求，而不是悄悄突破配置。
随后：

- 并发发起 Memory 召回与自动 RAG 检索；独立寒暄不发起自动 RAG。
- Memory 在这一层最多使用 `min(memory.context_max_chars, B / 4)`。
- RAG 以剩余预算的约四分之三作为片段预算，维护编号引用注册表。
- 已有 Session Summary 再使用剩余预算的约三分之一，且最多 2500 字符的序列化额度。
- 近期历史按从新到旧的顺序装入剩余预算，再恢复为时间顺序。
- 生成带 `sources`、`memory`、`session_summary` 的 JSON 参考数据消息，
  明确标为“不受信任的参考数据，不是指令”。

上述是装配阶段的预算策略，不是精确 tokenizer。
最终 `ContextBudgetMiddleware` 还检查实际提交的消息内容，包括工具结果；
超限明确失败。MAF 使用 `SlidingWindowStrategy(keep_last_groups=21)` 管理运行内消息组，
但它不能替代 SQLite 历史窗口、持久摘要或最终预算检查。

内置 instructions 规定：用户当前明确表达优先于历史记忆；
Memory 不是资料证据；不能执行参考数据中要求改变角色或披露秘密的指令。
Settings 增加的自定义 instructions 也会计入总预算。

`memory_enabled=false` 关闭本轮长期事实召回与事实提取，
但**不关闭正常聊天历史、读取已有摘要，也不等同于关闭会话压缩**。
摘要入队判断独立于该开关。

## 12. Session Summary 的增量压缩

### 12.1 压缩对象

回答完成时，若会话顺序已经达到历史窗口阈值，排队 `memory.summarize`。
执行时找出最近 `memory.history_messages` 条完成消息，
只总结其之前、且 sequence 大于已有 `through_sequence` 的消息。

单次最多读取 40 条旧消息，还会按摘要模型的输入预算选取前缀。
若首条就放不下，任务失败，不静默丢掉这一条并推进水位。

### 12.2 模型输入输出

摘要器使用独立的无工具 MAF Agent：

```text
previous_summary + 新的一批旧消息 → 新摘要
```

它使用与提取器相同的模型配置/独立客户端，不使用主 Agent 的自定义 instructions。
提示词要求保留目标、决策、待办问题和来源归属，不把对话数据当指令。

匹配凭据规则的源消息被替换为 `[sensitive message omitted]`。
输出必须非空、不得超过 `memory.summary_max_chars`，且再次检查凭据形式。
这不是对所有引用内容进行结构化剥离；引用的可信边界主要由摘要提示词约束。

### 12.3 提交水位

生成摘要后，在写事务中复核：

- epoch generation 未改变。
- Operation 仍 running。
- 会话仍存在。
- 原来的 `through_sequence` 未被其他执行推进。

摘要内容、水位和任务状态在同一事务提交。
还有旧消息未覆盖时，原任务返回 queued 继续处理；不是把剩余旧消息标记为已摘要。

后台尚未追上时，离开近期窗口且未被摘要覆盖的旧消息暂时不在主 Agent 上下文中，
但仍存于 SQLite。这是异步压缩的当前可见性边界。

## 13. 后台任务、幂等与事务

### 13.1 共用 Operation，而不是进程内临时队列

Memory 任务种类：

```text
memory.extract
memory.summarize
memory.create
memory.update
memory.rebuild
```

`memory.extract` 和 `memory.summarize` 使用确定性请求键：

```text
<kind>:<user_message_id>:native-v1
```

payload 记录源消息、会话、workspace/user、generation 和 run ID。
同一身份与参数重复入队复用 Operation；身份相同但请求指纹不同则冲突。
普通 Runtime Worker 排除 `memory.%`，由专门 Memory Worker 执行。

### 13.2 前台优先

Memory Worker 是 Generic Host 托管服务，单次执行通过锁串行化，
按 `queued_at` 和数据库 rowid 顺序取任务。
前台运行或排队、模型预热、最近一次前台交互后的空闲窗口都会影响调度。
默认 `performance.background_idle_seconds=5`。

这是开始执行前的空闲门控，不是抢占式调度：
已经发出的提取模型请求不会因为用户又发了一条消息而自动抢占或撤回。
没有可运行任务时，Worker 做索引维护并等待唤醒或约一秒超时。

### 13.3 两个关键事务

**回答事务：**

```text
assistant 标记 completed
+ citations / warnings
+ queued Memory Operation
+ assistant.memory_operation_id
→ commit
```

入队失败时，不把回答伪装成完整持久成功。

**事实事务：**

```text
校验 running Operation / epoch / 源消息
+ 去重 / 纠正 / 新事实
+ MemoryIndexOutbox
+ Operation succeeded 回执
→ commit
```

`SQLiteMemoryRepository.consolidate()` 使用 `BEGIN IMMEDIATE`。
多候选中途失败时整体回滚，不留下“前半批事实成功、任务却没有回执”的状态。
空候选也可原子提交成功回执。

这里保证的是 SQLite 原子性与幂等恢复，不是模型供应商侧严格 exactly-once：
崩溃重试可能再次调用模型，也可能再次计费。

### 13.4 失败重试

一般 Memory Operation 在尝试次数不足 3 时自动重新排队，
`retry_at = now + 2**attempts`，通常为第一次失败后约 2 秒、第二次后约 4 秒。
第三次仍失败则保留 failed 状态供查看和显式重试。
摘要正常分批续跑与失败重试不是同一概念。

显式重试还检查 Scope、generation、源消息是否仍存在。
对旧的失败提取，若数据库中已有较新的成功提取，当前实现保守拒绝重放，
防止旧断言再次覆盖较新事实；该检查并非精细到每个 topic 的因果重放系统。

## 14. Outbox 与向量最终一致性

### 14.1 为什么需要第二种队列

Operation 表示“我要提取/更新一条记忆”；Outbox 表示“某条事实的向量还没同步”。
这两个状态不能混为一谈。
SQLite 与 Zvec 没有一个跨库原子事务，因此不能可靠地先写事实再临时调用向量库，
然后希望进程不在中间退出。

### 14.2 维护步骤

每批最多取 16 条达到重试时刻的未确认 intent：

1. 从 SQLite 重新读取该记忆并校验 workspace/user。
2. 若 intent 是 upsert，且记录仍 active、revision 相同、未过期，则 embedding + upsert。
3. 否则执行确定性 ID 删除，避免把已经删除或替代的事实重新写活。
4. Zvec 调用成功并 `flush()` 后，才确认 Outbox。
5. 有效 upsert 确认时，把记录的 vector_state 标记 indexed 并记录模型版本。

所有新事实、替代旧事实、过期、软删除和硬删除的索引意图都来自 SQLite 事务。
知识库自身的 `DeletionOutbox` 是另一条独立链路，不能和 Memory Outbox 混用。

### 14.3 重试与崩溃窗口

失败保留 intent，增加 attempts，仅保存安全的错误类型。
退避为 `min(300, 2**min(attempts, 8))` 秒，
按当前表达式实际最高为 256 秒，没有配置最大重试次数。

若 Zvec 已 flush 但确认事务前进程退出，恢复后重复同一确定性 ID 的操作即可；
若事实已删但向量未删，SQLite 复核仍阻止暴露。
这就是“权威事实及时一致、派生索引最终一致”的含义。

## 15. 索引版本、重建与切换

### 15.1 model_version

共享 HTTP Embedder 的版本标识包含：

```text
provider:model@SHA256(
  endpoint、api_version、revision、预期 dimension、max_input_chars
)[:16]
```

不同 endpoint、维度或配置修订不能假定处于同一个向量空间。
如果服务端同名模型悄悄换权重、客户端配置却不变，系统无法自动知道；
应更新 embedding `revision` 并按新身份重建。

### 15.2 Zvec 中保存什么

索引保存 dense 向量和 `memory_id`、kind、归属、Scope、model_version、revision。
不保存事实正文作为权威副本。COSINE 度量用于相似检索。

Query embedding 有独立的内存 TTL 缓存，key 包含查询及模型版本的摘要。
不缓存事实、权限或检索结果，所以不能绕过删除与状态复核。
Memory 与 RAG 缓存独立，容量和 TTL 由 `[performance]` 配置。

### 15.3 重建触发

- 首次创建索引。
- 当前索引目录丢失或为空。
- 检测到 embedding 身份变化。
- 显式请求 `/memory/index/rebuild`。
- 恢复已开始但尚未结束的 building 状态。

### 15.4 Generation 切换

显式重建分配新 generation，不删除旧权威事实。
`reindex()` 把 active 记录标记 pending、补足 upsert intent，
并使已有待处理 intent 可立即重试。
写入优先进入 building collection；删除同时覆盖 active 与 building collection。

当这一 workspace/user 分区的未完成 Outbox 全部排空后：

```text
BEGIN IMMEDIATE
复核无 pending intents
active_generation = building_generation
model_version = 当前 embedder 版本
清空 building 元数据
state = ready
COMMIT
切换内存 handle，关闭旧 handle
```

兼容的旧 active 索引可在重建期间继续使用；
旧模型与新模型不兼容时不会混用向量，可能暂时没有语义召回。
写新索引失败或还有删除待处理时，不宣告切换成功。

旧 generation 目录不会自动清理，可能积累磁盘占用。
重建重新调用 embedding 服务，可能产生外部服务费用。

## 16. 取消、删除竞争与重启恢复

### 16.1 两种 generation 不要混淆

| 名称 | 保存位置 | 用途 |
| --- | --- | --- |
| 任务 epoch generation | `memory_epoch` | 阻止忘记前启动的旧任务迟到写入 |
| 索引 generation | `memory_index_state` 与 Zvec 目录 | 可重建索引的构建和切换 |

前者是业务写入围栏，后者是派生索引版本；两者都不是记录 revision。

### 16.2 用户忘记时

删除 API 首先验证记录归属，然后 `memory_tasks.invalidate()`：

- 尝试取消当前提取/摘要任务。
- 增加当前 workspace/user 的 epoch generation。
- 将这一分区 queued/running/failed Memory 任务标记 cancelled。
- 随后软/硬删除目标事实并排队向量清理。

这里故意采用分区级围栏，**忘记一条事实也可能取消同分区其他待处理 Memory 任务**，
而不是只取消这条事实相关的提取。
它不影响其他 workspace/user 分区。
失效与删除是先后事务，不是两者合一的跨方法大事务。

即使模型忽略取消并迟到返回，整理事务还会检查 Operation、epoch 和源消息，
旧结果不能仅靠“模型已完成”获得写入资格。

### 16.3 会话删除时

指定会话的 invalidate 取消该会话任务，不提高整个分区 generation。
提交时检查源消息/会话仍存在，防止删除后迟到的提取和摘要写回。
源断言曾形成的 agent Scope 事实不会因源会话删除自动撤回。

### 16.4 Host 重启

Runtime 启动顺序包括：存储锁、数据库升级、Memory epoch 初始化、
Operation 恢复和聊天 pending 状态恢复。

- 中断的 running Operation 没有已提交成功回执时重新 queued。
- 已原子提交事实和成功回执的任务不会因恢复被改回 running。
- 未确认 Outbox 继续处理，确定性 upsert/delete 可重放。
- 未完成摘要依据水位继续，不把未提交模型结果当作已保存摘要。
- pending assistant 变为明确失败，避免客户端误认为回答已完成。

正常 Host 停止时，中断任务可以重新排队；
停止/取消不能保证供应商服务器上的推理也停止。

### 16.5 原生 Zvec 资源的线程边界

Zvec 操作通过 `asyncio.to_thread` 执行，以线程锁保护原生 handle。
异步锁串行管理 active/building handle；取消时等待正在执行的原生 IO 收束，
避免在操作未结束时关闭或释放 handle。
初始化失败也释放部分打开的 collection，支持之后重试。

## 17. 安全、隐私与可信边界

### 17.1 已实现的限制

- 提取和摘要无工具，不能执行 MCP、函数、Skill 或直接改数据库。
- 只有用户源消息进入自动长期事实提取；工具审计不保存完整工具 payload。
- evidence 原文匹配、候选 schema、置信度和保守来源规则形成多道校验。
- 归属来自 Runtime，不来自模型。
- Memory、RAG、相关事实都被标为不可信数据，不应成为新系统指令。
- 当前用户明确纠正优先于旧记忆。
- 日志主要记录 ID、计数、耗时、状态和错误类型，不记录聊天/记忆正文或凭据。
- Loopback 访问限制、运行身份和 OS 存储锁维持本机单所有者边界。

### 17.2 不能宣称已经解决的问题

正则不是完备的 PII/秘密识别系统，也不保证防住所有记忆投毒。
不能把置信度 0.95 当作经过统计校准的正确率。
模型可能错误理解主体、topic、纠正或同义关系，仍需用户审阅。

SQLite 事实、原始历史和管理 API 返回的内容并非加密保险箱。
来源 evidence 可能出现在管理列表中，不应把原始密钥输入记忆系统。
“不保存工具全文”也不意味着助手自然语言回答不会包含工具结果：
助手原始回答依旧是聊天历史的一部分。

本地存储不等于本地推理。远程聊天模型会收到装配的 Memory/RAG/历史/摘要；
远程记忆提取模型会收到相关用户原文及候选参考事实；
远程 embedder 会收到被向量化的事实或查询。
摘要器收到旧摘要及选中的会话消息。应按具体 provider 判断数据出境和费用。

## 18. 配置及模型分工

### 18.1 四组模型配置

| 配置 | 用途 | 未配置时 |
| --- | --- | --- |
| `[model]` | 主 Agent 对话，默认也作为提取/摘要模型配置来源 | 不能正常聊天 |
| `[memory.llm]` | 可选的专用提取/摘要模型 | 整组回退到 `[model]` |
| `[memory.embedder]` | Memory 事实与问题的语义向量 | 不提供长期语义召回，SQLite 管理仍可用 |
| `[embedding]` | RAG 文档和查询向量 | 自动检索可走关键词，不等于禁用 Memory |

`[memory.llm]` 是独立模型配置对象，不是逐字段继承 `[model]`。
一旦要覆盖，至少提供完整的 provider/model，以及该 provider 所需 endpoint、
API version、凭据等；遗漏的普通字段使用该配置模型自身默认值。
它使用 `model_provider_type`、`model_name`、`base_endpoint`、`security_key` 等键，
不是旧的 `provider/model/base_url/api_key` 组合。

### 18.2 Memory 参数默认值

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `collection` | `shovel_memory` | Memory Zvec collection 名称 |
| `index_path` | 用户目录下 `.shovel\memory\zvec` | 索引根目录，内部再分区/generation |
| `dim` | 1024 | Memory embedding 预期维度 |
| `context_max_chars` | 2000 | 领域召回序列化字符预算，主 Agent 还有二次限制 |
| `max_items` | 10 | 长期召回条数上限 |
| `min_similarity` | 0.55 | 长期召回相关性门槛 |
| `min_confidence` | 0.8 | 自动候选置信度门槛 |
| `history_messages` | 20 | 最近完成消息的窗口，不是轮数 |
| `summary_max_chars` | 2000 | 摘要输出上限，注入时仍可能进一步缩减 |

所有时间戳使用 UTC Unix 秒，所有上述字符预算都不是 token 预算。

### 18.3 本地 Ollama 配置片段

在已有配置中合并下列段落，不要把该片段当作包含数据库和完整 RAG 的配置文件：

```toml
[memory]
collection = "shovel_memory"
index_path = '~\.shovel\memory\zvec'
dim = 1024
context_max_chars = 2000
max_items = 10
min_similarity = 0.55
min_confidence = 0.8
history_messages = 20
summary_max_chars = 2000

[memory.llm]
model_provider_type = "ollama"
model_name = "shovel-chat-fast"
base_endpoint = "http://127.0.0.1:11434"
security_key = ""
max_input_chars = 16000
max_output_tokens = 2000
timeout_seconds = 300

[memory.embedder]
provider = "ollama"
model = "bge-m3"
base_url = "http://127.0.0.1:11434"
api_key = ""
batch_size = 8
timeout_seconds = 300
```

`max_input_chars=16000` 是客户端字符上限示例，不保证匹配具体模型的 token 窗口。
应结合 Ollama `num_ctx`、实际消息长度和输出预算调整。
系统不会自动下载模型，也不保证任意小模型都能稳定遵守结构化 schema。
提取与摘要可以使用独立模型，但目前共用同一 `[memory.llm]`，不能分别指定两个。

完整已有示例见 [settings.example.toml](settings.example.toml)，
模型定义见 [shovel-chat-fast.Modelfile](shovel-chat-fast.Modelfile)。
Memory `dim` 必须匹配 Memory embedder 实际输出，与 RAG 的 `zvec.dim` 独立。

### 18.4 运行时与环境配置

运行时设置优先级是环境变量高于 TOML、高于默认值。
环境嵌套示例：`SHOVEL_MEMORY__EMBEDDER__API_KEY`、
`SHOVEL_MEMORY__LLM__SECURITY_KEY`。
`AppSettings.load()` 则有意只读取文件，不能用它验证环境覆盖后的全部运行配置。

provider、路径或模型版本变更通常需要重启 Host；
仅重启主 Agent 不会重建 Runtime 的独立 Memory 客户端和配置。
在改变存储身份前停止使用这些目录的 Host，不能通过删除锁文件绕过所有权锁。

## 19. 管理 API 与页面能力

下面路径均以 `/api/v1` 为前缀：

| API | 语义 |
| --- | --- |
| `GET /memories` | 默认列出当前可见的 active、未过期记录，返回 `{"items": [...]}` |
| `GET /memories?include_history=true` | 同时查看生命周期历史；仍受 Scope 和 limit 约束 |
| `GET /memories?conversation_id=...` | 在指定会话上下文中读取，包括该会话的 session Scope |
| `POST /memories` | 手动新建，HTTP 202，返回持久 Operation |
| `PUT /memories/{id}` | 显式替代更新，HTTP 202，返回 Operation |
| `DELETE /memories/{id}` | 默认软删除，返回 `{"deleted": true}` |
| `DELETE /memories/{id}?hard=true` | 硬删除事实行，仍保留向量清理意图 |
| `POST /memory/index/rebuild` | 排队重建，HTTP 202；需要配置 Memory embedder 才能执行成功 |
| `GET /operations`、`GET /operations/{id}` | 查看任务状态/回执 |
| `POST /operations/{id}/retry` | 显式重试符合条件的失败任务 |
| `GET /status` | 返回 Memory index_state、pending 数量、last_error 等 |

新建、更新和重建支持 `Idempotency-Key`：
同键同参数复用原请求，改变参数必须换键或得到冲突，不能盲目重复提交。
忘记接口不是 HTTP 202 的 Memory Operation；它执行任务失效和权威删除，
向量清理由 Outbox 异步完成。

手动新建示例：

```json
{
  "text": "User prefers concise answers.",
  "kind": "semantic",
  "scope": "agent",
  "subject": "user",
  "predicate": "prefers",
  "object": "concise answers",
  "topic": "response.style",
  "importance": 0.7
}
```

手动写入是受信任的显式管理路径，不经过用户消息的自动提取准入；
仍有输入校验和凭据检查，记录 `Provenance("manual")`、confidence=1。
session Scope 必须提供存在的 `session_id`，其他 Scope 不允许附带 session_id。
procedural 可经该路径显式维护，但不会自动提升为主 Agent instructions。

当前 **Settings → Manage long-term Memory** 支持按需加载、查看、
确认软删除和排队重建。后台任务页查看/重试失败 Operation。
API 比页面丰富：不能因为 API 有手动创建/更新，就认为页面已有完整表单。

## 20. 按依赖顺序理解实现步骤

以下是按当前代码依赖重建的实现路径，不声称是历史提交顺序，
也不是要求读者再次替换现有实现。

### 步骤 1：先明确边界和不变量

分开 History、Summary、长期事实和 RAG。
确定 SQLite 权威、模型只能输出候选、工具/文档不作为用户事实、
删除后不能凭旧向量复活等不变量。

### 步骤 2：建立领域值和策略

在 `memory\domain.py` 定义类型、归属、候选、记录、指纹、准入、重复及冲突。
在 `memory\ports.py` 定义模型、权威仓库和派生索引接口，
使规则不依赖供应商 SDK。

### 步骤 3：建立持久模型与可重复升级

在 `db\model\memory.py`、`db\model\runtime.py` 定义事实、摘要、
Outbox、generation 和 Operation 相关字段。
`db\migrations.py` 的有序升级步骤补充既有数据库列与索引，
由 `SchemaMigration` 追踪，不能只修改 ORM 而忽略已有存储。

原生记忆升级会补充归属；没有合法归属的开发期占位行不会被暴露为当前用户事实。
数据库保留历史迁移名称不表示仍依赖或调用旧 Memory 库。
当前没有把旧外部 Memory 服务数据导入原生事实库的迁移器。

### 步骤 4：先完成 SQLite 整理事务

实现 `_visible`、读取、去重、supersede、删除、过期、访问统计。
将事实、索引意图、任务回执绑定到同一事务，先保证没有 Zvec 时业务仍正确。

### 步骤 5：接入隔离的 MAF 提取与摘要

复用 provider 客户端创建逻辑，但使用独立 Agent、提示词与生命周期。
加入 JSON Schema、原文 evidence、字符预算、超时和明确错误。
禁止直接把模型输出解释为 SQL 或数据库操作。

### 步骤 6：建立独立 Memory 向量索引

复用共享 Embedder，但独立配置、分区、schema 和缓存。
使用确定性 `memory_vector_id`，实现 upsert/delete/flush、
线程安全的 handle 访问和关闭。

### 步骤 7：实现 Outbox 与 generation

先可靠地同步增删，再实现模型身份变化、目录丢失恢复、building generation、
无未完成意图后的切换。不能把“调用 upsert”当成“索引已持久完成”。

### 步骤 8：实现可验证的召回

向量候选先过相似度门槛，再逐条回到 SQLite 复核。
加上有界排序、provenance 精简、条数/字符预算和访问统计。

### 步骤 9：挂接持久后台任务与前台优先调度

使用现有 Operation 表与 request identity，增加 Memory Worker。
完成 epoch 围栏、源消息校验、自动有限重试和 Host 重启恢复。
不把后台模型调用放在 Web 请求结束后不可追踪的临时 task 中。

### 步骤 10：挂接主 Agent 上下文及对话事务

在 MAF before/after hooks 连接召回和提取标志；
在 ChatService 成功事务中入队。
History、Summary、Memory 和 RAG 共同受实际 instructions 及总上下文预算约束。

### 步骤 11：开放管理与可观测性

提供列表、历史、显式维护、忘记、重建、Operation 状态和安全日志，
使用户可以发现错误、修正事实，而不是维护不可见的黑箱画像。

### 步骤 12：用失败和竞争测试验证

优先验证事实/回执原子性、已忘记内容不复活、跨 Scope 不泄漏、
重建中删除、索引写入失败、迟到提取和恢复等工程契约，
而不仅是模型是否能输出一条偏好。

## 21. 测试与验证

2026-10-29 整理本文时执行：

```powershell
uv run --no-sync pytest backend\tests\test_native_memory.py backend\tests\test_memory_tasks.py --no-cov --tb=short
```

结果：**32 passed**。这些测试使用临时 SQLite、真实 Zvec 操作、
确定性测试 embedder 或 MockTransport；不调用付费模型服务。
结果证明这些用例覆盖的工程契约，不证明生产模型的事实提取质量。

| 测试文件 | 代表性验证 |
| --- | --- |
| `backend\tests\test_native_memory.py` | 去重、显式纠正、墓碑；workspace/user/agent/session 隔离 |
| 同上 | 真实 Zvec 召回、失败重试、删除和重新打开 |
| 同上 | 重建切换、索引丢失、模型版本变化、重建与删除竞争 |
| 同上 | 语义归并目标约束、过期、硬删除、字符预算、SQLite 二次复核 |
| 同上 | 敏感/外部源不调用提取器、无效 evidence、低置信度、禁止自动 procedural |
| 同上 | 管理 API 幂等更新；OpenAI/Azure OpenAI/Ollama Mock 下跨会话召回 |
| `backend\tests\test_memory_tasks.py` | 回答与任务、事实与 Outbox/回执的原子性及整体回滚 |
| 同上 | 忘记/删除后的迟到提取围栏、重启恢复、三次失败可见 |
| 同上 | 不误伤其他 workspace 的任务、摘要独立及随会话删除 |
| `backend\tests\test_hosted_runtime.py` | 默认 Memory、REST/AG-UI、RAG 引用、独立寒暄、无 embedder 降级、预算和诊断 |
| `backend\tests\test_agent_instructions.py` | instructions 加载/应用及其纳入上下文预算 |

若已有 Host 在运行，部分其他集成测试的默认 Memory 路径可能与其争用 OS 锁。
应隔离测试存储，不停止不相关服务、不删除用户数据，也不绕过锁。
只有明确配置并显式 opt-in 的 live 测试才应访问真实外部模型。

## 22. 排错与运维

| 现象 | 优先检查 | 正确理解 |
| --- | --- | --- |
| 回答成功但没有新事实 | 消息 `memory_operation_id`、Operation、准入条件、模型输出 | 可能仍排队、被规则排除或正常空候选 |
| 有事实但新会话想不起来 | `[memory.embedder]`、索引状态、pending、相似度与 Scope | SQLite 保存成功不等于向量已就绪 |
| 长时间 queued | 前台运行/排队、预热、idle 秒数、retry_at | 前台优先可能延后后台执行 |
| Operation failed | 安全错误信息、模型 schema 能力、输入/输出预算、timeout | 不应伪造成功；排除原因后按条件重试 |
| Index failed/pending | embedder 维度/身份、目录权限、Outbox last_error | 不直接删除 SQLite 事实；可修复后重试/重建 |
| 更换模型后召回为空 | model_version、building generation、待处理 intents | 不兼容向量不混用，等待新索引完成 |
| 点忘记后仍看到原文 | 聊天历史、摘要、旧 generation、备份 | 忘记事实不是擦除所有来源副本 |
| 删除会话后仍记得偏好 | 事实 scope、来源 | 跨会话 agent Scope 本来就保留 |
| 旧内容未进入上下文 | 最近历史窗口、摘要水位、总字符预算 | 完整原始历史仍可在 SQLite 中存在 |
| 修改配置不生效 | 是否重启 Host、环境覆盖、实际配置路径 | 主 Agent restart 不等于 Memory Runtime 重配 |

备份以 SQLite 权威数据和配置为中心，同时妥善保护包含用户原文的备份。
Zvec 可重建不代表“随时删除正在使用的索引目录”是安全操作。
索引重建和查询也可能调用外部模型，不应把恢复操作当作一定免费。

## 23. 当前限制与尚未接入的模型

以下边界来自真实调用链，而不是仅依据 ORM 类的说明文字：

1. `MemoryWorking` 已有槽位、压缩等模型字段，但未接入主 Agent 的 working-memory
   持久调度；当前短期上下文实际依赖聊天历史、摘要和 MAF 单次运行上下文。
2. `MemoryLink`、`MemoryRevision`、`StandingQuery` 是已有模型/扩展点，
   当前原生 Memory 路径没有构建完整关联图、逐次修订日志或主动订阅推送。
3. 自动提取仅 semantic/episodic；procedural 只可显式维护，
   不具备自动学习、成功率反馈优化或自动修改 Skills/工作流的闭环。
4. 自动候选不提供完整时间推理；有效期等可显式管理，
   不存在任意历史时点的完整双时态查询接口。
5. 对原始事实的宽泛敏感性、真假、自然语言等价和主体归属，没有完备判定器。
6. 缺失 Memory embedder 时没有长期语义召回替代算法；
   未提炼成事实的旧消息不会因为保存过就自动跨会话检索。
7. 异步摘要存在追赶窗口；强字符预算可能排除一部分历史或已召回事实。
8. 子 Agent / Workflow 不自动继承主 Agent 的 Memory、RAG、instructions 和 Skills。
9. 旧索引 generation 不自动回收；软删除、硬删除不擦除备份或所有源内容。
10. 本机单 owner/loopback 边界不是多用户 SaaS 权限体系，
    SQLite/Zvec 也不是默认加密的秘密存储。

这些限制不应被文档中的“理论分类”或“数据库字段已经存在”掩盖。
后续扩展应保持现有权威存储、来源、Scope、事务和遗忘围栏不变量。

## 24. 源码阅读索引

建议按下表顺序阅读。方法名比容易随修改漂移的固定行号更适合定位：

| 源码 | 重点入口 |
| --- | --- |
| [领域定义](../backend/src/shovel/memory/domain.py) | `MemoryScope`、`MemoryCandidate`、`admissible`、`conflicts` |
| [端口](../backend/src/shovel/memory/ports.py) | `MemoryRepository`、`MemoryExtractor`、`MemoryVectorIndex` |
| [领域服务](../backend/src/shovel/memory/service.py) | `extract`、`related`、`retrieve`、`rank` |
| [权威仓库](../backend/src/shovel/memory/sqlite_repository.py) | `_visible`、`consolidate`、`transition`、`pending`、`acknowledge` |
| [MAF 提取与摘要](../backend/src/shovel/memory/agent_framework.py) | `EXTRACTION`、输出 schema、`extract`、`summarize` |
| [Zvec 索引](../backend/src/shovel/memory/zvec_index.py) | `initialize`、`start_rebuild`、`finish_rebuild`、`search`、`_io` |
| [Runtime 门面](../backend/src/shovel/runtime/memory.py) | `extract`、`maintain`、`write`、`summarize`、`status` |
| [后台任务](../backend/src/shovel/runtime/memory_tasks.py) | `enqueue`、`invalidate`、`run_once`、`retry`、`_finish` |
| [主 Agent 接入](../backend/src/shovel/runtime/agent.py) | `ShovelContextProvider`、`ContextBudgetMiddleware`、`ShovelAgent.respond` |
| [对话事务](../backend/src/shovel/runtime/chat.py) | `_send`、`stream`、`maintain`、`recover` |
| [Host](../backend/src/shovel/runtime/hosting.py) | `MemoryExtractionWorker` |
| [运行时初始化](../backend/src/shovel/runtime/service.py) | `Runtime.__init__`、`create_runtime` |
| [前后台调度](../backend/src/shovel/runtime/performance.py) | `RuntimePerformance`、`RuntimeWarmup` |
| [实体表](../backend/src/shovel/db/model/memory.py) | `NativeMemory`、三类记忆、`SessionSummary`、索引元数据 |
| [任务与历史表](../backend/src/shovel/db/model/runtime.py) | `Operation`、`ChatMessage`、`MemoryEpoch` |
| [迁移](../backend/src/shovel/db/migrations.py) | `native_memory` 升级步骤、`upgrade_database` |
| [API](../backend/src/shovel/runtime/api.py) | `memories`、`create_memory`、`update_memory`、`forget` |
| [输入约束](../backend/src/shovel/runtime/schemas.py) | `MemoryInput`、`MessageInput` |
| [Memory 配置](../backend/src/shovel/config/memory_config.py) | `MemorySettings`、`MemoryModelSettings` |
| [向量身份与 schema](../backend/src/shovel/db/base.py) | `memory_vector_id`；schema 见 `vector\schema.py` |

相关文档：[项目总览与 Native Memory 配置](../README.md#native-memory)、
[Memory 向量身份](db_base_memory_vector_id.md)、
[完整 Ollama 配置示例](settings.example.toml)。
