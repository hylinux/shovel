# 任务：为 Shovel 设计并实现原生 Memory Layer

请为 Shovel AI Agent 平台设计并实现一套原生的长期记忆系统（Native Memory Layer）。

## 一、项目背景

Shovel 是一个基于 Microsoft Agent Framework 构建的 AI Agent 平台。

当前与 Memory 相关的技术架构如下：

- Microsoft Agent Framework 作为 Agent Runtime。
- SQLite 作为主要的本地数据库。
- SQLAlchemy 用于数据持久化和数据库访问。
- Zvec 作为本地向量数据库 / 向量索引。
- Shovel 采用 Local-first 架构。
- 默认情况下，Shovel 不应该要求用户部署 PostgreSQL、Redis、Qdrant、Neo4j 或独立的 Memory Server 等外部服务。
- 依赖管理、服务生命周期和组件注册应该遵循 Shovel 当前已有的架构和约定。

新的 Memory Layer 应作为 Shovel 自身的原生能力，而不是依赖 Mem0 或其他外部 Memory Framework。

但是整体架构必须保持可扩展性，以便未来可以支持：

- Mem0
- Qdrant
- PostgreSQL
- Azure AI Search
- Graph Memory
- 其他 Memory Provider


## 二、设计目标

构建一个清晰、独立、可扩展的 Memory 子系统，使 Shovel Agent 能够：

1. 跨 Conversation 保存有价值的长期信息。
2. 在 Agent / LLM 调用之前检索相关 Memory。
3. 从 Conversation 中提取有价值的长期 Memory。
4. 当信息发生变化时更新 Memory。
5. 避免保存重复或者低价值的信息。
6. 明确区分 Conversation History 和 Long-term Memory。
7. 支持 Memory 生命周期管理，例如：
   - Create
   - Update
   - Supersede
   - Expire
   - Delete
8. 保持 Local-first。
9. Vector Index 必须可以从持久化数据中完全重建。
10. 与 Microsoft Agent Framework 清晰集成，但 Memory Domain 不应该直接依赖 Agent Framework API。


## 三、核心架构原则

Memory Layer 必须遵循下面的核心原则：

SQLite = Memory 的 Source of Truth

Zvec = Memory 的 Semantic / Vector Search Index

也就是说：

SQLite 负责保存权威的 Memory 数据、状态和生命周期。

Zvec 只负责：

- Embedding
- Semantic Retrieval
- Vector Search

Zvec 必须被设计成一个可以随时重新生成的索引。

即使整个 Zvec Index 丢失，也不能导致用户的 Memory 丢失。

系统应该能够：

SQLite
    ↓
重新生成 Embedding
    ↓
重建 Zvec Index


## 四、Memory 类型设计

不要把 Conversation、Message、Summary 和 Long-term Memory 全部混在一起。

至少需要区分下面几类数据。


### 1. Conversation History

保存原始的：

- User Message
- Assistant Message
- 必要的 Tool Interaction

Conversation History 主要用于：

- Conversation 恢复
- Session 管理
- Summary
- Memory Extraction
- 必要的审计和追踪

Conversation History 不应该自动成为 Long-term Memory。


### 2. Session Summary

对一个 Conversation 或 Conversation 片段进行压缩。

主要用于：

- 控制 Context 长度
- 保留重要的 Session Context
- 支持长对话
- 降低 Token 消耗

Session Summary 不等同于 Long-term Memory。


### 3. Semantic Memory

保存相对稳定的：

- 用户事实
- 用户偏好
- 项目背景
- 项目约束
- 技术选择
- 明确决策
- 长期 Context

例如：

“用户偏好 Local-first 的应用架构。”

“Shovel 使用 SQLite 作为主要数据库。”

“Shovel 使用 Zvec 作为本地 Vector Index。”


### 4. Episodic Memory

记录某一个具体 Interaction 或时间点发生的重要事情。

例如：

“在一次 Shovel 架构讨论中，用户决定使用 Zvec 替代 Qdrant 作为默认本地 Vector Storage。”

Episodic Memory 应该保留适当的时间和来源 Context。


### 5. Procedural Memory

保存可以重复使用的：

- 工作流程
- 操作模式
- 问题解决方法
- Task Pattern

架构上应该支持 Procedural Memory。

但是 V1 暂时不需要实现自动生成 Procedural Memory 的复杂机制。


## 五、Memory 生命周期

Memory 写入应该按照下面的概念流程设计：

User Interaction
    ↓
Conversation Persistence
    ↓
Memory Candidate Extraction
    ↓
Normalization
    ↓
Deduplication
    ↓
Conflict Detection
    ↓
Create / Update / Supersede / Ignore
    ↓
Persist to SQLite
    ↓
Update Zvec Index


Memory 至少需要能够表达类似下面的状态：

- candidate
- active
- superseded
- expired
- deleted
- rejected


当新的信息和已有 Memory 冲突时：

不要简单覆盖或者删除旧 Memory。

应该优先考虑：

Old Memory
    ↓
Superseded

New Memory
    ↓
Active

从而保留 Memory 的变化历史。

例如：

Old:

“用户选择 Qdrant 作为 Shovel 的本地 Vector Database。”

New:

“用户决定改为使用 Zvec。”

应该允许系统保留这一变化过程。


## 六、Memory Retrieval

Agent 调用之前应该执行 Memory Retrieval。

基本流程：

Current User Query
    ↓
Zvec Semantic Search
    ↓
Candidate Memories
    ↓
SQLite 加载权威 Memory Records
    ↓
Scope Filter
    ↓
Status / Lifecycle Filter
    ↓
Ranking
    ↓
Context Budget Selection
    ↓
Memory Context
    ↓
Agent / LLM


不要仅仅依赖 Vector Similarity。

Ranking 架构应该允许组合以下信号：

- Semantic relevance
- Importance
- Recency
- Confidence
- Access / Usage History

第一版不需要构建非常复杂的 Ranking Algorithm。

但是架构需要为后续优化 Ranking 策略保留空间。


## 七、Memory Context 原则

最终注入 Agent 的 Memory 必须：

1. 只包含与当前 Query 高度相关的信息。
2. 有明确的 Context / Token Budget。
3. 不应该无限增加 Prompt 长度。
4. 不应该把所有 Memory 都传递给 Agent。
5. 应该允许识别 Memory 的来源和基本状态。

最重要的原则：

Current User Input
    >
Historical Memory

如果用户当前明确表达的信息与历史 Memory 冲突：

优先相信用户当前输入。

同时可以触发：

Memory Update

或者：

Memory Supersede


## 八、Memory Scope

Memory 应支持不同的隔离 Scope。

至少在架构上考虑：

- user
- agent
- session
- workspace

例如：

User Memory

只属于某个用户。

Agent Memory

只属于某个特定 Agent。

Session Memory

只在当前 Session 范围有效。

Workspace Memory

属于一个 Workspace 内多个 Agent 可以共享的 Context。


Memory Retrieval 时必须严格执行 Scope Isolation。

不能因为 Vector Similarity 很高，就把其他 User / Agent / Workspace 的 Memory 返回出来。

Scope 模型必须保持可扩展性，以支持未来增加新的 Scope。


## 九、SQLite 与 Zvec 的一致性

SQLite 和 Zvec 不应该被假设可以参与同一个 Atomic Transaction。

必须采用：

Eventual Consistency

设计。

基本原则：

SQLite
    =
Source of Truth

Zvec
    =
Derived Index


如果：

SQLite 写入成功

但：

Zvec Index Update 失败

Memory 本身仍然应该被视为成功保存。


系统需要提供机制处理：

- Pending Index Operations
- Failed Index Retry
- Stale Vector Cleanup
- Embedding Version Changes
- Index State Tracking
- Zvec Index Rebuild


可以考虑：

Outbox Pattern

或者采用符合 Shovel 当前架构的等价机制。


## 十、Memory Extraction

绝对不要把每一条 Conversation Message 直接转换成为 Long-term Memory。

应该建立：

Memory Extraction Layer


Memory Extraction 负责从 Conversation 中识别：

哪些信息未来还有价值？

通常可以考虑保存：

- 稳定的用户事实
- 明确表达的用户偏好
- 长期项目 Context
- 重要项目约束
- 明确做出的技术决策
- 可重复使用的 Task Outcome


通常不要保存：

- 问候
- 简单确认
- 无意义的闲聊
- 临时问题
- 未经过确认的推测
- Conversation History 的简单复制
- Password
- Access Token
- API Key
- Secret
- 其他敏感凭据


同时：

来自 Tool、Document、Web Page 或外部数据源的信息，不应该自动变成“关于用户的事实”。


应该优先采用：

Structured Memory Candidate

而不是让 LLM 直接执行数据库写入。


推荐概念流程：

Conversation
    ↓
LLM / Memory Extractor
    ↓
Structured Memory Candidates
    ↓
Validation
    ↓
Memory Consolidation
    ↓
Persistence


## 十一、Memory Consolidation

需要设计一层 Memory Consolidation。

它负责判断一个新的 Memory Candidate 应该：

- Create
- Update
- Merge
- Supersede
- Ignore


概念流程：

Memory Candidate
    ↓
Exact Duplicate Detection
    ↓
Semantic Similarity Detection
    ↓
Conflict Detection
    ↓
Memory Decision


例如：

完全相同的信息：

Ignore / Update Metadata


语义相同的信息：

Merge


新的信息替代旧信息：

Supersede


完全新的信息：

Create


## 十二、安全性与可靠性

Memory 系统必须考虑：

- Memory Poisoning
- Untrusted Content
- Sensitive Information
- User Isolation
- Agent Isolation
- Workspace Isolation
- Stale Memory
- Conflicting Memory
- Soft Delete
- Hard Delete
- Embedding Model Changes
- Failed Vector Indexing
- Memory Provenance


特别需要防止：

Tool Output

Document Content

Web Content

External Content

被自动转换成：

User Fact


例如：

网页内容中出现：

“用户喜欢 Python。”

不能因此创建：

User Preference:
    用户喜欢 Python。


Memory 应尽可能保存必要的 Provenance，以便知道：

Memory 是从哪里来的。


## 十三、Memory Abstraction

不要让 Memory Domain 直接依赖：

- SQLite
- Zvec
- Mem0
- Microsoft Agent Framework


建立清晰的抽象，例如：

MemoryRepository

MemoryVectorIndex

MemoryExtractor

MemoryRetriever

MemoryRanker

MemoryComposer

MemoryConsolidator

MemoryService


第一版 Native Provider：

MemoryRepository
    ↓
SQLite / SQLAlchemy


MemoryVectorIndex
    ↓
Zvec


未来应该能够增加：

MemoryRepository
    ↓
PostgreSQL


MemoryVectorIndex
    ↓
Qdrant


或者：

Azure AI Search

Mem0

Graph-based Memory System


而不需要重新设计整个 Memory 子系统。


## 十四、Microsoft Agent Framework 集成

Memory Layer 与 Microsoft Agent Framework 之间应该通过：

Adapter

或者：

Context Provider

这类 Integration Layer 进行集成。


概念上的读取流程：

User Input
    ↓
Memory Retrieval
    ↓
Context Composition
    ↓
Microsoft Agent Framework
    ↓
Agent / LLM


概念上的写入流程：

Agent Execution
    ↓
Conversation Persistence
    ↓
Memory Extraction
    ↓
Memory Consolidation
    ↓
SQLite
    ↓
Zvec Index Update


Microsoft Agent Framework 相关类型和 API：

只允许存在于 Integration Layer。

不要让：

Domain Layer

或者：

Application Layer

直接依赖 Microsoft Agent Framework。


## 十五、模块划分建议

Memory 子系统可以按照下面的概念进行组织：

memory/
    domain/
    application/
    infrastructure/
    integrations/
    workers/


### domain

负责：

- Memory Entity
- Memory Type
- Memory State
- Memory Scope
- Domain Policy
- Domain Event


### application

负责：

- Memory Service
- Memory Extraction
- Memory Retrieval
- Memory Ranking
- Memory Consolidation
- Context Composition
- Lifecycle Orchestration


### infrastructure/sqlite

负责：

- SQLAlchemy Model
- SQLite Repository
- Persistence


### infrastructure/zvec

负责：

- Vector Index
- Embedding Indexing
- Semantic Retrieval
- Index Management


### integrations/agent_framework

负责：

Microsoft Agent Framework 与 Shovel Memory Layer 之间的集成。


### workers

根据 Shovel 当前已有架构决定是否需要 Worker。

可能用于：

- Memory Extraction
- Vector Index Update
- Retry
- Cleanup
- Index Maintenance

不要因为这个设计建议而强制引入新的 Worker Framework。


## 十六、V1 范围

第一版本重点实现：


### 必须考虑

- Conversation Persistence
- Session Summary
- Semantic Memory
- Episodic Memory
- SQLite Persistence
- Zvec Semantic Index
- Memory Extraction
- Memory Deduplication
- Memory Consolidation
- Memory Retrieval
- Memory Ranking
- Memory Scope
- Memory Supersede
- Soft Delete
- Index Retry / Recovery
- Zvec Index Rebuild
- Embedding Version Tracking
- Configurable Memory Context Budget
- Basic Provenance
- Basic Memory Poisoning Protection


### V1 暂时不要实现

- Knowledge Graph
- Neo4j
- Multi-hop Graph Retrieval
- Graph Traversal
- Reinforcement Learning
- Autonomous Agent-managed Memory
- 自动 Procedural Memory Learning
- 复杂 Memory Relationship Graph
- 默认依赖外部 Memory Service


不要过度设计 V1。

目标是：

Simple

Reliable

Local-first

Extensible


## 十七、开发要求

在开始实现之前：

先阅读并分析当前 Shovel Repository。

重点确认：

1. 当前项目目录结构。
2. 当前 SQLite 实现。
3. 当前 SQLAlchemy Model / Repository 设计。
4. 当前 Database Abstraction。
5. 当前 Zvec Integration。
6. 当前 Embedding Service。
7. 当前 Dependency Injection 机制。
8. 当前 Service Registration 方式。
9. 当前 Generic Host 架构。
10. 当前 Microsoft Agent Framework Integration。
11. 当前 Session / Conversation 管理方式。
12. 当前 Background Service / Worker 机制。


非常重要：

不要因为本 Prompt 中提出了一种目录或者接口设计，就直接创建一套与现有 Shovel 架构平行的新体系。

应该：

优先复用现有 Shovel Architecture。

如果当前项目已经存在类似：

Repository

Service

Provider

Context Provider

Background Service

Unit of Work

Event

等抽象：

优先复用或者扩展现有设计。


不要重新设计与 Memory 无关的 Shovel 模块。


## 十八、设计原则

实现过程中遵循：

KISS

YAGNI

Separation of Concerns

Dependency Inversion

Local-first


避免：

Over-engineering

Premature Abstraction

Unnecessary Infrastructure

Framework Coupling


特别是：

不要为了“以后可能需要”而在 V1 中引入复杂组件。

但是应该通过合理的接口边界，为未来扩展留下空间。


## 十九、交付要求

不要直接开始大规模修改代码。

第一阶段先分析当前 Shovel Repository，并输出一份简洁的 Implementation Plan。


Implementation Plan 必须包括：

### 1. Current Architecture Analysis

当前 Shovel 中：

- SQLite
- SQLAlchemy
- Zvec
- Embedding
- DI
- Generic Host
- Microsoft Agent Framework

分别是如何组织的。


### 2. Proposed Memory Architecture

说明 Memory Layer 如何融入现有 Shovel Architecture。


### 3. Modules / Files

说明：

需要新增哪些模块。

需要修改哪些已有模块。

尽量避免修改与 Memory 无关的文件。


### 4. Core Abstractions

明确核心抽象及其职责，例如：

MemoryService

MemoryRepository

MemoryVectorIndex

MemoryExtractor

MemoryRetriever

MemoryRanker

MemoryConsolidator

MemoryComposer


但如果现有代码已经存在类似抽象：

优先复用已有设计。


### 5. Data Model

说明需要增加哪些：

Entity

Table

State

Scope

Index Metadata

Provenance


不需要一开始给出非常复杂的数据模型。


### 6. SQLite / Zvec Synchronization

解释：

SQLite 如何作为 Source of Truth。

Zvec 如何作为 Derived Index。

Index Failure 如何 Recovery。

如何 Rebuild Zvec。


### 7. Microsoft Agent Framework Integration

说明：

Memory Retrieval 在什么位置发生。

Memory Extraction 在什么位置发生。

Memory Context 如何注入 Agent。


### 8. Read Flow

描述：

User Query
    ↓
Memory Retrieval
    ↓
Context
    ↓
Agent


### 9. Write Flow

描述：

Conversation
    ↓
Memory Extraction
    ↓
Consolidation
    ↓
SQLite
    ↓
Zvec


### 10. Implementation Phases

将实现拆分成多个小阶段。


建议采用类似：

Phase 1
    Domain Model + Persistence

Phase 2
    Zvec Integration

Phase 3
    Memory Retrieval

Phase 4
    Memory Extraction + Consolidation

Phase 5
    Microsoft Agent Framework Integration

Phase 6
    Recovery + Maintenance

Phase 7
    Tests + Documentation


但最终 Phase 划分应该根据当前 Shovel Repository 实际情况决定。


## 二十、实现阶段要求

Implementation Plan 完成后，再开始逐步实现。

每一个 Phase：

1. 修改范围尽量小。
2. 遵循当前 Shovel Coding Style。
3. 复用当前 Infrastructure。
4. 添加必要的 Unit Tests。
5. 添加必要的 Integration Tests。
6. 解释重要 Architecture Decision。
7. 尽可能保持 Backward Compatibility。
8. 不修改与当前 Phase 无关的代码。


每完成一个 Phase：

检查：

- Architecture 是否仍然清晰。
- 是否出现不必要的 Coupling。
- 是否增加了不必要的 Infrastructure。
- 是否仍然符合 Local-first 原则。


## 二十一、最终目标

最终的 Memory Architecture 应该大致符合：

Shovel
    │
    ├── Microsoft Agent Framework
    │
    └── Memory Layer
          │
          ├── Memory Domain
          │
          ├── Memory Application Services
          │
          ├── SQLite Persistence
          │
          ├── Zvec Semantic Index
          │
          └── Agent Framework Integration


其中：

SQLite
    =
Memory Source of Truth


Zvec
    =
Rebuildable Semantic Index


Microsoft Agent Framework
    =
Agent Runtime


Shovel Memory Layer
    =
Memory Lifecycle + Retrieval + Extraction + Consolidation


最终请始终遵循 Shovel 的核心设计原则：

“Local-first by default, extensible when needed.”

即：

默认本地运行。

默认不需要单独部署外部基础设施。

但架构上允许未来根据需要扩展到更大型的存储、Vector Database 或 Memory Provider。